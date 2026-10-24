"""SQLite 数据层：建表、连接助手与首次管理员初始化。"""
import hashlib
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .secrets import decrypt_secret, encrypt_secret, is_encrypted

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DB_PATH = DATA_DIR / "obsessionapi.db"

_lock = threading.Lock()
PASSWORD_ITERATIONS = 600_000
LEGACY_PASSWORD_ITERATIONS = 60_000


def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class _DecryptedRow:
    """让旧的业务代码继续读取 channels.api_key，同时数据库只保存密文。"""

    def __init__(self, row: sqlite3.Row):
        self._keys = list(row.keys())
        self._values = list(row)
        if "api_key" in self._keys:
            index = self._keys.index("api_key")
            self._values[index] = decrypt_secret(self._values[index])

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._keys.index(key)]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return self._keys


def _safe_row(row: sqlite3.Row):
    return _DecryptedRow(row) if "api_key" in row.keys() else row


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        conn = get_db()
        try:
            return [_safe_row(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()


def query_one(sql: str, params: tuple = ()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> int:
    params = _encrypt_channel_write(sql, params)
    with _lock:
        conn = get_db()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def _encrypt_channel_write(sql: str, params: tuple) -> tuple:
    """兼容少量直接写库的旧调用方，避免明文重新进入 channels。"""
    normalized = re.sub(r"\s+", " ", sql.strip().lower())
    if "channels" not in normalized or "api_key" not in normalized:
        return params
    values = list(params)
    match = re.search(r"insert\s+into\s+channels\s*\(([^)]*)\)", normalized)
    if match:
        columns = [item.strip() for item in match.group(1).split(",")]
        if "api_key" in columns and columns.index("api_key") < len(values):
            index = columns.index("api_key")
            if not is_encrypted(str(values[index] or "")):
                values[index] = encrypt_secret(values[index])
        return tuple(values)
    update = re.search(r"update\s+channels\s+set\s+(.+?)\s+where\s+", normalized)
    if update:
        assignments = [item.strip() for item in update.group(1).split(",")]
        param_index = 0
        for assignment in assignments:
            if "?" in assignment:
                if assignment.startswith("api_key=") and param_index < len(values) and not is_encrypted(str(values[param_index] or "")):
                    values[param_index] = encrypt_secret(values[param_index])
                param_index += assignment.count("?")
        return tuple(values)
    return params


def maintenance() -> None:
    """WAL checkpoint + VACUUM，让清理日志后的数据库文件真正变小。"""
    with _lock:
        conn = get_db()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.isolation_level = None
            conn.execute("VACUUM")
            conn.execute("PRAGMA analyze")
        finally:
            conn.close()


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PASSWORD_ITERATIONS).hex()
    return f"{salt}${PASSWORD_ITERATIONS}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        parts = stored.split("$")
        if len(parts) == 3:
            salt, iteration_text, expected = parts
            iterations = int(iteration_text)
        elif len(parts) == 2:
            salt, expected = parts
            iterations = LEGACY_PASSWORD_ITERATIONS
        else:
            return False
    except (TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
    return secrets.compare_digest(actual, expected)


def new_token() -> str:
    return secrets.token_hex(24)


def new_api_key() -> str:
    return "sk-" + secrets.token_hex(20)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    quota REAL NOT NULL DEFAULT 20.0,
    used_quota REAL NOT NULL DEFAULT 0.0,
    status INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    vendor TEXT NOT NULL DEFAULT '',
    input_price REAL NOT NULL DEFAULT 0.0,
    output_price REAL NOT NULL DEFAULT 0.0,
    status INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'openai',
    base_url TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    models TEXT NOT NULL DEFAULT '',
    model_mapping TEXT NOT NULL DEFAULT '{}',
    tool_policy TEXT NOT NULL DEFAULT 'preserve',
    detected_protocol TEXT NOT NULL DEFAULT '',
    protocol_capabilities TEXT NOT NULL DEFAULT '{}',
    last_success_protocol TEXT NOT NULL DEFAULT '',
    last_protocol_check_at TEXT,
    extra_headers TEXT NOT NULL DEFAULT '{}',
    forward_headers TEXT NOT NULL DEFAULT '[]',
    user_agent TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 0,
    weight INTEGER NOT NULL DEFAULT 1,
    proxy_url TEXT NOT NULL DEFAULT '',
    connect_timeout_s REAL,
    read_timeout_s REAL,
    max_concurrency INTEGER NOT NULL DEFAULT 0,
    rpm_limit INTEGER NOT NULL DEFAULT 0,
    status INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    key TEXT UNIQUE NOT NULL,
    quota_limit REAL,
    used_quota REAL NOT NULL DEFAULT 0.0,
    allowed_models TEXT NOT NULL DEFAULT '',
    rpm_limit INTEGER NOT NULL DEFAULT 0,
    concurrency_limit INTEGER NOT NULL DEFAULT 0,
    status INTEGER NOT NULL DEFAULT 1,
    last_used_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS request_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    user_id INTEGER,
    username TEXT NOT NULL DEFAULT '',
    api_key_id INTEGER,
    channel_id INTEGER,
    channel_name TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL,
    endpoint TEXT NOT NULL DEFAULT '/v1/chat/completions',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    usage_source TEXT NOT NULL DEFAULT 'unknown',
    cost REAL NOT NULL DEFAULT 0.0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'success',
    status_code INTEGER NOT NULL DEFAULT 200,
    error_msg TEXT NOT NULL DEFAULT '',
    error_category TEXT NOT NULL DEFAULT '',
    will_retry INTEGER NOT NULL DEFAULT 0,
    ip TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_logs_created ON request_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_logs_model ON request_logs(model);
CREATE INDEX IF NOT EXISTS idx_logs_status ON request_logs(status);
CREATE INDEX IF NOT EXISTS idx_logs_channel ON request_logs(channel_id);
CREATE INDEX IF NOT EXISTS idx_logs_request_id ON request_logs(request_id);
CREATE INDEX IF NOT EXISTS idx_logs_category ON request_logs(error_category);
CREATE INDEX IF NOT EXISTS idx_logs_channel_created ON request_logs(channel_id, created_at);
CREATE INDEX IF NOT EXISTS idx_logs_status_created ON request_logs(status, created_at);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    ok INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    status_code INTEGER,
    error_category TEXT NOT NULL DEFAULT '',
    error_msg TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_channel_probes_channel_created ON channel_probes(channel_id, created_at);

CREATE TABLE IF NOT EXISTS channel_states (
    channel_id INTEGER PRIMARY KEY,
    streak INTEGER NOT NULL DEFAULT 0,
    cooldown_until REAL NOT NULL DEFAULT 0,
    cooldown_count INTEGER NOT NULL DEFAULT 0,
    auto_disabled INTEGER NOT NULL DEFAULT 0,
    disabled_reason TEXT NOT NULL DEFAULT '',
    disabled_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
);
"""


def init_db() -> None:
    with _lock:
        conn = get_db()
        try:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(request_logs)").fetchall()}
            if "will_retry" not in columns:
                conn.execute("ALTER TABLE request_logs ADD COLUMN will_retry INTEGER NOT NULL DEFAULT 0")
            if "usage_source" not in columns:
                conn.execute("ALTER TABLE request_logs ADD COLUMN usage_source TEXT NOT NULL DEFAULT 'unknown'")
            channel_columns = {row[1] for row in conn.execute("PRAGMA table_info(channels)").fetchall()}
            if "model_mapping" not in channel_columns:
                conn.execute("ALTER TABLE channels ADD COLUMN model_mapping TEXT NOT NULL DEFAULT '{}'")
            if "tool_policy" not in channel_columns:
                conn.execute("ALTER TABLE channels ADD COLUMN tool_policy TEXT NOT NULL DEFAULT 'preserve'")
            for column, definition in (
                ("detected_protocol", "TEXT NOT NULL DEFAULT ''"),
                ("protocol_capabilities", "TEXT NOT NULL DEFAULT '{}'"),
                ("last_success_protocol", "TEXT NOT NULL DEFAULT ''"),
                ("last_protocol_check_at", "TEXT"),
                ("extra_headers", "TEXT NOT NULL DEFAULT '{}'"),
                ("forward_headers", "TEXT NOT NULL DEFAULT '[]'"),
                ("user_agent", "TEXT NOT NULL DEFAULT ''"),
                ("proxy_url", "TEXT NOT NULL DEFAULT ''"),
                ("connect_timeout_s", "REAL"),
                ("read_timeout_s", "REAL"),
                ("max_concurrency", "INTEGER NOT NULL DEFAULT 0"),
                ("rpm_limit", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in channel_columns:
                    conn.execute(f"ALTER TABLE channels ADD COLUMN {column} {definition}")
            key_columns = {row[1] for row in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
            for column, definition in (
                ("allowed_models", "TEXT NOT NULL DEFAULT ''"),
                ("rpm_limit", "INTEGER NOT NULL DEFAULT 0"),
                ("concurrency_limit", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in key_columns:
                    conn.execute(f"ALTER TABLE api_keys ADD COLUMN {column} {definition}")
            if "error_category" not in columns:
                conn.execute("ALTER TABLE request_logs ADD COLUMN error_category TEXT NOT NULL DEFAULT ''")
            from .secrets import migrate_channel_secrets

            migrate_channel_secrets(conn)
            conn.commit()
        finally:
            conn.close()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def create_initial_admin(username: str, password: str) -> int | None:
    """仅在空库时创建首个管理员，避免预置账号和并发重复初始化。"""
    with _lock:
        conn = get_db()
        try:
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                return None
            cur = conn.execute(
                "INSERT INTO users(username,password_hash,role,quota,used_quota,status,created_at)"
                " VALUES(?,?, 'admin', 0, 0, 1, ?)",
                (username, hash_password(password), iso(utcnow())),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


if __name__ == "__main__":
    init_db()
    print("数据库已初始化:", DB_PATH)
