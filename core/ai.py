"""AI 接入层（预留位）。

产品上「AI 改写」是一个可选档位，不是必需依赖：

  · 关闭时（默认）：问答走**证据摘录**，3~30 毫秒出结果，每个字都来自被引原文；
  · 打开时：调用本地模型把检索到的证据改写成通顺中文要点。
    模型只改写，不找资料、不决定引用哪一条，写歪了会被逐句校验拦下。

接入一个新的模型运行时，只需要改 `generate()` 这一个函数——其余代码只认
`ai.ready()` / `ai.status()` / `ai.generate()`，不直接碰 HTTP 细节。

配置优先级：环境变量 > data/ai_config.json > 内置默认值。
环境变量：MEDKG_AI=1/0（总开关）、MEDKG_AI_BASE、MEDKG_AI_MODEL。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import llm

ROOT = Path(__file__).resolve().parent.parent
CONF = ROOT / "data" / "ai_config.json"

DEFAULTS = {"enabled": True, "provider": "openai",
            "base": llm.DEFAULT_BASE, "model": llm.DEFAULT_MODEL}

# 接入说明：前端「AI 设置」面板直接展示这段，避免文档和界面对不上
HINT_OFF = "AI 改写当前关闭，问答使用证据摘录（每个字都来自被引原文，毫秒级）。"
HINT_NO_RUNTIME = ("未配置云端模型。请设置环境变量 MEDQA_API_KEY（Agnes API Key），"
                   "或在下方填写自定义地址与模型。")
HINT_OK = "已接入云端模型，问答会先让它把证据改写成中文要点，再逐句校验。"


def _load() -> dict:
    if not CONF.is_file():
        return {}
    try:
        data = json.loads(CONF.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def settings() -> dict:
    """当前生效的配置（环境变量优先）。"""
    saved = _load()
    out = dict(DEFAULTS)
    out.update({k: v for k, v in saved.items() if k in DEFAULTS})

    env_on = os.environ.get("MEDKG_AI")
    if env_on is not None:
        out["enabled"] = env_on.strip().lower() not in ("0", "off", "false", "no", "")
    if os.environ.get("MEDKG_AI_BASE"):
        out["base"] = os.environ["MEDKG_AI_BASE"].strip()
    if os.environ.get("MEDKG_AI_MODEL"):
        out["model"] = os.environ["MEDKG_AI_MODEL"].strip()
    return out


def _apply(cfg: dict) -> None:
    """把本层配置同步给底层 llm，让两个模块看到同一份设置。"""
    os.environ["MEDKG_LLM"] = "1" if cfg["enabled"] else "0"
    os.environ["MEDKG_LLM_BASE"] = cfg["base"]
    os.environ["MEDKG_LLM_MODEL"] = cfg["model"]


def save(patch: dict) -> dict:
    """保存配置。允许只传一个字段，其余保持不变。"""
    cur = dict(DEFAULTS)
    cur.update({k: v for k, v in _load().items() if k in DEFAULTS})
    for k in ("enabled", "base", "model", "provider"):
        if k in patch and patch[k] is not None:
            cur[k] = patch[k]
    cur["enabled"] = bool(cur["enabled"])
    cur["base"] = str(cur["base"]).rstrip("/") or DEFAULTS["base"]
    cur["model"] = str(cur["model"]) or DEFAULTS["model"]

    CONF.parent.mkdir(parents=True, exist_ok=True)
    CONF.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    _apply(cur)
    return settings()


def status(force: bool = False) -> dict:
    """给前端用的状态：开没开、连没连、用哪个模型、该给什么提示。"""
    cfg = settings()
    t0 = time.time()
    probe = llm.probe(force=force) if cfg["enabled"] else {"ok": False, "detail": "AI 改写已关闭"}
    connected = bool(probe.get("ok"))

    if not cfg["enabled"]:
        hint = HINT_OFF
    elif connected:
        hint = HINT_OK
    else:
        hint = HINT_NO_RUNTIME

    return {
        "enabled": cfg["enabled"],
        "provider": cfg["provider"],
        "base": cfg["base"],
        "model": cfg["model"],
        "connected": connected,
        "detail": probe.get("detail", ""),
        "hint": hint,
        "ms": int((time.time() - t0) * 1000),
    }


def ready() -> bool:
    """是否可以走模型生成：开关打开且运行时可用。"""
    cfg = settings()
    return bool(cfg["enabled"]) and bool(llm.probe().get("ok"))


# ------------------------------------------------------------------ 接入点
def generate(system: str, user: str, timeout: float = 120.0, max_tokens: int = 400) -> dict:
    """把证据改写成 JSON 要点（旧契约，保留兼容）。"""
    return llm.chat_json(system, user, timeout=timeout, max_tokens=max_tokens)


def chat(user: str, timeout: float = 120.0, max_tokens: int = 800) -> str:
    """纯文本直连：不加任何提示词，模型自由回答，返回原始文本。"""
    return llm.chat(user, timeout=timeout, max_tokens=max_tokens)


def chat_with_system(system: str, user: str, timeout: float = 120.0,
                     max_tokens: int = 800, think: bool | None = None) -> str:
    """带 system 提示词的纯文本调用（think 控制思考模式）。"""
    return llm.chat_with_system(system, user, timeout=timeout, max_tokens=max_tokens,
                                think=think)


def chat_stream(system: str, user: str, timeout: float = 180.0,
                max_tokens: int = 4000, think: bool | None = None):
    """带 system 提示词的流式调用，逐块 yield 正文文本。"""
    return llm.chat_stream(system, user, timeout=timeout, max_tokens=max_tokens,
                           think=think)


# 模块加载时把配置同步下去，保证 llm.enabled() 与界面开关一致
_apply(settings())
