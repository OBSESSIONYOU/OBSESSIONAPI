"""LobsterAI account channel: OAuth login, token refresh, OpenAI-shaped forwarding.

Ported from dsh-our-free-model plugin vendor/channel-pack/src/lobsterai*.ts (MIT).
Protocol facts (all live-verified in that source):
- login URL: {portal}/portal#/login?source=electron&redirect_uri=http://127.0.0.1:{port}/auth/callback&state=...
- exchange: POST {api}/api/auth/exchange  body: authCode/firstKeyfrom/latestKeyfrom/uuid/version (no auth header)
- refresh:  POST {api}/api/auth/refresh   body: keyfrom + refreshToken (no auth header)
- chat:     POST {api}/api/proxy/v1/chat/completions  standard OpenAI shape
  headers: Bearer + X-LobsterAI-Client-Capabilities: kimi-k3-agentic-v1,thinking-level-control-v1
           + X-LobsterAI-Client-Version + User-Agent: LobsterAI/0.1.0
- models:   GET {api}/api/models/available (Capabilities header filters the set)
"""
from __future__ import annotations

import json
import time
import uuid as uuid_lib
from urllib.parse import urlencode

import httpx

API_BASE = "https://lobsterai-server.youdao.com"
PORTAL_BASE = "https://lobsterai.youdao.com"
VERSION_API = "https://api-overmind.youdao.com/openapi/get/luna/hardware/lobsterai/prod/update"
FALLBACK_VERSION = "2026.9.4"
USER_AGENT = "LobsterAI/0.1.0"
CLIENT_CAPABILITIES = "kimi-k3-agentic-v1,thinking-level-control-v1"
EXCHANGE_PATH = "/api/auth/exchange"
REFRESH_PATH = "/api/auth/refresh"
MODELS_PATH = "/api/models/available"
CHAT_PATH = "/api/proxy/v1/chat/completions"
CALLBACK_PATH = "/auth/callback"
REQUEST_TIMEOUT_S = 30


def new_login_session():
    return {"uuid": str(uuid_lib.uuid4()), "first_keyfrom": str(int(time.time() * 1000))}


def build_login_url(port, state):
    redirect = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
    query = urlencode({"source": "electron", "redirect_uri": redirect, "state": state})
    return f"{PORTAL_BASE}/portal#/login?{query}"


def _anonymous_headers():
    return {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": USER_AGENT}


def chat_headers(access_token, client_version, sse=True):
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "text/event-stream, application/json" if sse else "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-LobsterAI-Client-Capabilities": CLIENT_CAPABILITIES,
        "X-LobsterAI-Client-Version": client_version,
    }


async def fetch_client_version(client=None):
    try:
        own = client is None
        c = client or httpx.AsyncClient(timeout=10)
        r = await c.get(VERSION_API)
        if own:
            await c.aclose()
        if r.status_code == 200:
            data = r.json()
            version = (data.get("data") or {}).get("version") if isinstance(data, dict) else None
            if isinstance(version, str) and version.strip():
                return version.strip()
    except Exception:
        pass
    return FALLBACK_VERSION


async def exchange_auth_code(code, session):
    version = await fetch_client_version()
    body = {
        "authCode": code,
        "firstKeyfrom": session["first_keyfrom"],
        "latestKeyfrom": str(int(time.time() * 1000)),
        "uuid": session["uuid"],
        "version": version,
    }
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as c:
        r = await c.post(API_BASE + EXCHANGE_PATH, headers=_anonymous_headers(), json=body)
    r.raise_for_status()
    envelope = r.json()
    if not isinstance(envelope, dict) or envelope.get("code") not in (0, None):
        raise ValueError(f"LobsterAI exchange failed: {envelope.get('message') or envelope}")
    data = envelope.get("data") or {}
    token = data.get("accessToken") or ""
    if not token:
        raise ValueError("LobsterAI exchange response missing accessToken")
    expires_in = data.get("expiresIn")
    expires_at = int(time.time() * 1000 + expires_in * 1000) if isinstance(expires_in, (int, float)) else 0
    user = data.get("user") or {}
    return {
        "access_token": token,
        "refresh_token": data.get("refreshToken") or "",
        "expires_at": expires_at,
        "uuid": session["uuid"],
        "first_keyfrom": session["first_keyfrom"],
        "latest_keyfrom": body["latestKeyfrom"],
        "user_id": str(user.get("id") or ""),
        "nickname": str(user.get("nickname") or ""),
        "version": version,
    }


def is_expired(credential):
    expires_at = credential.get("expires_at") or 0
    return bool(expires_at) and time.time() * 1000 >= expires_at - 60_000


def is_refreshable(credential):
    return bool(credential.get("refresh_token"))


async def refresh(credential):
    version = credential.get("version") or await fetch_client_version()
    body = {
        "firstKeyfrom": credential.get("first_keyfrom") or "",
        "latestKeyfrom": credential.get("latest_keyfrom") or "",
        "version": version,
        "refreshToken": credential.get("refresh_token") or "",
    }
    if credential.get("uuid"):
        body["uuid"] = credential["uuid"]
    if credential.get("user_id"):
        body["userId"] = credential["user_id"]
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as c:
        r = await c.post(API_BASE + REFRESH_PATH, headers=_anonymous_headers(), json=body)
    r.raise_for_status()
    envelope = r.json()
    if not isinstance(envelope, dict) or envelope.get("code") not in (0, None):
        raise ValueError(f"LobsterAI refresh failed: {envelope.get('message') or envelope}")
    data = envelope.get("data") or {}
    token = data.get("accessToken") or ""
    if not token:
        raise ValueError("LobsterAI refresh response missing accessToken")
    expires_in = data.get("expiresIn")
    updated = dict(credential)
    updated["access_token"] = token
    if data.get("refreshToken"):
        updated["refresh_token"] = data["refreshToken"]
    if isinstance(expires_in, (int, float)):
        updated["expires_at"] = int(time.time() * 1000 + expires_in * 1000)
    updated["version"] = version
    return updated


async def ensure_fresh(credential):
    if is_expired(credential) and is_refreshable(credential):
        try:
            return await refresh(credential)
        except Exception:
            return credential
    return credential


async def list_models(credential):
    version = credential.get("version") or FALLBACK_VERSION
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as c:
        r = await c.get(API_BASE + MODELS_PATH, headers=chat_headers(credential["access_token"], version, sse=False))
    r.raise_for_status()
    envelope = r.json()
    rows = envelope.get("data") if isinstance(envelope, dict) else None
    if isinstance(rows, dict):
        rows = rows.get("models") or rows.get("list") or []
    if not isinstance(rows, list):
        return []
    out = []
    for item in rows:
        if isinstance(item, dict) and item.get("id"):
            out.append(str(item["id"]))
        elif isinstance(item, str):
            out.append(item)
    return out


def parse_credential(raw):
    data = json.loads(raw) if isinstance(raw, str) and raw.strip().startswith("{") else None
    if not isinstance(data, dict) or not data.get("access_token"):
        raise ValueError("LobsterAI credential format invalid")
    return data


def credential_json(credential):
    return json.dumps(credential, ensure_ascii=False)
