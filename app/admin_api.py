"""管理后台 API：登录态 + 控制台所需的配置、渠道、模型与日志接口。"""
import csv
import io
import json
import re
import secrets
from datetime import timedelta
from urllib.parse import quote, urlparse, urlunparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .database import create_initial_admin, execute, iso, maintenance, new_api_key, new_token, query, query_one, hash_password, verify_password, utcnow
from .gateway import (
    _anthropic_error_message,
    _anthropic_to_openai,
    _chat_response_state,
    _http_client,
    _invalid_response_detail,
    _is_client_restricted,
    anthropic_headers,
    build_channel_headers,
    channel_header_config,
    classify_upstream_error,
    channel_breaker_state,
    header_names,
    is_channel_auto_disabled,
    is_channel_cooling,
    normalize_extra_headers,
    normalize_forward_headers,
    normalize_user_agent,
    normalize_anthropic_base_url,
    normalize_openai_base_url,
    recover_channel,
    upstream_error_message,
    upstream_model_for_channel,
    validate_channel_type,
)
from . import database, settings_store
from .secrets import encrypt_secret
from .protocols import (
    channel_protocol,
    gemini_to_openai,
    normalize_gemini_base_url,
    normalize_openai_responses_base_url,
    normalize_protocol,
    openai_to_gemini,
    openai_to_responses,
    infer_channel_name,
    recommended_protocol,
    responses_to_chat,
    safe_public_url,
)

router = APIRouter(prefix="/api")

SESSION_DAYS = 7
CHANNEL_TEST_MAX_TOKENS = 4096


def app_version() -> str:
    return "v1.0.0"


def _mask_key(value: str) -> str:
    value = str(value or "")
    return value if len(value) <= 8 else f"{value[:5]}{'•' * 8}{value[-4:]}"


def _mask_header_value(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 4:
        return "••••"
    return f"{value[:2]}{'•' * 8}{value[-2:]}"


def _mask_proxy(value: str) -> str:
    """代理地址可能带凭据，列表只返回脱敏后的形态。"""
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value)
    except ValueError:
        return "••••"
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{'•' * 6}:{'•' * 6}@{host}{port}"
    return value


def _public_log(row) -> dict:
    result = dict(row)
    for field in ("cost", "user_id", "api_key_id"):
        result.pop(field, None)
    return result


def _request_rollup(where="1=1", args=()):
    """按客户端 request_id 聚合；原始日志仍保留每次上游尝试。"""
    return query(
        f"""SELECT request_id, substr(MIN(created_at),1,10) day,
                   COUNT(*) attempts,
                   COALESCE(SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0)),0) tokens,
                   COALESCE(SUM(CASE WHEN usage_source='upstream' THEN COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0) ELSE 0 END),0) upstream_tokens,
                   COALESCE(SUM(CASE WHEN usage_source='estimated' THEN COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0) ELSE 0 END),0) estimated_tokens,
                   COALESCE(SUM(CASE WHEN usage_source='synthetic' THEN COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0) ELSE 0 END),0) synthetic_tokens,
                   COALESCE(SUM(CASE WHEN usage_source IN ('upstream','estimated','synthetic') THEN 0 ELSE COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0) END),0) unknown_tokens,
                   MAX(CASE WHEN status='success' THEN 1 ELSE 0 END) ok,
                   MAX(COALESCE(latency_ms,0)) client_latency_ms
            FROM request_logs WHERE {where} GROUP BY request_id""",
        args,
    )


def _percentile(sorted_values, ratio: float):
    """对已排序列表取分位（最近秩法），空列表返回 None。"""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return int(sorted_values[0])
    index = int(round((len(sorted_values) - 1) * ratio))
    return int(sorted_values[max(0, min(index, len(sorted_values) - 1))])


def _rollup_totals(rows) -> dict:
    requests = len(rows)
    attempts = sum(int(row["attempts"] or 0) for row in rows)
    ok = sum(int(row["ok"] or 0) for row in rows)
    retries = sum(max(int(row["attempts"] or 0) - 1, 0) for row in rows)
    tokens = sum(int(row["tokens"] or 0) for row in rows)
    upstream_tokens = sum(int(row["upstream_tokens"] or 0) for row in rows)
    estimated_tokens = sum(int(row["estimated_tokens"] or 0) for row in rows)
    synthetic_tokens = sum(int(row["synthetic_tokens"] or 0) for row in rows)
    unknown_tokens = sum(int(row["unknown_tokens"] or 0) for row in rows)
    successful_latencies = sorted(int(row["client_latency_ms"] or 0) for row in rows if row["ok"])
    return {
        "requests": requests,
        "attempts": attempts,
        "retries": retries,
        "ok": ok,
        "failed_requests": max(0, requests - ok),
        "tokens": tokens,
        "upstream_tokens": upstream_tokens,
        "estimated_tokens": estimated_tokens,
        "synthetic_tokens": synthetic_tokens,
        "unknown_tokens": unknown_tokens,
        "avg_latency_ms": int(sum(successful_latencies) / len(successful_latencies)) if successful_latencies else None,
        "p95_latency_ms": _percentile(successful_latencies, 0.95),
        "p99_latency_ms": _percentile(successful_latencies, 0.99),
        "max_latency_ms": successful_latencies[-1] if successful_latencies else None,
    }


def _log_filters(params):
    where, args = ["1=1"], []
    q = str(params.get("q", "")).strip()
    if q:
        where.append("(request_id LIKE ? OR model LIKE ?)")
        args.extend([f"%{q}%", f"%{q}%"])
    if params.get("request_id"):
        where.append("request_id=?")
        args.append(params["request_id"])
    if params.get("model"):
        where.append("model=?")
        args.append(params["model"])
    if params.get("status"):
        where.append("status=?")
        args.append(params["status"])
    if params.get("status_code"):
        if params["status_code"] == "5xx":
            where.append("status_code BETWEEN 500 AND 599")
        else:
            try:
                where.append("status_code=?")
                args.append(int(params["status_code"]))
            except ValueError as exc:
                raise HTTPException(400, "status_code 必须是数字或 5xx") from exc
    if params.get("error_category"):
        where.append("error_category=?")
        args.append(params["error_category"])
    if params.get("username"):
        where.append("username LIKE ?")
        args.append(f"%{params['username']}%")
    if params.get("channel_id"):
        where.append("channel_id=?")
        args.append(params["channel_id"])
    if params.get("since"):
        where.append("created_at>=?")
        args.append(params["since"])
    if params.get("until"):
        where.append("created_at<=?")
        args.append(params["until"])
    return " AND ".join(where), tuple(args)


def current_user(request: Request):
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        raise HTTPException(401, "未登录")
    row = query_one(
        """SELECT s.token, u.* FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token = ? AND s.expires_at > ?""",
        (token, iso(utcnow())),
    )
    if row is None or row["status"] != 1:
        raise HTTPException(401, "登录已失效，请重新登录")
    return row


def require_admin(request: Request):
    user = current_user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


async def body_json(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "请求体不是合法的 JSON")
    if not isinstance(data, dict):
        raise HTTPException(400, "请求体顶层必须是 JSON 对象")
    return data


# ------------------------------------------------------------------ 登录
@router.get("/setup/status")
async def setup_status():
    return {"needs_setup": query_one("SELECT COUNT(*) c FROM users")["c"] == 0}


@router.post("/setup")
async def setup(request: Request):
    data = await body_json(request)
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    if len(username) < 3:
        raise HTTPException(400, "用户名至少需要 3 个字符")
    if len(password) < 12:
        raise HTTPException(400, "密码至少需要 12 位")
    user_id = create_initial_admin(username, password)
    if user_id is None:
        raise HTTPException(409, "初始化已完成，请直接登录")
    token = new_token()
    execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)",
            (token, user_id, iso(utcnow() + timedelta(days=SESSION_DAYS))))
    return {"token": token, "user": {"id": user_id, "username": username, "role": "admin"}}


@router.post("/login")
async def login(request: Request):
    data = await body_json(request)
    row = query_one("SELECT * FROM users WHERE username=?", (data.get("username", ""),))
    if row is None or not verify_password(data.get("password", ""), row["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    if row["status"] != 1:
        raise HTTPException(403, "该账户已被禁用")
    token = new_token()
    execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)",
            (token, row["id"], iso(utcnow() + timedelta(days=SESSION_DAYS))))
    return {"token": token, "user": {"id": row["id"], "username": row["username"], "role": row["role"]}}


@router.post("/logout")
async def logout(request: Request):
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    execute("DELETE FROM sessions WHERE token=?", (token,))
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    user = current_user(request)
    return {"id": user["id"], "username": user["username"], "role": user["role"], "quota": user["quota"]}


