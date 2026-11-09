"""混合检索：图谱锚定 + 文本召回 + 意图章节路由 + 语义向量兜底。

为什么不能只做向量/BM25 检索：
  「二甲双胍有哪些禁忌」在库里对应的正文是英文说明书的 CONTRAINDICATIONS，
  字面检索根本对不上。但图谱知道「二甲双胍」是一个药物实体，也记录了它禁用
  于哪些情况——先把问题锚定到实体，再取该实体名下的禁忌章节，就召得准。

语义向量的分工（core/vec.py，可选增强，缺模型/缺索引时整层自动静默）：
  · 补锚定 —— 用户说「拉肚子」，库里写「腹泻」，字面锚不上，向量负责把病认出来；
  · 补证据 —— 按 2:1 插进 Top-K，不动原有排序，图谱锚定的第一条永远是第一条。
  向量不负责"选章节"：同一疾病的各章节向量高度相似，章节选择仍交给意图路由。

所以每一路证据都带 path 字段，前端可以显示它是"被哪个实体带出来的"，
这让检索过程从黑箱变成可解释的路径。
"""
from __future__ import annotations

from collections import defaultdict

from .kg import graph
from .store import snippet, store
from . import vec


def candidate_diseases(symptom_ids: list[str], limit: int = 3) -> list[dict]:
    """症状 → 候选疾病（图谱多跳推理）。这是"只有症状没有病名"时的入口。"""
    score: dict[str, float] = defaultdict(float)
    hits: dict[str, list[str]] = defaultdict(list)
    for sid in symptom_ids:
        for e in graph.inc.get(sid, []):
            if e["r"] != "hasSymptom":
                continue
            h = graph.node(e["h"])
            if not h or h["t"] != "Disease":
                continue
            score[e["h"]] += e["w"]
            hits[e["h"]].append(graph.name(sid))
    if not score:
        return []
    top = max(score.values()) or 1.0
    out = [{
        "id": did, "name": graph.name(did),
        "score": round(s / top, 3), "matched": hits[did],
    } for did, s in score.items() if s >= top * 0.34]
    out.sort(key=lambda x: (-x["score"], -len(x["matched"])))
    return out[:limit]


def _anchor_scores(seeds: list[str], fields: list[str], intent_key: str) -> dict[str, tuple[float, str]]:
    """实体锚定：直接取实体名下的文档，按意图章节与归属加权。"""
    acc: dict[str, float] = defaultdict(float)
    why: dict[str, str] = {}
    hit_count: dict[str, int] = defaultdict(int)
    drug_intent = intent_key in ("dosage", "contraindication", "adverse", "interaction")
    for sid in seeds:
        sname = graph.name(sid)
        for did in store.by_entity.get(sid, []):
            d = store.by_id[did]
            s = 2.4
            if d["f"] in fields:
                s += 3.6
            elif d["f"] in UNIVERSAL_FIELDS:
                s += 2.0  # 通用章节给次一级加权，别压过意图指名的章节
            if d["o"] == sid:
                s += 1.2
            if d["k"] == "guide":
                s += 0.9
            if d["k"] == "drug":
                s += 1.6 if drug_intent else 0.2
            # 问药物的禁忌/剂量，却拿回一堆疾病章节 —— 明确压低
            if drug_intent and d["k"] == "disease" and d["f"] not in fields:
                s -= 2.2
            hit_count[did] += 1
            if s > acc[did]:
                acc[did] = s
                why[did] = sname
    for did in acc:
        acc[did] += 0.4 * min(3, hit_count[did] - 1)
    return {k: (v, why[k]) for k, v in acc.items()}


PROP_LABEL = {
    "legal_class": "法定分类", "cfr": "病死率", "incubation": "潜伏期",
    "zoonotic": "动物源性", "vaccine_preventable": "疫苗可预防",
    "department": "就诊科室", "icd10": "ICD-10", "icd11": "ICD-11",
    "en_name": "英文名", "drug_class": "药物类别", "route": "给药途径",
    "form": "剂型", "indication": "适应证", "category": "类别",
}

# 中文指南/共识不属于任何单一章节，但对任何主题都可能有价值，故视为通用章节。
UNIVERSAL_FIELDS = {"guideline"}

REL_PRIORITY = ["causedBy", "transmittedBy", "reservoirIn", "hasSymptom", "diagnosedBy",
                "treatedBy", "preventedBy", "contraindicatedIn", "interactsWith",
                "differentialWith", "highRiskGroup", "belongsToSystem"]


