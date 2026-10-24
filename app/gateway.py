"""API Gateway 核心：Bearer Key 鉴权 → 渠道路由（优先级 + 权重 + 失败自动切换）→ 上游转发（OpenAI 兼容 / 本地 mock）→ 用量记录 → 请求日志。"""
import asyncio
import hashlib
import logging
import json
import random
import re
import secrets
import time
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import AsyncIterator
from urllib.parse import quote, urlparse, urlunparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .database import execute, iso, query, query_one, utcnow
from . import settings_store
from .protocols import (
    PROTOCOLS,
    channel_protocol,
    gemini_request_to_openai,
    gemini_to_openai,
    normalize_gemini_base_url,
    normalize_openai_responses_base_url,
    anthropic_to_openai_request,
    openai_to_responses_response,
    openai_to_gemini,
    openai_to_anthropic_response,
    openai_to_gemini_response,
    openai_to_responses,
    responses_to_chat,
    responses_to_openai,
)

router = APIRouter(prefix="/v1")
external_router = APIRouter()
logger = logging.getLogger("obsessionapi.gateway")

MODEL_FALLBACK = {"gpt-4o": (2.5, 10.0), "gpt-4o-mini": (0.15, 0.6)}

# ---- 渠道熔断：连续失败 N 次进入冷却，冷却期内路由自动跳过（进程内状态，重启复位）
FAIL_THRESHOLD = 3
COOLDOWN_SECONDS = 300
_BREAKER: dict[int, dict] = {}
# ---- 失败自动切换：这些上游状态码视为渠道问题，换下一个渠道重试
FAILOVER_STATUSES = {401, 403, 408, 429}
MAX_CHANNEL_ATTEMPTS = 3

ERROR_CATEGORIES = {
    "auth", "rate_limit", "timeout", "upstream_5xx", "model_not_found",
    "content_filter", "invalid_request", "network", "protocol_error", "tool_not_supported",
    "incomplete_output",
}


def classify_upstream_error(status_code: int | None, body_text: str = "", exc: Exception | None = None) -> str:
    """把上游 HTTP、响应文本和网络异常收口为可供路由使用的分类。"""
    if exc is not None:
        if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
            return "timeout"
        return "network"
    code = int(status_code or 0)
    text = str(body_text or "").lower()
    if code in {401, 403} or any(mark in text for mark in ("unauthorized", "invalid api key", "authentication")):
        return "auth"
    if code == 429 or any(mark in text for mark in ("rate limit", "too many requests", "限流", "频率")):
        return "rate_limit"
    if code in {408, 504} or any(mark in text for mark in ("timeout", "timed out", "超时")):
        return "timeout"
    if any(mark in text for mark in ("content filter", "safety filter", "内容过滤", "safety system")):
        return "content_filter"
    if code in {404, 405} and any(mark in text for mark in ("model", "模型")):
        return "model_not_found"
    if code == 404:
        return "model_not_found"
    if code in {400, 422} or any(mark in text for mark in ("invalid request", "bad request", "参数错误", "请求无效")):
        return "invalid_request"
    if code >= 500:
        return "upstream_5xx"
    return "network" if code == 0 else "invalid_request"


def _settings():
    return settings_store.get_all()


def _http_timeout(stream: bool = False) -> httpx.Timeout:
    values = _settings()
    read_timeout = int(values["read_timeout_s"])
    if stream:
        read_timeout = max(read_timeout, 300)
    return httpx.Timeout(read_timeout, connect=int(values["connect_timeout_s"]))


def _retry_allowed(status_code: int | None, detail: str = "", exc: Exception | None = None) -> bool:
    category = classify_upstream_error(status_code, detail, exc)
    return category not in {"invalid_request", "content_filter", "model_not_found"}


async def _retry_pause(attempt_index: int) -> None:
    """指数退避 + 抖动：delay = base * 2^attempt，抖动 ±jitter%，受 retry_backoff_max_ms 封顶。"""
    values = _settings()
    base = int(values["retry_backoff_ms"])
    if base <= 0:
        return
    delay_ms = base * (2 ** max(0, attempt_index))
    jitter = int(values.get("retry_backoff_jitter_pct", 0) or 0)
    if jitter:
        delay_ms = delay_ms * (100 + random.uniform(-jitter, jitter)) / 100
    cap = int(values.get("retry_backoff_max_ms", 0) or 0)
    if cap > 0:
        delay_ms = min(delay_ms, cap)
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000)

_SEEKAI_TAIL = re.compile(
    r"(?:^|\n)[ \t]*(?:BY[ \t]+(?:https://seekai\.cc/?|\[[^\]\r\n]+\]\(https://seekai\.cc/?\))|\[BY\]\(https://seekai\.cc/?\))[ \t]*[.!]?[\s]*\Z",
    re.IGNORECASE,
)


def strip_seekai_attribution(text: str) -> str:
    """只移除最终独立行上的 SeekAI 尾注，保留正文 URL 与代码块内容。"""
    if not isinstance(text, str):
        return text
    match = _SEEKAI_TAIL.search(text)
    if not match:
        return text
    prefix = text[:match.start()]
    if prefix.count("```") % 2 or prefix.count("~~~") % 2:
        return text
    return prefix.rstrip()


def upstream_error_message(
    channel_name: str, status_code: int, detail: str = "", secret: str = "", content_type: str = ""
) -> str:
    raw_detail = str(detail or "")
    is_html = "text/html" in str(content_type or "").lower() or re.search(
        r"<!doctype\s+html|<html\b", raw_detail, re.IGNORECASE
    )
    safe_detail = raw_detail
    error_type = ""
    if not is_html:
        try:
            parsed = json.loads(raw_detail)
            if isinstance(parsed, dict):
                error = parsed.get("error")
                if isinstance(error, dict) and error.get("message"):
                    safe_detail = str(error["message"])
                    error_type = str(error.get("type") or "")
                elif parsed.get("message"):
                    safe_detail = str(parsed["message"])
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    detail = " ".join(safe_detail.split())[:200]
    if secret:
        detail = detail.replace(secret, "[redacted]")
    detail = re.sub(r"([?&](?:api[_-]?key|key|token|auth_token)=)[^&\s]+", r"\1[redacted]", detail, flags=re.IGNORECASE)
    detail = re.sub(r"\bBearer\s+\S+", "Bearer [redacted]", detail, flags=re.IGNORECASE)
    lower_detail = detail.lower()
    restricted = error_type == "unauthorized_client_error" or "unauthorized client detected" in lower_detail
    if restricted:
        return (
            f"渠道 {channel_name}：该上游拒绝通用 API 客户端。API Key 可能有效，但当前请求客户端不在服务商允许名单中。"
            "请联系上游开通通用 API/客户端白名单，或使用其面向通用 API 的地址和对应 Key。"
        )
    if is_html:
        detail = ""
    if status_code == 401:
        reason = "上游认证失败（401），请检查渠道 API Key"
    elif status_code == 403 and "cloudflare" in lower_detail:
        reason = ("上游 Cloudflare/403 HTML 错误页，需检查 API 地址、Key 或供应商访问限制"
                  if is_html else "上游 Cloudflare/403 访问限制，需检查 API 地址、Key 或供应商访问限制")
    elif status_code == 403:
        reason = "上游访问失败（403），需检查渠道 API 地址、Key 或供应商访问限制"
    elif is_html:
        reason = f"上游返回 {status_code} HTML 错误页，可能是供应商源站或 Cloudflare 故障"
        detail = ""
    else:
        reason = f"上游返回 {status_code}"
    return f"渠道 {channel_name}：{reason}" + (f"：{detail}" if detail else "")


def _protocol_error_message(channel_name: str, detail: str = "", secret: str = "", content_type: str = "") -> str:
    diagnostic = upstream_error_message(channel_name, 502, detail, secret, content_type)
    _, separator, suffix = diagnostic.partition("：")
    message = f"渠道 {channel_name}：上游协议错误（HTTP 200 响应不符合 Chat Completions 格式）"
    return message + (f"：{suffix}" if separator and suffix else "")


def _stream_content(event: dict):
    choices = event.get("choices") or []
    delta = choices[0].get("delta") if choices and isinstance(choices[0], dict) else None
    content = delta.get("content") if isinstance(delta, dict) else None
    return content if isinstance(content, str) else None


def _stream_has_output(event: dict) -> bool:
    choices = event.get("choices") or [] if isinstance(event, dict) else []
    delta = choices[0].get("delta") if choices and isinstance(choices[0], dict) else None
    if not isinstance(delta, dict):
        return False
    content = delta.get("content")
    if isinstance(content, str) and content:
        return True
    if delta.get("tool_calls"):
        return True
    if delta.get("function_call"):
        return True
    refusal = delta.get("refusal")
    return isinstance(refusal, str) and bool(refusal)


def _anthropic_stream_has_output(event: dict) -> bool:
    if not isinstance(event, dict):
        return False
    if event.get("type") == "content_block_start":
        block = event.get("content_block")
        return isinstance(block, dict) and block.get("type") == "tool_use"
    if event.get("type") != "content_block_delta":
        return False
    delta = event.get("delta")
    if not isinstance(delta, dict):
        return False
    return any(isinstance(delta.get(key), str) and delta.get(key)
               for key in ("text", "partial_json", "refusal"))


def _non_empty_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_content_blocks(content) -> bool:
    if _non_empty_text(content):
        return True
    if not isinstance(content, list):
        return False
    for block in content:
        if _non_empty_text(block):
            return True
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"text", "output_text", "refusal"} and _non_empty_text(block.get("text")):
            return True
    return False


def _valid_tool_call(call) -> bool:
    if not isinstance(call, dict):
        return False
    function = call.get("function")
    if not isinstance(function, dict):
        return False
    return _non_empty_text(function.get("name")) or _non_empty_text(function.get("arguments")) or (
        isinstance(function.get("arguments"), dict) and bool(function["arguments"])
    )


def _valid_legacy_function_call(call) -> bool:
    return isinstance(call, dict) and (
        _non_empty_text(call.get("name")) or _non_empty_text(call.get("arguments")) or (
            isinstance(call.get("arguments"), dict) and bool(call["arguments"])
        )
    )


def _chat_message_has_output(message: dict) -> bool:
    if not isinstance(message, dict):
        return False
    if _valid_content_blocks(message.get("content")):
        return True
    if isinstance(message.get("tool_calls"), list) and any(_valid_tool_call(call) for call in message["tool_calls"]):
        return True
    if _valid_legacy_function_call(message.get("function_call")):
        return True
    refusal = message.get("refusal")
    return _non_empty_text(refusal)


def _reasoning_value_non_empty(value) -> bool:
    if _non_empty_text(value):
        return True
    if isinstance(value, list):
        return any(_reasoning_value_non_empty(item) for item in value)
    if isinstance(value, dict):
        return any(
            _reasoning_value_non_empty(item)
            for key, item in value.items()
            if key not in {"type", "id", "signature"}
        )
    return False


def _message_has_reasoning(message: dict) -> bool:
    return isinstance(message, dict) and any(
        _reasoning_value_non_empty(message.get(key))
        for key in ("reasoning", "reasoning_content", "reasoning_details", "thinking")
    )


def _chat_response_state(result: dict) -> str:
    """区分可展示结果、预算耗尽和真正的协议/上游结构错误。"""
    if not isinstance(result, dict) or isinstance(result.get("error"), dict):
        return "invalid"
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "invalid"
    choice = choices[0]
    message = choice.get("message")
    if _chat_message_has_output(message):
        return "valid"
    if choice.get("finish_reason") in {"length", "max_tokens"}:
        return "incomplete"
    return "invalid"


def _valid_chat_response(result: dict) -> bool:
    return _chat_response_state(result) == "valid"


def _incomplete_response_detail(result: dict) -> str:
    usage = result.get("usage") if isinstance(result, dict) and isinstance(result.get("usage"), dict) else {}
    output_tokens = usage.get("completion_tokens")
    suffix = f"；completion_tokens={output_tokens}" if output_tokens is not None else ""
    choices = result.get("choices") if isinstance(result, dict) else []
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    if _message_has_reasoning(message):
        return f"请求已返回，但输出预算耗尽（finish_reason=length），仅收到非空 reasoning，未取得最终回答{suffix}"
    return f"请求已返回，但输出预算耗尽（finish_reason=length），未取得最终回答{suffix}"


def _request_diagnostic(client_body: dict, upstream_body: dict, endpoint: str = "",
                        stream: bool | None = None, protocol: str = "") -> str:
    def value(body, key):
        raw = body.get(key) if isinstance(body, dict) and key in body else None
        return str(raw) if raw is not None and not isinstance(raw, (dict, list)) else "unknown"

    def present(body, key):
        raw = body.get(key) if isinstance(body, dict) else None
        return "present" if _reasoning_value_non_empty(raw) else "absent"

    return (
        f"request_path={endpoint or 'unknown'};stream={str(stream).lower() if stream is not None else 'unknown'};protocol={protocol or 'unknown'};"
        f"request_max_tokens={value(client_body, 'max_tokens')};"
        f"request_max_completion_tokens={value(client_body, 'max_completion_tokens')};"
        f"upstream_max_tokens={value(upstream_body, 'max_tokens')};"
        f"upstream_max_completion_tokens={value(upstream_body, 'max_completion_tokens')};"
        f"thinking={present(client_body, 'thinking')};reasoning_effort={present(client_body, 'reasoning_effort')}"
    )


def _response_structure_diagnostic(result, content_type: str = "", target_protocol: str = "") -> str:
    """只记录响应结构状态，不记录正文、reasoning 正文或凭证。"""
    if not isinstance(result, dict):
        return f"upstream_content_type={content_type or 'unknown'};response_top_level=non_object"
    choices = result.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    message = (choice.get("message") or choice.get("delta")) if choice else None

    def state(body, key):
        if not isinstance(body, dict) or key not in body:
            return "unknown"
        value = body.get(key)
        if value is None:
            return "empty"
        if isinstance(value, str):
            return "nonempty" if value.strip() else "empty"
        if isinstance(value, (list, dict)):
            return "nonempty" if value else "empty"
        return "present"

    finish = choice.get("finish_reason") if choice else None
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    mapped_stop = {
        "stop": "end_turn",
        "length": "max_tokens",
        "max_tokens": "max_tokens",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "stop_sequence": "stop_sequence",
    }.get(finish, "unknown") if target_protocol == "anthropic" else "not_applicable"
    choice_fields = ",".join(sorted(choice.keys())) if choice else "unknown"
    message_fields = ",".join(sorted(message.keys())) if isinstance(message, dict) else "unknown"
    reasoning_state = "absent"
    if isinstance(message, dict) and any(key in message for key in ("reasoning", "reasoning_content", "reasoning_details", "thinking")):
        reasoning_state = "nonempty" if _message_has_reasoning(message) else "empty"
    return (
        f"upstream_content_type={content_type or 'unknown'};choices_fields={choice_fields};"
        f"message_fields={message_fields};content={state(message, 'content')};reasoning={reasoning_state};"
        f"finish_reason={finish if finish is not None else 'unknown'};"
        f"prompt_tokens={usage.get('prompt_tokens', 'unknown')};completion_tokens={usage.get('completion_tokens', 'unknown')};"
        f"converted_stop_reason={mapped_stop}"
    )


def _request_error_detail(exc: Exception) -> str:
    if isinstance(exc, httpx.ConnectTimeout):
        return "连接上游超时"
    if isinstance(exc, httpx.ReadTimeout):
        return "等待上游响应超时"
    if isinstance(exc, httpx.ConnectError):
        return "无法连接上游"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "上游 HTTP 协议异常"
    if isinstance(exc, httpx.RequestError):
        return "上游网络请求异常"
    return "上游流式响应异常"


def _invalid_response_detail(result) -> str:
    if not isinstance(result, dict):
        return "JSON 顶层不是对象"
    if isinstance(result.get("error"), dict):
        return "上游返回 error 错误对象"
    if not isinstance(result.get("choices"), list) or not result.get("choices"):
        return "choices 缺失或为空"
    choice = result["choices"][0] if isinstance(result["choices"][0], dict) else {}
    message = choice.get("message")
    if _chat_response_state(result) == "incomplete":
        return _incomplete_response_detail(result)
    if not isinstance(message, dict):
        return "choice 缺少合法 message 对象"
    if _message_has_reasoning(message):
        finish = choice.get("finish_reason")
        prefix = f"上游 finish_reason={finish}，" if finish else ""
        return f"{prefix}message 仅包含非空 reasoning，未提供最终文本、工具调用、function_call 或 refusal"
    return "message 没有非空 content、tool_calls、function_call 或 refusal"


async def _fallback_completion(channel, url: str, body: dict, headers: dict):
    fallback_body = {key: value for key, value in body.items() if key != "stream"}
    try:
        async with _http_client(channel) as fallback_client:
            response = await fallback_client.post(url, json=fallback_body, headers=headers)
    except httpx.RequestError as exc:
        return None, 502, upstream_error_message(
            channel["name"], 502, _request_error_detail(exc), channel["api_key"]
        )
    content_type = response.headers.get("content-type", "")
    if response.status_code != 200:
        return None, response.status_code, upstream_error_message(
            channel["name"], response.status_code, response.text[:300], channel["api_key"], content_type
        )
    try:
        result = response.json()
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, 502, _protocol_error_message(
            channel["name"], response.text[:300], channel["api_key"], content_type
        )
    response_state = _chat_response_state(result)
    if response_state == "incomplete":
        return result, 200, _incomplete_response_detail(result)
    if response_state != "valid":
        return None, 502, _protocol_error_message(
            channel["name"], _invalid_response_detail(result), channel["api_key"], content_type
        )
    return result, 200, ""


async def _fallback_anthropic_completion(channel, url: str, body: dict, headers: dict, requested_model: str):
    fallback_body = {key: value for key, value in body.items() if key != "stream"}
    try:
        async with _http_client(channel) as fallback_client:
            response = await fallback_client.post(url, json=_openai_to_anthropic(fallback_body), headers=headers)
    except (ValueError, httpx.RequestError) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else _request_error_detail(exc)
        return None, 502, _anthropic_error_message(channel, url, 502, detail)
    content_type = response.headers.get("content-type", "")
    if response.status_code != 200:
        return None, response.status_code, _anthropic_error_message(
            channel, url, response.status_code, response.text[:300], content_type
        )
    try:
        result = _anthropic_to_openai(response.json(), requested_model)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, 502, _anthropic_error_message(
            channel, url, 502, str(exc)[:300], content_type
        )
    return result, 200, ""


