import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import database, gateway
from app.main import app


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = database.DATA_DIR, database.DB_PATH
        database.DATA_DIR = Path(self.tmp.name)
        database.DB_PATH = database.DATA_DIR / "obsessionapi.db"
        gateway._BREAKER.clear()

    def tearDown(self):
        database.DATA_DIR, database.DB_PATH = self.old
        self.tmp.cleanup()

    def _admin(self, client):
        setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
        return {"Authorization": f"Bearer {setup['token']}"}, setup["user"]["id"]

    def test_channel_secret_is_encrypted_at_rest_and_decrypted_for_gateway(self):
        database.init_db()
        database.execute("INSERT INTO channels(name,api_key,created_at) VALUES(?,?,?)", ("one", "secret", "2026-01-01 00:00:00"))
        raw_conn = sqlite3.connect(database.DB_PATH)
        raw = raw_conn.execute("SELECT api_key FROM channels").fetchone()[0]
        raw_conn.close()
        self.assertTrue(raw.startswith("enc:v1:"))
        self.assertEqual(database.query_one("SELECT api_key FROM channels")["api_key"], "secret")

    def test_channel_update_blank_key_and_config_export_do_not_double_encrypt(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            created = client.post("/api/channels", headers=headers, json={
                "name": "fixture", "type": "mock", "models": "fixture-model", "api_key": "fixture-upstream-key",
            })
            channel_id = created.json()["id"]
            updated = client.put(f"/api/channels/{channel_id}", headers=headers, json={
                "name": "fixture-renamed", "type": "mock", "models": "fixture-model",
                "api_key": "", "priority": 0, "weight": 1,
            })
            masked = client.get("/api/config/export", headers=headers)
            full = client.get("/api/config/export?include_secrets=true", headers=headers)
        raw_conn = sqlite3.connect(database.DB_PATH)
        try:
            raw = raw_conn.execute(
                "SELECT api_key FROM channels WHERE id=?", (channel_id,)
            ).fetchone()[0]
        finally:
            raw_conn.close()
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertTrue(raw.startswith("enc:v1:"))
        self.assertNotIn("enc:v1:enc:v1:", raw)
        self.assertEqual(database.query_one("SELECT api_key FROM channels WHERE id=?", (channel_id,))["api_key"], "fixture-upstream-key")
        self.assertNotIn("fixture-upstream-key", masked.text)
        self.assertEqual(full.json()["channels"][0]["api_key"], "fixture-upstream-key")

    def test_dashboard_settings_logs_and_detail_contracts(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            client.post("/api/channels", headers=headers, json={"name": "local", "type": "mock", "models": "local-model"})
            client.post("/api/keys", headers=headers, json={"name": "client", "user_id": user_id})
            database.execute("INSERT INTO request_logs(request_id,username,model,endpoint,status,status_code,error_category,created_at) VALUES(?,?,?,?,?,?,?,?)", ("rid", "owner", "local-model", "/v1/chat/completions", "error", 429, "rate_limit", "2026-09-09 00:00:00"))
            database.execute("INSERT INTO request_logs(request_id,username,model,endpoint,status,status_code,error_category,created_at) VALUES(?,?,?,?,?,?,?,?)", ("rid-5xx", "owner", "local-model", "/v1/chat/completions", "error", 502, "upstream_5xx", "2026-09-09 00:00:01"))
            dashboard = client.get("/api/dashboard?range=30d", headers=headers)
            settings = client.get("/api/settings", headers=headers)
            logs = client.get("/api/logs?q=rid&error_category=rate_limit", headers=headers)
            five_xx = client.get("/api/logs?status_code=5xx", headers=headers)
            detail = client.get("/api/logs/1", headers=headers)
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("status_strip", dashboard.json())
        self.assertIn("series", dashboard.json())
        self.assertEqual(settings.json()["retry_max_attempts"], 3)
        self.assertEqual(logs.json()["failed_count"], 1)
        self.assertEqual(five_xx.json()["total"], 1)
        self.assertEqual(detail.json()["item"]["request_id"], "rid")

    def test_all_cooling_channels_are_not_called_and_return_request_id(self):
        with TestClient(app) as client:
            setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
            admin_headers = {"Authorization": "Bearer " + setup["token"]}
            created = client.post("/api/channels", headers=admin_headers, json={
                "name": "cooling-only", "type": "mock", "models": "cooling-model",
            })
            channel_id = created.json()["id"]
            key = client.post("/api/keys", headers=admin_headers, json={
                "name": "client", "user_id": setup["user"]["id"],
            }).json()["key"]
            gateway._BREAKER[channel_id] = {"streak": 2, "until": time.time() + 60}
            response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                "model": "cooling-model", "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["type"], "channels_cooling")
        self.assertEqual(response.headers.get("x-request-id"), response.json()["request_id"])
        self.assertEqual(database.query_one("SELECT COUNT(*) c FROM request_logs")["c"], 0)

    def test_dashboard_and_logs_separate_client_requests_from_upstream_attempts(self):
        with TestClient(app) as client:
            headers, _ = self._admin(client)
            created_at = database.iso(database.utcnow())
            database.execute(
                """INSERT INTO request_logs(request_id,channel_id,channel_name,model,endpoint,
                   prompt_tokens,completion_tokens,latency_ms,status,status_code,will_retry,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("rid-retry", 1, "primary", "model-a", "/v1/chat/completions", 3, 0, 40, "error", 502, 1, created_at),
            )
            database.execute(
                """INSERT INTO request_logs(request_id,channel_id,channel_name,model,endpoint,
                   prompt_tokens,completion_tokens,latency_ms,status,status_code,will_retry,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("rid-retry", 2, "backup", "model-a", "/v1/chat/completions", 3, 2, 90, "success", 200, 0, created_at),
            )
            dashboard = client.get("/api/dashboard", headers=headers)
            logs = client.get("/api/logs", headers=headers)
        metrics = dashboard.json()["metrics"]
        self.assertEqual(metrics["today_requests"], 1)
        self.assertEqual(metrics["success_rate"], 100.0)
        self.assertEqual(metrics["today_attempts"], 2)
        self.assertEqual(metrics["today_retries"], 1)
        self.assertEqual(logs.json()["client_requests"], 1)
        self.assertEqual(logs.json()["attempts"], 2)
        self.assertEqual(logs.json()["retries"], 1)

    def test_dashboard_and_logs_separate_upstream_and_estimated_tokens(self):
        with TestClient(app) as client:
            headers, _ = self._admin(client)
            database.init_db()
            gateway._record("rid-real", None, None, "model-a", "/v1/chat/completions",
                            3, 4, 0, 20, "success", 200, "", "127.0.0.1",
                            usage_source="upstream")
            gateway._record("rid-est", None, None, "model-a", "/v1/chat/completions",
                            2, 3, 0, 30, "success", 200, "", "127.0.0.1",
                            usage_source="estimated")
            gateway._record("rid-error", None, None, "model-a", "/v1/chat/completions",
                            1, 6, 0, 40, "error", 502, "upstream failed", "127.0.0.1",
                            usage_source="upstream")
            dashboard = client.get("/api/dashboard", headers=headers)
            logs = client.get("/api/logs", headers=headers)
        metrics = dashboard.json()["metrics"]
        self.assertEqual(metrics["today_upstream_tokens"], 14)
        self.assertEqual(metrics["today_estimated_tokens"], 5)
        self.assertEqual(logs.json()["upstream_tokens"], 14)
        self.assertEqual(logs.json()["estimated_tokens"], 5)

    def test_models_expose_real_channel_bindings_and_availability(self):
        with TestClient(app) as client:
            headers, _ = self._admin(client)
            client.post("/api/channels", headers=headers, json={
                "name": "primary", "type": "mock", "models": "shared-model",
                "model_mapping": {"shared-model": "upstream-primary"}, "priority": 10,
            })
            client.post("/api/channels", headers=headers, json={
                "name": "disabled", "type": "mock", "models": "shared-model", "status": 0,
            })
            response = client.get("/api/models", headers=headers)
        model = next(item for item in response.json() if item["model_name"] == "shared-model")
        bindings = {item["channel_name"]: item for item in model["bindings"]}
        self.assertEqual(model["channel_count"], 2)
        self.assertEqual(model["available_channel_count"], 1)
        self.assertEqual(bindings["primary"]["upstream_model"], "upstream-primary")
        self.assertTrue(bindings["primary"]["enabled"])
        self.assertFalse(bindings["disabled"]["enabled"])

    def test_error_classifier_keeps_non_retry_categories_explicit(self):
        self.assertEqual(gateway.classify_upstream_error(429, "too many requests"), "rate_limit")
        self.assertEqual(gateway.classify_upstream_error(400, "invalid request"), "invalid_request")
        self.assertEqual(gateway.classify_upstream_error(500, "upstream"), "upstream_5xx")

    def test_non_object_json_body_returns_400_instead_of_500(self):
        with TestClient(app) as client:
            setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
            headers = {"Authorization": "Bearer " + setup["token"]}
            key = client.post("/api/keys", headers=headers, json={
                "name": "client", "user_id": setup["user"]["id"],
            }).json()["key"]
            response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json=[])
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["type"], "invalid_request_error")

    def test_model_wildcards_do_not_expand_channel_matching(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            client.post("/api/channels", headers=headers, json={
                "name": "literal-model", "type": "mock", "models": "modelX",
            })
            client.post("/api/models", headers=headers, json={"model_name": "model_%"})
            key = client.post("/api/keys", headers=headers, json={
                "name": "client", "user_id": user_id,
            }).json()["key"]
            response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                "model": "model_%", "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["type"], "no_available_channel")

    def test_all_cooling_channels_advertise_retry_after(self):
        with TestClient(app) as client:
            setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
            headers = {"Authorization": "Bearer " + setup["token"]}
            created = client.post("/api/channels", headers=headers, json={
                "name": "cooling-only", "type": "mock", "models": "cooling-model",
            })
            channel_id = created.json()["id"]
            key = client.post("/api/keys", headers=headers, json={
                "name": "client", "user_id": setup["user"]["id"],
            }).json()["key"]
            gateway._BREAKER[channel_id] = {"streak": 2, "until": time.time() + 60}
            response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json={
                "model": "cooling-model", "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertEqual(response.status_code, 503, response.text)
        self.assertGreaterEqual(int(response.headers["retry-after"]), 1)

    def test_malformed_model_and_messages_return_400_instead_of_500(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            client.post("/api/channels", headers=headers, json={
                "name": "fixture", "type": "mock", "models": "model-a",
            })
            key = client.post("/api/keys", headers=headers, json={
                "name": "client", "user_id": user_id,
            }).json()["key"]
            auth = {"Authorization": "Bearer " + key}
            bad_model = client.post("/v1/chat/completions", headers=auth, json={
                "model": ["model-a"], "messages": [{"role": "user", "content": "hi"}],
            })
            bad_messages = client.post("/v1/chat/completions", headers=auth, json={
                "model": "model-a", "messages": ["hi"],
            })
        for response in (bad_model, bad_messages):
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()["error"]["type"], "invalid_request_error")


if __name__ == "__main__":
    unittest.main()
