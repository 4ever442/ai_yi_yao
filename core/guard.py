"""抗幻觉校验层。

不相信模型的自我评价，只相信可核验的东西。每一条医学断言都要过四关：

  1. 引用校验   —— 必须带 [En]，且编号在本次证据集里真实存在
  2. 蕴含校验   —— 断言里的实体、数值、语义片能在被引证据里找到支点
  3. 图谱冲突   —— 与图谱记录的禁忌、相互作用、剂量上限矛盾
  4. 置信度门控 —— 四项指标合成可解释分数，再决定答、慎答还是拒答

图谱在这里承担了双重角色：既是生成时的约束，也是校验时的反证来源。
"""
from __future__ import annotations

import re
from collections import defaultdict

from .kg import graph

RED_FLAGS = [
    "胸痛", "胸闷", "呼吸困难", "喘不上气", "口唇发紫", "意识模糊", "昏迷", "晕厥",
    "抽搐", "口角歪斜", "言语不清", "一侧肢体无力", "呕血", "黑便", "咯血",
    "大出血", "窒息", "剧烈腹痛", "高热不退", "自杀", "服毒", "抽搐不止",
]

NEG_WORDS = ["不推荐", "不建议", "不宜", "不应", "不能", "不可", "禁用", "禁忌", "避免",
             "无效", "切勿", "无需", "不要", "慎用", "不主张"]
POS_WORDS = ["推荐", "首选", "建议使用", "可以使用", "可以吃", "可以服用", "可用", "适用",
             "有效", "适合", "一起吃", "同时服用", "联用", "安全"]

OVERCLAIM = ["您患有", "可以确诊", "确诊为", "一定是", "肯定是", "百分之百", "保证治愈", "包治"]

# 单日剂量硬上限（mg）：用于抓「剂量翻倍」型幻觉
MAX_DAILY_MG = {
    "DRG_METFORMIN": 2000, "DRG_IBUPROFEN": 1200, "DRG_ACETAMINOPHEN": 2000,
    "DRG_AMLODIPINE": 10, "DRG_VALSARTAN": 320, "DRG_ATORVASTATIN": 80,
    "DRG_ALLOPURINOL": 800, "DRG_OMEPRAZOLE": 40, "DRG_LORATADINE": 10,
    "DRG_SUMATRIPTAN": 200, "dr_paracetamol": 2000, "dr_ibuprofen": 1200,
    "dr_metformin": 2000, "dr_amlodipine": 10, "dr_valsartan": 320,
    "dr_atorvastatin": 80, "dr_allopurinol": 800, "dr_omeprazole": 40,
    "dr_loratadine": 10, "dr_sumatriptan": 200,
}

BLOCKING_UNITS = {"mg", "g", "ml", "μg", "ug", "iu", "mmol/l", "μmol/l", "mmhg", "g/l", "%"}

_NUM_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mg|g|ml|mL|μg|ug|IU|mmol/L|μmol/L|mmHg|g/L|%|小时|天|周|次|片|粒)",
    re.IGNORECASE,
)
_CITE_RE = re.compile(r"\[\s*(E\d+)\s*\]")
_DROP = re.compile(r"[\s\u3000，。、；：？！“”‘’（）《》【】,.;:?!\"'()\[\]{}—…·~`@#$^&*+=|\\/<>_-]+")


def norm(text: str) -> str:
    return _DROP.sub("", (text or "")).lower()


def _grams(text: str) -> list[str]:
    t = norm(text)
    if len(t) < 2:
        return [t] if t else []
    return [t[i:i + 2] for i in range(len(t) - 1)]


def _clean(text: str) -> str:
    t = text.replace("**", "").replace("##", "").strip()
    t = re.sub(r"^[-*•·]\s*", "", t)
    t = re.sub(r"^\d+[.、)]\s*", "", t)
    return t.strip()


def split_claims(answer: str) -> list[str]:
    text = answer.replace("\r", "")
    raw = re.split(r"(?<=[。！？；\n])", text)
    claims: list[str] = []
    for piece in raw:
        c = _clean(piece)
        if not c:
            continue
        bare = _CITE_RE.sub("", c).strip(" 。；，、\n")
        if not bare and _CITE_RE.search(c):
            # 只有引用标记的碎片回贴到上一句
            if claims:
                claims[-1] = claims[-1].rstrip() + c
            continue
        if len(norm(c)) < 4:
            continue
        claims.append(c)
    return claims


