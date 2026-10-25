import hashlib
import json
import sqlite3
import socket
import tempfile
import threading
import time
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning, module="fastapi.testclient")

from fastapi.testclient import TestClient

from app import database, gateway
from app.main import app


def _sse_payloads(text):
    return [line[6:] for line in text.splitlines() if line.startswith("data: ")]


def _read_request_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    return handler.rfile.read(length)


def _header(headers, name):
    """大小写无关地读取一个请求头（HTTP/1.1 头名不区分大小写，客户端发送的大小写随实现而变）。"""
    lowered = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == lowered:
            return value
    return None


class FirstRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir, self.old_db_path = database.DATA_DIR, database.DB_PATH
        database.DATA_DIR = Path(self.tmp.name)
        database.DB_PATH = database.DATA_DIR / "obsessionapi.db"
        gateway._BREAKER.clear()

    def tearDown(self):
        database.DATA_DIR, database.DB_PATH = self.old_data_dir, self.old_db_path
        self.tmp.cleanup()

    def _configure_gateway(self, client, base_url, model="model-a", name="seekai",
                           model_mapping=None, tool_policy=None, priority=10, weight=1,
                           channel_type="openai"):
        setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
        admin_headers = {"Authorization": "Bearer " + setup["token"]}
        channel_body = {
            "name": name, "type": channel_type, "base_url": base_url,
            "api_key": "test-key", "models": model, "priority": priority, "weight": weight,
        }
        if model_mapping is not None:
            channel_body["model_mapping"] = model_mapping
        if tool_policy is not None:
            channel_body["tool_policy"] = tool_policy
        channel = client.post("/api/channels", headers=admin_headers, json=channel_body)
        self.assertEqual(channel.status_code, 200, channel.text)
        key = client.post("/api/keys", headers=admin_headers, json={
            "name": "test", "user_id": setup["user"]["id"],
        }).json()["key"]
        return admin_headers, key

    def _configure_openai_channels(self, client, specs, model="model-a"):
        setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
        headers = {"Authorization": "Bearer " + setup["token"]}
        for name, base_url, priority in specs:
            response = client.post("/api/channels", headers=headers, json={
                "name": name, "type": "openai", "base_url": base_url,
                "api_key": name + "-fixture-key", "models": model, "priority": priority,
            })
            self.assertEqual(response.status_code, 200, response.text)
        key = client.post("/api/keys", headers=headers, json={
            "name": "test", "user_id": setup["user"]["id"],
        }).json()["key"]
        return key

    def _stream_response(self, events):
        class StreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in events:
                    payload = event if isinstance(event, str) else json.dumps(event)
                    self.wfile.write(("data: " + payload + "\n\n").encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), StreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}")
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        return response

    def test_first_run_starts_empty_and_accepts_one_admin(self):
        database.init_db()
        self.assertEqual(database.query_one("SELECT COUNT(*) c FROM users")["c"], 0)

        admin_id = database.create_initial_admin("owner", "a-long-password")
        self.assertIsNotNone(admin_id)
        self.assertEqual(database.query_one("SELECT role FROM users WHERE id=?", (admin_id,))["role"], "admin")
        self.assertIsNone(database.create_initial_admin("other", "another-long-password"))

    def test_legacy_channel_migration_keeps_channel_and_key_data(self):
        database.DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(database.DB_PATH)
        conn.executescript("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
                quota REAL NOT NULL DEFAULT 20.0, used_quota REAL NOT NULL DEFAULT 0.0,
                status INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
            );
            CREATE TABLE api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                name TEXT NOT NULL, key TEXT UNIQUE NOT NULL, quota_limit REAL,
                used_quota REAL NOT NULL DEFAULT 0.0, status INTEGER NOT NULL DEFAULT 1,
                last_used_at TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'openai', base_url TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '', models TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL DEFAULT 0, weight INTEGER NOT NULL DEFAULT 1,
                status INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
            );
        """)
        conn.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                     ("legacy-owner", "fixture-hash", "2026-01-01 00:00:00"))
        conn.execute("INSERT INTO api_keys(user_id,name,key,created_at) VALUES(?,?,?,?)",
                     (1, "legacy", "fixture-only-key", "2026-01-01 00:00:00"))
        conn.execute(
            "INSERT INTO channels(name,type,base_url,api_key,models,priority,weight,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("legacy-channel", "openai", "https://example.test/v1", "fixture-upstream-key",
             "legacy-model", 7, 3, 1, "2026-01-01 00:00:00"),
        )
        conn.commit()
        conn.close()

        database.init_db()

        check_conn = database.get_db()
        columns = {row[1] for row in check_conn.execute("PRAGMA table_info(channels)")}
        check_conn.close()
        channel = database.query_one("SELECT * FROM channels WHERE id=1")
        api_key = database.query_one("SELECT key FROM api_keys WHERE id=1")
        self.assertIn("model_mapping", columns)
        self.assertIn("tool_policy", columns)
        self.assertEqual(channel["name"], "legacy-channel")
        self.assertEqual(channel["api_key"], "fixture-upstream-key")
        self.assertEqual(channel["models"], "legacy-model")
        self.assertEqual(channel["priority"], 7)
        self.assertEqual(channel["weight"], 3)
        self.assertEqual(channel["model_mapping"], "{}")
        self.assertEqual(channel["tool_policy"], "preserve")
        self.assertIn("detected_protocol", columns)
        self.assertIn("protocol_capabilities", columns)
        self.assertIn("last_success_protocol", columns)
        self.assertIn("last_protocol_check_at", columns)
        self.assertEqual(api_key["key"], "fixture-only-key")

    def test_channel_mapping_and_tool_policy_are_validated(self):
        with TestClient(app) as client:
            setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
            headers = {"Authorization": "Bearer " + setup["token"]}
            for value in ([], {"model-a": ""}, {"": "upstream"}, {"model-a": 1}):
                response = client.post("/api/channels", headers=headers, json={
                    "name": "invalid", "type": "mock", "models": "model-a", "model_mapping": value,
                })
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("model_mapping", response.text)
            invalid_policy = client.post("/api/channels", headers=headers, json={
                "name": "invalid-policy", "type": "mock", "models": "model-a", "tool_policy": "auto",
            })
        self.assertEqual(invalid_policy.status_code, 400)
        self.assertIn("tool_policy", invalid_policy.text)

    def test_channel_list_exposes_safe_mapping_and_never_api_key(self):
        with TestClient(app) as client:
            headers, _ = self._configure_gateway(
                client, "http://127.0.0.1:1", model_mapping={"model-a": "upstream-a"}, tool_policy="strip"
            )
            channels = client.get("/api/channels", headers=headers)
        self.assertEqual(channels.status_code, 200)
        self.assertNotIn("api_key", channels.json()[0])
        self.assertEqual(channels.json()[0]["model_mapping"], {"model-a": "upstream-a"})
        self.assertEqual(channels.json()[0]["tool_policy"], "strip")

    def test_setup_api_creates_the_only_initial_admin(self):
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/setup/status").json(), {"needs_setup": True})
            response = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["user"]["role"], "admin")
            self.assertEqual(client.get("/api/setup/status").json(), {"needs_setup": False})

    def test_channel_list_does_not_return_upstream_api_keys(self):
        upstream_secret = "upstream-secret-never-return"
        with TestClient(app) as client:
            setup = client.post(
                "/api/setup", json={"username": "owner", "password": "a-long-password"}
            ).json()
            admin_headers = {"Authorization": "Bearer " + setup["token"]}
            client.post("/api/channels", headers=admin_headers, json={
                "name": "private-upstream",
                "type": "openai",
                "base_url": "https://example.com/v1",
                "api_key": upstream_secret,
                "models": "model-a",
            })
            response = client.get("/api/channels", headers=admin_headers)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("api_key", response.json()[0])
        self.assertNotIn(upstream_secret, response.text)

    def test_missing_gateway_key_is_distinguished_from_upstream_failure(self):
        with TestClient(app) as client:
            response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer not-a-real-key"}, json={
                "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["type"], "invalid_api_key")
        self.assertIn("网关 API Key 无效", response.json()["error"]["message"])

    def test_password_hash_is_versioned_and_accepts_legacy_hashes(self):
        legacy_salt = "legacy"
        legacy_digest = hashlib.pbkdf2_hmac("sha256", b"password", legacy_salt.encode(), 60_000).hex()
        self.assertTrue(database.verify_password("password", f"{legacy_salt}${legacy_digest}"))
        self.assertEqual(len(database.hash_password("password").split("$")), 3)

    def test_cross_origin_preflight_is_not_allowed_by_default(self):
        with TestClient(app) as client:
            response = client.options("/health", headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "GET",
            })
        self.assertNotEqual(response.headers.get("access-control-allow-origin"), "*")

    def test_console_assets_are_not_served_from_stale_browser_cache(self):
        with TestClient(app) as client:
            response = client.get("/static/app.js")
        self.assertEqual(response.headers.get("cache-control"), "no-cache")

    def test_security_headers_are_present_without_changing_health_response(self):
        with TestClient(app) as client:
            response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(response.headers.get("x-frame-options"), "DENY")
        self.assertEqual(response.headers.get("referrer-policy"), "same-origin")

    def test_console_includes_copyable_client_connection_examples(self):
        script = Path("static/app.js").read_text(encoding="utf-8")
        for label in ("Harness", "Continue", "Open WebUI", "OpenAI Python", "Base URL", "API Key"):
            self.assertIn(label, script)
        self.assertIn("自动加载失败，可手动填写模型 ID", script)

    def test_docker_templates_keep_data_out_of_image_and_bind_loopback(self):
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        dockerignore = Path(".dockerignore").read_text(encoding="utf-8")
        self.assertIn('"127.0.0.1:8000:8000"', compose)
        self.assertIn("./data:/app/data", compose)
        self.assertIn("/health", compose)
        self.assertIn("data/", dockerignore)
        self.assertNotIn("obsessionapi.db", dockerfile)
        self.assertNotIn("obsessionapi.db", compose)

    def test_seekai_attribution_is_removed_only_from_final_nonstream_tail(self):
        class SeekAIHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                response = {
                    "choices": [{"message": {"content": (
                        "正文 BY https://example.com\n"
                        "代码块：```\nBY https://seekai.cc\n```\n\n"
                        "最后一段\nBY https://seekai.cc"
                    )}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), SeekAIHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}")
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        content = response.json()["choices"][0]["message"]["content"]
        self.assertEqual(content, "正文 BY https://example.com\n代码块：```\nBY https://seekai.cc\n```\n\n最后一段")

    def test_seekai_attribution_is_not_leaked_by_native_stream(self):
        class SeekAIStreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                events = [
                    {"choices": [{"delta": {"role": "assistant"}}]},
                    {"choices": [{"delta": {"content": "流式正文\n"}}]},
                    {"choices": [{"delta": {"content": "BY [SeekAI](https://seekai.cc)"}}]},
                    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                ]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in events:
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), SeekAIStreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}")
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200)
        self.assertIn("流式正文", response.text)
        self.assertNotIn("seekai.cc", response.text.lower())
        self.assertNotIn("by [seekai]", response.text.lower())
        self.assertIn("data: [DONE]", response.text)

    def test_stream_done_without_finish_reason_gets_stop_before_done(self):
        response = self._stream_response([
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "正文"}}]},
            "[DONE]",
        ])
        payloads = _sse_payloads(response.text)
        decoded = [json.loads(payload) for payload in payloads if payload != "[DONE]"]
        stops = [
            event for event in decoded
            if (event.get("choices") or [{}])[0].get("finish_reason") == "stop"
        ]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["choices"][0]["delta"], {})
        self.assertEqual(payloads.count("[DONE]"), 1)
        self.assertLess(
            next(index for index, event in enumerate(decoded)
                 if (event.get("choices") or [{}])[0].get("finish_reason") == "stop"),
            payloads.index("[DONE]"),
        )

    def test_stream_usage_chunk_without_finish_reason_gets_stop(self):
        response = self._stream_response([
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "SM"}}]},
            {"choices": [{"delta": {"content": "OKE"}}]},
            {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 3, "total_tokens": 4}},
            "[DONE]",
        ])
        payloads = _sse_payloads(response.text)
        decoded = [json.loads(payload) for payload in payloads if payload != "[DONE]"]
        self.assertEqual(sum(
            (event.get("choices") or [{}])[0].get("finish_reason") == "stop"
            for event in decoded
        ), 1)
        self.assertEqual(payloads.count("[DONE]"), 1)

    def test_stream_existing_finish_reason_is_not_duplicated(self):
        response = self._stream_response([
            {"choices": [{"delta": {"content": "正文"}}]},
            {"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}]},
            "[DONE]",
        ])
        payloads = _sse_payloads(response.text)
        decoded = [json.loads(payload) for payload in payloads if payload != "[DONE]"]
        self.assertEqual(sum(
            (event.get("choices") or [{}])[0].get("finish_reason") == "stop"
            for event in decoded
        ), 1)
        self.assertEqual(payloads.count("[DONE]"), 1)

    def test_stream_error_does_not_synthesize_stop(self):
        response = self._stream_response([
            {"choices": [{"delta": {"content": "部分正文"}}]},
            {"error": {"message": "upstream stream failed"}},
            "[DONE]",
        ])
        payloads = _sse_payloads(response.text)
        decoded = [json.loads(payload) for payload in payloads if payload != "[DONE]"]
        self.assertFalse(any(
            (event.get("choices") or [{}])[0].get("finish_reason") == "stop"
            for event in decoded
        ))
        self.assertTrue(any("error" in event for event in decoded))
        self.assertEqual(payloads.count("[DONE]"), 1)

    def test_stream_connection_error_does_not_synthesize_stop(self):
        class BrokenStreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(
                    b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
                )
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), BrokenStreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}")
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["type"], "server_error")
        self.assertEqual(response.headers["x-request-id"], response.json()["request_id"])

    def test_stream_emits_only_one_done(self):
        response = self._stream_response([
            {"choices": [{"delta": {"content": "正文"}}]},
            "[DONE]",
            "[DONE]",
        ])
        self.assertEqual(_sse_payloads(response.text).count("[DONE]"), 1)

    def test_stream_tool_calls_are_forwarded_as_valid_output(self):
        backup_requests = []

        class ToolStreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                events = [
                    {"choices": [{"delta": {"role": "assistant"}}]},
                    {"choices": [{"delta": {"tool_calls": [{
                        "index": 0, "id": "call_1", "type": "function",
                        "function": {"name": "weather", "arguments": "{}"},
                    }]}}]},
                    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                    "[DONE]",
                ]
                for event in events:
                    payload = event if isinstance(event, str) else json.dumps(event)
                    self.wfile.write(("data: " + payload + "\n\n").encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), ToolStreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}")
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "call weather"}],
                    "stream": True, "tools": [{"type": "function", "function": {"name": "weather"}}],
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        payloads = _sse_payloads(response.text)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(any("tool_calls" in payload for payload in payloads))
        self.assertTrue(any(
            (event.get("choices") or [{}])[0].get("finish_reason") == "tool_calls"
            for event in (json.loads(payload) for payload in payloads if payload != "[DONE]")
        ))
        self.assertEqual(payloads.count("[DONE]"), 1)
        self.assertEqual(backup_requests, [])

    def test_empty_sse_done_is_protocol_error_and_switches_to_backup(self):
        requests = []

        class EmptyStreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                requests.append("empty")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *_):
                pass

        class WorkingStreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                requests.append("working")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"backup"}}]}\n\n')
                self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *_):
                pass

        primary = ThreadingHTTPServer(("127.0.0.1", 0), EmptyStreamHandler)
        backup = ThreadingHTTPServer(("127.0.0.1", 0), WorkingStreamHandler)
        primary_thread = threading.Thread(target=primary.serve_forever, daemon=True)
        backup_thread = threading.Thread(target=backup.serve_forever, daemon=True)
        primary_thread.start()
        backup_thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                client.post("/api/channels", headers=headers, json={
                    "name": "empty-stream-primary", "type": "openai",
                    "base_url": f"http://127.0.0.1:{primary.server_port}",
                    "api_key": "primary-fixture-key", "models": "model-a", "priority": 10,
                })
                client.post("/api/channels", headers=headers, json={
                    "name": "working-stream-backup", "type": "openai",
                    "base_url": f"http://127.0.0.1:{backup.server_port}",
                    "api_key": "backup-fixture-key", "models": "model-a", "priority": 0,
                })
                key = client.post("/api/keys", headers=headers, json={
                    "name": "test", "user_id": setup["user"]["id"],
                }).json()["key"]
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                })
        finally:
            primary.shutdown()
            backup.shutdown()
            primary_thread.join()
            backup_thread.join()
            primary.server_close()
            backup.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("backup", response.text)
        self.assertEqual(requests, ["empty", "empty", "working"])
        self.assertEqual(_sse_payloads(response.text).count("[DONE]"), 1)

    def test_stream_tail_filter_releases_safe_content_before_stream_end(self):
        from app.gateway import _SeekAITailFilter

        tail_filter = _SeekAITailFilter()
        self.assertEqual(tail_filter.feed("流式正文\n"), "流式正文\n")
        self.assertEqual(tail_filter.feed("BY https://seekai.cc"), "")
        self.assertEqual(tail_filter.finish(), "")

    def test_reasoning_text_and_length_keep_budget_and_do_not_cool_channel(self):
        requests = []

        class ReasoningHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(_read_request_body(self))
                requests.append(payload)
                if payload.get("max_tokens") == 7:
                    response = {
                        "choices": [{"message": {"role": "assistant", "content": None,
                                                   "reasoning_content": "thinking"}, "finish_reason": "length"}],
                        "usage": {"prompt_tokens": 2, "completion_tokens": 7},
                    }
                else:
                    response = {
                        "choices": [{"message": {"role": "assistant", "content": "OK",
                                                   "reasoning_content": "thinking"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                    }
                body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), ReasoningHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}")
                auth = {"Authorization": "Bearer " + key}
                normal = client.post("/v1/chat/completions", headers=auth, json={
                    "model": "model-a", "max_tokens": 11, "messages": [{"role": "user", "content": "OK"}],
                })
                incomplete = client.post("/v1/chat/completions", headers=auth, json={
                    "model": "model-a", "max_tokens": 7, "messages": [{"role": "user", "content": "OK"}],
                })
                logs = database.query(
                    "SELECT status,status_code,error_category,will_retry,error_msg FROM request_logs ORDER BY id"
                )
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(normal.status_code, 200, normal.text)
        self.assertEqual(normal.json()["choices"][0]["message"]["content"], "OK")
        self.assertEqual(incomplete.status_code, 200, incomplete.text)
        self.assertEqual(incomplete.headers.get("X-OBSESSIONAPI-Completion-State"), "incomplete")
        self.assertEqual(incomplete.json()["choices"][0]["finish_reason"], "length")
        self.assertEqual([request["max_tokens"] for request in requests], [11, 7])
        self.assertEqual(logs[-1]["status"], "error")
        self.assertEqual(logs[-1]["status_code"], 200)
        self.assertEqual(logs[-1]["error_category"], "incomplete_output")
        self.assertEqual(logs[-1]["will_retry"], 0)
        self.assertIn("request_path=/v1/chat/completions", logs[-1]["error_msg"])
        self.assertIn("stream=false", logs[-1]["error_msg"])
        self.assertIn("protocol=openai_chat", logs[-1]["error_msg"])
        self.assertIn("upstream_content_type=application/json", logs[-1]["error_msg"])
        self.assertIn("choices_fields=finish_reason,message", logs[-1]["error_msg"])
        self.assertIn("message_fields=content,reasoning_content,role", logs[-1]["error_msg"])
        self.assertIn("content=empty", logs[-1]["error_msg"])
        self.assertIn("reasoning=nonempty", logs[-1]["error_msg"])
        self.assertIn("finish_reason=length", logs[-1]["error_msg"])
        self.assertIn("prompt_tokens=2", logs[-1]["error_msg"])
        self.assertIn("completion_tokens=7", logs[-1]["error_msg"])
        self.assertIn("converted_stop_reason=not_applicable", logs[-1]["error_msg"])

    def test_anthropic_messages_maps_length_to_max_tokens_without_faking_text(self):
        class ReasoningOnlyHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                body = json.dumps({
                    "choices": [{"message": {"role": "assistant", "content": None,
                                               "reasoning_content": "thinking"}, "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 7},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), ReasoningOnlyHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}")
                response = client.post("/v1/messages", headers={"x-api-key": key}, json={
                    "model": "model-a", "max_tokens": 7, "messages": [{"role": "user", "content": "OK"}],
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["stop_reason"], "max_tokens")
        self.assertEqual(payload["content"], [])

    def test_stream_reasoning_then_text_and_reasoning_length_keep_native_semantics(self):
        text_response = self._stream_response([
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
            {"choices": [{"delta": {"content": "OK"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            "[DONE]",
        ])
        self.assertEqual(text_response.status_code, 200, text_response.text)
        self.assertIn("reasoning_content", text_response.text)
        self.assertIn('"content": "OK"', text_response.text)


    def test_stream_reasoning_length_keeps_native_semantics(self):
        length_response = self._stream_response([
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}], "usage": {"prompt_tokens": 2, "completion_tokens": 7}},
            "[DONE]",
        ])
        self.assertEqual(length_response.status_code, 200, length_response.text)
        self.assertEqual(length_response.headers.get("X-OBSESSIONAPI-Completion-State"), "incomplete")
        self.assertRegex(length_response.text, r'"finish_reason":\s*"length"')
        self.assertEqual(_sse_payloads(length_response.text).count("[DONE]"), 1)

    def test_legacy_completions_translates_prompt_to_chat_messages(self):
        requests = []

        class CompletionHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(_read_request_body(self))
                requests.append(payload)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "model": "model-a",
                    "choices": [{"message": {"role": "assistant", "content": "from-upstream"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }).encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), CompletionHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}")
                response = client.post("/v1/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "prompt": "legacy prompt", "max_tokens": 5,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "from-upstream")
        self.assertEqual(requests[0]["messages"], [{"role": "user", "content": "legacy prompt"}])
        self.assertNotIn("prompt", requests[0])

    def test_upstream_502_uses_openai_server_error_semantics(self):
        class BadGatewayHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                self.send_response(502)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body>upstream gateway unavailable</body></html>")

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), BadGatewayHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}")
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 502)
        error = response.json()["error"]
        self.assertEqual(error["type"], "server_error")
        self.assertEqual(error["code"], 502)
        self.assertIn("seekai", error["message"])
        self.assertIn("502", error["message"])
        self.assertIn("HTML 错误页", error["message"])
        self.assertIn("供应商源站或 Cloudflare 故障", error["message"])
        self.assertNotIn("<html", response.text.lower())
        self.assertNotIn("<!doctype", response.text.lower())
        self.assertNotIn("AUTH", response.text)

    def test_upstream_json_400_preserves_error_message_without_html(self):
        class BadRequestHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"invalid request body"}}')

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), BadRequestHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}")
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 502)
        message = response.json()["error"]["message"]
        self.assertIn("上游返回 400", message)
        self.assertIn("invalid request body", message)
        self.assertNotIn("<html", response.text.lower())
        self.assertNotIn("<!doctype", response.text.lower())

    def test_upstream_200_non_json_is_protocol_error_and_switches_to_backup(self):
        primary_requests, backup_requests = [], []

        class PrimaryHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                primary_requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"not-json")

            def log_message(self, *_):
                pass

        class BackupHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                backup_requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"choices":[{"message":{"content":"backup-text"}}]}')

            def log_message(self, *_):
                pass

        primary = ThreadingHTTPServer(("127.0.0.1", 0), PrimaryHandler)
        backup = ThreadingHTTPServer(("127.0.0.1", 0), BackupHandler)
        primary_thread = threading.Thread(target=primary.serve_forever, daemon=True)
        backup_thread = threading.Thread(target=backup.serve_forever, daemon=True)
        primary_thread.start()
        backup_thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                client.post("/api/channels", headers=headers, json={
                    "name": "protocol-primary", "type": "openai",
                    "base_url": f"http://127.0.0.1:{primary.server_port}",
                    "api_key": "primary-fixture-key", "models": "model-a", "priority": 10,
                })
                client.post("/api/channels", headers=headers, json={
                    "name": "protocol-backup", "type": "openai",
                    "base_url": f"http://127.0.0.1:{backup.server_port}",
                    "api_key": "backup-fixture-key", "models": "model-a", "priority": 0,
                })
                key = client.post("/api/keys", headers=headers, json={
                    "name": "test", "user_id": setup["user"]["id"],
                }).json()["key"]
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
                })
        finally:
            primary.shutdown()
            backup.shutdown()
            primary_thread.join()
            backup_thread.join()
            primary.server_close()
            backup.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "backup-text")
        self.assertEqual(len(primary_requests), 1)
        self.assertEqual(len(backup_requests), 1)
        logs = database.query(
            "SELECT status,status_code,error_msg,will_retry FROM request_logs WHERE request_id=? ORDER BY id",
            (response.headers["x-request-id"],),
        )
        self.assertEqual([(row["status"], row["status_code"], row["will_retry"]) for row in logs], [
            ("error", 502, 1), ("success", 200, 0),
        ])
        self.assertIn("协议错误", logs[0]["error_msg"])

    def test_upstream_200_empty_choices_is_protocol_error_and_switches_to_backup(self):
        requests = []

        class EmptyHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                requests.append("empty")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"choices":[]}')

            def log_message(self, *_):
                pass

        class WorkingHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                requests.append("working")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"choices":[{"message":{"content":"ok"}}]}')

            def log_message(self, *_):
                pass

        primary = ThreadingHTTPServer(("127.0.0.1", 0), EmptyHandler)
        backup = ThreadingHTTPServer(("127.0.0.1", 0), WorkingHandler)
        primary_thread = threading.Thread(target=primary.serve_forever, daemon=True)
        backup_thread = threading.Thread(target=backup.serve_forever, daemon=True)
        primary_thread.start()
        backup_thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                client.post("/api/channels", headers=headers, json={
                    "name": "empty-primary", "type": "openai",
                    "base_url": f"http://127.0.0.1:{primary.server_port}",
                    "api_key": "primary-fixture-key", "models": "model-a", "priority": 10,
                })
                client.post("/api/channels", headers=headers, json={
                    "name": "empty-backup", "type": "openai",
                    "base_url": f"http://127.0.0.1:{backup.server_port}",
                    "api_key": "backup-fixture-key", "models": "model-a", "priority": 0,
                })
                key = client.post("/api/keys", headers=headers, json={
                    "name": "test", "user_id": setup["user"]["id"],
                }).json()["key"]
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
                })
        finally:
            primary.shutdown()
            backup.shutdown()
            primary_thread.join()
            backup_thread.join()
            primary.server_close()
            backup.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "ok")
        self.assertEqual(requests, ["empty", "working"])

    def test_sse_error_after_content_is_logged_as_error_and_stream_stays_valid(self):
        class ErrorStreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')
                self.wfile.write(b'data: {"error":{"message":"upstream stream failed"}}\n\n')
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), ErrorStreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}")
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200)
        self.assertIn("partial", response.text)
        self.assertIn('"error"', response.text)
        self.assertIn("data: [DONE]", response.text)
        log = database.query_one("SELECT status, status_code FROM request_logs ORDER BY id DESC LIMIT 1")
        self.assertEqual(log["status"], "error")
        self.assertEqual(log["status_code"], 502)

    def test_first_channel_502_switches_once_to_backup_channel(self):
        first_requests, backup_requests = [], []

        class FirstHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                first_requests.append(self.path)
                self.send_response(502)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html>first upstream failed</html>")

            def log_message(self, *_):
                pass

        class BackupHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                backup_requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "choices": [{"message": {"content": "backup-ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }).encode())

            def log_message(self, *_):
                pass

        first = ThreadingHTTPServer(("127.0.0.1", 0), FirstHandler)
        backup = ThreadingHTTPServer(("127.0.0.1", 0), BackupHandler)
        first_thread = threading.Thread(target=first.serve_forever, daemon=True)
        backup_thread = threading.Thread(target=backup.serve_forever, daemon=True)
        first_thread.start()
        backup_thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                admin_headers = {"Authorization": "Bearer " + setup["token"]}
                client.post("/api/channels", headers=admin_headers, json={
                    "name": "primary", "type": "openai", "base_url": f"http://127.0.0.1:{first.server_port}",
                    "api_key": "first-key", "models": "model-a", "priority": 10,
                })
                client.post("/api/channels", headers=admin_headers, json={
                    "name": "backup", "type": "openai", "base_url": f"http://127.0.0.1:{backup.server_port}",
                    "api_key": "backup-key", "models": "model-a", "priority": 0,
                })
                key = client.post("/api/keys", headers=admin_headers, json={
                    "name": "test", "user_id": setup["user"]["id"],
                }).json()["key"]
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
                })
                logs = client.get("/api/logs", headers=admin_headers).json()["items"]
        finally:
            first.shutdown()
            backup.shutdown()
            first_thread.join()
            backup_thread.join()
            first.server_close()
            backup.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "backup-ok")
        self.assertEqual(first_requests, ["/v1/chat/completions"])
        self.assertEqual(backup_requests, ["/v1/chat/completions"])
        self.assertEqual({log["channel_name"] for log in logs}, {"primary", "backup"})
        retry_log = next(log for log in logs if log["channel_name"] == "primary")
        success_log = next(log for log in logs if log["channel_name"] == "backup")
        self.assertEqual(retry_log["status"], "error")
        self.assertEqual(retry_log["will_retry"], 1)
        self.assertEqual(success_log["status"], "success")
        self.assertEqual(success_log["will_retry"], 0)

    def test_stream_failure_after_content_does_not_switch_to_backup(self):
        backup_requests = []

        class PartialFailureHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')
                self.wfile.write(b'data: {"error":{"message":"stream failed after content"}}\n\n')
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *_):
                pass

        class BackupHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                backup_requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"choices":[{"message":{"content":"must-not-run"}}]}')

            def log_message(self, *_):
                pass

        first = ThreadingHTTPServer(("127.0.0.1", 0), PartialFailureHandler)
        backup = ThreadingHTTPServer(("127.0.0.1", 0), BackupHandler)
        first_thread = threading.Thread(target=first.serve_forever, daemon=True)
        backup_thread = threading.Thread(target=backup.serve_forever, daemon=True)
        first_thread.start()
        backup_thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                admin_headers = {"Authorization": "Bearer " + setup["token"]}
                client.post("/api/channels", headers=admin_headers, json={
                    "name": "stream-primary", "type": "openai", "base_url": f"http://127.0.0.1:{first.server_port}",
                    "api_key": "first-key", "models": "model-a", "priority": 10,
                })
                client.post("/api/channels", headers=admin_headers, json={
                    "name": "stream-backup", "type": "openai", "base_url": f"http://127.0.0.1:{backup.server_port}",
                    "api_key": "backup-key", "models": "model-a", "priority": 0,
                })
                key = client.post("/api/keys", headers=admin_headers, json={
                    "name": "test", "user_id": setup["user"]["id"],
                }).json()["key"]
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                })
                logs = client.get("/api/logs", headers=admin_headers).json()["items"]
        finally:
            first.shutdown()
            backup.shutdown()
            first_thread.join()
            backup_thread.join()
            first.server_close()
            backup.server_close()
        self.assertEqual(response.status_code, 200)
        self.assertIn("partial", response.text)
        self.assertIn("data: [DONE]", response.text)
        self.assertEqual(backup_requests, [])
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["status"], "error")
        self.assertEqual(logs[0]["will_retry"], 0)

    def test_seekai_attribution_is_removed_after_stream_fallback_to_nonstream(self):
        requests = []

        class SeekAIFallbackHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(_read_request_body(self))
                requests.append(payload)
                self.send_response(200)
                if payload.get("stream"):
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')
                    self.wfile.write(b'data: {"error":{"message":"The upstream chat request failed."}}\n\n')
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "choices": [{"message": {"content": "降级正文\nBY https://seekai.cc"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }).encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), SeekAIFallbackHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}")
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200)
        self.assertIn("降级正文", response.text)
        self.assertNotIn("seekai.cc", response.text.lower())
        self.assertIn("data: [DONE]", response.text)
        self.assertEqual(len(requests), 2)
        self.assertNotIn("stream", requests[1])

    def test_channel_test_reports_cloudflare_403_with_channel_name(self):
        class CloudflareHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":[{"id":"gpt-5.6-terra"}]}')

            def do_POST(self):
                _read_request_body(self)
                self.send_response(403)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<!DOCTYPE html><html><title>Cloudflare</title>access denied</html>")

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), CloudflareHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                admin_headers, _ = self._configure_gateway(
                    client, f"http://127.0.0.1:{upstream.server_port}", model="gpt-5.6-terra", name="justwoker"
                )
                channel_id = client.get("/api/channels", headers=admin_headers).json()[0]["id"]
                response = client.post(f"/api/channels/{channel_id}/test", headers=admin_headers)
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["channel_name"], "justwoker")
        self.assertEqual(result["test_model"], "gpt-5.6-terra")
        self.assertEqual(result["request_url"], f"http://127.0.0.1:{upstream.server_port}/v1/chat/completions")
        self.assertEqual(result["status_code"], 403)
        self.assertEqual(result["model_list_status_code"], 200)
        self.assertNotIn("test-key", response.text)
        self.assertIn("justwoker", result["message"])
        self.assertIn("403", result["message"])
        self.assertIn("Cloudflare/403", result["message"])
        self.assertIn("模型列表正常、推理失败", result["message"])
        self.assertIn("检查 API 地址、Key 或供应商访问限制", result["message"])
        self.assertNotIn("<html", response.text.lower())
        self.assertNotIn("<!doctype", response.text.lower())

    def test_channel_test_runs_real_inference_without_usage_log(self):
        requests = []

        class InferenceHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":[{"id":"model-a"}]}')

            def do_POST(self):
                requests.append((self.path, json.loads(_read_request_body(self))))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"choices":[{"message":{"content":"OK"}}]}')

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), InferenceHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                admin_headers, _ = self._configure_gateway(
                    client, f"http://127.0.0.1:{upstream.server_port}", model="model-a", name="true-sota"
                )
                before_logs = database.query_one("SELECT COUNT(*) c FROM request_logs")["c"]
                before_user = database.query_one("SELECT used_quota FROM users WHERE username='owner'")["used_quota"]
                before_key = database.query_one("SELECT used_quota FROM api_keys WHERE name='test'")["used_quota"]
                channel_id = client.get("/api/channels", headers=admin_headers).json()[0]["id"]
                response = client.post(f"/api/channels/{channel_id}/test", headers=admin_headers)
                after_logs = database.query_one("SELECT COUNT(*) c FROM request_logs")["c"]
                after_user = database.query_one("SELECT used_quota FROM users WHERE username='owner'")["used_quota"]
                after_key = database.query_one("SELECT used_quota FROM api_keys WHERE name='test'")["used_quota"]
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        result = response.json()
        self.assertTrue(result["ok"], response.text)
        self.assertEqual(result["channel_name"], "true-sota")
        self.assertEqual(result["test_model"], "model-a")
        self.assertEqual(result["request_url"], f"http://127.0.0.1:{upstream.server_port}/v1/chat/completions")
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["model_list_status_code"], 200)
        self.assertGreaterEqual(result["latency_ms"], 0)
        self.assertIn("可能产生极少量额度", result["message"])
        self.assertEqual(requests, [(
            "/v1/chat/completions",
            {"model": "model-a", "messages": [{"role": "user", "content": "Reply only OK"}], "max_tokens": 4096, "stream": False},
        )])
        self.assertEqual(after_logs, before_logs)
        self.assertEqual(after_user, before_user)
        self.assertEqual(after_key, before_key)
        self.assertNotIn("test-key", response.text)

    def test_channel_test_requires_a_bound_model(self):
        with TestClient(app) as client:
            admin_headers, _ = self._configure_gateway(
                client, "http://127.0.0.1:1", model="", name="empty-model-channel"
            )
            channel_id = client.get("/api/channels", headers=admin_headers).json()[0]["id"]
            response = client.post(f"/api/channels/{channel_id}/test", headers=admin_headers)
        result = response.json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 400)
        self.assertEqual(result["message"], "请先填写测试模型")

    def test_gpt_channel_403_keeps_channel_name_and_diagnostic(self):
        class CloudflareChatHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"cloudflare forbidden"}}')

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), CloudflareChatHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(
                    client, f"http://127.0.0.1:{upstream.server_port}", model="gpt-5.6-terra", name="justwoker"
                )
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "gpt-5.6-terra", "messages": [{"role": "user", "content": "hi"}],
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 502)
        message = response.json()["error"]["message"]
        self.assertIn("justwoker", message)
        self.assertIn("403", message)
        self.assertIn("Cloudflare/403", message)
        self.assertEqual(response.json()["error"]["type"], "server_error")
        self.assertNotIn("invalid_api_key", response.text)
        self.assertNotIn("网关 API Key 无效", message)
        self.assertNotEqual(message, "所有可用渠道均调用失败，请到控制台查看请求日志或测试渠道连通性。")

    def test_key_quota_limit_blocks_request_before_mock_upstream(self):
        with TestClient(app) as client:
            setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
            admin_headers = {"Authorization": "Bearer " + setup["token"]}
            client.post("/api/models", headers=admin_headers, json={"model_name": "test-model"})
            client.post("/api/channels", headers=admin_headers, json={
                "name": "test-mock", "type": "mock", "models": "test-model",
            })
            key = client.post("/api/keys", headers=admin_headers, json={
                "name": "limited", "user_id": setup["user"]["id"], "quota_limit": 1,
            }).json()["key"]
            database.execute("UPDATE api_keys SET used_quota=1 WHERE key=?", (key,))
            response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                "model": "test-model", "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["type"], "insufficient_quota")

    def test_channel_save_normalizes_full_request_url_and_registers_models(self):
        with TestClient(app) as client:
            setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
            headers = {"Authorization": "Bearer " + setup["token"]}
            response = client.post("/api/channels", headers=headers, json={
                "name": "seekai", "type": "openai",
                "base_url": "https://seekai.cc/v1/chat/completions/",
                "api_key": "test-key", "models": "model-a, model-b",
            })
            channels = client.get("/api/channels", headers=headers).json()
            models = client.get("/api/models", headers=headers).json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(channels[0]["base_url"], "https://seekai.cc/v1")
        self.assertEqual({model["model_name"] for model in models}, {"model-a", "model-b"})

    def test_openai_base_url_forms_normalize_to_one_v1_root(self):
        from app.gateway import normalize_openai_base_url

        for value in (
            "https://example.com",
            "https://example.com/v1",
            "https://example.com/v1/",
            "https://example.com/v1/chat/completions",
            "https://example.com/v1/chat/completions/",
        ):
            self.assertEqual(normalize_openai_base_url(value), "https://example.com/v1")

    def test_v1_models_lists_only_models_with_enabled_channels(self):
        with TestClient(app) as client:
            setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
            headers = {"Authorization": "Bearer " + setup["token"]}
            client.post("/api/models", headers=headers, json={"model_name": "orphan-model"})
            client.post("/api/channels", headers=headers, json={
                "name": "test-mock", "type": "mock", "models": "model-a",
            })
            response = client.get("/v1/models")
        self.assertEqual(response.status_code, 200)
        model_ids = {item["id"] for item in response.json()["data"]}
        self.assertEqual(model_ids, {"model-a"})

    def test_discover_models_normalizes_root_url_and_returns_upstream_models(self):
        class ModelsHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/v1/models" else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"data": [{"id": "model-a"}, {"id": "model-b"}]}).encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), ModelsHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                response = client.post("/api/channels/discover-models", headers=headers, json={
                    "base_url": f"http://127.0.0.1:{upstream.server_port}", "api_key": "test-key",
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "base_url": f"http://127.0.0.1:{upstream.server_port}/v1",
            "models": ["model-a", "model-b"],
        })

    def test_discover_models_failure_keeps_manual_model_usable(self):
        class FailingModelsHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(502)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html>upstream unavailable</html>")

            def do_POST(self):
                _read_request_body(self)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "choices": [{"message": {"content": "manual-model-ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }).encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), FailingModelsHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                admin_headers = {"Authorization": "Bearer " + setup["token"]}
                base_url = f"http://127.0.0.1:{upstream.server_port}"
                discover = client.post("/api/channels/discover-models", headers=admin_headers, json={
                    "base_url": base_url, "api_key": "test-key",
                })
                channel = client.post("/api/channels", headers=admin_headers, json={
                    "name": "manual-upstream", "type": "openai", "base_url": base_url,
                    "api_key": "test-key", "models": "manual-model",
                })
                key = client.post("/api/keys", headers=admin_headers, json={
                    "name": "test", "user_id": setup["user"]["id"],
                }).json()["key"]
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "manual-model", "messages": [{"role": "user", "content": "hi"}],
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(discover.status_code, 502)
        self.assertFalse(discover.json()["ok"])
        self.assertIn("自动加载失败，可手动填写模型 ID", discover.json()["detail"])
        self.assertEqual(channel.status_code, 200, channel.text)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "manual-model-ok")

    def test_channel_name_is_derived_from_its_url_when_blank(self):
        with TestClient(app) as client:
            setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
            headers = {"Authorization": "Bearer " + setup["token"]}
            response = client.post("/api/channels", headers=headers, json={
                "name": "", "type": "openai", "base_url": "https://seekai.cc/v1", "models": "model-a",
            })
            channels = client.get("/api/channels", headers=headers).json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(channels[0]["name"], "seekai")

    def test_preserve_tools_unsupported_does_not_retry_same_channel(self):
        requests = []

        class TextOnlyHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(_read_request_body(self))
                requests.append(payload)
                if "tools" in payload:
                    self.send_response(400)
                    response = {"error": {"message": "tools is not supported; this gateway supports text generation only"}}
                else:
                    self.send_response(200)
                    response = {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), TextOnlyHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                admin_headers = {"Authorization": "Bearer " + setup["token"]}
                channel = client.post("/api/channels", headers=admin_headers, json={
                    "name": "text-only", "type": "openai", "base_url": f"http://127.0.0.1:{upstream.server_port}",
                    "api_key": "test-key", "models": "model-a",
                })
                key = client.post("/api/keys", headers=admin_headers, json={
                    "name": "test", "user_id": setup["user"]["id"],
                }).json()["key"]
                channels = client.get("/api/channels", headers=admin_headers).json()
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
                    "tools": [{"type": "function", "function": {"name": "weather"}}], "tool_choice": "auto",
                })
                nonstream_requests = list(requests)
                requests.clear()
                stream_response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                    "tools": [{"type": "function", "function": {"name": "weather"}}], "tool_choice": "auto",
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(channel.status_code, 200, channel.text)
        self.assertEqual(response.status_code, 400, f"{response.text}; channels={channels}; attempts={nonstream_requests}")
        self.assertEqual(len(nonstream_requests), 1)
        self.assertIn("tools", nonstream_requests[0])
        self.assertEqual(stream_response.status_code, 400)
        self.assertEqual(len(requests), 1)
        self.assertIn("tools", requests[0])

    def test_stream_error_before_content_falls_back_to_nonstream(self):
        requests = []

        class BrokenStreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(_read_request_body(self))
                requests.append(payload)
                self.send_response(200)
                if payload.get("stream"):
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')
                    self.wfile.write(b'data: {"error":{"message":"The upstream chat request failed."}}\n\n')
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }).encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), BrokenStreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                client.post("/api/channels", headers=headers, json={
                    "name": "broken-stream", "type": "openai", "base_url": f"http://127.0.0.1:{upstream.server_port}",
                    "api_key": "test-key", "models": "model-a",
                })
                key = client.post("/api/keys", headers=headers, json={
                    "name": "test", "user_id": setup["user"]["id"],
                }).json()["key"]
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200)
        self.assertIn("ok", response.text)
        self.assertNotIn("upstream chat request failed", response.text)
        self.assertEqual(len(requests), 2)
        self.assertNotIn("stream", requests[1])

    def test_channel_mapping_is_per_attempt_and_original_body_stays_public(self):
        primary_requests, backup_requests = [], []

        class PrimaryHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                primary_requests.append(json.loads(_read_request_body(self)))
                self.send_response(502)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html>primary failed</html>")

            def log_message(self, *_):
                pass

        class BackupHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                backup_requests.append(json.loads(_read_request_body(self)))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "choices": [{"message": {"content": "backup"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }).encode())

            def log_message(self, *_):
                pass

        primary = ThreadingHTTPServer(("127.0.0.1", 0), PrimaryHandler)
        backup = ThreadingHTTPServer(("127.0.0.1", 0), BackupHandler)
        primary_thread = threading.Thread(target=primary.serve_forever, daemon=True)
        backup_thread = threading.Thread(target=backup.serve_forever, daemon=True)
        primary_thread.start()
        backup_thread.start()
        body = {
            "model": "public-model",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "weather"}}],
            "tool_choice": "auto",
        }
        original_body = json.loads(json.dumps(body))
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                client.post("/api/channels", headers=headers, json={
                    "name": "primary", "type": "openai", "base_url": f"http://127.0.0.1:{primary.server_port}",
                    "api_key": "primary-fixture-key", "models": "public-model", "priority": 10,
                    "model_mapping": {"public-model": "upstream-primary"},
                })
                client.post("/api/channels", headers=headers, json={
                    "name": "backup", "type": "openai", "base_url": f"http://127.0.0.1:{backup.server_port}",
                    "api_key": "backup-fixture-key", "models": "public-model", "priority": 0,
                    "model_mapping": {"public-model": "upstream-backup"},
                })
                key = client.post("/api/keys", headers=headers, json={
                    "name": "test", "user_id": setup["user"]["id"],
                }).json()["key"]
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json=body)
        finally:
            primary.shutdown()
            backup.shutdown()
            primary_thread.join()
            backup_thread.join()
            primary.server_close()
            backup.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(primary_requests[0]["model"], "upstream-primary")
        self.assertEqual(backup_requests[0]["model"], "upstream-backup")
        self.assertEqual(primary_requests[0]["tools"], original_body["tools"])
        self.assertEqual(backup_requests[0]["tools"], original_body["tools"])
        self.assertEqual(body, original_body)

    def test_strip_tool_policy_removes_only_tool_fields(self):
        requests = []

        class StripHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(_read_request_body(self)))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"choices":[{"message":{"content":"text-only"}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}')

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), StripHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(
                    client, f"http://127.0.0.1:{upstream.server_port}", tool_policy="strip"
                )
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
                    "temperature": 0.2,
                    "tools": [{"type": "function", "function": {"name": "weather"}}],
                    "tool_choice": "auto", "parallel_tool_calls": True,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["temperature"], 0.2)
        for field in ("tools", "tool_choice", "parallel_tool_calls"):
            self.assertNotIn(field, requests[0])

    def test_preserve_tools_unsupported_switches_to_backup_without_stripping(self):
        primary_requests, backup_requests = [], []

        class UnsupportedHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                primary_requests.append(json.loads(_read_request_body(self)))
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"tools is not supported"}}')

            def log_message(self, *_):
                pass

        class WorkingHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                backup_requests.append(json.loads(_read_request_body(self)))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"choices":[{"message":{"content":"backup-ok"}}],"usage":{"prompt_tokens":1,"completion_tokens":1}}')

            def log_message(self, *_):
                pass

        primary = ThreadingHTTPServer(("127.0.0.1", 0), UnsupportedHandler)
        backup = ThreadingHTTPServer(("127.0.0.1", 0), WorkingHandler)
        primary_thread = threading.Thread(target=primary.serve_forever, daemon=True)
        backup_thread = threading.Thread(target=backup.serve_forever, daemon=True)
        primary_thread.start()
        backup_thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                client.post("/api/channels", headers=headers, json={
                    "name": "text-primary", "type": "openai", "base_url": f"http://127.0.0.1:{primary.server_port}",
                    "api_key": "primary-fixture-key", "models": "model-a", "priority": 10,
                })
                client.post("/api/channels", headers=headers, json={
                    "name": "text-backup", "type": "openai", "base_url": f"http://127.0.0.1:{backup.server_port}",
                    "api_key": "backup-fixture-key", "models": "model-a", "priority": 0,
                })
                key = client.post("/api/keys", headers=headers, json={
                    "name": "test", "user_id": setup["user"]["id"],
                }).json()["key"]
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
                    "tools": [{"type": "function", "function": {"name": "weather"}}],
                    "tool_choice": "auto", "parallel_tool_calls": True,
                })
        finally:
            primary.shutdown()
            backup.shutdown()
            primary_thread.join()
            backup_thread.join()
            primary.server_close()
            backup.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(primary_requests), 1)
        self.assertEqual(len(backup_requests), 1)
        self.assertIn("tools", primary_requests[0])
        self.assertIn("tools", backup_requests[0])

    def test_tools_unsupported_without_backup_is_clear_and_not_a_breaker_failure(self):
        requests = []

        class UnsupportedHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(_read_request_body(self)))
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"tools is not supported"}}')

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), UnsupportedHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                headers, key = self._configure_gateway(
                    client, f"http://127.0.0.1:{upstream.server_port}", name="text-only"
                )
                channel_id = client.get("/api/channels", headers=headers).json()[0]["id"]
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
                    "tools": [{"type": "function", "function": {"name": "weather"}}],
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["type"], "tool_not_supported")
        self.assertIn("渠道 text-only", response.json()["error"]["message"])
        self.assertIn("不支持工具调用", response.json()["error"]["message"])
        self.assertEqual(len(requests), 1)
        self.assertNotIn(channel_id, gateway._BREAKER)

    def test_stream_preserve_tools_unsupported_switches_before_content(self):
        primary_requests, backup_requests = [], []

        class UnsupportedStreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                primary_requests.append(json.loads(_read_request_body(self)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"error":{"message":"tools is not supported"}}\n\n')
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *_):
                pass

        class WorkingStreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                backup_requests.append(json.loads(_read_request_body(self)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"backup-stream"},"finish_reason":null}]}\n\n')
                self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *_):
                pass

        primary = ThreadingHTTPServer(("127.0.0.1", 0), UnsupportedStreamHandler)
        backup = ThreadingHTTPServer(("127.0.0.1", 0), WorkingStreamHandler)
        primary_thread = threading.Thread(target=primary.serve_forever, daemon=True)
        backup_thread = threading.Thread(target=backup.serve_forever, daemon=True)
        primary_thread.start()
        backup_thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                client.post("/api/channels", headers=headers, json={
                    "name": "stream-primary", "type": "openai", "base_url": f"http://127.0.0.1:{primary.server_port}",
                    "api_key": "primary-fixture-key", "models": "model-a", "priority": 10,
                })
                client.post("/api/channels", headers=headers, json={
                    "name": "stream-backup", "type": "openai", "base_url": f"http://127.0.0.1:{backup.server_port}",
                    "api_key": "backup-fixture-key", "models": "model-a", "priority": 0,
                })
                key = client.post("/api/keys", headers=headers, json={
                    "name": "test", "user_id": setup["user"]["id"],
                }).json()["key"]
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                    "tools": [{"type": "function", "function": {"name": "weather"}}], "tool_choice": "auto",
                })
        finally:
            primary.shutdown()
            backup.shutdown()
            primary_thread.join()
            backup_thread.join()
            primary.server_close()
            backup.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("backup-stream", response.text)
        self.assertEqual(len(primary_requests), 1)
        self.assertEqual(len(backup_requests), 1)
        self.assertIn("tools", primary_requests[0])
        self.assertIn("tools", backup_requests[0])

    def test_stream_strip_tool_policy_does_not_send_tool_fields(self):
        requests = []

        class StripStreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(_read_request_body(self)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"text"}}]}\n\n')
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), StripStreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(
                    client, f"http://127.0.0.1:{upstream.server_port}", tool_policy="strip"
                )
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                    "tools": [{"type": "function", "function": {"name": "weather"}}],
                    "tool_choice": "auto", "parallel_tool_calls": True,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(requests), 1)
        for field in ("tools", "tool_choice", "parallel_tool_calls"):
            self.assertNotIn(field, requests[0])
        self.assertEqual(_sse_payloads(response.text).count("[DONE]"), 1)

    def test_request_id_is_consistent_on_success_error_and_stream(self):
        with TestClient(app) as client:
            _, key = self._configure_gateway(
                client, "http://127.0.0.1:1", model="model-a", name="mock", channel_type="mock"
            )
            success = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
            })
            stream = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
            })
            error = client.post("/v1/chat/completions", headers={"Authorization": "Bearer fixture-invalid"}, json={
                "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertRegex(success.headers.get("x-request-id", ""), r"^[0-9a-f]{16}$")
        self.assertRegex(stream.headers.get("x-request-id", ""), r"^[0-9a-f]{16}$")
        self.assertRegex(error.headers.get("x-request-id", ""), r"^[0-9a-f]{16}$")
        self.assertEqual(error.headers["x-request-id"], error.json()["request_id"])
        log = database.query_one(
            "SELECT request_id,status FROM request_logs ORDER BY id DESC LIMIT 1"
        )
        self.assertEqual(log["request_id"], stream.headers["x-request-id"])

    def test_admin_query_token_is_rejected_but_bearer_token_works(self):
        with TestClient(app) as client:
            setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
            headers = {"Authorization": "Bearer " + setup["token"]}
            self.assertEqual(client.get("/api/channels", headers=headers).status_code, 200)
            response = client.get("/api/channels?token=" + setup["token"])
        self.assertEqual(response.status_code, 401)

    def test_tools_unsupported_detection_is_narrow_and_requires_tools(self):
        from app.gateway import _tools_unsupported

        body = {"tools": [{"type": "function"}]}
        for text in (
            '{"error":{"param":"tools","message":"invalid request"}}',
            "tools is not supported",
            "tool use is not supported",
            "function calling is not supported",
        ):
            self.assertTrue(_tools_unsupported(text, body), text)
        self.assertFalse(_tools_unsupported("tools is not supported", {}))
        self.assertFalse(_tools_unsupported("invalid tools", body))
        self.assertFalse(_tools_unsupported("invalid request", body))

    def test_nonstream_tool_calls_are_forwarded_without_text_content(self):
        tool_calls = [{
            "id": "call_1", "type": "function", "function": {"name": "weather", "arguments": "{}"},
        }]

        class ToolHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "choices": [{"message": {"role": "assistant", "tool_calls": tool_calls}}],
                }).encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), ToolHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}")
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "use weather"}],
                    "tools": [{"type": "function", "function": {"name": "weather"}}],
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["tool_calls"], tool_calls)

    def test_route_panel_copy_does_not_assume_first_row_is_primary(self):
        script = Path("static/app.js").read_text(encoding="utf-8")
        self.assertIn("同级主选（按权重随机）", script)
        self.assertIn("冷却中（当前跳过；全部冷却时不发送请求）", script)
        self.assertNotIn('index === 0 ? "主渠道"', script)

    def test_routes_explain_mapping_priority_weight_cooling_and_tool_policy(self):
        with TestClient(app) as client:
            setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
            headers = {"Authorization": "Bearer " + setup["token"]}
            created = []
            for name, priority, weight, mapping, policy in (
                ("primary", 20, 3, {"public-model": "up-primary"}, "preserve"),
                ("same-level", 20, 1, {"public-model": "up-same"}, "strip"),
                ("backup", 10, 5, {}, "preserve"),
            ):
                created.append(client.post("/api/channels", headers=headers, json={
                    "name": name, "type": "mock", "models": "public-model", "priority": priority,
                    "weight": weight, "model_mapping": mapping, "tool_policy": policy,
                }).json()["id"])
            gateway._BREAKER[created[0]] = {"streak": 0, "until": time.time() + 300}
            response = client.get("/api/routes?model=public-model", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual([item["channel_name"] for item in payload["channels"]], ["primary", "same-level", "backup"])
        self.assertEqual(payload["channels"][0]["upstream_model"], "up-primary")
        self.assertEqual(payload["channels"][1]["tool_policy"], "strip")
        self.assertTrue(payload["channels"][0]["cooling"])
        self.assertEqual(payload["channels"][0]["priority"], 20)
        self.assertEqual(payload["channels"][0]["weight"], 3)
        self.assertTrue(all("api_key" not in item for item in payload["channels"]))
        self.assertIn("优先级更大的渠道先使用", " ".join(payload["rules"]))
        self.assertIn("最多尝试 3 个渠道", " ".join(payload["rules"]))

    def test_channel_test_uses_selected_public_model_and_mapping(self):
        requests = []

        class InferenceHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":[{"id":"upstream-b"}]}')

            def do_POST(self):
                requests.append(json.loads(_read_request_body(self)))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"choices":[{"message":{"content":"OK"}}]}')

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), InferenceHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                headers, _ = self._configure_gateway(
                    client, f"http://127.0.0.1:{upstream.server_port}", model="public-a,public-b",
                    model_mapping={"public-b": "upstream-b"}, name="mapped-channel"
                )
                channel_id = client.get("/api/channels", headers=headers).json()[0]["id"]
                response = client.post(f"/api/channels/{channel_id}/test", headers=headers, json={"model": "public-b"})
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        result = response.json()
        self.assertTrue(result["ok"], response.text)
        self.assertEqual(result["test_model"], "public-b")
        self.assertEqual(result["upstream_model"], "upstream-b")
        self.assertEqual(result["model_list_status_code"], 200)
        self.assertEqual(requests[0]["model"], "upstream-b")
        self.assertIn("可能产生极少量额度", result["message"])

    def test_channel_test_uses_gemini_mapping_and_protocol(self):
        received = []

        class GeminiTestHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"models":[{"name":"models/gemini-upstream"}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                received.append((self.path, dict(self.headers), json.loads(_read_request_body(self))))
                body = b'{"candidates":[{"content":{"role":"model","parts":[{"text":"OK"}]},"finishReason":"STOP"}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), GeminiTestHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                channel = client.post("/api/channels", headers=headers, json={
                    "name": "gemini-test", "type": "gemini", "base_url": f"http://127.0.0.1:{upstream.server_port}/v1beta",
                    "api_key": "fixture-gemini-key", "models": "public-model",
                    "model_mapping": {"public-model": "gemini-upstream"},
                }).json()
                result = client.post(f"/api/channels/{channel['id']}/test", headers=headers, json={"model": "public-model"})
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["ok"], result.text)
        self.assertEqual(result.json()["protocol"], "gemini")
        self.assertEqual(result.json()["upstream_model"], "gemini-upstream")
        self.assertEqual(received[0][0], "/v1beta/models/gemini-upstream:generateContent")
        self.assertEqual(_header(received[0][1], "x-goog-api-key"), "fixture-gemini-key")

    def test_stream_error_before_output_fails_over_after_safe_fallback_failure(self):
        primary_requests, backup_requests = [], []

        class PrimaryHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(_read_request_body(self))
                primary_requests.append(payload)
                self.send_response(200)
                if payload.get("stream"):
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b'data: {"error":{"message":"temporary upstream error"}}\n\n')
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"not-json")

            def log_message(self, *_):
                pass

        class BackupHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                backup_requests.append(json.loads(_read_request_body(self)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"backup-result"}}]}\n\n')
                self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *_):
                pass

        primary = ThreadingHTTPServer(("127.0.0.1", 0), PrimaryHandler)
        backup = ThreadingHTTPServer(("127.0.0.1", 0), BackupHandler)
        primary_thread = threading.Thread(target=primary.serve_forever, daemon=True)
        backup_thread = threading.Thread(target=backup.serve_forever, daemon=True)
        primary_thread.start()
        backup_thread.start()
        try:
            with TestClient(app) as client:
                key = self._configure_openai_channels(client, [
                    ("stream-primary", f"http://127.0.0.1:{primary.server_port}", 10),
                    ("stream-backup", f"http://127.0.0.1:{backup.server_port}", 0),
                ])
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                })
        finally:
            primary.shutdown()
            backup.shutdown()
            primary_thread.join()
            backup_thread.join()
            primary.server_close()
            backup.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("backup-result", response.text)
        self.assertEqual(len(primary_requests), 2)
        self.assertTrue(primary_requests[0]["stream"])
        self.assertNotIn("stream", primary_requests[1])
        self.assertEqual(len(backup_requests), 1)
        logs = database.query(
            "SELECT channel_name,status,status_code,will_retry FROM request_logs WHERE request_id=? ORDER BY id",
            (response.headers["x-request-id"],),
        )
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0]["status"], "error")
        self.assertEqual(logs[1]["status"], "success")
        self.assertEqual(logs[0]["will_retry"], 1)

    def test_stream_error_before_output_with_tools_switches_without_stripping(self):
        primary_requests, backup_requests = [], []

        class PrimaryHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                primary_requests.append(json.loads(_read_request_body(self)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"error":{"message":"temporary upstream error"}}\n\n')
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *_):
                pass

        class BackupHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                backup_requests.append(json.loads(_read_request_body(self)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"backup-with-tools"}}]}\n\n')
                self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *_):
                pass

        primary = ThreadingHTTPServer(("127.0.0.1", 0), PrimaryHandler)
        backup = ThreadingHTTPServer(("127.0.0.1", 0), BackupHandler)
        primary_thread = threading.Thread(target=primary.serve_forever, daemon=True)
        backup_thread = threading.Thread(target=backup.serve_forever, daemon=True)
        primary_thread.start()
        backup_thread.start()
        tools = [{"type": "function", "function": {"name": "weather"}}]
        try:
            with TestClient(app) as client:
                key = self._configure_openai_channels(client, [
                    ("tools-primary", f"http://127.0.0.1:{primary.server_port}", 10),
                    ("tools-backup", f"http://127.0.0.1:{backup.server_port}", 0),
                ])
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "use weather"}],
                    "stream": True, "tools": tools, "tool_choice": "auto",
                })
        finally:
            primary.shutdown()
            backup.shutdown()
            primary_thread.join()
            backup_thread.join()
            primary.server_close()
            backup.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("backup-with-tools", response.text)
        self.assertEqual(len(primary_requests), 1)
        self.assertEqual(len(backup_requests), 1)
        self.assertEqual(primary_requests[0]["tools"], tools)
        self.assertEqual(backup_requests[0]["tools"], tools)
        self.assertEqual(backup_requests[0]["tool_choice"], "auto")

    def test_stream_fallback_converts_tool_calls_to_sse(self):
        requests = []
        tool_calls = [{
            "id": "call_1", "type": "function",
            "function": {"name": "weather", "arguments": "{}"},
        }]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(_read_request_body(self))
                requests.append(payload)
                self.send_response(200)
                if payload.get("stream"):
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b'data: {"error":{"message":"temporary upstream error"}}\n\n')
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "id": "fallback-tool", "model": "model-a",
                        "choices": [{"message": {"role": "assistant", "tool_calls": tool_calls},
                                     "finish_reason": "tool_calls"}],
                        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                    }).encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}")
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        payloads = _sse_payloads(response.text)
        decoded = [json.loads(payload) for payload in payloads if payload != "[DONE]"]
        tool_events = [event for event in decoded if (event.get("choices") or [{}])[0].get("delta", {}).get("tool_calls")]
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(tool_events[0]["choices"][0]["delta"]["tool_calls"], tool_calls)
        self.assertEqual(decoded[-1]["choices"][0]["finish_reason"], "tool_calls")
        self.assertNotIn("未返回文本", response.text)
        self.assertEqual(payloads.count("[DONE]"), 1)
        self.assertEqual(len(requests), 2)

    def test_stream_fallback_converts_function_call_to_sse(self):
        requests = []
        function_call = {"name": "weather", "arguments": "{}"}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(_read_request_body(self))
                requests.append(payload)
                self.send_response(200)
                if payload.get("stream"):
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b'data: {"error":{"message":"temporary upstream error"}}\n\n')
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "choices": [{"message": {"role": "assistant", "function_call": function_call}}],
                    }).encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}")
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        payloads = _sse_payloads(response.text)
        decoded = [json.loads(payload) for payload in payloads if payload != "[DONE]"]
        function_events = [event for event in decoded if (event.get("choices") or [{}])[0].get("delta", {}).get("function_call")]
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(function_events[0]["choices"][0]["delta"]["function_call"], function_call)
        self.assertEqual(decoded[-1]["choices"][0]["finish_reason"], "function_call")
        self.assertEqual(payloads.count("[DONE]"), 1)
        self.assertEqual(len(requests), 2)

    def test_stream_fallback_empty_choices_is_protocol_error_and_fails_over(self):
        requests, backup_requests = [], []

        class PrimaryHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(_read_request_body(self))
                requests.append(payload)
                self.send_response(200)
                if payload.get("stream"):
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b'data: {"error":{"message":"temporary upstream error"}}\n\n')
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"choices":[]}')

            def log_message(self, *_):
                pass

        class BackupHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(_read_request_body(self))
                backup_requests.append(payload)
                self.send_response(200)
                if payload.get("stream"):
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":"backup-after-empty"}}]}\n\n')
                    self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"choices":[{"message":{"content":"backup-after-empty"}}]}')

            def log_message(self, *_):
                pass

        primary = ThreadingHTTPServer(("127.0.0.1", 0), PrimaryHandler)
        backup = ThreadingHTTPServer(("127.0.0.1", 0), BackupHandler)
        primary_thread = threading.Thread(target=primary.serve_forever, daemon=True)
        backup_thread = threading.Thread(target=backup.serve_forever, daemon=True)
        primary_thread.start()
        backup_thread.start()
        try:
            with TestClient(app) as client:
                key = self._configure_openai_channels(client, [
                    ("empty-fallback-primary", f"http://127.0.0.1:{primary.server_port}", 10),
                    ("empty-fallback-backup", f"http://127.0.0.1:{backup.server_port}", 0),
                ])
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                })
        finally:
            primary.shutdown()
            backup.shutdown()
            primary_thread.join()
            backup_thread.join()
            primary.server_close()
            backup.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("backup-after-empty", response.text)
        self.assertEqual(len(requests), 2)
        self.assertNotIn("stream", requests[1])
        self.assertEqual(len(backup_requests), 1)
        logs = database.query(
            "SELECT status,status_code,will_retry,error_msg FROM request_logs WHERE request_id=? ORDER BY id",
            (response.headers["x-request-id"],),
        )
        self.assertEqual(logs[0]["status"], "error")
        self.assertEqual(logs[0]["status_code"], 502)
        self.assertEqual(logs[0]["will_retry"], 1)
        self.assertIn("协议错误", logs[0]["error_msg"])

    def test_stream_eof_before_output_fails_over_to_backup(self):
        requests = []

        class EarlyEOFHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                requests.append("primary")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')
                self.wfile.flush()
                self.close_connection = True

            def log_message(self, *_):
                pass

        class BackupHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(_read_request_body(self))
                requests.append("backup")
                self.send_response(200)
                if payload.get("stream"):
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":"backup-after-eof"}}]}\n\n')
                    self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"choices":[{"message":{"content":"backup-after-eof"}}]}')

            def log_message(self, *_):
                pass

        primary = ThreadingHTTPServer(("127.0.0.1", 0), EarlyEOFHandler)
        backup = ThreadingHTTPServer(("127.0.0.1", 0), BackupHandler)
        primary_thread = threading.Thread(target=primary.serve_forever, daemon=True)
        backup_thread = threading.Thread(target=backup.serve_forever, daemon=True)
        primary_thread.start()
        backup_thread.start()
        try:
            with TestClient(app) as client:
                key = self._configure_openai_channels(client, [
                    ("eof-primary", f"http://127.0.0.1:{primary.server_port}", 10),
                    ("eof-backup", f"http://127.0.0.1:{backup.server_port}", 0),
                ])
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                })
        finally:
            primary.shutdown()
            backup.shutdown()
            primary_thread.join()
            backup_thread.join()
            primary.server_close()
            backup.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("backup-after-eof", response.text)
        self.assertEqual(requests, ["primary", "primary", "backup"])

    def test_stream_tool_call_output_then_eof_does_not_fail_over(self):
        requests = []

        class PrimaryHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                requests.append("primary")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1"}]}}]}\n\n')
                self.wfile.flush()
                self.close_connection = True

            def log_message(self, *_):
                pass

        class BackupHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                requests.append("backup")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"choices":[{"message":{"content":"must-not-run"}}]}')

            def log_message(self, *_):
                pass

        primary = ThreadingHTTPServer(("127.0.0.1", 0), PrimaryHandler)
        backup = ThreadingHTTPServer(("127.0.0.1", 0), BackupHandler)
        primary_thread = threading.Thread(target=primary.serve_forever, daemon=True)
        backup_thread = threading.Thread(target=backup.serve_forever, daemon=True)
        primary_thread.start()
        backup_thread.start()
        try:
            with TestClient(app) as client:
                key = self._configure_openai_channels(client, [
                    ("output-primary", f"http://127.0.0.1:{primary.server_port}", 10),
                    ("output-backup", f"http://127.0.0.1:{backup.server_port}", 0),
                ])
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "call"}], "stream": True,
                })
        finally:
            primary.shutdown()
            backup.shutdown()
            primary_thread.join()
            backup_thread.join()
            primary.server_close()
            backup.server_close()
        payloads = _sse_payloads(response.text)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("tool_calls", response.text)
        self.assertIn('"error"', response.text)
        self.assertEqual(requests, ["primary"])
        self.assertEqual(payloads.count("[DONE]"), 1)

    def test_password_change_requires_twelve_characters_and_revokes_other_sessions(self):
        with TestClient(app) as client:
            setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
            current_headers = {"Authorization": "Bearer " + setup["token"]}
            second = client.post("/api/login", json={"username": "owner", "password": "a-long-password"}).json()
            second_headers = {"Authorization": "Bearer " + second["token"]}
            short = client.post("/api/me/password", headers=current_headers, json={
                "old_password": "a-long-password", "new_password": "12345678901",
            })
            self.assertEqual(short.status_code, 400, short.text)
            changed = client.post("/api/me/password", headers=current_headers, json={
                "old_password": "a-long-password", "new_password": "new-password-12",
            })
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual(client.get("/api/me", headers=current_headers).status_code, 200)
            self.assertEqual(client.get("/api/me", headers=second_headers).status_code, 401)

    def test_logs_support_exact_request_id_filter(self):
        with TestClient(app) as client:
            setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
            headers = {"Authorization": "Bearer " + setup["token"]}
            for request_id in ("rid-one", "rid-two"):
                database.execute(
                    "INSERT INTO request_logs(request_id,username,model,endpoint,created_at) VALUES(?,?,?,?,?)",
                    (request_id, "owner", "model-a", "/v1/chat/completions", "2026-01-01 00:00:00"),
                )
            response = client.get("/api/logs?request_id=rid-one", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(response.json()["items"][0]["request_id"], "rid-one")

    def test_url_preview_normalizes_all_openai_forms_without_upstream_request(self):
        forms = (
            "https://seekai.cc",
            "https://seekai.cc/",
            "https://seekai.cc/v1",
            "https://seekai.cc/v1/",
            "https://seekai.cc/v1/chat/completions",
        )
        with TestClient(app) as client:
            setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
            headers = {"Authorization": "Bearer " + setup["token"]}
            results = [client.post("/api/channels/preview-url", headers=headers, json={"base_url": value})
                       for value in forms]
        self.assertTrue(all(response.status_code == 200 for response in results), [r.text for r in results])
        payloads = [response.json() for response in results]
        self.assertEqual({payload["base_url"] for payload in payloads}, {"https://seekai.cc/v1"})
        self.assertEqual({payload["models_url"] for payload in payloads}, {"https://seekai.cc/v1/models"})
        self.assertEqual({payload["chat_url"] for payload in payloads}, {"https://seekai.cc/v1/chat/completions"})
        self.assertEqual({payload["name"] for payload in payloads}, {"seekai"})

    def test_gateway_info_does_not_return_complete_api_key(self):
        secret = "fixture-gateway-key-never-return"
        with TestClient(app) as client:
            setup = client.post("/api/setup", json={"username": "admin", "password": "a-long-password"}).json()
            headers = {"Authorization": "Bearer " + setup["token"]}
            key = client.post("/api/keys", headers=headers, json={
                "name": "fixture", "user_id": setup["user"]["id"],
            }).json()["key"]
            response = client.get("/api/gateway", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("demo_key", response.json())
        self.assertNotIn(key, response.text)
        self.assertNotIn(secret, response.text)

    def test_console_uses_native_accessible_controls_and_reduced_motion(self):
        index = Path("static/index.html").read_text(encoding="utf-8")
        script = Path("static/app.js").read_text(encoding="utf-8")
        styles = Path("static/style.css").read_text(encoding="utf-8")
        self.assertNotIn("window.prompt", script)
        self.assertIn('<button class="logout-btn"', index)
        self.assertIn('aria-live="polite"', index)
        self.assertIn("<dialog", script)
        self.assertIn("prefers-reduced-motion", styles)

    def test_console_password_copy_requires_twelve_characters(self):
        script = Path("static/app.js").read_text(encoding="utf-8")
        self.assertIn("新密码（至少 12 位）", script)
        self.assertNotIn("新密码（至少 6 位）", script)

    def test_channel_test_returns_request_id_without_writing_usage_log(self):
        with TestClient(app) as client:
            headers, _ = self._configure_gateway(client, "mock://local", channel_type="mock")
            channel_id = client.get("/api/channels", headers=headers).json()[0]["id"]
            response = client.post(f"/api/channels/{channel_id}/test", headers=headers, json={"model": "model-a"})
            log_count = database.query_one("SELECT COUNT(*) c FROM request_logs")["c"]
        self.assertEqual(response.status_code, 200, response.text)
        self.assertRegex(response.json().get("request_id", ""), r"^[0-9a-f]{16}$")
        self.assertEqual(log_count, 0)

    def test_anthropic_base_url_normalizes_to_root_without_duplicate_path(self):
        forms = (
            "https://agentrouter.org",
            "https://agentrouter.org/",
            "https://agentrouter.org/v1",
            "https://agentrouter.org/v1/",
            "https://agentrouter.org/v1/messages",
            "https://agentrouter.org/v1/messages/",
        )
        self.assertEqual(
            {gateway.normalize_anthropic_base_url(value) for value in forms},
            {"https://agentrouter.org"},
        )

    def test_anthropic_channel_type_is_validated_and_keeps_openai_channel(self):
        with TestClient(app) as client:
            setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
            headers = {"Authorization": "Bearer " + setup["token"]}
            openai = client.post("/api/channels", headers=headers, json={
                "name": "openai-fixture", "type": "openai", "base_url": "https://example.test/v1",
                "api_key": "fixture-openai-key", "models": "shared-model",
            })
            anthropic = client.post("/api/channels", headers=headers, json={
                "name": "anthropic-fixture", "type": "anthropic", "base_url": "https://agentrouter.org/v1/messages",
                "api_key": "fixture-anthropic-key", "models": "shared-model",
            })
            unknown = client.post("/api/channels", headers=headers, json={
                "name": "unknown", "type": "not-a-protocol", "base_url": "https://example.test",
                "api_key": "fixture-key", "models": "shared-model",
            })
            channels = client.get("/api/channels", headers=headers)
        self.assertEqual(openai.status_code, 200, openai.text)
        self.assertEqual(anthropic.status_code, 200, anthropic.text)
        self.assertEqual(unknown.status_code, 400, unknown.text)
        by_name = {channel["name"]: channel for channel in channels.json()}
        self.assertEqual(by_name["openai-fixture"]["type"], "openai")
        self.assertEqual(by_name["anthropic-fixture"]["type"], "anthropic")
        self.assertEqual(by_name["anthropic-fixture"]["base_url"], "https://agentrouter.org")
        self.assertNotIn("api_key", channels.json()[0])

    def test_anthropic_preview_and_model_discovery_are_manual_without_models_request(self):
        requests = []

        class NoModelsHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(500)
                self.end_headers()

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), NoModelsHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                preview = client.post("/api/channels/preview-url", headers=headers, json={
                    "type": "anthropic", "base_url": f"http://127.0.0.1:{upstream.server_port}/v1/messages",
                })
                discover = client.post("/api/channels/discover-models", headers=headers, json={
                    "type": "anthropic", "base_url": f"http://127.0.0.1:{upstream.server_port}",
                    "api_key": "fixture-anthropic-key",
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["messages_url"], f"http://127.0.0.1:{upstream.server_port}/v1/messages")
        self.assertIn("手动填写模型", preview.json()["message"])
        self.assertEqual(discover.status_code, 400, discover.text)
        self.assertIn("Anthropic", discover.text)
        self.assertEqual(requests, [])

    def test_anthropic_channel_test_uses_messages_and_mapping_without_models(self):
        received = []

        class AnthropicTestHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(("GET", self.path))
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                received.append(("POST", self.path, dict(self.headers), json.loads(_read_request_body(self))))
                payload = {"id": "msg_test", "model": "glm-5.3", "content": [{"type": "text", "text": "OK"}],
                           "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), AnthropicTestHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                created = client.post("/api/channels", headers=headers, json={
                    "name": "anthropic-test", "type": "anthropic", "base_url": f"http://127.0.0.1:{upstream.server_port}/v1",
                    "api_key": "fixture-anthropic-key", "models": "public-model",
                    "model_mapping": {"public-model": "glm-5.3"},
                })
                channel_id = created.json()["id"]
                result = client.post(f"/api/channels/{channel_id}/test", headers=headers, json={"model": "public-model"})
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["ok"], result.text)
        self.assertEqual(result.json()["upstream_model"], "glm-5.3")
        self.assertEqual([item[0:2] for item in received], [("POST", "/v1/messages")])
        _, path, request_headers, payload = received[0]
        self.assertEqual(_header(request_headers, "x-api-key"), "fixture-anthropic-key")
        self.assertEqual(_header(request_headers, "Authorization"), "Bearer fixture-anthropic-key")
        self.assertEqual(_header(request_headers, "anthropic-version"), "2023-06-01")
        self.assertEqual(payload["model"], "glm-5.3")
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertEqual(payload["messages"][0]["content"], "Reply only OK")

    def test_anthropic_nonstream_converts_system_tools_and_tool_result(self):
        received = []

        class AnthropicHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, dict(self.headers), json.loads(_read_request_body(self))))
                payload = {
                    "id": "msg_fixture",
                    "type": "message",
                    "role": "assistant",
                    "model": "glm-5.3",
                    "content": [{"type": "text", "text": "OK"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 11, "output_tokens": 3},
                }
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), AnthropicHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(
                    client, f"http://127.0.0.1:{upstream.server_port}/v1/messages",
                    model="shared-model", name="anthropic-fixture", channel_type="anthropic",
                    model_mapping={"shared-model": "glm-5.3"},
                )
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "shared-model",
                    "messages": [
                        {"role": "system", "content": "system rule"},
                        {"role": "developer", "content": "developer rule"},
                        {"role": "user", "content": "use the tool"},
                        {"role": "assistant", "tool_calls": [{
                            "id": "call_1", "type": "function",
                            "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                        }]},
                        {"role": "tool", "tool_call_id": "call_1", "content": "tool result"},
                    ],
                    "tools": [{"type": "function", "function": {
                        "name": "lookup", "description": "look up", "parameters": {
                            "type": "object", "properties": {"q": {"type": "string"}},
                        },
                    }}],
                    "tool_choice": "auto",
                    "max_tokens": 32,
                    "stream": False,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "OK")
        self.assertEqual(response.json()["choices"][0]["finish_reason"], "stop")
        self.assertEqual(response.json()["usage"], {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14})
        path, headers, payload = received[0]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(_header(headers, "x-api-key"), "test-key")
        self.assertEqual(_header(headers, "Authorization"), "Bearer test-key")
        self.assertEqual(_header(headers, "anthropic-version"), "2023-06-01")
        self.assertEqual(payload["model"], "glm-5.3")
        self.assertEqual(payload["system"][0]["text"], "system rule")
        self.assertEqual(payload["system"][1]["text"], "developer rule")
        self.assertEqual(payload["tools"][0]["input_schema"]["type"], "object")
        assistant_blocks = payload["messages"][1]["content"]
        self.assertEqual(assistant_blocks[0]["type"], "tool_use")
        self.assertEqual(assistant_blocks[0]["id"], "call_1")
        self.assertEqual(payload["messages"][2]["content"][0]["tool_use_id"], "call_1")

    def test_anthropic_tool_use_response_converts_to_openai_tool_calls(self):
        class ToolUseHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                payload = {
                    "id": "msg_tool",
                    "model": "glm-5.3",
                    "content": [{"type": "tool_use", "id": "tool_1", "name": "lookup", "input": {"q": "x"}}],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 5, "output_tokens": 7},
                }
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), ToolUseHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(
                    client, f"http://127.0.0.1:{upstream.server_port}",
                    model="model-a", name="anthropic-tools", channel_type="anthropic",
                )
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "tool"}],
                    "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {}}}],
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        message = response.json()["choices"][0]["message"]
        self.assertEqual(response.json()["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(message["tool_calls"][0]["id"], "tool_1")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "lookup")
        self.assertEqual(json.loads(message["tool_calls"][0]["function"]["arguments"]), {"q": "x"})

    def test_anthropic_stream_text_emits_openai_chunks_and_one_done(self):
        class AnthropicStreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                events = [
                    ("message_start", {"type": "message_start", "message": {"id": "msg_stream", "model": "glm-5.3", "usage": {"input_tokens": 4}}}),
                    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
                    ("message_stop", {"type": "message_stop"}),
                ]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event_name, payload in events:
                    self.wfile.write((f"event: {event_name}\n" + "data: " + json.dumps(payload) + "\n\n").encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), AnthropicStreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(
                    client, f"http://127.0.0.1:{upstream.server_port}",
                    model="model-a", name="anthropic-stream", channel_type="anthropic",
                )
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"content": "OK"', response.text)
        self.assertIn('"finish_reason": "stop"', response.text)
        self.assertEqual(_sse_payloads(response.text).count("[DONE]"), 1)
        self.assertNotIn('"type": "content_block_delta"', response.text)

    def test_anthropic_stream_tool_arguments_are_openai_tool_call_deltas(self):
        class AnthropicToolStreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                events = [
                    ("message_start", {"type": "message_start", "message": {"id": "msg_tool_stream", "model": "glm-5.3"}}),
                    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tool_1", "name": "lookup", "input": {}}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"q":"x"}'}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}),
                    ("message_stop", {"type": "message_stop"}),
                ]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event_name, payload in events:
                    self.wfile.write((f"event: {event_name}\n" + "data: " + json.dumps(payload) + "\n\n").encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), AnthropicToolStreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(
                    client, f"http://127.0.0.1:{upstream.server_port}",
                    model="model-a", name="anthropic-tool-stream", channel_type="anthropic",
                )
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "tool"}],
                    "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {}}}],
                    "stream": True,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        payloads = [json.loads(payload) for payload in _sse_payloads(response.text) if payload != "[DONE]"]
        tool_chunks = [payload for payload in payloads if payload.get("choices", [{}])[0].get("delta", {}).get("tool_calls")]
        self.assertTrue(tool_chunks)
        self.assertEqual(tool_chunks[0]["choices"][0]["delta"]["tool_calls"][0]["id"], "tool_1")
        argument_text = "".join(
            payload["choices"][0]["delta"]["tool_calls"][0]["function"].get("arguments", "")
            for payload in tool_chunks
        )
        self.assertEqual(argument_text, '{"q":"x"}')
        self.assertIn('"finish_reason": "tool_calls"', response.text)
        self.assertEqual(_sse_payloads(response.text).count("[DONE]"), 1)

    def test_anthropic_failure_falls_back_to_openai_for_same_public_model(self):
        class AnthropicFailureHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                self.send_response(502)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<!DOCTYPE html><html>upstream down</html>")

            def log_message(self, *_):
                pass

        class OpenAIWorkingHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                payload = {"id": "chat_backup", "model": "shared-model", "choices": [{
                    "index": 0, "message": {"role": "assistant", "content": "backup"}, "finish_reason": "stop",
                }]}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        first = ThreadingHTTPServer(("127.0.0.1", 0), AnthropicFailureHandler)
        backup = ThreadingHTTPServer(("127.0.0.1", 0), OpenAIWorkingHandler)
        first_thread = threading.Thread(target=first.serve_forever, daemon=True)
        backup_thread = threading.Thread(target=backup.serve_forever, daemon=True)
        first_thread.start()
        backup_thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                client.post("/api/channels", headers=headers, json={
                    "name": "anthropic-first", "type": "anthropic", "base_url": f"http://127.0.0.1:{first.server_port}",
                    "api_key": "fixture-anthropic-key", "models": "shared-model", "priority": 20,
                })
                client.post("/api/channels", headers=headers, json={
                    "name": "openai-backup", "type": "openai", "base_url": f"http://127.0.0.1:{backup.server_port}",
                    "api_key": "fixture-openai-key", "models": "shared-model", "priority": 10,
                })
                key = client.post("/api/keys", headers=headers, json={"name": "test", "user_id": setup["user"]["id"]}).json()["key"]
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "shared-model", "messages": [{"role": "user", "content": "hi"}],
                })
                logs = client.get("/api/logs?request_id=" + response.headers["X-Request-ID"], headers=headers).json()["items"]
        finally:
            first.shutdown()
            backup.shutdown()
            first_thread.join()
            backup_thread.join()
            first.server_close()
            backup.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "backup")
        self.assertEqual([log["channel_name"] for log in reversed(logs)], ["anthropic-first", "openai-backup"])
        first_log = next(log for log in logs if log["channel_name"] == "anthropic-first")
        self.assertIn("anthropic", first_log["error_msg"].lower())

    def test_openai_failure_falls_back_to_anthropic_for_same_public_model(self):
        class OpenAIFailureHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"fixture down"}}')

            def log_message(self, *_):
                pass

        class AnthropicWorkingHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                payload = {"id": "msg_backup", "model": "glm-5.3", "content": [{"type": "text", "text": "anthropic backup"}],
                           "stop_reason": "end_turn", "usage": {"input_tokens": 2, "output_tokens": 3}}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        first = ThreadingHTTPServer(("127.0.0.1", 0), OpenAIFailureHandler)
        backup = ThreadingHTTPServer(("127.0.0.1", 0), AnthropicWorkingHandler)
        first_thread = threading.Thread(target=first.serve_forever, daemon=True)
        backup_thread = threading.Thread(target=backup.serve_forever, daemon=True)
        first_thread.start()
        backup_thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                client.post("/api/channels", headers=headers, json={
                    "name": "openai-first", "type": "openai", "base_url": f"http://127.0.0.1:{first.server_port}",
                    "api_key": "fixture-openai-key", "models": "shared-model", "priority": 20,
                })
                client.post("/api/channels", headers=headers, json={
                    "name": "anthropic-backup", "type": "anthropic", "base_url": f"http://127.0.0.1:{backup.server_port}/v1/messages",
                    "api_key": "fixture-anthropic-key", "models": "shared-model", "priority": 10,
                })
                key = client.post("/api/keys", headers=headers, json={"name": "test", "user_id": setup["user"]["id"]}).json()["key"]
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "shared-model", "messages": [{"role": "user", "content": "hi"}],
                })
        finally:
            first.shutdown()
            backup.shutdown()
            first_thread.join()
            backup_thread.join()
            first.server_close()
            backup.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "anthropic backup")

    def test_anthropic_error_is_short_and_redacted(self):
        class ErrorHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                self.send_response(403)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<!DOCTYPE html><html><body>Cloudflare fixture page</body></html>")

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), ErrorHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(
                    client, f"http://127.0.0.1:{upstream.server_port}",
                    model="model-a", name="anthropic-error", channel_type="anthropic",
                )
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 502, response.text)
        message = response.json()["error"]["message"]
        self.assertIn("anthropic", message.lower())
        self.assertIn("403", message)
        self.assertIn("HTML", message)
        self.assertNotIn("test-key", message)
        self.assertNotIn("<!DOCTYPE", message)

    def test_console_exposes_anthropic_type_and_manual_model_guidance(self):
        script = Path("static/app.js").read_text(encoding="utf-8")
        self.assertIn('value="anthropic"', script)
        self.assertIn("/v1/messages", script)
        self.assertIn("没有统一的模型列表接口", script)

    def test_new_channel_defaults_to_auto_and_accepts_four_protocol_families(self):
        with TestClient(app) as client:
            setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
            headers = {"Authorization": "Bearer " + setup["token"]}
            default_channel = client.post("/api/channels", headers=headers, json={
                "name": "auto-default", "base_url": "https://fixture.example", "api_key": "fixture-key", "models": "auto-model",
            })
            created = [client.post("/api/channels", headers=headers, json={
                "name": protocol, "type": protocol, "base_url": "https://fixture.example",
                "api_key": "fixture-key", "models": "shared-model",
            }) for protocol in ("openai_chat", "anthropic", "openai_responses", "gemini", "mock")]
            unknown = client.post("/api/channels", headers=headers, json={
                "name": "bad", "type": "unknown", "base_url": "https://fixture.example", "models": "x",
            })
            channels = client.get("/api/channels", headers=headers).json()
        self.assertEqual(default_channel.status_code, 200, default_channel.text)
        self.assertTrue(all(item.status_code == 200 for item in created), [item.text for item in created])
        self.assertEqual(unknown.status_code, 400, unknown.text)
        by_name = {item["name"]: item for item in channels}
        self.assertEqual(by_name["auto-default"]["type"], "auto")
        self.assertEqual({by_name[name]["type"] for name in ("openai_chat", "anthropic", "openai_responses", "gemini", "mock")},
                         {"openai_chat", "anthropic", "openai_responses", "gemini", "mock"})

    def test_protocol_base_urls_are_normalized_independently(self):
        self.assertEqual(
            {gateway.normalize_openai_responses_base_url(value) for value in (
                "https://fixture.example", "https://fixture.example/", "https://fixture.example/v1",
                "https://fixture.example/v1/responses", "https://fixture.example/responses",
            )},
            {"https://fixture.example"},
        )
        self.assertEqual(
            {gateway.normalize_gemini_base_url(value) for value in (
                "https://fixture.example", "https://fixture.example/", "https://fixture.example/v1beta",
                "https://fixture.example/v1beta/models",
            )},
            {"https://fixture.example"},
        )

    def test_detect_openai_models_returns_protocol_and_safe_model_records(self):
        received = []

        class ModelsHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append((self.path, dict(self.headers)))
                body = json.dumps({"data": [{"id": "chat-a"}, {"id": "chat-b"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), ModelsHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                response = client.post("/api/channels/detect", headers=headers, json={
                    "base_url": f"http://127.0.0.1:{upstream.server_port}/v1", "api_key": "fixture-openai-key", "protocol": "auto",
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["protocol"], "openai_chat")
        self.assertEqual([item["id"] for item in payload["models"]], ["chat-a", "chat-b"])
        self.assertEqual(received[0][0], "/v1/models")
        self.assertNotIn("fixture-openai-key", response.text)
        self.assertNotIn("?api_key", payload["request_url"])

    def test_auto_detection_continues_after_openai_auth_mismatch(self):
        received = []

        class AuthSchemeHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(dict(self.headers))
                if self.headers.get("x-api-key") == "fixture-key":
                    body = b'{"data":[{"id":"glm-5.3"}]}'
                    self.send_response(200)
                else:
                    body = b'{"error":{"message":"wrong auth scheme"}}'
                    self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), AuthSchemeHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                response = client.post("/api/channels/detect", headers=headers, json={
                    "base_url": f"http://127.0.0.1:{upstream.server_port}",
                    "api_key": "fixture-key", "protocol": "auto",
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["protocol"], "anthropic")
        self.assertEqual(len(received), 2)
        self.assertEqual(_header(received[0], "authorization"), "Bearer fixture-key")
        self.assertEqual(_header(received[1], "x-api-key"), "fixture-key")
        self.assertEqual(_header(received[1], "Authorization"), "Bearer fixture-key")

    def test_auto_detection_continues_after_first_transport_error(self):
        received = []

        class DisconnectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(dict(self.headers))
                if len(received) == 1:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                body = b'{"data":[{"id":"glm-5.3"}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), DisconnectHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                response = client.post("/api/channels/detect", headers=headers, json={
                    "base_url": f"http://127.0.0.1:{upstream.server_port}",
                    "api_key": "fixture-key", "protocol": "auto",
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["protocol"], "anthropic")
        self.assertEqual(len(received), 2)

    def test_auto_detection_failure_reports_each_protocol_attempt(self):
        class RejectedHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"error":{"message":"unauthorized client detected"}}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), RejectedHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                response = client.post("/api/channels/detect", headers=headers, json={
                    "base_url": f"http://127.0.0.1:{upstream.server_port}",
                    "api_key": "fixture-secret-key", "protocol": "auto",
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 502, response.text)
        payload = response.json()
        self.assertEqual([item["protocol"] for item in payload["attempts"]], ["openai_chat", "anthropic", "gemini"])
        self.assertEqual([item["status_code"] for item in payload["attempts"]], [401, 401, 401])
        self.assertNotIn("fixture-secret-key", response.text)

    def test_detect_anthropic_models_and_keep_manual_fallback_on_404(self):
        class AnthropicModelsHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"data": [{"id": "claude-fixture"}]}).encode() if self.path == "/v1/models" else b"{}"
                self.send_response(200 if self.path == "/v1/models" else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), AnthropicModelsHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                success = client.post("/api/channels/detect", headers=headers, json={
                    "base_url": f"http://127.0.0.1:{upstream.server_port}", "api_key": "fixture-anthropic-key", "protocol": "anthropic",
                })
                manual = client.post("/api/channels/detect", headers=headers, json={
                    "base_url": f"http://127.0.0.1:{upstream.server_port}/unsupported", "api_key": "fixture-anthropic-key", "protocol": "anthropic",
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(success.status_code, 200, success.text)
        self.assertEqual(success.json()["protocol"], "anthropic")
        self.assertEqual(success.json()["models"][0]["id"], "claude-fixture")
        self.assertEqual(manual.status_code, 200, manual.text)
        self.assertFalse(manual.json()["ok"])
        self.assertTrue(manual.json()["manual_only"])

    def test_detect_gemini_models_strips_prefix_and_records_capabilities(self):
        received = []

        class GeminiModelsHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append((self.path, dict(self.headers)))
                body = json.dumps({"models": [{
                    "name": "models/gemini-fixture", "supportedGenerationMethods": ["generateContent", "embedContent"],
                }]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), GeminiModelsHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                response = client.post("/api/channels/detect", headers=headers, json={
                    "base_url": f"http://127.0.0.1:{upstream.server_port}/v1beta", "api_key": "fixture-gemini-key", "protocol": "gemini",
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["models"][0], {"id": "gemini-fixture", "capabilities": ["generateContent", "embedContent"], "protocol": "gemini"})
        self.assertEqual(received[0][0], "/v1beta/models")
        self.assertEqual(_header(received[0][1], "x-goog-api-key"), "fixture-gemini-key")

    def test_detection_uses_existing_key_and_saves_protocol_without_returning_key(self):
        received = []

        class ExistingKeyHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(dict(self.headers))
                body = b'{"data":[{"id":"stored-model"}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), ExistingKeyHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
                headers = {"Authorization": "Bearer " + setup["token"]}
                created = client.post("/api/channels", headers=headers, json={
                    "name": "stored-auto", "type": "auto", "base_url": f"http://127.0.0.1:{upstream.server_port}",
                    "api_key": "stored-fixture-key", "models": "stored-model",
                }).json()
                response = client.post("/api/channels/detect", headers=headers, json={
                    "channel_id": created["id"], "base_url": f"http://127.0.0.1:{upstream.server_port}", "protocol": "auto",
                })
                channel = client.get("/api/channels", headers=headers).json()[0]
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(_header(received[0], "authorization"), "Bearer stored-fixture-key")
        self.assertNotIn("stored-fixture-key", response.text)
        self.assertEqual(channel["detected_protocol"], "openai_chat")

    def test_openai_responses_upstream_converts_to_chat_completion(self):
        received = []

        class ResponsesHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, dict(self.headers), json.loads(_read_request_body(self))))
                body = json.dumps({
                    "id": "resp_fixture", "object": "response", "model": "response-model",
                    "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]}],
                    "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), ResponsesHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}/v1", model="response-model", channel_type="openai_responses")
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "response-model", "messages": [{"role": "user", "content": "hi"}], "stream": False,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "OK")
        self.assertEqual(response.json()["usage"]["total_tokens"], 5)
        self.assertEqual(received[0][0], "/v1/responses")
        self.assertEqual(received[0][2]["input"][0]["role"], "user")

    def test_openai_responses_stream_converts_text_and_done(self):
        class ResponsesStreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                events = [
                    {"type": "response.created", "response": {"id": "resp_stream", "model": "response-model"}},
                    {"type": "response.output_text.delta", "delta": "OK"},
                    {"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}},
                ]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in events:
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), ResponsesStreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}", model="response-model", channel_type="openai_responses")
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "response-model", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"content": "OK"', response.text)
        self.assertEqual(_sse_payloads(response.text).count("[DONE]"), 1)

    def test_gemini_upstream_converts_to_chat_completion_and_uses_header_key(self):
        received = []

        class GeminiHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, dict(self.headers), json.loads(_read_request_body(self))))
                body = json.dumps({
                    "candidates": [{"content": {"role": "model", "parts": [{"text": "OK"}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), GeminiHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}/v1beta", model="gemini-fixture", channel_type="gemini")
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "gemini-fixture", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.2,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "OK")
        self.assertEqual(received[0][0], "/v1beta/models/gemini-fixture:generateContent")
        self.assertEqual(_header(received[0][1], "x-goog-api-key"), "test-key")
        self.assertNotIn("test-key", received[0][0])
        self.assertEqual(received[0][2]["contents"][0]["role"], "user")

    def test_external_protocol_endpoints_accept_local_gateway_key_and_return_native_shapes(self):
        with TestClient(app) as client:
            _, key = self._configure_gateway(client, "mock://local", model="model-a", channel_type="mock")
            anthropic = client.post("/v1/messages", headers={"x-api-key": key}, json={
                "model": "model-a", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}],
            })
            responses = client.post("/v1/responses", headers={"Authorization": "Bearer " + key}, json={
                "model": "model-a", "input": "hi",
            })
            gemini = client.post("/v1beta/models/model-a:generateContent", headers={"x-goog-api-key": key}, json={
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            })
        self.assertEqual(anthropic.status_code, 200, anthropic.text)
        self.assertEqual(anthropic.json()["type"], "message")
        self.assertEqual(responses.status_code, 200, responses.text)
        self.assertEqual(responses.json()["object"], "response")
        self.assertEqual(gemini.status_code, 200, gemini.text)
        self.assertIn("candidates", gemini.json())

    def test_unauthorized_client_error_is_classified_without_api_key_diagnosis(self):
        class RestrictedHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                _read_request_body(self)
                body = b'{"error":{"type":"unauthorized_client_error","message":"unauthorized client detected"}}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), RestrictedHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                _, key = self._configure_gateway(client, f"http://127.0.0.1:{upstream.server_port}")
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                    "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
                })
                log = database.query_one("SELECT error_category FROM request_logs ORDER BY id DESC LIMIT 1")
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["error"]["type"], "client_restricted")
        self.assertIn("通用 API 客户端", response.json()["error"]["message"])
        self.assertNotIn("API Key 无效", response.json()["error"]["message"])
        self.assertEqual(log["error_category"], "client_restricted")

    def test_modal_layout_and_auto_detection_controls_are_present(self):
        script = Path("static/app.js").read_text(encoding="utf-8")
        styles = Path("static/style.css").read_text(encoding="utf-8")
        self.assertIn('value="auto"', script)
        self.assertIn("正在识别协议并加载模型", script)
        self.assertIn("重新检测并加载模型", script)
        self.assertIn("AbortController", script)
        self.assertIn('$("#f-type").value = result.protocol', script)
        self.assertIn("data.message ||", script)
        self.assertNotIn("?api_key=", script)
        self.assertIn("place-items: center", styles)
        self.assertIn("max-height: calc(100vh - 48px)", styles)
        self.assertIn("overflow: auto", styles)


if __name__ == "__main__":
    unittest.main()