class _SeekAITailFilter:
    """流式只暂存可能是尾注的独立行，安全正文仍即时转发。"""

    def __init__(self):
        self.pending = None
        self.pending_complete = False
        self.at_line_start = True
        self.fence_count = 0

    @staticmethod
    def _is_tail(line: str) -> bool:
        return strip_seekai_attribution("\n" + line) == ""

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self.fence_count += text.count("```") + text.count("~~~")
        output = []
        index = 0
        while index < len(text):
            if self.pending is not None:
                if self.pending_complete:
                    if text[index].isspace():
                        self.pending += text[index]
                        index += 1
                        continue
                    output.append(self.pending)
                    self.pending = None
                    self.pending_complete = False
                    self.at_line_start = True
                    continue
                newline = text.find("\n", index)
                if newline < 0:
                    self.pending += text[index:]
                    break
                self.pending += text[index:newline + 1]
                index = newline + 1
                if self._is_tail(self.pending[:-1]):
                    self.pending_complete = True
                else:
                    output.append(self.pending)
                    self.pending = None
                    self.at_line_start = True
                continue

            char = text[index]
            if self.at_line_start and char in "B[":
                self.pending = char
                self.at_line_start = False
                index += 1
                continue
            output.append(char)
            self.at_line_start = char == "\n"
            index += 1
        return "".join(output)

    def finish(self) -> str:
        if self.pending is None:
            return ""
        if self.fence_count % 2 == 0 and self._is_tail(self.pending):
            return ""
        return self.pending


def normalize_openai_base_url(value: str) -> str:
    """接受域名、/v1 或完整 Chat Completions 地址，统一为 OpenAI 兼容 Base URL。"""
    base_url = str(value or "").strip().rstrip("/")
    if not base_url:
        return ""
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
    if base_url.endswith("/chat/completions"):
        base_url = base_url.removesuffix("/chat/completions")
    elif base_url.endswith("/models"):
        base_url = base_url.removesuffix("/models")
    if urlparse(base_url).path in {"", "/"}:
        base_url += "/v1"
    return base_url


def normalize_anthropic_base_url(value: str) -> str:
    """接受 Anthropic 根地址、/v1 或 /v1/messages，统一保存为根地址。"""
    base_url = str(value or "").strip().rstrip("/")
    if not base_url:
        return ""
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Anthropic Base URL 必须以 http:// 或 https:// 开头")
    if parsed.query or parsed.fragment:
        raise ValueError("Anthropic Base URL 不得包含查询参数或片段")
    path = parsed.path.rstrip("/")
    if path.endswith("/v1/messages"):
        path = path[:-len("/v1/messages")]
    elif path.endswith("/v1"):
        path = path[:-len("/v1")]
    return urlunparse(parsed._replace(path=path, params="", query="", fragment="")).rstrip("/")


def anthropic_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }


_BLOCKED_CHANNEL_HEADERS = {
    "authorization", "x-api-key", "x-goog-api-key", "host", "content-length",
    "connection", "transfer-encoding", "cookie", "set-cookie", "proxy-authorization",
}
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$%&'*+.^_`|~-]{0,127}$")
_HEADER_VALUE_LIMIT = 2048


def _header_config_value(channel, key: str, default=""):
    try:
        return channel[key]
    except (KeyError, IndexError, TypeError):
        return default


def _header_value(value, field: str = "请求头值") -> str:
    value = str(value or "")
    if len(value) > _HEADER_VALUE_LIMIT:
        raise ValueError(f"{field}过长，最多 {_HEADER_VALUE_LIMIT} 个字符")
    if "\r" in value or "\n" in value:
        raise ValueError(f"{field}不能包含 CR/LF")
    return value


def normalize_extra_headers(value) -> dict[str, str]:
    if value in (None, "", {}):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("extra_headers 必须是 JSON 对象") from exc
    if not isinstance(value, dict):
        raise ValueError("extra_headers 必须是 JSON 对象")
    result = {}
    for raw_name, raw_value in value.items():
        name = str(raw_name or "").strip().lower()
        if not _HEADER_NAME_RE.fullmatch(name):
            raise ValueError(f"自定义请求头名称无效：{raw_name}")
        if name in _BLOCKED_CHANNEL_HEADERS:
            raise ValueError(f"禁止覆盖敏感请求头：{name}")
        result[name] = _header_value(raw_value)
    return result


def normalize_forward_headers(value) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            value = parsed if isinstance(parsed, list) else value.replace("\r", "\n").replace(",", "\n").split("\n")
        except (TypeError, ValueError, json.JSONDecodeError):
            value = value.replace("\r", "\n").replace(",", "\n").split("\n")
    if not isinstance(value, (list, tuple)):
        raise ValueError("forward_headers 必须是请求头名称列表")
    result = []
    for raw_name in value:
        name = str(raw_name or "").strip().lower()
        if not name:
            continue
        if name.endswith("*"):
            base_name = name[:-1]
            if not base_name or "*" in base_name:
                raise ValueError(f"透传请求头名称无效：{raw_name}")
        elif "*" in name:
            raise ValueError(f"透传请求头名称无效：{raw_name}")
        else:
            base_name = name
        if not _HEADER_NAME_RE.fullmatch(base_name):
            raise ValueError(f"透传请求头名称无效：{raw_name}")
        if base_name in _BLOCKED_CHANNEL_HEADERS:
            raise ValueError(f"禁止透传敏感请求头：{name}")
        if name not in result:
            result.append(name)
    return result[:32]


def normalize_user_agent(value) -> str:
    return _header_value(value, "User-Agent")[:512]


def channel_header_config(channel) -> tuple[dict[str, str], list[str], str]:
    return (
        normalize_extra_headers(_header_config_value(channel, "extra_headers", "{}")),
        normalize_forward_headers(_header_config_value(channel, "forward_headers", "[]")),
        normalize_user_agent(_header_config_value(channel, "user_agent", "")),
    )


def build_channel_headers(channel, base_headers: dict[str, str], request: Request | None = None,
                          protocol: str = "") -> dict[str, str]:
    """合并渠道配置和安全透传头；认证头始终由网关掌控。"""
    headers = dict(base_headers or {})
    protected = {str(name).lower() for name in headers}
    extra, forward, user_agent = channel_header_config(channel)
    if request is not None:
        for pattern in forward:
            for incoming_name, incoming_value in request.headers.items():
                name = incoming_name.lower()
                if name in _BLOCKED_CHANNEL_HEADERS or name in protected:
                    continue
                if fnmatchcase(name, pattern):
                    try:
                        headers[incoming_name] = _header_value(incoming_value, "透传请求头值")
                    except ValueError:
                        continue
        if protocol == "anthropic":
            version = str(request.headers.get("anthropic-version") or "").strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", version):
                headers["anthropic-version"] = version
    for name, value in extra.items():
        if name in _BLOCKED_CHANNEL_HEADERS or name in protected:
            continue
        for existing in list(headers):
            if existing.lower() == name:
                del headers[existing]
        headers[name] = value
    if user_agent:
        for existing in list(headers):
            if existing.lower() == "user-agent":
                del headers[existing]
        headers["User-Agent"] = user_agent
    logger.info("上游请求渠道=%s 协议=%s headers=%s", _header_config_value(channel, "name", ""), protocol,
                sorted({str(name).lower() for name in headers}))
    return headers


def header_names(headers: dict[str, str]) -> list[str]:
    return sorted({str(name).lower() for name in (headers or {})})


ALLOWED_CHANNEL_TYPES = {"openai", "openai_chat", "anthropic", "openai_responses", "gemini", "mock", "auto"}


def validate_channel_type(value: str) -> str:
    channel_type = str(value or "").strip().lower()
    if channel_type not in ALLOWED_CHANNEL_TYPES:
        raise ValueError("渠道类型只能是 auto、openai_chat、anthropic、openai_responses、gemini 或 mock")
    return channel_type


def _anthropic_text_blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    blocks = []
    for part in content:
        if isinstance(part, str):
            blocks.append({"type": "text", "text": part})
        elif isinstance(part, dict) and part.get("type") in {"text", "input_text"}:
            text = part.get("text")
            if isinstance(text, str):
                blocks.append({"type": "text", "text": text})
    return blocks


def _anthropic_tool_use(call: dict, index: int) -> dict:
    function = call.get("function") if isinstance(call.get("function"), dict) else call
    name = str(function.get("name") or "").strip()
    if not name:
        raise ValueError("Anthropic 工具调用缺少函数名")
    raw_arguments = function.get("arguments", "{}")
    if isinstance(raw_arguments, str):
        try:
            arguments = json.loads(raw_arguments or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("工具 arguments 不是合法 JSON") from exc
    else:
        arguments = raw_arguments
    if not isinstance(arguments, dict):
        raise ValueError("工具 arguments 必须是 JSON 对象")
    return {
        "type": "tool_use",
        "id": str(call.get("id") or f"call_{index}"),
        "name": name,
        "input": arguments,
    }


def _openai_message_to_anthropic(message: dict, index: int) -> dict:
    role = message.get("role")
    if role not in {"user", "assistant"}:
        raise ValueError(f"不支持的消息角色：{role}")
    blocks = _anthropic_text_blocks(message.get("content"))
    if role == "assistant":
        for call_index, call in enumerate(message.get("tool_calls") or []):
            if isinstance(call, dict):
                blocks.append(_anthropic_tool_use(call, call_index))
        legacy = message.get("function_call")
        if isinstance(legacy, dict):
            blocks.append(_anthropic_tool_use({"function": legacy, "id": f"call_legacy_{index}"}, index))
    if not blocks:
        blocks = [{"type": "text", "text": ""}]
    return {"role": role, "content": blocks}


def _openai_to_anthropic(body: dict) -> dict:
    """将已按渠道映射的 OpenAI body 转换为 Anthropic Messages body。"""
    max_tokens = body.get("max_tokens", body.get("max_completion_tokens", 1024))
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        max_tokens = 1024
    result = {
        "model": body.get("model", ""),
        "messages": [],
        "max_tokens": max_tokens,
        "stream": bool(body.get("stream")),
    }
    system_blocks = []
    for index, message in enumerate(body.get("messages") or []):
        if not isinstance(message, dict):
            raise ValueError("messages 中包含无效消息")
        role = message.get("role")
        if role in {"system", "developer"}:
            system_blocks.extend(_anthropic_text_blocks(message.get("content")))
        elif role == "tool":
            tool_id = str(message.get("tool_call_id") or "").strip()
            if not tool_id:
                raise ValueError("tool 消息缺少 tool_call_id")
            content = _anthropic_text_blocks(message.get("content")) or [{"type": "text", "text": ""}]
            result["messages"].append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content}],
            })
        elif role in {"user", "assistant"}:
            result["messages"].append(_openai_message_to_anthropic(message, index))
        else:
            raise ValueError(f"不支持的消息角色：{role}")
    if not result["messages"]:
        result["messages"] = [{"role": "user", "content": [{"type": "text", "text": ""}]}]
    if system_blocks:
        result["system"] = system_blocks
    for key in ("temperature", "top_p"):
        if key in body and body[key] is not None:
            result[key] = body[key]
    if body.get("stop") is not None:
        result["stop_sequences"] = body["stop"] if isinstance(body["stop"], list) else [body["stop"]]
    if body.get("thinking") is not None:
        if not isinstance(body["thinking"], dict):
            raise ValueError("thinking 必须是对象")
        result["thinking"] = body["thinking"]
    tools = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        tools.append({
            "name": name,
            "description": str(function.get("description") or ""),
            "input_schema": function.get("parameters") if isinstance(function.get("parameters"), dict) else {"type": "object"},
        })
    if tools:
        result["tools"] = tools
        choice = body.get("tool_choice")
        if choice == "auto":
            result["tool_choice"] = {"type": "auto"}
        elif choice == "required":
            result["tool_choice"] = {"type": "any"}
        elif isinstance(choice, dict) and choice.get("type") == "function":
            function = choice.get("function") if isinstance(choice.get("function"), dict) else {}
            if function.get("name"):
                result["tool_choice"] = {"type": "tool", "name": function["name"]}
    return result


def _anthropic_stop_reason(reason) -> str:
    if reason is None:
        return "stop"
    mapped = {
        "end_turn": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
        "stop_sequence": "stop_sequence",
    }.get(reason)
    if mapped is None:
        raise ValueError(f"Anthropic stop_reason={reason} 无等价 Chat Completions finish_reason")
    return mapped


def _anthropic_to_openai(result: dict, requested_model: str) -> dict:
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        raise ValueError("Anthropic 响应缺少合法 content")
    text_parts = []
    reasoning_parts = []
    tool_calls = []
    refusal = ""
    for block in result["content"]:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            if not isinstance(block.get("text"), str):
                raise ValueError("Anthropic text block 缺少文本")
            text_parts.append(block["text"])
        elif block_type == "thinking":
            if not isinstance(block.get("thinking"), str):
                raise ValueError("Anthropic thinking block 缺少 reasoning 文本")
            reasoning_parts.append(block["thinking"])
        elif block_type == "refusal":
            if not isinstance(block.get("text"), str):
                raise ValueError("Anthropic refusal block 缺少文本")
            refusal += block["text"]
        elif block_type == "tool_use":
            if not isinstance(block.get("id"), str) or not block["id"].strip():
                raise ValueError("Anthropic tool_use 缺少 id")
            if not isinstance(block.get("name"), str) or not block["name"].strip():
                raise ValueError("Anthropic tool_use 缺少 name")
            if not isinstance(block.get("input"), dict):
                raise ValueError("Anthropic tool_use input 必须是 JSON 对象")
            tool_calls.append({
                "id": block["id"],
                "type": "function",
                "function": {
                    "name": block["name"],
                    "arguments": json.dumps(block["input"], ensure_ascii=False, separators=(",", ":")),
                },
            })
        else:
            raise ValueError(f"不支持的 Anthropic content 类型：{block_type}")
    message = {"role": "assistant", "content": "".join(text_parts) if text_parts else None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if refusal:
        message["refusal"] = refusal
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    finish_reason = _anthropic_stop_reason(result.get("stop_reason"))
    if not _chat_message_has_output(message) and finish_reason != "length":
        raise ValueError("Anthropic 响应没有文本、工具调用或拒绝信息")
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    openai_usage = {}
    if usage.get("input_tokens") is not None:
        openai_usage["prompt_tokens"] = usage["input_tokens"]
    if usage.get("output_tokens") is not None:
        openai_usage["completion_tokens"] = usage["output_tokens"]
    if "total_tokens" in usage and usage["total_tokens"] is not None:
        openai_usage["total_tokens"] = usage["total_tokens"]
    elif "prompt_tokens" in openai_usage and "completion_tokens" in openai_usage:
        openai_usage["total_tokens"] = openai_usage["prompt_tokens"] + openai_usage["completion_tokens"]
    return {
        "id": str(result.get("id") or ""),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.get("model") or requested_model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        **({"usage": openai_usage} if openai_usage else {}),
    }


def _anthropic_error_message(channel, url: str, status_code: int, detail: str = "", content_type: str = "") -> str:
    message = upstream_error_message(channel["name"], status_code, detail, channel["api_key"], content_type)
    parsed = urlparse(str(url or ""))
    safe_url = urlunparse(parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[-1], query="", fragment=""))
    return f"{message}（协议 anthropic，地址 {safe_url}）"


def _without_tools(body: dict) -> dict:
    return {key: value for key, value in body.items() if key not in {"tools", "tool_choice", "parallel_tool_calls"}}


def _tools_unsupported(text: str, body: dict) -> bool:
    if "tools" not in body:
        return False
    try:
        parsed = json.loads(str(text or ""))
        error = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error, dict) and error.get("param") == "tools":
            return True
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    lowered = str(text or "").lower()
    return any(phrase in lowered for phrase in (
        "tools is not supported",
        "tool use is not supported",
        "function calling is not supported",
    ))


def upstream_model_for_channel(channel, public_model: str) -> str:
    """按渠道映射公开模型名；旧行或异常 JSON 安全回退到公开名。"""
    try:
        raw_mapping = channel["model_mapping"]
    except (KeyError, TypeError):
        raw_mapping = "{}"
    try:
        mapping = json.loads(raw_mapping) if isinstance(raw_mapping, str) else raw_mapping
    except (TypeError, ValueError, json.JSONDecodeError):
        mapping = {}
    upstream_model = mapping.get(public_model) if isinstance(mapping, dict) else None
    return upstream_model.strip() if isinstance(upstream_model, str) and upstream_model.strip() else public_model


def _prepare_openai_reasoning(channel, body: dict) -> None:
    """只把已确认的 scnet endpoint 支持的 thinking 形态发给 OpenAI 兼容上游。"""
    thinking = body.pop("thinking", None)
    if thinking is None:
        return
    if not isinstance(thinking, dict) or thinking.get("type") not in {"enabled", "disabled"}:
        raise ValueError("thinking 只支持 type=enabled 或 type=disabled")
    unsupported = set(thinking) - {"type"}
    if unsupported:
        raise ValueError("当前 OpenAI 兼容渠道不支持 Anthropic thinking 的附加字段")
    host = (urlparse(str(channel["base_url"] or "")).hostname or "").lower()
    if host != "api.scnet.cn":
        raise ValueError("当前 OpenAI 兼容渠道未声明支持 Anthropic thinking 参数")
    # 依据已配置的供应商 endpoint 做映射，不依据模型名猜测参数语义。
    body["thinking"] = {"type": thinking["type"]}


def _tool_not_supported_message(channel_name: str) -> str:
    return f"渠道 {channel_name}：当前渠道不支持工具调用"


def _auth_key(request: Request):
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        key = auth[7:].strip()
    else:
        key = (request.headers.get("x-api-key") or request.headers.get("x-goog-api-key") or "").strip()
    if not key:
        return None
    return query_one(
        """SELECT k.*, u.quota AS user_quota, u.status AS user_status, u.username, u.role AS user_role
           FROM api_keys k JOIN users u ON u.id = k.user_id WHERE k.key = ?""",
        (key,),
    )


def _breaker_default() -> dict:
    return {"streak": 0, "until": 0.0, "last_failure_count": 0, "cooldown_count": 0,
            "auto_disabled": 0, "disabled_reason": "", "disabled_at": None, "disabled_at_ts": 0.0,
            "probation": 0}


def _parse_iso_ts(value) -> float:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _persist_breaker(channel_id: int, st: dict) -> None:
    """把熔断状态写穿到数据库，重启后冷却/自动禁用不丢失。写库失败不影响转发。"""
    try:
        execute(
            """INSERT INTO channel_states(channel_id,streak,cooldown_until,cooldown_count,
                   auto_disabled,disabled_reason,disabled_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(channel_id) DO UPDATE SET
                   streak=excluded.streak, cooldown_until=excluded.cooldown_until,
                   cooldown_count=excluded.cooldown_count, auto_disabled=excluded.auto_disabled,
                   disabled_reason=excluded.disabled_reason, disabled_at=excluded.disabled_at,
                   updated_at=excluded.updated_at""",
            (channel_id, int(st.get("streak", 0) or 0), float(st.get("until", 0) or 0),
             int(st.get("cooldown_count", 0) or 0), int(st.get("auto_disabled", 0) or 0),
             str(st.get("disabled_reason", "") or ""), st.get("disabled_at"), iso(utcnow())),
        )
    except Exception:  # noqa: BLE001
        logger.warning("channel_state 持久化失败 channel_id=%s", channel_id, exc_info=True)


