"""Frame provider SSE before interpreting JSON; HTTP chunks/lines are not events."""
import json
from typing import AsyncIterator


async def iter_sse_data(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """Join data fields at event boundaries, accepting BOM, CRLF and compact data:."""
    data = []
    first = True
    async for line in lines:
        if first:
            line = line.removeprefix("\ufeff")
            first = False
        line = line.rstrip("\r\n")
        if line == "":
            if data:
                payload = "\n".join(data)
                data.clear()
                yield payload
                if payload.strip() == "[DONE]":
                    return
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and field == "data":
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        raise ValueError("SSE data 事件在空行终止前结束")


async def iter_sse_json_lines(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """Yield one canonical data line per JSON object, or the terminal [DONE]."""
    async for payload in iter_sse_data(lines):
        if payload.strip() == "[DONE]":
            yield "data: [DONE]"
            return
        try:
            value = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("SSE data 不是合法 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("SSE data 顶层必须是 JSON 对象")
        yield "data: " + json.dumps(value, ensure_ascii=False, separators=(",", ":"))