# ------------------------------------------------------------------ 总览
@router.get("/dashboard")
async def dashboard(request: Request):
    require_admin(request)
    params = request.query_params
    days = 30 if params.get("range") == "30d" else 7
    now = utcnow()
    today = iso(now)[:10]
    start = iso(now - timedelta(days=days - 1))[:10]
    rollups = _request_rollup("created_at >= ?", (start,))
    daily_map = {}
    for row in rollups:
        bucket = daily_map.setdefault(row["day"], {"requests": 0, "tokens": 0, "upstream_tokens": 0, "estimated_tokens": 0, "synthetic_tokens": 0, "unknown_tokens": 0, "ok": 0, "attempts": 0, "retries": 0, "latencies": []})
        attempts = int(row["attempts"] or 0)
        bucket["requests"] += 1
        bucket["tokens"] += int(row["tokens"] or 0)
        bucket["upstream_tokens"] += int(row["upstream_tokens"] or 0)
        bucket["estimated_tokens"] += int(row["estimated_tokens"] or 0)
        bucket["synthetic_tokens"] += int(row["synthetic_tokens"] or 0)
        bucket["unknown_tokens"] += int(row["unknown_tokens"] or 0)
        bucket["ok"] += int(row["ok"] or 0)
        bucket["attempts"] += attempts
        bucket["retries"] += max(attempts - 1, 0)
        if row["ok"]:
            bucket["latencies"].append(int(row["client_latency_ms"] or 0))
    series = []
    for offset in range(days - 1, -1, -1):
        day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        row = daily_map.get(day, {})
        series.append({
            "date": day,
            "requests": int(row.get("requests", 0)),
            "tokens": int(row.get("tokens", 0)),
            "upstream_tokens": int(row.get("upstream_tokens", 0)),
            "estimated_tokens": int(row.get("estimated_tokens", 0)),
            "synthetic_tokens": int(row.get("synthetic_tokens", 0)),
            "unknown_tokens": int(row.get("unknown_tokens", 0)),
            "attempts": int(row.get("attempts", 0)),
            "retries": int(row.get("retries", 0)),
        })
    today_stats = _rollup_totals(_request_rollup("created_at >= ?", (today,)))
    previous_start = iso(now - timedelta(days=1))[:10]
    previous_stats = _rollup_totals(_request_rollup("created_at >= ? AND created_at < ?", (previous_start, today)))
    active_channels = query_one("SELECT COUNT(*) c FROM channels WHERE status=1")["c"]
    total_channels = query_one("SELECT COUNT(*) c FROM channels")["c"]
    cooling_channels = sum(
        is_channel_cooling(row["id"])
        for row in query("SELECT id FROM channels WHERE status=1")
    )
    auto_disabled_channels = sum(
        is_channel_auto_disabled(row["id"])
        for row in query("SELECT id FROM channels WHERE status=1")
    )
    published_models = query_one("SELECT COUNT(*) c FROM models WHERE status=1")["c"]
    attention = query(
        """SELECT model, channel_id, channel_name, COUNT(DISTINCT request_id) failures,
                  COUNT(*) attempts,
                  COALESCE(MAX(error_category),'') error_category
           FROM request_logs WHERE status='error' AND created_at >= ?
           GROUP BY model, channel_id, channel_name ORDER BY failures DESC, MAX(id) DESC LIMIT 5""",
        (start,),
    )
    channel_health = await _channel_overview_rows(days)
    total = today_stats["requests"]
    ok = today_stats["ok"]
    metrics = {
        "today_requests": total,
        "today_attempts": today_stats["attempts"],
        "today_retries": today_stats["retries"],
        "success_rate": round(ok * 100 / total, 2) if total else None,
        "avg_latency_ms": today_stats["avg_latency_ms"],
        "today_tokens": today_stats["tokens"],
        "today_upstream_tokens": today_stats["upstream_tokens"],
        "today_estimated_tokens": today_stats["estimated_tokens"],
        "today_synthetic_tokens": today_stats["synthetic_tokens"],
        "today_unknown_tokens": today_stats["unknown_tokens"],
        "token_change": today_stats["tokens"] - previous_stats["tokens"],
        "p95_latency_ms": today_stats["p95_latency_ms"],
        "p99_latency_ms": today_stats["p99_latency_ms"],
        "max_latency_ms": today_stats["max_latency_ms"],
        "latency_scope": "成功客户端请求的总耗时",
    }
    model_health = [
        {
            "model": row["model"],
            "requests": int(row["requests"] or 0),
            "attempts": int(row["attempts"] or 0),
            "tokens": int(row["tokens"] or 0),
            "success_rate": round(int(row["ok"] or 0) * 100 / int(row["requests"]), 1) if row["requests"] else None,
            "avg_latency_ms": int(row["latency"] or 0) if row["ok"] else None,
        }
        for row in query(
            """SELECT model, COUNT(DISTINCT request_id) requests, COUNT(*) attempts,
                      SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) ok,
                      COALESCE(SUM(prompt_tokens+completion_tokens),0) tokens,
                      COALESCE(AVG(CASE WHEN status='success' THEN latency_ms END),0) latency
               FROM request_logs WHERE created_at >= ? GROUP BY model
               ORDER BY requests DESC LIMIT 10""",
            (start,),
        )
    ]
    return {
        "range": f"{days}d",
        "status_strip": {
            "service": "normal",
            "active_channels": active_channels,
            "total_channels": total_channels,
            "cooling_channels": cooling_channels,
            "auto_disabled_channels": auto_disabled_channels,
            "published_models": published_models,
        },
        "metrics": metrics,
        "series": series,
        "model_health": model_health,
        "attention": [
            {**dict(row), "jump": {"status": "error", "model": row["model"], "channel_id": row["channel_id"]}}
            for row in attention
        ],
        "channel_health": channel_health,
        # 兼容旧控制台字段，内容不包含金额。
        "cards": {
            "total_requests": sum(item["requests"] for item in series),
            "total_tokens": sum(item["tokens"] for item in series),
            "total_attempts": sum(item["attempts"] for item in series),
            "total_retries": sum(item["retries"] for item in series),
            "success_rate": metrics["success_rate"],
            "total_errors": total - ok,
            "avg_latency": metrics["avg_latency_ms"],
            "active_channels": active_channels,
            "total_channels": total_channels,
            "today_requests": metrics["today_requests"],
            "today_attempts": metrics["today_attempts"],
            "today_retries": metrics["today_retries"],
            "today_tokens": metrics["today_tokens"],
            "today_upstream_tokens": metrics["today_upstream_tokens"],
            "today_estimated_tokens": metrics["today_estimated_tokens"],
            "today_synthetic_tokens": metrics["today_synthetic_tokens"],
            "today_unknown_tokens": metrics["today_unknown_tokens"],
            "today_errors": total - ok,
        },
        "daily": series,
        "channels": channel_health,
        "errors_24h": [dict(row) for row in attention],
        "recent_logs": [_public_log(row) for row in query("SELECT * FROM request_logs ORDER BY id DESC LIMIT 6")],
    }


async def _channel_overview_rows(days: int = 7) -> list[dict]:
    since = iso(utcnow() - timedelta(days=max(1, days - 1)))
    channels = query("SELECT id,name,type,status,priority,weight FROM channels ORDER BY priority DESC, id")
    stats = {
        row["channel_id"]: row for row in query(
            """SELECT channel_id, COUNT(*) requests, COUNT(*) attempts,
                      COUNT(DISTINCT request_id) client_requests,
                      COALESCE(SUM(will_retry),0) retries,
                      SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) ok,
                      COALESCE(SUM(prompt_tokens+completion_tokens),0) tokens,
                      COALESCE(AVG(CASE WHEN status='success' THEN latency_ms END),0) latency
               FROM request_logs WHERE created_at >= ? AND channel_id IS NOT NULL GROUP BY channel_id""",
            (since,),
        )
    }
    total_requests = sum(row["requests"] for row in stats.values())
    rows = []
    for channel in channels:
        stat = stats.get(channel["id"])
        requests = int(stat["requests"] if stat else 0)
        ok = int(stat["ok"] if stat else 0)
        rate = round(ok * 100 / requests, 1) if requests else None
        latest_failure = query_one(
            "SELECT error_category,error_msg,created_at FROM request_logs WHERE channel_id=? AND status='error' ORDER BY id DESC LIMIT 1",
            (channel["id"],),
        )
        probe = query_one(
            "SELECT ok,latency_ms,status_code,error_category,error_msg,created_at FROM channel_probes WHERE channel_id=? ORDER BY id DESC LIMIT 1",
            (channel["id"],),
        )
        cooling = is_channel_cooling(channel["id"])
        auto_disabled = is_channel_auto_disabled(channel["id"])
        breaker = channel_breaker_state(channel["id"])
        if auto_disabled:
            status = "disabled"
        elif cooling:
            status = "cooling"
        elif not requests and not probe:
            status = "unknown"
        elif requests and rate is not None and rate < 50:
            status = "error"
        elif requests and rate is not None and rate < 90:
            status = "warning"
        elif (not requests and probe and not probe["ok"] and
              probe["error_category"] != "model_list_unavailable"):
            status = "error"
        elif not requests and probe and probe["error_category"] == "model_list_unavailable":
            status = "unknown"
        else:
            status = "normal"
        rows.append({
            "id": channel["id"], "name": channel["name"], "type": channel["type"],
            "status_enabled": bool(channel["status"]), "priority": channel["priority"],
            "requests": requests, "attempts": requests,
            "client_requests": int(stat["client_requests"] if stat else 0),
            "retries": int(stat["retries"] if stat else 0),
            "tokens": int(stat["tokens"] if stat else 0),
            "traffic_share": round(requests * 100 / total_requests, 1) if total_requests else None,
            "success_rate": rate, "avg_latency_ms": int(stat["latency"]) if stat and ok else None,
            "status": status, "cooling": cooling,
            "auto_disabled": auto_disabled,
            "disabled_reason": breaker["disabled_reason"],
            "recover_in_s": breaker["recover_in_s"],
            "consecutive_failures": breaker["consecutive_failures"],
            "cooldown_remaining_s": breaker["cooldown_remaining_s"],
            "last_failure": dict(latest_failure) if latest_failure else None,
            "probe": dict(probe) if probe else None,
        })
    return rows


@router.get("/channels/overview")
async def channels_overview(request: Request):
    require_admin(request)
    days = 30 if request.query_params.get("range") == "30d" else 7
    return {"range": f"{days}d", "items": await _channel_overview_rows(days)}


@router.post("/me/password")
async def change_password(request: Request):
    user = current_user(request)
    d = await body_json(request)
    if not verify_password(d.get("old_password", ""), user["password_hash"]):
        raise HTTPException(400, "旧密码不正确")
    new_password = str(d.get("new_password", ""))
    if len(new_password) < 12:
        raise HTTPException(400, "新密码至少需要 12 位")
    execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new_password), user["id"]))
    auth = request.headers.get("authorization", "")
    current_token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    execute("DELETE FROM sessions WHERE user_id=? AND token<>?", (user["id"], current_token))
    return {"ok": True}


# ------------------------------------------------------------------ 模型管理
@router.get("/models")
async def models_list(request: Request):
    require_admin(request)
    models = [dict(r) for r in query("SELECT * FROM models ORDER BY id")]
    channels = query(
        """SELECT id,name,type,models,model_mapping,priority,weight,status,
                  detected_protocol,last_protocol_check_at
           FROM channels ORDER BY priority DESC, id"""
    )
    for model in models:
        bindings = []
        for channel in channels:
            if model["model_name"] not in _model_names(channel["models"]).split(","):
                continue
            breaker = channel_breaker_state(channel["id"])
            probe = query_one(
                """SELECT ok,latency_ms,status_code,error_category,error_msg,created_at
                   FROM channel_probes WHERE channel_id=? ORDER BY id DESC LIMIT 1""",
                (channel["id"],),
            )
            enabled = bool(channel["status"])
            cooling = breaker["cooling"]
            bindings.append({
                "channel_id": channel["id"],
                "channel_name": channel["name"],
                "protocol": channel_protocol(channel),
                "upstream_model": upstream_model_for_channel(channel, model["model_name"]),
                "priority": channel["priority"],
                "weight": channel["weight"],
                "enabled": enabled,
                "cooling": cooling,
                "availability": "available" if enabled and not cooling else "cooling" if cooling else "disabled",
                "consecutive_failures": breaker["consecutive_failures"],
                "cooldown_remaining_s": breaker["cooldown_remaining_s"],
                "probe": dict(probe) if probe else None,
            })
        model["bindings"] = bindings
        model["channel_count"] = len(bindings)
        model["available_channel_count"] = sum(item["enabled"] and not item["cooling"] for item in bindings)
    return models


