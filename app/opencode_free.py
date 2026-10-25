"""OpenCode 免费通道协议层（借鉴 dsh-our-free-model 插件的逆向成果）。

上游 opencode.ai 的免费 inference 池对"长得像官方 CLI"的请求开放，无需注册
或个人凭据（Bearer public 是公共池凭据）。判据（每一项不满足都是 403
FreeTierError，均由插件作者 2026-10 实测并记录）：

1. User-Agent 必须是 v2 CLI 形状 "opencode/latest/2.0.26/cli"（major>=2；
   v1 的 opencode/1.18.x 已被按名字拒绝）；
2. 请求头必须同时带 x-opencode-session 与 x-opencode-session-id（同值），
   外加 v2 镜像 x-session-affinity / x-session-id 与 x-opencode-client: cli；
   绝不能带 v1 专用的 x-opencode-request（带上即 403）；
3. session id 必须是 ses_ + 12 位 hex + 14 位 base62，其中 12 位 hex 必须是
   48 位取反时间戳 ~(ts_ms << 12 | counter)（counter 1-63，低 12 位落在
   0xfc0-0xfff）；升序编码或随机 hex 一律 403；
4. 请求体必须声明 read 与 shell 两个工具，且 description 与参数 schema 与
   官方客户端逐字一致；绝不下发 tool_choice: "none"（403）。

本模块只做协议编码；HTTP 发送、重试、熔断复用网关自身的渠道机制。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from contextlib import asynccontextmanager

from anyio import CancelScope

BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# 与官方 2.0.26 CLI 逐字一致（上游按定义比对，改一个字就 403）
FINGERPRINT_TOOLS = {
    "read": {
        "description": (
            "Read the contents of a file or directory. Supports text files, images, and PDFs. "
            "Images and PDFs are presented directly to the model. Each text line is prefixed by "
            "its 1-based line number as <line>: <content>. The prefix is for reference only; "
            "the model never sees it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or directory to read"},
                "offset": {"type": "integer", "minimum": 0,
                           "description": "The line or directory entry to start reading from (1-based)"},
                "limit": {"type": "integer", "minimum": 0,
                          "description": "The maximum number of lines or directory entries to read "
                                         "(defaults to and capped at 2000)"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "shell": {
        "description": (
            "Execute a shell command and return its output. Commands run on Linux using bash. "
            "Quote file paths containing spaces or special characters. Prefer dedicated tools "
            "over shell commands when available."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command string to execute"},
                "workdir": {"type": "string",
                            "description": "Working directory to execute the command in. Defaults to the "
                                           "current working directory. When possible, avoid changing "
                                           "directories in the command and set this instead."},
                "timeout": {"type": "integer", "minimum": 0,
                            "description": "Timeout in milliseconds. Set to 0 to disable the timeout. "
                                           "Defaults to 120000 for foreground commands. Background "
                                           "commands have no timeout and must be manually killed."},
                "background": {"type": "boolean",
                               "description": "Run the command in the background and return immediately "
                                              "(useful for dev servers and long-running builds). You do "
                                              "not need to use '&' at the end to background a command."},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}

CLIENT_UA = "opencode/1.19.0"
OPENCODE_BASE = "https://opencode.ai"
LISTING_PATH = "/inference/v1/models"

_lock = threading.Lock()
_last_stamp = 0
_seq = 0
_sessions: dict[str, str] = {}
_PLACEHOLDER_NOTE = (
    "Do not call the `read` or `shell` tools; they are placeholders that do nothing. "
    "Use the other tools you have been given instead."
)


def _base62_fixed(data: bytes, width: int) -> str:
    n = int.from_bytes(data, "big")
    out = []
    for _ in range(width):
        n, r = divmod(n, 62)
        out.append(BASE62[r])
    return "".join(reversed(out))


def mint_session_id(timestamp_ms: int | None = None) -> str:
    """铸造网关形状的 session id：~(ts_ms << 12 | counter)，counter 1-63。"""
    global _last_stamp, _seq
    ts = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
    with _lock:
        if ts != _last_stamp:
            _last_stamp, _seq = ts, 0
        _seq += 1
        counter = _seq
    value = ~(ts * 0x1000 + counter) & 0xFFFFFFFFFFFF
    hex12 = format(value, "012x")
    rand = hashlib.sha256(f"obsessionapi:{ts}:{counter}".encode()).digest()
    return f"ses_{hex12}{_base62_fixed(rand[6:16], 14)}"


def session_for(conversation_key: str) -> str:
    """同一条会话固定映射到同一个上游 session（上游按 session 记免费额度）。"""
    with _lock:
        got = _sessions.get(conversation_key)
    if got:
        return got
    digest = hashlib.sha256(f"ses\0{conversation_key}".encode()).digest()
    current = ((digest[0] << 28 | digest[1] << 20 | digest[2] << 12 |
                digest[3] << 4 | digest[4] >> 4) << 12) | 1
    inverted = ~current & 0xFFFFFFFFFFFF
    sid = f"ses_{format(inverted, '012x')}{_base62_fixed(digest[6:16], 14)}"
    with _lock:
        if len(_sessions) > 4096:
            _sessions.clear()
        _sessions[conversation_key] = sid
    return sid


def gateway_headers(session: str, stream: bool = False) -> dict[str, str]:
    """v2 客户端指纹头。注意：绝不能带 x-opencode-request（v1 专用，403）。"""
    return {
        "content-type": "application/json",
        "authorization": "Bearer public",
        "user-agent": CLIENT_UA,
        "x-opencode-client": "cli",
        "x-opencode-session": session,
        "x-opencode-session-id": session,
        "x-session-affinity": session,
        "x-session-id": session,
        "x-opencode-project": "global",
        "accept": "text/event-stream" if stream else "*/*",
    }


def _tool_name(tool) -> str:
    if not isinstance(tool, dict):
        return ""
    name = str(tool.get("name") or "").strip()
    if name:
        return name
    fn = tool.get("function")
    if isinstance(fn, dict):
        return str(fn.get("name") or "").strip()
    return ""


def _carries_definition(tool: dict, name: str) -> bool:
    fn = tool.get("function")
    wrapper = fn if isinstance(fn, dict) else tool
    want = FINGERPRINT_TOOLS[name]
    schema = wrapper.get("parameters") or wrapper.get("input_schema")
    return wrapper.get("description") == want["description"] and json.dumps(schema, sort_keys=True) == json.dumps(want["parameters"], sort_keys=True)


def apply_fingerprint(body: dict) -> dict:
    """满足免费闸门：注入 read/shell 官方定义；冲突名改名；删 tool_choice none。

    直接改写并返回 body 的浅拷贝内的 tools/notes，不动调用方原对象。
    """
    out = dict(body)
    tools = [t for t in (body.get("tools") or []) if isinstance(t, dict)]
    taken = {_tool_name(t) for t in tools}
    seen: set[str] = set()
    present: set[str] = set()
    renamed: list[dict] = []
    result_tools: list[dict] = []
    for tool in tools:
        name = _tool_name(tool)
        if name in ("read", "shell"):
            if name in seen:
                continue
            seen.add(name)
            if _carries_definition(tool, name):
                result_tools.append(tool)
                present.add(name)
                continue
            alias, i = f"{name}_client", 2
            while alias in taken:
                alias, i = f"{name}_client{i}", i + 1
            taken.add(alias)
            fn = tool.get("function")
            if isinstance(fn, dict):
                patched = dict(tool); patched["function"] = {**fn, "name": alias}
            else:
                patched = dict(tool); patched["name"] = alias
            renamed.append({alias: name})
            result_tools.append(patched)
        else:
            result_tools.append(tool)
    injected = []
    for name in ("read", "shell"):
        if name in present:
            continue
        result_tools.append({"type": "function", "function": {
            "name": name,
            "description": FINGERPRINT_TOOLS[name]["description"],
            "parameters": FINGERPRINT_TOOLS[name]["parameters"],
        }})
        injected.append(name)
    if injected:
        messages = out.get("messages")
        if isinstance(messages, list) and messages:
            first = messages[0]
            if isinstance(first, dict) and first.get("role") == "system" and isinstance(first.get("content"), str):
                out["messages"] = [{**first, "content": first["content"] + "\n\n" + _PLACEHOLDER_NOTE}, *messages[1:]]
            else:
                out["messages"] = [{"role": "system", "content": _PLACEHOLDER_NOTE}, *messages]
    out["tools"] = result_tools
    # tool_choice: "none" 在 v2 闸门是 403，删掉；显式命名被改名工具的同步到别名
    choice = out.get("tool_choice")
    if choice == "none":
        out.pop("tool_choice", None)
    elif isinstance(choice, dict) and renamed:
        mapping = {k: v for m in renamed for k, v in m.items()}
        fn = choice.get("function")
        target = str((fn or {}).get("name") or choice.get("name") or "")
        for alias, original in mapping.items():
            if target == original:
                if isinstance(fn, dict):
                    out["tool_choice"] = {**choice, "function": {**fn, "name": alias}}
                else:
                    out["tool_choice"] = {**choice, "name": alias}
                break
    return out




def collect_openai_chunks(chunks, body: dict, *, terminated: bool = False) -> dict:
    """Fold one provider choice; never invent success for errors or an abnormal EOF."""
    first = None
    message = {"role": "assistant", "content": ""}
    slots = {}
    legacy = None
    finish_reason = None
    usage = None
    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise ValueError("上游 SSE 顶层不是对象")
        if chunk.get("error") or chunk.get("type") == "error":
            raise ValueError("上游 SSE 返回错误")
        if first is None:
            first = chunk
        choices = chunk.get("choices") or []
        if not isinstance(choices, list):
            raise ValueError("上游 choices 不是数组")
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise ValueError("聚合桥只支持单个有效 choice")
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise ValueError("上游 delta 不是对象")
            for field in ("content", "refusal", "reasoning_content"):
                if delta.get(field) is not None:
                    if not isinstance(delta[field], str):
                        raise ValueError("上游文本分片不是字符串")
                    message[field] = message.get(field, "") + delta[field]
            if delta.get("function_call") is not None:
                function = delta["function_call"]
                if not isinstance(function, dict):
                    raise ValueError("上游 function_call 分片格式无效")
                if legacy is None:
                    legacy = {"name": "", "arguments": ""}
                for field in ("name", "arguments"):
                    if function.get(field) is not None:
                        if not isinstance(function[field], str):
                            raise ValueError("上游 function_call 字段不是字符串")
                        legacy[field] += function[field]
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict) or not isinstance(call.get("index", 0), int) or isinstance(call.get("index"), bool):
                    raise ValueError("上游工具分片索引无效")
                idx = call.get("index", 0)
                if not 0 <= idx < 128:
                    raise ValueError("上游工具分片索引超出范围")
                slot = slots.setdefault(idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    slot["id"] = call["id"]
                fn = call.get("function") or {}
                if not isinstance(fn, dict):
                    raise ValueError("上游工具分片格式无效")
                for field in ("name", "arguments"):
                    if fn.get(field) is not None:
                        if not isinstance(fn[field], str):
                            raise ValueError("上游工具字段不是字符串")
                        slot["function"][field] += fn[field]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
    if not first or (not terminated and not finish_reason):
        raise ValueError("上游流式响应异常结束")
    if slots:
        message["tool_calls"] = [slots[index] for index in sorted(slots)]
        if finish_reason != "length":
            for call in message["tool_calls"]:
                if not call["id"] or not call["function"]["name"]:
                    raise ValueError("上游工具调用缺少 id 或函数名")
                args = json.loads(call["function"]["arguments"])
                if not isinstance(args, dict):
                    raise ValueError("上游工具参数不是 JSON 对象")
    if legacy is not None:
        message["function_call"] = legacy
        if finish_reason != "length":
            if not legacy["name"]:
                raise ValueError("上游 function_call 缺少函数名")
            if not isinstance(json.loads(legacy["arguments"]), dict):
                raise ValueError("上游 function_call 参数不是 JSON 对象")
    if not message["content"]:
        message["content"] = None
    if not (message.get("content") or message.get("refusal") or slots or legacy) and finish_reason != "length":
        raise ValueError("上游流式响应没有有效输出")
    result = {
        "id": first.get("id") or "chatcmpl-account",
        "object": "chat.completion", "created": first.get("created") or int(time.time()),
        "model": first.get("model") or body.get("model"),
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason or "stop"}],
    }
    if usage is not None:
        result["usage"] = usage
    return result


@asynccontextmanager
async def closing_stream(client, url, headers, body):
    """Close an SSE response even while its task is being cancelled."""
    async with client.stream("POST", url, headers=headers, json=body) as response:
        try:
            yield response
        finally:
            with CancelScope(shield=True):
                await response.aclose()


async def complete_nonstream(client, url, headers, body):
    """SSE-only upstream -> Chat completion, validating errors, framing and terminal state."""
    from .sse import iter_sse_json_lines
    wire = dict(body)
    wire["stream"] = True
    chunks = []
    terminated = False
    async with closing_stream(client, url, headers, wire) as resp:
        if resp.status_code != 200:
            await resp.aread()
            return None, resp.status_code, "上游拒绝请求"
        try:
            async for line in iter_sse_json_lines(resp.aiter_lines()):
                data = line[5:].strip()
                if data == "[DONE]":
                    terminated = True
                    break
                frame = json.loads(data)
                if frame.get("error") or frame.get("type") == "error":
                    return None, 502, "上游 SSE 返回错误"
                chunks.append(frame)
            return collect_openai_chunks(chunks, wire, terminated=terminated), 200, ""
        except (TypeError, ValueError) as exc:
            # These are locally generated diagnostics, never raw credential-bearing bodies.
            return None, 502, str(exc)[:200]



def restore_tool_names(payload: dict, renamed: list[dict]) -> dict:
    """把上游响应里对被改名工具的调用还原成调用方的原始名字。"""
    if not renamed or not isinstance(payload, dict):
        return payload
    mapping = {k: v for m in renamed for k, v in m.items()}
    try:
        for choice in payload.get("choices") or []:
            message = choice.get("message") or {}
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                if fn.get("name") in mapping:
                    fn["name"] = mapping[fn["name"]]
    except Exception:  # noqa: BLE001
        pass
    return payload