# ------------------------------------------------------------------ 语义层
# 补锚定的最低余弦：低于它说明"只是词面沾边"，宁可不补也别答错病。
# 0.55 是扫出来的——再高就开始损失召回，再低则放进明显不相关的实体。
MIN_ANCHOR_COS = 0.55


def _vec_hits(question: str, n: int = 12) -> list[tuple[str, float]]:
    """语义召回候选。向量层不可用（没模型 / 没索引 / 关开关）时返回空表。

    这里吞掉所有异常：向量是增强项，任何失败都只该退回原行为，
    不能让一次 embedding 超时把整个问答打成 500。
    """
    try:
        return vec.search(question, top_k=n)
    except Exception:  # noqa: BLE001
        return []


def _semantic_anchor(seeds: list[str], hits: list[tuple[str, float]]) -> list[str]:
    """向量补锚定：字面没锚上的实体，用语义命中最强的那个补进来。

    「拉肚子吃什么药」在图谱里找不到「拉肚子」，但向量能把「腹泻」的章节排到最前，
    于是这里就能补出一个正确的种子，后面全部逻辑（章节路由、档案兜底）照常生效。

    只补一个，且只接受疾病/药物：补多了会把「无力」这类泛化实体塞进锚定，
    反而稀释原有种子。相似度低于 0.55 的一律不补——扫过门槛，0.55 不影响收益，
    但能挡住「只是词面相近、其实不是这个病」的误锚（医学上答错病比答不出更糟）。
    """
    if not hits:
        return seeds
    voted: dict[str, float] = {}
    for did, cos in hits:
        d = store.by_id.get(did)
        if d and d["o"] not in voted:
            voted[d["o"]] = cos
    for eid, cos in sorted(voted.items(), key=lambda x: -x[1]):
        if cos < MIN_ANCHOR_COS:
            return seeds
        if eid in seeds:
            return seeds
        n = graph.node(eid)
        if n and n["t"] in ("Disease", "Drug"):
            return seeds + [eid]
    return seeds


def semantic(question: str, seeds: list[str],
             n: int = 12) -> tuple[list[str], list[tuple[str, float]]]:
    """一次 embed，同时给出「补好锚定的 seeds」与「语义候选」。

    问答侧（retrieve）和知识库检索侧（server.search_docs）共用这一个入口，
    保证两边对同一句话的语义理解完全一致，也避免同一问题 embed 两次。
    """
    hits = _vec_hits(question, n)
    return _semantic_anchor(seeds, hits), hits


def _semantic_fill(items: list[dict], top_k: int,
                   hits: list[tuple[str, float]]) -> list[dict]:
    """把语义命中的段落按 2:1 插进已排好的结果（b b v b b v b）。

    只插入、不改原有顺序：图谱锚定的第一条永远还是第一条——医学问答里
    「答错病」的代价远高于「少一条证据」。腾出来的位置是最末尾那一两条本来
    就靠加权进来的段落，所以不会挤掉主证据。
    """
    if not hits:
        return items
    have = {it["doc"]["id"] for it in items}
    extra: list[dict] = []
    for did, cos in hits:
        if did in have:
            continue
        d = store.by_id.get(did)
        if not d:
            continue
        have.add(did)
        extra.append({"doc": d, "score": round(2.4 * max(0.0, cos), 2),
                      "path": "vec", "why": ""})
        if len(extra) >= 4:
            break
    if not extra:
        return items
    if not items:
        return extra[:top_k]
    out: list[dict] = []
    ei = 0
    for i, it in enumerate(items):
        out.append(it)
        if (i + 1) % 2 == 0 and ei < len(extra):
            out.append(extra[ei])
            ei += 1
    while ei < len(extra):
        out.append(extra[ei])
        ei += 1
    return out[:top_k]