@router.post("/models")
async def models_create(request: Request):
    require_admin(request)
    d = await body_json(request)
    if not d.get("model_name"):
        raise HTTPException(400, "model_name 不能为空")
    try:
        rid = execute(
            "INSERT INTO models(model_name,display_name,vendor,input_price,output_price,status,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (d["model_name"], d.get("display_name") or d["model_name"], d.get("vendor", ""),
             float(d.get("input_price", 0)), float(d.get("output_price", 0)),
             1 if d.get("status", 1) else 0, iso(utcnow())))
    except Exception:
        raise HTTPException(409, "模型名已存在")
    return {"id": rid}


@router.put("/models/{mid}")
async def models_update(mid: int, request: Request):
    require_admin(request)
    d = await body_json(request)
    row = query_one("SELECT * FROM models WHERE id=?", (mid,))
    if row is None:
        raise HTTPException(404, "模型不存在")
    execute("UPDATE models SET display_name=?, vendor=?, input_price=?, output_price=?, status=? WHERE id=?",
            (d.get("display_name", row["display_name"]), d.get("vendor", row["vendor"]),
             float(d.get("input_price", row["input_price"])),
             float(d.get("output_price", row["output_price"])),
             1 if d.get("status", row["status"]) else 0, mid))
    return {"ok": True}


@router.delete("/models/{mid}")
async def models_delete(mid: int, request: Request):
    require_admin(request)
    execute("DELETE FROM models WHERE id=?", (mid,))
    return {"ok": True}


# ------------------------------------------------------------------ 渠道管理
def _model_names(value) -> str:
    return ",".join(dict.fromkeys(name.strip() for name in str(value or "").split(",") if name.strip()))


def _register_models(models: str) -> None:
    for name in models.split(","):
        if name:
            execute(
                "INSERT OR IGNORE INTO models(model_name,display_name,vendor,input_price,output_price,status,created_at)"
                " VALUES(?,?, '',0,0,1,?)",
                (name, name, iso(utcnow())),
            )


def _prune_unbound_models() -> None:
    """删除不再被任何启用渠道引用、且从未被用户定制过的自动注册模型。
    定制判定：display_name/vendor 非空，或价格非 0 —— 这类保留，避免误删用户手工配置。"""
    execute(
        """DELETE FROM models WHERE status=1
             AND display_name=model_name AND vendor=''
             AND (input_price IS NULL OR input_price=0)
             AND (output_price IS NULL OR output_price=0)
             AND NOT EXISTS (
               SELECT 1 FROM channels ch WHERE ch.status=1
                 AND instr(','||replace(ch.models,' ','')||',', ','||models.model_name||',') > 0
             )"""
    )


def _channel_base_url(channel_type: str, value: str) -> str:
    try:
        channel_type = validate_channel_type(channel_type)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if channel_type == "mock":
        return str(value or "").strip()
    if channel_type == "opencode_free":
        # 免费通道固定指向 opencode.ai；base_url 仅作展示，可留空
        return "https://opencode.ai"
    if channel_type == "lobsterai":
        return "https://lobsterai-server.youdao.com"
    if channel_type == "access_gemini":
        return "https://daily-cloudcode-pa.googleapis.com"
    if channel_type == "access_minimax":
        return "https://agent.minimax.cn"
    if channel_type == "access_cline":
        return "https://api.cline.bot"
    try:
        if channel_type == "anthropic":
            return normalize_anthropic_base_url(value)
        if channel_type == "openai_responses":
            return normalize_openai_responses_base_url(value)
        if channel_type == "gemini":
            return normalize_gemini_base_url(value)
        return normalize_openai_base_url(value)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _channel_name(base_url: str) -> str:
    return infer_channel_name(base_url)


def _recommended_protocol(channel_type: str, base_url: str) -> str:
    if channel_type != "auto":
        return channel_type
    return recommended_protocol(base_url)


def _channel_header_fields(data: dict, row=None) -> tuple[str, str, str]:
    try:
        extra_value = data["extra_headers"] if "extra_headers" in data else (row["extra_headers"] if row else "{}")
        forward_value = data["forward_headers"] if "forward_headers" in data else (row["forward_headers"] if row else "[]")
        user_agent_value = data["user_agent"] if "user_agent" in data else (row["user_agent"] if row else "")
        extra = normalize_extra_headers(extra_value)
        forward = normalize_forward_headers(forward_value)
        user_agent = normalize_user_agent(user_agent_value)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return (
        json.dumps(extra, ensure_ascii=False, separators=(",", ":")),
        json.dumps(forward, ensure_ascii=False, separators=(",", ":")),
        user_agent,
    )


def _safe_header_names(row) -> list[str]:
    try:
        extra, _, _ = channel_header_config(row)
        return sorted(extra)
    except (TypeError, ValueError):
        return []


def _public_url(value: str) -> str:
    parsed = urlparse(value)
    return urlunparse(parsed._replace(
        netloc=parsed.netloc.rsplit("@", 1)[-1], query="", fragment=""
    ))


def _test_response_summary(response, secret: str = "") -> str:
    content_type = response.headers.get("content-type", "")
    raw = response.text or ""
    if "text/html" in content_type.lower() or re.search(r"<!doctype\s+html|<html\b", raw, re.IGNORECASE):
        return "上游返回 HTML 错误页（详情已隐藏）"
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                raw = str(error["message"])
            elif payload.get("message"):
                raw = str(payload["message"])
            elif isinstance(payload.get("content"), list):
                raw = " ".join(str(block.get("text", "")) for block in payload["content"]
                               if isinstance(block, dict) and block.get("text"))
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    if secret:
        raw = raw.replace(secret, "[redacted]")
    raw = re.sub(r"Bearer\s+\S+", "Bearer [redacted]", raw, flags=re.IGNORECASE)
    raw = re.sub(r"([?&](?:api[_-]?key|key|token|auth_token)=)[^&\s]+", r"\1[redacted]", raw, flags=re.IGNORECASE)
    return " ".join(raw.split())[:200] or "上游返回空摘要"


def _channel_test_model(channel) -> str:
    return next((name.strip() for name in str(channel["models"] or "").split(",") if name.strip()), "")


def _parse_model_mapping(value) -> str:
    if value == "":
        value = {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(400, "model_mapping 必须是 JSON 对象，且 Key 和 Value 必须是非空字符串") from exc
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not key.strip() or not isinstance(upstream, str) or not upstream.strip()
        for key, upstream in value.items()
    ):
        raise HTTPException(400, "model_mapping 必须是 JSON 对象，且 Key 和 Value 必须是非空字符串")
    return json.dumps({key.strip(): upstream.strip() for key, upstream in value.items()}, ensure_ascii=False, separators=(",", ":"))


def _parse_tool_policy(value) -> str:
    if value not in {"preserve", "strip"}:
        raise HTTPException(400, "tool_policy 只能是 preserve 或 strip")
    return value


def _parse_proxy_url(value) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise HTTPException(400, "proxy_url 不是合法的代理地址") from exc
    if parsed.scheme not in {"http", "https", "socks5", "socks5h", "socks4"} or not parsed.hostname:
        raise HTTPException(400, "proxy_url 必须是合法的 http/https/socks5 代理地址")
    return value


def _parse_optional_timeout(value, name: str):
    if value in (None, "", 0, "0"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"{name} 必须是数字秒数") from exc
    if number <= 0:
        raise HTTPException(400, f"{name} 必须大于 0")
    return number


def _parse_non_negative_int(value, name: str, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"{name} 必须是非负整数") from exc
    if number < 0:
        raise HTTPException(400, f"{name} 不能小于 0")
    return number


