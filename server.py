"""医疗知识中枢 · 融合版 HTTP 服务（仅标准库）。

一个进程同时承担四件事：静态前端、图谱查询、知识库检索、问答推理。
不引入框架是有意的：数据已经在内存/本地库里，多一层框架只是多一层部署门槛。

融合版在原有四视图之上补了三块能力（都直连 medical_kg.db）：
  · 分层浏览：部位 → 解剖系统 → 疾病，逐级下钻
  · 分类图谱：按实体类型 + 解剖系统筛选，双击节点以它为中心展开
  · 监测序列：疾病月度发病/死亡曲线（stats_monthly，5,996 个数据点）

接口：
  GET  /api/bootstrap            字典、统计、示例（首屏）
  GET  /api/suggest?q=           实体联想（顶栏搜索）
  GET  /api/entity?id=           实体详情：属性、关系、关联正文
  GET  /api/graph?ids=&hops=     中枢布局子图
  GET  /api/search?q=&kind=&field=&limit=   知识库检索（中枢语料）
  GET  /api/doc?id=              中枢语料单篇全文
  GET  /api/stats                概览页统计
  POST /api/ask                  问答（含抗幻觉校验）

  数据库直连通道（medical_kg.db，只读、格式不变）：
  GET  /api/kg/graph?center=&limit=&hops=&types=&preds=  原生 node/link（ECharts）
  GET  /api/kg/entity?id=        实体详情（DB）
  GET  /api/kg/search?q=         实体联想（DB）
  GET  /api/kg/stats             图谱分布统计（DB）
  GET  /api/kg/schema            库表结构与行数（DB）
  GET  /api/kb/search?q=&kind=&field=&limit=   知识库全文检索（DB，当场 SQL）
  GET  /api/kb/section?id=       知识库单章全文（DB）

  融合层新接口：
  GET  /api/parts                身体部位 + 病种数
  GET  /api/part?name=           部位下的系统分组与疾病清单
  GET  /api/systems              解剖系统清单（分类图谱筛选）
  GET  /api/system-graph?name=&limit=&types=   系统子图（原生 node/link）
  GET  /api/timeline?id=&metric= 疾病月度监测序列
  GET  /api/drug/label?id=       药品登记 + 说明书字段（含中英标记）
"""
from __future__ import annotations

import gzip
import json
import mimetypes
import os
import sys
import threading
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import db as db_mod                   # noqa: E402
from core import browse, feedback, kbdb, kgdb, vec     # noqa: E402
from core import ai as ai_mod                   # noqa: E402
from core import llm as llm_mod                 # noqa: E402
from core.kg import graph                       # noqa: E402
from core.search import semantic                # noqa: E402
from core.pipeline import analyze, analyze_stream, entity_detail  # noqa: E402
from core.search import profile_evidence, retrieve  # noqa: E402
from core.store import snippet, store           # noqa: E402
import core.intent as intent_mod                # noqa: E402

WEB = ROOT / "web"
UI_CONF = ROOT / "data" / "ui_profile.json"

# 搜索只认医学实体。库里还有 Region（佛山市、中山市）和 DataPoint（5,997 个统计点），
# 它们是疫情数据带进来的，用户搜「山」不该弹出地级市。
MED_TYPES = [
    "Disease", "Drug", "Symptom", "Syndrome", "Pathogen", "DrugClass",
    "Complication", "Vaccine", "DiagnosticTest", "BodyRegion", "System",
    "Strain", "Population", "Host", "Vector", "Intervention",
]

# 显示模式：青年版就是现在这套；长辈版只留接口，不做半成品。
# 真做长辈版要重排信息密度、放大点击热区、简化图谱，光调字号反而是伪需求。
UI_MODES = [
    {"id": "youth", "name": "青年版", "ready": True, "note": "默认字号与信息密度"},
    {"id": "elder", "name": "长辈版", "ready": False, "note": "大字号、高对比、简化图谱；开发中"},
]


def ui_profile() -> dict:
    mode = "youth"
    try:
        mode = (json.loads(UI_CONF.read_text(encoding="utf-8")) or {}).get("mode") or "youth"
    except (OSError, ValueError):
        pass
    if mode not in {m["id"] for m in UI_MODES if m["ready"]}:
        mode = "youth"
    return {"mode": mode, "modes": UI_MODES}


