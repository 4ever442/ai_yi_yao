"""本地大模型接入（Ollama，默认 qwen3:4b）。

这一层只做一件事：把检索到的证据改写成两三句中文要点。
找资料、定引用编号都由检索层完成，模型不参与——所以它即使胡说，逐句校验也能
指出是哪一句没有依据，而不是让整段答案失去可核对性。

几个来自实测的约束（别轻易改）：
1. **必须用 format="json" 约束解码。** qwen3:4b 在自由文本模式下会把推理过程
   直接写进正文（"首先，用户的问题是…"），600 token 全被推理吃掉，答案根本出不来；
   开 think 让它走独立通道更要命——实测单题 45 秒、思考 5000 字，最后还答"证据不足"。
   加上 JSON 约束后同样的题只要 0.5~2 秒，输出干净。
2. **temperature 要低**（0.1）。医学答案不需要创造性。
3. **不可用必须快速失败**。探测结果缓存 20 秒，避免 Ollama 没开时每次提问都白等。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# 云端部署默认走 Agnes AI（OpenAI 兼容），无需本地 Ollama。
# 环境变量可覆盖：MEDQA_API_KEY / MEDQA_BASE_URL / MEDQA_MODEL
DEFAULT_BASE = "https://apihub.agnes-ai.com/v1"
DEFAULT_MODEL = "agnes-2.5-flash"
OLLAMA_BASE = "http://127.0.0.1:11434"
OLLAMA_MODEL = "qwen3:4b"
PROBE_TTL = 20.0

_cache: dict = {"ts": 0.0, "key": None, "res": None}

# 本机地址必须绕过系统代理。开发环境里常配了 HTTP_PROXY，urllib 会照样把
# 127.0.0.1:11434 交给代理，结果拿到一段 HTML 错误页，报错信息就成了
# 「返回内容无法解析」——真实原因是根本没连上 Ollama。
_LOCAL = ("127.0.0.1", "localhost", "::1", "0.0.0.0")
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 10):
    host = urllib.parse.urlsplit(url).hostname or ""
    opener = _DIRECT if host in _LOCAL else urllib.request.build_opener()
    req = urllib.request.Request(url, data=data, headers=headers or {})
    return opener.open(req, timeout=timeout)


def mode() -> str:
    """默认走云端 OpenAI 兼容接口（Agnes），显式设置本地模型时才回 Ollama。

    判定优先级：
      1. 显式 MEDKG_LLM_BASE 指向本机 Ollama（含 127.0.0.1）→ ollama
      2. 否则 → openai（云端 API）
    """
    base = (os.environ.get("MEDKG_LLM_BASE") or "").strip()
    if base and (base.startswith("http://127.0.0.1")
                 or base.startswith("http://localhost")
                 or "11434" in base):
        return "ollama"
    return "openai"


def config() -> dict:
    m = mode()
    if m == "openai":
        base = os.environ.get("MEDQA_BASE_URL") or os.environ.get("MEDKG_LLM_BASE") or DEFAULT_BASE
        model = os.environ.get("MEDQA_MODEL") or os.environ.get("MEDKG_LLM_MODEL") or DEFAULT_MODEL
    else:
        base = os.environ.get("MEDKG_LLM_BASE") or OLLAMA_BASE
        model = os.environ.get("MEDKG_LLM_MODEL") or OLLAMA_MODEL
    return {"mode": m, "base": base.rstrip("/"), "model": model}


def enabled() -> bool:
    return os.environ.get("MEDKG_LLM", "1").lower() not in ("0", "off", "false", "no")


def _match_model(name: str, want: str) -> bool:
    """qwen3:4b 与 qwen3:4b-instruct 这类同族名字要能对上，但不能跨族。"""
    a, b = name.strip(), want.strip()
    if a == b:
        return True
    if ":" not in b:
        return a.split(":")[0] == b
    return False


def probe(force: bool = False) -> dict:
    """返回 {ok, mode, base, model, detail, ms}。20 秒内复用，避免反复白等。"""
    cfg = config()
    key = (cfg["mode"], cfg["base"], cfg["model"])
    now = time.time()
    if not force and _cache["res"] and _cache["key"] == key and now - _cache["ts"] < PROBE_TTL:
        return _cache["res"]

    t0 = time.time()
    res = {"ok": False, "mode": cfg["mode"], "base": cfg["base"], "model": cfg["model"],
           "detail": "", "ms": 0}
    if not enabled():
        res["detail"] = "已通过 MEDKG_LLM=0 关闭"
    elif cfg["mode"] == "openai":
        if not os.environ.get("MEDQA_API_KEY"):
            res["ok"] = False
            res["detail"] = "未配置 API Key（环境变量 MEDQA_API_KEY）"
        else:
            res["ok"] = True
            res["detail"] = f"使用云端接口 {cfg['base']} / {cfg['model']}"
    else:
        try:
            with _open(cfg["base"] + "/api/tags", timeout=1.5) as r:
                data = json.loads(r.read().decode("utf-8"))
            names = [m.get("name", "") for m in data.get("models", [])]
            hit = [n for n in names if _match_model(n, cfg["model"])]
            if hit:
                res["ok"] = True
                res["model"] = hit[0]
                res["detail"] = f"已连接 {cfg['base']}"
            else:
                res["detail"] = (f"本机没有 {cfg['model']} 模型，先执行 "
                                 f"ollama pull {cfg['model']}")
        except urllib.error.URLError:
            res["detail"] = "连不上 Ollama。请确认 ollama 已在运行（ollama serve）"
        except ValueError:
            res["detail"] = "Ollama 返回了无法解析的内容"
    res["ms"] = int((time.time() - t0) * 1000)
    _cache.update(ts=now, key=key, res=res)
    return res


def available() -> bool:
    return bool(probe()["ok"])


# ---------------------------------------------------------------- 调用
class LLMError(RuntimeError):
    """模型不可用或返回不可解析的内容。调用方应回退到证据摘录模式。"""


def chat_json(system: str, user: str, timeout: float = 120.0,
              max_tokens: int = 600, temperature: float = 0.1) -> dict:
    """带 JSON 约束的调用，返回解析后的 dict。失败抛 LLMError。"""
    p = probe()
    if not p["ok"]:
        raise LLMError(p["detail"] or "本地模型不可用")

    text = _openai_json(system, user, timeout, max_tokens, temperature) \
        if p["mode"] == "openai" else \
        _ollama_json(system, user, timeout, max_tokens, temperature)
    return _parse_json(text)


def chat(user: str, timeout: float = 120.0, max_tokens: int = 800,
         temperature: float = 0.7) -> str:
    """纯文本直连：不加 system 提示词、不加 JSON 约束，模型自由回答。

    直接把用户消息发给 Ollama，返回模型原始文本。失败抛 LLMError。
    """
    return chat_with_system(None, user, timeout=timeout, max_tokens=max_tokens,
                             temperature=temperature)


def chat_with_system(system: str | None, user: str, timeout: float = 120.0,
                     max_tokens: int = 4000, temperature: float = 0.7,
                     think: bool | None = None) -> str:
    """带可选 system 提示词的纯文本调用。

    think 参数控制思考模式：
      · think=True  —— 强制模型思考（深度思考）
      · think=False —— 关闭思考（快速回答）
      · think=None  —— 由模型自行决定（默认）
    max_tokens 默认 4000：qwen3 思考时 thinking 会占掉 1~2k token，
    太小会把 answer 截断成空（done_reason=length）。
    """
    p = probe()
    if not p["ok"]:
        raise LLMError(p["detail"] or "模型不可用")

    cfg = config()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    if cfg["mode"] == "openai":
        return _openai_chat(messages, timeout=timeout, max_tokens=max_tokens,
                            temperature=temperature)

    body = {
        "model": cfg["model"],
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    if think is not None:
        body["think"] = think
    try:
        with _open(cfg["base"] + "/api/chat", data=json.dumps(body).encode("utf-8"),
                   headers={"Content-Type": "application/json"}, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:  # noqa: BLE001
            detail = f"HTTP {exc.code}"
        raise LLMError(f"模型接口报错：{detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError("连不上 Ollama，请确认服务在运行") from exc
    except TimeoutError as exc:
        raise LLMError("模型响应超时") from exc

    msg = data.get("message") or {}
    content = (msg.get("content") or "").strip()
    # 思考把 token 耗尽导致 answer 为空时，明确抛错让上层回退，而不是静默返回空串
    if not content and data.get("done_reason") == "length":
        raise LLMError("模型思考过长，输出被截断")
    return content


def chat_stream(system: str | None, user: str, timeout: float = 180.0,
                max_tokens: int = 4000, temperature: float = 0.7,
                think: bool | None = None):
    """流式调用：yield 模型逐块生成的 content 文本。

    与 chat_with_system 同参数，但 stream=True，边生成边返回，
    供前端做打字机效果。失败在迭代时抛 LLMError。

    qwen3 的思考（thinking）单独走 message.thinking 字段，content 里只含正文，
    所以流式只需累加 message.content 的增量即可。
    """
    p = probe()
    if not p["ok"]:
        raise LLMError(p["detail"] or "模型不可用")

    cfg = config()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    if cfg["mode"] == "openai":
        yield from _openai_stream(messages, timeout=timeout, max_tokens=max_tokens,
                                  temperature=temperature)
        return

    body = {
        "model": cfg["model"],
        "messages": messages,
        "stream": True,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    if think is not None:
        body["think"] = think

    try:
        resp = _open(cfg["base"] + "/api/chat", data=json.dumps(body).encode("utf-8"),
                     headers={"Content-Type": "application/json"}, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:  # noqa: BLE001
            detail = f"HTTP {exc.code}"
        raise LLMError(f"模型接口报错：{detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError("连不上 Ollama，请确认服务在运行") from exc
    except TimeoutError as exc:
        raise LLMError("模型响应超时") from exc

    # 逐行读取 NDJSON：Ollama 流式响应每行一个 JSON 对象
    with resp:
        for line in resp:
            line = line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except (ValueError, TypeError):
                continue
            if chunk.get("done"):
                break
            msg = chunk.get("message") or {}
            piece = msg.get("content")
            if piece:
                yield piece


def _ollama_json(system, user, timeout, max_tokens, temperature) -> str:
    cfg = config()
    body = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        # 关掉思考：不关的话推理过程会写进正文，答案直接被截断
        "think": False,
        # 关键：约束解码成 JSON。没有这个，4B 模型会先长篇推理再作答
        "format": "json",
        "options": {"temperature": temperature, "num_predict": max_tokens,
                    "top_p": 0.9, "repeat_penalty": 1.05},
    }
    try:
        with _open(cfg["base"] + "/api/chat", data=json.dumps(body).encode("utf-8"),
                   headers={"Content-Type": "application/json"}, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:  # noqa: BLE001 — 读不到就用状态码
            detail = f"HTTP {exc.code}"
        raise LLMError(f"模型接口报错：{detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError("连不上 Ollama，请确认服务在运行") from exc
    except TimeoutError as exc:
        raise LLMError("模型响应超时") from exc

    msg = data.get("message") or {}
    if data.get("done_reason") == "length":
        # 输出被截断时 JSON 多半不完整，交给上层回退
        raise LLMError("模型输出被截断")
    return (msg.get("content") or "").strip()


def _openai_headers() -> dict:
    return {"Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ.get('MEDQA_API_KEY', '')}"}


def _openai_chat(messages, timeout, max_tokens, temperature) -> str:
    """OpenAI 兼容纯文本调用（Agnes 等云端模型）。返回完整正文。"""
    cfg = config()
    body = {
        "model": cfg["model"],
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        with _open(cfg["base"] + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                   headers=_openai_headers(), timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {})
            if isinstance(detail, dict):
                detail = detail.get("message", "")
        except Exception:  # noqa: BLE001
            detail = f"HTTP {exc.code}"
        raise LLMError(f"模型接口报错：{detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"模型接口不可用：{exc}") from exc
    except TimeoutError as exc:
        raise LLMError("模型响应超时") from exc

    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise LLMError("模型返回内容无法解析")


def _openai_stream(messages, timeout, max_tokens, temperature):
    """OpenAI 兼容流式调用：解析 SSE，逐块 yield 正文增量。"""
    cfg = config()
    body = {
        "model": cfg["model"],
        "messages": messages,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        resp = _open(cfg["base"] + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                     headers=_openai_headers(), timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {})
            if isinstance(detail, dict):
                detail = detail.get("message", "")
        except Exception:  # noqa: BLE001
            detail = f"HTTP {exc.code}"
        raise LLMError(f"模型接口报错：{detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"模型接口不可用：{exc}") from exc
    except TimeoutError as exc:
        raise LLMError("模型响应超时") from exc

    # SSE 逐行解析：data: {json}\n\n
    with resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except (ValueError, TypeError):
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if piece:
                yield piece


def _openai_json(system, user, timeout, max_tokens, temperature) -> str:
    cfg = config()
    body = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    try:
        with _open(cfg["base"] + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                   headers={"Content-Type": "application/json",
                            "Authorization": f"Bearer {os.environ.get('MEDQA_API_KEY', '')}"},
                   timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError) as exc:
        raise LLMError(f"模型接口不可用：{exc}") from exc
    return (data["choices"][0]["message"]["content"] or "").strip()


def _parse_json(text: str) -> dict:
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s.split("\n", 1)[1] if "\n" in s else s
    try:
        obj = json.loads(s)
    except ValueError as exc:
        raise LLMError("模型返回的不是合法 JSON") from exc
    if not isinstance(obj, dict):
        raise LLMError("模型返回的 JSON 结构不对")
    # Ollama 的 format=json 约束解码失败时，会把错误本身塞进 content：
    # {"error": "The given input is not a valid JSON object."}
    # 必须当成失败抛出，好让上层降级为纯文本重试，而不是当成「空答案」。
    if obj.get("error"):
        raise LLMError(f"模型 JSON 约束解码失败：{obj['error']}")
    return obj
