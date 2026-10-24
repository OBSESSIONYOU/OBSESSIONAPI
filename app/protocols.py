"""Small protocol adapters used by the gateway and channel discovery.

The gateway routes one OpenAI-shaped internal request.  This module only
translates the wire format at the protocol boundary; it intentionally has no
network or database code.
"""
import ipaddress
import json
import re
from urllib.parse import urlparse, urlunparse


PROTOCOLS = {"auto", "openai_chat", "anthropic", "openai_responses", "gemini", "mock"}
LEGACY_PROTOCOLS = {"openai": "openai_chat"}

_IGNORED_HOST_LABELS = {"api", "www", "openapi", "gateway", "console"}
_KNOWN_CHANNEL_NAMES = {
    "api.scnet.cn": "scnet",
    "www.scnet.cn": "scnet",
    "api.deepseek.com": "deepseek",
    "dashscope.aliyuncs.com": "dashscope",
    "open.bigmodel.cn": "bigmodel",
    "api.moonshot.cn": "moonshot",
    "api.siliconflow.cn": "siliconflow",
    "api.minimax.chat": "minimax",
    "api.openai.com": "openai",
    "agentrouter.org": "agentrouter",
}


def infer_channel_name(base_url: str) -> str:
    """从 URL 本地推导短渠道名，不读取网络也不依赖路径片段。"""
    try:
        parsed = urlparse(str(base_url or "").strip())
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    if not hostname:
        return ""
    try:
        is_ip = bool(ipaddress.ip_address(hostname))
    except ValueError:
        is_ip = False
    if hostname in {"localhost", "127.0.0.1", "::1"} or is_ip:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return f"local-{port}"
    if hostname in _KNOWN_CHANNEL_NAMES:
        return _KNOWN_CHANNEL_NAMES[hostname]
    for suffix, name in _KNOWN_CHANNEL_NAMES.items():
        if hostname.endswith("." + suffix):
            return name
    labels = [re.sub(r"[^a-z0-9]+", "-", label).strip("-")
              for label in hostname.split(".")]
    labels = [label for label in labels if label and label not in _IGNORED_HOST_LABELS]
    if not labels:
        return ""
    return labels[0].lower()


def recommended_protocol(base_url: str) -> str:
    """仅基于地址形态给出提示，不替代真实检测。"""
    try:
        path = (urlparse(str(base_url or "")).path or "").lower().rstrip("/")
    except ValueError:
        return "openai_chat"
    if path.endswith("/messages"):
        return "anthropic"
    if path.endswith("/responses"):
        return "openai_responses"
    if "/v1beta" in path or "generativelanguage.googleapis.com" in str(base_url or "").lower():
        return "gemini"
    return "openai_chat"


def normalize_protocol(value: str, default: str = "auto") -> str:
    protocol = str(value or default).strip().lower()
    protocol = LEGACY_PROTOCOLS.get(protocol, protocol)
    if protocol not in PROTOCOLS:
        raise ValueError("渠道协议只能是 auto、openai_chat、anthropic、openai_responses、gemini 或 mock")
    return protocol


def _root_url(value: str, suffixes: tuple[str, ...]) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
    path = parsed.path.rstrip("/")
    for suffix in sorted(suffixes, key=len, reverse=True):
        if path.endswith(suffix):
            path = path[:-len(suffix)] or ""
            break
    return urlunparse(parsed._replace(path=path.rstrip("/"), params="", query="", fragment="")).rstrip("/")


def normalize_openai_responses_base_url(value: str) -> str:
    return _root_url(value, ("/v1/responses", "/responses", "/v1/chat/completions", "/v1"))


def normalize_gemini_base_url(value: str) -> str:
    return _root_url(value, ("/v1beta/models", "/v1beta"))


def safe_public_url(value: str) -> str:
    parsed = urlparse(str(value or ""))
    return urlunparse(parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[-1], query="", fragment=""))


