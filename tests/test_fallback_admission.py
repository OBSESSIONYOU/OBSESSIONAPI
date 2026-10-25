"""Each ordinary API fallback POST must consume a separate channel RPM slot."""
import asyncio
from contextlib import ExitStack
import json
from types import SimpleNamespace
import unittest
from unittest import mock

import httpx
from starlette.responses import StreamingResponse

from app import gateway


class FallbackAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        gateway._reset_runtime_state()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(gateway._reset_runtime_state)
        self.stack.enter_context(mock.patch.object(gateway, "_settings", return_value={
            **gateway.settings_store.DEFAULTS, "retry_backoff_ms": 0,
        }))
        self.stack.enter_context(mock.patch.object(gateway, "_authorize_request", return_value=({"id": 1}, None)))
        self.stack.enter_context(mock.patch.object(gateway, "_pricing", return_value=(0, 0)))
        self.stack.enter_context(mock.patch.object(gateway, "_persist_breaker"))
        self.records = self.stack.enter_context(mock.patch.object(gateway, "_record"))

    async def run_fixture(self, protocol, limit, backup=False):
        gateway._reset_runtime_state()
        self.records.reset_mock()
        first = {"id": 1, "name": "primary", "type": protocol, "models": "m",
                 "base_url": "http://fixture.invalid", "api_key": "fixture", "model_mapping": "{}",
                 "priority": 1, "weight": 1, "rpm_limit": limit, "tool_policy": "keep"}
        candidates = [first]
        if backup:
            candidates.append({**first, "id": 2, "name": "backup", "type": "mock", "rpm_limit": 0})
        calls = []

        def handle(request):
            body = json.loads(request.content)
            calls.append(bool(body.get("stream")))
            if body.get("stream"):
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"data: [DONE]\n\n")
            result = ({"type": "message", "id": "fixture", "role": "assistant", "model": "m",
                       "content": [{"type": "text", "text": "fallback result"}], "stop_reason": "end_turn",
                       "usage": {"input_tokens": 1, "output_tokens": 2}}
                      if protocol == "anthropic" else
                      {"choices": [{"message": {"role": "assistant", "content": "fallback result"}, "finish_reason": "stop"}]})
            return httpx.Response(200, json=result)

        def client_factory(*args, **kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handle))

        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={},
                                  is_disconnected=mock.AsyncMock(return_value=False))
        with mock.patch.object(gateway, "_resolve_candidates", return_value=(candidates, None)), \
                mock.patch.object(gateway, "_http_client", side_effect=client_factory):
            response = await gateway._chat_inner(request, {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True})
            if isinstance(response, StreamingResponse):
                async for _ in response.body_iterator:
                    pass
        return response, calls

    async def test_one_rpm_slot_prevents_generic_fallback_post(self):
        response, calls = await self.run_fixture("openai_chat", 1)
        self.assertEqual(calls, [True])
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(gateway._CHANNEL_RATE[1]), 1)
        self.assertEqual(self.records.call_args.args[9:11], ("error", 429))

    async def test_one_rpm_slot_prevents_anthropic_fallback_post(self):
        response, calls = await self.run_fixture("anthropic", 1)
        self.assertEqual(calls, [True])
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(gateway._CHANNEL_RATE[1]), 1)
        self.assertEqual(self.records.call_args.args[9:11], ("error", 429))

    async def test_two_rpm_slots_allow_generic_fallback_and_count_both_posts(self):
        response, calls = await self.run_fixture("openai_chat", 2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [True, False])
        self.assertEqual(len(gateway._CHANNEL_RATE[1]), 2)
        self.assertEqual(self.records.call_args.args[9:11], ("success", 200))

    async def test_two_rpm_slots_allow_anthropic_fallback_and_count_both_posts(self):
        response, calls = await self.run_fixture("anthropic", 2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [True, False])
        self.assertEqual(len(gateway._CHANNEL_RATE[1]), 2)
        self.assertEqual(self.records.call_args.args[9:11], ("success", 200))

    async def test_denied_fallback_still_tries_next_candidate(self):
        for protocol in ("openai_chat", "anthropic"):
            with self.subTest(protocol=protocol):
                response, calls = await self.run_fixture(protocol, 1, backup=True)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(calls, [True])
                self.assertEqual([call.args[2]["id"] for call in self.records.call_args_list], [1, 2])

    async def test_unlimited_channels_keep_the_existing_fallback(self):
        for protocol in ("openai_chat", "anthropic"):
            with self.subTest(protocol=protocol):
                response, calls = await self.run_fixture(protocol, 0)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(calls, [True, False])


if __name__ == "__main__":
    unittest.main()