def extract_numbers(text: str) -> list[dict]:
    out = []
    for m in _NUM_RE.finditer(text):
        unit = m.group(2).lower()
        out.append({"raw": m.group(0).strip(), "value": float(m.group(1)),
                    "unit": unit, "blocking": unit in BLOCKING_UNITS})
    return out


def _doc_of(item: dict) -> dict:
    return item.get("doc", item)


def _evidence_text(item: dict) -> str:
    d = _doc_of(item)
    if d.get("kind") == "fact":
        return f"{d.get('h_name','')} {d.get('r_label','')} {d.get('t_name','')}"
    # 已译成中文的说明书章节：用译文比对译文。
    # 拿中文答案去英文原文里找词，必然处处「找不到支点」，那是误判不是缺陷。
    if d.get("zh_points"):
        return " ".join(d["zh_points"])
    return d.get("text", "")


def _evidence_tags(item: dict) -> set[str]:
    d = _doc_of(item)
    if d.get("kind") == "fact":
        return {d.get("h", ""), d.get("t", "")}
    return set(d.get("tags", [d.get("owner", "")]))


def _entity_supported(eid: str, cited: list[dict]) -> bool:
    n = graph.node(eid)
    if not n:
        return False
    forms = [n["n"], *n.get("a", [])]
    for it in cited:
        if eid in _evidence_tags(it):
            return True
        cn = norm(_evidence_text(it))
        if any(f and norm(f) in cn for f in forms):
            return True
    return False


def _kg_conflicts(claim: str, entities: list[dict]) -> list[dict]:
    """在图谱里找与断言矛盾的既有事实。方向按谓词定义来，不靠猜。"""
    conflicts: list[dict] = []
    diseases = [e["id"] for e in entities if e["type"] == "Disease"]
    drugs = [e["id"] for e in entities if e["type"] == "Drug"]
    positive = any(w in claim for w in POS_WORDS)
    negative = any(w in claim for w in NEG_WORDS)

    if positive and not negative:
        for g in drugs:
            for d in diseases:
                if graph.has_triple(g, "contraindicatedIn", d):
                    conflicts.append({
                        "type": "禁忌用药",
                        "detail": f"图谱记录「{graph.name(g)}」禁用于「{graph.name(d)}」，"
                                  f"而回答建议使用。",
                        "h": g, "t": d,
                    })
            for e in graph.out.get(g, []):
                if e["r"] == "adverseReaction" and e["t"] in {x["id"] for x in entities}:
                    conflicts.append({
                        "type": "不良反应",
                        "detail": f"图谱记录「{graph.name(g)}」可导致「{graph.name(e['t'])}」，"
                                  f"回答中未做提示。",
                    })
        for i, g1 in enumerate(drugs):
            for g2 in drugs[i + 1:]:
                if graph.has_triple(g1, "interactsWith", g2) or graph.has_triple(g2, "interactsWith", g1):
                    conflicts.append({
                        "type": "相互作用",
                        "detail": f"图谱记录「{graph.name(g1)}」与「{graph.name(g2)}」存在相互作用，"
                                  f"回答却建议同时使用。",
                    })

    for g in drugs:
        cap = MAX_DAILY_MG.get(g)
        if not cap:
            continue
        for n in extract_numbers(claim):
            if n["unit"] == "mg" and n["value"] > cap:
                conflicts.append({
                    "type": "剂量超限",
                    "detail": f"「{graph.name(g)}」单日剂量上限 {cap:g}mg，"
                              f"回答中出现 {n['raw']}。",
                })
    return conflicts


def _overclaim(claim: str) -> list[dict]:
    hits = [w for w in OVERCLAIM if w in claim]
    if not hits:
        return []
    return [{"type": "确定性表述",
             "detail": f"出现「{hits[0]}」这类确定性诊断表述，医疗问答中应改为概率化表述。"}]