def channel_protocol(channel) -> str:
    def value(key, default=""):
        try:
            return channel[key]
        except (KeyError, IndexError, TypeError):
            return default

    raw = str(value("type", "auto"))
    protocol = LEGACY_PROTOCOLS.get(raw.lower(), raw.lower())
    if protocol == "auto":
        for key in ("last_success_protocol", "detected_protocol"):
            candidate = str(value_for_key(channel, key, "") or "").lower()
            if candidate in PROTOCOLS - {"auto"}:
                return candidate
        return "openai_chat"
    return protocol if protocol in PROTOCOLS else "openai_chat"


def value_for_key(channel, key, default=""):
    try:
        return channel[key]
    except (KeyError, IndexError, TypeError):
        return default


def _message_content_parts(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return content
    return ""


def _tool_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(not isinstance(part, dict) or part.get("type") not in {"text", "input_text", "output_text"}
               or not isinstance(part.get("text"), str) for part in content):
            raise ValueError("工具结果目前只支持文本内容")
        return "".join(part["text"] for part in content)
    raise ValueError("工具结果必须是文本或文本块数组")


def _function_arguments(value) -> dict:
    try:
        args = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise ValueError("工具 arguments 不是合法 JSON") from exc
    if not isinstance(args, dict):
        raise ValueError("工具 arguments 必须是 JSON 对象")
    return args


def _function_tool(tool: dict) -> dict:
    if not isinstance(tool, dict) or tool.get("type", "function") != "function":
        raise ValueError("跨协议转换仅支持 function 工具")
    fn = tool.get("function")
    if not isinstance(fn, dict) or not isinstance(fn.get("name"), str) or not fn["name"]:
        raise ValueError("function 工具缺少函数名")
    return fn


def openai_to_responses(body: dict) -> dict:
    messages = []
    instructions = []
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        if role in {"system", "developer"}:
            if isinstance(message.get("content"), str):
                instructions.append(message["content"])
            continue
        if role == "tool":
            if not message.get("tool_call_id"):
                raise ValueError("工具结果缺少 tool_call_id")
            messages.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                             "output": _tool_text(message.get("content"))})
            continue
        if message.get("content") is not None or not message.get("tool_calls"):
            messages.append({"role": role, "content": _message_content_parts(message.get("content"))})
        for call in message.get("tool_calls") or []:
            fn = _function_tool(call)
            if not call.get("id") or not isinstance(fn.get("arguments"), str):
                raise ValueError("工具调用缺少 id 或 arguments 字符串")
            messages.append({"type": "function_call", "call_id": call["id"], "name": fn["name"],
                             "arguments": fn["arguments"]})
    result = {"model": body.get("model", ""), "input": messages, "stream": bool(body.get("stream"))}
    if instructions:
        result["instructions"] = "\n\n".join(instructions)
    for source, target in (("max_tokens", "max_output_tokens"), ("max_completion_tokens", "max_output_tokens"),
                           ("temperature", "temperature"), ("top_p", "top_p")):
        if source in body and target not in result:
            result[target] = body[source]
    tools = []
    for tool in body.get("tools") or []:
        fn = _function_tool(tool)
        tools.append({"type": "function", **{key: fn[key] for key in ("name", "description", "parameters", "strict") if key in fn}})
    if tools:
        result["tools"] = tools
    if "tool_choice" in body:
        choice = body["tool_choice"]
        result["tool_choice"] = {"type": "function", "name": _function_tool(choice)["name"]} if isinstance(choice, dict) else choice
    if "parallel_tool_calls" in body:
        result["parallel_tool_calls"] = body["parallel_tool_calls"]
    return result


