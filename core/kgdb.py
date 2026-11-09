"""图谱层（数据库直连）：实体链接、邻域、子图与 ECharts 载荷。

这里产出的 /api/kg/graph 与工作目录里原来的 FastAPI 后端**同一套格式**：

  nodes: [{id, name, symbolSize, category, type, isCenter}]
  links: [{source, target, label}]

没有改名、没有把 h/t 换成 source/target 之外的重构，前端拿到的就是
medical_kg.db 里 entity / triple 的原样投影。category 沿用构建报告里的
五色分类（疾病橙 / 药物蓝 / 系统灰 / 病原体紫 / 症状黄 / 其他青），
所以换一个前端（哪怕是原来的 ECharts 页面）也不用改数据。
"""
from __future__ import annotations

from . import db

# 与构建报告、原后端一致的六类配色
CATEGORIES = [
    {"name": "疾病", "color": "#e6823c", "hint": "疾病 / 综合征"},
    {"name": "药物", "color": "#3f7fc2", "hint": "药物 / 药物类别"},
    {"name": "系统部位", "color": "#8a8a8a", "hint": "解剖系统 / 身体部位"},
    {"name": "病原体", "color": "#9a6fb0", "hint": "病原体 / 毒株"},
    {"name": "症状", "color": "#e5c75b", "hint": "症状 / 并发症"},
    {"name": "其他", "color": "#5aa8a0", "hint": "地区 / 疫苗 / 检测 / 防控等"},
]

_CATEGORY_OF = {
    "Disease": 0, "Syndrome": 0,
    "Drug": 1, "DrugClass": 1,
    "System": 2, "BodyRegion": 2,
    "Pathogen": 3, "Strain": 3,
    "Symptom": 4, "Complication": 4,
}


def category_of(tid: str) -> int:
    return _CATEGORY_OF.get(tid, 5)


def category_color(tid: str) -> str:
    return CATEGORIES[category_of(tid)]["color"]


