"""Account-channel aggregation endpoint powering the overview "free-tier channels" panel."""
from fastapi import APIRouter, Request

from .admin_api import require_admin
from .database import query
from .gateway import channel_breaker_state

router = APIRouter(prefix="/api/account-channels")

# family -> 渠道 type（渠道表里的协议类型）
FAMILY_TYPES = {
    "opencode_free": "opencode_free",
    "lobsterai": "lobsterai",
    "gemini": "access_gemini",
    "minimax": "access_minimax",
    "cline": "access_cline",
    "qoder": "access_qoder",
    "trae": "access_trae",
}

FAMILIES = {
    "opencode_free": {"name": "OpenCode \u514d\u8d39\u6c60", "vendor": "opencode.ai", "login": "none",
                      "flow": "none",
                      "desc": "\u533f\u540d\u514d\u8d39\u989d\u5ea6\uff0c\u65e0\u9700\u8d26\u53f7\uff1b\u6309\u51fa\u53e3 IP \u8ba1\u91cf\u3002"},
    "lobsterai": {"name": "LobsterAI (\u6709\u9053)", "vendor": "youdao.com", "login": "oauth",
                  "flow": "browser",
                  "desc": "\u6709\u9053\u8d26\u53f7 OAuth \u767b\u5f55\uff0c\u4e2a\u4eba\u514d\u8d39\u989d\u5ea6\u3002"},
    "gemini": {"name": "Gemini Cloud Code (Google)", "vendor": "google.com", "login": "oauth",
               "flow": "browser",
               "desc": "Google \u8d26\u53f7 OAuth\uff1b\u8d70 Cloud Code IDE \u514d\u8d39\u989d\u5ea6\u3002"},
    "minimax": {"name": "MiniMax Code", "vendor": "minimax.cn", "login": "device",
                "flow": "device",
                "desc": "\u8bbe\u5907\u7801\u767b\u5f55\uff1b\u6bcf\u65e5\u7b7e\u5230\u9886\u79ef\u5206\u6362\u989d\u5ea6\u3002"},
    "cline": {"name": "Cline", "vendor": "cline.bot", "login": "device",
              "flow": "device",
              "desc": "WorkOS \u8bbe\u5907\u7801\u767b\u5f55\uff1b\u5176\u81ea\u6709\u7f51\u5173\u63a8\u7406\u3002"},
    "qoder": {"name": "Qoder (\u963f\u91cc)", "vendor": "qoder.com", "login": "device", "flow": "device",
              "desc": "\u963f\u91cc Qoder \u8bbe\u5907\u7801\u767b\u5f55\uff08\u542b WASM \u7b7e\u540d\uff0c\u5f53\u524d\u4ec5\u9aa8\u67b6\uff09\u3002", "skeleton": True},
    "trae": {"name": "TRAE (\u5b57\u8282)", "vendor": "trae.cn", "login": "oauth", "flow": "device",
             "desc": "\u5b57\u8282 TRAE \u79c1\u6709\u534f\u8bae\uff08\u5f53\u524d\u4ec5\u9aa8\u67b6\uff09\u3002", "skeleton": True},
}


@router.get("")
def list_account_channels(request: Request):
    require_admin(request)
    rows = query("SELECT id, name, type, status, models FROM channels WHERE type IN ('opencode_free','lobsterai','access_gemini','access_minimax','access_cline','access_qoder','access_trae') ORDER BY type, id")
    cards = []
    for family, meta in FAMILIES.items():
        own = [r for r in rows if r["type"] == FAMILY_TYPES.get(family, family)]
        model_count = 0
        for r in own:
            model_count += len([m for m in str(r["models"] or "").split(",") if m.strip()])
        card = {
            "family": family,
            "name": meta["name"],
            "vendor": meta["vendor"],
            "login": meta["login"],
            "desc": meta["desc"],
            "connected": bool(own),
            "enabled": any(r["status"] == 1 for r in own),
            "accounts": len(own),
            "models": model_count,
            "channels": [{"id": r["id"], "name": r["name"], "status": r["status"],
                          "breaker": channel_breaker_state(r["id"])} for r in own],
        }
        cards.append(card)
    return {"cards": cards}


@router.get("/free-models")
async def free_models(request: Request):
    """拉取 OpenCode 免费池的模型清单（免登录，与网关转发同一指纹协议）。"""
    require_admin(request)
    import httpx
    from . import opencode_free
    session = opencode_free.mint_session_id()
    headers = opencode_free.gateway_headers(session, stream=False)
    headers["accept"] = "application/json"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(opencode_free.OPENCODE_BASE + opencode_free.LISTING_PATH, headers=headers)
        if r.status_code != 200:
            return {"ok": False, "error": f"上游 HTTP {r.status_code}", "models": []}
        data = r.json().get("data") or []
        models = [item.get("id") for item in data if isinstance(item, dict) and item.get("id")]
        return {"ok": True, "models": models}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200], "models": []}



