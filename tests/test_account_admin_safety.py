"""Account credentials must never enter generic model-list probes."""
import json
import unittest
from unittest import mock

import httpx
from fastapi import HTTPException
from starlette.requests import Request

from app import admin_api, health_probe


def request(body):
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req._body = json.dumps(body).encode()
    return req


def channel(protocol):
    return {"id": 12, "name": "fixture-account", "type": protocol,
            "base_url": "https://fixture.invalid", "models": "fixture-model",
            "model_mapping": '{"fixture-model":"native-fixture"}', "api_key": json.dumps({
                "access_token": "fixture-access", "refresh_token": "fixture-refresh"}),
            "extra_headers": "{}", "forward_headers": "[]", "user_agent": ""}


class AccountAdminSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_probes_do_not_send_generic_credentials(self):
        for protocol in ("access_gemini", "access_minimax", "access_cline", "lobsterai", "opencode_free"):
            with self.subTest(protocol=protocol), mock.patch.object(health_probe, "_http_client") as client:
                result = await health_probe.probe_channel(channel(protocol))
                client.assert_not_called()
                self.assertFalse(result["ok"])
                self.assertEqual(result["error_category"], "model_list_unavailable")
                self.assertNotIn("fixture-refresh", json.dumps(result))

    async def test_explicit_account_detection_stops_before_generic_transport(self):
        for protocol in ("access_gemini", "access_minimax", "access_cline", "lobsterai", "opencode_free"):
            with self.subTest(protocol=protocol), \
                    mock.patch.object(admin_api, "require_admin"), \
                    mock.patch.object(admin_api, "_http_client") as client:
                with self.assertRaises(HTTPException) as error:
                    await admin_api.detect_channel(request({"protocol": protocol,
                        "base_url": "https://fixture.invalid", "api_key": channel(protocol)["api_key"]}))
                self.assertEqual(error.exception.status_code, 400)
                client.assert_not_called()

    async def test_account_inference_test_uses_native_transport_without_logging_usage(self):
        chat = [{"choices": [{"delta": {"content": "OK"}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
        anthropic = [{"type": "message_start", "message": {"id": "fixture"}},
                     {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "OK"}},
                     {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                     {"type": "message_stop"}]
        gemini = [{"response": {"candidates": [{"content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP"}]}}]
        for protocol, path, frames in (("access_gemini", "/v1internal:streamGenerateContent?alt=sse", gemini),
                                       ("access_minimax", "/mavis/api/v1/llm/v1/messages", anthropic),
                                       ("access_cline", "/api/v1/chat/completions", chat),
                                       ("lobsterai", "/api/proxy/v1/chat/completions", chat),
                                       ("opencode_free", "/inference/v1/chat/completions", chat)):
            calls = []

            def handle(req):
                calls.append({"url": str(req.url), "headers": dict(req.headers), "body": json.loads(req.content)})
                raw = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw.encode())

            def factory(*args, **kwargs):
                return httpx.AsyncClient(transport=httpx.MockTransport(handle))

            with self.subTest(protocol=protocol), mock.patch.object(admin_api, "require_admin"), \
                    mock.patch.object(admin_api, "query_one", return_value=channel(protocol)), \
                    mock.patch.object(admin_api, "execute") as write, \
                    mock.patch.object(admin_api, "_http_client", side_effect=factory):
                result = await admin_api.channels_test(12, request({"model": "fixture-model", "max_tokens": 32}))
                self.assertTrue(result["ok"], result)
                self.assertEqual(len(calls), 1)
                self.assertTrue(calls[0]["url"].endswith(path), calls)
                self.assertEqual(calls[0]["body"]["model"], "native-fixture")
                if protocol != "access_gemini":
                    self.assertTrue(calls[0]["body"]["stream"])
                    self.assertEqual(calls[0]["body"]["max_tokens"], 32)
                else:
                    self.assertEqual(calls[0]["body"]["request"]["generationConfig"]["maxOutputTokens"], 32)
                self.assertNotIn("fixture-refresh", json.dumps(calls))
                self.assertNotIn("fixture-access", json.dumps(result))
                write.assert_not_called()

    async def test_account_test_error_body_cannot_echo_refresh_token(self):
        def factory(*args, **kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
                403, json={"error": {"message": "fixture-access fixture-refresh"}})))

        with mock.patch.object(admin_api, "require_admin"), \
                mock.patch.object(admin_api, "query_one", return_value=channel("access_cline")), \
                mock.patch.object(admin_api, "_http_client", side_effect=factory):
            result = await admin_api.channels_test(12, request({"model": "fixture-model"}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 403)
        self.assertNotIn("fixture-access", json.dumps(result))
        self.assertNotIn("fixture-refresh", json.dumps(result))

    async def test_account_test_partial_text_at_token_limit_is_incomplete(self):
        frames = [{"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]},
                  {"choices": [{"delta": {}, "finish_reason": "length"}]}]
        raw = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)

        def factory(*args, **kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=raw.encode())))

        with mock.patch.object(admin_api, "require_admin"), \
                mock.patch.object(admin_api, "query_one", return_value=channel("access_cline")), \
                mock.patch.object(admin_api, "_http_client", side_effect=factory):
            result = await admin_api.channels_test(12, request({"model": "fixture-model"}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["completion_state"], "incomplete")
        self.assertEqual(result["error_category"], "incomplete_output")

    async def test_saved_account_cannot_be_probed_using_generic_override(self):
        with mock.patch.object(admin_api, "require_admin"), \
                mock.patch.object(admin_api, "query_one", return_value=channel("access_cline")), \
                mock.patch.object(admin_api, "_http_client") as client:
            with self.assertRaises(HTTPException) as error:
                await admin_api.detect_channel(request({"channel_id": 12, "protocol": "openai_chat"}))
            self.assertEqual(error.exception.status_code, 400)
            client.assert_not_called()

    async def test_discovery_checks_explicit_and_saved_account_types(self):
        for body in ({"type": "access_minimax", "base_url": "https://fixture.invalid",
                      "api_key": channel("access_minimax")["api_key"]},
                     {"type": "openai_chat", "base_url": "https://fixture.invalid", "channel_id": 12}):
            with self.subTest(body=body), mock.patch.object(admin_api, "require_admin"), \
                    mock.patch.object(admin_api, "query_one", return_value=channel("access_minimax")), \
                    mock.patch.object(admin_api, "_http_client") as client:
                with self.assertRaises(HTTPException) as error:
                    await admin_api.discover_channel_models(request(body))
                self.assertEqual(error.exception.status_code, 400)
                client.assert_not_called()