def _node(row, is_center: bool = False) -> dict:
    """节点载荷与原后端逐字段一致；度数映射到 12–70px 的符号直径。"""
    deg = row["degree"] or 1
    return {
        "id": row["id"],
        "name": row["name"],
        "symbolSize": min(70, 12 + deg // 8),
        "category": category_of(row["type"]),
        "type": row["type"],
        "isCenter": bool(is_center),
    }


def _find(con, key: str):
    key = (key or "").strip()
    if not key:
        return None
    row = con.execute(
        "SELECT id,name,type,degree FROM entity WHERE id=?", (key,)).fetchone()
    if row:
        return row
    row = con.execute(
        "SELECT id,name,type,degree FROM entity WHERE name=? ORDER BY degree DESC LIMIT 1",
        (key,)).fetchone()
    if row:
        return row
    return con.execute(
        "SELECT id,name,type,degree FROM entity WHERE name LIKE ? ORDER BY degree DESC LIMIT 1",
        (f"%{key}%",)).fetchone()


def _placeholders(items) -> str:
    return ",".join("?" * len(items))


def graph(center: str = "", limit: int = 160, hops: int = 1,
          types: list[str] | None = None, preds: list[str] | None = None) -> dict:
    """返回原生 node/link 子图。

    center 为空 → 按度数取核心子图；center 非空 → 以该实体为种子做 hops 跳邻域。
    5,997 个监测数据点默认不进图（会把结构图淹没），这与构建报告的取舍一致。
    """
    limit = max(10, min(int(limit or 160), 600))
    hops = max(1, min(int(hops or 1), 3))
    structural = db.structural_ids()
    types = [t for t in (types or []) if t]
    preds = [p for p in (preds or []) if p]

    with db.connect() as con:
        seed_row = None
        if center:
            seed_row = _find(con, center)
            if seed_row is None:
                return {"nodes": [], "links": [], "center": center, "centerId": None,
                        "truncated": False, "error": f"未找到实体：{center}",
                        "categories": CATEGORIES, "stats": db.totals()}
            seed_id = seed_row["id"]
            order = [seed_id]
            dist = {seed_id: 0}
            frontier = [seed_id]
            for depth in range(hops):
                nxt = []
                for nid in frontier:
                    rows = con.execute(
                        "SELECT subject,object FROM triple WHERE subject=? OR object=?",
                        (nid, nid)).fetchall()
                    for r in rows:
                        other = r["object"] if r["subject"] == nid else r["subject"]
                        if other in dist or other not in structural:
                            continue
                        dist[other] = depth + 1
                        nxt.append(other)
                        order.append(other)
                frontier = nxt
                if len(order) >= limit * 3:
                    break
            order = order[:limit]
            rows = {r["id"]: r for r in con.execute(
                f"SELECT id,name,type,degree FROM entity WHERE id IN ({_placeholders(order)})",
                order)}
            nodes = [_node(rows[i], i == seed_id) for i in order if i in rows]
        else:
            sql = ("SELECT id,name,type,degree FROM entity "
                   "WHERE type<>'DataPoint' AND in_graph=1")
            args: list = []
            if types:
                sql += f" AND type IN ({_placeholders(types)})"
                args += types
            sql += " ORDER BY degree DESC LIMIT ?"
            args.append(limit)
            rows = con.execute(sql, args).fetchall()
            nodes = [_node(r) for r in rows]
            order = [r["id"] for r in rows]

        nodes = [n for n in nodes if not types or n["type"] in types]
        keep = {n["id"] for n in nodes}

        links: list[dict] = []
        if keep and len(keep) <= 600:
            ids = list(keep)
            cond = f"subject IN ({_placeholders(ids)}) AND object IN ({_placeholders(ids)})"
            args = ids + ids
            if preds:
                cond += f" AND predicate IN ({_placeholders(preds)})"
                args += preds
            for r in con.execute(
                    f"SELECT subject,object,predicate_label FROM triple WHERE {cond} "
                    f"ORDER BY id", args):
                links.append({"source": r["subject"], "target": r["object"],
                              "label": r["predicate_label"] or ""})

    return {
        "nodes": nodes,
        "links": links,
        "center": (seed_row["name"] if seed_row else None),
        "centerId": (seed_row["id"] if seed_row else None),
        "hops": hops,
        "limit": limit,
        "truncated": len(nodes) >= limit,
        "categories": CATEGORIES,
        "stats": db.totals(),
    }


def search(q: str, limit: int = 12, types: list[str] | None = None) -> list[dict]:
    """实体联想：精确 > 前缀 > 包含 > 别名，同名时连接度高的靠前。"""
    q = (q or "").strip()
    if not q:
        return []
    low = q.lower()
    with db.connect() as con:
        rows = con.execute(
            "SELECT id,name,type,type_label,degree,aliases,sources FROM entity "
            "WHERE name LIKE ? OR aliases LIKE ? ORDER BY degree DESC LIMIT 400",
            (f"%{q}%", f"%{q}%")).fetchall()

    scored = []
    for r in rows:
        if types and r["type"] not in types:
            continue
        name = r["name"].lower()
        s = 0.0
        if name == low:
            s = 120
        elif name.startswith(low):
            s = 95 - (len(name) - len(low)) * 4
        elif low in name:
            s = 58
        for alias in db._json(r["aliases"], []):
            a = str(alias).lower()
            if a == low:
                s = max(s, 96)
            elif a.startswith(low):
                s = max(s, 88)
            elif low in a:
                s = max(s, 44)
        if s <= 0:
            continue
        s += min(28, r["degree"] or 0) * 0.9
        scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    return [{
        "id": r["id"], "name": r["name"], "type": r["type"],
        "type_label": r["type_label"] or db.type_label(r["type"]),
        "degree": r["degree"] or 0,
    } for _, r in scored[:limit]]


def neighbours(eid: str, limit: int = 60) -> list[dict]:
    with db.connect() as con:
        rows = con.execute(
            "SELECT t.subject,t.predicate,t.predicate_label,t.object,t.inferred,"
            "       t.source,t.evidence,t.weight,"
            "       s.name sname,s.type stype,o.name oname,o.type otype "
            "FROM triple t JOIN entity s ON s.id=t.subject JOIN entity o ON o.id=t.object "
            "WHERE (t.subject=? OR t.object=?) AND t.predicate<>'hasStatistic' LIMIT ?",
            (eid, eid, limit * 3)).fetchall()
    out = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        outbound = r["subject"] == eid
        other_id = r["object"] if outbound else r["subject"]
        key = (r["predicate"], other_id)
        if key in seen:
            # 对称谓词（如 differentialWith）在库里存了双向边，同一关系只留一条
            continue
        seen.add(key)
        out.append({
            "dir": "out" if outbound else "in",
            "rel": r["predicate"],
            "rel_label": r["predicate_label"] or db.rel_label(r["predicate"]),
            "other": other_id,
            "other_name": r["oname"] if outbound else r["sname"],
            "other_type": r["otype"] if outbound else r["stype"],
            "w": r["weight"] or 1,
            "inferred": bool(r["inferred"]),
            "src": r["source"] or "",
            "u": "",
        })
    out.sort(key=lambda x: (-x["w"], x["rel"]))
    return out[:limit]


def entity(eid: str, doc_limit: int = 12) -> dict | None:
    """实体详情，字段与中枢的 /api/entity 对齐，前端抽屉无需分支。"""
    with db.connect() as con:
        row = con.execute("SELECT * FROM entity WHERE id=?", (eid,)).fetchone()
        if row is None:
            return None
        props = db._json(row["props"], {})
        aliases = [a for a in db._json(row["aliases"], []) if isinstance(a, str)]

        system = props.get("system_name") or ""
        if not system:
            r = con.execute(
                "SELECT o.name FROM triple t JOIN entity o ON o.id=t.object "
                "WHERE t.subject=? AND t.predicate='belongsToSystem' LIMIT 1", (eid,)).fetchone()
            system = r["name"] if r else ""
        region = ""
        r = con.execute(
            "SELECT o.name FROM triple t JOIN entity o ON o.id=t.object "
            "WHERE t.subject=? AND t.predicate='locatedIn' AND o.type='BodyRegion' LIMIT 1",
            (eid,)).fetchone()
        if r:
            region = r["name"]

        docs = []
        if row["type"] == "Disease":
            for r in con.execute(
                    "SELECT field,label,char_count FROM disease_content WHERE disease_id=? "
                    "ORDER BY char_count DESC", (eid,)):
                docs.append({"id": f"db:disease:{eid}:{r['field']}", "field": r["field"],
                             "field_label": r["label"] or r["field"], "kind": "disease",
                             "source": row["source"] or "默沙东诊疗手册中文专业版",
                             "title": f"{row['name']} · {r['label'] or r['field']}",
                             "chars": r["char_count"] or 0})
        for r in con.execute(
                "SELECT field,label,char_count,source FROM drug_content WHERE drug_id=? "
                "ORDER BY char_count DESC", (eid,)):
            docs.append({"id": f"db:drug:{eid}:{r['field']}", "field": r["field"],
                         "field_label": r["label"] or r["field"], "kind": "drug",
                         "source": r["source"] or row["source"] or "openFDA 药品标签库",
                         "title": f"{row['name']} · {r['label'] or r['field']}",
                         "chars": r["char_count"] or 0})

    rel = neighbours(eid, limit=80)
    grouped: dict[str, list[dict]] = {}
    for item in rel:
        grouped.setdefault(item["rel"], []).append(item)

    return {
        "id": eid, "name": row["name"], "type": row["type"],
        "type_label": row["type_label"] or db.type_label(row["type"]),
        "type_color": category_color(row["type"]),
        "props": props, "aliases": aliases,
        "summary": row["summary"] or "",
        "system": system, "region": region,
        "degree": row["degree"] or 0,
        "source": row["source"] or "", "url": row["url"] or "",
        "clinic": False, "from_db": True,
        "relations": [
            {"rel": k, "label": v[0]["rel_label"], "items": v[:12], "total": len(v)}
            for k, v in sorted(grouped.items(), key=lambda x: -len(x[1]))
        ],
        "docs": docs[:doc_limit],
        "doc_total": len(docs),
    }


def stats() -> dict:
    """图谱侧统计：类型/谓词分布 + 总量。"""
    d = db.distribution()
    return {"totals": db.totals(), "types": d["types"],
            "predicates": d["predicates"], "systems": d["systems"],
            "categories": CATEGORIES}