def responses_to_openai(body: dict) -> dict:
    data = dict(body or {})
    if data.get("previous_response_id") or data.get("conversation"):
        raise ValueError("跨协议转换不支持 previous_response_id/conversation，请传完整 input 历史")
    input_value = data.get("input", "")
    messages = []
    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
    elif isinstance(input_value, list):
        for item in input_value:
            if not isinstance(item, dict):
                raise ValueError("Responses input 必须是对象数组")
            kind = item.get("type", "message")
            if kind == "function_call":
                if not item.get("call_id") or not item.get("name") or not isinstance(item.get("arguments"), str):
                    raise ValueError("function_call 缺少 call_id、name 或 arguments")
                call = {"id": item["call_id"], "type": "function", "function": {
                    "name": item["name"], "arguments": item["arguments"]}}
                if messages and messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls"):
                    messages[-1]["tool_calls"].append(call)
                else:
                    messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
                continue
            if kind == "function_call_output":
                if not item.get("call_id"):
                    raise ValueError("function_call_output 缺少 call_id")
                messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": _tool_text(item.get("output"))})
                continue
            if kind != "message":
                raise ValueError(f"不支持跨协议转换 Responses input 类型 {kind}")
            role = item.get("role", "user")
            content = item.get("content", "")
            if isinstance(content, list):
                content = _tool_text(content)
            messages.append({"role": role, "content": content})
    else:
        raise ValueError("Responses input 必须是文本或对象数组")
    instructions = data.get("instructions")
    if instructions:
        messages.insert(0, {"role": "system", "content": instructions})
    result = {"model": data.get("model", ""), "messages": messages, "stream": bool(data.get("stream"))}
    if "max_output_tokens" in data:
        result["max_tokens"] = data["max_output_tokens"]
    for key in ("temperature", "top_p", "parallel_tool_calls"):
        if key in data:
            result[key] = data[key]
    if "tools" in data:
        tools = []
        for tool in data["tools"] or []:
            if not isinstance(tool, dict) or tool.get("type") != "function" or not tool.get("name"):
                raise ValueError("跨协议转换仅支持 Responses function 工具")
            tools.append({"type": "function", "function": {key: tool[key] for key in ("name", "description", "parameters", "strict") if key in tool}})
        result["tools"] = tools
    if "tool_choice" in data:
        choice = data["tool_choice"]
        if isinstance(choice, dict):
            if choice.get("type") != "function" or not choice.get("name"):
                raise ValueError("跨协议转换仅支持 function tool_choice")
            choice = {"type": "function", "function": {"name": choice["name"]}}
        result["tool_choice"] = choice
    return result


def responses_to_chat(result: dict, requested_model: str) -> dict:
    if not isinstance(result, dict):
        raise ValueError("Responses 响应顶层不是对象")
    text_parts = []
    tool_calls = []
    refusals = []
    for item in result.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                    if isinstance(part.get("text"), str):
                        text_parts.append(part["text"])
                elif isinstance(part, dict) and part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
                    refusals.append(part["refusal"])
        elif item.get("type") in {"function_call", "tool_call"}:
            tool_calls.append({
                "id": str(item.get("call_id") or item.get("id") or ""),
                "type": "function",
                "function": {"name": str(item.get("name") or ""),
                              "arguments": item.get("arguments", "{}") if isinstance(item.get("arguments", "{}"), str)
                              else json.dumps(item.get("arguments", {}), ensure_ascii=False, separators=(",", ":"))},
            })
    if isinstance(result.get("output_text"), str) and not text_parts:
        text_parts.append(result["output_text"])
    message = {"role": "assistant", "content": "".join(text_parts) if text_parts else None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if not (text_parts or tool_calls or refusals or result.get("refusal")):
        raise ValueError("Responses 响应没有文本、工具调用或拒绝信息")
    if isinstance(result.get("refusal"), str) and result["refusal"]:
        refusals.append(result["refusal"])
    if refusals:
        message["refusal"] = "".join(refusals)
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    mapped_usage = {}
    for source, target in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"),
                           ("total_tokens", "total_tokens")):
        if usage.get(source) is not None:
            mapped_usage[target] = usage[source]
    if "total_tokens" not in mapped_usage and mapped_usage:
        mapped_usage["total_tokens"] = mapped_usage.get("prompt_tokens", 0) + mapped_usage.get("completion_tokens", 0)
    finish = "tool_calls" if tool_calls else "stop"
    if isinstance(result.get("status"), str) and result["status"] == "incomplete":
        finish = "length"
    return {"id": str(result.get("id") or ""), "object": "chat.completion", "created": int(__import__("time").time()),
            "model": result.get("model") or requested_model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            **({"usage": mapped_usage} if mapped_usage else {})}