def load_breaker_state() -> None:
    """启动时把持久化的冷却/自动禁用状态载入进程内热缓存（已过期且未禁用的记录不载入）。"""
    _BREAKER.clear()
    try:
        rows = query("SELECT * FROM channel_states")
    except Exception:  # noqa: BLE001
        return
    now = time.time()
    for row in rows:
        until = float(row["cooldown_until"] or 0)
        auto_disabled = bool(row["auto_disabled"])
        if until <= now and not auto_disabled:
            continue
        _BREAKER[row["channel_id"]] = {
            "streak": int(row["streak"] or 0),
            "until": until,
            "last_failure_count": int(row["streak"] or 0),
            "cooldown_count": int(row["cooldown_count"] or 0),
            "auto_disabled": 1 if auto_disabled else 0,
            "disabled_reason": row["disabled_reason"] or "",
            "disabled_at": row["disabled_at"],
            "disabled_at_ts": _parse_iso_ts(row["disabled_at"]),
            "probation": 0,
        }


# ---- 分级冷却（借鉴 zzcsapi）：不同错误需要不同耐心 ----
# 凭证/配置类（auth、invalid_request、model_not_found、content_filter）：一分钟内绝不可能自愈，
#   直接用配置的 cooldown_seconds（默认 300s），避免拿真流量反复撞墙；
# 限流（rate_limit）：上游说"别急"——优先听 Retry-After，没有就 60s 起；
# 瞬时故障（timeout / upstream_5xx / network 等）：指数退避 5s * 2^(n-1)，封顶 cooldown_seconds，
#   偶发抖动不再被一刀切罚 5 分钟。
_COOLDOWN_TRANSIENT_BASE_S = 5
_COOLDOWN_RATE_LIMIT_S = 60
_COOLDOWN_HARD_CATEGORIES = {"auth", "invalid_request", "model_not_found", "content_filter", "tool_not_supported", "protocol_error"}


def _parse_retry_after_ms(headers) -> int:
    """解析上游 Retry-After（RFC 9110：秒数或 HTTP 日期），返回毫秒；解析不出返回 0。"""
    try:
        raw = headers.get("retry-after") if hasattr(headers, "get") else None
        if not raw:
            return 0
        text = str(raw).strip()
        import re as _re
        if _re.fullmatch(r"\d+(\.\d+)?", text):
            return int(float(text) * 1000)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00")) if "T" in text or "-" in text else None
        if parsed is not None:
            return max(0, int(parsed.timestamp() * 1000 - time.time() * 1000))
    except Exception:  # noqa: BLE001
        pass
    return 0


