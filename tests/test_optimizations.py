"""本轮优化的回归契约：熔断持久化/自动禁用恢复、Key 权限限流、渠道代理、响应缓存与分位统计。"""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import database, gateway
from app.main import app


class OptimizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = database.DATA_DIR, database.DB_PATH
        database.DATA_DIR = Path(self.tmp.name)
        database.DB_PATH = database.DATA_DIR / "obsessionapi.db"
        gateway._reset_runtime_state()

    def tearDown(self):
        gateway._reset_runtime_state()
        database.DATA_DIR, database.DB_PATH = self.old
        self.tmp.cleanup()

    def _admin(self, client):
        setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
        return {"Authorization": f"Bearer {setup['token']}"}, setup["user"]["id"]

    def _mock_channel(self, client, headers, name="local", models="model-a"):
        return client.post("/api/channels", headers=headers, json={
            "name": name, "type": "mock", "models": models,
        }).json()["id"]

    def _client_key(self, client, headers, user_id, **extra):
        return client.post("/api/keys", headers=headers, json={
            "name": "client", "user_id": user_id, **extra,
        }).json()["key"]

    # ---------------------------------------------------------- 熔断持久化
    def test_breaker_state_survives_process_restart(self):
        database.init_db()
        database.execute("INSERT INTO channels(name,type,models,created_at) VALUES(?,?,?,?)",
                         ("c1", "mock", "model-a", database.iso(database.utcnow())))
        channel_id = database.query_one("SELECT id FROM channels")["id"]
        for _ in range(int(gateway.settings_store.DEFAULTS["fail_threshold"])):
            gateway._breaker_mark(channel_id, False, reason="network")
        self.assertTrue(gateway.is_channel_cooling(channel_id))

        # 模拟进程重启：清空内存后从数据库恢复
        gateway._BREAKER.clear()
        self.assertFalse(gateway.is_channel_cooling(channel_id))
        gateway.load_breaker_state()
        self.assertTrue(gateway.is_channel_cooling(channel_id))
        self.assertGreaterEqual(gateway.channel_breaker_state(channel_id)["cooldown_remaining_s"], 1)

    def test_repeated_cooldowns_auto_disable_then_manual_recover(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            channel_id = self._mock_channel(client, headers)
            client.put("/api/settings", headers=headers, json={
                "settings": {"fail_threshold": 2, "auto_disable_after": 1, "cooldown_seconds": 60},
            })
            key = self._client_key(client, headers, user_id)
            auth = {"Authorization": "Bearer " + key}

            gateway._breaker_mark(channel_id, False, reason="upstream_5xx")
            gateway._breaker_mark(channel_id, False, reason="upstream_5xx")

            self.assertTrue(gateway.is_channel_auto_disabled(channel_id))
            blocked = client.post("/v1/chat/completions", headers=auth, json={
                "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
            })
            self.assertEqual(blocked.status_code, 503, blocked.text)
            self.assertEqual(blocked.json()["error"]["type"], "channels_cooling")

            recovered = client.post(f"/api/channels/{channel_id}/recover", headers=headers)
            self.assertEqual(recovered.status_code, 200, recovered.text)
            self.assertFalse(gateway.is_channel_auto_disabled(channel_id))

            ok = client.post("/v1/chat/completions", headers=auth, json={
                "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
            })
            self.assertEqual(ok.status_code, 200, ok.text)

    def test_auto_recover_loop_reenables_expired_channel(self):
        database.init_db()
        database.execute("INSERT INTO channels(name,type,models,created_at) VALUES(?,?,?,?)",
                         ("c1", "mock", "model-a", database.iso(database.utcnow())))
        channel_id = database.query_one("SELECT id FROM channels")["id"]
        gateway._BREAKER[channel_id] = {
            "streak": 0, "until": 0.0, "last_failure_count": 2, "cooldown_count": 3,
            "auto_disabled": 1, "disabled_reason": "连续失败", "disabled_at": None,
            "disabled_at_ts": time.time() - 10,
        }
        with mock.patch.object(gateway.settings_store, "get_all",
                               return_value={**gateway.settings_store.DEFAULTS, "auto_recover_seconds": 5}):
            recovered = gateway._auto_recover_once()
        self.assertEqual(recovered, 1)
        self.assertFalse(gateway.is_channel_auto_disabled(channel_id))
        # v1.1 半愈合：到点恢复不再满血——欠账减半、进入观察期，要一次真实成功才彻底清零
        state = gateway.channel_breaker_state(channel_id)
        self.assertTrue(state["probation"], state)
        gateway._breaker_mark(channel_id, True, real_completion=False)  # 再来一次探测成功仍是半愈合
        self.assertTrue(gateway.channel_breaker_state(channel_id)["probation"])
        gateway._breaker_mark(channel_id, True, real_completion=True)   # 真实对话成功才彻底恢复
        self.assertFalse(gateway.channel_breaker_state(channel_id)["probation"])

    def test_graded_cooldown_by_error_category(self):
        """分级冷却：auth 类用满额 cooldown_seconds，瞬时故障走 5s 指数退避，429 听 Retry-After。"""
        database.init_db()
        database.execute("INSERT INTO channels(name,type,models,created_at) VALUES(?,?,?,?)",
                         ("c1", "mock", "model-a", database.iso(database.utcnow())))
        channel_id = database.query_one("SELECT id FROM channels")["id"]
        with mock.patch.object(gateway.settings_store, "get_all",
                               return_value={**gateway.settings_store.DEFAULTS,
                                             "fail_threshold": 1, "cooldown_seconds": 300}):
            gateway._breaker_mark(channel_id, False, reason="auth")
            self.assertEqual(gateway._BREAKER[channel_id]["last_cooldown_seconds"], 300)
            gateway._breaker_mark(channel_id, True)
            gateway._breaker_mark(channel_id, False, reason="upstream_5xx")
            self.assertEqual(gateway._BREAKER[channel_id]["last_cooldown_seconds"], 5)
            gateway._breaker_mark(channel_id, True)
            gateway._breaker_mark(channel_id, False, reason="rate_limit", retry_after_ms=45000)
            self.assertEqual(gateway._BREAKER[channel_id]["last_cooldown_seconds"], 45)
            gateway._breaker_mark(channel_id, True)
            gateway._breaker_mark(channel_id, False, reason="rate_limit")  # 无 Retry-After → 60s
            self.assertEqual(gateway._BREAKER[channel_id]["last_cooldown_seconds"], 60)

    def test_swrr_long_term_share_matches_weights(self):
        """SWRR 平滑加权轮询：3:1 权重的两个渠道，长期份额精确等于 75%/25%。"""
        database.init_db()
        now = database.iso(database.utcnow())
        database.execute("INSERT INTO channels(name,type,models,priority,weight,created_at) VALUES(?,?,?,?,?,?)",
                         ("a", "mock", "model-a", 1, 3, now))
        database.execute("INSERT INTO channels(name,type,models,priority,weight,created_at) VALUES(?,?,?,?,?,?)",
                         ("b", "mock", "model-a", 1, 1, now))
        gateway._SWRR_CUR.clear(); gateway._SWRR_HITS.clear()
        gateway._SWRR_TOTAL = 0
        picks = []
        for _ in range(40):
            ordered = gateway._candidates("model-a")
            picks.append(ordered[0]["name"])
        share_a = picks.count("a") / len(picks)
        self.assertAlmostEqual(share_a, 0.75, delta=0.01)
        stats = gateway.swrr_stats()
        self.assertEqual(stats["total"], 40)
        self.assertAlmostEqual(stats["channels"][str(gateway.query_one('SELECT id FROM channels WHERE name=?', ('a',))['id'])]["share_pct"], 75.0, delta=0.5)

    # ---------------------------------------------------------- Key 权限与限流
    def test_key_model_whitelist_blocks_other_models(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            self._mock_channel(client, headers, models="model-a,model-b")
            key = self._client_key(client, headers, user_id, allowed_models=["model-a"])
            auth = {"Authorization": "Bearer " + key}
            allowed = client.post("/v1/chat/completions", headers=auth, json={
                "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
            })
            denied = client.post("/v1/chat/completions", headers=auth, json={
                "model": "model-b", "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["type"], "model_not_allowed")

    def test_key_rpm_limit_returns_429(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            self._mock_channel(client, headers)
            key = self._client_key(client, headers, user_id, rpm_limit=1)
            auth = {"Authorization": "Bearer " + key}
            first = client.post("/v1/chat/completions", headers=auth, json={
                "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
            })
            second = client.post("/v1/chat/completions", headers=auth, json={
                "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 429, second.text)
        self.assertEqual(second.json()["error"]["type"], "rate_limit_exceeded")

    def test_key_concurrency_limit_blocks_when_slot_taken(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            self._mock_channel(client, headers)
            key = self._client_key(client, headers, user_id, concurrency_limit=1)
            auth = {"Authorization": "Bearer " + key}
            key_row = database.query_one("SELECT id FROM api_keys")
            gateway._CONCURRENCY[key_row["id"]] = 1
            blocked = client.post("/v1/chat/completions", headers=auth, json={
                "model": "model-a", "messages": [{"role": "user", "content": "hi"}],
            })
        self.assertEqual(blocked.status_code, 429, blocked.text)
        self.assertEqual(blocked.json()["error"]["type"], "concurrency_limit_exceeded")

    def test_key_limits_are_visible_in_admin_list(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            self._client_key(client, headers, user_id, allowed_models=["model-a", "model-b"],
                             rpm_limit=30, concurrency_limit=4)
            listed = client.get("/api/keys", headers=headers).json()[0]
        self.assertEqual(listed["allowed_models"], ["model-a", "model-b"])
        self.assertEqual(listed["rpm_limit"], 30)
        self.assertEqual(listed["concurrency_limit"], 4)

    # ---------------------------------------------------------- 渠道代理
    def test_channel_proxy_is_stored_and_masked(self):
        with TestClient(app) as client:
            headers, _ = self._admin(client)
            created = client.post("/api/channels", headers=headers, json={
                "name": "proxied", "type": "mock", "models": "model-a",
                "proxy_url": "http://user:pass@127.0.0.1:7890",
                "connect_timeout_s": 3, "read_timeout_s": 30, "max_concurrency": 8,
            })
            self.assertEqual(created.status_code, 200, created.text)
            row = client.get("/api/channels", headers=headers).json()[0]
            invalid = client.post("/api/channels", headers=headers, json={
                "name": "bad", "type": "mock", "models": "model-a", "proxy_url": "ftp://nope",
            })
        self.assertEqual(row["proxy_url"], "http://••••••:••••••@127.0.0.1:7890")
        self.assertEqual(row["connect_timeout_s"], 3)
        self.assertEqual(row["read_timeout_s"], 30)
        self.assertEqual(row["max_concurrency"], 8)
        self.assertEqual(invalid.status_code, 400, invalid.text)

    def test_http_client_applies_channel_proxy_and_limits(self):
        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with mock.patch.object(gateway.httpx, "AsyncClient", FakeClient):
            gateway._http_client({"proxy_url": "http://127.0.0.1:7890", "max_concurrency": 5})
        self.assertEqual(captured.get("proxy"), "http://127.0.0.1:7890")
        self.assertIsInstance(captured.get("limits"), gateway.httpx.Limits)

    # ---------------------------------------------------------- 响应缓存
    def test_response_cache_returns_hit_header_and_reuses_body(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            self._mock_channel(client, headers)
            client.put("/api/settings", headers=headers, json={"settings": {"cache_enabled": True}})
            key = self._client_key(client, headers, user_id)
            auth = {"Authorization": "Bearer " + key}
            payload = {"model": "model-a", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}
            first = client.post("/v1/chat/completions", headers=auth, json=payload)
            second = client.post("/v1/chat/completions", headers=auth, json=payload)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertNotEqual(first.headers.get("x-cache"), "HIT")
        self.assertEqual(second.headers.get("x-cache"), "HIT")
        self.assertEqual(first.json()["choices"][0]["message"]["content"],
                         second.json()["choices"][0]["message"]["content"])

    def test_response_cache_skips_streaming_and_hot_temperature(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            self._mock_channel(client, headers)
            client.put("/api/settings", headers=headers, json={"settings": {"cache_enabled": True}})
            key = self._client_key(client, headers, user_id)
            auth = {"Authorization": "Bearer " + key}
            hot = client.post("/v1/chat/completions", headers=auth, json={
                "model": "model-a", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.9,
            })
        self.assertNotEqual(hot.headers.get("x-cache"), "HIT")

    # ---------------------------------------------------------- 分位统计
    def test_dashboard_reports_latency_percentiles_and_model_health(self):
        with TestClient(app) as client:
            headers, _ = self._admin(client)
            for index, latency in enumerate([10, 20, 30, 100]):
                database.execute(
                    "INSERT INTO request_logs(request_id,username,model,endpoint,prompt_tokens,completion_tokens,"
                    "usage_source,latency_ms,status,status_code,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (f"rid-{index}", "owner", "model-a", "/v1/chat/completions", 1, 1, "upstream",
                     latency, "success", 200, database.iso(database.utcnow())),
                )
            dashboard = client.get("/api/dashboard", headers=headers).json()
        metrics = dashboard["metrics"]
        self.assertEqual(metrics["max_latency_ms"], 100)
        self.assertIsNotNone(metrics["p95_latency_ms"])
        self.assertIsNotNone(metrics["p99_latency_ms"])
        self.assertGreaterEqual(metrics["p99_latency_ms"], metrics["p95_latency_ms"])
        self.assertEqual(dashboard["status_strip"]["auto_disabled_channels"], 0)
        self.assertEqual(dashboard["model_health"][0]["model"], "model-a")

    def test_health_reports_auto_disabled_channels(self):
        with TestClient(app) as client:
            headers, _ = self._admin(client)
            channel_id = self._mock_channel(client, headers)
            gateway._BREAKER[channel_id] = {
                "streak": 0, "until": 0.0, "last_failure_count": 3, "cooldown_count": 3,
                "auto_disabled": 1, "disabled_reason": "连续失败", "disabled_at": None, "disabled_at_ts": 0.0,
            }
            health = client.get("/health").json()
        self.assertEqual(health["auto_disabled_channels"], 1)

    # ---------------------------------------------------------- /v1/models 鉴权与白名单
    def test_models_endpoint_filters_by_key_whitelist(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            self._mock_channel(client, headers, models="model-a,model-b")
            open_key = self._client_key(client, headers, user_id)
            scoped_key = self._client_key(client, headers, user_id, allowed_models=["model-a"])
            anonymous = client.get("/v1/models")
            unrestricted = client.get("/v1/models", headers={"Authorization": "Bearer " + open_key})
            scoped = client.get("/v1/models", headers={"Authorization": "Bearer " + scoped_key})
        self.assertEqual({item["id"] for item in anonymous.json()["data"]}, {"model-a", "model-b"})
        self.assertEqual({item["id"] for item in unrestricted.json()["data"]}, {"model-a", "model-b"})
        self.assertEqual({item["id"] for item in scoped.json()["data"]}, {"model-a"})

    def test_models_endpoint_requires_key_when_enabled(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            self._mock_channel(client, headers)
            client.put("/api/settings", headers=headers, json={"settings": {"models_require_key": True}})
            anonymous = client.get("/v1/models")
            key = self._client_key(client, headers, user_id)
            authorized = client.get("/v1/models", headers={"Authorization": "Bearer " + key})
        self.assertEqual(anonymous.status_code, 401, anonymous.text)
        self.assertEqual(anonymous.json()["error"]["type"], "invalid_api_key")
        self.assertEqual(authorized.status_code, 200, authorized.text)

    # ---------------------------------------------------------- 渠道级限流
    def test_channel_rpm_limit_skips_saturated_channel(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            client.post("/api/channels", headers=headers, json={
                "name": "saturated", "type": "mock", "models": "model-a", "rpm_limit": 1,
            })
            key = self._client_key(client, headers, user_id)
            auth = {"Authorization": "Bearer " + key}
            payload = {"model": "model-a", "messages": [{"role": "user", "content": "hi"}]}
            first = client.post("/v1/chat/completions", headers=auth, json=payload)
            second = client.post("/v1/chat/completions", headers=auth, json=payload)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 429, second.text)
        self.assertEqual(second.json()["error"]["type"], "channel_rate_limited")
        self.assertEqual(second.headers.get("retry-after"), "60")

    def test_channel_rpm_limit_is_configurable_and_visible(self):
        with TestClient(app) as client:
            headers, _ = self._admin(client)
            client.post("/api/channels", headers=headers, json={
                "name": "limited", "type": "mock", "models": "model-a", "rpm_limit": 50,
            })
            listed = client.get("/api/channels", headers=headers).json()[0]
            routes = client.get("/api/routes?model=model-a", headers=headers).json()
        self.assertEqual(listed["rpm_limit"], 50)
        self.assertTrue(any("速率上限" in rule for rule in routes["rules"]))

    # ---------------------------------------------------------- 指数退避
    def test_retry_backoff_is_exponential_capped_and_jittered(self):
        captured = []

        async def fake_sleep(seconds):
            captured.append(seconds)

        settings = {**gateway.settings_store.DEFAULTS, "retry_backoff_ms": 100,
                    "retry_backoff_max_ms": 250, "retry_backoff_jitter_pct": 0}
        with mock.patch.object(gateway.asyncio, "sleep", fake_sleep), \
                mock.patch.object(gateway.settings_store, "get_all", return_value=settings):
            for index in range(3):
                asyncio.run(gateway._retry_pause(index))
        self.assertEqual(captured, [0.1, 0.2, 0.25])

    def test_retry_backoff_stays_within_jitter_band(self):
        captured = []

        async def fake_sleep(seconds):
            captured.append(seconds)

        settings = {**gateway.settings_store.DEFAULTS, "retry_backoff_ms": 1000,
                    "retry_backoff_max_ms": 60000, "retry_backoff_jitter_pct": 25}
        with mock.patch.object(gateway.asyncio, "sleep", fake_sleep), \
                mock.patch.object(gateway.settings_store, "get_all", return_value=settings):
            for _ in range(20):
                asyncio.run(gateway._retry_pause(1))
        self.assertTrue(all(1.5 <= value <= 2.5 for value in captured), captured)

    # ---------------------------------------------------------- 日志索引
    def test_log_query_indexes_are_created(self):
        database.init_db()
        names = {row["name"] for row in database.query(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='request_logs'")}
        self.assertIn("idx_logs_request_id", names)
        self.assertIn("idx_logs_category", names)
        self.assertIn("idx_logs_channel_created", names)
        self.assertIn("idx_logs_status_created", names)


    # ---------------------------------------------------------- /v1/embeddings
    def test_embeddings_endpoint_returns_vectors_and_records_usage(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            self._mock_channel(client, headers, models="embed-a")
            key = self._client_key(client, headers, user_id)
            auth = {"Authorization": "Bearer " + key}
            response = client.post("/v1/embeddings", headers=auth, json={
                "model": "embed-a", "input": ["你好", "world"],
            })
            logs = client.get("/api/logs?model=embed-a", headers=headers).json()
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["object"], "list")
        self.assertEqual(len(payload["data"]), 2)
        self.assertEqual([item["index"] for item in payload["data"]], [0, 1])
        self.assertEqual(len(payload["data"][0]["embedding"]), gateway.EMBEDDING_MOCK_DIM)
        self.assertEqual(payload["data"][0]["object"], "embedding")
        self.assertGreater(payload["usage"]["prompt_tokens"], 0)
        self.assertEqual(logs["total"], 1)
        self.assertEqual(logs["items"][0]["endpoint"], "/v1/embeddings")

    def test_embeddings_is_deterministic_for_same_input(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            self._mock_channel(client, headers, models="embed-a")
            key = self._client_key(client, headers, user_id)
            auth = {"Authorization": "Bearer " + key}
            body = {"model": "embed-a", "input": "同一个输入"}
            first = client.post("/v1/embeddings", headers=auth, json=body).json()
            second = client.post("/v1/embeddings", headers=auth, json=body).json()
        self.assertEqual(first["data"][0]["embedding"], second["data"][0]["embedding"])

    def test_embeddings_validates_input_and_model(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            self._mock_channel(client, headers, models="embed-a")
            key = self._client_key(client, headers, user_id)
            auth = {"Authorization": "Bearer " + key}
            missing = client.post("/v1/embeddings", headers=auth, json={"model": "embed-a"})
            bad_input = client.post("/v1/embeddings", headers=auth, json={"model": "embed-a", "input": [1, 2]})
            bad_model = client.post("/v1/embeddings", headers=auth, json={"model": ["embed-a"], "input": "hi"})
        for response in (missing, bad_input, bad_model):
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()["error"]["type"], "invalid_request_error")

    def test_embeddings_enforces_auth_and_model_whitelist(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            self._mock_channel(client, headers, models="embed-a,embed-b")
            key = self._client_key(client, headers, user_id, allowed_models=["embed-a"])
            auth = {"Authorization": "Bearer " + key}
            anonymous = client.post("/v1/embeddings", json={"model": "embed-a", "input": "hi"})
            denied = client.post("/v1/embeddings", headers=auth, json={"model": "embed-b", "input": "hi"})
        self.assertEqual(anonymous.status_code, 401, anonymous.text)
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["type"], "model_not_allowed")

    def test_embeddings_rejects_unsupported_protocol(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            client.post("/api/channels", headers=headers, json={
                "name": "anthropic-only", "type": "anthropic",
                "base_url": "https://api.example.com", "models": "embed-a",
            })
            key = self._client_key(client, headers, user_id)
            response = client.post("/v1/embeddings", headers={"Authorization": "Bearer " + key},
                                   json={"model": "embed-a", "input": "hi"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["type"], "unsupported_protocol")

    def test_embeddings_duplicate_v1_path_is_flagged(self):
        with TestClient(app) as client:
            headers, user_id = self._admin(client)
            self._mock_channel(client, headers, models="embed-a")
            key = self._client_key(client, headers, user_id)
            response = client.post("/v1/v1/embeddings", headers={"Authorization": "Bearer " + key},
                                   json={"model": "embed-a", "input": "hi"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers.get("X-OBSESSIONAPI-Path-Warning"), "duplicated-v1")

    # ---------------------------------------------------------- 渠道批量操作
    def test_channels_batch_disable_enable_and_recover(self):
        with TestClient(app) as client:
            headers, _ = self._admin(client)
            first = self._mock_channel(client, headers, name="c1", models="model-a")
            second = self._mock_channel(client, headers, name="c2", models="model-b")
            disabled = client.post("/api/channels/batch", headers=headers,
                                   json={"action": "disable", "ids": [first, second]})
            statuses = {row["id"]: row["status"] for row in client.get("/api/channels", headers=headers).json()}
            enabled = client.post("/api/channels/batch", headers=headers,
                                  json={"action": "enable", "ids": [first]})
            gateway._BREAKER[second] = {
                "streak": 0, "until": 0.0, "last_failure_count": 3, "cooldown_count": 3,
                "auto_disabled": 1, "disabled_reason": "连续失败", "disabled_at": None, "disabled_at_ts": 0.0,
            }
            recovered = client.post("/api/channels/batch", headers=headers,
                                    json={"action": "recover", "ids": [second]})
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertEqual(disabled.json()["affected"], 2)
        self.assertEqual(statuses[first], 0)
        self.assertEqual(statuses[second], 0)
        self.assertEqual(enabled.json()["affected"], 1)
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertFalse(gateway.is_channel_auto_disabled(second))

    def test_channels_batch_probe_returns_per_channel_results(self):
        with TestClient(app) as client:
            headers, _ = self._admin(client)
            channel_id = self._mock_channel(client, headers)
            result = client.post("/api/channels/batch", headers=headers,
                                 json={"action": "probe", "ids": [channel_id]})
        self.assertEqual(result.status_code, 200, result.text)
        payload = result.json()
        self.assertEqual(payload["action"], "probe")
        self.assertEqual(payload["affected"], 1)
        self.assertTrue(payload["results"][0]["ok"])

    def test_channels_batch_rejects_bad_input(self):
        with TestClient(app) as client:
            headers, _ = self._admin(client)
            bad_action = client.post("/api/channels/batch", headers=headers,
                                     json={"action": "explode", "ids": [1]})
            empty_ids = client.post("/api/channels/batch", headers=headers,
                                    json={"action": "enable", "ids": []})
            missing = client.post("/api/channels/batch", headers=headers,
                                  json={"action": "enable", "ids": [9999]})
            anonymous = client.post("/api/channels/batch", json={"action": "enable", "ids": [1]})
        self.assertEqual(bad_action.status_code, 400, bad_action.text)
        self.assertEqual(empty_ids.status_code, 400, empty_ids.text)
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual(anonymous.status_code, 401, anonymous.text)


if __name__ == "__main__":
    unittest.main()
