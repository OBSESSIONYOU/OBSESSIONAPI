"""Account credentials and fixed inference routes; never send stored JSON as a key."""
from __future__ import annotations

import json

from . import cline_access, gemini_access, lobsterai, minimax_access


ACCOUNT_TYPES = {"lobsterai", "access_gemini", "access_minimax", "access_cline"}


def parse_credential(raw) -> dict:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as exc:
        raise ValueError("账号凭据不是有效 JSON，请重新登录") from exc
    if not isinstance(value, dict) or not isinstance(value.get("access_token"), str) or not value["access_token"].strip():
        raise ValueError("账号凭据缺少 access_token，请重新登录")
    if any(mark in value["access_token"] for mark in ("\r", "\n")):
        raise ValueError("账号 access_token 包含非法字符")
    return dict(value)


def validate_request(family: str, body: dict) -> None:
    """Keep native conversions explicit: unsupported options must not disappear."""
    if body.get("n", 1) != 1:
        raise ValueError("账号渠道目前仅支持 n=1")
    if family not in {"access_gemini", "access_minimax"}:
        return
    supported = {"model", "messages", "stream", "temperature", "top_p", "max_tokens", "max_completion_tokens",
                 "stop", "tools", "tool_choice", "parallel_tool_calls", "stream_options", "user", "n"}
    unsupported = set(body) - supported
    if unsupported:
        raise ValueError("账号协议未实现参数映射：" + ", ".join(sorted(unsupported)))
    if family == "access_gemini" and body.get("parallel_tool_calls") is False:
        raise ValueError("Gemini Cloud Code 未实现 parallel_tool_calls=false 的等价语义")
    for name in ("max_tokens", "max_completion_tokens"):
        if name in body and (not isinstance(body[name], int) or isinstance(body[name], bool) or body[name] <= 0):
            raise ValueError(name + " 必须是正整数")
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            raise ValueError("messages 必须是对象数组")
        if message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError("账号协议不支持该消息角色")
        if set(message) - {"role", "content", "tool_calls", "tool_call_id"}:
            raise ValueError("账号协议存在未实现映射的消息字段")
        content = message.get("content")
        if content is not None and not isinstance(content, (str, list)):
            raise ValueError("账号协议消息 content 必须是文本或文本块")
        if isinstance(content, list) and any(not isinstance(part, dict) or part.get("type") not in {"text", "input_text", "output_text"}
                                             or not isinstance(part.get("text"), str) or set(part) - {"type", "text"} for part in content):
            raise ValueError("账号协议当前只支持文本消息及 function 工具")
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            raise ValueError("账号协议仅支持 function 工具")
        if tool["function"].get("strict"):
            raise ValueError("账号协议未实现 strict function 工具")
        if set(tool["function"]) - {"name", "description", "parameters", "strict"}:
            raise ValueError("账号协议存在未实现映射的 function 字段")
        if not isinstance(tool["function"].get("name"), str) or not tool["function"]["name"].strip():
            raise ValueError("function 工具缺少函数名")
    if "tool_choice" in body:
        choice = body["tool_choice"]
        if isinstance(choice, dict):
            function = choice.get("function")
            if choice.get("type") != "function" or not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"].strip():
                raise ValueError("账号协议不支持该 tool_choice")
        elif not isinstance(choice, str) or choice not in {"auto", "none", "required"}:
            raise ValueError("账号协议不支持该 tool_choice")
    # Run the actual mapper before opening a connection, so malformed tool history
    # is a caller error and cannot surface later as an upstream HTTP 502.
    if family == "access_gemini":
        from .protocols import openai_to_gemini
        openai_to_gemini(text_messages(body))
    else:
        anthropic_payload(body)


def text_messages(body: dict) -> dict:
    """Flatten validated text blocks, retaining system and output_text content."""
    normalized = dict(body)
    normalized["messages"] = [
        {**message, "content": "".join(part["text"] for part in message["content"])}
        if isinstance(message.get("content"), list) else dict(message)
        for message in body.get("messages") or []
    ]
    return normalized


async def prepare_channel(channel, body: dict | None = None, stream: bool = False) -> dict:
    """Return an ephemeral row with native token, protocol and fixed transport fields.

    ``_updated_credential`` is returned only after refresh. The caller persists that
    JSON, never the temporary ``api_key`` field used for an individual HTTP request.
    """
    prepared = dict(channel)
    family = str(prepared["type"])
    if family not in ACCOUNT_TYPES:
        raise ValueError("不是已实现的账号渠道")
    if body is not None:
        validate_request(family, body)
    credential = parse_credential(prepared.get("api_key"))
    original = dict(credential)
    module = {"lobsterai": lobsterai, "access_gemini": gemini_access, "access_minimax": minimax_access,
              "access_cline": cline_access}[family]
    if module.is_expired(credential):
        if family == "access_cline":
            raise ValueError("Cline 凭据已过期，请重新登录")
        # A failed refresh must not fall back to an expired token.
        try:
            credential = parse_credential(await module.refresh(credential))
        except Exception:
            raise ValueError("账号凭据刷新失败，请重新登录") from None
    token = credential["access_token"]
    if family == "access_cline" and not token.startswith("workos:"):
        token = "workos:" + token
    prepared.update(api_key=token, _account_family=family, _account_credential=credential)
    if credential != original:
        prepared["_updated_credential"] = credential
    if family == "lobsterai":
        prepared.update(type="openai_chat", base_url=lobsterai.API_BASE + "/api/proxy/v1",
                        _account_url=lobsterai.API_BASE + lobsterai.CHAT_PATH,
                        _account_headers=lobsterai.chat_headers(token, credential.get("version") or lobsterai.FALLBACK_VERSION, sse=True))
    elif family == "access_cline":
        prepared.update(type="openai_chat", base_url=cline_access.API_BASE + "/api/v1",
                        _account_url=cline_access.API_BASE + cline_access.CHAT_PATH,
                        _account_headers=cline_access.chat_headers({**credential, "access_token": token}))
        prepared["_account_headers"]["Accept"] = "text/event-stream"
    elif family == "access_minimax":
        prepared.update(type="anthropic", base_url=minimax_access.API_HOST + "/mavis/api/v1/llm",
                        _account_url=minimax_access.API_HOST + minimax_access.INFER_PATH,
                        _account_headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "text/event-stream"})
    else:
        prepared.update(type="access_gemini", base_url=gemini_access.ENDPOINTS[0],
                        _account_url=gemini_access.ENDPOINTS[0] + gemini_access.STREAM_PATH,
                        _account_headers=gemini_access._headers(credential),
                        _account_project=credential.get("project") or gemini_access.DEFAULT_PROJECT)
    return prepared