def profile_evidence(eid: str, rel_first: list[str] | None = None) -> dict | None:
    """实体档案：把图谱里关于这个实体的事实，组织成一段可引用的结构化证据。

    传染病图谱里 400 多个实体在正文库里没有对应章节，但它们在图谱里的属性
    和关系本身就是知识。与其让检索去撞不相关的文档，不如把图谱读成一段话。

    rel_first 让档案跟着问题走：问传播就先把传播途径挪到前面，问治疗就先说用药。
    """
    n = graph.node(eid)
    if not n:
        return None
    head = [f"{n['n']}（{graph.type_label(n['t'])}）"]
    if n.get("s"):
        head.append(n["s"].rstrip("。"))

    grouped: dict[str, list[str]] = defaultdict(list)
    for r in graph.neighbours(eid, limit=80):
        if r["other"] == eid:
            continue
        grouped[r["rel"]].append(r["other_name"])

    wanted = [r for r in (rel_first or []) if r in grouped]
    body = []
    if wanted:
        # 问题指向明确时只回它问的那一类，别把整张档案糊到用户脸上
        for rel in wanted[:2]:
            names = list(dict.fromkeys(grouped[rel]))[:5]
            body.append(f"{graph.rel_label(rel)}：{'、'.join(names)}")
    else:
        props = n.get("p") or {}
        extra = [f"{PROP_LABEL[k]}：{v}" for k, v in props.items()
                 if k in PROP_LABEL and not isinstance(v, bool)]
        if extra:
            body.append("，".join(extra[:4]))
        for rel in [r for r in REL_PRIORITY if r in grouped][:3]:
            names = list(dict.fromkeys(grouped[rel]))[:5]
            body.append(f"{graph.rel_label(rel)}：{'、'.join(names)}")

    # 内部一律用逗号，整段只留末尾一个句号：这样它是一句「可整体引用」的话
    text = "，".join(head) + "。" + "，".join(body) + "。"
    if len(text) < 12:
        return None
    return {
        "id": f"P:{eid}", "k": "profile", "o": eid, "f": "profile",
        "t": text, "src": n.get("src") or "医疗知识图谱",
        "u": n.get("u", ""), "y": None,
        "ti": f"{n['n']} · 图谱档案",
        "tg": [eid],
        "profile": True,
        "why_name": n["n"],
    }


