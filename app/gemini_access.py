"""Gemini (Cloud Code) account channel: Google OAuth + CloudCode private inference.

Ported from dsh-our-free-model vendor/channel-pack/src/gemini-*.ts (MIT).
Contract highlights (live-verified in that source):
- OAuth form MUST carry client_secret; listen on the port BEFORE opening the URL; state must be checked.
- Inference: POST {endpoint}/v1internal:streamGenerateContent?alt=sse (SSE only).
- Identity headers are frozen verbatim; never send x-goog-api-key / x-goog-api-client.
- Project id: loadCodeAssist probe with the byte-exact body '{"metadata":{"ideType":"ANTIGRAVITY"}}'.
"""
from __future__ import annotations

import hashlib
import json
import time
from urllib.parse import urlencode

import httpx

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v2/userinfo"
DEFAULT_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
DEFAULT_CLIENT_SECRET = "GOCSPX-" "K58FWR486LdLJ1mLB8sXC4z6qDAf"
CALLBACK_HOST = "localhost"
CALLBACK_PATH = "/oauth-callback"
DEFAULT_PORT = 8845
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
]
ENDPOINTS = ["https://daily-cloudcode-pa.googleapis.com", "https://daily-cloudcode-pa.sandbox.googleapis.com"]
STREAM_PATH = "/v1internal:streamGenerateContent?alt=sse"
LOAD_CODE_ASSIST_PATH = "/v1internal:loadCodeAssist"
LOAD_CODE_ASSIST_BODY = '{"metadata":{"ideType":"ANTIGRAVITY"}}'
IDENTITY_HEADERS = {
    "User-Agent": "antigravity/4.3.0 (cmdc-pak)",
    "x-client-name": "antigravity",
    "x-client-version": "4.3.0",
    "x-machine-id": "cmdc-pak",
    "x-vscode-sessionid": "proxy",
}
DEFAULT_PROJECT = "aicode-consumers"
REQUEST_TIMEOUT_S = 300
LOGIN_TIMEOUT_S = 360


def build_auth_url(port: int, state: str) -> str:
    redirect = "http://%s:%d%s" % (CALLBACK_HOST, port, CALLBACK_PATH)
    query = urlencode({
        "client_id": DEFAULT_CLIENT_ID,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
    return AUTH_ENDPOINT + "?" + query


async def exchange_code(code: str, port: int) -> dict:
    data = {
        "code": code,
        "client_id": DEFAULT_CLIENT_ID,
        "client_secret": DEFAULT_CLIENT_SECRET,
        "redirect_uri": "http://%s:%d%s" % (CALLBACK_HOST, port, CALLBACK_PATH),
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(TOKEN_ENDPOINT, data=data)
    r.raise_for_status()
    token = r.json()
    credential = {
        "access_token": token.get("access_token", ""),
        "refresh_token": token.get("refresh_token", ""),
        "expires_at": int(time.time() * 1000 + token.get("expires_in", 3600) * 1000),
        "email": "",
        "project": "",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            u = await client.get(USERINFO_ENDPOINT, headers={"Authorization": "Bearer " + credential["access_token"]})
        if u.status_code == 200:
            credential["email"] = u.json().get("email", "")
    except Exception:
        pass
    return credential


async def refresh(credential: dict) -> dict:
    if not credential.get("refresh_token"):
        raise ValueError("credential has no refresh_token; relogin required")
    data = {
        "refresh_token": credential["refresh_token"],
        "client_id": DEFAULT_CLIENT_ID,
        "client_secret": DEFAULT_CLIENT_SECRET,
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(TOKEN_ENDPOINT, data=data)
    r.raise_for_status()
    token = r.json()
    updated = dict(credential)
    updated["access_token"] = token.get("access_token", "")
    updated["expires_at"] = int(time.time() * 1000 + token.get("expires_in", 3600) * 1000)
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
    headers = dict(IDENTITY_HEADERS)
    headers["Authorization"] = "Bearer " + credential["access_token"]
    headers["Content-Type"] = "application/json"
    return headers


async def resolve_project(credential: dict) -> str:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(ENDPOINTS[0] + LOAD_CODE_ASSIST_PATH, headers=_headers(credential), content=LOAD_CODE_ASSIST_BODY)
        if r.status_code == 200:
            global_obj = r.json().get("currentGenCodeAssistGlobal") or {}
            project = global_obj.get("cloudaicompanionProject") or ""
            if project:
                return project
    except Exception:
        pass
    return credential.get("project") or DEFAULT_PROJECT


def build_stream_request(openai_body: dict, model: str, project: str, session_seed: str):
    from .protocols import openai_to_gemini
    from .account_transport import text_messages, validate_request
    validate_request("access_gemini", openai_body)
    native = openai_to_gemini(text_messages(openai_body))
    for tool in native.get("tools", []):
        if "function_declarations" in tool:
            tool["functionDeclarations"] = tool.pop("function_declarations")
    session_id = hashlib.sha256(("%s:%s" % (project, session_seed)).encode()).hexdigest()[:32]
    native["sessionId"] = session_id
    envelope = {
        "model": model,
        "project": project,
        "requestId": hashlib.sha1(str(time.time()).encode()).hexdigest()[:16],
        "request": native,
        "userAgent": "antigravity",
    }
    return ENDPOINTS[0] + STREAM_PATH, envelope


def sse_chunk_to_openai(line: str, model: str):
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        event = json.loads(payload)
    except ValueError:
        return None
    candidates = event.get("candidates") or []
    delta = {}
    finish = None
    if candidates:
        for part in (candidates[0].get("content") or {}).get("parts") or []:
            if isinstance(part.get("text"), str):
                delta["content"] = (delta.get("content") or "") + part["text"]
        reason = str(candidates[0].get("finishReason") or "").lower()
        if reason:
            finish = "length" if reason == "max_tokens" else "stop"
    usage_meta = event.get("usageMetadata") or {}
    chunk = {
        "id": "gemini-" + model,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta if delta else {}, "finish_reason": finish}],
    }
    if usage_meta:
        chunk["usage"] = {
            "prompt_tokens": usage_meta.get("promptTokenCount", 0),
            "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
            "total_tokens": usage_meta.get("totalTokenCount", 0),
        }
    return chunk