def openai_to_gemini(body: dict) -> dict:
    contents = []
    system_parts = []
    call_names = {}
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role in {"system", "developer"}:
            if isinstance(message.get("content"), str):
                system_parts.append({"text": message["content"]})
            continue
        gemini_role = "model" if role == "assistant" else "user"
        parts = []
        if role == "tool":
            call_id = message.get("tool_call_id")
            name = call_names.get(call_id)
            if not name:
                raise ValueError("Gemini 工具结果找不到对应的 tool_call_id")
            text = _tool_text(message.get("content"))
            try:
                value = json.loads(text)
            except (TypeError, ValueError):
                value = text
            parts.append({"functionResponse": {"id": call_id, "name": name,
                          "response": value if isinstance(value, dict) else {"output": value}}})
            contents.append({"role": "user", "parts": parts})
            continue
        content = message.get("content")
        if isinstance(content, str) and content:
            parts.append({"text": content})
        elif isinstance(content, list):
            parts.extend(part for part in content if isinstance(part, dict))
        for call in message.get("tool_calls") or []:
            fn = _function_tool(call)
            if not call.get("id"):
                raise ValueError("工具调用缺少 id")
            call_names[call["id"]] = fn["name"]
            parts.append({"functionCall": {"id": call["id"], "name": fn["name"],
                                           "args": _function_arguments(fn.get("arguments"))}})
        contents.append({"role": gemini_role, "parts": parts or [{"text": ""}]})
    result = {"contents": contents or [{"role": "user", "parts": [{"text": ""}]}]}
    if system_parts:
        result["systemInstruction"] = {"parts": system_parts}
    generation = {target: body[source] for source, target in (
        ("temperature", "temperature"), ("top_p", "topP"), ("max_tokens", "maxOutputTokens"),
        ("max_completion_tokens", "maxOutputTokens"), ("stop", "stopSequences")) if source in body}
    if isinstance(generation.get("stopSequences"), str):
        generation["stopSequences"] = [generation["stopSequences"]]
    if generation:
        result["generationConfig"] = generation
    declarations = []
    for tool in body.get("tools") or []:
        fn = _function_tool(tool)
        declarations.append({key: fn[key] for key in ("name", "description", "parameters") if key in fn})
    if declarations:
        result["tools"] = [{"function_declarations": declarations}]
    if "tool_choice" in body:
        choice = body["tool_choice"]
        if isinstance(choice, dict):
            config = {"mode": "ANY", "allowedFunctionNames": [_function_tool(choice)["name"]]}
        elif choice in {"auto", "none", "required"}:
            config = {"mode": {"auto": "AUTO", "none": "NONE", "required": "ANY"}[choice]}
        else:
            raise ValueError("不支持的 Gemini tool_choice 转换")
        result["toolConfig"] = {"functionCallingConfig": config}
    return result


