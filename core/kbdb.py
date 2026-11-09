"""知识库层（数据库直查）：正文检索、章节详情与统计。

与中枢语料（构建时快照）不同，这里的每一次检索都是**当场 SQL**：
  disease_content  (4,953 段 / 181 万字)
  drug_content     (1,350 段 / 608 万字)
按 field 切章是源库自带的，不重新切分、不改写正文。

排序规则和问答侧一致：先用图谱把问题锚定到实体，实体名下的章节优先级最高，
其余走全文字面命中；每条结果都带 source 与命中位置。
"""
from __future__ import annotations

import json
import re

from . import browse, db, kgdb
from .store import snippet

KIND_LABEL = {"disease": "疾病章节", "drug": "药品说明书"}

_TABLES = {
    "disease": ("disease_content", "disease_id", "疾病章节"),
    "drug": ("drug_content", "drug_id", "药品说明书"),
}

_SPLIT = re.compile(r"[\s\u3000,，。;；、/|？?！!：:]+")


def _terms(q: str) -> list[str]:
    parts = [p for p in _SPLIT.split(q) if p]
    out: list[str] = []
    for p in [q.strip(), *parts]:
        if len(p) >= 2 and p not in out:
            out.append(p)
    # 长词优先，避免「糖尿病」把「妊娠期糖尿病」的分数提前
    out.sort(key=len, reverse=True)
    return out[:6]


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _count(text: str, term: str) -> int:
    return text.count(term) if term else 0


def search(q: str, kind: str = "all", field: str = "", limit: int = 30) -> dict:
    q = (q or "").strip()
    if not q:
        return {"docs": [], "total": 0, "mentions": [], "took_ms": 0, "field": field}

    import time
    t0 = time.time()

    kinds = [k for k in _TABLES if kind in ("all", k)]
    terms = _terms(q)
    if not terms:
        return {"docs": [], "total": 0, "mentions": [], "took_ms": 0, "field": field}

    mentions: list[dict] = []
    seen_m: set[str] = set()
    for term in [q, *terms]:
        for m in kgdb.search(term, limit=4):
            if m["id"] not in seen_m:
                seen_m.add(m["id"])
                mentions.append(m)
    mentions = mentions[:6]
    anchors = {m["id"]: m for m in mentions}

    found: dict[tuple, dict] = {}

    def put(row, owner_id: str, k: str, anchor_bonus: float) -> None:
        text = row["content"] or ""
        key = (owner_id, row["field"], k)
        score = anchor_bonus
        if anchors.get(owner_id):
            score += 30
        if row["owner_name"] == q:
            score += 80
        elif q in (row["owner_name"] or ""):
            score += 45
        for t in terms:
            c = _count(text, t)
            score += min(c, 5) * 6
        if score <= 0:
            return
        cur = found.get(key)
        if cur and cur["score"] >= score:
            return
        found[key] = {
            "row": row, "owner_id": owner_id, "kind": k, "score": score,
            "anchor_bonus": anchor_bonus,
        }

    with db.connect() as con:
        for k in kinds:
            table, idcol, _ = _TABLES[k]
            # 1) 图谱锚定的实体：把它名下的章节直接取出
            for owner_id in anchors:
                rows = con.execute(
                    f"SELECT c.{idcol} owner_id, e.name owner_name, e.type owner_type, "
                    f"       e.source owner_source, c.field, c.label, c.content, "
                    f"       c.char_count FROM {table} c JOIN entity e ON e.id=c.{idcol} "
                    f"WHERE c.{idcol}=?", (owner_id,)).fetchall()
                for r in rows:
                    put(r, owner_id, k, 24.0)

            # 2) 全文字面命中（多词 OR，再按命中词数打分）
            cond = " OR ".join([f"c.content LIKE ? ESCAPE '\\'" for _ in terms])
            sql = (f"SELECT c.{idcol} owner_id, e.name owner_name, e.type owner_type, "
                   f"       e.source owner_source, c.field, c.label, c.content, c.char_count "
                   f"FROM {table} c JOIN entity e ON e.id=c.{idcol} "
                   f"WHERE {cond} LIMIT 400")
            for r in con.execute(sql, [_like(t) for t in terms]):
                put(r, r["owner_id"], k, 0.0)

    items = []
    for key, hit in found.items():
        row = hit["row"]
        text = row["content"] or ""
        if field and row["field"] != field:
            continue
        sn = snippet(text, q)
        hit_n = sum(1 for t in terms if t in text)
        ascii_ratio = sum(1 for ch in text[:400] if ord(ch) < 128) / max(1, len(text[:400]))
        items.append({
            "id": f"db:{hit['kind']}:{hit['owner_id']}:{row['field']}",
            "kind": "db",
            "db_kind": hit["kind"],
            "from": f"medical_kg.db · {_TABLES[hit['kind']][0]}",
            "lang": "en" if ascii_ratio > 0.55 else "zh",
            "owner": {"id": hit["owner_id"], "name": row["owner_name"],
                      "type": row["owner_type"]},
            "field": row["field"],
            "field_label": row["label"] or row["field"],
            "title": f"{row['owner_name']} · {row['label'] or row['field']}",
            "source": row["owner_source"] or _TABLES[hit["kind"]][2],
            "chars": row["char_count"] or len(text),
            "snippet": sn["snip"], "hits": sn["hits"],
            "term_hits": hit_n,
            "score": round(hit["score"], 1),
        })

    items.sort(key=lambda x: (-x["score"], -x["term_hits"], -x["chars"]))
    items = items[:limit]

    by_field: dict[str, int] = {}
    for x in items:
        by_field[x["field"]] = by_field.get(x["field"], 0) + 1

    return {
        "query": q,
        "docs": items,
        "total": len(items),
        "mentions": mentions,
        "by_field": by_field,
        "took_ms": int((time.time() - t0) * 1000),
        "source": "medical_kg.db",
    }


