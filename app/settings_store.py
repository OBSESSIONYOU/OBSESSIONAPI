"""网关可调行为的轻量持久化设置。"""
import json
import threading

from . import database


DEFAULTS = {
    "retry_max_attempts": 3,
    "retry_backoff_ms": 0,
    "retry_backoff_max_ms": 10000,
    "retry_backoff_jitter_pct": 25,
    "connect_timeout_s": 5,
    "read_timeout_s": 120,
    "fail_threshold": 3,
    "cooldown_seconds": 300,
    "auto_disable_after": 3,
    "auto_recover_seconds": 1800,
    "cache_enabled": False,
    "cache_ttl_seconds": 300,
    "models_require_key": False,
    "log_retention_days": 30,
    "probe_enabled": False,
    "probe_interval_s": 300,
}

_MIN_ONE = {"retry_max_attempts", "fail_threshold", "auto_recover_seconds", "cache_ttl_seconds",
            "retry_backoff_max_ms"}

_lock = threading.Lock()
_cache: dict[str, object] | None = None


def _coerce(key: str, value):
    if key not in DEFAULTS:
        raise KeyError(key)
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, str):
            value = value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"设置 {key} 必须是数字") from exc
    minimum = 0 if key not in _MIN_ONE else 1
    if number < minimum:
        raise ValueError(f"设置 {key} 不能小于 {minimum}")
    if key in {"connect_timeout_s", "read_timeout_s"}:
        return max(1, number)
    return number


def _load() -> dict[str, object]:
    values = dict(DEFAULTS)
    for row in database.query("SELECT key, value FROM settings"):
        if row["key"] not in DEFAULTS:
            continue
        try:
            values[row["key"]] = _coerce(row["key"], json.loads(row["value"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return values


def get_all() -> dict[str, object]:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load()
        return dict(_cache)


def get(key: str):
    return get_all()[key]


def update(values: dict) -> dict[str, object]:
    global _cache
    current = get_all()
    changed = {}
    for key, value in values.items():
        if key not in DEFAULTS:
            raise ValueError(f"不支持的设置：{key}")
        changed[key] = _coerce(key, value)
    current.update(changed)
    for key, value in changed.items():
        database.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), database.iso(database.utcnow())),
        )
    with _lock:
        _cache = current
    return dict(current)


def clear_cache() -> None:
    global _cache
    with _lock:
        _cache = None