def gemini_request_to_openai(body: dict, requested_model: str) -> dict:
    messages = []
    pending_calls = []
    next_call_index = 0
    system = body.get("systemInstruction") if isinstance(body.get("systemInstruction"), dict) else {}
    system_text = "".join(str(part.get("text", "")) for part in system.get("parts", []) if isinstance(part, dict))
    if system_text:
        messages.append({"role": "system", "content": system_text})
    for item in body.get("contents") or []:
        if not isinstance(item, dict):
            continue
        role = "assistant" if item.get("role") == "model" else "user"
        text, calls, results = [], [], []
        for part in item.get("parts") or []:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                text.append(part["text"])
            if isinstance(part.get("functionCall"), dict):
                call = part["functionCall"]
                if not call.get("name"):
                    raise ValueError("Gemini functionCall 缺少 name")
                call_id = call.get("id") or f"gemini_call_{next_call_index}"
                next_call_index += 1
                calls.append({"id": call_id, "type": "function", "function": {
                    "name": call["name"], "arguments": json.dumps(_function_arguments(call.get("args", {})), ensure_ascii=False, separators=(",", ":"))}})
                pending_calls.append((call_id, call["name"]))
            if isinstance(part.get("functionResponse"), dict):
                response = part["functionResponse"]
                match = next((call for call in pending_calls if
                              (call[0] == response["id"] if response.get("id") else call[1] == response.get("name"))), None)
                if match is None:
                    raise ValueError("Gemini functionResponse 找不到对应的 functionCall")
                pending_calls.remove(match)
                results.append({"role": "tool", "tool_call_id": match[0], "content": json.dumps(
                    response.get("response", {}), ensure_ascii=False, separators=(",", ":"))})
        message = {"role": role, "content": "".join(text) or None}
        if calls:
            message["tool_calls"] = calls
        # 工具结果必须紧随 assistant 调用，后续普通 user 文本单独保留。
        messages.extend(results)
        if text or calls or not results:
            messages.append(message)
    result = {"model": requested_model, "messages": messages, "stream": bool(body.get("stream"))}
    generation = body.get("generationConfig") if isinstance(body.get("generationConfig"), dict) else {}
    for source, target in (("maxOutputTokens", "max_tokens"), ("temperature", "temperature"), ("topP", "top_p"), ("stopSequences", "stop")):
        if source in generation:
            result[target] = generation[source]
    tools = []
    for group in body.get("tools") or []:
        if not isinstance(group, dict):
            continue
        declarations = group.get("functionDeclarations", group.get("function_declarations", [])) or []
        if not declarations:
            raise ValueError("跨协议转换仅支持 Gemini functionDeclarations 工具")
        for declaration in declarations:
            if isinstance(declaration, dict) and declaration.get("name"):
                tools.append({"type": "function", "function": {
                    key: declaration[key] for key in ("name", "description", "parameters") if key in declaration
                }})
    if tools:
        result["tools"] = tools
    config = body.get("toolConfig", body.get("tool_config", {})) or {}
    config = config.get("functionCallingConfig", config.get("function_calling_config", {})) or {}
    if config:
        mode = config.get("mode", "AUTO")
        names = config.get("allowedFunctionNames", config.get("allowed_function_names", [])) or []
        if mode == "ANY" and len(names) == 1:
            result["tool_choice"] = {"type": "function", "function": {"name": names[0]}}
        elif mode in {"AUTO", "NONE", "ANY"} and not names:
            result["tool_choice"] = {"AUTO": "auto", "NONE": "none", "ANY": "required"}[mode]
        else:
            raise ValueError("Gemini functionCallingConfig 无等价 Chat tool_choice")
    return result


