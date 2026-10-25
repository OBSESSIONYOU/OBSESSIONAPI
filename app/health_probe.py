"""可选的低成本渠道健康探测。"""
import asyncio
import time

import httpx

from . import database, settings_store
from .gateway import (
    _http_client,
    _request_error_detail,
    build_channel_headers,
    classify_upstream_error,
    normalize_anthropic_base_url,
    normalize_openai_base_url,
    upstream_error_message,
)
from .protocols import ACCOUNT_PROTOCOLS, channel_protocol, normalize_gemini_base_url


async def probe_channel(channel) -> dict:
    started = time.perf_counter()
    protocol = channel_protocol(channel)
    model = next((item.strip() for item in str(channel["models"] or "").split(",") if item.strip()), "")
    status_code = None
    error = ""
    ok = False
    try:
        if protocol == "mock":
            ok, status_code = True, 200
        elif protocol in ACCOUNT_PROTOCOLS:
            # These lanes use private account protocols, not a generic /models API.
            status_code = 400
            error = "账号渠道不支持通用模型列表探测，请通过 API 接入发起推理验证。"
        else:
            if protocol == "anthropic":
                url = normalize_anthropic_base_url(channel["base_url"]) + "/v1/models"
                headers = build_channel_headers(
                    channel, {"x-api-key": channel["api_key"], "anthropic-version": "2023-06-01"},
                    protocol="anthropic"
                )
            elif protocol == "gemini":
                url = normalize_gemini_base_url(channel["base_url"]) + "/v1beta/models"
                headers = build_channel_headers(channel, {"x-goog-api-key": channel["api_key"]}, protocol="gemini")
            else:
                url = normalize_openai_base_url(channel["base_url"]) + "/models"
                headers = build_channel_headers(channel, {"Authorization": f"Bearer {channel['api_key']}"}, protocol="openai_chat")
            async with _http_client(channel) as client:
                response = await client.get(url, headers=headers)
            status_code = response.status_code
            if response.status_code == 200:
                ok = True
            elif protocol == "anthropic" and response.status_code in {404, 405}:
                error = "模型列表接口不可用，可手动填写模型并运行 Messages 测试"
            else:
                error = upstream_error_message(channel["name"], response.status_code, response.text[:300], channel["api_key"], response.headers.get("content-type", ""))
    except httpx.RequestError as exc:
        error = upstream_error_message(channel["name"], 502, _request_error_detail(exc), channel["api_key"])
        status_code = 502
    except Exception as exc:  # noqa: BLE001
        error = upstream_error_message(channel["name"], 502, str(exc), channel["api_key"])
        status_code = 502
    latency = max(1, int((time.perf_counter() - started) * 1000))
    category = "" if ok else (
        "model_list_unavailable" if protocol in ACCOUNT_PROTOCOLS or (protocol == "anthropic" and status_code in {404, 405})
        else classify_upstream_error(status_code, error)
    )
    return {
        "channel_id": channel["id"],
        "ok": ok,
        "latency_ms": latency,
        "status_code": status_code,
        "error_category": category,
        "error_msg": error[:300],
        "created_at": database.iso(database.utcnow()),
    }


async def run_probe(channel_id: int) -> dict:
    channel = database.query_one("SELECT * FROM channels WHERE id=?", (channel_id,))
    if channel is None:
        raise ValueError("渠道不存在")
    result = await probe_channel(channel)
    database.execute(
        "INSERT INTO channel_probes(channel_id,ok,latency_ms,status_code,error_category,error_msg,created_at) VALUES(?,?,?,?,?,?,?)",
        (result["channel_id"], int(result["ok"]), result["latency_ms"], result["status_code"], result["error_category"], result["error_msg"], result["created_at"]),
    )
    return result


async def probe_loop() -> None:
    while True:
        for channel in database.query("SELECT * FROM channels WHERE status=1 ORDER BY priority DESC, id"):
            await run_probe(channel["id"])
        await asyncio.sleep(max(10, int(settings_store.get("probe_interval_s"))))
