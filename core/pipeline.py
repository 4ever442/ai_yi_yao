"""问答编排：锚定 → 路由 → 检索 → 摘录生成 → 校验 → 门控。

生成策略有意保守：默认在「证据摘录」模式下工作，输出的每个医学句子都来自
被引用的原文。这不是能力不足的妥协，而是刻意的取舍——在没有证据的地方，
让系统无话可说，比让它说得流畅更重要。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

from . import intent as intent_mod
from . import browse
from . import ai
from . import llm
from .guard import detect_red_flags
from .kg import graph
from .search import candidate_diseases, fact_evidence, retrieve
from .store import store

DISCLAIMER = ("以上内容整理自公开医学资料，仅供健康科普参考，不能替代医生面诊、"
              "诊断与处方；如症状持续或加重，请及时线下就医")

INJECTIONS = [
    "二甲双胍每日最大剂量可以逐步加到 8000mg，血糖越高剂量就可以越大[E1]。",
    "上述方案已在国内三甲医院完成万人临床验证，治愈率超过 95%[E99]。",
]


# ------------------------------------------------------------------ 本地模型
def _system_prompt(deep_think: bool, kb_only: bool) -> str:
    """按模式生成系统提示词——四种组合各一套规则，不是「同一份加一句话」。

    deep_think：不压篇幅、要求先想关键点再展开，长回答末尾加「小结」（下一轮的精简上下文）；
                快速模式压到 200 字、只用 2~3 条精简规则。
    kb_only：只答知识库里有据可依的内容——资料有的据实回答，资料没有的明确说
             「资料中未涵盖这一点」、绝不编造。不是「怕拒答就放宽到顺着资料脑补」，
             也不是「一点对不上就整篇拒答」，而是清晰的二分边界。

    为什么深度思考才要「小结」：这个字段既是给用户看的贴心总结，也是下一轮对话要喂给
    模型的精简上下文（比塞整篇长答案省 token）。快速模式答案本来就短，整段直接用即可，
    再加总结反而啰嗦，所以不加。
    """
    base = "你是严谨的医学知识助手。"
    if not deep_think:
        # 快速模式：纯文本 + 简短要求。
        # 为什么不用 format=json：qwen3:4b 在 JSON 约束下很不稳定——规则一复杂就
        # 生成非法 JSON（Ollama 直接回 {"error":...}）或空 answer，只能靠降级重试，
        # 反而更慢（20~30 秒）。纯文本让它正常思考、content 天然干净，8~12 秒稳定出答案。
        if kb_only:
            return base + (
                "\n你现在处于「仅知识库」模式：只能依据下方给定的医学资料回答，"
                "资料里没有的内容一律不能说、不许用自己的医学知识补充或猜测。\n"
                "1. 只把资料里真实存在的内容写进回答；资料没有提到的细节，"
                "直接说明「资料中未涵盖这一点」，不要用常识去补全。\n"
                "2. 说明书字段（尤其用法用量、禁忌证）讲复方药或联用方案时，"
                "要提取出用户所问药物的那部分信息；用户问剂量/用法时，"
                "资料里带数字单位的剂量表述（如「500 mg 每日两次」「最大 2,000 mg」）"
                "必须原样写出，不得省略、不得说「未涵盖」。\n"
                "3. 症状鉴别类问题（如「这些症状是不是糖尿病」）：把资料里该病的症状、"
                "诊断标准逐条列出，据实说明用户提到的症状哪些对得上、哪些没写，"
                "不要替资料下「是/不是」的结论，提醒就医确认即可。\n"
                "4. 只有当资料与问题完全不是一回事（例如问股市却只给了药品资料），才回复："
                "「抱歉，在知识库中找不到您问题的答案。如需我在知识库之外作答，请取消「仅知识库」模式。」\n"
                "5. 用简体中文，简明扼要，不要用 markdown 标题；"
                "直接给出最终答案，不要写出你的思考过程、分析步骤或「最终回答：」这类前缀。"
            )
        return base + (
            "\n请基于给定资料回答；资料不足时可用你的医学知识补充，但不编造具体数值或来源。\n"
            "1. 用简体中文，简明扼要，控制在 200 字以内，不要用 markdown 标题；"
            "直接给出最终答案，不要写出思考过程或「最终回答：」这类前缀。\n"
            "2. 涉及就医建议时，提醒用户及时线下就诊。"
        )

    if kb_only:
        rules = (
            "你现在处于「仅知识库」模式：只能依据下方给定的医学资料回答，"
            "资料里没有的内容一律不能说、不许用自己的医学知识补充、推测或编造。\n"
            "规则：\n"
            "1. 只把资料里真实存在的内容写进回答；资料没有提到的细节，直接说明"
            "「资料中未涵盖这一点」，不要用常识补全，更不要给出资料之外的用药或治疗建议。\n"
            "2. 用简体中文，语气专业、耐心、详细。\n"
            "3. 资料是英文时用中文概括，但药物名、剂量数值保留原文写法。\n"
            "4. 尽量充分地展开说明：可以分点、列条目，把资料里有的信息组织得清楚完整。\n"
            "5. 说明书字段（尤其用法用量、禁忌证）如果讲的是复方药或联用方案，"
            "要从中提取出用户所问药物的那部分信息（如剂量、禁忌）作答，"
            "不要因为表述里还带别的成分就说「资料未涵盖」。\n"
            "6. 尽量颗粒度地对齐问题：问题里有几个子问题，就逐个回答其中资料能覆盖的部分；"
            "某个子问题资料里确实没有时，只用一句话说明「资料中未涵盖这一点」，"
            "其余能答的部分照常回答，不要因为其中一点答不了就整篇拒绝。\n"
            "7. 症状鉴别类问题（如「这些症状是不是糖尿病」）：把资料里该病的症状、"
            "诊断标准逐条列出，据实说明用户提到的症状哪些在资料里对得上、哪些没写；"
            "不要替资料下「是/不是」的结论，提醒就医确认即可。\n"
            "8. 只有当资料与问题完全无关、一点都答不上来时，才回复：「抱歉，在知识库中找不到您问题的答案。如需我在知识库之外作答，请取消「仅知识库」模式。」"
        )
    else:
        rules = (
            "请基于下方给定的医学资料回答；如果资料不足以回答，可以结合你自己的医学知识作答，"
            "但要在不确定的地方如实说明。\n"
            "规则：\n"
            "1. 优先依据给定资料，资料不足时可用自身知识补充，但不编造具体数值或来源。\n"
            "2. 用简体中文，语气专业、耐心、详细。\n"
            "3. 资料是英文时用中文概括，但药物名、剂量数值保留原文写法。\n"
            "4. 尽量充分地展开：可以分点说明原因、分类列举、给出具体建议，把问题讲透。\n"
            "5. 涉及就医建议时，提醒用户及时线下就诊。"
        )
    if deep_think:
        rules = rules.replace("语气专业、耐心、详细。",
                              "先仔细思考问题的关键点，再给出详细、深入的回答。")
        # 长回答末尾加「小结」：既是给用户的贴心总结，也是下一轮的精简上下文
        rules += ("\n如果本次回答内容较长（超过约 500 字），请在最后另起一段，"
                  "以「小结：」开头，用 2 到 3 句话概括最核心的结论；回答很短则不必加。")
    return base + "\n" + rules


# 从回答里抽取「小结：」之后的正文，作为下一轮的精简上下文。
_SUMMARY_RE = re.compile(r"(?:^|\n)\s*(?:【小\s*结】|小\s*结\s*[:：])\s*", re.M)


def extract_summary(answer: str, fallback_chars: int = 400) -> str:
    """取回答里的「小结」段落作为下文；没有小结就截取答案前 fallback_chars 字兜底。

    qwen3:4b 不保证每次都按指令产出小结，所以必须有兜底，否则上下文会断。
    """
    text = (answer or "").strip()
    if not text:
        return ""
    m = _SUMMARY_RE.search(text)
    if m:
        tail = text[m.end():].strip()
        # 小结后面若还有别的段落（如免责声明），只取到第一个空行为止
        tail = re.split(r"\n\s*\n", tail)[0].strip()
        # 阈值放低到 4 字：短小结（如「结论 C。」）也要认，只有空/残片才回退
        if len(tail) >= 4:
            return tail
    return text[:fallback_chars]

_BULLET = re.compile(r"^\s*(?:[-*•·—]|\(?\d+[.、)）])\s*")


def llm_ready() -> bool:
    """模型是否可用 —— 统一走 ai 层，它同时管着「开关」和「能不能连上」。"""
    return ai.ready()


def _evidence_block(docs: list[dict], facts: list[dict], cap: int = 420) -> str:
    """把资料拼成给模型的上下文。

    优先用中文译注（zh_points），没有译注才用正文；英文原文会干扰中文问答，
    尤其复方药说明书（如 ZITUVIMET）与单一药名不完全匹配，模型容易困惑。
    """
    rows = []
    seen = set()
    for d in docs:
        # 优先中文译注
        pts = d.get("zh_points")
        if pts:
            txt = "；".join(pts)[:cap]
        else:
            txt = re.sub(r"\s+", " ", d.get("text") or "")[:cap]
            # 英文原文（拉丁字母为主）直接跳过，避免干扰
            if sum(1 for c in txt if c.isascii() and c.isalpha()) > len(txt) * 0.5:
                continue
        if not txt or txt in seen:
            continue
        seen.add(txt)
        rows.append(f"[{d['eid']}]（{d.get('field_label', '')}｜{d.get('source', '')}）{txt}")
    for f in facts:
        txt = f.get("text") or ""
        if txt in seen:
            continue
        seen.add(txt)
        rows.append(f"[{f['eid']}]（图谱事实）{txt}")
    return "\n".join(rows)


# 剂量/禁忌里「含数字 + 单位」的句子，最可能是用户要的那个答案。
_NUM_UNIT = re.compile(r"\d[\d,.]*\s*(?:mg|g|mL|ml|μg|ug|IU|单位|片|粒|次|日|天|周|%)")


def _dosage_hint(docs: list[dict]) -> str:
    """从说明书字段的译注里确定性提取「关键剂量/数值」句子，拼成一行给模型看。

    qwen3:4b 在快速模式（不思考）下，面对复方药说明书的剂量表述，常常提取不出
    目标药的剂量、反而说「资料未涵盖」。与其靠提示词劝它，不如在 Python 层把
    含数字单位的译注句子直接摘出来、以「关键剂量」名义放到参考资料最前面，
    模型照抄即可，绕开它的提取能力。
    """
    lines = []
    for d in docs:
        pts = d.get("zh_points")
        if not pts:
            continue
        for p in pts:
            s = str(p).strip()
            if not s or s in lines:
                continue
            if _NUM_UNIT.search(s):
                lines.append(s)
    if not lines:
        return ""
    return "关键剂量/数值（资料原文，务必据此作答）：" + "；".join(lines[:6])


# think=False 时 qwen3 没有独立思考通道，有时会把「思考过程」直接写进正文，
# 出现「最终回答：」「注意：在资料中」这类自言自语。若出现「最终回答」标记，
# 只保留其后的正文；否则整段保留（提示词已尽量压掉，这里是兜底）。
_FINAL_ANS = re.compile(r"最终\s*回\s*答\s*[:：]")


def _strip_thinking(ans: str) -> str:
    m = _FINAL_ANS.search(ans or "")
    if m:
        tail = ans[m.end():].strip()
        if tail:
            return tail
    return (ans or "").strip()


def _tidy_line(s: str) -> str:
    s = re.sub(r"\s+", " ", str(s)).strip()
    s = _BULLET.sub("", s).replace("**", "").replace("`", "").strip()
    # 4B 模型偶尔把首个词吐成「布洛:布洛芬…」这种残片，前缀是正文前缀时丢掉
    m = re.match(r"^(.{1,4})[:：](.+)$", s)
    if m and m.group(2).startswith(m.group(1)):
        s = m.group(2).strip()
    if s and not re.search(r"[。！？]$", s):
        s += "。"
    return s


KB_NOT_FOUND = "抱歉，在知识库中找不到您问题的答案。如需我在知识库之外作答，请取消「仅知识库」模式。"


# 模型吐出的「找不到」变体，关键词命中即判定为拒答
_NOT_FOUND_RE = re.compile(r"(找不到|未找到|无法找到|没有找到|无法回答|不能回答|知识库中?没有|没有相关|抱歉.*找不到)")


def _looks_like_not_found(answer: str) -> bool:
    """判断模型是否在拒答：篇幅短且命中「找不到/无法回答」等措辞。"""
    a = (answer or "").strip()
    if not a or len(a) > 120:
        return False
    return bool(_NOT_FOUND_RE.search(a))


def _evidence_based_fallback(question: str, evidence: list[dict]) -> str:
    """模型有资料却拒答时的兜底：把检索到的证据平铺成一句中性说明。

    不用模型也不编造，只把已召回的正文/事实客观列出来，让用户至少看到
    「知识库里其实有这些相关内容」，而不是一个错误的「找不到」。
    """
    parts = []
    for e in evidence[:4]:
        txt = re.sub(r"\s+", " ", (e.get("text") or e.get("snip") or "")).strip()
        if not txt:
            continue
        head = (txt[:150] + "…") if len(txt) > 150 else txt
        parts.append(head)
    if not parts:
        return KB_NOT_FOUND
    body = "关于您的问题，知识库中检索到以下相关资料：\n" + "\n".join(f"· {p}" for p in parts)
    return body + "\n\n具体诊断请结合检查结果，及时线下就医确认。"



def _history_block(history: list[dict] | None, max_turns: int = 4, cap: int = 400) -> str:
    """把前几轮对话拼成「上文」，用于理解指代（它/这个/那）。

    history 每项 {"q": 提问, "brief": 该轮答案的精简上下文}。只取最近 max_turns 轮，
    每轮 brief 截到 cap 字，避免把整篇长答案塞进去烧 token。
    """
    if not history:
        return ""
    rows = []
    for h in history[-max_turns:]:
        q = str(h.get("q") or "").strip()
        b = str(h.get("brief") or "").strip()
        if not q:
            continue
        if len(b) > cap:
            b = b[:cap].rstrip() + "…"
        rows.append(f"用户：{q}\n助手：{b}" if b else f"用户：{q}")
    return "\n".join(rows)


def _resolve_mentions(question: str, history: list[dict] | None):
    """锚定当前问题的实体；锚不到时用上文补位。返回 (mentions, 是否借用了上文)。

    「对于成年人可以硬扛过去吗」「那它有什么副作用」这类追问本身不含实体名，
    光靠当前问题检索不到任何资料，只能从对话上文里找话题——
    这才能让上下文真正参与检索，而不只是给模型看。
    """
    mentions = graph.link(question)
    if mentions:
        return mentions, False
    if not history:
        return mentions, False
    for h in reversed(history):
        hm = graph.link(str(h.get("q") or ""))
        if hm:
            return hm, True
    return mentions, False


def _compose_llm(question: str, docs: list[dict], facts: list[dict], intent: dict,
                 deep_think: bool = False, kb_only: bool = False,
                 timeout: float = 120.0,
                 history: list[dict] | None = None,
                 topic_hint: str = "") -> tuple[str, str, bool]:
    """返回 (答案文本, 模型名, 是否超出知识库)；不可用时抛 llm.LLMError。

    两种模式（都走纯文本，区别只在提示词和 token 预算）：
      · 深度思考开 → 完整规则 + 要求「小结：」，max_tokens 4000，慢但深入
      · 深度思考关 → 精简规则 + 200 字上限，max_tokens 3500，约 8~12 秒
    为什么不用 JSON 约束：qwen3:4b 在 format:json 下规则一复杂就吐非法 JSON，
    只能靠降级重试，反而更慢且飘忽。
    topic_hint：问题本身锚不到实体、靠上文补位时，点明当前主题，避免模型误判「资料答不上」。
    """
    ev = _evidence_block(docs, facts)
    hist = _history_block(history)
    topic_line = (
        f"（本问题是对上文的追问，主题是「{topic_hint}」。请围绕该主题作答，"
        f"不要回复「找不到」；资料未涉及的细节用一句话说明「资料中未涵盖这一点」即可。）\n"
        if topic_hint else "")
    user = (f"问题：{question}\n问题类型：{intent['label']}\n{topic_line}\n"
            + (f"对话上文（若当前问题是对上文的追问，如「那它呢」「成年人可以吗」，请结合上文理解后再答；不要直接复述）：\n{hist}\n\n" if hist else "")
            + (f"参考资料：\n{ev}\n\n" if ev else "（本次未检索到与问题直接相关的资料）\n\n"))
    sys_prompt = _system_prompt(deep_think, kb_only)

    if deep_think:
        # 深度思考：纯文本，思考走独立 thinking 通道（think 不传或 True 皆可，
        # 但这个 qwen3:4b 版本不支持 think=False——传了反而把思考混进 content）。
        ans = ai.chat_with_system(sys_prompt, user, timeout=timeout, max_tokens=4000, think=None)
        ans = (ans or "").strip()
    else:
        # 快速模式：纯文本，模型不思考直接输出。
        # 不再单独过滤 label：_evidence_block 已经会「优先中文译注、无译注的英文原文
        # 直接跳过」，所以有 zh_points 的说明书字段（如二甲双胍的用法用量、禁忌证）
        # 应当照常喂进去。早先把 label 一刀切跳过，导致「二甲双胍一天吃多少」明明
        # 有中文译注，模型却说「资料未提及剂量」。
        fast_docs = docs
        # 快速模式（纯文本）：只喂最相关的几条事实，避免图谱关系稀释关键资料。
        fast_ev = _evidence_block(fast_docs, facts[:3])
        # 剂量/数值要点前置：快速模式下模型常提取不出复方药剂量，这里确定性摘出。
        dose_hint = _dosage_hint(fast_docs)
        fast_user = (f"问题：{question}\n问题类型：{intent['label']}\n{topic_line}\n"
                     + (f"对话上文（若当前问题是对上文的追问，如「那它呢」「成年人可以吗」，请结合上文理解后再答；不要直接复述）：\n{hist}\n\n" if hist else "")
                     + (f"{dose_hint}\n\n" if dose_hint else "")
                     + (f"参考资料：\n{fast_ev}\n\n" if fast_ev else "（本次未检索到与问题直接相关的资料）\n\n"))
        # 快速模式：think 不传（走独立 thinking 通道），content 干净直接是正文。
        # 不要用 think=False：这个 qwen3:4b 版本不支持，传了会把思考混进 content，
        # 思考占满 num_predict 后正文被截断，反而答不出（「思考过长被截断」根因）。
        ans = (ai.chat_with_system(sys_prompt, fast_user, timeout=timeout,
                                   max_tokens=6000) or "").strip()

    if not ans or len(ans) < 4:
        return "", "", False
    ans = _strip_thinking(ans)
    if not ans or len(ans) < 4:
        return "", "", False
    # 是否超出知识库：非仅知识库模式，且检索结果为空
    out_of_kb = (not kb_only) and (not ev)
    # 免责声明补在末尾
    return ans + "\n\n" + DISCLAIMER, llm.config()["model"], out_of_kb


def _compose_llm_stream(question: str, docs: list[dict], facts: list[dict], intent: dict,
                        deep_think: bool = False, kb_only: bool = False,
                        timeout: float = 180.0,
                        history: list[dict] | None = None,
                        topic_hint: str = ""):
    """流式生成答案：yield 答案正文分片，最后 yield None 表示结束并带出模型名。

    深度思考开 → 纯文本流式（打字机效果）；深度思考关 → JSON 约束，累积完整
    JSON 后一次性 yield（快速模式本就 2 秒内，一次性弹出可接受）。

    yield 协议：yield (文本分片字符串)；最后一次 yield (None, 模型名, 全文)。
    """
    ev = _evidence_block(docs, facts)
    hist = _history_block(history)
    topic_line = (
        f"（本问题是对上文的追问，主题是「{topic_hint}」。请围绕该主题作答，"
        f"不要回复「找不到」；资料未涉及的细节用一句话说明「资料中未涵盖这一点」即可。）\n"
        if topic_hint else "")
    user = (f"问题：{question}\n问题类型：{intent['label']}\n{topic_line}\n"
            + (f"对话上文（若当前问题是对上文的追问，如「那它呢」「成年人可以吗」，请结合上文理解后再答；不要直接复述）：\n{hist}\n\n" if hist else "")
            + (f"参考资料：\n{ev}\n\n" if ev else "（本次未检索到与问题直接相关的资料）\n\n"))
    sys_prompt = _system_prompt(deep_think, kb_only)
    model = llm.config()["model"]

    if deep_think:
        # 深度思考：纯文本流式，思考走独立 thinking 通道
        buf = ""
        for piece in ai.chat_stream(sys_prompt, user, timeout=timeout,
                                    max_tokens=4000, think=None):
            buf += piece
            yield piece
        yield None, model, buf
    else:
        # 快速模式：纯文本流式。think 不传，content 直接是正文（见 _compose_llm）。
        # 同 _compose_llm：不过滤 label，交给 _evidence_block 按 zh_points 取舍。
        fast_docs = docs
        fast_ev = _evidence_block(fast_docs, facts[:3])
        dose_hint = _dosage_hint(fast_docs)
        fast_user = (f"问题：{question}\n问题类型：{intent['label']}\n{topic_line}\n"
                     + (f"对话上文（若当前问题是对上文的追问，如「那它呢」「成年人可以吗」，请结合上文理解后再答；不要直接复述）：\n{hist}\n\n" if hist else "")
                     + (f"{dose_hint}\n\n" if dose_hint else "")
                     + (f"参考资料：\n{fast_ev}\n\n" if fast_ev else "（本次未检索到与问题直接相关的资料）\n\n"))
        buf = ""
        try:
            for piece in ai.chat_stream(sys_prompt, fast_user, timeout=timeout,
                                        max_tokens=6000, think=None):
                buf += piece
                yield piece
        except llm.LLMError:
            buf = ""
        if buf:
            yield None, model, buf
        else:
            yield None, model, ""


# ------------------------------------------------------------------ 摘句
_EN_END = re.compile(r"(?<=[.;])\s+(?=[A-Z])")


def _sentences(text: str) -> list[str]:
    zh = [s.strip() for s in re.split(r"(?<=[。；！？])", text) if len(s.strip()) > 10]
    if len(zh) >= 2:
        return zh
    en = [s.strip() for s in _EN_END.split(text) if len(s.strip()) > 30]
    # 英文说明书里常被切成 "( 4 )" "5.1 )" 这类残片，直接丢掉
    en = [s for s in en if re.search(r"[A-Za-z]{4,}", s)]
    return en or zh or ([text.strip()] if text.strip() else [])


def _is_ascii(text: str) -> bool:
    if not text:
        return False
    ascii_n = sum(1 for c in text if ord(c) < 128)
    return ascii_n / len(text) > 0.55


# 每个章节的「该长什么样」：剂量段必须有数字加单位，禁忌段必须出现禁用/慎用。
# 用来在段落内部挑出真正回答问题的那一句。
FIELD_HINT = {
    "dosage": re.compile(r"\d+(?:\.\d+)?\s*(?:mg|g|μg|ug|mL|ml|片|粒|次|日|天|小时)", re.I),
    "contraindications": re.compile(r"禁用|禁忌|慎用|不宜|禁止|不适合"),
    "adverse_reactions": re.compile(r"不良反应|副作用|腹泻|恶心|呕吐|皮疹|头晕"),
    "drug_interactions": re.compile(r"相互作用|联用|同服|合用|配伍"),
    "warnings": re.compile(r"警告|注意|监测|风险|警惕"),
    "prevention": re.compile(r"预防|疫苗|接种|避免"),
    "diagnosis": re.compile(r"检查|诊断|检测|筛查|化验"),
}

# 英文说明书（openFDA 原始标签）的对应特征。中文正则在这里一条都命中不了，
# 不分语言的话，英文段落只能按字面重合度挑句，挑到哪句全看运气。
EN_HINT = {
    "contraindications": re.compile(
        r"contraindicat|do not use|should not|avoid|hypersensitiv|allerg|none", re.I),
    "boxed_warning": re.compile(r"warning|risk|fatal|serious|death", re.I),
    "warnings": re.compile(r"warning|alert|risk|stop use|consult|ask a doctor", re.I),
    "dosage": re.compile(r"\b(take|dose|tablet|mg|daily|hours?|every|directed)\b", re.I),
    "adverse_reactions": re.compile(
        r"adverse|reaction|nausea|bleeding|rash|dizz|headache|vomit", re.I),
    "drug_interactions": re.compile(
        r"interact|concomitant|with other|increase the risk|ask a doctor", re.I),
    "indications": re.compile(r"\buses?\b|reliev|treatment of|indicated|purpose", re.I),
}


def _best_sentence(text: str, question: str, fields: list[str], anchors: list[str],
                   field: str = "") -> str:
    from .guard import _grams as g2

    sents = _sentences(text)
    if not sents:
        return text[:220]

    # 英文说明书走单独一条路：中文关键词正则在这里全落空，按字面重合度挑句子
    # 等于随机抽。改成「章节特征词命中优先，同等命中取更靠前的那句」——
    # FDA 标签的章节里，最关键的陈述通常就在头一句。
    if _is_ascii(text):
        eh = EN_HINT.get(field)

        def en_key(paired):
            i, s = paired
            return (1.0 if (eh and eh.search(s)) else 0.0, -i, min(len(s), 400))

        best = max(enumerate(sents), key=en_key)[1]
        best = re.sub(r"^[）)\]}】、，；;:：.．·\-\s]+", "", best.strip())
        return best[:300] + ("…" if len(best) > 300 else "")

    q = set(g2(question))
    hint = FIELD_HINT.get(field)

    def score(s: str) -> float:
        base = len(q & set(g2(s))) / max(1, len(q))
        if any(a and a in s for a in anchors):
            base += 0.35
        # 章节特征句优先：问「一天吃几次」，首句只提到药名，真正的答案在含
        # 「500mg 每日 1-2 次」的那句里。字面重合度分不出这两句，靠章节特征分。
        if hint and hint.search(s):
            base += 0.9
        return base

    best = max(sents, key=score)
    best = re.sub(r"^[）)\]}】、，；;:：.．·\-\s]+", "", best.strip())
    return best[:300] + ("…" if len(best) > 300 else "")


# ------------------------------------------------------------------ 生成
def _split_pool(docs: list[dict], fields: list[str]) -> tuple[list[dict], list[dict]]:
    """把证据分成「可用的中文段落」与「英文原文」两池。

    一段中文资料能不能拿来作答，看两点：要么命中意图章节，要么与问题有共同词。
    只按「是不是中文」来挑会出事：问「阿莫西林和华法林」，库里那段中文讲的是
    抗菌药物滥用，一个字都没撞上，拿来答就是答非所问；这时宁可给英文原文加提示。
    提示条也靠这个函数判断，保证「说出来的语言」和「提示的语言」一致。
    """
    def answerable(d: dict) -> bool:
        return d.get("field") in fields or bool(d.get("hits"))

    zh = [d for d in docs if not _is_ascii(d["text"]) and answerable(d)]
    en = [d for d in docs if _is_ascii(d["text"])]
    return zh, en


def _label_excerpt(text: str, field: str) -> str:
    """说明书摘录：关键句 + 紧跟的一句。

    FDA 标签常把一句结论拆成两截（"…allergic to aspirin." 后面才是症状清单），
    只取一句会读成半句话，所以命中关键句后再顺延一句，凑到 150 字左右。
    """
    sents = _sentences(text)
    if not sents:
        return text[:240]
    eh = EN_HINT.get(field)

    def key(paired):
        i, s = paired
        return (1.0 if (eh and eh.search(s)) else 0.0, -i, min(len(s), 400))

    i, best = max(enumerate(sents), key=key)
    out = best
    j = i + 1
    while j < len(sents) and len(out) < 150:
        out = out.rstrip() + " " + sents[j].strip()
        j += 1
    out = re.sub(r"^[）)\]}】、，；;:：.．·\-\s]+", "", out.strip())
    if len(out) > 300:
        cut = out[:300]
        k = max(cut.rfind(". "), cut.rfind("; "))
        if k > 120:
            cut = cut[:k + 1]
        return cut + "…"
    return out


def compose(question: str, docs: list[dict], facts: list[dict], intent: dict,
            candidates: list[dict], anchors: list[str], diseases: list[str] | None = None) -> str:
    if not docs and not facts:
        return "当前知识库中没有检索到与该问题相关的权威资料，因此无法给出可靠回答。建议线下就诊咨询医生。"

    fields = intent.get("fields", [])
    # 药物类问题以说明书字段为准：中文语料里往往没有这种药的禁忌/剂量章节，
    # 硬在里面检索必然漂到别的药身上（布洛芬的禁忌被答成曲普坦就是一例）。
    label = [d for d in docs if d.get("kind") == "label"]
    zh, en = _split_pool(docs, fields)
    # 有中文资料就不用英文说明书撑场面；但药物问题反过来，说明书才是权威
    pool = label or zh or en or docs
    ranked = sorted(pool, key=lambda d: (0 if d["field"] in fields else 1, -d["score"]))

    lines: list[str] = []
    zh_used = 0
    zh_seen: list[str] = []
    if ranked:
        lines.append("根据知识库中的权威资料，整理如下要点：")
        for d in ranked[:3]:
            # 有中文译文的说明书章节：直接用中文要点作答，一条一句、各带引用。
            # 这些要点是人工逐条译好的（药物名称、剂量、阈值都照原文），
            # 不是运行时翻译，所以不存在译文漂移。
            # 条数要克制：三章各四条会变成十行清单，用户反而抓不住重点。
            if d.get("zh_points"):
                for p in d["zh_points"]:
                    if zh_used >= 6:
                        break
                    # 句内的分号会被逐句校验当成断句：前半有引用、后半没有，
                    # 于是正确的译文反而被判「无引用」。统一压成逗号，一句一引用。
                    s = re.sub(r"\s+", " ", p).strip().rstrip("。；.")
                    s = s.replace("；", "，")
                    if not s:
                        continue
                    # 不同章节常把同一句话说两遍（禁忌与黑框警告尤甚）
                    head = s[:14]
                    if any(head == h or head in h for h in zh_seen):
                        continue
                    zh_seen.append(head)
                    lines.append(f"{s}[{d['eid']}]。")
                    zh_used += 1
                if zh_used >= 6:
                    break
                continue
            if d["kind"] == "profile":
                # 图谱档案是一整段压缩信息，必须作为「一句话」输出：
                # 一旦被句号切开，前半段就会变成没有引用的断言，校验层会正确地把它们判为无依据。
                sent = re.sub(r"\s+", " ", d["text"]).rstrip("。").replace("。", "，").replace("；", "，").replace("\n", " ")
                if len(sent) > 175:
                    sent = sent[:175].rstrip("，、；") + "…"
            else:
                if d.get("kind") == "label":
                    sent = re.sub(r"\s+", " ", _label_excerpt(d["text"], d.get("field", ""))).rstrip("。；.")
                else:
                    sent = re.sub(r"\s+", " ", _best_sentence(d["text"], question, fields, anchors,
                                                              d.get("field", ""))).rstrip("。；.")
            tag = "（原文为英文说明书，此处直接摘录）" if _is_ascii(sent) else ""
            lines.append(f"{sent}{tag}[{d['eid']}]。")

    if facts:
        lines.append("知识图谱中与该问题直接相关的记录：" if ranked else "知识图谱中有以下直接记录：")
        for f in facts[:3]:
            lines.append(f"{f['text'].rstrip('。')}[{f['eid']}]。")

    # 只有在「从症状反推疾病」时才说"优先考虑"，否则用户问的就是这个病本身
    if intent["key"] == "differential" and (candidates or len(anchors) > 2):
        names = [c["name"] for c in candidates] or [graph.name(d) for d in (diseases or [])]
        if names:
            lines.insert(0, f"从症状与图谱中的表现关系看，需要优先考虑：{'、'.join(names[:3])}。")

    lines.append("以上为资料摘录，不能替代医生面诊；如果症状持续或加重，请及时线下就医")
    return "\n".join(lines)


def compose_emergency(red_flags: list[str], evidence: list[dict]) -> str:
    flag = "、".join(red_flags)
    cite = f"[{evidence[0]['eid']}]" if evidence else ""
    return (
        f"请立即就医。您提到的「{flag}」属于需要紧急处理的警示症状，不适合在线等待判断。\n"
        "出现突发剧烈胸痛或胸闷持续不缓解、呼吸困难或口唇发紫、意识模糊、晕厥、抽搐、"
        f"突发一侧肢体无力或言语不清、大量呕血或黑便等情况时，应立即就医或拨打急救电话{cite}。\n"
        "等待救援期间请保持安静，不要自行驾车前往，也不要自行服用药物。"
    )


# ------------------------------------------------------------------ 结构化速览
# 与 ai_tp 的「定位 / 表现 / 用药 / 并发症」一栏同源：直接从图谱的一跳边分组，
# 不依赖意图命中，所以不管用户怎么问，这一栏永远是这个实体的完整画像。
STRUCT_GROUPS = [
    ("定位", ("belongsToSystem", "locatedIn"), 4),
    ("主要表现", ("hasSymptom", "manifestsAs"), 8),
    ("常用药物", ("treatedBy", "treatedByClass", "memberOf"), 8),
    ("可能并发症", ("mayCause",), 6),
    ("病因与传播", ("causedBy", "transmittedBy", "reservoirIn"), 5),
    ("鉴别诊断", ("differentialWith",), 5),
    ("预防措施", ("preventedBy", "controlledBy", "highRiskGroup"), 5),
    ("禁用于", ("contraindicatedIn",), 6),
]

# 同一个谓词，站在疾病和站在药物两端看，意思正好反过来：
# 「偏头痛 —治疗用药→ 布洛芬」对药是「用于治疗」，对病是「常用药物」。
LABEL_BY_TYPE = {
    "Disease": {"禁用于": "禁用药物", "可能并发症": "可能并发症"},
    "Drug": {"常用药物": "用于治疗的疾病", "可能并发症": "相关不良反应"},
    "Symptom": {"定位": "常见于", "常用药物": "相关药物"},
    "Pathogen": {"主要表现": "所致疾病", "常用药物": "相关药物"},
}


def build_structure(eid: str) -> list[dict]:
    """把一个实体的一跳边按语义分组，供前端渲染速览卡。"""
    if not eid or not graph.node(eid):
        return []
    n = graph.node(eid)
    etype = n.get("t", "")
    relabel = LABEL_BY_TYPE.get(etype, {})
    edges = list(graph.out.get(eid, [])) + list(graph.inc.get(eid, []))
    groups = []
    for label, preds, cap in STRUCT_GROUPS:
        seen, items = set(), []
        for e in edges:
            if e["r"] not in preds:
                continue
            other = e["t"] if e["h"] == eid else e["h"]
            if other == eid or other in seen:
                continue
            nm = graph.name(other)
            if not nm:
                continue
            seen.add(other)
            items.append({"id": other, "name": nm})
        if items:
            groups.append({"label": relabel.get(label, label),
                           "items": items[:cap], "total": len(items)})
    return groups


def _safe_answer(diseases: list[str], candidates: list[dict]) -> str:
    dept = "相应专科"
    target = None
    if candidates:
        target = graph.node(candidates[0]["id"])
    elif diseases:
        target = graph.node(diseases[0])
    if target:
        dept = (target.get("p") or {}).get("department") or dept
    return ("根据当前可检索到的权威资料，这个问题我无法给出足够可靠的回答。"
            "为避免误导，我不会提供推测性内容。"
            f"建议您携带既往检查与用药记录，到{dept}就诊，由医生结合面诊与检查判断。")


# ------------------------------------------------------------------ 主流程
def analyze(question: str, use_llm: bool = False, inject_hallucination: bool = False,
            deep_think: bool = False, kb_only: bool = True,
            history: list[dict] | None = None) -> dict:
    """问答主流程：检索资料作上下文 + 提示词约束模型行为。

    deep_think / kb_only 共同决定 system 提示词（4 种组合各一套规则，见 _system_prompt），
    不是简单的「加一句话」。
    history：本次对话之前几轮的 [{q, brief}]，既用于理解指代，也用于检索补位。
    """
    t0 = time.time()
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")

    # ── 检索：锚定实体 + 意图 + 找资料（作为 RAG 上下文） ──
    mentions, from_history = _resolve_mentions(question, history)
    # 靠上文补位时点明主题，避免模型把「追问」当新问题而误答找不到
    topic_hint = mentions[0]["name"] if (from_history and mentions) else ""
    it = intent_mod.classify(question, mentions)

    diseases = [m["id"] for m in mentions if m["type"] == "Disease"]
    symptoms = [m["id"] for m in mentions if m["type"] == "Symptom"]
    drugs = [m["id"] for m in mentions if m["type"] == "Drug"]
    others = [m["id"] for m in mentions if m["type"] not in ("Disease", "Symptom", "Drug")]

    candidates: list[dict] = []
    if not diseases and symptoms:
        candidates = candidate_diseases(symptoms)
        diseases = [c["id"] for c in candidates]

    seeds = list(dict.fromkeys(diseases[:3] + symptoms[:4] + drugs[:3] + others[:2]))
    docs = retrieve(question, seeds, it, top_k=7)
    facts = fact_evidence(seeds, it, limit=6)

    drug_id = drugs[0] if drugs else ""
    drug_name = graph.name(drug_id) if drug_id else ""
    label_fields: list[dict] = []
    if drug_id:
        try:
            label_fields = browse.label_sections(drug_id, it.get("fields", []), limit=3)
        except Exception:  # noqa: BLE001
            label_fields = []
        for d in reversed(label_fields):
            docs.insert(0, {
                "kind": "label", "doc_id": f"label:{drug_id}:{d['field']}",
                "title": f"{drug_name} · {d['label']}", "field": d["field"],
                "field_label": d["label"], "doc_kind": "drug",
                "text": d["content"], "snippet": d["content"][:160],
                "hits": [], "source": d["source"] or "openFDA 药品标签",
                "year": None, "url": "", "tags": [drug_id],
                "owner": drug_id, "owner_name": drug_name,
                "score": 999.0, "path": "label", "why": "说明书字段直取",
                "zh_points": browse.zh_points(drug_id, d["field"]),
            })

    cover = {d["owner"] for d in docs if d["kind"] == "profile"}
    if cover:
        facts = [f for f in facts if f["h"] not in cover]

    for i, d in enumerate(docs, start=1):
        d["eid"] = f"E{i}"
    base = len(docs)
    for i, f in enumerate(facts, start=1):
        f["eid"] = f"E{base + i}"
        f["title"] = f"{f['h_name']} · {f['r_label']}"
        f["field_label"] = "图谱事实"
        f["tags"] = [f["h"], f["t"]]
        f["score"] = round(f["w"] * 10, 2)
        f["path"] = "graph"
    evidence = docs + facts

    # ── 生成：提示词约束模型（检索资料作上下文） ──
    mode = "llm"
    model_name = ""
    out_of_kb = False
    try:
        # 非 kb_only 且未锚定到实体时，全文召回的多半无关，不喂给模型，让它自由回答
        use_docs = docs if (kb_only or mentions) else []
        use_facts = facts if (kb_only or mentions) else []
        answer, model_name, out_of_kb = _compose_llm(
            question, use_docs, use_facts, it, deep_think=deep_think, kb_only=kb_only,
            history=history, topic_hint=topic_hint)
        if not answer:
            answer = KB_NOT_FOUND if kb_only else _safe_answer(diseases, candidates)
            mode = "offline"
        # 防御：模型明明有资料却回了「找不到」（常见于症状鉴别类问题，qwen3:4b 会
        # 因资料没逐字回答「是/不是」而误判）。证据块非空时，绝不把拒答落地——
        # 改成一句基于证据的中立说明，提示词本身也会尽量压掉这种情况，这里是兜底。
        elif kb_only and _looks_like_not_found(answer) and evidence:
            answer = _evidence_based_fallback(question, evidence)
            mode = "offline"
        # 超纲判断：非 kb_only 且未锚定到实体 → 模型是自行作答
        if not kb_only:
            out_of_kb = not mentions
    except llm.LLMError as exc:
        answer = f"模型未参与生成（{exc}）。"
        mode = "offline"
    except (ValueError, KeyError) as exc:
        answer = f"本地模型输出无法解析（{exc}）。"
        mode = "offline"

    # ── 展示增强（结构化速览 / 趋势图，非核心） ──
    main_id = mentions[0]["id"] if mentions else (diseases[0] if diseases else "")
    try:
        structure = build_structure(main_id)
    except Exception:  # noqa: BLE001
        structure = []

    tl = None
    disease_for_tl = next((m["id"] for m in mentions if m["type"] == "Disease"), "")
    if not disease_for_tl and candidates:
        disease_for_tl = candidates[0]["id"]
    if disease_for_tl:
        try:
            t = browse.timeline(disease_for_tl)
            if t.get("available"):
                tl = t
        except Exception:  # noqa: BLE001
            tl = None

    # brief：下一轮的精简上下文（深度思考取小结，快速取全文；去掉免责声明）
    _ans = (answer or "").replace(DISCLAIMER, "").strip()
    brief = extract_summary(_ans) if deep_think else _ans

    return {
        "question": question,
        "intent": {"key": it["key"], "label": it["label"], "fields": it.get("fields", [])},
        "mentions": mentions,
        "entities": [{"id": m["id"], "name": m["name"], "type": m["type"],
                      "type_label": m["type_label"]} for m in mentions],
        "candidates": candidates,
        "evidence": evidence,
        "answer": answer,
        "emergency": False,
        "red_flags": [],
        "mode": mode,
        "model": model_name,
        "out_of_kb": out_of_kb,
        "kb_only": kb_only,
        "structure": structure,
        "timeline": tl,
        "drug": {"id": drug_id, "name": drug_name,
                 "fields": [{"field": f["field"], "label": f["label"], "chars": f["chars"]}
                            for f in label_fields]} if drug_id else None,
        "disclaimer": DISCLAIMER,
        "brief": brief,
        "latency_ms": int((time.time() - t0) * 1000),
    }


def analyze_stream(question: str, deep_think: bool = False, kb_only: bool = True,
                   history: list[dict] | None = None):
    """流式问答生成器。yield 协议：

      · yield ("meta", {检索元数据})      —— 检索完成后先发元数据
      · yield ("delta", "文本分片")       —— 答案逐块输出
      · yield ("done", {最终元数据})       —— 流结束（含 mode/model/out_of_kb 等）

    检索逻辑与 analyze 完全一致，只有生成阶段改成流式。
    history：本次对话之前几轮的 [{q, brief}]，只用于理解指代。
    """
    t0 = time.time()
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")

    mentions, from_history = _resolve_mentions(question, history)
    # 靠上文补位时点明主题，避免模型把「追问」当新问题而误答找不到
    topic_hint = mentions[0]["name"] if (from_history and mentions) else ""
    it = intent_mod.classify(question, mentions)

    diseases = [m["id"] for m in mentions if m["type"] == "Disease"]
    symptoms = [m["id"] for m in mentions if m["type"] == "Symptom"]
    drugs = [m["id"] for m in mentions if m["type"] == "Drug"]
    others = [m["id"] for m in mentions if m["type"] not in ("Disease", "Symptom", "Drug")]

    candidates: list[dict] = []
    if not diseases and symptoms:
        candidates = candidate_diseases(symptoms)
        diseases = [c["id"] for c in candidates]

    seeds = list(dict.fromkeys(diseases[:3] + symptoms[:4] + drugs[:3] + others[:2]))
    docs = retrieve(question, seeds, it, top_k=7)
    facts = fact_evidence(seeds, it, limit=6)

    drug_id = drugs[0] if drugs else ""
    drug_name = graph.name(drug_id) if drug_id else ""
    label_fields: list[dict] = []
    if drug_id:
        try:
            label_fields = browse.label_sections(drug_id, it.get("fields", []), limit=3)
        except Exception:  # noqa: BLE001
            label_fields = []
        for d in reversed(label_fields):
            docs.insert(0, {
                "kind": "label", "doc_id": f"label:{drug_id}:{d['field']}",
                "title": f"{drug_name} · {d['label']}", "field": d["field"],
                "field_label": d["label"], "doc_kind": "drug",
                "text": d["content"], "snippet": d["content"][:160],
                "hits": [], "source": d["source"] or "openFDA 药品标签",
                "year": None, "url": "", "tags": [drug_id],
                "owner": drug_id, "owner_name": drug_name,
                "score": 999.0, "path": "label", "why": "说明书字段直取",
                "zh_points": browse.zh_points(drug_id, d["field"]),
            })

    cover = {d["owner"] for d in docs if d["kind"] == "profile"}
    if cover:
        facts = [f for f in facts if f["h"] not in cover]

    for i, d in enumerate(docs, start=1):
        d["eid"] = f"E{i}"
    base = len(docs)
    for i, f in enumerate(facts, start=1):
        f["eid"] = f"E{base + i}"
        f["title"] = f"{f['h_name']} · {f['r_label']}"
        f["field_label"] = "图谱事实"
        f["tags"] = [f["h"], f["t"]]
        f["score"] = round(f["w"] * 10, 2)
        f["path"] = "graph"
    evidence = docs + facts

    # 展示增强（与 analyze 一致）
    main_id = mentions[0]["id"] if mentions else (diseases[0] if diseases else "")
    try:
        structure = build_structure(main_id)
    except Exception:  # noqa: BLE001
        structure = []

    tl = None
    disease_for_tl = next((m["id"] for m in mentions if m["type"] == "Disease"), "")
    if not disease_for_tl and candidates:
        disease_for_tl = candidates[0]["id"]
    if disease_for_tl:
        try:
            t = browse.timeline(disease_for_tl)
            if t.get("available"):
                tl = t
        except Exception:  # noqa: BLE001
            tl = None

    meta = {
        "question": question,
        "intent": {"key": it["key"], "label": it["label"], "fields": it.get("fields", [])},
        "mentions": mentions,
        "entities": [{"id": m["id"], "name": m["name"], "type": m["type"],
                      "type_label": m["type_label"]} for m in mentions],
        "candidates": candidates,
        "structure": structure,
        "timeline": tl,
        "drug": {"id": drug_id, "name": drug_name,
                 "fields": [{"field": f["field"], "label": f["label"], "chars": f["chars"]}
                            for f in label_fields]} if drug_id else None,
    }
    yield "meta", meta

    # 生成阶段
    mode = "llm"
    model_name = ""
    out_of_kb = False
    full = ""
    # 提前初始化：生成阶段异常时 done 部分仍会读它，避免 NameError
    use_docs = docs if (kb_only or mentions) else []
    use_facts = facts if (kb_only or mentions) else []
    try:
        gen = _compose_llm_stream(question, use_docs, use_facts, it,
                                  deep_think=deep_think, kb_only=kb_only,
                                  history=history, topic_hint=topic_hint)
        for item in gen:
            if isinstance(item, tuple) and len(item) == 3 and item[0] is None:
                # (None, model, buf) 结束标记
                _, model_name, full = item
            else:
                piece = item
                full += piece
                yield "delta", {"text": piece}

        if not full or len(full.strip()) < 4:
            full = KB_NOT_FOUND if kb_only else _safe_answer(diseases, candidates)
            mode = "offline"
            yield "delta", {"text": full}
        if not kb_only:
            out_of_kb = not mentions
    except llm.LLMError as exc:
        full = f"模型未参与生成（{exc}）。"
        mode = "offline"
        yield "delta", {"text": full}
    except (ValueError, KeyError) as exc:
        full = f"本地模型输出无法解析（{exc}）。"
        mode = "offline"
        yield "delta", {"text": full}

    # 免责声明只在「实际用了知识库正文」时补在末尾：图谱事实类问题没有引用正文，
    # 不该显示"整理自公开医学资料"；超纲自由回答（没喂 docs）同理。
    # 注意用 use_docs（真正喂给模型的正文），不是原始 docs（全文检索总会召回几条）。
    has_docs = bool(use_docs)
    if has_docs and DISCLAIMER not in full:
        yield "delta", {"text": "\n\n" + DISCLAIMER}

    # brief 是下一轮对话要喂给模型的精简上下文：深度思考取「小结」，快速模式取全文
    brief = extract_summary(full) if deep_think else full.strip()

    yield "done", {
        "mode": mode,
        "model": model_name,
        "out_of_kb": out_of_kb,
        "kb_only": kb_only,
        "has_docs": has_docs,
        "has_mentions": bool(mentions),
        "disclaimer": DISCLAIMER,
        "brief": brief,
        "latency_ms": int((time.time() - t0) * 1000),
    }


# ------------------------------------------------------------------ 供前端用的聚合查询
def entity_detail(eid: str, doc_limit: int = 12) -> dict | None:
    n = graph.node(eid)
    if not n:
        return None
    rel = graph.neighbours(eid, limit=60)
    grouped: dict[str, list[dict]] = {}
    for r in rel:
        grouped.setdefault(r["rel"], []).append(r)
    docs = store.of_entity(eid)
    return {
        "id": eid, "name": n["n"], "type": n["t"],
        "type_label": graph.type_label(n["t"]),
        "type_color": graph.types.get(n["t"], {}).get("color", "#6b7a8f"),
        "props": n.get("p", {}),
        "aliases": n.get("a", []),
        "summary": n.get("s", ""),
        "system": n.get("sys", ""),
        "region": n.get("reg", ""),
        "degree": n["d"],
        "source": n.get("src", ""),
        "url": n.get("u", ""),
        "clinic": bool(n.get("clinic")),
        "relations": [
            {"rel": k, "label": graph.rel_label(k), "items": v[:12], "total": len(v)}
            for k, v in sorted(grouped.items(), key=lambda x: -len(x[1]))
        ],
        "docs": [{
            "id": d["id"], "title": d["ti"], "field": d["f"],
            "field_label": store.fields.get(d["f"], d["f"]),
            "kind": d["k"], "source": d["src"],
            "chars": len(d["t"]), "excerpt": d["t"][:150] + ("…" if len(d["t"]) > 150 else ""),
        } for d in docs[:doc_limit]],
        "doc_total": len(docs),
    }