def verify_claim(claim: str, evidence: list[dict]) -> dict:
    by_id = {e["eid"]: e for e in evidence}
    cites = list(dict.fromkeys(_CITE_RE.findall(claim)))
    valid = [c for c in cites if c in by_id]
    plain = _clean(_CITE_RE.sub("", claim)).strip(" 。；，、\n")

    entities = graph.link(plain)
    numbers = extract_numbers(plain)
    medical = bool(entities or numbers)

    res = {
        "text": claim, "plain": plain,
        "citations": cites, "valid_citations": valid,
        "medical": medical,
        "entities": [{"id": e["id"], "name": e["name"], "type": e["type_label"]} for e in entities],
        "numbers": [n["raw"] for n in numbers],
        "support": 0.0, "status": "skip", "issues": [], "level": "ok",
    }

    if not medical:
        res["note"] = "非医学断言（引导语/免责声明），不参与校验"
        return res
    if not cites:
        res["status"] = "no_citation"
        res["level"] = "fail"
        res["issues"].append("该句是医学断言，但没有标注任何证据编号")
        return res
    if not valid:
        res["status"] = "invalid_citation"
        res["level"] = "fail"
        res["issues"].append(f"引用 {', '.join(cites)} 不在本次证据集中，属于虚构引用")
        return res

    cited = [by_id[c] for c in valid]
    cited_text = " ".join(_evidence_text(it) for it in cited)

    if entities:
        hit = [e for e in entities if _entity_supported(e["id"], cited)]
        ent_cov = len(hit) / len(entities)
        missing = [e["name"] for e in entities if e not in hit]
    else:
        ent_cov, missing = 1.0, []

    cn = norm(cited_text)
    if numbers:
        ok = [n for n in numbers
              if norm(n["raw"]) in cn or str(n["value"]).rstrip("0").rstrip(".") in cn]
        num_cov = len(ok) / len(numbers)
        bad_nums = [n["raw"] for n in numbers if n not in ok]
    else:
        num_cov, bad_nums = 1.0, []

    a, b = set(_grams(plain)), set(_grams(cited_text))
    overlap = len(a & b) / max(1, len(a))

    support = 0.45 * ent_cov + 0.25 * num_cov + 0.30 * min(1.0, overlap / 0.30)
    res["support"] = round(min(1.0, support), 3)
    res["overlap"] = round(overlap, 3)

    if missing:
        res["issues"].append(f"「{missing[0]}」在被引证据里找不到支点")
    if bad_nums:
        res["issues"].append(f"数值「{bad_nums[0]}」未在被引证据中出现")

    conflicts = _kg_conflicts(plain, entities) + _overclaim(plain)
    if conflicts:
        res["conflicts"] = conflicts
        res["issues"] += [f"{c['type']}：{c['detail']}" for c in conflicts]
        res["level"] = "fail"
        res["status"] = "conflict"
        return res

    if res["support"] >= 0.60:
        res["status"], res["level"] = "pass", "ok"
    elif res["support"] >= 0.42:
        res["status"], res["level"] = "weak", "warn"
        res["issues"].append("证据支点偏弱，可能包含推断性表述")
    else:
        res["status"], res["level"] = "unsupported", "fail"
        res["issues"].append("断言与所引证据不匹配，疑似无依据生成")
    return res


def aggregate(verdicts: list[dict], retrieval_score: float, has_evidence: bool) -> dict:
    checked = [v for v in verdicts if v["medical"]]
    n = len(checked) or 1
    cited_ok = sum(1 for v in checked if v["valid_citations"])
    supported = sum(1 for v in checked if v["status"] == "pass")
    weak = sum(1 for v in checked if v["status"] == "weak")
    conflicts = [c for v in checked for c in v.get("conflicts", [])]
    bad_nums = sum(1 for v in checked for i in v["issues"] if i.startswith("数值"))

    cited_rate = cited_ok / n if checked else 1.0
    support_rate = (supported + 0.5 * weak) / n if checked else 1.0
    retr = max(0.0, min(1.0, retrieval_score))

    score = 0.34 * cited_rate + 0.34 * support_rate + 0.16 * retr + 0.16 * (1 - min(1.0, len(conflicts) / 2))
    if conflicts:
        score *= 0.55
    if bad_nums:
        score *= 0.70
    if not has_evidence:
        score = 0.0
    score = round(max(0.0, min(1.0, score)), 3)

    if score >= 0.72:
        level, label = "high", "可回答"
    elif score >= 0.45:
        level, label = "medium", "谨慎回答"
    else:
        level, label = "low", "证据不足"

    return {
        "claims": verdicts,
        "metrics": {
            "claims_total": len(verdicts),
            "claims_checked": len(checked),
            "cited_rate": round(cited_rate, 3),
            "support_rate": round(support_rate, 3),
            "retrieval_score": round(retr, 3),
            "conflicts": len(conflicts),
            "unsupported_numbers": bad_nums,
        },
        "conflicts": conflicts,
        "confidence": score,
        "level": level,
        "level_label": label,
    }


def verify_answer(answer: str, evidence: list[dict], retrieval_score: float) -> dict:
    return aggregate([verify_claim(c, evidence) for c in split_claims(answer)],
                     retrieval_score, bool(evidence))


def detect_red_flags(text: str) -> list[str]:
    return [w for w in RED_FLAGS if w in text]
