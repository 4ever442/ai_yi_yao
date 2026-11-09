"""分层浏览 / 分类图谱 / 监测序列 / 药品标签 —— 融合版新增的四个数据通道。

这一层全部直连 medical_kg.db（只读），不经过构建期快照：
  /api/parts         一级：身体部位 + 病种数
  /api/part          二级：某部位下的解剖系统分组与疾病清单
  /api/systems       解剖系统清单（分类图谱的筛选条件）
  /api/system-graph  系统子图：该系统疾病 + 一跳邻居，规模可控
  /api/timeline      疾病月度监测序列（发病数 / 死亡数）
  /api/drug/label    药品登记信息 + 说明书可用字段（含中英文标记）

与旧版 ai_tp 的差别：筛选条件一律下推到 SQL，不把整张 triple 表拉进 Python
再逐行判断。triple 上有 idx_triple_s / idx_triple_o 两个索引，一跳查询是走索引的。
"""
from __future__ import annotations

import json
import re

from . import db, kgdb

# 部位的展示顺序：从头顶到脚，最后是两个「全身」类目
PART_ORDER = ["头部", "颈部", "胸部", "腹部", "腰部", "盆腔",
              "四肢与脊柱", "全身（皮肤）", "全身"]

# 出图时排除的类型：监测数据点太密，会把图撑成毛线球
HIDDEN_TYPES = ("DataPoint",)


def _rows(sql: str, args=()) -> list:
    with db.connect() as con:
        return con.execute(sql, args).fetchall()


# ---------------------------------------------------------------- 一级：部位
def parts() -> dict:
    rows = _rows(
        """SELECT json_extract(props,'$.part') AS part, COUNT(*) AS n
           FROM entity WHERE type='Disease' AND json_extract(props,'$.part') IS NOT NULL
           GROUP BY part""")
    got = {r["part"]: r["n"] for r in rows}
    items = [{"name": p, "count": got[p]} for p in PART_ORDER if got.get(p)]
    # 数据里出现了没在 PART_ORDER 声明过的部位，追加到末尾而不是丢掉
    items += [{"name": p, "count": n} for p, n in
              sorted(got.items(), key=lambda x: -x[1]) if p not in PART_ORDER]

    unassigned = _rows("""SELECT COUNT(*) AS n FROM entity
                          WHERE type='Disease' AND json_extract(props,'$.part') IS NULL""")[0]["n"]
    return {"items": items, "total": sum(got.values()), "unassigned": unassigned}


# ---------------------------------------------------------------- 二级：部位下钻
def part(name: str) -> dict:
    rows = _rows(
        """SELECT e.id, e.name, e.degree, json_extract(e.props,'$.system_name') AS sys
           FROM entity e
           WHERE e.type='Disease' AND json_extract(e.props,'$.part')=?
           ORDER BY sys, e.degree DESC, e.name""", (name,))
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["sys"] or "其他", []).append(
            {"id": r["id"], "name": r["name"], "degree": r["degree"] or 0})
    items = [{"system": k, "count": len(v), "diseases": v} for k, v in groups.items()]
    items.sort(key=lambda x: (-x["count"], x["system"]))
    return {"part": name, "total": len(rows), "systems": items}


# ---------------------------------------------------------------- 系统清单
def systems() -> dict:
    """每个解剖系统下挂了多少个病种 —— 分类图谱的筛选下拉用它。"""
    rows = _rows(
        """SELECT s.id, s.name, COUNT(t.subject) AS n
           FROM entity s
           LEFT JOIN triple t ON t.object=s.id AND t.predicate='belongsToSystem'
           WHERE s.type='System'
           GROUP BY s.id ORDER BY n DESC, s.name""")
    return {"items": [{"id": r["id"], "name": r["name"], "count": r["n"]} for r in rows]}


