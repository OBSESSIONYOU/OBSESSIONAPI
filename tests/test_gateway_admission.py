"""Admission regressions use synthetic upstreams; no real database or network."""
import asyncio
from contextlib import ExitStack
from email.utils import formatdate
import json
from types import SimpleNamespace
import unittest
from unittest import mock

import httpx

from app import gateway


def request_fixture():
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    request.is_disconnected = mock.AsyncMock(return_value=False)
    return request


def channel_fixture(channel_id=1, **overrides):
    return {"id": channel_id, "name": f"fixture-{channel_id}", "type": "openai", "models": "m",
            "base_url": "http://fixture.invalid", "api_key": "upstream-fixture-key", "model_mapping": "{}",
            "priority": 1, "weight": 1, "rpm_limit": 0, "tool_policy": "keep", **overrides}


class GatewayAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        gateway._reset_runtime_state()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(gateway._reset_runtime_state)
        self.key = {"id": 7, "concurrency_limit": 0}
        self.channels = [channel_fixture()]
        settings = {**gateway.settings_store.DEFAULTS, "fail_threshold": 1, "cooldown_seconds": 300,
                    "retry_backoff_ms": 0, "retry_max_attempts": 3}
        self.stack.enter_context(mock.patch.object(gateway, "_settings", return_value=settings))
        self.stack.enter_context(mock.patch.object(gateway, "_authorize_request", side_effect=lambda *a: (self.key, None)))
        self.stack.enter_context(mock.patch.object(gateway, "_auth_key", side_effect=lambda *a: self.key))
        self.stack.enter_context(mock.patch.object(gateway, "_resolve_candidates", side_effect=lambda model, rid: (gateway._candidates(model), None)))
        self.stack.enter_context(mock.patch.object(gateway, "_model_channels", side_effect=lambda model: self.channels))
        self.stack.enter_context(mock.patch.object(gateway, "_pricing", return_value=(0, 0)))
        self.records = self.stack.enter_context(mock.patch.object(gateway, "_record"))
        self.stack.enter_context(mock.patch.object(gateway, "_persist_breaker"))

    def upstream(self, responder):
        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, *args, **kwargs):
                return await responder(*args, **kwargs)

        self.stack.enter_context(mock.patch.object(gateway, "_http_client", side_effect=lambda *a, **k: Client()))

    async def call(self, kind="chat", request=None):
        if kind == "embeddings":
            return await gateway._embeddings(request or request_fixture(), {"model": "m", "input": "hi"})
        return await gateway._chat(request or request_fixture(), {"model": "m", "messages": [{"role": "user", "content": "hi"}]})

    async def test_request_errors_are_logged_without_poisoning_channel_health(self):
        async def responder(*a, **kw):
            return httpx.Response(400, json={"error": {"message": "invalid request: context too long"}})

        self.upstream(responder)
        for kind in ("chat", "embeddings"):
            with self.subTest(kind=kind):
                gateway._BREAKER.clear()
                for _ in range(3):
                    await self.call(kind)
                self.assertEqual(gateway._BREAKER.get(1, {}).get("streak", 0), 0)
                self.assertFalse(gateway.is_channel_cooling(1))
        self.assertEqual(self.records.call_count, 6)

    async def test_content_filter_does_not_penalize_the_channel(self):
        async def responder(*a, **kw):
            return httpx.Response(400, json={"error": {"message": "content filter rejected this prompt"}})

        self.upstream(responder)
        for kind in ("chat", "embeddings"):
            with self.subTest(kind=kind):
                gateway._BREAKER.clear()
                await self.call(kind)
                self.assertFalse(gateway.is_channel_cooling(1))

    async def test_auth_failure_still_cools_the_channel(self):
        async def responder(*a, **kw):
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})

        self.upstream(responder)
        await self.call()
        self.assertTrue(gateway.is_channel_cooling(1))

    async def test_concurrent_fallbacks_cannot_overbook_one_channel_rpm_slot(self):
        for kind in ("chat", "embeddings"):
            with self.subTest(kind=kind):
                gateway._reset_runtime_state()
                self.channels = [channel_fixture(), channel_fixture(2, type="mock", priority=0, rpm_limit=1)]
                arrivals = 0
                ready = asyncio.Event()

                async def responder(*a, **kw):
                    nonlocal arrivals
                    arrivals += 1
                    if arrivals == 2:
                        ready.set()
                    await ready.wait()
                    return httpx.Response(500, text="fixture upstream failed")

                self.upstream(responder)
                responses = await asyncio.gather(self.call(kind), self.call(kind))
                self.assertEqual(sorted(r.status_code for r in responses), [200, 429])
                self.assertEqual(len(gateway._CHANNEL_RATE[2]), 1)
                denied = next(r for r in responses if r.status_code == 429)
                self.assertEqual(denied.headers["Retry-After"], "60")
                self.assertEqual(json.loads(denied.body)["error"]["type"], "channel_rate_limited")

    async def test_stale_saturated_candidate_is_skipped_for_an_available_backup(self):
        self.channels = [channel_fixture(), channel_fixture(2, type="mock", priority=0, rpm_limit=1),
                         channel_fixture(3, type="mock", priority=-1)]

        async def responder(*a, **kw):
            gateway._channel_rate_consume(2, 1)
            return httpx.Response(500, text="fixture upstream failed")

        self.upstream(responder)
        response = await self.call("embeddings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.records.call_args.args[2]["id"], 3)
        self.assertEqual(len(gateway._CHANNEL_RATE[2]), 1)

    async def test_embeddings_and_chat_share_one_key_concurrency_budget(self):
        self.key["concurrency_limit"] = 1
        entered = asyncio.Event()
        release = asyncio.Event()

        async def responder(*a, **kw):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5]}], "usage": {"prompt_tokens": 1}})

        self.upstream(responder)
        task = asyncio.create_task(self.call("embeddings"))
        await entered.wait()
        try:
            blocked = await asyncio.wait_for(self.call("chat"), 0.5)
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(json.loads(blocked.body)["error"]["type"], "concurrency_limit_exceeded")
        finally:
            release.set()
            await task
        self.assertNotIn(7, gateway._CONCURRENCY)

    async def test_chat_occupancy_blocks_embedding_and_invalid_embedding_releases_slot(self):
        self.key["concurrency_limit"] = 1
        gateway._key_acquire(7)
        blocked = await self.call("embeddings")
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.headers["Retry-After"], "1")
        self.assertEqual(gateway._CONCURRENCY[7], 1)
        gateway._key_release(7)
        invalid = await gateway._embeddings(request_fixture(), {"model": "m"})
        self.assertEqual(invalid.status_code, 400)
        self.assertNotIn(7, gateway._CONCURRENCY)

    async def test_cancelled_embedding_releases_its_key_slot(self):
        self.key["concurrency_limit"] = 1
        entered = asyncio.Event()

        async def responder(*a, **kw):
            entered.set()
            await asyncio.Event().wait()

        self.upstream(responder)
        task = asyncio.create_task(self.call("embeddings"))
        await entered.wait()
        try:
            self.assertEqual(gateway._CONCURRENCY.get(7), 1)
        finally:
            task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(7, gateway._CONCURRENCY)


