"""知识库层：正文段落的加载、倒排索引与检索。

库里有两类东西：
  1. 文献正文（疾病章节、药品说明书、临床指南）—— 5600 余段，311 万字
  2. 图谱事实（三元组）—— 6000 余条

两类都能被检索、被引用、被校验，但索引方式不同：
正文用字符二元组 BM25（中文无需分词），事实用实体反查（结构化精确命中）。
"""
from __future__ import annotations

import json
import math
import re
from array import array
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

_DROP = re.compile(r"[\s\u3000，。、；：？！“”‘’（）《》【】,.;:?!\"'()\[\]{}—…·~`@#$^&*+=|\\/<>_-]+")
INDEX_CHARS = 360           # 每段只索引开头部分，主题句在这里，足够召回


def norm(text: str) -> str:
    return _DROP.sub("", (text or "").lower())


def grams(text: str) -> list[str]:
    t = norm(text)
    if len(t) < 2:
        return [t] if t else []
    return [t[i:i + 2] for i in range(len(t) - 1)]


class Store:
    def __init__(self, path: Path | None = None) -> None:
        raw = json.loads((path or DATA / "docs.json").read_text(encoding="utf-8"))
        self.docs: list[dict] = raw["docs"]
        self.fields: dict[str, str] = raw["fields"]
        self.by_id: dict[str, dict] = {d["id"]: d for d in self.docs}
        self.by_entity: dict[str, list[str]] = defaultdict(list)
        for d in self.docs:
            for tid in d.get("tg", [d["o"]]):
                self.by_entity[tid].append(d["id"])

        self.lens: list[int] = []
        self.posting: dict[str, array] = {}
        self.tfs: dict[str, array] = {}
        self.df: dict[str, int] = {}
        self._build()

        self.avg_len = sum(self.lens) / max(1, len(self.lens))
        self.n = len(self.docs)

    def _build(self) -> None:
        posting, tfs = self.posting, self.tfs
        for i, d in enumerate(self.docs):
            head = f"{d['ti']}{d['ti']}{self.fields.get(d['f'], d['f'])}{d['t'][:INDEX_CHARS]}"
            g = grams(head)
            self.lens.append(max(1, len(g)))
            for term, c in Counter(g).items():
                posting.setdefault(term, array("i")).append(i)
                tfs.setdefault(term, array("B")).append(min(c, 255))
        self.df = {k: len(v) for k, v in posting.items()}
        self.k1, self.b = 1.2, 0.62

    def _idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def bm25(self, query: str, top_k: int = 40) -> list[dict]:
        terms = list(dict.fromkeys(grams(query)))
        if not terms:
            return []
        scores: dict[int, float] = defaultdict(float)
        for term in terms:
            ids = self.posting.get(term)
            if not ids:
                continue
            idf = self._idf(term)
            tfv = self.tfs[term]
            for k in range(len(ids)):
                i = ids[k]
                f = tfv[k]
                denom = f + self.k1 * (1 - self.b + self.b * self.lens[i] / self.avg_len)
                scores[i] += idf * (f * (self.k1 + 1)) / denom
        if not scores:
            return []
        best = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
        return [{"doc": self.docs[i], "score": round(s, 3)} for i, s in best]

    def get(self, did: str) -> dict | None:
        return self.by_id.get(did)

    def of_entity(self, eid: str, fields: list[str] | None = None) -> list[dict]:
        ids = self.by_entity.get(eid, [])
        docs = [self.by_id[i] for i in ids]
        if fields:
            docs.sort(key=lambda d: (0 if d["f"] in fields else 1, -len(d["t"])))
        return docs


def snippet(text: str, query: str, width: int = 200) -> dict:
    """取命中位置附近的窗口，并给出高亮词表（前端按词做 <mark>）。"""
    plain = re.sub(r"\s+", " ", text)
    terms = [t for t in dict.fromkeys(grams(query)) if len(t) >= 2]
    low = norm(plain)
    pos = -1
    for t in terms:
        p = low.find(t)
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    if pos < 0:
        head = plain[:width]
        return {"snip": head + ("…" if len(plain) > width else ""), "hits": []}
    # norm 去掉了标点，位置会偏移，这里用原文近似回退到句首
    start = max(0, min(len(plain) - 1, int(pos * len(plain) / max(1, len(low)))) - 40)
    start = max(0, plain.rfind("。", 0, start) + 1) if start > 0 else 0
    end = min(len(plain), start + width)
    piece = plain[start:end]
    return {
        "snip": ("…" if start > 0 else "") + piece + ("…" if end < len(plain) else ""),
        "hits": [t for t in terms if t in norm(piece)],
    }


store = Store()