def anthropic_payload(body: dict) -> dict:
    """Use the established gateway tool/history mapper, with explicit choice options."""
    from .gateway import _openai_to_anthropic
    payload = _openai_to_anthropic(text_messages(body))
    if body.get("tool_choice") == "none":
        payload["tool_choice"] = {"type": "none"}
    if "parallel_tool_calls" in body:
        payload.setdefault("tool_choice", {"type": "auto"})["disable_parallel_tool_use"] = not body["parallel_tool_calls"]
    return payload


async def complete_nonstream(client, channel, body: dict, public_model: str):
    """Consume the native SSE-only lane, then return one validated Chat completion."""
    from .gateway import _anthropic_to_openai, _protocol_event
    from .opencode_free import collect_openai_chunks, closing_stream, complete_nonstream as collect_openai
    from .sse import iter_sse_json_lines

    family = channel["_account_family"]
    if family in {"lobsterai", "access_cline"}:
        return await collect_openai(client, channel["_account_url"], channel["_account_headers"], body)
    wire = dict(body)
    wire["stream"] = True
    if family == "access_gemini":
        url, payload = gemini_access.build_stream_request(wire, wire["model"], channel["_account_project"], public_model)
    else:
        url, payload = channel["_account_url"], anthropic_payload(wire)
    async with closing_stream(client, url, channel["_account_headers"], payload) as response:
        if response.status_code != 200:
            # The caller provides a generic redacted error; do not echo token-bearing bodies.
            await response.aread()
            return None, response.status_code, "账号上游拒绝请求"
        if family == "access_gemini":
            state, chunks = {}, []
            async for line in iter_sse_json_lines(response.aiter_lines()):
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                frame = json.loads(raw)
                frame = frame.get("response", frame)
                chunk, _, failed, detail = _protocol_event(frame, "gemini", state, public_model)
                if failed:
                    raise ValueError("Gemini Cloud Code 流式响应返回错误")
                if chunk:
                    chunks.append(json.loads(chunk[5:]))
            if not state.get("completed"):
                raise ValueError("Gemini Cloud Code 流式响应异常结束")
            return collect_openai_chunks(chunks, wire), 200, ""
        message = {"id": "", "model": public_model, "role": "assistant", "content": [], "usage": {}}
        blocks, arguments = {}, {}
        stopped = False
        async for line in iter_sse_json_lines(response.aiter_lines()):
            raw = line[5:].strip()
            if raw == "[DONE]":
                break
            frame = json.loads(raw)
            kind = frame.get("type")
            if kind == "error" or frame.get("error"):
                raise ValueError("MiniMax 流式响应返回错误")
            if kind == "message_start":
                initial = frame.get("message") or {}
                message.update(id=initial.get("id", ""), model=initial.get("model") or public_model,
                               usage=dict(initial.get("usage") or {}))
            elif kind == "content_block_start":
                index = frame.get("index")
                block = frame.get("content_block")
                if not isinstance(index, int) or not isinstance(block, dict) or block.get("type") not in {"text", "tool_use", "thinking", "redacted_thinking"}:
                    raise ValueError("MiniMax content block 格式不兼容")
                blocks[index] = dict(block)
            elif kind == "content_block_delta":
                index, delta = frame.get("index"), frame.get("delta") or {}
                block = blocks.get(index)
                if block is None:
                    raise ValueError("MiniMax delta 在 content_block_start 前到达")
                if delta.get("type") == "text_delta":
                    block["text"] = block.get("text", "") + str(delta.get("text", ""))
                elif delta.get("type") == "input_json_delta":
                    arguments[index] = arguments.get(index, "") + str(delta.get("partial_json", ""))
            elif kind == "message_delta":
                message["stop_reason"] = (frame.get("delta") or {}).get("stop_reason")
                message["usage"].update(frame.get("usage") or {})
            elif kind == "message_stop":
                stopped = True
        if not stopped or not message.get("stop_reason"):
            raise ValueError("MiniMax 流式响应异常结束")
        for index in sorted(blocks):
            block = blocks[index]
            if block.get("type") == "tool_use" and index in arguments:
                parsed = json.loads(arguments[index])
                if not isinstance(parsed, dict):
                    raise ValueError("MiniMax 工具参数必须是 JSON 对象")
                block["input"] = parsed
            message["content"].append(block)
        return _anthropic_to_openai(message, public_model), 200, ""