class RetryAfterTests(unittest.TestCase):
    def test_client_cancellation_has_an_explicit_error_category(self):
        self.assertEqual(gateway.classify_upstream_error(499, "client disconnected"), "client_disconnected")
        self.assertIn("client_disconnected", gateway.ERROR_CATEGORIES)

    def test_standard_http_date_is_parsed_relative_to_now(self):
        now = 1_800_000_000
        with mock.patch.object(gateway.time, "time", return_value=now):
            self.assertEqual(gateway._parse_retry_after_ms({"retry-after": formatdate(now + 180, usegmt=True)}), 180000)
            self.assertEqual(gateway._parse_retry_after_ms({"retry-after": formatdate(now - 1, usegmt=True)}), 0)

    def test_fractional_seconds_are_not_rounded_down_in_cooldown(self):
        with mock.patch.object(gateway, "_settings", return_value={"cooldown_seconds": 300}):
            for value, expected in (("1.001", 2), ("0.0001", 1), ("2", 2)):
                with self.subTest(value=value):
                    parsed = gateway._parse_retry_after_ms({"retry-after": value})
                    self.assertGreater(parsed, 0)
                    self.assertEqual(gateway._cooldown_seconds_for(1, "rate_limit", parsed), expected)

    def test_request_error_preserves_an_existing_channel_failure_streak(self):
        original = gateway._BREAKER.get(987)
        gateway._BREAKER[987] = {"streak": 2, "until": 0.0, "cooldown_count": 1, "probation": 1}
        try:
            for reason in ("invalid_request", "content_filter"):
                gateway._breaker_mark(987, False, reason=reason)
            self.assertEqual(gateway._BREAKER[987], {"streak": 2, "until": 0.0, "cooldown_count": 1, "probation": 1})
        finally:
            if original is None:
                gateway._BREAKER.pop(987, None)
            else:
                gateway._BREAKER[987] = original


if __name__ == "__main__":
    unittest.main()