# ---------------- 统一登录：浏览器回调流（lobsterai / gemini）与设备码流（minimax / cline） ----------------
import asyncio  # noqa: E402
import secrets as _secrets  # noqa: E402
import time as _time  # noqa: E402

_LOGIN: dict[str, dict] = {}


@router.post("/login/start")
async def login_start(request: Request):
    require_admin(request)
    from .admin_api import body_json
    data = await body_json(request)
    family = str(data.get("family", ""))
    meta = FAMILIES.get(family)
    if meta is None:
        from fastapi import HTTPException
        raise HTTPException(404, "未知反代家族: " + family)
    if meta.get("skeleton"):
        return {"ok": False, "error": "该渠道协议尚未完成移植（骨架）"}
    channel_id = data.get("channel_id")
    state = _secrets.token_urlsafe(24)
    record = {"status": "waiting", "error": "", "family": family}
    _LOGIN[state] = record

    if meta["flow"] == "device":
        if family == "minimax":
            from . import minimax_access
            try:
                dev = await minimax_access.start_device_login()
            except Exception as exc:
                _LOGIN.pop(state, None)
                return {"ok": False, "error": "发起设备码失败: %s" % str(exc)[:200]}
            record.update(device_code=dev.get("device_code", ""), user_code=dev.get("user_code", ""))
            uri = dev.get("verification_uri") or dev.get("verification_uri_complete") or ""
            if dev.get("user_code") and uri and "user_code" not in uri:
                uri = uri.rstrip("/") + "?user_code=" + dev["user_code"]

            async def device_loop():
                deadline = _time.time() + 600
                while _time.time() < deadline and state in _LOGIN:
                    try:
                        result = await minimax_access.poll_token(record["device_code"])
                    except Exception as exc:
                        record["status"] = "error"
                        record["error"] = str(exc)[:300]
                        return
                    if not result.get("pending"):
                        record["credential"] = result
                        record["status"] = "credential"
                        return
                    await asyncio.sleep(5)
                if state in _LOGIN and record["status"] == "waiting":
                    record["status"] = "error"
                    record["error"] = "登录超时（10 分钟）"

            asyncio.get_running_loop().create_task(device_loop())
            return {"ok": True, "state": state, "verify_url": uri, "user_code": record.get("user_code", "")}

        if family == "cline":
            from . import cline_access
            try:
                dev = await cline_access.start_device_login()
            except Exception as exc:
                _LOGIN.pop(state, None)
                return {"ok": False, "error": "发起设备码失败: %s" % str(exc)[:200]}
            record.update(device_code=dev.get("device_code", ""), user_code=dev.get("user_code", ""))

            async def cline_loop():
                deadline = _time.time() + 300
                while _time.time() < deadline and state in _LOGIN:
                    try:
                        result = await cline_access.poll_token(record["device_code"])
                    except Exception as exc:
                        record["status"] = "error"
                        record["error"] = str(exc)[:300]
                        return
                    if not result.get("pending"):
                        record["credential"] = result
                        record["status"] = "credential"
                        return
                    await asyncio.sleep(5)
                if state in _LOGIN and record["status"] == "waiting":
                    record["status"] = "error"
                    record["error"] = "登录超时（5 分钟）"

            asyncio.get_running_loop().create_task(cline_loop())
            return {"ok": True, "state": state, "verify_url": dev.get("verification_uri", ""), "user_code": record.get("user_code", "")}

    # 浏览器回调流
    port_holder = {}

    def run_callback_server():
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import urlparse, parse_qs

        class Callback(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                expected = "/auth/callback" if family == "lobsterai" else "/oauth-callback"
                if parsed.path != expected:
                    self.send_response(404)
                    self.end_headers()
                    return
                qs = parse_qs(parsed.query)
                code = (qs.get("code") or [""])[0]
                got_state = (qs.get("state") or [""])[0]
                body = "<html><body><h3>OK - return to console.</h3></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body.encode())))
                self.end_headers()
                self.wfile.write(body.encode())
                if got_state == state and code and not record.get("code"):
                    record["code"] = code

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
        port_holder["port"] = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        deadline = _time.time() + 600
        while _time.time() < deadline and state in _LOGIN and not record.get("code"):
            _time.sleep(0.5)
        server.shutdown()
        server.server_close()

    async def orchestrate():
        await asyncio.to_thread(run_callback_server)
        code = record.get("code")
        if not code:
            record["status"] = "error"
            record["error"] = "登录超时（10 分钟未完成浏览器授权）"
            return
        record["status"] = "exchanging"
        try:
            if family == "lobsterai":
                from . import lobsterai as lb
                session = lb.new_login_session()
                credential = await lb.exchange_auth_code(code, session)
            elif family == "gemini":
                from . import gemini_access
                credential = await gemini_access.exchange_code(code, port_holder.get("port", 8845))
            else:
                raise ValueError("unsupported browser family")
            record["credential"] = credential
            record["status"] = "credential"
        except Exception as exc:
            record["status"] = "error"
            record["error"] = str(exc)[:300]

    asyncio.get_running_loop().create_task(orchestrate())
    for _ in range(50):
        if "port" in port_holder:
            break
        await asyncio.sleep(0.1)
    port = port_holder.get("port", 0)
    if family == "lobsterai":
        from . import lobsterai as lb
        login_url = lb.build_login_url(port, state)
    else:
        from . import gemini_access
        login_url = gemini_access.build_auth_url(port, state)
    return {"ok": True, "state": state, "login_url": login_url}


@router.post("/login/bind")
async def login_bind(request: Request):
    """把已完成的登录凭据写进指定渠道。"""
    require_admin(request)
    from .admin_api import body_json
    from .database import execute
    data = await body_json(request)
    state = str(data.get("state", ""))
    channel_id = data.get("channel_id")
    record = _LOGIN.get(state)
    if not record or record.get("status") != "credential":
        from fastapi import HTTPException
        raise HTTPException(400, "登录会话未完成或已过期")
    credential = record["credential"]
    import json as _json
    execute("UPDATE channels SET api_key=?, status=1 WHERE id=?",
            (_json.dumps(credential, ensure_ascii=False), int(channel_id)))
    nickname = credential.get("email") or credential.get("nickname") or ""
    _LOGIN.pop(state, None)
    return {"ok": True, "nickname": nickname}


@router.get("/login/poll")
async def login_poll(request: Request, state: str = ""):
    require_admin(request)
    record = _LOGIN.get(state)
    if record is None:
        from fastapi import HTTPException
        raise HTTPException(404, "登录会话不存在或已结束")
    out = {"status": record["status"]}
    if record.get("error"):
        out["error"] = record["error"]
    if record.get("user_code"):
        out["user_code"] = record["user_code"]
    return out


@router.get("/insights")
def insights(request: Request, days: int = 30):
    """Data dashboard: channel x model token pivot (requests/success/tokens/latency/cost)."""
    require_admin(request)
    from .database import query as _q
    span = max(1, min(90, days))
    since = "datetime('now', '-" + str(span) + " days')"
    keys = ["requests", "ok", "pt", "ct", "cost", "avg_ms"]
    by_channel = _q(
        "SELECT channel_id, channel_name, COUNT(*) requests,"
        " SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) ok,"
        " SUM(COALESCE(prompt_tokens,0)) pt, SUM(COALESCE(completion_tokens,0)) ct,"
        " SUM(COALESCE(cost,0)) cost, AVG(COALESCE(latency_ms,0)) avg_ms"
        " FROM request_logs WHERE created_at >= " + since +
        " GROUP BY channel_id, channel_name ORDER BY requests DESC LIMIT 50")
    by_model = _q(
        "SELECT model, COUNT(*) requests,"
        " SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) ok,"
        " SUM(COALESCE(prompt_tokens,0)) pt, SUM(COALESCE(completion_tokens,0)) ct,"
        " SUM(COALESCE(cost,0)) cost, AVG(COALESCE(latency_ms,0)) avg_ms"
        " FROM request_logs WHERE created_at >= " + since +
        " GROUP BY model ORDER BY requests DESC LIMIT 50")
    total = _q(
        "SELECT COUNT(*) requests,"
        " SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) ok,"
        " SUM(COALESCE(prompt_tokens,0)) pt, SUM(COALESCE(completion_tokens,0)) ct,"
        " SUM(COALESCE(cost,0)) cost, AVG(COALESCE(latency_ms,0)) avg_ms"
        " FROM request_logs WHERE created_at >= " + since)[0]
    def plain(r):
        return {k: r[k] for k in keys}
    return {
        "days": span,
        "total": plain(total),
        "by_channel": [dict(r) for r in by_channel],
        "by_model": [dict(r) for r in by_model],
    }


@router.post("/signin/{channel_id}")
async def channel_signin(channel_id: int, request: Request):
    """每日签到领额度（当前支持 MiniMax Code）。"""
    require_admin(request)
    from .database import query_one as _one
    row = _one("SELECT id, type, api_key FROM channels WHERE id=?", (channel_id,))
    if row is None:
        from fastapi import HTTPException
        raise HTTPException(404, "渠道不存在")
    if row["type"] != "access_minimax":
        return {"ok": False, "error": "该渠道不支持签到"}
    import json as _json
    from . import minimax_access
    try:
        credential = _json.loads(row["api_key"])
        status = await minimax_access.signin_status(credential)
        if not status.get("ok"):
            return {"ok": False, "error": status.get("error", "查询签到状态失败")}
        if status.get("signedIn") or status.get("todaySignedIn") or status.get("signed"):
            return {"ok": True, "already": True, "detail": status}
        result = await minimax_access.signin_claim(credential)
        return {"ok": bool(result.get("ok")), "detail": result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
