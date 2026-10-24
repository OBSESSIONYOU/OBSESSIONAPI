"""Cline account channel: WorkOS device-code OAuth + OpenRouter-shaped inference.

Ported from dsh-our-free-model vendor/channel-pack/src/cline-*.ts (MIT):
- device authorize: POST {workos}/user-management/device_authorizations
- token poll: POST {workos}/user-management/devices (device_code + client_id)
- inference: POST {api}/v1/chat/completions (OpenAI/OpenRouter shape), token prefixed workos:
- client identity headers are mandatory.
"""
from __future__ import annotations

import time

import httpx

API_BASE = "https://api.cline.bot"
WORKOS_BASE = "https://api.workos.com"
WORKOS_CLIENT_ID = "client_01K3A541FN8TA3EPPHTD2325AR"
DEVICE_AUTH_PATH = "/user-management/device_authorizations"
DEVICE_TOKEN_PATH = "/user-management/devices"
CLIENT_HEADERS = {
    "HTTP-Referer": "https://cline.bot",
    "X-Title": "Cline",
    "X-IS-MULTIROOT": "false",
    "X-CLIENT-TYPE": "cline-sdk",
}
POLL_INTERVAL_S = 5
EXPIRES_S = 300


async def start_device_login() -> dict:
    data = {"client_id": WORKOS_CLIENT_ID}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(WORKOS_BASE + DEVICE_AUTH_PATH, data=data)
    r.raise_for_status()
    payload = r.json()
    return {
        "device_code": payload.get("device_code", ""),
        "user_code": payload.get("user_code", ""),
        "verification_uri": payload.get("verification_uri", "")
        or "https://app.cline.bot/device",
    }


async def poll_token(device_code: str) -> dict:
    data = {
        "device_code": device_code,
        "client_id": WORKOS_CLIENT_ID,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(WORKOS_BASE + DEVICE_TOKEN_PATH, data=data)
    payload = r.json()
    if r.status_code in (400, 403):
        error = payload.get("error") or payload.get("code") or ""
        if "pending" in str(error).lower():
            return {"pending": True}
        raise ValueError(payload.get("error_description") or str(error))
    access = payload.get("access_token", "")
    if not access:
        return {"pending": True}
    return {
        "access_token": "workos:" + access if not access.startswith("workos:") else access,
        "refresh_token": payload.get("refresh_token", ""),
        "expires_at": int(time.time() * 1000 + payload.get("expires_in", 3600) * 1000),
    }


def is_expired(credential: dict) -> bool:
    expires_at = credential.get("expires_at") or 0
    return bool(expires_at) and time.time() * 1000 >= expires_at - 60_000


def chat_headers(credential: dict) -> dict:
    headers = dict(CLIENT_HEADERS)
    headers["Authorization"] = "Bearer " + credential["access_token"]
    headers["Content-Type"] = "application/json"
    return headers


CHAT_PATH = "/v1/chat/completions"
