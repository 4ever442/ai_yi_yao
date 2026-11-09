"""语义向量召回：本地 embedding + 纯标准库全库扫描。

这一层是**可选增强**，不是必需依赖：
  · 没有模型、没有 data/vec.db —— available() 返回 False，检索照旧走图谱 + 字面；
  · 有索引 —— search() 返回 [(doc_id, 余弦相似度)]，由 search.py 融合进打分。

为什么不用 numpy / FAISS：5,649 段 × 1,024 维的暴力点积实测 0.26 秒，
比一次模型生成（0.5~2 秒）低一个量级。到几十万段再谈 ANN 索引。

向量在**建库时就做 L2 归一化**，所以运行时点积即余弦，省一次开方。
"""
from __future__ import annotations

import array
import json
import math
import operator
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "vec.db"

DEFAULT_BASE = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3-embedding:0.6b"
# 建库与查询必须用同一个模型，否则向量空间对不上；改模型名等于重建
MODEL = os.environ.get("MEDKG_EMB_MODEL") or DEFAULT_MODEL
BASE = (os.environ.get("MEDKG_EMB_BASE") or DEFAULT_BASE).rstrip("/")

_LOCAL = ("127.0.0.1", "localhost", "::1", "0.0.0.0")
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 30):
    host = urllib.parse.urlsplit(url).hostname or ""
    opener = _DIRECT if host in _LOCAL else urllib.request.build_opener()
    req = urllib.request.Request(url, data=data, headers=headers or {})
    return opener.open(req, timeout=timeout)


def enabled() -> bool:
    return os.environ.get("MEDKG_VEC", "1").lower() not in ("0", "off", "false", "no")


# ------------------------------------------------------------------ 调用模型
def _norm(vec: list[float]) -> array.array:
    s = math.sqrt(sum(x * x for x in vec)) or 1.0
    return array.array("f", (x / s for x in vec))


def embed(texts: list[str], timeout: float = 120.0) -> list[array.array]:
    """批量取向量。Ollama 的 /api/embed 支持一次传多条，比逐条快一个数量级。"""
    body = json.dumps({"model": MODEL, "input": texts}, ensure_ascii=False).encode("utf-8")
    try:
        with _open(BASE + "/api/embed", data=body,
                   headers={"Content-Type": "application/json"}, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:  # noqa: BLE001
            detail = f"HTTP {exc.code}"
        raise RuntimeError(f"向量模型报错：{detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("连不上 Ollama，请确认 ollama 已在运行") from exc
    except TimeoutError as exc:
        raise RuntimeError("向量模型响应超时") from exc

    rows = data.get("embeddings") or []
    if len(rows) != len(texts):
        raise RuntimeError(f"向量条数不符：要 {len(texts)}，回 {len(rows)}")
    return [_norm(v) for v in rows]


def embed_one(text: str) -> array.array:
    return embed([text])[0]


# ------------------------------------------------------------------ 索引
class Index:
    """把 vec.db 读进内存：id 列表 + 定长 float32 向量。"""

    def __init__(self, path: Path | None = None) -> None:
        p = Path(path or DB)
        self.path = p
        self.ids: list[str] = []
        self.vecs: list[array.array] = []
        self.model = ""
        self.dim = 0
        self.built_at = ""
        if not p.is_file():
            return
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        try:
            meta = dict(con.execute("SELECT k, v FROM vec_meta").fetchall())
            self.model = meta.get("model", "")
            self.dim = int(meta.get("dim", 0) or 0)
            self.built_at = meta.get("built_at", "")
            for doc_id, blob in con.execute("SELECT doc_id, v FROM vec"):
                a = array.array("f")
                a.frombytes(blob)
                self.ids.append(doc_id)
                self.vecs.append(a)
        except sqlite3.Error:
            self.ids, self.vecs = [], []
        finally:
            con.close()

    def __len__(self) -> int:
        return len(self.ids)

    def search(self, q: array.array, top_k: int = 60) -> list[tuple[str, float]]:
        """全库暴力点积。q 已归一化，结果即余弦，范围 [-1, 1]。"""
        ids, vecs = self.ids, self.vecs
        if not ids:
            return []
        # sum(map(operator.mul, ...)) 把循环压到 C 层，比手写 for 快 2~3 倍
        mul = operator.mul
        score = [sum(map(mul, v, q)) for v in vecs]
        order = sorted(range(len(score)), key=score.__getitem__, reverse=True)[:top_k]
        return [(ids[i], score[i]) for i in order]


_index: Index | None = None

# 模型连不上时的冷却：30 秒内不再尝试，直接走原检索。
# 否则 Ollama 没开的时候，每个问题都要白等一次连接超时。
_FAIL = {"ts": 0.0}
FAIL_TTL = 30.0


def _cooling() -> bool:
    return time.time() - _FAIL["ts"] < FAIL_TTL


def index() -> Index:
    global _index
    if _index is None:
        _index = Index()
    return _index


def available() -> bool:
    """模型与索引都在才算可用。任何一项缺失都静默退回原检索。"""
    if not enabled():
        return False
    ix = index()
    if not len(ix):
        return False
    # 索引是别的模型建的，就别拿新模型的查询向量去比
    return ix.model == MODEL


def status() -> dict:
    ix = index()
    return {
        "enabled": enabled(),
        "available": available(),
        "cooling": _cooling(),
        "model": MODEL,
        "indexed": len(ix),
        "dim": ix.dim,
        "built_at": ix.built_at,
        "index_model": ix.model,
        "path": str(ix.path) if len(ix) else "",
    }


def search(question: str, top_k: int = 60) -> list[tuple[str, float]]:
    """查询侧入口。不可用时返回空列表，调用方无需分支。"""
    if not available() or _cooling():
        return []
    try:
        q = embed_one(question)
    except RuntimeError:
        # 连不上就冷静 30 秒，别让每次提问都白等一次超时
        _FAIL["ts"] = time.time()
        return []
    if len(q) != index().dim:
        return []
    return index().search(q, top_k=top_k)


def doc_text(doc: dict, fields: dict[str, str], limit: int = 900) -> str:
    """给一段文档拼建库文本：标题权重最高，其次是章节名，最后是正文。

    与字面检索的 head 取法保持一致（title ×2 + 章节 + 正文前缀），
    这样两路召回的"看到的"是同一段文字，融合时才不会互相打架。
    """
    f = fields.get(doc.get("f", ""), doc.get("f", ""))
    head = f"{doc.get('ti', '')}。{doc.get('ti', '')}。{f}。"
    return head + (doc.get("t", "") or "")[:limit]