def section(did: str) -> dict | None:
    """按 db:disease:DIS_X:treatment 取回整段原文。"""
    parts = (did or "").split(":")
    if len(parts) != 4 or parts[0] != "db":
        return None
    _, kind, owner, field = parts
    table_info = _TABLES.get(kind)
    if not table_info:
        return None
    table, idcol, _ = table_info
    with db.connect() as con:
        row = con.execute(
            f"SELECT c.*, e.name owner_name, e.type owner_type, e.type_label owner_type_label, "
            f"       e.source owner_source, e.aliases owner_aliases, e.url owner_url "
            f"FROM {table} c JOIN entity e ON e.id=c.{idcol} "
            f"WHERE c.{idcol}=? AND c.field=?", (owner, field)).fetchone()
    if row is None:
        return None
    keys = row.keys()
    tags = [{
        "id": owner, "name": row["owner_name"], "type": row["owner_type"],
        "type_label": row["owner_type_label"] or db.type_label(row["owner_type"]),
        "color": kgdb.category_color(row["owner_type"]),
    }]
    text = row["content"] or ""
    out = {
        "id": did,
        "title": f"{row['owner_name']} · {row['label'] or field}",
        "text": row["content"] or "",
        "kind": kind,
        "field": field,
        "field_label": row["label"] or field,
        "source": (row["source"] if "source" in keys else "") or row["owner_source"] or table_info[2],
        "year": None,
        "url": row["owner_url"] or "",
        "chars": row["char_count"] or len(row["content"] or ""),
        "table": f"medical_kg.db · {table}",
        "from_db": True,
        "tags": tags,
        "gloss": browse.FIELD_GLOSS.get(field, ""),
    }
    # 正文是英文时，把带单位的数值单独摘出来：剂量、阈值这类硬信息不该被语言挡住
    han = len(re.findall(r"[\u4e00-\u9fff]", text[:2000]))
    lat = len(re.findall(r"[A-Za-z]", text[:2000]))
    if lat > han * 3:
        out["numbers"] = browse.zh_numbers(text)
    # 药品补一段中文导读（药品登记 + 图谱关系，都是库里现成的中文事实）
    if kind == "drug":
        try:
            zh = browse.drug_zh(owner)
            if zh:
                out["zh"] = zh
            # 人工译好的中文正文（drug_content_zh）：有就用它代替英文原文显示
            pts = zh_points(owner, field)
            if pts:
                out["zh_points"] = pts
                out["zh_label"] = out["field_label"]
                nt = browse.zh_note(owner, field)
                if nt:
                    out["zh_note"] = nt
            # 标签错配：正文讲的根本是另一个药（源库已知问题），必须显式提示
            note = browse.label_note(owner, text)
            if note:
                out["suspect"] = note
        except Exception:  # noqa: BLE001 — 中文缺失不该影响正文
            pass
    return out


def zh_points(drug_id: str, field: str) -> list[str]:
    """取人工译好的中文要点；没有则返回空列表（实现在 browse 层，问答侧共用）。"""
    return browse.zh_points(drug_id, field)


def stats() -> dict:
    """知识库侧统计：两类正文的章节数与字数，按 field 展开。"""
    with db.connect() as con:
        out = []
        for kind, (table, _idcol, label) in _TABLES.items():
            total = con.execute(
                f"SELECT COUNT(*), COALESCE(SUM(char_count),0) FROM {table}").fetchone()
            fields = [{
                "field": r["field"], "label": r["label"] or r["field"],
                "count": r["n"], "chars": r["chars"] or 0,
            } for r in con.execute(
                f"SELECT field, label, COUNT(*) n, SUM(char_count) chars FROM {table} "
                f"GROUP BY field ORDER BY n DESC")]
            out.append({"kind": kind, "label": label, "table": table,
                        "sections": total[0], "chars": total[1], "fields": fields})
    return {"kinds": out, "totals": db.totals()}


def fields(limit: int = 14) -> list[dict]:
    """筛选条用：两类正文的常用章节（隐藏 x_ 开头的表格碎片字段）。"""
    with db.connect() as con:
        rows = con.execute(
            "SELECT field,label,COUNT(*) n FROM ("
            " SELECT field,label FROM disease_content UNION ALL"
            " SELECT field,label FROM drug_content) "
            "WHERE field NOT LIKE 'x\\_%' ESCAPE '\\' "
            "GROUP BY field,label HAVING n >= 10 ORDER BY n DESC LIMIT ?", (limit,)
        ).fetchall()
    return [{"id": r["field"], "label": r["label"] or r["field"], "count": r["n"]} for r in rows]
