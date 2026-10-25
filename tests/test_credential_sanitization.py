"""Credential sanitization tests: secrets never reach errors, logs or exports.

Reference: our-free-model usage-sanitize/sanitize suites - assert the
user-visible surface only, against the real sanitizer functions.
"""
import json
from unittest import mock

from fastapi.testclient import TestClient

from app import database, gateway
from app.main import app


class SanitizeTestCaseBase:
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir, self.old_path = database.DATA_DIR, database.DB_PATH
        database.DATA_DIR = Path(self.tmp.name)
        database.DB_PATH = database.DATA_DIR / "obsessionapi.db"
        gateway._BREAKER.clear()

    def tearDown(self):
        database.DATA_DIR, database.DB_PATH = self.old_dir, self.old_path
        self.tmp.cleanup()


class UpstreamErrorSanitizeTests(SanitizeTestCaseBase, __import__("unittest").TestCase):
    def test_secret_in_detail_is_redacted(self):
        msg = gateway.upstream_error_message("ch", 401, "bad key sk-secret123", secret="sk-secret123")
        self.assertNotIn("sk-secret123", msg)
        self.assertIn("[redacted]", msg)

    def test_api_key_in_url_query_is_redacted(self):
        msg = gateway.upstream_error_message("ch", 402, "failed https://x.io/v1?api_key=ABCD1234&x=1")
        self.assertNotIn("ABCD1234", msg)
        self.assertIn("api_key=[redacted]", msg)

    def test_html_error_page_is_reduced(self):
        msg = gateway.upstream_error_message("ch", 502, "<!doctype html><html><body>Cloudflare 524</body></html>", content_type="text/html")
        self.assertNotIn("<html", msg)

    def test_json_error_message_is_extracted(self):
        msg = gateway.upstream_error_message("ch", 429, json.dumps({"error": {"message": "rate limited", "type": "too_many"}}))
        self.assertIn("rate limited", msg)
        self.assertNotIn("{", msg)


class ConfigExportSanitizeTests(SanitizeTestCaseBase, __import__("unittest").TestCase):
    """配置导出与渠道列表绝不能携带渠道密钥（含账号反代的凭据 JSON）。"""

    def _admin(self, client):
        setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
        return {"Authorization": "Bearer " + setup["token"]}

    def test_export_omits_channel_secrets(self):
        database.init_db()
        with TestClient(app) as client:
            headers = self._admin(client)
            created = client.post("/api/channels", headers=headers, json={
                "name": "sec", "type": "openai_chat", "base_url": "http://127.0.0.1:9/v1",
                "api_key": "sk-live-SECRETKEY", "models": "m"})
            self.assertEqual(created.status_code, 200, created.text)
            exported = client.get("/api/config/export", headers=headers)
            self.assertEqual(exported.status_code, 200)
            body = json.dumps(exported.json(), ensure_ascii=False)
            self.assertNotIn("sk-live-SECRETKEY", body)

    def test_channel_listing_hides_credentials(self):
        database.init_db()
        with TestClient(app) as client:
            headers = self._admin(client)
            created = client.post("/api/channels", headers=headers, json={
                "name": "lob", "type": "lobsterai", "api_key": "", "models": "m"})
            self.assertEqual(created.status_code, 200, created.text)
            database.execute("UPDATE channels SET api_key=? WHERE id=?",
                             (json.dumps({"access_token": "SECRET-TOKEN", "refresh_token": "R"}),
                              created.json()["id"]))
            listed = client.get("/api/channels", headers=headers)
            self.assertEqual(listed.status_code, 200)
            self.assertNotIn("SECRET-TOKEN", listed.text)