def _parse_allowed_models(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        items = re.split(r"[,\s]+", value)
    elif isinstance(value, list):
        items = [str(item) for item in value]
    else:
        raise HTTPException(400, "allowed_models 必须是模型名数组或逗号分隔字符串")
    cleaned = [item.strip() for item in items if str(item).strip()]
    return ",".join(dict.fromkeys(cleaned))


def _public_channel(channel) -> dict:
    result = dict(channel)
    result.pop("api_key", None)
    try:
        mapping = json.loads(result.get("model_mapping") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        mapping = {}
    result["model_mapping"] = mapping if isinstance(mapping, dict) else {}
    result["tool_policy"] = result.get("tool_policy") or "preserve"
    try:
        result["protocol_capabilities"] = json.loads(result.get("protocol_capabilities") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        result["protocol_capabilities"] = {}
    result["extra_headers"] = _safe_header_names(channel)
    try:
        result["forward_headers"] = normalize_forward_headers(result.get("forward_headers", "[]"))
    except ValueError:
        result["forward_headers"] = []
    result["user_agent_configured"] = bool(str(result.get("user_agent") or "").strip())
    result.pop("user_agent", None)
    result["proxy_url"] = _mask_proxy(result.get("proxy_url", ""))
    result["breaker"] = channel_breaker_state(channel["id"])
    return result


@router.get("/channels")
async def channels_list(request: Request):
    require_admin(request)
    return [_public_channel(r) for r in query(
        "SELECT id,name,type,base_url,models,model_mapping,tool_policy,detected_protocol,protocol_capabilities,last_success_protocol,last_protocol_check_at,extra_headers,forward_headers,user_agent,proxy_url,connect_timeout_s,read_timeout_s,max_concurrency,rpm_limit,priority,weight,status,created_at "
        "FROM channels ORDER BY priority DESC, id"
    )]


@router.post("/channels/preview-url")
async def preview_channel_url(request: Request):
    require_admin(request)
    d = await body_json(request)
    try:
        channel_type = validate_channel_type(d.get("type", "auto"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    raw_base_url = str(d.get("base_url", "")).strip()
    suggested_protocol = _recommended_protocol(channel_type, raw_base_url)
    normalization_type = suggested_protocol if channel_type == "auto" else channel_type
    base_url = _channel_base_url(normalization_type, raw_base_url)
    if not base_url:
        raise HTTPException(400, "请先填写 Base URL")
    suggested_name = _channel_name(base_url)
    if normalization_type == "anthropic":
        return {
            "protocol": "anthropic",
            "recommended_protocol": "anthropic",
            "suggested_name": suggested_name,
            "name": suggested_name,
            "normalized_base_url": base_url,
            "base_url": base_url,
            "models_url": base_url + "/v1/models",
            "messages_url": base_url + "/v1/messages",
            "chat_url": base_url + "/v1/messages",
            "message": "Anthropic Messages 没有统一的模型列表接口，请手动填写模型 ID",
            "example_model": "glm-5.3",
        }
    if normalization_type == "gemini":
        return {"protocol": "gemini", "recommended_protocol": "gemini", "suggested_name": suggested_name,
                "name": suggested_name, "normalized_base_url": base_url, "base_url": base_url,
                "models_url": base_url + "/v1beta/models",
                "chat_url": base_url + "/v1beta/models/{model}:generateContent"}
    if normalization_type == "openai_responses":
        return {"protocol": "openai_responses", "recommended_protocol": "openai_responses", "suggested_name": suggested_name,
                "name": suggested_name, "normalized_base_url": base_url, "base_url": base_url,
                "models_url": base_url + "/v1/models", "responses_url": base_url + "/v1/responses"}
    return {
        "protocol": "openai_chat" if channel_type != "auto" else suggested_protocol,
        "recommended_protocol": suggested_protocol,
        "suggested_name": suggested_name,
        "name": suggested_name,
        "normalized_base_url": base_url,
        "base_url": base_url,
        "models_url": base_url + "/models",
        "chat_url": base_url + "/chat/completions",
    }


@router.get("/routes")
async def routes_list(request: Request):
    require_admin(request)
    public_model = str(request.query_params.get("model", "")).strip()
    if not public_model:
        raise HTTPException(400, "请提供公开模型名")
    rows = query(
        "SELECT id,name,models,model_mapping,tool_policy,priority,weight "
        "FROM channels WHERE status=1 ORDER BY priority DESC, id ASC"
    )
    channels = []
    for row in rows:
        if public_model not in _model_names(row["models"]).split(","):
            continue
        channels.append({
            "channel_id": row["id"],
            "channel_name": row["name"],
            "protocol": channel_protocol(row),
            "priority": row["priority"],
            "weight": row["weight"],
            "cooling": is_channel_cooling(row["id"]),
            "auto_disabled": is_channel_auto_disabled(row["id"]),
            "public_model": public_model,
            "upstream_model": upstream_model_for_channel(row, public_model),
            "tool_policy": row["tool_policy"] or "preserve",
        })
    routing = settings_store.get_all()
    return {
        "model": public_model,
        "channels": channels,
        "rules": [
            "优先级更大的渠道先使用。",
            "同优先级按权重随机选择。",
            "首选渠道失败后尝试下一候选。",
            f"单次最多尝试 {routing['retry_max_attempts']} 个渠道。",
            f"连续失败 {routing['fail_threshold']} 次后冷却 {routing['cooldown_seconds']} 秒。",
            "全部候选渠道冷却时不发送请求，直接返回 channels_cooling。",
            "渠道达到自身速率上限时自动跳过；全部达限返回 channel_rate_limited。",
        ],
    }


@router.post("/channels")
async def channels_create(request: Request):
    require_admin(request)
    d = await body_json(request)
    try:
        channel_type = validate_channel_type(d.get("type", "auto"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    base_url = _channel_base_url(channel_type, d.get("base_url", ""))
    name = str(d.get("name", "")).strip() or _channel_name(base_url)
    if not name:
        raise HTTPException(400, "请填写渠道名称或有效的 Base URL")
    models = _model_names(d.get("models", ""))
    model_mapping = _parse_model_mapping(d.get("model_mapping", {}))
    tool_policy = _parse_tool_policy(d.get("tool_policy", "preserve"))
    extra_headers, forward_headers, user_agent = _channel_header_fields(d)
    proxy_url = _parse_proxy_url(d.get("proxy_url", ""))
    connect_timeout_s = _parse_optional_timeout(d.get("connect_timeout_s"), "connect_timeout_s")
    read_timeout_s = _parse_optional_timeout(d.get("read_timeout_s"), "read_timeout_s")
    max_concurrency = _parse_non_negative_int(d.get("max_concurrency"), "max_concurrency")
    rpm_limit = _parse_non_negative_int(d.get("rpm_limit"), "rpm_limit")
    rid = execute(
        "INSERT INTO channels(name,type,base_url,api_key,models,model_mapping,tool_policy,extra_headers,forward_headers,user_agent,proxy_url,connect_timeout_s,read_timeout_s,max_concurrency,rpm_limit,priority,weight,status,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, channel_type, base_url, encrypt_secret(d.get("api_key", "")),
         models, model_mapping, tool_policy, extra_headers, forward_headers, user_agent,
         proxy_url, connect_timeout_s, read_timeout_s, max_concurrency, rpm_limit,
         int(d.get("priority", 0)), int(d.get("weight", 1)),
         1 if d.get("status", 1) else 0, iso(utcnow())))
    _register_models(models)
    return {"id": rid}


@router.put("/channels/{cid}")
async def channels_update(cid: int, request: Request):
    require_admin(request)
    d = await body_json(request)
    row = query_one("SELECT * FROM channels WHERE id=?", (cid,))
    if row is None:
        raise HTTPException(404, "渠道不存在")
    try:
        channel_type = validate_channel_type(d.get("type", row["type"]))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    base_url = _channel_base_url(channel_type, d.get("base_url", row["base_url"]))
    name = str(d.get("name", row["name"])).strip() or _channel_name(base_url) or row["name"]
    models = _model_names(d.get("models", row["models"]))
    model_mapping = _parse_model_mapping(d["model_mapping"] if "model_mapping" in d else row["model_mapping"])
    tool_policy = _parse_tool_policy(d.get("tool_policy", row["tool_policy"]))
    extra_headers, forward_headers, user_agent = _channel_header_fields(d, row)
    api_key = encrypt_secret(d.get("api_key")) if str(d.get("api_key", "")).strip() else encrypt_secret(row["api_key"])
    proxy_url = _parse_proxy_url(d["proxy_url"]) if "proxy_url" in d else row["proxy_url"]
    connect_timeout_s = _parse_optional_timeout(d["connect_timeout_s"], "connect_timeout_s") if "connect_timeout_s" in d else row["connect_timeout_s"]
    read_timeout_s = _parse_optional_timeout(d["read_timeout_s"], "read_timeout_s") if "read_timeout_s" in d else row["read_timeout_s"]
    max_concurrency = _parse_non_negative_int(d["max_concurrency"], "max_concurrency", row["max_concurrency"]) if "max_concurrency" in d else row["max_concurrency"]
    rpm_limit = _parse_non_negative_int(d["rpm_limit"], "rpm_limit", row["rpm_limit"]) if "rpm_limit" in d else row["rpm_limit"]
    execute("UPDATE channels SET name=?, type=?, base_url=?, api_key=?, models=?, model_mapping=?, tool_policy=?, extra_headers=?, forward_headers=?, user_agent=?, proxy_url=?, connect_timeout_s=?, read_timeout_s=?, max_concurrency=?, rpm_limit=?, priority=?, weight=?, status=? WHERE id=?",
            (name, channel_type, base_url,
             api_key, models, model_mapping, tool_policy, extra_headers, forward_headers, user_agent,
             proxy_url, connect_timeout_s, read_timeout_s, max_concurrency, rpm_limit,
             int(d.get("priority", row["priority"])), int(d.get("weight", row["weight"])),
             1 if d.get("status", row["status"]) else 0, cid))
    _register_models(models)
    _prune_unbound_models()
    return {"ok": True}


@router.delete("/channels/{cid}")
async def channels_delete(cid: int, request: Request):
    require_admin(request)
    execute("DELETE FROM channels WHERE id=?", (cid,))
    _prune_unbound_models()
    return {"ok": True}


@router.post("/channels/{cid}/recover")
async def channel_recover(cid: int, request: Request):
    """手动恢复渠道：清除冷却与自动禁用状态，立即重新参与路由。"""
    require_admin(request)
    if query_one("SELECT id FROM channels WHERE id=?", (cid,)) is None:
        raise HTTPException(404, "渠道不存在")
    recover_channel(cid)
    return {"ok": True, "breaker": channel_breaker_state(cid)}


@router.post("/channels/batch")
async def channels_batch(request: Request):
    """批量渠道操作：probe（探测）/ enable / disable / recover。"""
    require_admin(request)
    d = await body_json(request)
    action = str(d.get("action", "")).strip()
    if action not in {"probe", "enable", "disable", "recover"}:
        raise HTTPException(400, "action 只能是 probe / enable / disable / recover")
    raw_ids = d.get("ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise HTTPException(400, "ids 必须是非空数组")
    try:
        ids = [int(item) for item in raw_ids]
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "ids 必须是渠道 ID 数组") from exc
    existing = {row["id"] for row in query("SELECT id FROM channels")}
    targets = [cid for cid in dict.fromkeys(ids) if cid in existing]
    if not targets:
        raise HTTPException(404, "没有匹配的渠道")

    if action == "probe":
        from .health_probe import run_probe

        results = []
        for cid in targets:
            try:
                probe = await run_probe(cid)
                results.append({"id": cid, "ok": bool(probe["ok"]),
                                "error_category": probe["error_category"],
                                "latency_ms": probe["latency_ms"]})
            except ValueError as exc:
                results.append({"id": cid, "ok": False, "error_category": "not_found", "error": str(exc)})
        return {"action": action, "affected": len(results), "results": results}

    if action == "recover":
        for cid in targets:
            recover_channel(cid)
    else:
        status = 1 if action == "enable" else 0
        for cid in targets:
            execute("UPDATE channels SET status=? WHERE id=?", (status, cid))
        if status == 0:
            _prune_unbound_models()
    return {"action": action, "affected": len(targets),
            "results": [{"id": cid, "ok": True} for cid in targets]}


@router.post("/channels/{cid}/probe")
async def channel_probe(cid: int, request: Request):
    require_admin(request)
    from .health_probe import run_probe

    try:
        return await run_probe(cid)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


def _detection_models(protocol: str, payload: dict) -> list[dict]:
    if protocol in {"openai_chat", "openai_responses"}:
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ValueError("上游 /models 返回格式不兼容")
        return [{"id": str(item.get("id")).strip(), "protocol": protocol,
                 "capabilities": ["chat.completions"]}
                for item in data if isinstance(item, dict) and str(item.get("id", "")).strip()]
    if protocol == "anthropic":
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ValueError("上游 /v1/models 返回格式不兼容")
        return [{"id": str(item.get("id")).strip(), "protocol": protocol,
                 "capabilities": ["messages"]}
                for item in data if isinstance(item, dict) and str(item.get("id", "")).strip()]
    if protocol == "gemini":
        data = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ValueError("上游 /v1beta/models 返回格式不兼容")
        result = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if name.startswith("models/"):
                name = name[7:]
            if name:
                result.append({"id": name, "capabilities": item.get("supportedGenerationMethods") or [], "protocol": protocol})
        return result
    return []


def _detection_probe(protocol: str, base_url: str) -> tuple[str, str]:
    if protocol == "anthropic":
        return normalize_anthropic_base_url(base_url) + "/v1/models", "anthropic"
    if protocol == "gemini":
        return normalize_gemini_base_url(base_url) + "/v1beta/models", "gemini"
    return normalize_openai_base_url(base_url) + "/models", "openai_chat"


@router.post("/channels/detect")
async def detect_channel(request: Request):
    require_admin(request)
    d = await body_json(request)
    row = query_one("SELECT * FROM channels WHERE id=?", (d.get("channel_id"),)) if d.get("channel_id") else None
    try:
        requested_protocol = normalize_protocol(d.get("protocol", row["type"] if row else "auto"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    raw_base_url = str(d.get("base_url") or (row["base_url"] if row else "")).strip()
    if not raw_base_url:
        raise HTTPException(400, "请先填写 Base URL")
    api_key = str(d.get("api_key") or (row["api_key"] if row else ""))
    channel_name = (row["name"] if row else "") or _channel_name(raw_base_url)
    suggested_protocol = _recommended_protocol(requested_protocol, raw_base_url)
    normalization_type = suggested_protocol if requested_protocol == "auto" else requested_protocol
    try:
        normalized_base_url = _channel_base_url(normalization_type, raw_base_url)
    except HTTPException:
        normalized_base_url = raw_base_url
    extra_headers, forward_headers, user_agent = _channel_header_fields(d, row)
    header_channel = {"name": channel_name, "extra_headers": extra_headers,
                      "forward_headers": forward_headers, "user_agent": user_agent}
    protocols = [requested_protocol] if requested_protocol != "auto" else list(dict.fromkeys(
        [suggested_protocol, "openai_chat", "anthropic", "gemini"]
    ))
    last_failure = None
    attempts = []
    async with _http_client(row or {"proxy_url": d.get("proxy_url", "")}) as client:
        for protocol in protocols:
            try:
                request_url, header_protocol = _detection_probe(protocol, raw_base_url)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            headers = {"authorization": f"Bearer {api_key}"} if header_protocol == "openai_chat" else (
                anthropic_headers(api_key) if header_protocol == "anthropic"
                else {"x-goog-api-key": api_key}
            )
            headers = build_channel_headers(header_channel, headers, request, header_protocol)
            try:
                response = await client.get(request_url, headers=headers)
            except httpx.RequestError as exc:
                request_url_public = safe_public_url(request_url)
                last_failure = (request_url_public, 502, str(exc), "")
                attempts.append({"protocol": header_protocol, "method": "GET", "url": request_url_public,
                                 "request_url": request_url_public, "status_code": 502,
                                 "response_type": "network-error",
                                 "message": upstream_error_message(channel_name, 502, str(exc), api_key, "")})
                if requested_protocol == "auto":
                    continue
                continue
            request_url_public = safe_public_url(request_url)
            if response.status_code == 200:
                try:
                    models = _detection_models(header_protocol, response.json())
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    last_failure = (request_url_public, 502, str(exc), response.headers.get("content-type", ""))
                    attempts.append({"protocol": header_protocol, "method": "GET", "url": request_url_public,
                                     "request_url": request_url_public, "status_code": 502,
                                     "response_type": "invalid-model-list",
                                     "message": upstream_error_message(channel_name, 502, str(exc), api_key,
                                                                       response.headers.get("content-type", ""))})
                    break
                capabilities = {"models": True, "protocol": header_protocol}
                if row:
                    execute("UPDATE channels SET detected_protocol=?, protocol_capabilities=?, last_success_protocol=?, last_protocol_check_at=? WHERE id=?",
                            (header_protocol, json.dumps(capabilities), header_protocol, iso(utcnow()), row["id"]))
                return {"ok": True, "protocol": header_protocol, "recommended_protocol": suggested_protocol,
                        "suggested_name": _channel_name(normalized_base_url), "base_url": _channel_base_url(header_protocol, raw_base_url),
                        "normalized_base_url": _channel_base_url(header_protocol, raw_base_url),
                        "request_url": request_url_public, "status_code": 200, "models": models,
                        "sent_header_names": header_names(headers),
                        "response_type": f"{header_protocol}-model-list",
                        "attempts": attempts,
                        "message": f"已识别为 {header_protocol}，加载 {len(models)} 个模型"}
            last_failure = (request_url_public, response.status_code, response.text[:300], response.headers.get("content-type", ""))
            attempts.append({"protocol": header_protocol, "method": "GET", "url": request_url_public,
                             "request_url": request_url_public,
                             "status_code": response.status_code,
                             "response_type": "error",
                             "message": upstream_error_message(channel_name, response.status_code, response.text[:300], api_key,
                                                               response.headers.get("content-type", ""))})
            if requested_protocol == "auto":
                continue
            break
    request_url, status_code, detail, content_type = last_failure or (safe_public_url(raw_base_url), 502, "自动识别失败", "")
    summary = upstream_error_message(channel_name, status_code, detail, api_key, content_type)
    manual_only = requested_protocol == "anthropic" and status_code in {404, 405}
    attempt_summary = "；".join(f"{item['protocol']} HTTP {item['status_code']}" for item in attempts)
    payload = {"ok": False, "manual_only": manual_only, "manual_test_available": bool(d.get("model") or d.get("models")),
               "protocol": requested_protocol, "recommended_protocol": suggested_protocol,
               "suggested_name": _channel_name(normalized_base_url), "normalized_base_url": normalized_base_url,
               "request_url": request_url, "status_code": status_code, "models": [], "attempts": attempts,
               "sent_header_names": header_names(headers) if "headers" in locals() else [],
               "message": f"自动加载失败；已尝试 {attempt_summary}。最后结果：{summary}"}
    if manual_only:
        return payload
    return JSONResponse(status_code=502, content=payload)


@router.post("/channels/{cid}/test")
async def channels_test(cid: int, request: Request):
    require_admin(request)
    import time as _t
    try:
        raw_body = await request.body()
        d = json.loads(raw_body) if raw_body.strip() else {}
        if not isinstance(d, dict):
            raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "请求体不是合法的 JSON") from exc
    raw_budget = d.get("max_tokens", CHANNEL_TEST_MAX_TOKENS)
    if isinstance(raw_budget, bool) or not isinstance(raw_budget, int) or not 1 <= raw_budget <= 32768:
        raise HTTPException(400, "max_tokens 必须是 1 到 32768 之间的整数")
    test_max_tokens = raw_budget
    ch = query_one("SELECT * FROM channels WHERE id=?", (cid,))
    if ch is None:
        raise HTTPException(404, "渠道不存在")
    start = _t.perf_counter()
    request_id = secrets.token_hex(8)
    requested_model = str(d.get("model", "")).strip()
    bound_models = [name.strip() for name in str(ch["models"] or "").split(",") if name.strip()]
    test_model = requested_model or _channel_test_model(ch)
    if requested_model and requested_model not in bound_models:
        raise HTTPException(400, "测试模型必须来自该渠道已绑定的公开模型")
    upstream_model = upstream_model_for_channel(ch, test_model) if test_model else ""
    protocol = channel_protocol(ch)
    request_url = "mock://local" if protocol == "mock" else ""
    status_code = None
    model_list_status_code = None
    response_summary = ""
    upstream_request_id = None
    sent_header_names = []
    error_category = ""
    completion_state = "untested"
    if not test_model:
        return {
            "ok": False,
            "channel_name": ch["name"],
            "test_model": "",
            "upstream_model": "",
            "request_url": request_url,
            "status_code": 400,
            "model_list_status_code": None,
            "request_id": request_id,
            "message": "请先填写测试模型",
            "latency_ms": 0,
            "public_model": "",
            "final_url": request_url,
            "sent_header_names": [],
            "error_category": "invalid_request",
            "failover": False,
        }
    try:
        if protocol == "mock":
            ok, msg, status_code = True, "本地模拟渠道测试通过（不会产生上游额度）", 200
            completion_state = "valid"
            response_summary = msg
        elif protocol == "anthropic":
            base_url = normalize_anthropic_base_url(ch["base_url"])
            messages_url = base_url + "/v1/messages"
            request_url = _public_url(messages_url)
            payload = {
                "model": upstream_model,
                "messages": [{"role": "user", "content": "Reply only OK"}],
                "max_tokens": test_max_tokens,
                "stream": False,
            }
            async with _http_client(ch) as client:
                headers = build_channel_headers(ch, anthropic_headers(ch["api_key"]), request, "anthropic")
                sent_header_names = header_names(headers)
                resp = await client.post(
                    messages_url,
                    json=payload,
                    headers=headers,
                )
            status_code = resp.status_code
            upstream_request_id = (resp.headers.get("x-request-id") or "")[:128] or None
            if resp.status_code == 200:
                try:
                    converted = _anthropic_to_openai(resp.json(), test_model)
                    completion_state = _chat_response_state(converted)
                    if completion_state == "incomplete":
                        ok = False
                        msg = "请求已返回，但输出预算耗尽，未取得最终回答（finish_reason=length）"
                    elif completion_state != "valid":
                        raise ValueError(_invalid_response_detail(converted))
                    else:
                        ok = True
                        msg = "Anthropic Messages 推理测试通过；模型列表不适用；真实推理测试可能产生极少量额度"
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    ok = False
                    msg = ("推理失败；渠道协议错误：HTTP 200 响应不符合 Anthropic Messages 格式"
                           f"（{str(exc)[:160]}）；真实推理测试可能产生极少量额度")
                response_summary = _test_response_summary(resp, ch["api_key"])
            else:
                ok = False
                response_summary = _test_response_summary(resp, ch["api_key"])
                msg = (_anthropic_error_message(
                    ch, request_url, resp.status_code, resp.text,
                    resp.headers.get("content-type", ""),
                ) + "；真实推理测试可能产生极少量额度")
        elif protocol == "gemini":
            root = normalize_gemini_base_url(ch["base_url"])
            models_url = root + "/v1beta/models"
            chat_url = root + "/v1beta/models/" + quote(upstream_model, safe="") + ":generateContent"
            request_url = _public_url(chat_url)
            openai_payload = {"model": upstream_model, "messages": [{"role": "user", "content": "Reply only OK"}], "max_tokens": test_max_tokens, "stream": False}
            payload = openai_to_gemini(openai_payload)
            async with _http_client(ch) as client:
                headers = build_channel_headers(ch, {"x-goog-api-key": ch["api_key"], "Content-Type": "application/json"}, request, "gemini")
                sent_header_names = header_names(headers)
                try:
                    model_list = await client.get(models_url, headers=headers)
                    model_list_status_code = model_list.status_code
                except httpx.RequestError:
                    pass
                resp = await client.post(chat_url, json=payload, headers=headers)
            status_code = resp.status_code
            upstream_request_id = (resp.headers.get("x-request-id") or "")[:128] or None
            if status_code == 200:
                try:
                    converted = gemini_to_openai(resp.json(), test_model)
                    completion_state = _chat_response_state(converted)
                    if completion_state == "incomplete":
                        ok, msg = False, "请求已返回，但输出预算耗尽，未取得最终回答（finish_reason=length）"
                    elif completion_state != "valid":
                        raise ValueError(_invalid_response_detail(converted))
                    else:
                        ok, msg = True, "真实推理测试通过"
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    ok, msg = False, f"推理失败；渠道协议错误：{str(exc)[:160]}"
                if model_list_status_code == 200:
                    msg = "模型列表正常、" + msg
                msg += "；真实推理测试可能产生极少量额度"
            else:
                ok = False
                summary = upstream_error_message(ch["name"], status_code, resp.text, ch["api_key"], resp.headers.get("content-type", ""))
                msg = ("模型列表正常、推理失败；" if model_list_status_code == 200 else "推理失败；") + summary + "；真实推理测试可能产生极少量额度"
            response_summary = _test_response_summary(resp, ch["api_key"])
        elif protocol == "openai_responses":
            root = normalize_openai_responses_base_url(ch["base_url"])
            models_url = root + "/v1/models"
            chat_url = root + "/v1/responses"
            request_url = _public_url(chat_url)
            openai_payload = {"model": upstream_model, "messages": [{"role": "user", "content": "Reply only OK"}], "max_tokens": test_max_tokens, "stream": False}
            payload = openai_to_responses(openai_payload)
            async with _http_client(ch) as client:
                headers = build_channel_headers(ch, {"Authorization": f"Bearer {ch['api_key']}", "Content-Type": "application/json"}, request, "openai_responses")
                sent_header_names = header_names(headers)
                try:
                    model_list = await client.get(models_url, headers=headers)
                    model_list_status_code = model_list.status_code
                except httpx.RequestError:
                    pass
                resp = await client.post(chat_url, json=payload, headers=headers)
            status_code = resp.status_code
            upstream_request_id = (resp.headers.get("x-request-id") or "")[:128] or None
            if status_code == 200:
                try:
                    converted = responses_to_chat(resp.json(), test_model)
                    completion_state = _chat_response_state(converted)
                    if completion_state == "incomplete":
                        ok, msg = False, "请求已返回，但输出预算耗尽，未取得最终回答（finish_reason=length）"
                    elif completion_state != "valid":
                        raise ValueError(_invalid_response_detail(converted))
                    else:
                        ok, msg = True, "真实推理测试通过"
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    ok, msg = False, f"推理失败；渠道协议错误：{str(exc)[:160]}"
                if model_list_status_code == 200:
                    msg = "模型列表正常、" + msg
                msg += "；真实推理测试可能产生极少量额度"
            else:
                ok = False
                summary = upstream_error_message(ch["name"], status_code, resp.text, ch["api_key"], resp.headers.get("content-type", ""))
                msg = ("模型列表正常、推理失败；" if model_list_status_code == 200 else "推理失败；") + summary + "；真实推理测试可能产生极少量额度"
            response_summary = _test_response_summary(resp, ch["api_key"])
        else:
            base_url = normalize_openai_base_url(ch["base_url"])
            models_url = base_url + "/models"
            chat_url = base_url + "/chat/completions"
            request_url = _public_url(chat_url)
            payload = {
                "model": upstream_model,
                "messages": [{"role": "user", "content": "Reply only OK"}],
                "max_tokens": test_max_tokens,
                "stream": False,
            }
            async with _http_client(ch) as client:
                headers = build_channel_headers(ch, {"Authorization": f"Bearer {ch['api_key']}", "Content-Type": "application/json"}, request, "openai_chat")
                sent_header_names = header_names(headers)
                try:
                    model_list = await client.get(
                        models_url, headers=headers
                    )
                    model_list_status_code = model_list.status_code
                except httpx.RequestError:
                    pass
                resp = await client.post(
                    chat_url,
                    json=payload,
                    headers=headers,
                )
            status_code = resp.status_code
            upstream_request_id = (resp.headers.get("x-request-id") or "")[:128] or None
            if resp.status_code == 200:
                response_summary = _test_response_summary(resp, ch["api_key"])
                try:
                    parsed = resp.json()
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    ok = False
                    msg = f"推理失败；渠道协议错误：HTTP 200 响应不是合法 JSON（{str(exc)[:160]}）"
                else:
                    completion_state = _chat_response_state(parsed)
                    if completion_state == "incomplete":
                        ok = False
                        msg = "请求已返回，但输出预算耗尽，未取得最终回答（finish_reason=length）"
                    elif completion_state != "valid":
                        ok = False
                        msg = f"推理失败；渠道协议错误：HTTP 200 响应不符合 Chat Completions 格式（{_invalid_response_detail(parsed)}）"
                    else:
                        ok = True
                        msg = "真实推理测试通过"
                        if model_list_status_code == 200:
                            msg = "模型列表正常、推理测试通过"
                        msg += "；真实推理测试可能产生极少量额度"
            else:
                ok = False
                response_summary = _test_response_summary(resp, ch["api_key"])
                summary = upstream_error_message(
                    ch["name"], resp.status_code, resp.text, ch["api_key"],
                    resp.headers.get("content-type", ""),
                )
                prefix = "模型列表正常、推理失败；" if model_list_status_code == 200 else "推理失败；"
                msg = prefix + summary + "；真实推理测试可能产生极少量额度"
    except httpx.RequestError as exc:
        ok, msg, status_code = False, upstream_error_message(ch["name"], 502, str(exc), ch["api_key"]), 502
        response_summary = msg
    except Exception as exc:  # noqa: BLE001
        ok, msg, status_code = False, upstream_error_message(ch["name"], 502, str(exc), ch["api_key"]), 502
        response_summary = msg
    if not ok:
        error_category = (
            "incomplete_output" if completion_state == "incomplete" else
            "protocol_error" if "协议错误" in msg else
            "client_restricted" if _is_client_restricted(response_summary or msg) else
            classify_upstream_error(status_code, response_summary or msg)
        )
    upstream_content_type = resp.headers.get("content-type", "") if "resp" in locals() else ""
    latency = int((_t.perf_counter() - start) * 1000)
    return {
        "ok": ok,
        "channel_name": ch["name"],
        "protocol": protocol,
        "test_model": test_model,
        "upstream_model": upstream_model,
        "request_url": request_url,
        "status_code": status_code,
        "model_list_status_code": model_list_status_code,
        "request_id": request_id,
        "upstream_request_id": upstream_request_id,
        "upstream_content_type": upstream_content_type,
        "response_summary": response_summary,
        "max_tokens": test_max_tokens,
        "completion_state": completion_state if ok else (completion_state if completion_state != "untested" else "error"),
        "message": msg,
        "latency_ms": latency,
        "public_model": test_model,
        "final_url": request_url,
        "sent_header_names": sent_header_names,
        "error_category": error_category,
        "failover": False,
    }


def _discover_error(
    channel_name: str, request_url: str, status_code: int, detail: str, secret: str, content_type: str = ""
):
    summary = upstream_error_message(channel_name, status_code, detail, secret, content_type)
    message = f"自动加载失败，可手动填写模型 ID；{summary}"
    return JSONResponse(status_code=502, content={
        "ok": False,
        "detail": message,
        "channel_name": channel_name,
        "request_url": request_url,
        "status_code": status_code,
        "models": [],
    })


@router.post("/channels/discover-models")
async def discover_channel_models(request: Request):
    require_admin(request)
    d = await body_json(request)
    try:
        channel_type = validate_channel_type(d.get("type", "openai"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if channel_type == "anthropic":
        raise HTTPException(400, "Anthropic Messages 没有统一的模型列表接口，请手动填写模型 ID")
    base_url = _channel_base_url(channel_type, d.get("base_url", ""))
    if not base_url:
        raise HTTPException(400, "请先填写 Base URL")
    api_key = str(d.get("api_key", ""))
    channel_name = _channel_name(base_url)
    row = None
    if not api_key and d.get("channel_id"):
        row = query_one("SELECT name, api_key, extra_headers, forward_headers, user_agent, proxy_url, connect_timeout_s, read_timeout_s FROM channels WHERE id=?", (d["channel_id"],))
        if row:
            channel_name = row["name"] or channel_name
            api_key = row["api_key"]
    extra_headers, forward_headers, user_agent = _channel_header_fields(d, row)
    header_channel = {"name": channel_name, "extra_headers": extra_headers,
                      "forward_headers": forward_headers, "user_agent": user_agent}
    headers = build_channel_headers(header_channel, {"authorization": f"Bearer {api_key}"} if api_key else {}, request, "openai_chat")
    request_url = _public_url(base_url + "/models")
    client_channel = {
        "proxy_url": d.get("proxy_url") or (row["proxy_url"] if row else ""),
        "connect_timeout_s": d.get("connect_timeout_s") or (row["connect_timeout_s"] if row else None),
        "read_timeout_s": d.get("read_timeout_s") or (row["read_timeout_s"] if row else None),
    }
    try:
        async with _http_client(client_channel) as client:
            response = await client.get(base_url + "/models", headers=headers)
    except httpx.RequestError as exc:
        return _discover_error(channel_name, request_url, 502, str(exc), api_key)
    if response.status_code != 200:
        return _discover_error(
            channel_name, request_url, response.status_code, response.text, api_key,
            response.headers.get("content-type", ""),
        )
    try:
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError
        models = [str(item["id"]).strip() for item in payload["data"]
                  if isinstance(item, dict) and str(item.get("id", "")).strip()]
    except (TypeError, ValueError):
        return _discover_error(channel_name, request_url, 502, "上游 /models 返回格式不兼容", api_key)
    return {"base_url": base_url, "models": models}


# ------------------------------------------------------------------ 用户管理
@router.get("/users")
async def users_list(request: Request):
    require_admin(request)
    rows = query(
        """SELECT id, username, role, quota, used_quota, status, created_at,
                  (SELECT COUNT(*) FROM api_keys k WHERE k.user_id = users.id) key_count
           FROM users ORDER BY id""")
    return [dict(r) for r in rows]


@router.post("/users")
async def users_create(request: Request):
    require_admin(request)
    d = await body_json(request)
    if not d.get("username") or not d.get("password"):
        raise HTTPException(400, "用户名和密码不能为空")
    if len(str(d["password"])) < 12:
        raise HTTPException(400, "密码至少需要 12 位")
    if query_one("SELECT id FROM users WHERE username=?", (d["username"],)):
        raise HTTPException(409, "用户名已存在")
    uid = execute(
        "INSERT INTO users(username,password_hash,role,quota,used_quota,status,created_at) VALUES(?,?,?,?,0,?,?)",
        (d["username"], hash_password(d["password"]), d.get("role", "user"),
         float(d.get("quota", 10.0)), 1 if d.get("status", 1) else 0, iso(utcnow())))
    return {"id": uid}


@router.put("/users/{uid}")
async def users_update(uid: int, request: Request):
    admin = require_admin(request)
    d = await body_json(request)
    row = query_one("SELECT * FROM users WHERE id=?", (uid,))
    if row is None:
        raise HTTPException(404, "用户不存在")
    if d.get("password"):
        if len(str(d["password"])) < 12:
            raise HTTPException(400, "密码至少需要 12 位")
        execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(d["password"]), uid))
        auth = request.headers.get("authorization", "")
        current_token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        execute("DELETE FROM sessions WHERE user_id=? AND token<>?", (uid, current_token))
    role = d.get("role", row["role"])
    if row["id"] == admin["id"] and role != "admin":
        raise HTTPException(400, "不能降级当前登录的管理员")
    execute("UPDATE users SET role=?, quota=?, status=? WHERE id=?",
            (role, float(d.get("quota", row["quota"])),
             1 if d.get("status", row["status"]) else 0, uid))
    return {"ok": True}


@router.delete("/users/{uid}")
async def users_delete(uid: int, request: Request):
    admin = require_admin(request)
    if uid == admin["id"]:
        raise HTTPException(400, "不能删除当前登录账户")
    execute("UPDATE users SET status=0 WHERE id=?", (uid,))
    execute("UPDATE api_keys SET status=0 WHERE user_id=?", (uid,))
    return {"ok": True}


@router.post("/users/{uid}/topup")
async def users_topup(uid: int, request: Request):
    require_admin(request)
    d = await body_json(request)
    amount = float(d.get("amount", 0))
    if amount <= 0:
        raise HTTPException(400, "充值金额必须大于 0")
    execute("UPDATE users SET quota=quota+? WHERE id=?", (amount, uid))
    execute("INSERT INTO topups(user_id,amount,note,created_at) VALUES(?,?,?,?)",
            (uid, amount, d.get("note", "管理员充值"), iso(utcnow())))
    return {"ok": True}


# ------------------------------------------------------------------ API Key 管理
@router.get("/keys")
async def keys_list(request: Request):
    require_admin(request)
    rows = query(
        """SELECT k.*, u.username FROM api_keys k JOIN users u ON u.id = k.user_id
           ORDER BY k.id DESC""")
    items = []
    for r in rows:
        item = {**dict(r), "key": _mask_key(r["key"])}
        item["allowed_models"] = [m for m in re.split(r"[,\s]+", str(r["allowed_models"] or "")) if m]
        items.append(item)
    return items


@router.post("/keys")
async def keys_create(request: Request):
    require_admin(request)
    d = await body_json(request)
    uid = d.get("user_id")
    if not query_one("SELECT id FROM users WHERE id=?", (uid,)):
        raise HTTPException(400, "所属用户不存在")
    allowed = _parse_allowed_models(d.get("allowed_models"))
    rpm = _parse_non_negative_int(d.get("rpm_limit"), "rpm_limit")
    concurrency = _parse_non_negative_int(d.get("concurrency_limit"), "concurrency_limit")
    kid = execute(
        "INSERT INTO api_keys(user_id,name,key,quota_limit,used_quota,allowed_models,rpm_limit,concurrency_limit,status,created_at) VALUES(?,?,?,?,0,?,?,?,?,?)",
        (uid, d.get("name") or "未命名密钥", new_api_key(),
         float(d["quota_limit"]) if d.get("quota_limit") not in (None, "", 0) else None,
         allowed, rpm, concurrency,
         1 if d.get("status", 1) else 0, iso(utcnow())))
    return {"id": kid, "key": query_one("SELECT key FROM api_keys WHERE id=?", (kid,))["key"]}


@router.put("/keys/{kid}")
async def keys_update(kid: int, request: Request):
    require_admin(request)
    d = await body_json(request)
    row = query_one("SELECT * FROM api_keys WHERE id=?", (kid,))
    if row is None:
        raise HTTPException(404, "Key 不存在")
    limit = d.get("quota_limit", row["quota_limit"])
    allowed = _parse_allowed_models(d["allowed_models"]) if "allowed_models" in d else row["allowed_models"]
    rpm = _parse_non_negative_int(d["rpm_limit"], "rpm_limit", row["rpm_limit"]) if "rpm_limit" in d else row["rpm_limit"]
    concurrency = _parse_non_negative_int(d["concurrency_limit"], "concurrency_limit", row["concurrency_limit"]) if "concurrency_limit" in d else row["concurrency_limit"]
    execute("UPDATE api_keys SET name=?, quota_limit=?, allowed_models=?, rpm_limit=?, concurrency_limit=?, status=? WHERE id=?",
            (d.get("name", row["name"]),
             float(limit) if limit not in (None, "", 0) else None,
             allowed, rpm, concurrency,
             1 if d.get("status", row["status"]) else 0, kid))
    return {"ok": True}


@router.delete("/keys/{kid}")
async def keys_delete(kid: int, request: Request):
    require_admin(request)
    execute("DELETE FROM api_keys WHERE id=?", (kid,))
    return {"ok": True}


@router.post("/keys/{kid}/reset")
async def keys_reset(kid: int, request: Request):
    require_admin(request)
    if query_one("SELECT id FROM api_keys WHERE id=?", (kid,)) is None:
        raise HTTPException(404, "Key 不存在")
    key = new_api_key()
    execute("UPDATE api_keys SET key=? WHERE id=?", (key, kid))
    return {"key": key}


@router.get("/keys/{kid}/reveal")
async def keys_reveal(kid: int, request: Request):
    require_admin(request)
    row = query_one("SELECT key FROM api_keys WHERE id=?", (kid,))
    if row is None:
        raise HTTPException(404, "Key 不存在")
    return {"key": row["key"]}


# ------------------------------------------------------------------ 请求日志
@router.get("/logs")
async def logs_list(request: Request):
    require_admin(request)
    p = request.query_params
    cond, args = _log_filters(p)
    try:
        page = max(1, int(p.get("page", 1)))
        page_size = min(100, max(5, int(p.get("page_size", 20))))
    except ValueError as exc:
        raise HTTPException(400, "分页参数必须是数字") from exc
    total = query_one(f"SELECT COUNT(*) c FROM request_logs WHERE {cond}", args)["c"]
    rollups = _request_rollup(cond, args)
    summary = _rollup_totals(rollups)
    failed_attempts = query_one(f"SELECT COUNT(*) c FROM request_logs WHERE {cond} AND status='error'", args)["c"]
    rows = query(
        f"SELECT * FROM request_logs WHERE {cond} ORDER BY id DESC LIMIT ? OFFSET ?",
        args + (page_size, (page - 1) * page_size))
    return {"total": total, "client_requests": summary["requests"], "attempts": summary["attempts"],
            "retries": summary["retries"], "failed_count": summary["failed_requests"],
            "failed_attempts": failed_attempts, "tokens": summary["tokens"],
            "upstream_tokens": summary["upstream_tokens"], "estimated_tokens": summary["estimated_tokens"],
            "synthetic_tokens": summary["synthetic_tokens"], "unknown_tokens": summary["unknown_tokens"],
            "page": page, "page_size": page_size,
            "items": [_public_log(row) for row in rows]}


@router.get("/logs/{log_id}")
async def log_detail(log_id: int, request: Request):
    require_admin(request)
    row = query_one("SELECT * FROM request_logs WHERE id=?", (log_id,))
    if row is None:
        raise HTTPException(404, "日志不存在")
    attempts = query(
        "SELECT * FROM request_logs WHERE request_id=? ORDER BY id",
        (row["request_id"],),
    )
    summary = _rollup_totals(_request_rollup("request_id=?", (row["request_id"],)))
    return {
        "item": _public_log(row),
        "attempts": [_public_log(item) for item in attempts],
        "request_summary": summary,
        "channel_chain": [
            {"channel_id": item["channel_id"], "channel_name": item["channel_name"],
             "status": item["status"], "status_code": item["status_code"],
             "will_retry": bool(item["will_retry"])}
            for item in attempts
        ],
        "upstream_response": row["error_msg"] or "",
        "replayed": False,
    }


def _export_rows(params):
    cond, args = _log_filters(params)
    return [_public_log(row) for row in query(
        f"SELECT * FROM request_logs WHERE {cond} ORDER BY id DESC", args
    )]


@router.get("/logs/export")
async def logs_export(request: Request):
    require_admin(request)
    rows = _export_rows(request.query_params)
    fmt = request.query_params.get("format", "csv").lower()
    if fmt == "json":
        return JSONResponse(rows, headers={"Content-Disposition": "attachment; filename=obsessionapi-logs.json"})
    if fmt != "csv":
        raise HTTPException(400, "format 只能是 csv 或 json")
    output = io.StringIO()
    fields = ["id", "request_id", "channel_id", "channel_name", "model", "endpoint", "prompt_tokens", "completion_tokens", "usage_source", "latency_ms", "status", "status_code", "error_category", "error_msg", "will_retry", "ip", "created_at"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=obsessionapi-logs.csv"})


@router.post("/logs/clear")
async def logs_clear(request: Request):
    """清理请求日志：days 缺省/"all" 清空全部，数字表示删除 N 天前的记录。不影响渠道/模型/密钥配置。"""
    require_admin(request)
    d = await body_json(request)
    days = d.get("days")
    if days in (None, "", "all"):
        execute("DELETE FROM request_logs")
    else:
        days = int(days)
        execute("DELETE FROM request_logs WHERE created_at < ?",
                (iso(utcnow() - timedelta(days=days)),))
    maintenance()
    return {"ok": True}


@router.get("/settings")
async def settings_get(request: Request):
    require_admin(request)
    return settings_store.get_all()


@router.put("/settings")
async def settings_put(request: Request):
    require_admin(request)
    data = await body_json(request)
    values = data.get("settings", data)
    if not isinstance(values, dict):
        raise HTTPException(400, "settings 必须是对象")
    try:
        return settings_store.update(values)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


def _config_channel(row, include_secrets: bool) -> dict:
    value = str(row["api_key"] or "")
    extra_headers, forward_headers, user_agent = channel_header_config(row)
    proxy = str(row["proxy_url"] or "")
    return {
        "name": row["name"], "type": row["type"], "base_url": row["base_url"],
        "api_key": value if include_secrets else _mask_key(value),
        "models": row["models"], "model_mapping": json.loads(row["model_mapping"] or "{}"),
        "tool_policy": row["tool_policy"] or "preserve", "priority": row["priority"],
        "weight": row["weight"], "status": row["status"],
        "proxy_url": proxy if include_secrets else _mask_proxy(proxy),
        "connect_timeout_s": row["connect_timeout_s"], "read_timeout_s": row["read_timeout_s"],
        "max_concurrency": row["max_concurrency"],
        "rpm_limit": row["rpm_limit"],
        "extra_headers": extra_headers if include_secrets else {name: _mask_header_value(value) for name, value in extra_headers.items()},
        "forward_headers": forward_headers,
        "user_agent": user_agent,
    }


@router.get("/config/export")
async def config_export(request: Request):
    require_admin(request)
    include_secrets = request.query_params.get("include_secrets", "false").lower() in {"1", "true", "yes"}
    channels = []
    for row in query("SELECT * FROM channels ORDER BY priority DESC, id"):
        channels.append(_config_channel(row, include_secrets))
    return {
        "version": app_version(),
        "models": [dict(row) for row in query("SELECT model_name,display_name,vendor,input_price,output_price,status FROM models ORDER BY id")],
        "channels": channels,
        "settings": settings_store.get_all(),
        "include_secrets": include_secrets,
    }


@router.post("/config/import")
async def config_import(request: Request):
    require_admin(request)
    data = await body_json(request)
    strategy = str(data.get("conflict", "skip"))
    if strategy not in {"skip", "overwrite"}:
        raise HTTPException(400, "conflict 只能是 skip 或 overwrite")
    include_secrets = bool(data.get("include_secrets", False))
    result = {"models": [], "channels": [], "settings": []}
    for item in data.get("models", []):
        name = str(item.get("model_name", "")).strip()
        if not name:
            result["models"].append({"name": name, "status": "invalid"})
            continue
        existing = query_one("SELECT id FROM models WHERE model_name=?", (name,))
        if existing and strategy == "skip":
            result["models"].append({"name": name, "status": "skipped"})
            continue
        values = (str(item.get("display_name", name)), str(item.get("vendor", "")), float(item.get("input_price", 0) or 0), float(item.get("output_price", 0) or 0), 1 if item.get("status", 1) else 0)
        if existing:
            execute("UPDATE models SET display_name=?,vendor=?,input_price=?,output_price=?,status=? WHERE id=?", values + (existing["id"],))
            result["models"].append({"name": name, "status": "updated"})
        else:
            execute("INSERT INTO models(model_name,display_name,vendor,input_price,output_price,status,created_at) VALUES(?,?,?,?,?,?,?)", (name, *values, iso(utcnow())))
            result["models"].append({"name": name, "status": "created"})
    for item in data.get("channels", []):
        name = str(item.get("name", "")).strip()
        if not name:
            result["channels"].append({"name": name, "status": "invalid"})
            continue
        existing = query_one("SELECT * FROM channels WHERE name=?", (name,))
        if existing and strategy == "skip":
            result["channels"].append({"name": name, "status": "skipped"})
            continue
        channel_type = validate_channel_type(item.get("type", "auto"))
        base_url = _channel_base_url(channel_type, item.get("base_url", ""))
        models = _model_names(item.get("models", ""))
        mapping = _parse_model_mapping(item.get("model_mapping", {}))
        policy = _parse_tool_policy(item.get("tool_policy", "preserve"))
        if existing and not include_secrets and "extra_headers" in item:
            extra_headers = str(existing["extra_headers"] or "{}")
            forward_headers = str(existing["forward_headers"] or "[]")
            user_agent = str(existing["user_agent"] or "")
        else:
            extra_headers, forward_headers, user_agent = _channel_header_fields(item, existing)
        imported_key = str(item.get("api_key", ""))
        if imported_key and "•" not in imported_key and include_secrets:
            stored_key = encrypt_secret(imported_key)
        elif existing:
            stored_key = encrypt_secret(existing["api_key"])
        else:
            stored_key = ""
        imported_proxy = str(item.get("proxy_url", ""))
        if imported_proxy and "•" not in imported_proxy:
            proxy_url = _parse_proxy_url(imported_proxy)
        elif existing:
            proxy_url = existing["proxy_url"]
        else:
            proxy_url = ""
        connect_timeout_s = _parse_optional_timeout(item.get("connect_timeout_s"), "connect_timeout_s")
        read_timeout_s = _parse_optional_timeout(item.get("read_timeout_s"), "read_timeout_s")
        max_concurrency = _parse_non_negative_int(item.get("max_concurrency"), "max_concurrency")
        rpm_limit = _parse_non_negative_int(item.get("rpm_limit"), "rpm_limit")
        values = (name, channel_type, base_url, stored_key, models, mapping, policy,
                  extra_headers, forward_headers, user_agent, proxy_url,
                  connect_timeout_s, read_timeout_s, max_concurrency, rpm_limit,
                  int(item.get("priority", 0)),
                  max(1, int(item.get("weight", 1))), 1 if item.get("status", 1) else 0)
        if existing:
            execute("UPDATE channels SET name=?,type=?,base_url=?,api_key=?,models=?,model_mapping=?,tool_policy=?,extra_headers=?,forward_headers=?,user_agent=?,proxy_url=?,connect_timeout_s=?,read_timeout_s=?,max_concurrency=?,rpm_limit=?,priority=?,weight=?,status=? WHERE id=?", values + (existing["id"],))
            result["channels"].append({"name": name, "status": "updated"})
        else:
            execute("INSERT INTO channels(name,type,base_url,api_key,models,model_mapping,tool_policy,extra_headers,forward_headers,user_agent,proxy_url,connect_timeout_s,read_timeout_s,max_concurrency,rpm_limit,priority,weight,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values + (iso(utcnow()),))
            result["channels"].append({"name": name, "status": "created"})
        _register_models(models)
    if isinstance(data.get("settings"), dict):
        try:
            settings_store.update(data["settings"])
            result["settings"].append({"status": "updated"})
        except (KeyError, ValueError) as exc:
            result["settings"].append({"status": "invalid", "message": str(exc)})
    return result


# ------------------------------------------------------------------ 用量统计（保留后端接口）
@router.get("/billing")
async def billing(request: Request):
    require_admin(request)
    totals = query_one(
        """SELECT COALESCE(SUM(cost),0) cost, COALESCE(SUM(prompt_tokens),0) pt,
                  COALESCE(SUM(completion_tokens),0) ct, COUNT(*) c FROM request_logs WHERE status='success'""")
    daily = query(
        """SELECT substr(created_at,1,10) d, COALESCE(SUM(cost),0) cost, COUNT(*) c
           FROM request_logs WHERE status='success' AND created_at >= ?
           GROUP BY d ORDER BY d""",
        (iso(utcnow() - timedelta(days=13))[:10],))
    by_model = query(
        """SELECT model, COALESCE(SUM(cost),0) cost, COUNT(*) c,
                  COALESCE(SUM(prompt_tokens),0) pt, COALESCE(SUM(completion_tokens),0) ct
           FROM request_logs WHERE status='success' GROUP BY model ORDER BY cost DESC""")
    by_user = query(
        """SELECT username, COALESCE(SUM(cost),0) cost, COUNT(*) c
           FROM request_logs WHERE status='success' GROUP BY username ORDER BY cost DESC""")
    topups = query(
        """SELECT t.*, u.username FROM topups t JOIN users u ON u.id=t.user_id ORDER BY t.id DESC LIMIT 15""")
    users = query("SELECT id, username, quota, used_quota FROM users ORDER BY used_quota DESC")
    return {
        "totals": {"cost": round(totals["cost"], 4), "prompt_tokens": totals["pt"],
                   "completion_tokens": totals["ct"], "requests": totals["c"]},
        "daily": [dict(r) for r in daily],
        "by_model": [dict(r) for r in by_model],
        "by_user": [dict(r) for r in by_user],
        "topups": [dict(r) for r in topups],
        "users": [dict(r) for r in users],
    }


# ------------------------------------------------------------------ 网关概览
@router.get("/gateway")
async def gateway_info(request: Request):
    require_admin(request)
    import platform
    import sys
    db_path = database.DB_PATH
    db_size = 0
    for path in (db_path, db_path.with_name(db_path.name + "-wal")):
        try:
            db_size += path.stat().st_size
        except FileNotFoundError:
            # SQLite can checkpoint and remove the WAL while its size is sampled.
            pass
    channels = query("SELECT id,name,type,status FROM channels ORDER BY id")
    return {
        "version": "v1.0.0",
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "db_size_mb": round(db_size / 1048576, 2),
        "endpoints": [
            {"method": "GET", "path": "/v1/models", "desc": "列出网关可用模型"},
            {"method": "POST", "path": "/v1/chat/completions", "desc": "Chat Completions（支持 stream）"},
            {"method": "POST", "path": "/v1/completions", "desc": "文本补全（兼容旧版）"},
            {"method": "POST", "path": "/v1/embeddings", "desc": "文本向量化（OpenAI 兼容）"},
        ],
        "channels": [dict(r) for r in channels],
        "auth": "Authorization: Bearer sk-xxx（在「API Key 管理」中创建）",
        "routing": (
            f"按模型过滤渠道 → 优先级分组 → 组内按权重排序；失败自动切换下一个候选"
            f"（连续失败 {settings_store.get('fail_threshold')} 次冷却 {settings_store.get('cooldown_seconds')} 秒，"
            f"冷却累计 {settings_store.get('auto_disable_after')} 次自动禁用并在 "
            f"{settings_store.get('auto_recover_seconds')} 秒后自动恢复）；"
            f"渠道达自身速率上限时跳过，退避为指数 + 抖动（上限 {settings_store.get('retry_backoff_max_ms')} 毫秒）"
        ),
        "usage": "记录请求 token、耗时、渠道与状态，写入请求日志用于个人用量分析",
    }
