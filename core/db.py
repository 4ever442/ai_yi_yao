"""只读访问 medical_kg.db —— 图谱与知识库的唯一事实来源。

前端 → HTTP 接口 → 本模块 → SQLite。这条链路里没有第二份数据副本：
  /api/kg/*  读 entity / triple / predicate_meta
  /api/kb/*  读 disease_content / drug_content / drug_label

数据库文件只以 mode=ro 打开，服务不可能写坏它；路径可被环境变量
MEDKG_DB 覆盖，默认在「工作目录/medical_kg.db」。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

HUB = Path(__file__).resolve().parent.parent
WORKSPACE = HUB.parent


def _candidates() -> list[Path]:
    out: list[Path] = []
    env = os.environ.get("MEDKG_DB")
    if env:
        out.append(Path(env))
    out += [HUB / "data" / "medical_kg.db", WORKSPACE / "medical_kg.db", HUB / "medical_kg.db"]
    return out


DB_PATH: Path | None = next((p for p in _candidates() if p.is_file()), None)
MISSING_HINT = ("没有找到 medical_kg.db。把它放在 "
                f"{WORKSPACE} 下，或设置环境变量 MEDKG_DB 指向它。")


def available() -> bool:
    return DB_PATH is not None


def size_mb() -> float:
    return round(DB_PATH.stat().st_size / 1024 / 1024, 1) if DB_PATH else 0.0


@contextmanager
def connect():
    """每次调用开一条只读连接。SQLite 打开是微秒级的，比维护线程池更省心。"""
    if not DB_PATH:
        raise RuntimeError(MISSING_HINT)
    con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


def _json(text, fallback):
    if not text:
        return fallback
    try:
        value = json.loads(text)
        return value if isinstance(value, type(fallback)) else fallback
    except (ValueError, TypeError):
        return fallback


# ---------------------------------------------------------------- 字典（缓存）
@lru_cache(maxsize=1)
def type_meta() -> dict[str, dict]:
    with connect() as con:
        rows = con.execute("SELECT name,label,descr FROM type_meta").fetchall()
    return {r["name"]: {"label": r["label"] or r["name"], "descr": r["descr"] or ""} for r in rows}


@lru_cache(maxsize=1)
def predicate_meta() -> dict[str, dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT name,label,from_types,to_types,symmetric,note FROM predicate_meta").fetchall()
    return {r["name"]: {"label": r["label"] or r["name"], "note": r["note"] or "",
                        "from_types": r["from_types"] or "", "to_types": r["to_types"] or ""}
                    for r in rows}


def type_label(tid: str) -> str:
    return type_meta().get(tid, {}).get("label", tid)


def rel_label(rid: str) -> str:
    return predicate_meta().get(rid, {}).get("label", rid)


@lru_cache(maxsize=1)
def structural_ids() -> frozenset[str]:
    """可视化与问答的实体集合：排除 5,997 个监测数据点（它们只做统计序列，不进图）。"""
    with connect() as con:
        rows = con.execute("SELECT id FROM entity WHERE type<>'DataPoint'").fetchall()
    return frozenset(r["id"] for r in rows)


@lru_cache(maxsize=1)
def totals() -> dict:
    with connect() as con:
        entities = con.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
        triples = con.execute("SELECT COUNT(*) FROM triple").fetchone()[0]
        diseases = con.execute("SELECT COUNT(*) FROM entity WHERE type='Disease'").fetchone()[0]
        drugs = con.execute("SELECT COUNT(*) FROM entity WHERE type='Drug'").fetchone()[0]
        dc = con.execute("SELECT COUNT(*), COALESCE(SUM(char_count),0) FROM disease_content").fetchone()
        rc = con.execute("SELECT COUNT(*), COALESCE(SUM(char_count),0) FROM drug_content").fetchone()
    return {
        "entities": entities, "triples": triples,
        "structural": len(structural_ids()),
        "datapoints": entities - len(structural_ids()),
        "diseases": diseases, "drugs": drugs,
        "disease_sections": dc[0], "disease_chars": dc[1],
        "drug_sections": rc[0], "drug_chars": rc[1],
        "sections": dc[0] + rc[0], "chars": dc[1] + rc[1],
        "db_mb": size_mb(),
    }


# ---------------------------------------------------------------- 结构自检
TABLE_NOTE = {
    "entity": ("图谱 · 实体", "7,699 个节点：疾病、药物、病原体、症状、系统…"),
    "triple": ("图谱 · 关系", "12,057 条三元组，带中文谓词、证据句与来源"),
    "predicate_meta": ("图谱 · 谓词字典", "30 个谓词的标签、头尾类型约束与说明"),
    "type_meta": ("图谱 · 类型字典", "19 类实体的中文名与定义"),
    "disease_content": ("知识库 · 疾病章节", "默沙东诊疗手册正文，按 field 切章"),
    "drug_content": ("知识库 · 药品说明书", "openFDA 标签正文，按 field 切章"),
    "drug_label": ("知识库 · 药品登记", "225 种药物的中英文名、剂型与厂商"),
    "stats_monthly": ("图谱 · 监测序列", "月度发病/死亡数据点，挂在疾病实体下"),
    "merge_log": ("图谱 · 消歧记录", "18 组跨库重复实体的合并明细"),
}


@lru_cache(maxsize=1)
def schema() -> list[dict]:
    with connect() as con:
        names = [r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        out = []
        for name in names:
            cols = [{"name": r["name"], "type": r["type"] or "", "pk": bool(r["pk"])}
                    for r in con.execute(f"PRAGMA table_info({name})")]
            try:
                rows = con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            except sqlite3.Error:
                rows = 0
            role, note = TABLE_NOTE.get(name, ("内部表", ""))
            out.append({"name": name, "role": role, "note": note, "rows": rows, "columns": cols})
    return out


@lru_cache(maxsize=1)
def distribution() -> dict:
    with connect() as con:
        types = [{"id": r["type"], "label": r["type_label"] or r["type"],
                  "count": r["n"]} for r in con.execute(
            "SELECT type, type_label, COUNT(*) n FROM entity WHERE type<>'DataPoint' "
            "GROUP BY type ORDER BY n DESC")]
        preds = [{"id": r["predicate"], "label": r["predicate_label"] or r["predicate"],
                  "count": r["n"]} for r in con.execute(
            "SELECT predicate, predicate_label, COUNT(*) n FROM triple "
            "GROUP BY predicate ORDER BY n DESC")]
        systems = [{"name": r["name"], "count": r["n"]} for r in con.execute(
            "SELECT o.name, COUNT(*) n FROM triple t JOIN entity o ON o.id=t.object "
            "WHERE t.predicate='belongsToSystem' AND o.type='System' "
            "GROUP BY o.name ORDER BY n DESC")]
    return {"types": types, "predicates": preds, "systems": systems}