# ---------------------------------------------------------------- 系统子图
def system_graph(name: str, limit: int = 60, types: list[str] | None = None) -> dict:
    """以某个解剖系统为中心：该系统下的疾病 + 它们的一跳邻居。

    limit 只约束邻居数量，疾病本身最多取 16 个（按度数），否则一个系统
    就能把画面塞满 100 多个节点，图上什么都看不清。
    """
    sys_row = _rows("SELECT id, name FROM entity WHERE name=? AND type='System'", (name,))
    if not sys_row:
        return {"error": f"未找到系统：{name}", "nodes": [], "links": []}
    sid = sys_row[0]["id"]

    dis = _rows(
        """SELECT e.id, e.name, e.type, e.type_label, e.degree
           FROM triple t JOIN entity e ON e.id=t.subject
           WHERE t.predicate='belongsToSystem' AND t.object=?
           ORDER BY e.degree DESC LIMIT 16""", (sid,))
    if not dis:
        return {"nodes": [], "links": [], "system": name, "count": 0}

    seed = {r["id"]: r for r in dis}
    nodes = [{
        "id": r["id"], "name": r["name"], "type": r["type"],
        "symbolSize": min(70, 12 + (r["degree"] or 1) // 8),
        "category": kgdb.category_of(r["type"]), "isCenter": False, "seed": True,
    } for r in dis]
    ids = set(seed)

    ph = ",".join("?" * len(seed))
    allow = {t for t in (types or []) if t}
    nbs = _rows(
        f"""SELECT t.predicate AS p, t.predicate_label AS pl,
                   e.id AS nid, e.name AS nm, e.type AS tp, e.degree AS dg
            FROM triple t JOIN entity e ON e.id=t.object
            WHERE t.subject IN ({ph}) AND e.type<>'DataPoint'
            UNION
            SELECT t.predicate AS p, t.predicate_label AS pl,
                   e.id AS nid, e.name AS nm, e.type AS tp, e.degree AS dg
            FROM triple t JOIN entity e ON e.id=t.subject
            WHERE t.object IN ({ph}) AND e.type<>'DataPoint'""",
        list(seed) * 2)

    added = 0
    for r in nbs:
        if allow and r["tp"] not in allow:
            continue
        if r["nid"] in ids:
            continue
        if added >= limit:
            break
        ids.add(r["nid"])
        added += 1
        nodes.append({
            "id": r["nid"], "name": r["nm"], "type": r["tp"],
            "symbolSize": min(70, 12 + (r["dg"] or 1) // 8),
            "category": kgdb.category_of(r["tp"]), "isCenter": False, "seed": False,
        })

    # 只保留两端都在本次节点集合里的边
    ph2 = ",".join("?" * len(ids))
    idl = list(ids)
    links = [{"source": r["s"], "target": r["o"], "label": r["pl"] or r["p"]}
             for r in _rows(
                f"""SELECT subject AS s, object AS o, predicate AS p, predicate_label AS pl
                    FROM triple
                    WHERE predicate<>'统计数据'
                      AND subject IN ({ph2}) AND object IN ({ph2})
                    ORDER BY id""", idl + idl)]

    return {
        "system": name, "count": len(dis), "total": len(nodes),
        "nodes": nodes, "links": links,
        "categories": kgdb.CATEGORIES,
    }


# ---------------------------------------------------------------- 部位图谱（图谱主视图）
SYMPTOM_PREDS = ("hasSymptom", "manifestsAs")


def part_graph(part: str, limit: int = 24, symptom_limit: int = 20) -> dict:
    """一个身体部位的「一屏图谱」：部位 → 疾病 → 常见症状。

    这是图谱主视图唯一的数据来源，设计目标是「不用点就能看懂」：
      · 疾病按连接度取前 limit 个，环绕部位节点排一圈
      · 症状按「有多少个该部位的疾病会表现为它」排序，只保留高频的
      · 每条症状边只连 3 个代表性疾病，避免 150 条以上连线糊成一团
    节点全部带名字，前端不再靠悬停/点击才知道是什么。
    """
    if not part:
        return {"error": "缺少部位参数", "nodes": [], "links": []}

    all_dis = _rows(
        """SELECT e.id, e.name, e.degree, json_extract(e.props,'$.system_name') AS sys
           FROM entity e
           WHERE e.type='Disease' AND json_extract(e.props,'$.part')=?
           ORDER BY e.degree DESC, e.name""", (part,))
    if not all_dis:
        return {"error": f"「{part}」下没有疾病记录", "nodes": [], "links": []}

    region = _rows("SELECT id FROM entity WHERE type='BodyRegion' AND name=?", (part,))
    region_id = region[0]["id"] if region else f"PART:{part}"

    shown = all_dis[:limit]
    shown_ids = [r["id"] for r in shown]
    all_ids = [r["id"] for r in all_dis]
    ph_all = ",".join("?" * len(all_ids))

    # 症状在「该部位全部疾病」上统计，否则只会留下前 24 个病才有的偏门表现
    sym_rows = _rows(
        f"""SELECT t.object AS sid, e.name AS sname, e.degree AS sdeg,
                   COUNT(DISTINCT t.subject) AS n
            FROM triple t JOIN entity e ON e.id=t.object
            WHERE t.predicate IN ('hasSymptom','manifestsAs')
              AND t.subject IN ({ph_all})
            GROUP BY t.object
            ORDER BY n DESC, e.degree DESC
            LIMIT ?""", all_ids + [symptom_limit])
    symptoms = [{"id": r["sid"], "name": r["sname"], "count": r["n"], "degree": r["sdeg"] or 0}
                for r in sym_rows if r["sname"]]
    sym_ids = [s["id"] for s in symptoms]

    links: list[dict] = [
        {"source": region_id, "target": r["id"], "label": "位于", "kind": "part"}
        for r in shown]

    if sym_ids:
        ph_s = ",".join("?" * len(shown_ids))
        ph_y = ",".join("?" * len(sym_ids))
        edges = _rows(
            f"""SELECT subject AS did, object AS sid, predicate_label AS pl
                FROM triple
                WHERE predicate IN ('hasSymptom','manifestsAs')
                  AND subject IN ({ph_s}) AND object IN ({ph_y})
                ORDER BY weight DESC""", shown_ids + sym_ids)
        # 每条症状最多连 3 个病，否则「头痛」一个人就能连出二十多条线
        used: dict[str, int] = {}
        for r in edges:
            if used.get(r["sid"], 0) >= 3:
                continue
            used[r["sid"]] = used.get(r["sid"], 0) + 1
            links.append({"source": r["did"], "target": r["sid"],
                          "label": r["pl"] or "表现为", "kind": "symptom"})

    by_system: dict[str, list] = {}
    for r in all_dis:
        by_system.setdefault(r["sys"] or "其他", []).append(
            {"id": r["id"], "name": r["name"], "degree": r["degree"] or 0})

    return {
        "part": part, "regionId": region_id, "total": len(all_dis),
        "diseases": [{"id": r["id"], "name": r["name"], "degree": r["degree"] or 0,
                      "system": r["sys"] or "其他"} for r in shown],
        "symptoms": symptoms,
        "links": links,
        "systems": [{"system": k, "count": len(v), "diseases": v}
                    for k, v in sorted(by_system.items(), key=lambda x: -len(x[1]))],
    }


# ---------------------------------------------------------------- 时间序列
def timeline(disease_id: str = "", name: str = "", metric: str = "发病数") -> dict:
    """月度监测序列。库里有序列的疾病只有 63 个，其余返回空让前端优雅降级。"""
    if not disease_id and name:
        r = _rows("SELECT id, name FROM entity WHERE name=? AND type='Disease'", (name,))
        if not r:
            return {"name": name, "metric": metric, "items": [], "available": False}
        disease_id, name = r[0]["id"], r[0]["name"]

    rows = _rows(
        """SELECT period, value, unit FROM stats_monthly
           WHERE disease_id=? AND metric=? ORDER BY period""",
        (disease_id, metric))
    items = [{"period": r["period"], "value": r["value"], "unit": r["unit"] or ""}
             for r in rows if r["value"] is not None]

    metrics = [r["metric"] for r in _rows(
        "SELECT DISTINCT metric FROM stats_monthly WHERE disease_id=?", (disease_id,))]
    return {
        "id": disease_id, "name": name, "metric": metric,
        "items": items, "available": bool(items), "metrics": metrics,
    }


# ---------------------------------------------------------------- 药品标签
_ZH = re.compile(r"[\u4e00-\u9fff]")
_EN = re.compile(r"[A-Za-z]")


def _is_en(text: str) -> bool:
    if not text:
        return False
    han = len(_ZH.findall(text))
    lat = len(_EN.findall(text))
    return lat > han


def drug_label(drug_id: str) -> dict:
    """药品登记 + 说明书字段清单。

    源数据里药品说明书只有英文（openFDA 原始标签），这里如实标出每个字段的
    语言，前端据此决定是否提示「原文为英文」，而不是假装中文资料存在。
    """
    head = _rows("SELECT * FROM drug_label WHERE drug_id=?", (drug_id,))
    ent = _rows("SELECT name, props FROM entity WHERE id=?", (drug_id,))
    props = {}
    if ent and ent[0]["props"]:
        try:
            props = json.loads(ent[0]["props"]) or {}
        except (ValueError, TypeError):
            props = {}

    fields = [{
        "field": r["field"], "label": r["label"], "chars": r["char_count"] or 0,
        "english": _is_en(r["content"]), "source": r["source"] or "",
        "excerpt": (r["content"] or "")[:160],
    } for r in _rows(
        """SELECT field, label, content, char_count, source FROM drug_content
           WHERE drug_id=?
           ORDER BY CASE field WHEN 'boxed_warning' THEN 1 WHEN 'contraindications' THEN 2
                               WHEN 'dosage' THEN 3 WHEN 'warnings' THEN 4
                               WHEN 'adverse_reactions' THEN 5 WHEN 'indications' THEN 6
                               ELSE 7 END""", (drug_id,))]

    info = dict(head[0]) if head else {}
    return {
        "id": drug_id,
        "name": (ent[0]["name"] if ent else "") or info.get("cn_name", ""),
        "cn_name": info.get("cn_name", ""), "en_name": info.get("en_name", ""),
        "generic_name": info.get("generic_name", ""), "brand_name": info.get("brand_name", ""),
        "manufacturer": info.get("manufacturer", ""), "route": info.get("route", ""),
        "source": info.get("source", ""), "props": props,
        "fields": fields, "has_label": bool(fields),
    }


def has_monthly(disease_ids: list[str]) -> set[str]:
    """批量判断哪些疾病有月度序列 —— 概览页用一次查询点亮可用标记。"""
    if not disease_ids:
        return set()
    ph = ",".join("?" * len(disease_ids))
    return {r["disease_id"] for r in _rows(
        f"SELECT DISTINCT disease_id FROM stats_monthly WHERE disease_id IN ({ph})",
        disease_ids)}


# ---------------------------------------------------------------- 说明书章节（问答用）
# 意图没有指明字段时的兜底顺序：先把最可能出事的几章放前面
_LABEL_FALLBACK = ("contraindications", "boxed_warning", "warnings",
                   "dosage", "adverse_reactions", "indications", "drug_interactions")


def label_sections(drug_id: str, fields: list[str] | None = None,
                   limit: int = 3) -> list[dict]:
    """按意图字段取说明书章节，供问答当证据用。

    这是融合版修掉「问布洛芬禁忌却答成曲普坦」的关键：药物问题不再去中文语料里
    做全文检索——那里根本没有该药的禁忌章节，检索必然漂移——而是直接按字段取
    说明书原文。缺哪个字段就退到兜底顺序，取到了什么如实返回。
    """
    rows = _rows("""SELECT field, label, content, char_count, source
                    FROM drug_content WHERE drug_id=?""", (drug_id,))
    by_field = {r["field"]: r for r in rows}
    picked, seen = [], set()
    for f in (fields or []):
        r = by_field.get(f)
        if r and f not in seen:
            seen.add(f)
            picked.append(r)
    if not picked:
        for f in _LABEL_FALLBACK:
            r = by_field.get(f)
            if r and f not in seen:
                seen.add(f)
                picked.append(r)
            if len(picked) >= limit:
                break
    return [{"field": r["field"], "label": r["label"], "content": r["content"] or "",
             "chars": r["char_count"] or 0, "source": r["source"] or ""}
            for r in picked[:limit]]


# ---------------------------------------------------------------- 药品中文导读
# 说明书正文只有英文（openFDA 原始标签），系统不代译——医学表述一旦译错，
# 比看不懂更糟。但知识库里本来就有大量中文事实（药品登记、图谱关系），
# 可以拼成一段准确的中文导读，先把"这药是什么、治什么、忌什么"说清楚。
FIELD_GLOSS = {
    "indications": "说明该药用于治疗什么。",
    "dosage": "给出服用方法、单次剂量与每日次数。",
    "contraindications": "列出不能使用该药的情形。",
    "warnings": "用药前必须知道的风险提示。",
    "adverse_reactions": "用药后可能出现的不良反应。",
    "drug_interactions": "与其他药物同服时的影响。",
    "boxed_warning": "FDA 最高级别的安全警告，通常涉及致命风险。",
}

ROUTE_CN = {
    "ORAL": "口服", "INTRAVENOUS": "静脉注射", "INTRAMUSCULAR": "肌内注射",
    "SUBCUTANEOUS": "皮下注射", "TOPICAL": "外用", "TRANSDERMAL": "经皮",
    "INHALATION": "吸入", "RESPIRATORY (INHALATION)": "吸入", "RECTAL": "直肠给药",
    "VAGINAL": "阴道给药", "OPHTHALMIC": "滴眼", "OTIC": "滴耳", "NASAL": "鼻用",
    "SUBLINGUAL": "舌下含服", "BUCCAL": "口腔黏膜", "EPIDURAL": "硬膜外",
    "INTRADERMAL": "皮内注射", "IRRIGATION": "冲洗", "CUTANEOUS": "皮肤外用",
}

# 长单位写在短单位前面：先匹配 mL/min/1.73 m2，否则会被前面的 mL 抢先吃掉
_NUM_UNIT = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:mL/min/1\.73\s*m2|mL/min|mg/day|mg|mcg|μg|"
    r"hours?|days?|weeks?|g|mL|ml|L|%)", re.I)


def zh_numbers(text: str, limit: int = 6) -> list[str]:
    """从英文原文里摘出带单位的数值。

    不翻译句子，只把「2,000 mg」「30 mL/min/1.73 m2」这类硬信息单独列出来——
    剂量和阈值是最不该被语言挡住的部分，而这些数值本身不需要翻译。
    """
    out, seen = [], set()
    for m in _NUM_UNIT.finditer(text or ""):
        v = re.sub(r"\s+", " ", m.group(0)).strip()
        k = v.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(v)
        if len(out) >= limit:
            break
    return out


def drug_zh(drug_id: str) -> dict | None:
    """拼一段中文导读：全部取自库里已有的中文事实，不做推断。"""
    label = drug_label(drug_id)
    if not label.get("id"):
        return None
    props = label.get("props") or {}

    def names(sql: str, args) -> list[str]:
        return [r["name"] for r in _rows(sql, args) if r["name"]]

    treats = names(
        """SELECT DISTINCT e.name, e.degree FROM triple t JOIN entity e ON e.id=t.subject
           WHERE t.object=? AND t.predicate='treatedBy' AND e.type='Disease'
           ORDER BY e.degree DESC LIMIT 12""", (drug_id,))
    banned = names(
        """SELECT DISTINCT e.name FROM triple t JOIN entity e ON e.id=t.object
           WHERE t.subject=? AND t.predicate='contraindicatedIn' LIMIT 8""", (drug_id,))
    combo = names(
        """SELECT DISTINCT e.name FROM triple t JOIN entity e ON e.id=t.object
           WHERE t.subject=? AND t.predicate='coMentionedWith' AND e.type='Drug' LIMIT 8""",
        (drug_id,))
    classes = names(
        """SELECT DISTINCT e.name FROM triple t JOIN entity e ON e.id=t.object
           WHERE t.subject=? AND t.predicate='memberOf' AND e.type='DrugClass' LIMIT 6""",
        (drug_id,))

    route_raw = (label.get("route") or props.get("route") or "").strip()
    route = ROUTE_CN.get(route_raw.upper(), route_raw)
    if not classes and props.get("drug_class"):
        classes = [props["drug_class"]]

    bits = []
    head = "、".join(classes) if classes else ""
    if head:
        bits.append(f"属于{head}")
    if route:
        bits.append(f"常用给药途径为{route}")
    line = "，".join(bits)
    if treats:
        more = f"等 {len(treats)} 种疾病" if len(treats) >= 12 else "等"
        line += ("；" if line else "") + f"图谱中收录其用于{'、'.join(treats[:6])}{more}的治疗"
    if banned:
        line += f"，禁用于{'、'.join(banned[:4])}"
    if line:
        line += "。"

    return {
        "id": drug_id,
        "name": label.get("name") or label.get("cn_name") or "",
        "cn_name": label.get("cn_name") or "",
        "en_name": label.get("en_name") or props.get("en_name") or "",
        "generic_name": label.get("generic_name") or props.get("generic_name") or "",
        "brand_name": label.get("brand_name") or props.get("brand_name") or "",
        "manufacturer": label.get("manufacturer") or props.get("manufacturer") or "",
        "route": route,
        "classes": classes,
        "treats": treats,
        "banned": banned,
        "combo": combo,
        "line": line,
    }


# ---------------------------------------------------------------- 中文正文
def zh_points(drug_id: str, field: str = "") -> list[str]:
    """取人工译好的中文要点（drug_content_zh）。

    这张表是离线录入的：说明书原文是英文，前端要显示中文，所以逐条译好落库，
    运行时不调用任何模型。field 留空则返回该药所有字段的要点合集。
    """
    if not drug_id:
        return []
    sql = "SELECT field, points_zh FROM drug_content_zh WHERE drug_id=?"
    args = [drug_id]
    if field:
        sql += " AND field=?"
        args.append(field)
    out: list[str] = []
    try:
        for r in _rows(sql, args):
            if not r["points_zh"]:
                continue
            try:
                out.extend(json.loads(r["points_zh"]))
            except (ValueError, TypeError):
                continue
    except Exception:  # noqa: BLE001 — 老库没有这张表
        return []
    return out


def label_note(drug_id: str, text: str) -> str:
    """返回「这一节正文与药品对不上」的说明；没问题返回空串。

    源库有两类问题，都会让证据看起来权威、实际答非所问：
      1) 标签错配：字段里装的是另一个药的说明书；
      2) 复方制剂：单一成分药挂了复方试剂盒（如 Dyural 40 Kit）的标签，
         正文主要在讲配伍里的另一种成分。
    """
    if not text or len(text) < 400:
        return ""
    try:
        r = _rows("SELECT en_name, generic_name, brand_name FROM drug_label WHERE drug_id=?",
                  (drug_id,))
    except Exception:  # noqa: BLE001
        return ""
    if not r:
        return ""
    gen = (r[0]["generic_name"] or "").strip()
    parts = [x.strip() for x in re.split(r"[,;/]", gen) if len(x.strip()) > 3]
    if len(parts) >= 3:
        return ("该药的标签取自复方制剂（含 " + "、".join(parts[:4]).title() +
                " 等成分），正文可能主要描述其他成分，请结合具体制剂核对。")
    words: list[str] = []
    for key in ("en_name", "generic_name", "brand_name"):
        v = r[0][key] or ""
        words += [w.lower() for w in re.split(r"[^A-Za-z]+", v) if len(w) > 4]
    if not words:
        return ""
    body = text.lower()[:6000]
    if any(w in body for w in words):
        return ""
    return "本节正文与该药品不符（源库标签错配），请勿据此用药，可点开原始出处核对。"


def label_mismatch(drug_id: str, text: str) -> bool:
    """判断这一节正文是否真的在讲这个药。

    源库里存在标签错配：甲泼尼龙的字段里装的是布比卡因的说明书。
    这类内容一旦被当成「权威证据」展示或引用，比缺内容危险得多，
    所以用最朴素的办法先拦一道：正文里必须出现过该药的任一英文名。
    OTC「Drug Facts」标签正文本来就不写药名，正文短的不做判定，避免误伤。
    """
    return bool(label_note(drug_id, text))


def zh_note(drug_id: str, field: str) -> str:
    """译注说明：剂型差异、复方标签这类必须提醒的限定条件。"""
    try:
        r = _rows("SELECT note FROM drug_content_zh WHERE drug_id=? AND field=?",
                  (drug_id, field))
    except Exception:  # noqa: BLE001
        return ""
    return (r[0]["note"] or "") if r else ""
