"""意图识别与「意图 → 章节」路由。

普通 RAG 只按问题字面去检索；这里多做一步：先判断用户问的是用法、禁忌、
不良反应还是诊断，再定向到知识库里对应的权威章节。药物的禁忌证在说明书
的 CONTRAINDICATIONS 段，不在概述里——不知道问什么，就检索不准。
"""
from __future__ import annotations

INTENTS: list[dict] = [
    {
        "key": "interaction", "label": "药物相互作用",
        "words": ["一起吃", "同服", "同时吃", "同时服", "相互作用", "联用", "混着吃", "能不能一起", "可以一起", "合用"],
        "fields": ["drug_interactions", "drug_interaction"],
        "rel": ["interactsWith"],
    },
    {
        "key": "dosage", "label": "用法用量",
        "words": ["剂量", "吃多少", "吃几", "一天几次", "每天几次", "怎么吃", "用量", "几片", "多久吃", "多长时间", "用法",
                  "怎么用", "如何使用", "怎么服用", "怎么打", "给药", "打多少", "多久用一次"],
        "fields": ["dosage"],
        "rel": [],
    },
    {
        "key": "adverse", "label": "不良反应",
        "words": ["副作用", "不良反应", "伤肝", "伤肾", "伤胃", "危害", "有害", "黑框"],
        "fields": ["adverse_reactions", "warnings", "boxed_warning"],
        "rel": ["adverseReaction"],
    },
    {
        "key": "contraindication", "label": "禁忌与适用",
        "words": ["禁忌", "能不能用", "能用吗", "可以吃吗", "能吃吗", "孕妇", "哺乳", "哺乳期", "儿童能吃",
                  "老人能吃", "能不能吃", "可以服用吗", "不适合", "慎用"],
        "fields": ["contraindications", "boxed_warning", "warnings"],
        "rel": ["contraindicatedIn"],
    },
    {
        "key": "exam", "label": "检查与解读",
        "words": ["做什么检查", "查什么", "怎么确诊", "需要检查", "做什么化验", "报告", "指标", "化验单", "筛查"],
        "fields": ["diagnosis", "screening", "guideline"],
        "rel": ["diagnosedBy"],
    },
    {
        "key": "treatment", "label": "治疗与用药",
        "words": ["吃什么药", "用什么药", "怎么治", "如何治疗", "怎么缓解", "用药", "开点药", "治疗方法", "怎么办"],
        "fields": ["treatment", "indications", "guideline"],
        "rel": ["treatedBy", "treatedByClass"],
    },
    {
        "key": "differential", "label": "症状鉴别",
        "words": ["是不是", "可能是什么", "什么病", "怎么回事", "得了", "会不会是", "还是", "判断", "怀疑"],
        "fields": ["symptoms", "key_points", "diagnosis"],
        "rel": ["hasSymptom", "differentialWith"],
    },
    {
        "key": "lifestyle", "label": "生活方式",
        "words": ["饮食", "忌口", "注意什么", "生活习惯", "运动", "吃什么好", "调理", "预防"],
        "fields": ["prevention", "risk_factors", "key_points"],
        "rel": ["highRiskGroup"],
    },
    {
        "key": "feature", "label": "临床特征",
        "words": ["潜伏期", "病死率", "致死率", "传染性", "传染力", "重症率", "好发年龄", "易感人群"],
        "fields": ["key_points", "epidemiology", "symptoms"],
        "rel": [],
    },
    {
        "key": "cause", "label": "病因与传播",
        "words": ["为什么", "原因", "病因", "怎么引起", "传播", "传染", "病原", "宿主", "潜伏期"],
        "fields": ["etiology", "epidemiology", "key_points"],
        "rel": ["causedBy", "transmittedBy", "reservoirIn"],
    },
]

DEFAULT = {"key": "general", "label": "综合咨询",
           "fields": ["key_points", "treatment", "symptoms"], "rel": []}


def classify(question: str, entities: list[dict]) -> dict:
    q = question or ""
    hits = []   # [(score, rule)]，所有命中意图
    for rule in INTENTS:
        hit = [w for w in rule["words"] if w in q]
        if hit:
            # 命中词越长越具体，优先级越高
            hits.append((max(len(w) for w in hit), rule))

    if hits:
        hits.sort(key=lambda x: -x[0])
        primary = hits[0][1]
        # 复合问题（如「吃多少？有哪些禁忌？」）要同时取多个意图的字段，
        # 否则只按主意图取章节，另一个子问题就没资料答，模型会整体拒答。
        fields, rel, labels = [], [], []
        for _, r in hits:
            for f in r["fields"]:
                if f not in fields:
                    fields.append(f)
            for x in r["rel"]:
                if x not in rel:
                    rel.append(x)
            if r["label"] not in labels:
                labels.append(r["label"])
        return {"key": primary["key"], "label": primary["label"],
                "fields": fields, "rel": rel, "labels": labels,
                "multi": len(labels) > 1, "matched": True}

    types = {e["type"] for e in entities}
    if "Drug" in types and ("吗" in q or "能不能" in q):
        r = next(x for x in INTENTS if x["key"] == "contraindication")
        return {"key": r["key"], "label": r["label"], "fields": r["fields"], "rel": r["rel"], "matched": False}
    if "Symptom" in types:
        r = next(x for x in INTENTS if x["key"] == "differential")
        return {"key": r["key"], "label": r["label"], "fields": r["fields"], "rel": r["rel"], "matched": False}
    return {"key": DEFAULT["key"], "label": DEFAULT["label"],
            "fields": DEFAULT["fields"], "rel": [], "matched": False}