def ui_set_mode(mode: str) -> dict:
    ok = any(m["id"] == mode and m["ready"] for m in UI_MODES)
    if not ok:
        return {"ok": False, "error": "该模式尚未开放", **ui_profile()}
    try:
        UI_CONF.write_text(json.dumps({"mode": mode}, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"无法保存：{exc}", **ui_profile()}
    return {"ok": True, **ui_profile()}

EXAMPLES = [
    {"q": "二甲双胍一天吃多少？有哪些禁忌？", "tag": "用药与禁忌"},
    {"q": "最近老是口渴、多尿、体重下降，是不是糖尿病？", "tag": "症状鉴别"},
    {"q": "布洛芬和缬沙坦能一起吃吗？", "tag": "药物相互作用"},
    {"q": "登革热是怎么传播的？", "tag": "传染病图谱"},
    {"q": "新冠肺炎的潜伏期有多久？", "tag": "图谱属性"},
    {"q": "突然胸痛、呼吸困难怎么办？", "tag": "红旗症状"},
]

def llm_brief() -> dict:
    """AI 改写层状态。开关关闭时不去连模型，避免启动时白等一次探测。"""
    try:
        s = ai_mod.status()
    except Exception:  # noqa: BLE001 — 状态接口不该因为模型探测失败而 500
        return {"ok": False, "enabled": False, "model": "", "detail": "模型状态未知"}
    return {"ok": s["enabled"] and s["connected"], "enabled": s["enabled"],
            "model": s["model"], "mode": s["provider"], "base": s["base"],
            "detail": s["hint"], "ms": s["ms"]}


def db_brief() -> dict:
    info = {
        "available": db_mod.available(),
        "name": "medical_kg.db",
        "path": str(db_mod.DB_PATH) if db_mod.available() else "",
        "categories": kgdb.CATEGORIES,
        "hint": "" if db_mod.available() else db_mod.MISSING_HINT,
    }
    if db_mod.available():
        try:
            info.update(db_mod.totals())
        except Exception:  # noqa: BLE001 — 库坏了不该让整个接口 500
            info["hint"] = "数据库读取失败，图谱已切换到离线副本"
    return info


def vec_brief() -> dict:
    """语义索引状态：有几段、几维、用的哪个模型。

    缺模型或缺索引时 available=False，前端据此隐藏这一行，
    检索本身会自动退回图谱+字面，不需要任何额外处理。
    """
    try:
        return vec.status()
    except Exception:  # noqa: BLE001 — 状态接口不该让首屏 500
        return {"available": False, "enabled": False, "indexed": 0, "dim": 0,
                "model": "", "detail": "语义索引状态未知"}


def graph_fallback(center: str = "", limit: int = 160, hops: int = 1,
                   types: list[str] | None = None,
                   preds: list[str] | None = None) -> dict:
    """数据库读不到时的图谱兜底：改用 data/graph.json 里的同一批实体与关系。

    不是第二套图谱——graph.json 就是构建时从 medical_kg.db 导出的，实体和关系一致。
    有它在，「图谱」这一页就不会因为库文件缺失或路径变化而变成一片空白。
    """
    tl = graph.type_list
    idx = {t["id"]: i for i, t in enumerate(tl)}
    cats = [{"name": t["label"], "color": t["color"], "hint": t["label"]} for t in tl]
    tset, pset = set(types or []), set(preds or [])

    ok_type = lambda tid: not tset or tid in tset          # noqa: E731
    ok_pred = lambda rid: not pset or rid in pset          # noqa: E731

    if center and center in graph.nodes:
        sg = graph.subgraph([center], hops=max(1, hops), limit=max(5, limit))
        raw_nodes, raw_edges = sg["nodes"], sg["edges"]
        raw_nodes = [n for n in raw_nodes if ok_type(n["t"]) or n["id"] == center]
        keep = {n["id"] for n in raw_nodes}
        raw_edges = [e for e in raw_edges if e["h"] in keep and e["t"] in keep and ok_pred(e["r"])]
        center_id = center
    else:
        pool = [n for n in sorted(graph.nodes.values(), key=lambda x: -x["d"]) if ok_type(n["t"])]
        pool = pool[:max(5, limit)]
        keep = {n["id"] for n in pool}
        raw_nodes = [{"id": n["id"], "n": n["n"], "t": n["t"], "d": n["d"]} for n in pool]
        raw_edges = [e for e in graph.edges
                     if e["h"] in keep and e["t"] in keep and ok_pred(e["r"])]
        center_id = None

    nodes = [{
        "id": n["id"], "name": n["n"],
        "symbolSize": 10 + min(35, int(n["d"] * 1.8)),
        "category": idx.get(n["t"], 0), "type": n["t"],
        "isCenter": n["id"] == center_id,
    } for n in raw_nodes]
    ids = {n["id"] for n in nodes}
    links = [{"source": e["h"], "target": e["t"],
              "label": e.get("rl") or graph.rel_label(e["r"])}
             for e in raw_edges if e["h"] in ids and e["t"] in ids]

    stats = dict(graph.stats)
    stats.setdefault("triples", stats.get("edges", 0))
    return {
        "nodes": nodes, "links": links,
        "center": graph.name(center_id) if center_id else None,
        "centerId": center_id, "hops": hops, "limit": limit,
        "truncated": center_id is None and len(nodes) < len(graph.nodes),
        "categories": cats, "stats": stats,
        "fallback": True,
        "fallbackNote": "medical_kg.db 不可用，图谱已切换到 data/graph.json 离线副本",
    }


def bootstrap() -> dict:
    # 首屏数据必须永远能出来：数据库读不动时退化成只用中枢语料的字典，
    # 否则前端一连 bootstrap 就 500，四个视图全都起不来。
    try:
        kb_fields = kbdb.fields() if db_mod.available() else []
    except Exception:  # noqa: BLE001
        kb_fields = []
    return {
        "meta": graph.meta,
        "stats": graph.stats,
        "types": graph.type_list,
        "predicates": graph.pred_list,
        "systems": graph.systems,
        "fields": graph.fields,
        "examples": EXAMPLES,
        "intents": [{"key": i["key"], "label": i["label"]} for i in intent_mod.INTENTS],
        "db": db_brief(),
        "llm": llm_brief(),
        "vec": vec_brief(),
        "kb_fields": kb_fields,
    }


# 浏览顺序：先把「关键点」这种提纲挈领的字段放前面，再是治疗/诊断/症状
BROWSE_FIELD_ORDER = ["key_points", "symptoms", "etiology", "diagnosis", "treatment",
                      "indications", "contraindications", "dosage", "prognosis",
                      "prevention", "complications", "warnings", "adverse_reactions"]


def browse_docs(kind: str = "all", field: str = "", limit: int = 24) -> dict:
    """无关键词浏览：按实体连接度取代表章节，让知识库首页不空白。

    每个实体挑它最重要的 1~2 章（按 BROWSE_FIELD_ORDER 排），实体按图谱连接度
    降序——用户最可能查的病/药排最前。kind 限定 disease/drug/guide。
    """
    import time
    t0 = time.time()
    kinds = {"disease", "drug", "guide"}
    if kind in kinds:
        wanted = {kind}
    else:
        wanted = kinds

    # 实体 → 连接度
    deg = {}
    for n in graph.nodes.values():
        if n["t"] in ("Disease", "Drug"):
            deg[n["id"]] = n.get("d", 0) or 0

    # 每个 owner 实体挑出最优先的章节
    by_owner: dict[str, dict] = {}
    for d in store.docs:
        if d["k"] not in wanted:
            continue
        if field and d["f"] != field:
            continue
        o = d["o"]
        cur = by_owner.get(o)
        if cur is None:
            by_owner[o] = d
            continue
        # 已有更优字段就跳过；同字段取更长的
        rank_cur = BROWSE_FIELD_ORDER.index(cur["f"]) if cur["f"] in BROWSE_FIELD_ORDER else 99
        rank_new = BROWSE_FIELD_ORDER.index(d["f"]) if d["f"] in BROWSE_FIELD_ORDER else 99
        if rank_new < rank_cur or (rank_new == rank_cur and len(d["t"]) > len(cur["t"])):
            by_owner[o] = d

    # 按 owner 实体的连接度排序
    ordered = sorted(by_owner.values(),
                     key=lambda d: deg.get(d["o"], 0), reverse=True)

    # 「全部」分类下，疾病/药品/指南混着排：各自取 top，再按连接度交错，
    # 避免药品（连接度普遍高）把疾病全压下去，默认打开全是说明书。
    if kind not in kinds:
        buckets = {"disease": [], "drug": [], "guide": []}
        for d in ordered:
            buckets.setdefault(d["k"], []).append(d)
        # 各分类内部已按连接度排好，现在轮流取
        merged = []
        while True:
            added = False
            for k in ("disease", "drug", "guide"):
                if buckets.get(k):
                    merged.append(buckets[k].pop(0))
                    added = True
            if not added:
                break
        ordered = merged

    out = []
    for d in ordered[:limit]:
        # 药品说明书：改成 db: 格式 id，让前端直接走 /api/kb/section，
        # 这样能读到 drug_content_zh 里的中文译注；疾病/指南用快照 id 走 /api/doc。
        doc_id = d["id"]
        snip = d["t"][:120]
        if d["k"] == "drug" and d.get("o"):
            doc_id = f"db:drug:{d['o']}:{d['f']}"
            # 摘要优先用中文译注第一条，没有译注再退回英文原文片段
            try:
                pts = browse.zh_points(d["o"], d["f"])
                if pts:
                    snip = pts[0]
            except Exception:  # noqa: BLE001
                pass
        out.append({
            "id": doc_id, "title": d["ti"],
            "kind": "doc", "doc_kind": d["k"],
            "field": d["f"],
            "field_label": store.fields.get(d["f"], d["f"]),
            "source": d["src"], "year": d.get("y"), "url": d.get("u", ""),
            "snippet": snip,
            "hits": [],
            "chars": len(d["t"]),
            "score": round(deg.get(d["o"], 0), 2),
            "path": "browse",
            "tags": [{"id": t, "name": graph.name(t),
                      "type_label": graph.type_label(graph.nodes[t]["t"])}
                     for t in d.get("tg", [])[:4] if t in graph.nodes],
        })
    return {
        "query": "",
        "intent": None,
        "mentions": [],
        "docs": out,
        "total": len(out),
        "anchored": 0,
        "took_ms": int((time.time() - t0) * 1000),
        "browse": True,
        "browse_total": len(by_owner),
    }


def search_docs(q: str, kind: str = "all", field: str = "", limit: int = 20) -> dict:
    """知识库检索：实体名下的章节优先，其次是全库字面命中。

    这里刻意比问答更宽松——问答要克制（宁缺毋滥），而检索是给人翻的，
    多给几条相关线索比只给一条更有用。每条结果都标注它是怎么被找到的。
    """
    import time
    t0 = time.time()
    q = (q or "").strip()
    if not q:
        # 没有关键词但点了分类 → 浏览模式：按实体连接度排序，取核心章节
        return browse_docs(kind, field, limit)

    mentions = graph.link(q)
    seeds = [m["id"] for m in mentions]
    it = intent_mod.classify(q, mentions)
    # 语义通道：字面锚不上实体时（"拉肚子" → "腹泻"），先用向量把实体补出来；
    # 命中的章节以 path=vec 加入，前端单独成一组。向量不可用时两个返回值都为空。
    seeds, vec_hits = semantic(q, seeds)

    found: dict[str, dict] = {}

    def put(doc: dict, score: float, path: str) -> None:
        cur = found.get(doc["id"])
        if cur is None:
            found[doc["id"]] = {"doc": doc, "score": score, "path": path}
        else:
            cur["score"] = max(cur["score"], score) + 0.6
            if cur["path"] != path:
                cur["path"] = "both"

    for sid in seeds:
        docs = store.by_entity.get(sid) or []
        for did in docs:
            d = store.by_id[did]
            put(d, 6.0 + (4.0 if d["f"] in it.get("fields", []) else 0), "entity")
        if not docs:
            pe = profile_evidence(sid, it.get("rel"))
            if pe:
                found[pe["id"]] = {"doc": pe, "score": 7.2, "path": "profile"}

    lex = store.bm25(q, top_k=max(40, limit * 3))
    top = lex[0]["score"] if lex else 1.0
    for h in lex:
        put(h["doc"], 4.0 * (h["score"] / top), "text")

    # 语义命中：只补没被实体组/字面组收过的段落，权重压在两者之下，
    # 让它「多给线索」而不抢原有排序
    for did, cos in vec_hits:
        d = store.by_id.get(did)
        if d and did not in found:
            found[did] = {"doc": d, "score": 3.4 * max(0.0, cos), "path": "vec"}

    items = list(found.values())
    if field:
        items = [x for x in items if x["doc"]["f"] == field]
    if kind in ("doc", "profile"):
        items = [x for x in items if x["doc"].get("profile") == (kind == "profile")]
    items.sort(key=lambda x: -x["score"])
    items = items[:limit]

    out = []
    for x in items:
        d = x["doc"]
        is_profile = bool(d.get("profile"))
        sn = snippet(d["t"], q)
        # 药品说明书：改成 db: 格式 id，让前端走 /api/kb/section 读中文译注
        doc_id = d["id"]
        snip = sn["snip"]
        if d["k"] == "drug" and d.get("o"):
            doc_id = f"db:drug:{d['o']}:{d['f']}"
            # 摘要优先用中文译注第一条，没有译注再退回英文命中片段
            try:
                pts = browse.zh_points(d["o"], d["f"])
                if pts:
                    snip = pts[0]
            except Exception:  # noqa: BLE001
                pass
        out.append({
            "id": doc_id, "title": d["ti"],
            "kind": "profile" if is_profile else "doc",
            "doc_kind": d["k"],
            "field": d["f"],
            "field_label": "图谱档案" if is_profile else store.fields.get(d["f"], d["f"]),
            "source": d["src"], "year": d.get("y"), "url": d.get("u", ""),
            "snippet": snip, "hits": sn["hits"],
            "chars": len(d["t"]),
            "score": round(x["score"], 2),
            "path": x["path"],
            "tags": [{"id": t, "name": graph.name(t),
                      "type_label": graph.type_label(graph.nodes[t]["t"])}
                     for t in d.get("tg", [])[:4] if t in graph.nodes],
        })

    return {
        "query": q,
        "intent": {"key": it["key"], "label": it["label"]},
        "mentions": [{"id": m["id"], "name": m["name"], "type_label": m["type_label"]}
                     for m in mentions],
        "docs": out,
        "total": len(out),
        "anchored": sum(1 for d in out if d["path"] in ("entity", "both", "profile")),
        "took_ms": int((time.time() - t0) * 1000),
    }


def doc_detail(did: str) -> dict | None:
    # 图谱档案不是正文库里的文档，但用户点开了就应该看得到内容
    if did.startswith("P:"):
        eid = did[2:]
        n = graph.node(eid)
        if not n:
            return None
        lines = []
        # 摘要如果只是重复实体名（"登革热"），单独列一行没有意义
        if n.get("s") and len(n["s"]) > len(n["n"]) + 8:
            lines.append(n["s"])
        props = n.get("p") or {}
        plabel = {"legal_class": "法定分类", "cfr": "病死率", "incubation": "潜伏期",
                  "zoonotic": "动物源性", "vaccine_preventable": "疫苗可预防",
                  "department": "就诊科室", "icd10": "ICD-10", "drug_class": "药物类别",
                  "en_name": "英文名", "route": "给药途径", "form": "剂型"}
        for k, v in props.items():
            if k in plabel and not isinstance(v, bool):
                lines.append(f"{plabel[k]}：{v}")
        grouped: dict[str, list[str]] = {}
        for r in graph.neighbours(eid, limit=80):
            if r["other"] == eid:
                continue
            grouped.setdefault(graph.rel_label(r["rel"]), []).append(r["other_name"])
        for label, names in grouped.items():
            uniq = list(dict.fromkeys(names))[:8]
            lines.append(f"{label}：{'、'.join(uniq)}")
        return {
            "id": did, "title": f"{n['n']} · 图谱档案",
            "text": "\n".join(lines),
            "kind": "profile", "field": "profile", "field_label": "图谱档案",
            "source": n.get("src") or "医疗知识图谱", "year": None, "url": n.get("u", ""),
            "chars": len("\n".join(lines)),
            "tags": [{"id": eid, "name": n["n"], "type": n["t"],
                      "type_label": graph.type_label(n["t"])}],
        }

    d = store.get(did)
    if not d:
        return None
    tags = [{"id": t, "name": graph.name(t), "type": graph.nodes[t]["t"],
             "type_label": graph.type_label(graph.nodes[t]["t"])}
            for t in d.get("tg", []) if t in graph.nodes]
    return {
        "id": d["id"], "title": d["ti"], "text": d["t"],
        "kind": d["k"], "field": d["f"],
        "field_label": store.fields.get(d["f"], d["f"]),
        "source": d["src"], "year": d.get("y"), "url": d.get("u", ""),
        "chars": len(d["t"]), "tags": tags,
    }


def _dedupe_relations(detail: dict) -> dict:
    """对称谓词（鉴别、共现、共感染）在库里存了双向边，抽屉里同一实体只留一条。"""
    for group in detail.get("relations", []):
        seen, items = set(), []
        for item in group.get("items", []):
            key = item.get("other")
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
        group["items"] = items
        group["total"] = len(items)
    return detail


def stats_page() -> dict:
    kinds = Counter(d["k"] for d in store.docs)
    by_field = Counter(d["f"] for d in store.docs)
    by_src = Counter(d["src"] for d in store.docs)
    sys_dist = Counter()
    for n in graph.nodes.values():
        s = n.get("sys")
        if s and n["t"] in ("Disease", "Symptom", "Drug"):
            sys_dist[s] += 1
    covered = sum(1 for n in graph.nodes.values() if graph.nodes and store.by_entity.get(n["id"]))
    return {
        "totals": {
            "entities": len(graph.nodes),
            "edges": len(graph.edges),
            "docs": len(store.docs),
            "doc_chars": sum(len(d["t"]) for d in store.docs),
            "diseases": len(graph.by_type.get("Disease", [])),
            "drugs": len(graph.by_type.get("Drug", [])),
            "systems": len(graph.systems),
            "sources": len(by_src),
            "covered": covered,
        },
        "kinds": [{"id": k, "label": {"disease": "疾病章节", "drug": "药品说明书",
                                      "guide": "临床指南", "profile": "图谱档案"}.get(k, k), "count": v}
                  for k, v in kinds.most_common()],
        "fields": [{"id": k, "label": store.fields.get(k, k), "count": v}
                   for k, v in by_field.most_common(12)],
        "sources": [{"name": k, "count": v} for k, v in by_src.most_common(10)],
        "systems": [{"name": k, "count": v} for k, v in sys_dist.most_common()],
        "types": [{"id": t["id"], "label": t["label"], "color": t["color"], "count": t["count"]}
                  for t in graph.type_list],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "MedHub/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    # -------------------------------------------------- 响应
    def _send(self, body: bytes, ctype: str, code: int = 200, cache: bool = False) -> None:
        gz = False
        if len(body) > 1200 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, 6)
            gz = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Cache-Control", "public, max-age=0, must-revalidate" if cache else "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200, cache: bool = False) -> None:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", code, cache)

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"error": msg}, code)

    def _sse(self, gen) -> None:
        """SSE 流式响应：HTTP/1.1 chunked 编码，逐块把事件写回前端。

        事件格式：`data: <json>\n\n`，前端用 fetch + ReadableStream 读取。
        客户端断开时静默结束（BrokenPipeError / ConnectionResetError）。
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for kind, obj in gen:
                payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                # SSE 事件：data 行 + 空行分隔
                chunk = (f"event: {kind}\n" + "data: " + payload.decode("utf-8") + "\n\n").encode("utf-8")
                # 手动 chunked 编码：hex长度 + \r\n + 数据 + \r\n
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                self.wfile.write(chunk + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    def _file(self, path: Path) -> None:
        if not path.is_file():
            self._err("资源不存在", 404)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix in (".html", ".css", ".js", ".json", ".svg"):
            ctype += "; charset=utf-8"
        self._send(path.read_bytes(), ctype, 200, cache=True)

    # -------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)

        def arg(k: str, default: str = "") -> str:
            return (qs.get(k) or [default])[0]

        def arg_list(k: str) -> list[str]:
            return [x for x in arg(k).split(",") if x]

        try:
            if p == "/api/bootstrap":
                self._json(bootstrap(), cache=True)
                return

            # ---------------------------------------------- 数据库直连通道
            if p.startswith("/api/kg/") or p.startswith("/api/kb/"):
                # 图谱有离线兜底，所以放在「数据库是否可用」这道门之前判断：
                # 库丢了也要能看到图，只是数据换成 data/graph.json 里的那份。
                if p == "/api/kg/graph":
                    lim = int(arg("limit", "160") or 160)
                    hp = int(arg("hops", "1") or 1)
                    res = None
                    if db_mod.available():
                        try:
                            res = kgdb.graph(arg("center"), lim, hp,
                                             arg_list("types"), arg_list("preds"))
                        except Exception:  # noqa: BLE001 — 库损坏/被占用都走兜底
                            res = None
                    if not res or res.get("error"):
                        res = graph_fallback(arg("center"), lim, hp,
                                             arg_list("types"), arg_list("preds"))
                    self._json(res)
                    return
                if not db_mod.available():
                    self._err(db_mod.MISSING_HINT, 503)
                    return
                if p == "/api/kg/entity":
                    d = kgdb.entity(arg("id"))
                    if not d:
                        self._err("实体不存在", 404)
                        return
                    self._json(d)
                    return
                if p == "/api/kg/search":
                    self._json({"items": kgdb.search(arg("q"),
                                                     int(arg("limit", "10") or 10),
                                                     arg_list("types") or MED_TYPES)})
                    return
                if p == "/api/kg/stats":
                    try:
                        self._json(kgdb.stats(), cache=True)
                    except Exception:  # noqa: BLE001 — 库读不动就退回语料统计
                        self._json({**stats_page(), "fallback": True}, cache=True)
                    return
                if p == "/api/kg/schema":
                    self._json({"totals": db_mod.totals(),
                                "tables": db_mod.schema(),
                                "distribution": db_mod.distribution()}, cache=True)
                    return
                if p == "/api/kb/search":
                    self._json(kbdb.search(arg("q"), arg("kind", "all"), arg("field"),
                                           int(arg("limit", "30") or 30)))
                    return
                if p == "/api/kb/section":
                    d = kbdb.section(arg("id"))
                    if not d:
                        self._err("章节不存在", 404)
                        return
                    self._json(d)
                    return
                if p == "/api/kb/stats":
                    try:
                        self._json(kbdb.stats(), cache=True)
                    except Exception:  # noqa: BLE001
                        self._json({"fallback": True, "docs": len(store.docs)}, cache=True)
                    return

            if p == "/api/suggest":
                self._json({"items": graph.search(arg("q"), limit=int(arg("limit", "10") or 10),
                                                  types=MED_TYPES)})
                return
            if p == "/api/entity":
                eid = arg("id")
                detail = entity_detail(eid)
                if not detail and db_mod.available():
                    # 中枢语料里没有的实体，回落到数据库里的原始记录
                    detail = kgdb.entity(eid)
                if not detail:
                    self._err("实体不存在", 404)
                    return
                self._json(_dedupe_relations(detail))
                return
            if p == "/api/graph":
                ids = [i for i in arg("ids").split(",") if i]
                hops = int(arg("hops", "2") or 2)
                sub = graph.subgraph(ids, hops=hops, limit=int(arg("limit", "70") or 70))
                self._json({**sub, "relation_label": graph.predicates})
                return
            if p == "/api/search":
                self._json(search_docs(arg("q"), arg("kind", "all"), arg("field"),
                                       int(arg("limit", "20") or 20)))
                return
            if p == "/api/doc":
                d = doc_detail(arg("id"))
                if not d:
                    self._err("文档不存在", 404)
                    return
                self._json(d)
                return
            if p == "/api/stats":
                self._json(stats_page(), cache=True)
                return
            if p == "/api/status":
                info = {"ok": True, "entities": len(graph.nodes), "docs": len(store.docs),
                        "db": db_brief(), "llm": llm_brief()}
                self._json(info)
                return

            # ---------------------------------------------- AI 接入层（预留位）
            if p == "/api/ui/profile":
                self._json(ui_profile())
                return
            if p == "/api/ai/status":
                self._json(ai_mod.status(force=arg("probe") == "1"))
                return

            # ---------------------------------------------- 融合层：分层浏览 / 序列 / 药品标签
            # 这一组全部直连数据库，和 /api/kg/* 同一条通道；库不可用时明确报 503，
            # 不像图谱那样有离线兜底——分层浏览离开实体表就没有意义。
            if p.startswith("/api/parts") or p.startswith("/api/part") or \
                    p in ("/api/systems", "/api/system-graph") or \
                    p.startswith("/api/timeline") or p.startswith("/api/drug/"):
                if not db_mod.available():
                    self._err(db_mod.MISSING_HINT, 503)
                    return
                if p == "/api/parts":
                    self._json(browse.parts(), cache=True)
                    return
                if p == "/api/part":
                    self._json(browse.part(arg("name")))
                    return
                if p == "/api/part-graph":
                    self._json(browse.part_graph(
                        arg("name"), int(arg("limit", "24") or 24),
                        int(arg("symptoms", "20") or 20)))
                    return
                if p == "/api/systems":
                    self._json(browse.systems(), cache=True)
                    return
                if p == "/api/system-graph":
                    self._json(browse.system_graph(
                        arg("name"), int(arg("limit", "60") or 60), arg_list("types")))
                    return
                if p == "/api/timeline":
                    self._json(browse.timeline(arg("id"), arg("name"),
                                               arg("metric", "发病数")))
                    return
                if p == "/api/drug/label":
                    self._json(browse.drug_label(arg("id")))
                    return
        except (ValueError, KeyError) as exc:
            self._err(f"参数错误：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            # 任何未预料的异常都要有响应体，否则浏览器只会看到 ERR_EMPTY_RESPONSE，
            # 前端拿不到原因，页面就静默空白了。
            self._err(f"服务内部错误：{exc}", 500)
            return

        if p in ("/", "/home.html"):
            self._file(WEB / "home.html")
            return
        # 旧地址 /index.html 保留，避免有人存了书签；内容就是四视图主界面
        if p in ("/app", "/app.html", "/index.html"):
            self._file(WEB / "app.html")
            return
        target = (WEB / p.lstrip("/")).resolve()
        if WEB.resolve() in target.parents and target.is_file():
            self._file(target)
            return
        self._err("资源不存在", 404)

    # -------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except (ValueError, json.JSONDecodeError):
            self._err("请求体不是合法 JSON")
            return

        if u.path == "/api/ui/profile":
            self._json(ui_set_mode((payload or {}).get("mode") or ""))
            return

        if u.path == "/api/ai/config":
            try:
                saved = ai_mod.save({
                    "enabled": payload.get("enabled"),
                    "model": payload.get("model"),
                    "base": payload.get("base"),
                })
                self._json({"ok": True, "settings": saved,
                            "status": ai_mod.status(force=True)})
            except (ValueError, OSError) as exc:
                self._err(f"保存失败：{exc}", 500)
            return

        if u.path == "/api/ask/stream":
            q = str(payload.get("question", "")).strip()
            if not q:
                self._err("问题不能为空")
                return
            if len(q) > 500:
                self._err("问题过长，请精简到 500 字以内")
                return
            try:
                gen = analyze_stream(q,
                                     deep_think=bool(payload.get("deep_think")),
                                     kb_only=bool(payload.get("kb_only", True)),
                                     history=payload.get("history") or [])
                self._sse(gen)
            except ValueError as exc:
                self._err(str(exc))
            except Exception as exc:  # noqa: BLE001
                self._err(f"服务内部错误：{exc}", 500)
            return

        if u.path == "/api/ask":
            q = str(payload.get("question", "")).strip()
            if not q:
                self._err("问题不能为空")
                return
            if len(q) > 500:
                self._err("问题过长，请精简到 500 字以内")
                return
            try:
                res = analyze(q,
                              deep_think=bool(payload.get("deep_think")),
                              kb_only=bool(payload.get("kb_only", True)),
                              history=payload.get("history") or [])
            except ValueError as exc:
                self._err(str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                self._err(f"服务内部错误：{exc}", 500)
                return
            # 前端不需要整段正文重复传输
            for e in res["evidence"]:
                e["text_len"] = len(e.get("text", ""))
                if len(e.get("text", "")) > 400:
                    e["text"] = e["text"][:400]
            self._json(res)
            return

        if u.path == "/api/feedback":
            q = str(payload.get("question", "")).strip()
            a = str(payload.get("answer", "")).strip()
            vote = str(payload.get("vote", "")).strip()
            if not q or not a:
                self._err("问题或答案不能为空")
                return
            if vote not in ("good", "bad", "none"):
                self._err("vote 需为 good / bad / none")
                return
            try:
                out = feedback.save(q, a, vote, bool(payload.get("out_of_kb")))
                self._json(out)
            except Exception as exc:  # noqa: BLE001
                self._err(f"保存失败：{exc}", 500)
            return

        self._err("接口不存在", 404)


class QuietServer(ThreadingHTTPServer):
    """浏览器提前断开是常态（关标签页、切换请求），不要往控制台刷栈。"""
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def _bind(host: str, port: int) -> ThreadingHTTPServer:
    for p in range(port, port + 20):
        try:
            return QuietServer((host, p), Handler)
        except OSError:
            continue
    raise SystemExit(f"端口 {port}-{port + 19} 均被占用")


def _setup_stdout() -> None:
    """控制台是 UTF-8（65001）就用 UTF-8，否则跟随系统代码页（中文 Windows 是 GBK）。

    直接写死 UTF-8 会让 936 代码页的 cmd 窗口把启动横幅打成乱码。
    """
    try:
        enc = "utf-8"
        if os.name == "nt" and sys.stdout.isatty():
            try:
                import ctypes

                if ctypes.windll.kernel32.GetConsoleOutputCP() != 65001:
                    enc = "gbk"
            except Exception:
                enc = "gbk"
        sys.stdout.reconfigure(encoding=enc, errors="replace")
    except Exception:
        pass


def main() -> None:
    _setup_stdout()
    # 云端部署：监听 0.0.0.0 并从 PORT 环境变量读端口（Vercel/Render 等平台约定）。
    # 本地运行：MEDHUB_PORT 仍可覆盖，默认 8100。
    host = os.environ.get("MEDHUB_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("MEDHUB_PORT", "8100")))
    httpd = _bind(host, port)
    url = f"http://{host}:{httpd.server_address[1]}"
    print("=" * 64)
    print("  医疗知识中枢 · 融合版（首页 / 问答 / 知识库 / 图谱 / 浏览）")
    print(f"  访问地址 : {url}")
    print(f"  首页     : {url}/            （卡片入口 + 外观设置）")
    print(f"  主界面   : {url}/app.html    （问答 / 知识库 / 图谱 / 浏览 / 概览）")
    print(f"  人体导航 : {url}/catalog.html （按部位进入图谱）")
    if db_mod.available():
        try:
            t = db_mod.totals()
        except Exception as exc:  # noqa: BLE001 — 库损坏也照样起服务，图谱走离线副本
            print(f"  [!] 数据库读取失败（{exc}），图谱将使用 data/graph.json 离线副本")
            t = None
        if t:
            print(f"  知识图谱 : {t['entities']} 实体 / {t['triples']} 关系 "
                  f"（其中 {t['structural']} 个结构实体进图）")
            print(f"  知识库   : {t['sections']} 段 / {t['chars']/10000:.0f} 万字 "
                  f"（disease_content + drug_content，直接读 SQL）")
            print(f"  数据库   : {db_mod.DB_PATH}")
    else:
        print(f"  [!] {db_mod.MISSING_HINT}")
        print("  [!] 图谱将使用 data/graph.json 离线副本")
    print(f"  中枢语料 : {len(store.docs)} 段 / "
          f"{sum(len(d['t']) for d in store.docs)/10000:.0f} 万字")
    info = llm_brief()
    if info["ok"]:
        print(f"  云端模型 : {info['model']} 已接入（{info['base']}）")
        print("             只负责把证据改写成中文要点，引用与事实仍由检索层决定")
    else:
        print(f"  云端模型 : 未使用 —— {info['detail']}")
        print("             问答会自动改用证据摘录模式，其余功能不受影响")
    print("=" * 64)
    if "--no-browser" not in sys.argv:
        # 默认落到主目录：那里是「从身体进入图谱」的入口，点部位即跳图谱对应位置
        # 打开首页：四个入口都在那里，别一上来就落到人体图
        threading.Timer(0.9, lambda: webbrowser.open(url + "/")).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        httpd.shutdown()


if __name__ == "__main__":
    main()