def retrieve(
    question: str,
    seeds: list[str],
    intent: dict,
    top_k: int = 7,
    exclude: set[str] | None = None,
) -> list[dict]:
    seeds = [s for s in dict.fromkeys(seeds) if s in graph.nodes]
    # 语义层一次调用管两件事：补锚定（下面）与补证据（末尾的 _semantic_fill），
    # 这样每条问题只 embed 一次，代价约 150~250ms。
    seeds, vec_hits = semantic(question, seeds)
    fields = intent.get("fields", [])
    anchored = _anchor_scores(seeds, fields, intent["key"]) if seeds else {}

    # 意图已经指名章节，而且该章节的资料确实存在 —— 就把跑偏章节的条目挡掉。
    # 否则「问相互作用」会连适应证段落一起引进来：它对药物实体有归属分，
    # 光靠加权压不下去，只能在这里做硬闸。（图谱档案 P: 由下面单独兜底，不在此列）
    # guideline 是「中文指南/共识」，不属于任何单一章节，但讲什么主题都可能用到，故放行。
    strict_field = False
    if fields and anchored:
        allow = set(fields) | UNIVERSAL_FIELDS
        if any(store.by_id[did]["f"] in allow for did in anchored):
            anchored = {did: v for did, v in anchored.items() if store.by_id[did]["f"] in allow}
            strict_field = True

    lexical = store.bm25(question, top_k=50)
    lex_top = lexical[0]["score"] if lexical else 1.0
    lex_ids = {h["doc"]["id"] for h in lexical}

    # 通用章节按「字面也撞上了」才收：它靠归属分进来，内容却可能与本题无关
    # （问华法林相互作用，进来一段讲抗生素滥用）。这种段落留着只会稀释证据。
    if anchored:
        by_field = [did for did in anchored if store.by_id[did]["f"] not in UNIVERSAL_FIELDS]
        if by_field:
            anchored = {did: v for did, v in anchored.items()
                        if store.by_id[did]["f"] not in UNIVERSAL_FIELDS or did in lex_ids}

    pool: dict[str, dict] = {}

    def bump(did: str, add: float, path: str, why: str = "") -> None:
        cur = pool.get(did)
        if cur is None:
            pool[did] = {"doc": store.by_id[did], "score": add, "path": path, "why": why}
            return
        cur["score"] += add
        if path == "both" or cur["path"] != path:
            cur["path"] = "both"

    for did, (s, why) in anchored.items():
        bump(did, s, "graph", why)

    # 实体锚定了，但它在正文库里没有章节 —— 用图谱档案顶上，别让字面检索乱召回
    missing = [s for s in seeds if not store.by_entity.get(s)]
    for sid in missing[:2]:
        pe = profile_evidence(sid, intent.get("rel"))
        if pe:
            pool[pe["id"]] = {"doc": pe, "score": 6.4, "path": "graph", "why": graph.name(sid)}

    strong = {s for s in seeds if graph.node(s) and graph.node(s)["t"] in ("Disease", "Drug")}
    for h in lexical:
        d = h["doc"]
        f = 3.6 * (h["score"] / lex_top)
        # 已经锚定了明确的疾病/药物，却召回一个跟它毫无关系的段落 —— 多半是字面巧合
        if strong and not (set(d.get("tg", [])) & strong):
            f *= 0.35
        # 章节已经锁定，字面命中别的章节同样按巧合处理
        if strict_field and d["f"] not in fields and d["f"] not in UNIVERSAL_FIELDS:
            f *= 0.22
        # 通用章节保底进来就行，别靠字面加成反超意图指名的章节
        # （否则问「高血压吃什么药」，讲生活方式的那段会被顶到第一句）
        elif strict_field and d["f"] in UNIVERSAL_FIELDS:
            f *= 0.5
        bump(d["id"], f, "text")

    exclude = exclude or set()
    items = [v for k, v in pool.items() if k not in exclude]
    if items:
        items.sort(key=lambda x: -x["score"])
        top = items[0]["score"]
        threshold = max(1.35, 0.30 * top)
        items = [x for x in items if x["score"] >= threshold][:top_k]
    # 图谱和字面都空时，语义是最后一道兜底；有结果时按 2:1 插入
    items = _semantic_fill(items, top_k, vec_hits)

    out = []
    for i, it in enumerate(items, start=1):
        d = it["doc"]
        is_profile = bool(d.get("profile"))
        sn = snippet(d["t"], question)
        out.append({
            "eid": f"E{i}",
            "kind": "profile" if is_profile else "doc",
            "doc_id": d["id"],
            "title": d["ti"],
            "field": d["f"],
            "field_label": "图谱档案" if is_profile else store.fields.get(d["f"], d["f"]),
            "doc_kind": d["k"],
            "text": d["t"],
            "snippet": sn["snip"],
            "hits": sn["hits"],
            "source": d["src"],
            "year": d.get("y"),
            "url": d.get("u", ""),
            "tags": d.get("tg", [d["o"]]),
            "owner": d["o"],
            "owner_name": graph.name(d["o"]),
            "score": round(it["score"], 2),
            "path": it["path"],
            "why": it.get("why", ""),
        })
    return out


def fact_evidence(seeds: list[str], intent: dict, limit: int = 5) -> list[dict]:
    """图谱事实也可以作为证据：一条边就是一条可引用的事实，自带来源。"""
    rels = intent.get("rel", [])
    rows = []
    for sid in seeds:
        for e in graph.out.get(sid, []) + graph.inc.get(sid, []):
            if e["h"] == e["t"]:
                continue
            # 系统归属 / 部位这类结构性边在实体面板里展示，不作为问答证据
            if e["r"] in ("belongsToSystem", "locatedIn"):
                continue
            bonus = 2.0 if e["r"] in rels else 0.0
            rows.append((e["w"] + bonus, e))
    rows.sort(key=lambda x: -x[0])

    # 意图有明确目标关系且有命中时，只保留命中的那几类，避免"问治疗却答病因"
    if rels:
        hit = [e for _, e in rows if e["r"] in rels]
        if hit:
            rows = [(1.0, e) for e in hit]
        else:
            # 图谱里没有回答这个问题的边，那就一条都别给，免得用无关事实充数
            return []

    out, seen = [], set()
    for _, e in rows:
        key = (e["h"], e["r"], e["t"])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "kind": "fact",
            "h": e["h"], "h_name": graph.name(e["h"]),
            "r": e["r"], "r_label": graph.rel_label(e["r"]),
            "t": e["t"], "t_name": graph.name(e["t"]),
            "text": f"{graph.name(e['h'])} · {graph.rel_label(e['r'])}：{graph.name(e['t'])}。",
            "source": e["src"], "url": e.get("u", ""),
            "w": e["w"], "conf": e.get("c", "medium"),
            "clinic": bool(e.get("clinic")),
        })
        if len(out) >= limit:
            break
    return out