def _cooldown_seconds_for(streak: int, reason: str, retry_after_ms: int = 0) -> int:
    """按错误分类决定这次冷却多久。Retry-After 优先（仅限流场景），全部钳制在 cooldown_seconds 内。"""
    cap = max(1, int(_settings()["cooldown_seconds"]))
    if reason == "rate_limit":
        if retry_after_ms > 0:
            return max(1, min(cap, retry_after_ms // 1000))
        return max(1, min(cap, _COOLDOWN_RATE_LIMIT_S))
    if reason in _COOLDOWN_HARD_CATEGORIES:
        return cap
    n = max(1, int(streak))
    return max(1, min(cap, _COOLDOWN_TRANSIENT_BASE_S * (2 ** (n - 1))))


def _breaker_mark(channel_id: int, ok: bool, reason: str = "", retry_after_ms: int = 0,
                   real_completion: bool = True) -> None:
    """记录一次渠道成败。连续失败达阈值进入分级冷却；反复冷却达 auto_disable_after 自动禁用。

    real_completion=False 表示这次成功不是一次真实对话（如探测/列表拉取），只做半愈合：
    失败计数减半、清冷却，但保留观察期标志——候选排序仍排在健康渠道之后，
    直到一次真实成功才彻底清零（探测活着 ≠ 对话能用）。
    """
    st = _BREAKER.get(channel_id) or _breaker_default()
    if ok:
        if real_completion or not (st.get("streak") or st.get("cooldown_count") or st.get("probation")):
            st = _breaker_default()  # 真实成功（或本来就没欠账）：彻底清零
        else:
            # 半愈合：解除自动禁用、清冷却给机会，失败计数减半进观察期；欠账还记着，
            # 再失败会更快退避回去；last_error 类证据保留在日志里
            st["until"] = 0.0
            st["auto_disabled"] = 0
            st["disabled_reason"] = ""
            st["streak"] = max(1, int(st.get("streak", 1) or 1) // 2)
            st["probation"] = 1
    else:
        st["probation"] = 0
        st["streak"] = int(st.get("streak", 0) or 0) + 1
        st["last_failure_count"] = st["streak"]
        values = _settings()
        if st["streak"] >= int(values["fail_threshold"]):
            seconds = _cooldown_seconds_for(st["streak"], reason, retry_after_ms)
            st["until"] = time.time() + seconds
            st["last_cooldown_reason"] = reason
            st["last_cooldown_seconds"] = seconds
            st["streak"] = 0
            st["cooldown_count"] = int(st.get("cooldown_count", 0) or 0) + 1
            auto_after = int(values.get("auto_disable_after", 0) or 0)
            if auto_after and st["cooldown_count"] >= auto_after:
                st["auto_disabled"] = 1
                st["disabled_reason"] = reason or "连续失败达到自动禁用阈值"
                st["disabled_at"] = iso(utcnow())
                st["disabled_at_ts"] = time.time()
    _BREAKER[channel_id] = st
    _persist_breaker(channel_id, st)


def _breaker_cooling(channel) -> bool:
    st = _BREAKER.get(channel["id"])
    return bool(st and float(st.get("until", 0) or 0) > time.time())


def _breaker_unavailable(channel) -> bool:
    """冷却中或已被自动禁用，都不应再作为路由候选。"""
    st = _BREAKER.get(channel["id"])
    if not st:
        return False
    return bool(float(st.get("until", 0) or 0) > time.time() or st.get("auto_disabled"))


def is_channel_cooling(channel_id: int) -> bool:
    """供管理后台展示渠道熔断（临时冷却）状态。"""
    st = _BREAKER.get(channel_id)
    return bool(st and float(st.get("until", 0) or 0) > time.time())


def is_channel_auto_disabled(channel_id: int) -> bool:
    """渠道是否因反复失败被自动禁用（等待自动恢复或人工恢复）。"""
    st = _BREAKER.get(channel_id)
    return bool(st and st.get("auto_disabled"))


def channel_breaker_state(channel_id: int) -> dict:
    """返回不含凭据的熔断状态，供控制台诊断使用。"""
    st = _BREAKER.get(channel_id) or {}
    until = float(st.get("until") or 0)
    cooling = until > time.time()
    auto_disabled = bool(st.get("auto_disabled"))
    values = _settings()
    recover_in = 0
    if auto_disabled:
        seconds = int(values.get("auto_recover_seconds", 0) or 0)
        disabled_ts = float(st.get("disabled_at_ts") or _parse_iso_ts(st.get("disabled_at")))
        if seconds and disabled_ts:
            recover_in = max(0, int(disabled_ts + seconds - time.time()) + 1)
    return {
        "cooling": cooling,
        "auto_disabled": auto_disabled,
        "probation": bool(st.get("probation") and not cooling and not auto_disabled),
        "disabled_reason": str(st.get("disabled_reason") or ""),
        "disabled_at": st.get("disabled_at"),
        "recover_in_s": recover_in,
        "consecutive_failures": int((st.get("last_failure_count") if (cooling or auto_disabled) else st.get("streak")) or 0),
        "cooldown_count": int(st.get("cooldown_count") or 0),
        "cooldown_remaining_s": max(0, int(until - time.time()) + 1) if cooling else 0,
        "last_cooldown_reason": str(st.get("last_cooldown_reason") or ""),
    }


def recover_channel(channel_id: int) -> None:
    """人工恢复：管理员明确操作，直接满血（清空冷却、禁用与观察期）。"""
    if channel_id in _BREAKER:
        _breaker_mark(channel_id, True, real_completion=True)
        return
    execute("DELETE FROM channel_states WHERE channel_id=?", (channel_id,))


def _auto_recover_once() -> int:
    """把自动禁用已超过 auto_recover_seconds 的渠道做半愈合恢复，返回恢复数量。

    半愈合（借鉴 zzcsapi）：到点只解除禁用并清冷却，失败计数减半进入观察期（probation），
    排序仍在健康渠道之后；要一次真实对话成功才彻底恢复。避免"key 已换/余额已充"的渠道
    被钉死，也避免坏渠道 30 分钟一到就满血回链首再次打脸。
    """
    values = _settings()
    seconds = int(values.get("auto_recover_seconds", 0) or 0)
    if not seconds:
        return 0
    now = time.time()
    recovered = 0
    for channel_id, st in list(_BREAKER.items()):
        if not st.get("auto_disabled"):
            continue
        disabled_ts = float(st.get("disabled_at_ts") or _parse_iso_ts(st.get("disabled_at")))
        if disabled_ts and now - disabled_ts >= seconds:
            _breaker_mark(channel_id, True, real_completion=False)
            recovered += 1
    if recovered:
        logger.info("自动半愈合恢复 %s 个渠道", recovered)
    return recovered


async def auto_recover_loop(interval: int = 30) -> None:
    while True:
        try:
            _auto_recover_once()
        except Exception:  # noqa: BLE001
            logger.warning("自动恢复渠道失败", exc_info=True)
        await asyncio.sleep(max(5, int(interval)))


# ---- 渠道级出网客户端：代理 / 超时覆盖 / 连接池上限
def _channel_proxy(channel) -> str:
    try:
        return str(channel["proxy_url"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return ""


def _http_timeout_for(channel, stream: bool = False) -> httpx.Timeout:
    values = _settings()
    connect = int(values["connect_timeout_s"])
    read = int(values["read_timeout_s"])
    try:
        if channel["connect_timeout_s"]:
            connect = max(1, int(float(channel["connect_timeout_s"])))
        if channel["read_timeout_s"]:
            read = max(1, int(float(channel["read_timeout_s"])))
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    if stream:
        read = max(read, 300)
    return httpx.Timeout(read, connect=connect)


def _http_client(channel=None, stream: bool = False) -> httpx.AsyncClient:
    """按渠道构造 httpx 客户端：支持 HTTP/SOCKS 代理、超时覆盖与连接池上限。"""
    kwargs = {"timeout": _http_timeout_for(channel, stream) if channel is not None else _http_timeout(stream)}
    proxy = _channel_proxy(channel) if channel is not None else ""
    if proxy:
        kwargs["proxy"] = proxy
    limit = 0
    try:
        limit = int(channel["max_concurrency"] or 0) if channel is not None else 0
    except (KeyError, IndexError, TypeError, ValueError):
        limit = 0
    if limit:
        kwargs["limits"] = httpx.Limits(max_connections=limit, max_keepalive_connections=limit)
    return httpx.AsyncClient(**kwargs)


# ---- Key 级权限与限流（进程内滑动窗口 / 并发计数）
_RATE: dict[int, list] = {}
_CONCURRENCY: dict[int, int] = {}
# ---- 渠道级出网上游请求速率（保护上游，避免被上游封禁）
_CHANNEL_RATE: dict[int, list] = {}


def key_allowed_models(key_row) -> set[str]:
    try:
        raw = str(key_row["allowed_models"] or "")
    except (KeyError, IndexError, TypeError):
        return set()
    return {item.strip() for item in re.split(r"[,\s]+", raw) if item.strip()}


def _int_field(row, name: str, default: int = 0) -> int:
    try:
        return int(row[name] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        return default


def _key_rate_limited(key_id: int, limit: int) -> bool:
    """每 Key 每分钟请求数上限（滑动窗口）。limit<=0 表示不限。"""
    if limit <= 0:
        return False
    now = time.time()
    window = _RATE.setdefault(key_id, [])
    cutoff = now - 60
    while window and window[0] < cutoff:
        window.pop(0)
    if len(window) >= limit:
        return True
    window.append(now)
    return False


def _key_concurrency_exceeded(key_id: int, limit: int) -> bool:
    if limit <= 0:
        return False
    return int(_CONCURRENCY.get(key_id, 0)) >= limit


def _key_acquire(key_id: int) -> None:
    _CONCURRENCY[key_id] = int(_CONCURRENCY.get(key_id, 0)) + 1


def _key_release(key_id: int) -> None:
    current = int(_CONCURRENCY.get(key_id, 0)) - 1
    if current <= 0:
        _CONCURRENCY.pop(key_id, None)
    else:
        _CONCURRENCY[key_id] = current


def _reset_runtime_state() -> None:
    """测试与进程重启复用：清空熔断、限流与缓存等进程内状态。"""
    _BREAKER.clear()
    _RATE.clear()
    _CONCURRENCY.clear()
    _CHANNEL_RATE.clear()
    _CACHE.clear()


def _channel_rate_exceeded(channel_id: int, limit: int) -> bool:
    """渠道每分钟上游请求数是否已达上限（只读检查，不消费配额）。limit<=0 表示不限。"""
    if limit <= 0:
        return False
    window = _CHANNEL_RATE.get(channel_id)
    if not window:
        return False
    cutoff = time.time() - 60
    while window and window[0] < cutoff:
        window.pop(0)
    return len(window) >= limit


def _channel_rate_consume(channel_id: int, limit: int) -> None:
    """渠道被选中发起上游请求时占用一个窗口配额。"""
    if limit <= 0:
        return
    now = time.time()
    window = _CHANNEL_RATE.setdefault(channel_id, [])
    cutoff = now - 60
    while window and window[0] < cutoff:
        window.pop(0)
    window.append(now)


# ---- 响应缓存：仅缓存显式 temperature=0 的非流式成功响应
_CACHE: dict[str, dict] = {}


def _cache_key(model: str, body: dict) -> str:
    material = json.dumps({"model": model, "body": body}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _cache_lookup(model: str, body: dict):
    values = _settings()
    if not values.get("cache_enabled") or body.get("stream"):
        return None
    temperature = body.get("temperature")
    if isinstance(temperature, bool) or temperature != 0:
        return None
    entry = _CACHE.get(_cache_key(model, body))
    if not entry:
        return None
    if entry["expires_at"] <= time.time():
        _CACHE.pop(entry["key"], None)
        return None
    return entry


def _cache_store(model: str, body: dict, channel_id, result: dict, pt, ct) -> None:
    values = _settings()
    if not values.get("cache_enabled") or body.get("stream"):
        return
    temperature = body.get("temperature")
    if isinstance(temperature, bool) or temperature != 0:
        return
    ttl = int(values.get("cache_ttl_seconds", 0) or 0)
    if ttl <= 0:
        return
    if len(_CACHE) > 500:
        _CACHE.clear()
    key = _cache_key(model, body)
    _CACHE[key] = {"key": key, "expires_at": time.time() + ttl, "channel_id": channel_id,
                   "result": result, "prompt_tokens": pt, "completion_tokens": ct}


def _validate_chat_request(body: dict) -> str:
    """把明显非法的 model / messages 拦在 500 之前，返回可读错误（空串表示通过）。"""
    model = body.get("model")
    if model is not None and not isinstance(model, str):
        return "请求参数 model 必须是字符串。"
    messages = body.get("messages")
    if messages is not None and (not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages)):
        return "请求参数 messages 必须是对象数组。"
    return ""



def _model_channels(model: str) -> list:
    return query("SELECT * FROM channels WHERE status=1 AND instr(','||replace(models,' ','')||',', ','||?||',') > 0",
                 (model,))


# ---- 平滑加权轮询（SWRR，nginx upstream 算法，借鉴 zzcsapi）----
# 与加权随机相比：长期分流比例精确等于权重比（3:1 ⇒ 75%/25%），且不会突发连打同一家。
# 观察期（probation，半愈合）渠道不进轮询池——可被兜底用，但不凭权重抢链首。
_SWRR_CUR: dict[int, float] = {}
_SWRR_HITS: dict[int, int] = {}
_SWRR_TOTAL = 0


def _swrr_pick_index(group: list) -> int:
    """在同级优先级组内按平滑加权轮询选出链首，返回其下标；无可轮询成员返回 -1。"""
    global _SWRR_TOTAL
    pool_idx = [i for i, r in enumerate(group)
                if int(r["weight"] or 1) > 0
                and not _breaker_probation(r)
                and not _breaker_unavailable(r)]
    if not pool_idx:
        return -1
    total = 0.0
    for i in pool_idx:
        w = max(1, int(group[i]["weight"] or 1))
        total += w
        _SWRR_CUR[group[i]["id"]] = _SWRR_CUR.get(group[i]["id"], 0.0) + w
    best = pool_idx[0]
    for i in pool_idx:
        if _SWRR_CUR.get(group[i]["id"], 0.0) > _SWRR_CUR.get(group[best]["id"], 0.0):
            best = i
    _SWRR_CUR[group[best]["id"]] = _SWRR_CUR.get(group[best]["id"], 0.0) - total
    _SWRR_HITS[group[best]["id"]] = _SWRR_HITS.get(group[best]["id"], 0) + 1
    _SWRR_TOTAL += 1
    return best


def swrr_stats() -> dict:
    """各渠道加权轮询命中份额，供管理接口观测分流是否精确。"""
    out = {str(cid): {"hits": n, "share_pct": round(n * 1000 / _SWRR_TOTAL) / 10}
           for cid, n in _SWRR_HITS.items()} if _SWRR_TOTAL else {}
    return {"total": _SWRR_TOTAL, "channels": out}


def _breaker_probation(channel) -> bool:
    """渠道处于半愈合观察期：可作为兜底候选，但排序排在健康渠道之后。"""
    st = _BREAKER.get(channel["id"])
    return bool(st and st.get("probation") and not st.get("auto_disabled")
                and float(st.get("until", 0) or 0) <= time.time())


def _candidates(model: str) -> list:
    """按模型过滤启用渠道：组间按优先级从高到低，组内按 SWRR 平滑加权轮询排链首，
    观察期（半愈合）渠道沉到各组末尾做兜底，返回失败切换候选列表。"""
    rows = _model_channels(model)
    if not rows:
        return []
    ordered = []
    for prio in sorted({r["priority"] for r in rows}, reverse=True):
        grp = [r for r in rows if r["priority"] == prio]
        pick = _swrr_pick_index(grp)
        if pick > 0:
            grp = [grp[pick]] + grp[:pick] + grp[pick + 1:]
        # 健康在前、观察期兜底；组内其余保持稳定顺序
        grp.sort(key=lambda r: 1 if _breaker_probation(r) else 0)
        ordered.extend(grp)
    alive = [r for r in ordered
             if not _breaker_unavailable(r)
             and not _channel_rate_exceeded(r["id"], _int_field(r, "rpm_limit"))]
    return alive[: int(_settings()["retry_max_attempts"])]


def _all_model_channels_cooling(model: str) -> bool:
    rows = _model_channels(model)
    return bool(rows) and all(_breaker_unavailable(row) for row in rows)


def _all_model_channels_rate_limited(model: str) -> bool:
    """模型的所有渠道都因渠道级速率上限被跳过（且没有处于冷却/禁用的渠道）。"""
    rows = _model_channels(model)
    if not rows or any(_breaker_unavailable(row) for row in rows):
        return False
    return all(_channel_rate_exceeded(row["id"], _int_field(row, "rpm_limit")) for row in rows)


def _pricing(model: str) -> tuple[float, float]:
    row = query_one("SELECT input_price, output_price FROM models WHERE model_name=?", (model,))
    if row:
        return row["input_price"], row["output_price"]
    return MODEL_FALLBACK.get(model, (1.0, 2.0))


def _est_tokens(chars: int) -> int:
    return max(1, int(chars) // 3)


def _est_prompt_tokens(body: dict) -> int:
    chars = sum(len(str(m.get("content") or "")) for m in (body.get("messages") or []))
    return _est_tokens(chars) + 8


def _usage_or_estimate(usage, prompt_tokens, completion_tokens):
    """保留上游 usage 来源；没有可用 usage 时才标记为估算。"""
    usage = usage if isinstance(usage, dict) else {}
    has_usage = any(usage.get(key) is not None for key in ("prompt_tokens", "completion_tokens"))
    if has_usage:
        return (usage.get("prompt_tokens") if usage.get("prompt_tokens") is not None else 0,
                usage.get("completion_tokens") if usage.get("completion_tokens") is not None else 0,
                "upstream")
    if prompt_tokens is None or completion_tokens is None:
        return prompt_tokens or 0, completion_tokens or 0, "unknown"
    return prompt_tokens, completion_tokens, "estimated"


def _record(rid, key_row, channel, model, endpoint, pt, ct, cost,
            latency_ms, status, status_code, error_msg, ip, will_retry=False,
            usage_source="unknown") -> None:
    """key_row 同时携带所属用户信息（username/user_id），用量分别落到用户与 Key 上。
    同一次客户端调用在渠道切换时可能产生多条日志，rid 用于串起一次请求的所有尝试。"""
    uid = key_row["user_id"] if key_row else None
    error_category = ""
    if status != "success":
        lowered = str(error_msg or "").lower()
        if "通用 api 客户端" in lowered or "unauthorized client" in lowered:
            error_category = "client_restricted"
        elif "输出预算耗尽" in str(error_msg):
            error_category = "incomplete_output"
        elif "协议错误" in str(error_msg):
            error_category = "protocol_error"
        elif "工具调用" in str(error_msg):
            error_category = "tool_not_supported"
        else:
            error_category = classify_upstream_error(status_code, error_msg)
    if usage_source not in {"upstream", "estimated", "synthetic", "unknown"}:
        usage_source = "unknown"
    execute(
        """INSERT INTO request_logs(request_id,user_id,username,api_key_id,channel_id,channel_name,
           model,endpoint,prompt_tokens,completion_tokens,usage_source,cost,latency_ms,status,status_code,error_msg,error_category,will_retry,ip,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rid, uid,
         key_row["username"] if key_row else "",
         key_row["id"] if key_row else None,
         channel["id"] if channel else None,
         channel["name"] if channel else "",
         model, endpoint, pt, ct, usage_source, cost, latency_ms, status, status_code, error_msg, error_category, int(will_retry), ip,
         iso(utcnow())),
    )
    if key_row and status == "success":
        execute("UPDATE users SET used_quota=used_quota+?, quota=quota-? WHERE id=?",
                (cost, cost, uid))
        execute("UPDATE api_keys SET used_quota=used_quota+?, last_used_at=? WHERE id=?",
                (cost, iso(utcnow()), key_row["id"]))


def _error(code: int, message: str, err_type: str = "obsession_api_error", request_id: str = ""):
    payload = {"error": {"message": message, "type": err_type, "code": code}}
    if request_id:
        payload["request_id"] = request_id
    headers = {"X-Request-ID": request_id} if request_id else None
    return JSONResponse(payload, status_code=code, headers=headers)


async def _request_json_object(request: Request, request_id: str):
    try:
        body = await request.json()
    except Exception:
        return None, _error(400, "请求体不是合法的 JSON。", "invalid_request_error", request_id=request_id)
    if not isinstance(body, dict):
        return None, _error(400, "请求体顶层必须是 JSON 对象。", "invalid_request_error", request_id=request_id)
    return body, None


# ------------------------------------------------------------------ mock 上游
MOCK_REPLIES = [
    "你好！我是 OBSESSIONAPI 本地模拟渠道返回的回答，用于在未接入真实上游时演示网关的完整链路：鉴权、路由、用量统计与日志。",
    "This response is generated by the built-in mock upstream of OBSESSIONAPI, so the gateway pipeline (auth -> routing -> usage -> logging) can be demonstrated end to end.",
    "网关已成功完成一次完整调用：你的 API Key 已通过鉴权，请求被路由到本地模拟渠道，token 用量已计入用量统计。",
]


async def _mock_completion(body: dict, model: str) -> dict:
    messages = body.get("messages") or [{"role": "user", "content": ""}]
    prompt_text = " ".join(str(m.get("content", "")) for m in messages)
    pt = max(8, int(len(prompt_text) * 0.35) + random.randint(5, 40))
    ct = random.randint(40, 260)
    await asyncio.sleep(random.uniform(0.15, 0.6))
    reply = random.choice(MOCK_REPLIES)
    return {
        "id": "chatcmpl-mock" + secrets.token_hex(10),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": reply},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    }


async def _mock_stream(body: dict, model: str) -> AsyncIterator[bytes]:
    messages = body.get("messages") or [{"role": "user", "content": ""}]
    prompt_text = " ".join(str(m.get("content", "")) for m in messages)
    pt = max(8, int(len(prompt_text) * 0.35) + random.randint(5, 40))
    reply = random.choice(MOCK_REPLIES)
    pieces = [reply[i:i + 6] for i in range(0, len(reply), 6)]
    chat_id = "chatcmpl-mock" + secrets.token_hex(10)

    def sse(obj) -> bytes:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()

    yield sse({"id": chat_id, "object": "chat.completion.chunk", "created": int(time.time()),
               "model": model, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
    for piece in pieces:
        await asyncio.sleep(random.uniform(0.02, 0.08))
        yield sse({"id": chat_id, "object": "chat.completion.chunk", "created": int(time.time()),
                   "model": model,
                   "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]})
    ct = len(pieces) * 3
    yield sse({"id": chat_id, "object": "chat.completion.chunk", "created": int(time.time()),
               "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}})
    yield b"data: [DONE]\n\n"


def _chat_chunk(stream_id: str, model: str, delta: dict, finish_reason=None, usage=None) -> bytes:
    payload = {
        "id": stream_id or "chatcmpl-gateway",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage:
        payload["usage"] = usage
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


def _protocol_request(channel, protocol: str, body: dict, upstream_model: str, stream: bool,
                      request: Request | None = None):
    if protocol == "openai_responses":
        url = normalize_openai_responses_base_url(channel["base_url"]) + "/v1/responses"
        headers = {"Authorization": f"Bearer {channel['api_key']}", "Content-Type": "application/json"}
        return url, openai_to_responses(body), build_channel_headers(channel, headers, request, protocol)
    root = normalize_gemini_base_url(channel["base_url"])
    action = "streamGenerateContent?alt=sse" if stream else "generateContent"
    url = root + "/v1beta/models/" + quote(upstream_model, safe="") + ":" + action
    headers = {"x-goog-api-key": channel["api_key"], "Content-Type": "application/json"}
    return url, openai_to_gemini(body), build_channel_headers(channel, headers, request, protocol)


def _is_client_restricted(detail: str) -> bool:
    lowered = str(detail or "").lower()
    return "unauthorized_client_error" in lowered or "unauthorized client detected" in lowered or "通用 api 客户端" in lowered


async def _protocol_completion(channel, protocol: str, body: dict, public_model: str,
                               request: Request | None = None):
    try:
        url, payload, headers = _protocol_request(
            channel, protocol, body, payload_model := body.get("model", public_model), False, request
        )
    except (TypeError, ValueError) as exc:
        return None, 502, upstream_error_message(channel["name"], 502, str(exc), channel["api_key"]), "server_error"
    try:
        async with _http_client(channel) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.RequestError as exc:
        message = upstream_error_message(channel["name"], 502, _request_error_detail(exc), channel["api_key"])
        return None, 502, message, "server_error"
    content_type = response.headers.get("content-type", "")
    if response.status_code != 200:
        if _tools_unsupported(response.text, body):
            return None, 400, _tool_not_supported_message(channel["name"]), "tool_not_supported"
        message = upstream_error_message(channel["name"], response.status_code, response.text[:300], channel["api_key"], content_type)
        return None, response.status_code, message, "client_restricted" if _is_client_restricted(response.text) else "server_error"
    try:
        parsed = response.json()
        result = responses_to_chat(parsed, public_model) if protocol == "openai_responses" else gemini_to_openai(parsed, public_model)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        detail = _invalid_response_detail(parsed) if "parsed" in locals() else str(exc)
        message = _protocol_error_message(channel["name"], detail, channel["api_key"], content_type)
        return None, 502, message, "server_error"
    response_state = _chat_response_state(result)
    if response_state == "incomplete":
        return result, 200, _incomplete_response_detail(result), "incomplete_output"
    if response_state != "valid":
        message = _protocol_error_message(channel["name"], _invalid_response_detail(result), channel["api_key"], content_type)
        return None, 502, message, "server_error"
    return result, 200, "", ""


def _protocol_event(event: dict, protocol: str, state: dict, model: str):
    if not isinstance(event, dict):
        return None, False, True, "SSE data 顶层不是对象"
    if event.get("type") in {"error", "response.failed"} or event.get("error"):
        error = event.get("error") if isinstance(event.get("error"), dict) else {}
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        if not error and isinstance(response.get("error"), dict):
            error = response["error"]
        return None, False, True, str(error.get("message") or "上游流式响应返回错误")
    if protocol == "openai_responses":
        event_type = event.get("type")
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        state["stream_id"] = str(response.get("id") or state.get("stream_id") or "")
        if response.get("model"):
            state["model"] = str(response["model"])
        if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str) and event["delta"]:
            state["output"] = True
            return _chat_chunk(state["stream_id"], state.get("model", model), {"content": event["delta"]}), True, False, ""
        if event_type == "response.output_item.added":
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if item.get("type") == "function_call":
                state["output"] = True
                indices = state.setdefault("tool_indices", {})
                identities = ([f"item:{item['id']}"] if item.get("id") else [])
                if event.get("output_index") is not None:
                    identities.append(f"output:{event['output_index']}")
                index = next((indices[key] for key in identities if key in indices), None)
                if index is None:
                    index = state.get("next_tool_index", 0)
                    state["next_tool_index"] = index + 1
                for key in identities:
                    indices[key] = index
                state["tool_index"] = index
                delta = {"tool_calls": [{"index": index, "id": item.get("call_id") or item.get("id", ""),
                                          "type": "function", "function": {"name": item.get("name", ""), "arguments": ""}}]}
                return _chat_chunk(state["stream_id"], state.get("model", model), delta), True, False, ""
        if event_type == "response.function_call_arguments.delta" and isinstance(event.get("delta"), str):
            state["output"] = True
            indices = state.get("tool_indices", {})
            index = indices.get(f"item:{event.get('item_id')}", indices.get(f"output:{event.get('output_index')}"))
            if index is None:
                return None, False, True, "工具参数在 function_call 开始之前到达"
            delta = {"tool_calls": [{"index": index, "function": {"arguments": event["delta"]}}]}
            return _chat_chunk(state["stream_id"], state.get("model", model), delta), True, False, ""
        if event_type in {"response.completed", "response.incomplete"}:
            if event_type == "response.incomplete":
                details = response.get("incomplete_details") if isinstance(response.get("incomplete_details"), dict) else {}
                if details.get("reason") != "max_output_tokens":
                    return None, False, True, f"上游 Responses 输出不完整（reason={details.get('reason', 'unknown')}）"
                state["incomplete"] = True
            usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
            mapped = {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0)} if usage else None
            if mapped:
                mapped["total_tokens"] = mapped["prompt_tokens"] + mapped["completion_tokens"]
                state["usage"] = mapped
            state["completed"] = True
            if state.get("output") or state.get("incomplete"):
                state["finish_sent"] = True
                finish = "length" if state.get("incomplete") else "tool_calls" if state.get("tool_index") is not None else "stop"
                return _chat_chunk(state["stream_id"], state.get("model", model), {}, finish, mapped), False, False, ""
        return None, False, False, ""
    usage = event.get("usageMetadata")
    if isinstance(usage, dict):
        mapped = {target: usage[source] for source, target in (
            ("promptTokenCount", "prompt_tokens"), ("candidatesTokenCount", "completion_tokens"),
            ("totalTokenCount", "total_tokens"),
        ) if usage.get(source) is not None}
        if mapped:
            mapped.setdefault("total_tokens", mapped.get("prompt_tokens", 0) + mapped.get("completion_tokens", 0))
            state["usage"] = mapped
    candidates = event.get("candidates")
    candidate = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else {}
    finish_reason = candidate.get("finishReason")
    if finish_reason:
        if finish_reason not in {"STOP", "MAX_TOKENS"}:
            return None, False, True, f"Gemini 响应未正常完成（finishReason={finish_reason}）"
        state["completed"] = True
        if finish_reason == "MAX_TOKENS":
            state["incomplete"] = True
    try:
        result = gemini_to_openai(event, model)
    except (TypeError, ValueError, json.JSONDecodeError):
        if finish_reason and (state.get("output") or state.get("incomplete")):
            state["finish_sent"] = True
            return _chat_chunk(state.get("stream_id", ""), model, {},
                               "length" if finish_reason == "MAX_TOKENS" else "tool_calls" if state.get("next_tool_index") else "stop",
                               state.get("usage")), False, False, ""
        if isinstance(usage, dict) and state.get("usage"):
            return _chat_chunk(state.get("stream_id", ""), model, {}, usage=state["usage"]), False, False, ""
        return None, False, False, ""
    choice = (result.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    if isinstance(result.get("usage"), dict) and result.get("usage"):
        state["usage"] = result["usage"]
    delta = {key: message[key] for key in ("content", "tool_calls", "function_call", "refusal") if key in message and message[key] not in (None, "", [])}
    raw_parts = candidate.get("content", {}).get("parts", [])
    raw_calls = [part["functionCall"] for part in raw_parts if isinstance(part, dict) and isinstance(part.get("functionCall"), dict)]
    for position, call in enumerate(delta.get("tool_calls", [])):
        index = state.get("next_tool_index", 0)
        state["next_tool_index"] = index + 1
        call["index"] = index
        if position >= len(raw_calls) or not raw_calls[position].get("id"):
            call["id"] = f"gemini_call_{index}"
    if not delta:
        return None, False, False, ""
    state["output"] = True
    finish = choice.get("finish_reason") if finish_reason else None
    if finish:
        state["finish_sent"] = True
    return _chat_chunk(state.get("stream_id", ""), model, delta, finish), True, False, ""


async def _begin_protocol_stream(channel, protocol: str, body: dict, public_model: str,
                                 request: Request | None = None):
    try:
        url, payload, headers = _protocol_request(
            channel, protocol, body, body.get("model", public_model), True, request
        )
    except (TypeError, ValueError) as exc:
        return None, 502, upstream_error_message(channel["name"], 502, str(exc), channel["api_key"]), "server_error"
    client = _http_client(channel, stream=True)
    response = None
    handed_off = False
    try:
        response = await client.send(client.build_request("POST", url, json=payload, headers=headers), stream=True)
        if response.status_code != 200:
            content_type = response.headers.get("content-type", "")
            detail = (await response.aread()).decode(errors="replace")[:300]
            if _tools_unsupported(detail, body):
                return None, 400, _tool_not_supported_message(channel["name"]), "tool_not_supported"
            message = upstream_error_message(channel["name"], response.status_code, detail, channel["api_key"], content_type)
            return None, response.status_code, message, "client_restricted" if _is_client_restricted(detail) else "server_error"
        events = []
        state = {}
        stream_lines = response.aiter_lines()
        while True:
            try:
                line = await anext(stream_lines)
            except StopAsyncIteration:
                break
            if not line.startswith("data:"):
                continue
            raw = line.split(":", 1)[1].strip()
            if raw == "[DONE]":
                break
            try:
                event = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                return None, 502, _protocol_error_message(channel["name"], "SSE data 不是合法 JSON", channel["api_key"], response.headers.get("content-type", "")), "server_error"
            events.append(event)
            _, has_output, is_error, error_detail = _protocol_event(event, protocol, state, public_model)
            if is_error:
                if _tools_unsupported(error_detail, body):
                    return None, 400, _tool_not_supported_message(channel["name"]), "tool_not_supported"
                message = upstream_error_message(channel["name"], 502, error_detail, channel["api_key"])
                return None, 502, message, "client_restricted" if _is_client_restricted(error_detail) else "server_error"
            if has_output:
                break
            if state.get("completed"):
                break
        if not state.get("output") and not state.get("incomplete"):
            return None, 502, _protocol_error_message(channel["name"], "流式响应在产生有效输出前结束", channel["api_key"]), "server_error"
        handed_off = True
        return (client, response, stream_lines, events), 200, "", ""
    except httpx.RequestError as exc:
        return None, 502, upstream_error_message(channel["name"], 502, _request_error_detail(exc), channel["api_key"]), "server_error"
    finally:
        if not handed_off:
            try:
                if response is not None:
                    await response.aclose()
            finally:
                await client.aclose()


async def _protocol_stream_relay(attempt, channel, protocol, public_model, finalize, pt_est):
    client, response, stream_lines, prefetched = attempt
    state = {}
    normal_end = False
    error = ""
    usage = {}
    try:
        for event in prefetched:
            chunk, _, is_error, detail = _protocol_event(event, protocol, state, public_model)
            if is_error:
                error = upstream_error_message(channel["name"], 502, detail, channel["api_key"])
                yield ("data: " + json.dumps({"error": {"message": error, "type": "server_error", "code": 502}}, ensure_ascii=False) + "\n\n").encode()
                break
            if chunk:
                yield chunk
        if not error:
            async for line in stream_lines:
                if not line.startswith("data:"):
                    continue
                raw = line.split(":", 1)[1].strip()
                if raw == "[DONE]":
                    break
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    error = _protocol_error_message(channel["name"], "SSE data 不是合法 JSON", channel["api_key"])
                    yield ("data: " + json.dumps({"error": {"message": error, "type": "server_error", "code": 502}}, ensure_ascii=False) + "\n\n").encode()
                    break
                chunk, _, is_error, detail = _protocol_event(event, protocol, state, public_model)
                if is_error:
                    error = upstream_error_message(channel["name"], 502, detail, channel["api_key"])
                    yield ("data: " + json.dumps({"error": {"message": error, "type": "server_error", "code": 502}}, ensure_ascii=False) + "\n\n").encode()
                    break
                if chunk:
                    yield chunk
        if state.get("completed"):
            normal_end = True
        if not error and not normal_end:
            error = upstream_error_message(channel["name"], 502, "流式响应异常结束", channel["api_key"])
            yield ("data: " + json.dumps({"error": {"message": error, "type": "server_error", "code": 502}}, ensure_ascii=False) + "\n\n").encode()
        if not error and normal_end and state.get("output") and not state.get("finish_sent"):
            state["finish_sent"] = True
            yield _chat_chunk(state.get("stream_id", ""), state.get("model", public_model), {}, "stop")
        usage = state.get("usage") if isinstance(state.get("usage"), dict) else {}
        pt, ct, usage_source = _usage_or_estimate(usage, pt_est, 0)
        if error:
            finalize(channel, pt, ct, "error", 502, error, breaker_failure=True,
                     usage_source=usage_source)
        elif state.get("incomplete"):
            finalize(channel, pt, ct, "error", 200, "请求已返回，但输出预算耗尽（finish_reason=length），未取得最终回答",
                     breaker_failure=False, usage_source=usage_source)
        else:
            finalize(channel, pt, ct, "success", 200, usage_source=usage_source)
    except Exception as exc:  # noqa: BLE001
        error = upstream_error_message(channel["name"], 502, _request_error_detail(exc), channel["api_key"])
        yield ("data: " + json.dumps({"error": {"message": error, "type": "server_error", "code": 502}}, ensure_ascii=False) + "\n\n").encode()
        finalize(channel, pt_est, 0, "error", 502, error, breaker_failure=True,
                 usage_source="estimated")
    finally:
        try:
            await response.aclose()
        finally:
            await client.aclose()
    yield b"data: [DONE]\n\n"


# ------------------------------------------------------------------ 共享实现
def _authorize_request(request: Request, model: str, rid: str):
    """网关鉴权 + Key 级白名单/RPM 限流。返回 (key_row, error_response)，成功时后者为 None。"""
    key_row = _auth_key(request)
    if key_row is None:
        return None, _error(401, "网关 API Key 无效，请在管理后台「API Key 管理」中创建。", "invalid_api_key", request_id=rid)
    if key_row["status"] != 1 or key_row["user_status"] != 1:
        return None, _error(403, "该 API Key 或所属用户已被禁用。", "permission_denied", request_id=rid)
    if key_row["quota_limit"] is not None and key_row["used_quota"] >= key_row["quota_limit"]:
        return None, _error(429, "该 API Key 已达到额度上限。", "insufficient_quota", request_id=rid)
    if key_row["user_role"] != "admin" and key_row["user_quota"] <= 0:
        return None, _error(429, "所属用户额度已用尽。", "insufficient_quota", request_id=rid)
    allowed = key_allowed_models(key_row)
    if allowed and model not in allowed:
        return None, _error(403, f"该 API Key 未授权访问模型 {model}。", "model_not_allowed", request_id=rid)
    rpm_limit = _int_field(key_row, "rpm_limit")
    if _key_rate_limited(key_row["id"], rpm_limit):
        response = _error(429, f"该 API Key 每分钟请求数已超过上限（{rpm_limit}）。", "rate_limit_exceeded", request_id=rid)
        response.headers["Retry-After"] = "60"
        return None, response
    return key_row, None


def _resolve_candidates(model: str, rid: str):
    """校验公开模型并解析渠道候选。返回 (candidates, error_response)，成功时后者为 None。"""
    model_row = query_one("SELECT * FROM models WHERE model_name=? AND status=1", (model,))
    if model_row is None:
        return None, _error(404, f"模型 {model} 不存在或已停用。", "model_not_found", request_id=rid)
    candidates = _candidates(model)
    if not candidates:
        if _all_model_channels_cooling(model):
            remaining = min(
                (channel_breaker_state(row["id"])["cooldown_remaining_s"] for row in _model_channels(model)),
                default=1,
            )
            response = _error(503, f"公开模型 {model} 的所有渠道都在冷却中，请稍后重试。", "channels_cooling", request_id=rid)
            response.headers["Retry-After"] = str(max(1, remaining))
            return None, response
        if _all_model_channels_rate_limited(model):
            response = _error(429, f"公开模型 {model} 的所有渠道都达到速率上限，请稍后重试。", "channel_rate_limited", request_id=rid)
            response.headers["Retry-After"] = "60"
            return None, response
        return None, _error(503, f"没有可用渠道服务模型 {model}，请检查渠道配置。", "no_available_channel", request_id=rid)
    return candidates, None


async def _chat_inner(request: Request, body: dict, endpoint: str = "/v1/chat/completions", request_id: str = ""):
    start = time.perf_counter()
    ip = request.client.host if request.client else ""
    rid = request_id or secrets.token_hex(8)

    if not isinstance(body, dict):
        return _error(400, "请求体顶层必须是 JSON 对象。", "invalid_request_error", request_id=rid)
    invalid = _validate_chat_request(body)
    if invalid:
        return _error(400, invalid, "invalid_request_error", request_id=rid)

    model = body.get("model", "")
    if not isinstance(model, str):
        model = ""
    stream = bool(body.get("stream"))

    key_row, error = _authorize_request(request, model, rid)
    if error is not None:
        return error
    candidates, error = _resolve_candidates(model, rid)
    if error is not None:
        return error

    cached = _cache_lookup(model, body)
    if cached is not None:
        channel_row = query_one("SELECT * FROM channels WHERE id=?", (cached["channel_id"],)) if cached.get("channel_id") else None
        latency = int((time.perf_counter() - start) * 1000)
        _record(rid, key_row, channel_row, model, endpoint,
                cached["prompt_tokens"], cached["completion_tokens"], 0.0, latency,
                "success", 200, "", ip, False, "synthetic")
        return JSONResponse(cached["result"], headers={"X-Request-ID": rid, "X-Cache": "HIT"})

    pt_est = _est_prompt_tokens(body)
    last_error = ""
    attempt_diagnostic = ""

    def finalize(ch, pt, ct, status, code, err="", will_retry=False, breaker_failure=True,
                 usage_source="unknown", result=None, retry_after_ms=0):
        ip_p, op_p = _pricing(model)
        cost = ((pt if isinstance(pt, (int, float)) else 0) * ip_p +
                (ct if isinstance(ct, (int, float)) else 0) * op_p) / 1_000_000
        latency = int((time.perf_counter() - start) * 1000)
        if status != "success" and err:
            upstream_model = upstream_model_for_channel(ch, model) if ch else ""
            upstream_status = 200 if "HTTP 200" in str(err) else code
            gateway_status = 400 if code == 400 else 200 if code == 200 else 502
            stage = "upstream_response" if ("协议错误" in str(err) or "输出预算耗尽" in str(err)) else "upstream_request"
            err = (
                f"{err}；request_id={rid}；public_model={model}；upstream_model={upstream_model}；"
                f"channel={ch['name'] if ch else 'gateway'}；stage={stage}；"
                f"upstream_status={upstream_status}；gateway_status={gateway_status}"
            )
            if attempt_diagnostic:
                err += f"；{attempt_diagnostic}"
        _record(rid, key_row, ch, model, endpoint,
                pt, ct, cost, latency, status, code, err, ip, will_retry, usage_source)
        if status == "success":
            _breaker_mark(ch["id"], True)
            if result is not None and ch is not None:
                _cache_store(model, body, ch["id"], result, pt, ct)
        elif breaker_failure:
            _breaker_mark(ch["id"], False, reason=classify_upstream_error(code, err),
                          retry_after_ms=retry_after_ms)

    for attempt_index, ch in enumerate(candidates):
        if attempt_index:
            try:
                disconnected = await request.is_disconnected()
            except Exception:  # noqa: BLE001
                disconnected = False
            if disconnected:
                return _error(499, "客户端已断开连接，停止重试。", "client_disconnected", request_id=rid)
        has_next_channel = attempt_index + 1 < len(candidates)
        _channel_rate_consume(ch["id"], _int_field(ch, "rpm_limit"))
        protocol = channel_protocol(ch)
        outbound_body = dict(body)
        outbound_body["model"] = upstream_model_for_channel(ch, model)
        if ch["tool_policy"] == "strip":
            outbound_body = _without_tools(outbound_body)
        try:
            if protocol == "openai_chat":
                _prepare_openai_reasoning(ch, outbound_body)
            elif "thinking" in outbound_body:
                raise ValueError(f"渠道协议 {protocol} 未实现 Anthropic thinking 映射")
        except ValueError as exc:
            last_error = f"请求参数不支持：{exc}"
            attempt_diagnostic = _request_diagnostic(body, outbound_body, endpoint, stream, protocol)
            finalize(ch, None, None, "error", 400, last_error, False, breaker_failure=False)
            return _error(400, last_error, "invalid_request_error", request_id=rid)
        attempt_diagnostic = _request_diagnostic(body, outbound_body, endpoint, stream, protocol)
        # ---- 本地模拟渠道
        if protocol == "mock":
            if stream:
                async def mock_relay(ch=ch):
                    ct_out = {"v": 0}
                    async for chunk in _mock_stream(outbound_body, model):
                        if b'"usage"' in chunk:
                            try:
                                payload = json.loads(chunk[6:-2].decode())
                                usage = payload.get("usage") or {}
                                ct_out["v"] = usage.get("completion_tokens", 0)
                            except Exception:
                                pass
                        yield chunk
                    finalize(ch, pt_est, ct_out["v"], "success", 200, usage_source="synthetic")
                return StreamingResponse(mock_relay(), media_type="text/event-stream",
                                         headers={"X-Accel-Buffering": "no", "X-Request-ID": rid})
            result = await _mock_completion(outbound_body, model)
            usage = result["usage"]
            finalize(ch, usage["prompt_tokens"], usage["completion_tokens"], "success", 200,
                     usage_source="synthetic", result=result)
            return JSONResponse(result, headers={"X-Request-ID": rid})

        if protocol in {"openai_responses", "gemini"}:
            if not stream:
                result, result_code, result_message, result_type = await _protocol_completion(
                    ch, protocol, outbound_body, model, request
                )
                if result is not None:
                    usage = result.get("usage") or {}
                    message = ((result.get("choices") or [{}])[0].get("message") or {})
                    if isinstance(message.get("content"), str):
                        message["content"] = strip_seekai_attribution(message["content"])
                    if result_type == "incomplete_output":
                        pt, ct, usage_source = _usage_or_estimate(usage, None, None)
                        finalize(ch, pt, ct, "error", 200, result_message, False,
                                 breaker_failure=False, usage_source=usage_source)
                        return JSONResponse(
                            result, status_code=200,
                            headers={"X-Request-ID": rid, "X-OBSESSIONAPI-Completion-State": "incomplete"},
                        )
                    pt, ct, usage_source = _usage_or_estimate(
                        usage, pt_est, _est_tokens(len(str(message.get("content") or ""))))
                    finalize(ch, pt, ct, "success", 200, usage_source=usage_source, result=result)
                    return JSONResponse(result, headers={"X-Request-ID": rid})
                last_error = result_message
                if result_type == "tool_not_supported":
                    finalize(ch, 0, 0, "error", 400, last_error, has_next_channel, breaker_failure=False)
                    if has_next_channel:
                        continue
                    return _error(400, last_error, "tool_not_supported", request_id=rid)
                will_retry = has_next_channel and _retry_allowed(result_code, last_error)
                finalize(ch, 0, 0, "error", result_code, last_error, will_retry)
                if has_next_channel and will_retry:
                    await _retry_pause(attempt_index)
                    continue
                return _error(502, last_error, result_type or "server_error", request_id=rid)
            attempt, result_code, result_message, result_type = await _begin_protocol_stream(
                ch, protocol, outbound_body, model, request
            )
            if attempt is not None:
                return StreamingResponse(
                    _protocol_stream_relay(attempt, ch, protocol, model, finalize, pt_est),
                    media_type="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "X-Request-ID": rid},
                )
            last_error = result_message
            if result_type == "tool_not_supported":
                finalize(ch, 0, 0, "error", 400, last_error, has_next_channel, breaker_failure=False)
                if has_next_channel:
                    continue
                return _error(400, last_error, "tool_not_supported", request_id=rid)
            will_retry = has_next_channel and _retry_allowed(result_code, last_error)
            finalize(ch, 0, 0, "error", result_code, last_error, will_retry)
            if has_next_channel and will_retry:
                await _retry_pause(attempt_index)
                continue
            return _error(502, last_error, result_type or "server_error", request_id=rid)

        # ---- Anthropic Messages 上游：只在网关内部转换，外部仍保持 OpenAI 协议
        if protocol == "anthropic":
            try:
                url = normalize_anthropic_base_url(ch["base_url"]) + "/v1/messages"
                anthropic_body = _openai_to_anthropic(outbound_body)
            except ValueError as exc:
                last_error = _anthropic_error_message(ch, ch["base_url"], 400, str(exc))
                finalize(ch, 0, 0, "error", 400, last_error, False, breaker_failure=False)
                return _error(400, last_error, "invalid_request_error", request_id=rid)
            headers = build_channel_headers(ch, anthropic_headers(ch["api_key"]), request, "anthropic")

            if not stream:
                try:
                    async with _http_client(ch) as client:
                        resp = await client.post(url, json=anthropic_body, headers=headers)
                except Exception as exc:  # noqa: BLE001
                    last_error = _anthropic_error_message(ch, url, 502, _request_error_detail(exc))
                    finalize(ch, 0, 0, "error", 502, last_error, has_next_channel)
                    continue
                if resp.status_code != 200:
                    if _tools_unsupported(resp.text, outbound_body):
                        last_error = _tool_not_supported_message(ch["name"])
                        finalize(ch, 0, 0, "error", 400, last_error, has_next_channel, breaker_failure=False)
                        if has_next_channel:
                            continue
                        return _error(400, last_error, "tool_not_supported", request_id=rid)
                    last_error = _anthropic_error_message(
                        ch, url, resp.status_code, resp.text,
                        resp.headers.get("content-type", ""),
                    )
                    will_retry = has_next_channel and _retry_allowed(resp.status_code, last_error)
                    finalize(ch, 0, 0, "error", resp.status_code, last_error, will_retry,
                             retry_after_ms=_parse_retry_after_ms(resp.headers))
                    if will_retry:
                        await _retry_pause(attempt_index)
                        continue
                    return _error(502, last_error, "client_restricted" if _is_client_restricted(resp.text) else "server_error", request_id=rid)
                try:
                    result = _anthropic_to_openai(resp.json(), model)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    last_error = _anthropic_error_message(ch, url, 502, str(exc), resp.headers.get("content-type", ""))
                    finalize(ch, 0, 0, "error", 502, last_error, has_next_channel)
                    if has_next_channel:
                        continue
                    return _error(502, last_error, "server_error", request_id=rid)
                usage = result.get("usage") or {}
                message = ((result.get("choices") or [{}])[0].get("message") or {})
                if isinstance(message.get("content"), str):
                    message["content"] = strip_seekai_attribution(message["content"])
                reply_text = message.get("content") or json.dumps(message, ensure_ascii=False)
                pt, ct, usage_source = _usage_or_estimate(
                    usage, pt_est, _est_tokens(len(str(reply_text))))
                finalize(ch, pt, ct, "success", 200, usage_source=usage_source, result=result)
                return JSONResponse(result, headers={"X-Request-ID": rid})

            client = _http_client(ch, stream=True)
            try:
                req = client.build_request("POST", url, json=anthropic_body, headers=headers)
                resp = await client.send(req, stream=True)
            except Exception as exc:  # noqa: BLE001
                await client.aclose()
                last_error = _anthropic_error_message(ch, url, 502, _request_error_detail(exc))
                finalize(ch, 0, 0, "error", 502, last_error, has_next_channel)
                continue
            if resp.status_code != 200:
                content_type = resp.headers.get("content-type", "")
                retry_after_ms = _parse_retry_after_ms(resp.headers)
                text = (await resp.aread()).decode(errors="replace")[:300]
                status_code = resp.status_code
                await resp.aclose()
                await client.aclose()
                if _tools_unsupported(text, outbound_body):
                    last_error = _tool_not_supported_message(ch["name"])
                    finalize(ch, 0, 0, "error", 400, last_error, has_next_channel, breaker_failure=False)
                    if has_next_channel:
                        continue
                    return _error(400, last_error, "tool_not_supported", request_id=rid)
                last_error = _anthropic_error_message(ch, url, status_code, text, content_type)
                will_retry = has_next_channel and _retry_allowed(status_code, last_error)
                finalize(ch, 0, 0, "error", status_code, last_error, will_retry,
                         retry_after_ms=retry_after_ms)
                if will_retry:
                    await _retry_pause(attempt_index)
                    continue
                return _error(502, last_error, "server_error", request_id=rid)

            stream_lines = resp.aiter_lines()
            prefetched = []
            prefetch_error = None
            prefetch_error_message = None
            prefetch_eof = False
            prefetch_normal_end = False
            prefetch_has_output = False
            tool_prefetch_error = False
            try:
                while True:
                    line = await anext(stream_lines)
                    if not line.startswith("data:"):
                        continue
                    payload = line.split(":", 1)[1].strip()
                    if payload == "[DONE]":
                        prefetched.append(line)
                        prefetch_normal_end = True
                        break
                    try:
                        event = json.loads(payload)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        prefetch_error_message = _anthropic_error_message(
                            ch, url, 502, "SSE data 不是合法 JSON", ch["api_key"]
                        )
                        break
                    if not isinstance(event, dict):
                        event = {}
                    event_text = json.dumps(event, ensure_ascii=False)
                    if event.get("type") == "error":
                        error = event.get("error") if isinstance(event.get("error"), dict) else {}
                        detail = error.get("message") or "流式上游返回错误"
                        if _tools_unsupported(event_text, outbound_body):
                            tool_prefetch_error = True
                        else:
                            prefetch_error_message = _anthropic_error_message(ch, url, 502, detail)
                        break
                    prefetched.append(line)
                    if _anthropic_stream_has_output(event):
                        prefetch_has_output = True
                        break
                    if event.get("type") == "message_stop":
                        prefetch_normal_end = True
                        break
            except StopAsyncIteration:
                prefetch_eof = True
            except Exception as exc:  # noqa: BLE001
                prefetch_error = exc

            if tool_prefetch_error:
                await resp.aclose()
                await client.aclose()
                last_error = _tool_not_supported_message(ch["name"])
                finalize(ch, 0, 0, "error", 400, last_error, has_next_channel, breaker_failure=False)
                if has_next_channel:
                    continue
                return _error(400, last_error, "tool_not_supported", request_id=rid)

            pre_output_error = prefetch_error_message
            if not pre_output_error and prefetch_error:
                pre_output_error = _anthropic_error_message(ch, url, 502, _request_error_detail(prefetch_error))
            if not pre_output_error and not prefetch_has_output and (prefetch_eof or prefetch_normal_end):
                pre_output_error = _anthropic_error_message(ch, url, 502, "流式响应在产生有效输出前结束")
            if pre_output_error and not prefetch_has_output:
                await resp.aclose()
                await client.aclose()
                if "tools" not in outbound_body:
                    fallback_result, fallback_code, fallback_message = await _fallback_anthropic_completion(
                        ch, url, outbound_body, headers, model
                    )
                    if fallback_result is not None:
                        async def nonstream_relay(result=fallback_result, ch=ch):
                            response_state = _chat_response_state(result)
                            choice = result["choices"][0]
                            message = choice["message"]
                            delta = {key: message[key] for key in (
                                "content", "tool_calls", "function_call", "refusal", "reasoning_content"
                            ) if key in message}
                            if isinstance(delta.get("content"), str):
                                delta["content"] = strip_seekai_attribution(delta["content"])
                            finish_reason = choice.get("finish_reason") or (
                                "tool_calls" if delta.get("tool_calls") else
                                "function_call" if delta.get("function_call") else "stop"
                            )
                            usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
                            event_base = {
                                "id": result.get("id", ""), "object": "chat.completion.chunk",
                                "created": result.get("created", int(time.time())),
                                "model": result.get("model", model),
                            }
                            def event(choices, extra=None):
                                payload = {**event_base, "choices": choices}
                                if extra:
                                    payload.update(extra)
                                return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()
                            yield event([{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}])
                            yield event([{"index": 0, "delta": delta, "finish_reason": None}])
                            extra = {"usage": usage} if usage else None
                            yield event([{"index": 0, "delta": {}, "finish_reason": finish_reason}], extra)
                            yield b"data: [DONE]\n\n"
                            if response_state == "incomplete":
                                pt, ct, usage_source = _usage_or_estimate(usage, None, None)
                                finalize(ch, pt, ct, "error", 200, _incomplete_response_detail(result), False,
                                         breaker_failure=False, usage_source=usage_source)
                            else:
                                pt, ct, usage_source = _usage_or_estimate(
                                    usage, pt_est, _est_tokens(len(str(delta.get("content") or ""))))
                                finalize(ch, pt, ct, "success", 200, usage_source=usage_source)
                        return StreamingResponse(nonstream_relay(), media_type="text/event-stream",
                                                 headers={"X-Accel-Buffering": "no", "X-Request-ID": rid})
                    last_error = fallback_message
                    finalize(ch, 0, 0, "error", fallback_code, last_error, has_next_channel,
                             breaker_failure=_retry_allowed(fallback_code, last_error))
                else:
                    last_error = pre_output_error
                    finalize(ch, 0, 0, "error", 502, last_error, has_next_channel)
                if has_next_channel:
                    await _retry_pause(attempt_index)
                    continue
                return _error(502, last_error, "server_error", request_id=rid)

            async def relay_anthropic(ch=ch, client=client, resp=resp, prefetched=prefetched, stream_lines=stream_lines):
                ct_chars = {"n": 0}
                real_usage = {"pt": 0, "ct": 0}
                usage_seen = {"v": False}
                tail_filter = _SeekAITailFilter()
                stream_error = None
                stream_error_code = 502
                tool_capability_error = False
                role_sent = False
                output_started = False
                finish_sent = False
                normal_end = False
                done_sent = False
                stream_id = ""
                stream_model = model
                tool_blocks = {}

                def chunk(delta, finish_reason=None, usage=None):
                    payload = {
                        "id": stream_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": stream_model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
                    }
                    if usage:
                        payload["usage"] = usage
                    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()

                def with_role(delta):
                    nonlocal role_sent
                    if not role_sent:
                        role_sent = True
                        return {"role": "assistant", **delta}
                    return delta

                async def lines():
                    for line in prefetched:
                        yield line
                    if prefetch_error:
                        raise prefetch_error
                    async for line in stream_lines:
                        yield line

                try:
                    try:
                        async for line in lines():
                            if not line.startswith("data:"):
                                continue
                            payload = line.split(":", 1)[1].strip()
                            if payload == "[DONE]":
                                normal_end = True
                                break
                            try:
                                event = json.loads(payload)
                            except (TypeError, ValueError, json.JSONDecodeError):
                                event = {"type": "error", "error": {"message": "SSE data 不是合法 JSON"}}
                            if not isinstance(event, dict):
                                event = {"type": "error", "error": {"message": "SSE 数据不是对象"}}
                            event_type = event.get("type")
                            if event_type == "message_start":
                                message = event.get("message") if isinstance(event.get("message"), dict) else {}
                                stream_id = str(message.get("id") or stream_id)
                                stream_model = str(message.get("model") or stream_model)
                                usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
                                if usage.get("input_tokens") is not None:
                                    real_usage["pt"] = usage["input_tokens"]
                                    usage_seen["v"] = True
                                continue
                            if event_type == "error":
                                error = event.get("error") if isinstance(event.get("error"), dict) else {}
                                detail = error.get("message") or "流式上游返回错误"
                                if _tools_unsupported(json.dumps(event, ensure_ascii=False), outbound_body):
                                    tool_capability_error = True
                                    stream_error_code = 400
                                    stream_error = _tool_not_supported_message(ch["name"])
                                else:
                                    stream_error = _anthropic_error_message(ch, url, 502, detail)
                                yield ("data: " + json.dumps({"error": {
                                    "message": stream_error,
                                    "type": "tool_not_supported" if tool_capability_error else "server_error",
                                    "code": stream_error_code,
                                }}, ensure_ascii=False) + "\n\n").encode()
                                break
                            if event_type == "content_block_start":
                                block = event.get("content_block") if isinstance(event.get("content_block"), dict) else {}
                                if block.get("type") != "tool_use":
                                    continue
                                source_index = event.get("index", len(tool_blocks))
                                call_index = len(tool_blocks)
                                tool_blocks[source_index] = {"index": call_index, "id": str(block.get("id") or ""),
                                                             "name": str(block.get("name") or "")}
                                call = {"index": call_index, "id": tool_blocks[source_index]["id"],
                                        "type": "function", "function": {"name": tool_blocks[source_index]["name"], "arguments": ""}}
                                initial_input = block.get("input")
                                if isinstance(initial_input, dict) and initial_input:
                                    call["function"]["arguments"] = json.dumps(initial_input, ensure_ascii=False, separators=(",", ":"))
                                output_started = True
                                yield chunk(with_role({"tool_calls": [call]}))
                                continue
                            if event_type == "content_block_delta":
                                delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
                                text = delta.get("text")
                                partial_json = delta.get("partial_json")
                                refusal = delta.get("refusal")
                                if isinstance(text, str) and text:
                                    output_started = True
                                    replacement = tail_filter.feed(text)
                                    if replacement:
                                        ct_chars["n"] += len(replacement)
                                        yield chunk(with_role({"content": replacement}))
                                elif isinstance(partial_json, str) and partial_json:
                                    source_index = event.get("index", 0)
                                    tool = tool_blocks.setdefault(source_index, {"index": len(tool_blocks), "id": "", "name": ""})
                                    output_started = True
                                    call = {"index": tool["index"], "function": {"arguments": partial_json}}
                                    yield chunk(with_role({"tool_calls": [call]}))
                                elif isinstance(refusal, str) and refusal:
                                    output_started = True
                                    yield chunk(with_role({"refusal": refusal}))
                                continue
                            if event_type == "message_delta":
                                delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
                                usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
                                if usage.get("output_tokens") is not None:
                                    real_usage["ct"] = usage["output_tokens"]
                                    usage_seen["v"] = True
                                reason = delta.get("stop_reason")
                                if reason and not finish_sent:
                                    finish_sent = True
                                    yield chunk({}, _anthropic_stop_reason(reason), {
                                        "prompt_tokens": real_usage["pt"],
                                        "completion_tokens": real_usage["ct"],
                                        "total_tokens": real_usage["pt"] + real_usage["ct"],
                                    } if real_usage["pt"] or real_usage["ct"] else None)
                                continue
                            if event_type == "message_stop":
                                normal_end = True
                                if output_started and not finish_sent:
                                    finish_sent = True
                                    yield chunk({}, "stop")
                                break
                    except Exception as exc:  # noqa: BLE001
                        stream_error = _anthropic_error_message(ch, url, 502, _request_error_detail(exc))
                    if not normal_end and not stream_error:
                        stream_error = _anthropic_error_message(ch, url, 502, "流式响应异常结束")
                    tail = tail_filter.finish()
                    if tail and output_started and not stream_error:
                        ct_chars["n"] += len(tail)
                        yield chunk({"content": tail})
                    if output_started and not finish_sent and normal_end and not stream_error:
                        finish_sent = True
                        yield chunk({}, "stop")
                    if not done_sent:
                        yield b"data: [DONE]\n\n"
                        done_sent = True
                finally:
                    await resp.aclose()
                    await client.aclose()
                    if usage_seen["v"]:
                        usage_pt, usage_ct, usage_source = real_usage["pt"], real_usage["ct"], "upstream"
                    else:
                        usage_pt, usage_ct, usage_source = pt_est, _est_tokens(ct_chars["n"]), "estimated"
                    if stream_error:
                        finalize(ch, usage_pt, usage_ct, "error", stream_error_code,
                                 stream_error, breaker_failure=not tool_capability_error,
                                 usage_source=usage_source)
                    else:
                        finalize(ch, usage_pt, usage_ct, "success", 200, usage_source=usage_source)
            return StreamingResponse(relay_anthropic(), media_type="text/event-stream",
                                     headers={"X-Accel-Buffering": "no", "X-Request-ID": rid})

        # ---- OpenAI 兼容上游（失败自动切换到下一个候选渠道）
        try:
            url = normalize_openai_base_url(ch["base_url"]) + "/chat/completions"
        except ValueError as exc:
            last_error = upstream_error_message(ch["name"], 502, str(exc), ch["api_key"])
            finalize(ch, 0, 0, "error", 502, last_error, has_next_channel)
            continue
        headers = build_channel_headers(
            ch, {"Authorization": f"Bearer {ch['api_key']}", "Content-Type": "application/json"},
            request, "openai_chat"
        )

        if stream:
            client = _http_client(ch, stream=True)
            try:
                req = client.build_request("POST", url, json=outbound_body, headers=headers)
                resp = await client.send(req, stream=True)
            except Exception as exc:  # noqa: BLE001
                await client.aclose()
                last_error = upstream_error_message(
                    ch["name"], 502, _request_error_detail(exc), ch["api_key"]
                )
                finalize(ch, 0, 0, "error", 502, last_error, has_next_channel)
                continue
            if resp.status_code != 200:
                content_type = resp.headers.get("content-type", "")
                text = (await resp.aread()).decode(errors="replace")[:300]
                status_code = resp.status_code
                await resp.aclose()
                await client.aclose()
                if _tools_unsupported(text, outbound_body):
                    last_error = _tool_not_supported_message(ch["name"])
                    finalize(ch, 0, 0, "error", 400, last_error, has_next_channel, breaker_failure=False)
                    if has_next_channel:
                        continue
                    return _error(400, last_error, "tool_not_supported", request_id=rid)
                if status_code != 200:
                    last_error = upstream_error_message(
                        ch["name"], status_code, text, ch["api_key"], content_type
                    )
                    will_retry = has_next_channel and _retry_allowed(status_code, last_error)
                    finalize(ch, 0, 0, "error", status_code, last_error, will_retry)
                    if will_retry:
                        await _retry_pause(attempt_index)
                        continue
                    return _error(502, last_error, "client_restricted" if _is_client_restricted(text) else "server_error", request_id=rid)

            stream_lines = resp.aiter_lines()
            prefetched = []
            prefetch_error = None
            prefetch_eof = False
            prefetch_error_message = None
            tool_prefetch_error = False
            prefetch_has_output = False
            prefetch_incomplete = False
            prefetch_usage = {}
            prefetch_last_event = None

            async def nonstream_relay(result, ch=ch):
                response_state = _chat_response_state(result)
                choice = result["choices"][0]
                message = choice["message"]
                delta = {
                    key: message[key]
                    for key in ("content", "tool_calls", "function_call", "refusal", "reasoning_content")
                    if key in message
                }
                if isinstance(delta.get("content"), str):
                    delta["content"] = strip_seekai_attribution(delta["content"])
                finish_reason = choice.get("finish_reason") or (
                    "tool_calls" if delta.get("tool_calls") else
                    "function_call" if delta.get("function_call") else "stop"
                )
                usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
                event_base = {
                    "id": result.get("id", ""),
                    "object": "chat.completion.chunk",
                    "created": result.get("created", int(time.time())),
                    "model": result.get("model", model),
                }

                def sse_event(choices):
                    return ("data: " + json.dumps({**event_base, "choices": choices}, ensure_ascii=False) + "\n\n").encode()

                yield sse_event([{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}])
                yield sse_event([{"index": 0, "delta": delta, "finish_reason": None}])
                finish_event = {"index": 0, "delta": {}, "finish_reason": finish_reason}
                finish_payload = {**event_base, "choices": [finish_event]}
                if usage:
                    finish_payload["usage"] = usage
                yield ("data: " + json.dumps(finish_payload, ensure_ascii=False) + "\n\n").encode()
                yield b"data: [DONE]\n\n"
                if response_state == "incomplete":
                    pt, ct, usage_source = _usage_or_estimate(usage, None, None)
                    finalize(ch, pt, ct, "error", 200, _incomplete_response_detail(result), False,
                             breaker_failure=False, usage_source=usage_source)
                else:
                    pt, ct, usage_source = _usage_or_estimate(
                        usage, pt_est, _est_tokens(len(str(delta.get("content") or ""))))
                    finalize(ch, pt, ct, "success", 200, usage_source=usage_source)

            try:
                while True:
                    line = await anext(stream_lines)
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload.strip() == "[DONE]":
                        prefetched.append(line)
                        break
                    try:
                        event = json.loads(payload)
                    except Exception:
                        event = {}
                    if not isinstance(event, dict):
                        event = {}
                    prefetch_last_event = event
                    event_text = json.dumps(event, ensure_ascii=False)
                    if isinstance(event.get("usage"), dict):
                        prefetch_usage = event["usage"]
                    choices = event.get("choices") or []
                    finish_reason = choices[0].get("finish_reason") if choices and isinstance(choices[0], dict) else None
                    if finish_reason in {"length", "max_tokens"}:
                        prefetch_incomplete = True
                    has_output = _stream_has_output(event)
                    if event.get("error"):
                        prefetched.append(line)
                        error = event["error"] if isinstance(event["error"], dict) else {}
                        error_text = error.get("message", "流式上游返回错误")
                        if not has_output and _tools_unsupported(event_text, outbound_body):
                            tool_prefetch_error = True
                            break
                        if not has_output:
                            prefetch_error_message = upstream_error_message(
                                ch["name"], 502, error_text, ch["api_key"]
                            )
                            break
                        prefetch_has_output = True
                        break
                    prefetched.append(line)
                    if has_output:
                        prefetch_has_output = True
                        break
            except StopAsyncIteration:
                prefetch_eof = True
            except Exception as exc:  # noqa: BLE001
                prefetch_error = exc
            if tool_prefetch_error:
                await resp.aclose()
                await client.aclose()
                last_error = _tool_not_supported_message(ch["name"])
                finalize(ch, 0, 0, "error", 400, last_error, has_next_channel, breaker_failure=False)
                if has_next_channel:
                    continue
                return _error(400, last_error, "tool_not_supported", request_id=rid)
            pre_output_error = prefetch_error_message
            if not pre_output_error and prefetch_error:
                pre_output_error = upstream_error_message(
                    ch["name"], 502, _request_error_detail(prefetch_error), ch["api_key"]
                )
            if not pre_output_error and not prefetch_has_output and (
                prefetch_eof or any(line[6:].strip() == "[DONE]" for line in prefetched if line.startswith("data: "))
            ):
                pre_output_error = _protocol_error_message(
                    ch["name"], "流式响应在产生有效输出前结束", ch["api_key"],
                    resp.headers.get("content-type", ""),
                )
            if prefetch_incomplete and not prefetch_has_output and not prefetch_error_message and not prefetch_error:
                attempt_diagnostic = (
                    f"{attempt_diagnostic};" + _response_structure_diagnostic(
                        prefetch_last_event,
                        resp.headers.get("content-type", ""),
                        "anthropic" if endpoint.endswith("/messages") else "",
                    )
                )
                await resp.aclose()
                await client.aclose()

                async def incomplete_stream_relay(prefetched=prefetched, usage=prefetch_usage, ch=ch):
                    for line in prefetched:
                        yield (line + "\n\n").encode()
                    if not any(line[6:].strip() == "[DONE]" for line in prefetched if line.startswith("data: ")):
                        yield b"data: [DONE]\n\n"
                    pt, ct, usage_source = _usage_or_estimate(usage, None, None)
                    finalize(ch, pt, ct,
                             "error", 200, "请求已返回，但输出预算耗尽（finish_reason=length），未取得最终回答",
                             False, breaker_failure=False, usage_source=usage_source)

                return StreamingResponse(
                    incomplete_stream_relay(), media_type="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "X-Request-ID": rid,
                             "X-OBSESSIONAPI-Completion-State": "incomplete"},
                )
            if pre_output_error and not prefetch_has_output:
                attempt_diagnostic = (
                    f"{attempt_diagnostic};" + _response_structure_diagnostic(
                        prefetch_last_event,
                        resp.headers.get("content-type", ""),
                        "anthropic" if endpoint.endswith("/messages") else "",
                    )
                )
                await resp.aclose()
                await client.aclose()
                if "tools" not in outbound_body:
                    fallback_result, fallback_code, fallback_message = await _fallback_completion(
                        ch, url, outbound_body, headers
                    )
                    if fallback_result is not None:
                        return StreamingResponse(nonstream_relay(fallback_result), media_type="text/event-stream",
                                                 headers={"X-Accel-Buffering": "no", "X-Request-ID": rid})
                    last_error = fallback_message
                    pt, ct, usage_source = _usage_or_estimate(prefetch_usage, None, None)
                    finalize(ch, pt, ct, "error", fallback_code, last_error, has_next_channel,
                             usage_source=usage_source)
                else:
                    last_error = pre_output_error
                    pt, ct, usage_source = _usage_or_estimate(prefetch_usage, None, None)
                    finalize(ch, pt, ct, "error", 502, last_error, has_next_channel,
                             usage_source=usage_source)
                if has_next_channel:
                    continue
                return _error(502, last_error, "server_error", request_id=rid)

            async def relay(ch=ch, client=client, resp=resp, prefetched=prefetched, stream_lines=stream_lines):
                ct_chars = {"n": 0}
                real_usage = {"pt": 0, "ct": 0}
                usage_seen = {"v": False}
                tail_filter = _SeekAITailFilter()
                last_content_event = None
                last_output_event = None
                output_started = False
                stream_error = None
                stream_error_code = 502
                tool_capability_error = False
                stream_error_sent = False
                finish_reason_seen = False
                upstream_done = False
                done_sent = False
                try:
                    async def lines():
                        for line in prefetched:
                            yield line
                        if prefetch_error:
                            raise prefetch_error
                        async for line in stream_lines:
                            yield line

                    try:
                        async for line in lines():
                            if not line.startswith("data: "):
                                continue
                            payload = line[6:]
                            if payload.strip() == "[DONE]":
                                upstream_done = True
                                break
                            try:
                                obj = json.loads(payload)
                                if obj.get("usage"):
                                    if obj["usage"].get("prompt_tokens") is not None:
                                        real_usage["pt"] = obj["usage"]["prompt_tokens"]
                                        usage_seen["v"] = True
                                    if obj["usage"].get("completion_tokens") is not None:
                                        real_usage["ct"] = obj["usage"]["completion_tokens"]
                                        usage_seen["v"] = True
                                choices = obj.get("choices") or []
                                finish_reason = (
                                    choices[0].get("finish_reason")
                                    if choices and isinstance(choices[0], dict) else None
                                )
                                if finish_reason:
                                    finish_reason_seen = True
                                content = _stream_content(obj)
                                if _stream_has_output(obj):
                                    output_started = True
                                    last_output_event = obj
                                if content:
                                    last_content_event = obj
                            except Exception:
                                obj, content, finish_reason = None, None, None
                            if obj and obj.get("error"):
                                error = obj["error"] if isinstance(obj["error"], dict) else {}
                                error_text = error.get("message", "流式上游返回错误")
                                if _tools_unsupported(json.dumps(obj, ensure_ascii=False), outbound_body):
                                    tool_capability_error = True
                                    stream_error_code = 400
                                    stream_error = _tool_not_supported_message(ch["name"])
                                else:
                                    stream_error = upstream_error_message(
                                        ch["name"], 502, error_text, ch["api_key"]
                                    )
                                stream_error_sent = True
                                yield ("data: " + json.dumps({
                                    "error": {
                                        "message": stream_error,
                                        "type": "tool_not_supported" if tool_capability_error else "server_error",
                                        "code": stream_error_code,
                                    }
                                }, ensure_ascii=False) + "\n\n").encode()
                                continue
                            if obj is None or content is None:
                                yield (line + "\n\n").encode()
                                continue
                            replacement = tail_filter.feed(content)
                            if not replacement:
                                if finish_reason:
                                    obj["choices"][0]["delta"] = {}
                                    yield ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
                                continue
                            ct_chars["n"] += len(replacement)
                            obj["choices"][0]["delta"]["content"] = replacement
                            yield ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
                    except Exception as exc:  # noqa: BLE001
                        stream_error = upstream_error_message(
                            ch["name"], 502, _request_error_detail(exc), ch["api_key"]
                        )
                        if not stream_error_sent:
                            yield ("data: " + json.dumps({
                                "error": {"message": stream_error, "type": "server_error", "code": 502}
                            }, ensure_ascii=False) + "\n\n").encode()
                            stream_error_sent = True
                    if not upstream_done and not stream_error:
                        stream_error = upstream_error_message(
                            ch["name"], 502, "流式响应异常结束", ch["api_key"]
                        )
                        if not stream_error_sent:
                            yield ("data: " + json.dumps({
                                "error": {"message": stream_error, "type": "server_error", "code": 502}
                            }, ensure_ascii=False) + "\n\n").encode()
                            stream_error_sent = True
                    tail = tail_filter.finish()
                    if tail and last_content_event:
                        tail_event = json.loads(json.dumps(last_content_event))
                        tail_event["choices"][0]["delta"]["content"] = tail
                        ct_chars["n"] += len(tail)
                        yield ("data: " + json.dumps(tail_event, ensure_ascii=False) + "\n\n").encode()
                    if upstream_done and not stream_error and output_started and not finish_reason_seen:
                        if last_content_event or last_output_event:
                            finish_event = json.loads(json.dumps(last_content_event or last_output_event))
                        else:
                            finish_event = {
                                "id": "",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": model,
                            }
                        finish_event["choices"] = [{
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }]
                        finish_event.pop("usage", None)
                        yield ("data: " + json.dumps(finish_event, ensure_ascii=False) + "\n\n").encode()
                    if not done_sent:
                        yield b"data: [DONE]\n\n"
                        done_sent = True
                finally:
                    await resp.aclose()
                    await client.aclose()
                    # 上游流式响应通常不带 usage，缺省时按内容长度估算，保证用量记录不断档
                    if usage_seen["v"]:
                        usage_pt, usage_ct, usage_source = real_usage["pt"], real_usage["ct"], "upstream"
                    else:
                        usage_pt, usage_ct, usage_source = pt_est, _est_tokens(ct_chars["n"]), "estimated"
                    if stream_error:
                        finalize(ch, usage_pt, usage_ct, "error", stream_error_code,
                                 stream_error, breaker_failure=not tool_capability_error,
                                 usage_source=usage_source)
                    else:
                        finalize(ch, usage_pt, usage_ct, "success", 200, usage_source=usage_source)
            return StreamingResponse(relay(), media_type="text/event-stream",
                                     headers={"X-Accel-Buffering": "no", "X-Request-ID": rid})

        # 非流式
        try:
            async with _http_client(ch) as client:
                resp = await client.post(url, json=outbound_body, headers=headers)
        except Exception as exc:  # noqa: BLE001
            last_error = upstream_error_message(
                ch["name"], 502, _request_error_detail(exc), ch["api_key"]
            )
            finalize(ch, 0, 0, "error", 502, last_error, has_next_channel)
            continue
        if resp.status_code != 200:
            if _tools_unsupported(resp.text, outbound_body):
                last_error = _tool_not_supported_message(ch["name"])
                finalize(ch, 0, 0, "error", 400, last_error, has_next_channel, breaker_failure=False)
                if has_next_channel:
                    continue
                return _error(400, last_error, "tool_not_supported", request_id=rid)
            last_error = upstream_error_message(
                ch["name"], resp.status_code, resp.text, ch["api_key"],
                resp.headers.get("content-type", ""),
            )
            will_retry = has_next_channel and _retry_allowed(resp.status_code, last_error)
            finalize(ch, 0, 0, "error", resp.status_code, last_error, will_retry,
                     retry_after_ms=_parse_retry_after_ms(resp.headers))
            if will_retry:
                await _retry_pause(attempt_index)
                continue
            return _error(502, last_error, "client_restricted" if _is_client_restricted(resp.text) else "server_error", request_id=rid)
        try:
            result = resp.json()
        except (TypeError, ValueError, json.JSONDecodeError):
            last_error = _protocol_error_message(
                ch["name"], resp.text[:300], ch["api_key"], resp.headers.get("content-type", "")
            )
            finalize(ch, 0, 0, "error", 502, last_error, has_next_channel)
            if has_next_channel:
                continue
            return _error(502, last_error, "server_error", request_id=rid)
        attempt_diagnostic = (
            f"{attempt_diagnostic};" + _response_structure_diagnostic(
                result,
                resp.headers.get("content-type", ""),
                "anthropic" if endpoint.endswith("/messages") else "",
            )
        )
        response_state = _chat_response_state(result)
        if response_state == "incomplete":
            usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            last_error = _incomplete_response_detail(result)
            pt, ct, usage_source = _usage_or_estimate(usage, None, None)
            finalize(ch, pt, ct, "error", 200, last_error, False,
                     breaker_failure=False, usage_source=usage_source)
            return JSONResponse(
                result, status_code=200,
                headers={"X-Request-ID": rid, "X-OBSESSIONAPI-Completion-State": "incomplete"},
            )
        if response_state != "valid":
            last_error = _protocol_error_message(
                ch["name"], _invalid_response_detail(result), ch["api_key"]
            )
            usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            pt, ct, usage_source = _usage_or_estimate(usage, None, None)
            finalize(ch, pt, ct, "error", 502, last_error, has_next_channel,
                     usage_source=usage_source)
            if has_next_channel:
                continue
            return _error(502, last_error, "server_error", request_id=rid)
        usage = result.get("usage") or {}
        try:
            message = ((result.get("choices") or [{}])[0].get("message") or {})
            if isinstance(message.get("content"), str):
                message["content"] = strip_seekai_attribution(message["content"])
            reply_text = str(message.get("content") or "")
        except Exception:
            reply_text = ""
        pt, ct, usage_source = _usage_or_estimate(
            usage, pt_est, _est_tokens(len(reply_text)))
        finalize(ch, pt, ct, "success", 200, usage_source=usage_source, result=result)
        return JSONResponse(result, headers={"X-Request-ID": rid})

    return _error(502, last_error or "所有可用渠道均调用失败，请到控制台查看请求日志或测试渠道连通性。", "server_error", request_id=rid)


async def _chat(request: Request, body: dict, endpoint: str = "/v1/chat/completions", request_id: str = ""):
    """网关入口：非流式请求额外做 Key 级并发准入与释放，转发逻辑在 _chat_inner。"""
    rid = request_id or secrets.token_hex(8)
    if not isinstance(body, dict) or body.get("stream"):
        return await _chat_inner(request, body, endpoint, rid)
    key_row = _auth_key(request)
    limit = _int_field(key_row, "concurrency_limit") if key_row is not None else 0
    if limit <= 0:
        return await _chat_inner(request, body, endpoint, rid)
    if _key_concurrency_exceeded(key_row["id"], limit):
        response = _error(429, f"该 API Key 并发请求数已达上限（{limit}）。", "concurrency_limit_exceeded", request_id=rid)
        response.headers["Retry-After"] = "1"
        return response
    _key_acquire(key_row["id"])
    try:
        return await _chat_inner(request, body, endpoint, rid)
    finally:
        _key_release(key_row["id"])


# ------------------------------------------------------------------ embeddings
EMBEDDING_MOCK_DIM = 16


def _embedding_inputs(body: dict) -> list[str]:
    value = body.get("input")
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _est_embedding_tokens(body: dict) -> int:
    return max(1, len(" ".join(_embedding_inputs(body))) // 3) + 8


def _validate_embedding_request(body: dict) -> str:
    """embeddings 请求体的最小校验，避免非法入参打到上游或触发 500。"""
    model = body.get("model")
    if model is not None and not isinstance(model, str):
        return "请求参数 model 必须是字符串。"
    if "input" not in body:
        return "请求参数 input 不能为空。"
    value = body["input"]
    if isinstance(value, str):
        return ""
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return ""
    return "请求参数 input 必须是字符串或非空字符串数组。"


def _mock_embedding(body: dict, model: str) -> dict:
    """本地 mock 上游：按输入文本生成确定性向量，便于离线验证整条链路。"""
    data = []
    for index, text in enumerate(_embedding_inputs(body)):
        seed = hashlib.sha256(f"{model}:{text}".encode("utf-8")).digest()
        vector = [round((seed[i % len(seed)] / 255) * 2 - 1, 6) for i in range(EMBEDDING_MOCK_DIM)]
        data.append({"object": "embedding", "index": index, "embedding": vector})
    tokens = _est_embedding_tokens(body)
    return {"object": "list", "data": data, "model": model,
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens}}


async def _embeddings(request: Request, body: dict, endpoint: str = "/v1/embeddings", request_id: str = ""):
    """OpenAI 兼容 embeddings 转发：复用鉴权、白名单、限流、熔断与用量记录。"""
    start = time.perf_counter()
    ip = request.client.host if request.client else ""
    rid = request_id or secrets.token_hex(8)

    if not isinstance(body, dict):
        return _error(400, "请求体顶层必须是 JSON 对象。", "invalid_request_error", request_id=rid)
    invalid = _validate_embedding_request(body)
    if invalid:
        return _error(400, invalid, "invalid_request_error", request_id=rid)

    model = body.get("model", "")
    if not isinstance(model, str):
        model = ""

    key_row, error = _authorize_request(request, model, rid)
    if error is not None:
        return error
    candidates, error = _resolve_candidates(model, rid)
    if error is not None:
        return error

    pt_est = _est_embedding_tokens(body)
    last_error = ""

    def finalize(ch, pt, status, code, err="", will_retry=False, breaker_failure=True,
                 usage_source="unknown", retry_after_ms=0):
        input_price, _output_price = _pricing(model)
        cost = (pt if isinstance(pt, (int, float)) else 0) * input_price / 1_000_000
        latency = int((time.perf_counter() - start) * 1000)
        if status != "success" and err:
            err = (f"{err}；request_id={rid}；public_model={model}；"
                   f"channel={ch['name'] if ch else 'gateway'}；stage=upstream_request；"
                   f"gateway_status={400 if code == 400 else 502}")
        _record(rid, key_row, ch, model, endpoint, pt, 0, cost, latency, status, code, err, ip,
                will_retry, usage_source)
        if status == "success":
            _breaker_mark(ch["id"], True)
        elif breaker_failure:
            _breaker_mark(ch["id"], False, reason=classify_upstream_error(code, err),
                          retry_after_ms=retry_after_ms)

    for attempt_index, ch in enumerate(candidates):
        if attempt_index:
            try:
                disconnected = await request.is_disconnected()
            except Exception:  # noqa: BLE001
                disconnected = False
            if disconnected:
                return _error(499, "客户端已断开连接，停止重试。", "client_disconnected", request_id=rid)
        has_next_channel = attempt_index + 1 < len(candidates)
        _channel_rate_consume(ch["id"], _int_field(ch, "rpm_limit"))
        protocol = channel_protocol(ch)
        outbound_body = dict(body)
        outbound_body["model"] = upstream_model_for_channel(ch, model)

        if protocol == "mock":
            result = _mock_embedding(outbound_body, model)
            finalize(ch, pt_est, "success", 200, usage_source="synthetic")
            return JSONResponse(result, headers={"X-Request-ID": rid})

        if protocol not in {"openai_chat", "openai_responses"}:
            last_error = f"渠道 {ch['name']} 的协议 {protocol} 暂不支持 embeddings，请改用 OpenAI 兼容渠道"
            finalize(ch, 0, "error", 400, last_error, False, breaker_failure=False)
            return _error(400, last_error, "unsupported_protocol", request_id=rid)

        try:
            url = normalize_openai_base_url(ch["base_url"]) + "/embeddings"
        except ValueError as exc:
            last_error = upstream_error_message(ch["name"], 400, str(exc), ch["api_key"])
            finalize(ch, 0, "error", 400, last_error, False, breaker_failure=False)
            return _error(400, last_error, "invalid_request_error", request_id=rid)
        headers = build_channel_headers(ch, {"Authorization": f"Bearer {ch['api_key']}"}, request, "openai_chat")
        try:
            async with _http_client(ch) as client:
                resp = await client.post(url, json=outbound_body, headers=headers)
        except Exception as exc:  # noqa: BLE001
            last_error = upstream_error_message(ch["name"], 502, _request_error_detail(exc), ch["api_key"])
            finalize(ch, 0, "error", 502, last_error, has_next_channel)
            if has_next_channel:
                await _retry_pause(attempt_index)
                continue
            return _error(502, last_error, "server_error", request_id=rid)

        if resp.status_code != 200:
            last_error = upstream_error_message(ch["name"], resp.status_code, resp.text[:300],
                                                ch["api_key"], resp.headers.get("content-type", ""))
            will_retry = has_next_channel and _retry_allowed(resp.status_code, last_error)
            finalize(ch, 0, "error", resp.status_code, last_error, will_retry,
                     retry_after_ms=_parse_retry_after_ms(resp.headers))
            if will_retry:
                await _retry_pause(attempt_index)
                continue
            return _error(502, last_error, "server_error", request_id=rid)

        try:
            result = resp.json()
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise ValueError("响应缺少 data 数组")
        except (TypeError, ValueError, json.JSONDecodeError):
            last_error = upstream_error_message(ch["name"], 502, "上游 embeddings 返回格式不兼容", ch["api_key"])
            finalize(ch, 0, "error", 502, last_error, has_next_channel)
            if has_next_channel:
                continue
            return _error(502, last_error, "server_error", request_id=rid)

        usage = result.get("usage") or {}
        pt, _ct, usage_source = _usage_or_estimate(usage, pt_est, 0)
        finalize(ch, pt, "success", 200, usage_source=usage_source)
        return JSONResponse(result, headers={"X-Request-ID": rid})

    return _error(502, last_error or "所有可用渠道均调用失败，请到控制台查看请求日志或测试渠道连通性。",
                  "server_error", request_id=rid)


# ------------------------------------------------------------------ 端点
def _native_stream_lines(protocol: str, payload: dict, state: dict) -> list[bytes]:
    if not isinstance(payload, dict):
        return []
    choices = payload.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
    finish = choice.get("finish_reason")
    output = []
    if protocol == "anthropic":
        def event(name: str, data: dict) -> bytes:
            return (f"event: {name}\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode()

        def fail(message: str) -> None:
            state["errored"] = True
            state["stopped"] = True
            output.append(event("error", {
                "type": "error",
                "error": {"type": "protocol_error", "message": message},
            }))

        if isinstance(payload.get("error"), dict):
            error = payload["error"]
            state["errored"] = True
            state["stopped"] = True
            output.append(event("error", {
                "type": "error",
                "error": {
                    "type": error.get("type") or "server_error",
                    "message": error.get("message") or "上游流式响应错误",
                },
            }))
            return output

        def close_blocks() -> None:
            text_index = state.get("text_block_index")
            if text_index is not None:
                output.append(event("content_block_stop", {
                    "type": "content_block_stop", "index": text_index,
                }))
                state["text_block_index"] = None
            for block in state.get("tool_blocks", {}).values():
                if block.get("open"):
                    output.append(event("content_block_stop", {
                        "type": "content_block_stop", "index": block["index"],
                    }))
                    block["open"] = False

        if not state.get("started") and (choices or delta or finish is not None):
            state["started"] = True
            state.setdefault("next_block_index", 0)
            output.append(event("message_start", {"type": "message_start", "message": {
                "id": payload.get("id", ""), "type": "message", "role": "assistant", "content": [], "model": payload.get("model", "")
            }}))
        content = delta.get("content")
        if content is not None and not isinstance(content, str):
            fail("OpenAI 流式 content 不是字符串，无法转换为 Anthropic text_delta")
            return output

        def emit_text(text: str) -> None:
            if not text:
                return
            if state.get("text_block_index") is None:
                index = state.get("next_block_index", 0)
                state["next_block_index"] = index + 1
                state["text_block_index"] = index
                output.append(event("content_block_start", {
                    "type": "content_block_start", "index": index,
                    "content_block": {"type": "text", "text": ""},
                }))
            output.append(event("content_block_delta", {"type": "content_block_delta",
                "index": state["text_block_index"],
                "delta": {"type": "text_delta", "text": text}}))
            state["has_output"] = True

        if isinstance(content, str):
            emit_text(content)
        refusal = delta.get("refusal")
        if refusal is not None and not isinstance(refusal, str):
            fail("OpenAI 流式 refusal 不是字符串，无法转换为 Anthropic text_delta")
            return output
        if isinstance(refusal, str):
            emit_text(refusal)
        tool_blocks = state.setdefault("tool_blocks", {})
        for call_position, call in enumerate(delta.get("tool_calls") or []):
            if not isinstance(call, dict):
                fail("工具调用流分片不是对象")
                return output
            source_index = call.get("index", call_position)
            key = str(source_index)
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            block = tool_blocks.get(key)
            if block is None and fn.get("name"):
                name = fn.get("name")
                call_id = call.get("id")
                if not isinstance(name, str) or not name.strip() or not isinstance(call_id, str) or not call_id.strip():
                    fail("工具调用缺少有效 id 或 name")
                    return output
                index = state.get("next_block_index", 0)
                state["next_block_index"] = index + 1
                block = {"index": index, "open": True}
                tool_blocks[key] = block
                state["has_output"] = True
                output.append(event("content_block_start", {"type": "content_block_start", "index": index,
                    "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}}}))
            arguments = fn.get("arguments")
            if arguments is not None and not isinstance(arguments, str):
                fail("工具参数流分片不是字符串")
                return output
            if arguments:
                if block is None or not block.get("open"):
                    fail("工具参数在 content_block_start 之前到达")
                    return output
                state["has_output"] = True
                output.append(event("content_block_delta", {"type": "content_block_delta", "index": block["index"],
                    "delta": {"type": "input_json_delta", "partial_json": arguments}}))
        if finish and not state.get("stopped"):
            reason = {
                "stop": "end_turn",
                "length": "max_tokens",
                "max_tokens": "max_tokens",
                "tool_calls": "tool_use",
                "function_call": "tool_use",
                "stop_sequence": "stop_sequence",
            }.get(finish)
            if reason is None:
                fail(f"OpenAI finish_reason={finish} 无等价 Anthropic stop_reason")
                return output
            if finish in {"stop", "stop_sequence", "tool_calls", "function_call"} and not state.get("has_output"):
                fail(f"OpenAI finish_reason={finish} 但没有可用文本或工具调用")
                return output
            close_blocks()
            message_delta = {"type": "message_delta", "delta": {"stop_reason": reason}}
            usage = payload.get("usage")
            if isinstance(usage, dict) and usage.get("completion_tokens") is not None:
                message_delta["usage"] = {"output_tokens": usage["completion_tokens"]}
            output.append(event("message_delta", message_delta))
            output.append(event("message_stop", {"type": "message_stop"}))
            state["stopped"] = True
        return output
    if state.get("stopped"):
        return output

    def emit(data: dict) -> None:
        prefix = ""
        if protocol == "openai_responses":
            data["sequence_number"] = state.get("sequence", 0)
            state["sequence"] = data["sequence_number"] + 1
            prefix = f"event: {data['type']}\n"
        output.append((prefix + "data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode())

    def fail_native(message: str, kind: str = "protocol_error") -> None:
        state["stopped"] = state["errored"] = True
        error = {"type": kind, "message": message}
        if protocol == "openai_responses":
            response = dict(state.get("response") or {}, status="failed", error=error)
            emit({"type": "response.failed", "response": response, "error": error})
        else:
            emit({"error": {"code": 502, "status": "INTERNAL", **error}})

    if isinstance(payload.get("error"), dict):
        error = payload["error"]
        fail_native(str(error.get("message") or "上游流式响应错误"), str(error.get("type") or "server_error"))
        return output
    if payload.get("usage"):
        state["usage"] = payload["usage"]
    if protocol == "openai_responses" and not state.get("created"):
        state["created"] = True
        state["items"] = []
        state["response"] = {"id": payload.get("id") or "resp_gateway", "object": "response",
            "created_at": payload.get("created") or int(time.time()), "model": payload.get("model", ""),
            "status": "in_progress", "error": None, "incomplete_details": None, "output": []}
        emit({"type": "response.created", "response": dict(state["response"])})

    content = delta.get("content")
    if content is not None and not isinstance(content, str):
        fail_native("流式 content 必须是字符串")
        return output
    if any(delta.get(key) for key in ("function_call", "refusal", "audio")):
        fail_native("当前协议流转换不支持 function_call、refusal 或 audio 字段")
        return output
    if content:
        state["has_output"] = True
        if protocol == "openai_responses":
            if "text_index" not in state:
                state["text_index"] = len(state["items"])
                item = {"id": "msg_" + state["response"]["id"], "type": "message", "role": "assistant",
                    "status": "in_progress", "content": []}
                state["items"].append(item)
                emit({"type": "response.output_item.added", "output_index": state["text_index"], "item": dict(item)})
                emit({"type": "response.content_part.added", "item_id": item["id"],
                    "output_index": state["text_index"], "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []}})
            item = state["items"][state["text_index"]]
            state["text"] = state.get("text", "") + content
            emit({"type": "response.output_text.delta", "item_id": item["id"],
                "output_index": state["text_index"], "content_index": 0, "delta": content})
        else:
            emit({"candidates": [{"index": 0, "content": {"role": "model", "parts": [{"text": content}]}}]})
    tools = state.setdefault("native_tools", {})
    for position, call in enumerate(delta.get("tool_calls") or []):
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            fail_native("流式工具调用必须包含 function 对象")
            return output
        key = call.get("index", position)
        if not isinstance(key, int) or key < 0:
            fail_native("流式工具调用 index 无效")
            return output
        fn = call["function"]
        if any(fn.get(field) is not None and not isinstance(fn[field], str) for field in ("name", "arguments")):
            fail_native("流式工具名称和参数必须是字符串")
            return output
        tool = tools.get(key)
        if tool is None:
            if not isinstance(call.get("id"), str) or not call["id"] or not fn.get("name"):
                fail_native("工具调用在参数增量之前缺少 id 或 name")
                return output
            tool = {"id": "fc_" + call["id"], "type": "function_call", "call_id": call["id"],
                "name": fn["name"], "arguments": "", "status": "in_progress"}
            tools[key] = tool
            if protocol == "openai_responses":
                tool["output_index"] = len(state["items"])
                state["items"].append(tool)
                emit({"type": "response.output_item.added", "output_index": tool["output_index"],
                    "item": {k: v for k, v in tool.items() if k != "output_index"}})
        elif fn.get("name"):
            tool["name"] += fn["name"]
        args = fn.get("arguments") or ""
        tool["arguments"] += args
        state["has_output"] = True
        if args and protocol == "openai_responses":
            emit({"type": "response.function_call_arguments.delta", "item_id": tool["id"],
                "output_index": tool["output_index"], "delta": args})
    if finish:
        if finish not in {"stop", "length", "tool_calls", "function_call"}:
            fail_native(f"无法转换 finish_reason={finish}")
            return output
        if not state.get("has_output") and finish != "length":
            fail_native("流式响应结束但没有有效文本或工具调用")
            return output
        parts = []
        for tool in tools.values():
            try:
                arguments = json.loads(tool["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError("工具参数必须是 JSON 对象")
            except (TypeError, ValueError, json.JSONDecodeError):
                # Length-limited Responses preserve partial arguments as incomplete output.
                if not (protocol == "openai_responses" and finish == "length"):
                    fail_native("工具 arguments 不是合法 JSON 对象")
                    return output
                arguments = None
            parts.append({"functionCall": {"id": tool["call_id"], "name": tool["name"], "args": arguments}})
        usage = state.get("usage") or {}
        if protocol == "openai_responses":
            status = "incomplete" if finish == "length" else "completed"
            for index, item in enumerate(state["items"]):
                item["status"] = status
                if item["type"] == "message":
                    part = {"type": "output_text", "text": state.get("text", ""), "annotations": []}
                    item["content"] = [part]
                    emit({"type": "response.output_text.done", "item_id": item["id"],
                        "output_index": index, "content_index": 0, "text": part["text"]})
                    emit({"type": "response.content_part.done", "item_id": item["id"],
                        "output_index": index, "content_index": 0, "part": part})
                else:
                    emit({"type": "response.function_call_arguments.done", "item_id": item["id"],
                        "output_index": index, "arguments": item["arguments"], "name": item["name"]})
                emit({"type": "response.output_item.done", "output_index": index,
                    "item": {k: v for k, v in item.items() if k != "output_index"}})
            response = dict(state["response"], status=status,
                output=[{k: v for k, v in item.items() if k != "output_index"} for item in state["items"]],
                incomplete_details={"reason": "max_output_tokens"} if finish == "length" else None)
            if usage:
                response["usage"] = {"input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0)}
            emit({"type": "response." + status, "response": response})
        else:
            result = {"candidates": [{"index": 0, "content": {"role": "model", "parts": parts},
                "finishReason": "MAX_TOKENS" if finish == "length" else "STOP"}]}
            if usage:
                result["usageMetadata"] = {"promptTokenCount": usage.get("prompt_tokens", 0),
                    "candidatesTokenCount": usage.get("completion_tokens", 0),
                    "totalTokenCount": usage.get("total_tokens", 0)}
            emit(result)
        state["stopped"] = True
    return output


async def _as_native_response(response, protocol: str, rid: str):
    if isinstance(response, StreamingResponse):
        async def relay():
            state = {}
            done_marker = False
            pending_finish = None
            buffer = b""
            data_lines = []

            def parse_frame(data):
                nonlocal done_marker, pending_finish
                if done_marker or state.get("stopped"):
                    return []
                if data.strip() == b"[DONE]":
                    done_marker = True
                    if pending_finish is not None:
                        if state.get("usage"):
                            pending_finish["usage"] = state["usage"]
                        return _native_stream_lines(protocol, pending_finish, state)
                    return []
                try:
                    payload = json.loads(data)
                    if not isinstance(payload, dict):
                        raise ValueError("SSE payload 必须是对象")
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = {"error": {"type": "protocol_error", "message": "上游 SSE data 不是合法 JSON 对象"}}
                if protocol != "anthropic" and not payload.get("error"):
                    if payload.get("usage"):
                        state["usage"] = payload["usage"]
                    choices = payload.get("choices") or []
                    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
                    if choice.get("finish_reason"):
                        pending_finish = {**payload, "choices": [{**choice, "delta": {}}]}
                        payload = {**payload, "choices": [{**choice, "finish_reason": None}]}
                return _native_stream_lines(protocol, payload, state)

            try:
                async for raw in response.body_iterator:
                    buffer += raw if isinstance(raw, bytes) else str(raw).encode()
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.rstrip(b"\r")
                        if not line:
                            if data_lines:
                                for native in parse_frame(b"\n".join(data_lines)):
                                    yield native
                                data_lines.clear()
                        elif line.startswith(b"data:"):
                            data_lines.append(line[5:].lstrip(b" "))
                if not state.get("stopped") and not buffer and not data_lines and pending_finish is not None:
                    if state.get("usage"):
                        pending_finish["usage"] = state["usage"]
                    for native in _native_stream_lines(protocol, pending_finish, state):
                        yield native
            except Exception:
                if not state.get("stopped"):
                    for native in _native_stream_lines(protocol, {
                        "error": {"type": "stream_error", "message": "上游流式读取异常"}
                    }, state):
                        yield native
            finally:
                close = getattr(response.body_iterator, "aclose", None)
                if close is not None:
                    await close()
            if not state.get("stopped"):
                for native in _native_stream_lines(protocol, {
                    "error": {"type": "stream_error", "message": (
                        "上游在 [DONE] 前缺少 finish_reason" if done_marker
                        else "流式响应在完成事件前结束"
                    )}
                }, state):
                    yield native
        return StreamingResponse(relay(), media_type="text/event-stream",
                                 headers={"X-Request-ID": rid, "X-Accel-Buffering": "no"})
    if response.status_code >= 400:
        return response
    try:
        payload = json.loads(response.body.decode())
    except (TypeError, ValueError, json.JSONDecodeError):
        return response
    try:
        if protocol == "anthropic":
            native = openai_to_anthropic_response(payload)
        elif protocol == "openai_responses":
            native = openai_to_responses_response(payload)
        else:
            native = openai_to_gemini_response(payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return _error(502, f"协议转换错误：{str(exc)[:200]}", "protocol_conversion_error", request_id=rid)
    return JSONResponse(native, status_code=response.status_code, headers={"X-Request-ID": rid})


async def _external_protocol_call(request: Request, body: dict, protocol: str, endpoint: str, model: str = ""):
    rid = secrets.token_hex(8)
    try:
        if protocol == "anthropic":
            canonical = anthropic_to_openai_request(body)
        elif protocol == "openai_responses":
            canonical = responses_to_openai(body)
        else:
            canonical = gemini_request_to_openai(body, model)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return _error(400, str(exc), "invalid_request_error", request_id=rid)
    if model:
        canonical["model"] = model
    response = await _chat(request, canonical, endpoint, request_id=rid)
    return await _as_native_response(response, protocol, rid)


@router.get("/models")
async def list_models(request: Request):
    rid = secrets.token_hex(8)
    key_row = _auth_key(request)
    if key_row is not None and (key_row["status"] != 1 or key_row["user_status"] != 1):
        return _error(403, "该 API Key 或所属用户已被禁用。", "permission_denied", request_id=rid)
    if key_row is None and _settings().get("models_require_key"):
        return _error(401, "网关 API Key 无效，请在管理后台「API Key 管理」中创建。", "invalid_api_key", request_id=rid)
    rows = query(
        """SELECT m.model_name, m.display_name FROM models m
           WHERE m.status=1 AND EXISTS (
             SELECT 1 FROM channels c WHERE c.status=1
               AND instr(','||replace(c.models,' ','')||',', ','||m.model_name||',') > 0
           ) ORDER BY m.model_name"""
    )
    allowed = key_allowed_models(key_row) if key_row is not None else set()
    data = [
        {"id": r["model_name"], "object": "model", "owned_by": "obsession-api"}
        for r in rows if not allowed or r["model_name"] in allowed
    ]
    return JSONResponse({"object": "list", "data": data}, headers={"X-Request-ID": rid})


def _duplicate_v1_response(response, path: str):
    response.headers["X-OBSESSIONAPI-Path-Warning"] = "duplicated-v1"
    logger.warning("客户端请求了重复 /v1 路径，建议修正 Base URL：%s", path)
    return response


@router.get("/v1/models")
async def list_models_duplicate(request: Request):
    return _duplicate_v1_response(await list_models(request), request.url.path)


@router.post("/chat/completions")
async def chat_completions(request: Request):
    rid = secrets.token_hex(8)
    body, error = await _request_json_object(request, rid)
    if error:
        return error
    return await _chat(request, body, "/v1/chat/completions", request_id=rid)


@router.post("/v1/chat/completions")
async def chat_completions_duplicate(request: Request):
    return _duplicate_v1_response(await chat_completions(request), request.url.path)


@router.post("/completions")
async def completions(request: Request):
    rid = secrets.token_hex(8)
    body, error = await _request_json_object(request, rid)
    if error:
        return error
    prompt = body.pop("prompt", None)
    if prompt is not None:
        body["messages"] = [{"role": "user", "content": prompt}]
    else:
        body.setdefault("messages", [{"role": "user", "content": ""}])
    return await _chat(request, body, "/v1/completions", request_id=rid)


@router.post("/embeddings")
async def embeddings(request: Request):
    rid = secrets.token_hex(8)
    body, error = await _request_json_object(request, rid)
    if error:
        return error
    return await _embeddings(request, body, "/v1/embeddings", request_id=rid)


@router.post("/v1/embeddings")
async def embeddings_duplicate(request: Request):
    return _duplicate_v1_response(await embeddings(request), request.url.path)


@external_router.post("/v1/messages")
async def anthropic_messages(request: Request):
    rid = secrets.token_hex(8)
    body, error = await _request_json_object(request, rid)
    if error:
        return error
    return await _external_protocol_call(request, body, "anthropic", "/v1/messages")


@external_router.post("/v1/v1/messages")
async def anthropic_messages_duplicate(request: Request):
    return _duplicate_v1_response(await anthropic_messages(request), request.url.path)


@external_router.post("/v1/responses")
async def openai_responses(request: Request):
    rid = secrets.token_hex(8)
    body, error = await _request_json_object(request, rid)
    if error:
        return error
    return await _external_protocol_call(request, body, "openai_responses", "/v1/responses")


@external_router.post("/v1/v1/responses")
async def openai_responses_duplicate(request: Request):
    return _duplicate_v1_response(await openai_responses(request), request.url.path)


@external_router.post("/v1beta/models/{model}:generateContent")
async def gemini_generate_content(model: str, request: Request):
    rid = secrets.token_hex(8)
    body, error = await _request_json_object(request, rid)
    if error:
        return error
    return await _external_protocol_call(request, body, "gemini", "/v1beta/models/{model}:generateContent", model)


@external_router.post("/v1beta/models/{model}:streamGenerateContent")
async def gemini_stream_generate_content(model: str, request: Request):
    rid = secrets.token_hex(8)
    body, error = await _request_json_object(request, rid)
    if error:
        return error
    body["stream"] = True
    return await _external_protocol_call(request, body, "gemini", "/v1beta/models/{model}:streamGenerateContent", model)
