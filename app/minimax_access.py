"""MiniMax Code account channel: device-code OAuth + Anthropic-Messages-shaped inference.

Ported from dsh-our-free-model vendor/channel-pack/src/minimax-*.ts (MIT):
- device code: POST {account}/oauth2/device/code (client_id=mcode-public, audience=agent-backend)
- poll/refresh: POST {account}/oauth2/token
- models: GET {api}/mavis/api/v1/models?region=&buildEnv=
- inference: POST {api}/mavis/api/v1/llm/v1/messages (Anthropic Messages shape)
- daily signin: /minimax-cloud/api/v1/signin/status + claim (timezone_id required)
"""
from __future__ import annotations

import time

import httpx

ACCOUNT_HOST = "https://account.minimax.cn"
API_HOST = "https://agent.minimax.cn"
CLIENT_ID = "mcode-public"
AUDIENCE = "agent-backend"
SCOPE = "offline_access openid profile email"
DEVICE_CODE_PATH = "/oauth2/device/code"
TOKEN_PATH = "/oauth2/token"
MODELS_PATH = "/mavis/api/v1/models"
INFER_PATH = "/mavis/api/v1/llm/v1/messages"
SIGNIN_STATUS_PATH = "/minimax-cloud/api/v1/signin/status"
SIGNIN_CLAIM_PATH = "/minimax-cloud/api/v1/signin/claim"
REQUEST_TIMEOUT_S = 30


async def start_device_login() -> dict:
    data = {
        "client_id": CLIENT_ID,
        "audience": AUDIENCE,
        "scope": SCOPE,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(ACCOUNT_HOST + DEVICE_CODE_PATH, data=data)
    r.raise_for_status()
    return r.json()


async def poll_token(device_code: str) -> dict:
    """Poll for token. Returns credential dict, or {"pending": True} while waiting."""
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
        "client_id": CLIENT_ID,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(ACCOUNT_HOST + TOKEN_PATH, data=data)
    payload = r.json()
    error = payload.get("error")
    if error in ("authorization_pending", "slow_down"):
        return {"pending": True}
    if error:
        raise ValueError(payload.get("error_description") or error)
    return {
        "access_token": payload.get("access_token", ""),
        "refresh_token": payload.get("refresh_token", ""),
        "expires_at": int(time.time() * 1000 + payload.get("expires_in", 3600) * 1000),
    }


async def refresh(credential: dict) -> dict:
    if not credential.get("refresh_token"):
        raise ValueError("no refresh_token; relogin required")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": credential["refresh_token"],
        "client_id": CLIENT_ID,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(ACCOUNT_HOST + TOKEN_PATH, data=data)
    r.raise_for_status()
    payload = r.json()
    updated = dict(credential)
    updated["access_token"] = payload.get("access_token", "")
    updated["expires_at"] = int(time.time() * 1000 + payload.get("expires_in", 3600) * 1000)
    return updated


def is_expired(credential: dict) -> bool:
    expires_at = credential.get("expires_at") or 0
    return bool(expires_at) and time.time() * 1000 >= expires_at - 60_000


async def ensure_fresh(credential: dict) -> dict:
    if is_expired(credential):
        try:
            return await refresh(credential)
        except Exception:
            return credential
    return credential


def _headers(credential: dict) -> dict:
    return {
        "Authorization": "Bearer " + credential["access_token"],
        "Content-Type": "application/json",
    }


async def list_models(credential: dict) -> list:
    credential = await ensure_fresh(credential)
    params = {"region": "CN", "buildEnv": "prod"}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        r = await client.get(API_HOST + MODELS_PATH, params=params, headers=_headers(credential))
    r.raise_for_status()
    data = r.json()
    rows = data.get("data") or data.get("models") or []
    out = []
    for item in rows if isinstance(rows, list) else []:
        if isinstance(item, dict) and item.get("model"):
            out.append(item["model"])
    return out


async def signin_status(credential: dict) -> dict:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        r = await client.get(
            API_HOST + SIGNIN_STATUS_PATH,
            params={"timezone_id": "Asia/Shanghai"},
            headers=_headers(credential),
        )
    if r.status_code != 200:
        return {"ok": False, "error": "HTTP %d" % r.status_code}
    return {"ok": True, **r.json()}


async def signin_claim(credential: dict) -> dict:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        r = await client.post(
            API_HOST + SIGNIN_CLAIM_PATH,
            params={"timezone_id": "Asia/Shanghai"},
            headers=_headers(credential),
        )
    if r.status_code != 200:
        return {"ok": False, "error": "HTTP %d" % r.status_code}
    return {"ok": True, **r.json()}


def openai_to_anthropic_body(openai_body: dict, model: str) -> dict:
    """OpenAI chat body -> Anthropic Messages body (the wire MiniMax Code speaks)."""
    system_text = ""
    messages = []
    for message in openai_body.get("messages") or []:
        role = message.get("role")
        raw = message.get("content")
        text = raw if isinstance(raw, str) else str(raw or "")
        if role == "system":
            system_text += (chr(10) if system_text else "") + text
            continue
        messages.append({"role": "assistant" if role == "assistant" else "user", "content": text})
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": openai_body.get("max_tokens") or 32768,
        "stream": bool(openai_body.get("stream")),
    }
    if system_text:
        body["system"] = system_text
    return body


async def credit_points(credential: dict) -> dict:
    """Sum credit points from /minimax-cloud/api/v1/credit/details."""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
        r = await client.get(
            API_HOST + '/minimax-cloud/api/v1/credit/details',
            params={"timezone_id": "Asia/Shanghai"},
            headers=_headers(credential),
        )
    if r.status_code != 200:
        return {"points": 0, "error": "HTTP %d" % r.status_code}
    payload = r.json()
    data = payload.get("data") or payload
    items = data.get("list") or data.get("credits") or []
    total = 0.0
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            total += float(item.get("points") or 0)
    return {"points": round(total, 2)}