def gemini_to_openai(body: dict, requested_model: str) -> dict:
    if not isinstance(body, dict):
        raise ValueError("Gemini 响应顶层不是对象")
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        raise ValueError("Gemini 响应缺少 candidates")
    candidate = candidates[0]
    parts = candidate.get("content", {}).get("parts", []) if isinstance(candidate.get("content"), dict) else []
    texts, tool_calls = [], []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            texts.append(part["text"])
        call = part.get("functionCall")
        if isinstance(call, dict):
            if not call.get("name"):
                raise ValueError("Gemini functionCall 缺少 name")
            tool_calls.append({"id": call.get("id") or f"gemini_call_{len(tool_calls)}", "type": "function", "function": {
                "name": str(call.get("name") or ""),
                "arguments": json.dumps(_function_arguments(call.get("args", {})), ensure_ascii=False, separators=(",", ":")),
            }})
    message = {"role": "assistant", "content": "".join(texts) if texts else None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if not texts and not tool_calls:
        raise ValueError("Gemini 响应没有文本或工具调用")
    usage = body.get("usageMetadata") if isinstance(body.get("usageMetadata"), dict) else {}
    mapped = {}
    for source, target in (("promptTokenCount", "prompt_tokens"), ("candidatesTokenCount", "completion_tokens"), ("totalTokenCount", "total_tokens")):
        if usage.get(source) is not None:
            mapped[target] = usage[source]
    if "total_tokens" not in mapped and mapped:
        mapped["total_tokens"] = mapped.get("prompt_tokens", 0) + mapped.get("completion_tokens", 0)
    reason = candidate.get("finishReason")
    if reason in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"}:
        finish = "content_filter"
    elif reason in {None, "STOP", "MAX_TOKENS"}:
        finish = "length" if reason == "MAX_TOKENS" else "tool_calls" if tool_calls else "stop"
    else:
        raise ValueError(f"Gemini finishReason={reason} 无等价 Chat finish_reason")
    return {"id": "gemini-" + str(abs(hash(json.dumps(body, sort_keys=True)))), "object": "chat.completion",
            "created": int(__import__("time").time()), "model": requested_model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            **({"usage": mapped} if mapped else {})}


def anthropic_to_openai_request(body: dict) -> dict:
    messages = []
    if body.get("system"):
        system = body["system"] if isinstance(body["system"], str) else body["system"]
        messages.append({"role": "system", "content": system})
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            text, calls, results = [], [], []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text.append(str(block.get("text") or ""))
                elif block.get("type") == "tool_use":
                    calls.append({"id": block.get("id", ""), "type": "function", "function": {
                        "name": block.get("name", ""), "arguments": json.dumps(block.get("input", {}), ensure_ascii=False, separators=(",", ":"))}})
                elif block.get("type") == "tool_result":
                    if not block.get("tool_use_id"):
                        raise ValueError("Anthropic tool_result 缺少 tool_use_id")
                    results.append({"role": "tool", "tool_call_id": block["tool_use_id"],
                                    "content": _tool_text(block.get("content", ""))})
            item = {"role": message.get("role", "user"), "content": "".join(text) or None}
            if calls:
                item["tool_calls"] = calls
            messages.extend(results)
            if text or calls or not results:
                messages.append(item)
        else:
            messages.append({"role": message.get("role", "user"), "content": content or ""})
    result = {"model": body.get("model", ""), "messages": messages, "stream": bool(body.get("stream"))}
    if body.get("max_tokens") is not None:
        result["max_tokens"] = body["max_tokens"]
    # 保留显式推理意图，具体 OpenAI 兼容渠道是否支持由网关按渠道处理。
    for key in ("thinking", "reasoning_effort"):
        if key in body:
            result[key] = body[key]
    tools = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type", "custom") != "custom" or not tool.get("name"):
            raise ValueError("跨协议转换仅支持 Anthropic 自定义函数工具")
        if isinstance(tool, dict) and tool.get("name"):
            tools.append({"type": "function", "function": {
                key: tool[key] for key in ("name", "description", "input_schema") if key in tool
            }})
            if "input_schema" in tools[-1]["function"]:
                tools[-1]["function"]["parameters"] = tools[-1]["function"].pop("input_schema")
    if tools:
        result["tools"] = tools
    if "tool_choice" in body:
        choice = body["tool_choice"]
        if not isinstance(choice, dict):
            raise ValueError("Anthropic tool_choice 必须是对象")
        kind = choice.get("type")
        if kind == "tool" and choice.get("name"):
            result["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
        elif kind in {"auto", "any", "none"}:
            result["tool_choice"] = {"auto": "auto", "any": "required", "none": "none"}[kind]
        else:
            raise ValueError("不支持的 Anthropic tool_choice 类型")
        if "disable_parallel_tool_use" in choice:
            result["parallel_tool_calls"] = not bool(choice["disable_parallel_tool_use"])
    return result


def openai_to_anthropic_response(result: dict) -> dict:
    # OpenAI-compatible reasoning_content 没有可验证的 Anthropic signature；不伪造 thinking block，最终文本/工具与停止语义仍照实转换。
    if not isinstance(result, dict):
        raise ValueError("OpenAI 响应顶层不是对象")
    choice = (result.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = []
    raw_content = message.get("content")
    if isinstance(raw_content, str) and raw_content:
        content.append({"type": "text", "text": raw_content})
    elif isinstance(raw_content, list):
        for block in raw_content:
            if not isinstance(block, dict) or block.get("type") not in {"text", "output_text"}:
                raise ValueError("不支持的 OpenAI content 类型，无法转换为 Anthropic text")
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError("Anthropic text block 缺少文本")
            if text:
                content.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            raise ValueError("工具调用不是对象")
        fn = call.get("function")
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str) or not fn["name"].strip():
            raise ValueError("工具调用缺少函数名")
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            raise ValueError("工具调用缺少 id")
        raw_arguments = fn.get("arguments")
        if not isinstance(raw_arguments, str) or not raw_arguments.strip():
            raise ValueError("工具 arguments 不是合法 JSON")
        try:
            inp = json.loads(raw_arguments)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("工具 arguments 不是合法 JSON") from exc
        if not isinstance(inp, dict):
            raise ValueError("工具 arguments 必须是 JSON 对象")
        content.append({"type": "tool_use", "id": call_id, "name": fn["name"], "input": inp})
    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal:
        content.append({"type": "text", "text": refusal})
    finish_reason = choice.get("finish_reason")
    if message.get("tool_calls") or finish_reason in {"tool_calls", "function_call"}:
        stop_reason = "tool_use"
    elif finish_reason in (None, "stop"):
        stop_reason = "end_turn"
    elif finish_reason == "length":
        stop_reason = "max_tokens"
    elif finish_reason == "stop_sequence":
        stop_reason = "stop_sequence"
    else:
        raise ValueError(f"OpenAI finish_reason={finish_reason} 无等价 Anthropic stop_reason")
    if not content and stop_reason != "max_tokens":
        raise ValueError("OpenAI 响应没有可转换的文本、工具调用或拒答信息")
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    mapped_usage = {}
    for source, target in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")):
        if usage.get(source) is not None:
            mapped_usage[target] = usage[source]
    return {"id": result.get("id", ""), "type": "message", "role": "assistant", "model": result.get("model", ""),
            "content": content, "stop_reason": stop_reason,
            **({"usage": mapped_usage} if mapped_usage else {})}


def openai_to_gemini_response(result: dict) -> dict:
    choice = (result.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    parts = []
    if isinstance(message.get("content"), str) and message["content"]:
        parts.append({"text": message["content"]})
    for call in message.get("tool_calls") or []:
        fn = _function_tool(call)
        parts.append({"functionCall": {"name": fn["name"], "args": _function_arguments(fn.get("arguments")),
                                        **({"id": call["id"]} if call.get("id") else {})}})
    finish = {None: "STOP", "stop": "STOP", "tool_calls": "STOP", "function_call": "STOP",
              "length": "MAX_TOKENS", "content_filter": "SAFETY"}.get(choice.get("finish_reason"))
    if finish is None:
        raise ValueError("OpenAI finish_reason 无等价 Gemini finishReason")
    payload = {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}]}
    if result.get("usage"):
        usage = result["usage"]
        payload["usageMetadata"] = {"promptTokenCount": usage.get("prompt_tokens", 0),
                                     "candidatesTokenCount": usage.get("completion_tokens", 0),
                                     "totalTokenCount": usage.get("total_tokens", 0)}
    return payload


def openai_to_responses_response(result: dict) -> dict:
    choice = (result.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = []
    if isinstance(message.get("content"), str) and message["content"]:
        content.append({"type": "output_text", "text": message["content"], "annotations": []})
    if isinstance(message.get("refusal"), str) and message["refusal"]:
        content.append({"type": "refusal", "refusal": message["refusal"]})
    finish = choice.get("finish_reason")
    if finish not in {None, "stop", "tool_calls", "function_call", "length", "content_filter"}:
        raise ValueError("OpenAI finish_reason 无等价 Responses status")
    incomplete = finish in {"length", "content_filter"}
    status = "incomplete" if incomplete else "completed"
    output = [{"type": "message", "id": result.get("id", ""), "role": "assistant", "content": content,
               "status": status}] if content else []
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        output.append({"type": "function_call", "id": call.get("id", ""), "call_id": call.get("id", ""),
                       "name": fn.get("name", ""), "arguments": fn.get("arguments", "{}")})
    payload = {"id": result.get("id", ""), "object": "response", "model": result.get("model", ""),
               "output": output, "output_text": message.get("content") or "", "status": status}
    if incomplete:
        payload["incomplete_details"] = {"reason": "max_output_tokens" if finish == "length" else "content_filter"}
    if result.get("usage"):
        usage = result["usage"]
        payload["usage"] = {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0),
                             "total_tokens": usage.get("total_tokens", 0)}
    return payload
