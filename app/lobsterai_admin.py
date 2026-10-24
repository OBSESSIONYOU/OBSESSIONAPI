"""LobsterAI 渠道管理端点：浏览器登录流程与凭据落库。"""
import asyncio
import secrets

from fastapi import APIRouter, HTTPException, Request

from . import lobsterai
from .admin_api import body_json, require_admin
from .database import execute, query_one

router = APIRouter(prefix="/api/channels/lobsterai")

# 进程内登录会话：state -> {future(credential), session}
_PENDING: dict[str, dict] = {}


def _SESSIONS_CLEANUP(state):
    _PENDING.pop(state, None)


@router.post("/login/start")
async def login_start(request: Request):
    require_admin(request)
    data = await body_json(request)
    channel_id = data.get("channel_id")
    row = query_one("SELECT id FROM channels WHERE id=?", (channel_id,)) if channel_id else None
    if row is None:
        raise HTTPException(404, "渠道不存在")
    state = secrets.token_urlsafe(24)
    session = lobsterai.new_login_session()
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    _PENDING[state] = {"future": future, "session": session, "channel_id": int(channel_id)}

    async def run_server():
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Callback(BaseHTTPRequestHandler):
            def do_GET(self):
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(self.path)
                if parsed.path != lobsterai.CALLBACK_PATH:
                    self.send_response(404); self.end_headers(); return
                qs = parse_qs(parsed.query)
                code = (qs.get("code") or [""])[0]
                got_state = (qs.get("state") or [""])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                body = "<h3>\u767b\u5f55\u6210\u529f\uff0c\u53ef\u4ee5\u5173\u95ed\u6b64\u9875\u9762\u5e76\u56de\u5230\u63a7\u5236\u53f0\u3002</h3>"
                self.send_header("Content-Length", str(len(body.encode())))
                self.end_headers()
                self.wfile.write(body.encode())
                pending = _PENDING.get(got_state)
                if pending and code and not pending["future"].done():
                    pending["future"].set_result(code)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
        port = server.server_address[1]
        _PENDING[state]["port"] = port
        _PENDING[state]["ready"].set()
        server.handle_request()  # 处理一次回调即退出
        server.handle_request()  # 某些浏览器会多发 favicon 请求，容忍一次
        server.server_close()

    ready = asyncio.Event()
    _PENDING[state]["ready"] = ready
    task = asyncio.create_task(asyncio.to_thread(run_server))
    await ready.wait()
    login_url = lobsterai.build_login_url(_PENDING[state]["port"], state)

    _PENDING[state]["status"] = "waiting"

    async def wait_result():
        try:
            code = await asyncio.wait_for(future, timeout=lobsterai.LOGIN_TIMEOUT_S if hasattr(lobsterai, "LOGIN_TIMEOUT_S") else 600)
            credential = await lobsterai.exchange_auth_code(code, session)
            execute("UPDATE channels SET api_key=?, status=1 WHERE id=?",
                    (lobsterai.credential_json(credential), channel_id))
            _PENDING[state]["status"] = "done"
            _PENDING[state]["nickname"] = credential.get("nickname") or credential.get("user_id") or ""
            return {"ok": True}
        except asyncio.TimeoutError:
            _PENDING[state]["status"] = "error"
            _PENDING[state]["error"] = "登录超时（10 分钟），请重试"
        except Exception as exc:  # noqa: BLE001
            _PENDING[state]["status"] = "error"
            _PENDING[state]["error"] = str(exc)[:300]
        finally:
            _PENDING.pop(state, None)
            task.cancel()

    asyncio.get_running_loop().create_task(wait_result())
    return {"login_url": login_url, "state": state}


@router.get("/login/poll")
async def login_poll(request: Request, state: str = ""):
    require_admin(request)
    pending = _PENDING.get(state)
    if not pending:
        raise HTTPException(404, "登录会话不存在或已结束")
    out = {"status": pending.get("status", "waiting")}
    if pending.get("error"):
        out["error"] = pending["error"]
    if pending.get("nickname"):
        out["nickname"] = pending["nickname"]
    if out["status"] in ("done", "error"):
        _SESSIONS_CLEANUP(state)
    return out
