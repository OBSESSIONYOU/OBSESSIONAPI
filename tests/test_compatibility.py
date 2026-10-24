import json
import asyncio
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from app import database
from app.gateway import (
    _as_native_response,
    _chat_response_state,
    _incomplete_response_detail,
    _invalid_response_detail,
    _native_stream_lines,
    _prepare_openai_reasoning,
    _valid_chat_response,
    build_channel_headers,
    normalize_extra_headers,
)
from app.main import app
from app.protocols import infer_channel_name, openai_to_anthropic_response


def _read_request_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    return handler.rfile.read(length)


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir, self.old_db_path = database.DATA_DIR, database.DB_PATH
        database.DATA_DIR = Path(self.tmp.name)
        database.DB_PATH = database.DATA_DIR / "obsessionapi.db"

    def tearDown(self):
        database.DATA_DIR, database.DB_PATH = self.old_data_dir, self.old_db_path
        self.tmp.cleanup()

    def _admin(self, client):
        setup = client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
        return {"Authorization": "Bearer " + setup["token"]}, setup

    def test_channel_name_inference_ignores_common_labels_and_paths(self):
        cases = {
            "https://api.scnet.cn/api/llm/v1": "scnet",
            "https://dashscope.aliyuncs.com/compatible-mode/v1": "dashscope",
            "https://open.bigmodel.cn/api/paas/v4": "bigmodel",
            "https://api.moonshot.cn/v1": "moonshot",
            "https://agentrouter.org/v1/messages": "agentrouter",
            "http://127.0.0.1:8123/v1": "local-8123",
        }
        for url, expected in cases.items():
            self.assertEqual(infer_channel_name(url), expected)

    def test_channel_header_safety_keeps_authentication_owned_by_gateway(self):
        channel = {
            "name": "fixture",
            "extra_headers": {"x-provider-client": "fixture-value"},
            "forward_headers": [],
            "user_agent": "OBSESSIONAPI/compat-test",
        }
        headers = build_channel_headers(channel, {"authorization": "Bearer gateway-key"}, protocol="openai_chat")
        self.assertEqual(headers["authorization"], "Bearer gateway-key")
        self.assertEqual(headers["x-provider-client"], "fixture-value")
        self.assertEqual(headers["User-Agent"], "OBSESSIONAPI/compat-test")
        with self.assertRaises(ValueError):
            normalize_extra_headers({"authorization": "should-not-win"})
        with self.assertRaises(ValueError):
            normalize_extra_headers({"x-provider": "line1\nline2"})

    def test_forward_headers_allow_provider_metadata_without_auth_override(self):
        class Incoming:
            headers = {
                "anthropic-beta": "prompt-caching-2024-07-31",
                "x-client-name": "fixture-client",
                "Authorization": "Bearer client-key",
                "Cookie": "should-not-forward",
            }

        channel = {
            "name": "fixture",
            "extra_headers": {},
            "forward_headers": ["anthropic-beta", "x-client-*"],
            "user_agent": "",
        }
        headers = build_channel_headers(channel, {"Authorization": "Bearer gateway-key"}, Incoming(), "anthropic")
        self.assertEqual(headers["anthropic-beta"], "prompt-caching-2024-07-31")
        self.assertEqual(headers["x-client-name"], "fixture-client")
        self.assertEqual(headers["Authorization"], "Bearer gateway-key")
        self.assertNotIn("Cookie", headers)

    def test_chat_response_validation_requires_real_output(self):
        valid = [
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            {"choices": [{"message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}]},
            {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
            ]}}]},
            {"choices": [{"message": {"role": "assistant", "content": None, "function_call": {
                "name": "lookup", "arguments": "{}"
            }}}]},
            {"choices": [{"message": {"role": "assistant", "content": None, "refusal": "不能完成"}}]},
        ]
        invalid = [
            {"choices": []},
            {"choices": [{"message": {"role": "assistant", "content": None, "reasoning_content": "思考"}}]},
            {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{}]}}]},
            {"choices": [{"message": {"role": "assistant", "content": None, "function_call": {}}}]},
            {"error": {"message": "upstream failed"}},
        ]
        for result in valid:
            self.assertTrue(_valid_chat_response(result), result)
        for result in invalid:
            self.assertFalse(_valid_chat_response(result), result)
        self.assertIn("reasoning", _invalid_response_detail(invalid[1]))

    def test_chat_response_state_distinguishes_budget_stop_and_empty_reasoning(self):
        self.assertEqual(_chat_response_state({
            "choices": [{"message": {"content": "OK", "reasoning_content": "thinking"}, "finish_reason": "stop"}],
        }), "valid")
        incomplete = {
            "choices": [{"message": {"content": None, "reasoning_content": "thinking"}, "finish_reason": "length"}],
            "usage": {"completion_tokens": 1024},
        }
        self.assertEqual(_chat_response_state(incomplete), "incomplete")
        self.assertIn("输出预算耗尽", _incomplete_response_detail(incomplete))
        self.assertEqual(_chat_response_state({
            "choices": [{"message": {"content": None, "reasoning_content": "thinking"}, "finish_reason": "stop"}],
        }), "invalid")
        empty_reasoning = {"choices": [{"message": {"content": None, "reasoning_content": ""}, "finish_reason": "stop"}]}
        self.assertEqual(_chat_response_state(empty_reasoning), "invalid")
        self.assertNotIn("reasoning", _invalid_response_detail(empty_reasoning))

    def test_openai_to_anthropic_preserves_finish_reason_and_rejects_bad_tools(self):
        length = openai_to_anthropic_response({
            "id": "chat_1", "model": "fixture-model",
            "choices": [{"message": {"role": "assistant", "content": None, "reasoning_content": "thinking"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 8},
        })
        self.assertEqual(length["content"], [])
        self.assertEqual(length["stop_reason"], "max_tokens")
        self.assertEqual(length["usage"], {"input_tokens": 4, "output_tokens": 8})
        stop_sequence = openai_to_anthropic_response({
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop_sequence"}],
        })
        self.assertEqual(stop_sequence["stop_reason"], "stop_sequence")
        tool = openai_to_anthropic_response({
            "id": "chat_2", "model": "fixture-model",
            "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": '{"q":"x"}'},
            }]}, "finish_reason": "tool_calls"}],
        })
        self.assertEqual(tool["stop_reason"], "tool_use")
        self.assertEqual(tool["content"][0]["input"], {"q": "x"})
        with self.assertRaises(ValueError):
            openai_to_anthropic_response({
                "choices": [{"message": {"content": None, "tool_calls": [{
                    "id": "call_1", "function": {"name": "lookup", "arguments": "not-json"},
                }]}, "finish_reason": "tool_calls"}],
            })
        native_stream = b"".join(_native_stream_lines("anthropic", {
            "id": "chat_3", "model": "fixture-model",
            "choices": [{"delta": {"reasoning_content": "thinking"}, "finish_reason": "length"}],
        }, {})).decode()
        self.assertIn('"stop_reason": "max_tokens"', native_stream)
        self.assertNotIn('"stop_reason": "end_turn"', native_stream)

    def test_anthropic_stream_has_text_and_tool_block_lifecycles(self):
        def events(chunks):
            state = {}
            result = []
            for chunk in chunks:
                for raw in _native_stream_lines("anthropic", chunk, state):
                    lines = raw.decode().splitlines()
                    result.append((lines[0].removeprefix("event: "), json.loads(lines[1][6:])))
            return result

        text = events([
            {"id": "text", "model": "fixture-model", "choices": [{"delta": {"role": "assistant"}}]},
            {"id": "text", "model": "fixture-model", "choices": [{"delta": {"content": "OK"}}]},
            {"id": "text", "model": "fixture-model", "choices": [{"delta": {}, "finish_reason": "stop"}]},
        ])
        self.assertEqual([name for name, _ in text], [
            "message_start", "content_block_start", "content_block_delta",
            "content_block_stop", "message_delta", "message_stop",
        ])
        self.assertEqual(text[1][1]["content_block"]["type"], "text")
        self.assertEqual(text[4][1]["delta"]["stop_reason"], "end_turn")

        tool = events([
            {"id": "tool", "model": "fixture-model", "choices": [{"delta": {
                "tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "lookup"}}]
            }}]},
            {"id": "tool", "model": "fixture-model", "choices": [{"delta": {
                "tool_calls": [{"index": 0, "function": {"arguments": '{"q":"x"}'}}]
            }}]},
            {"id": "tool", "model": "fixture-model", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ])
        self.assertEqual([name for name, _ in tool], [
            "message_start", "content_block_start", "content_block_delta",
            "content_block_stop", "message_delta", "message_stop",
        ])
        self.assertEqual(tool[4][1]["delta"]["stop_reason"], "tool_use")

    def test_anthropic_stream_forwards_upstream_error_and_rejects_abnormal_eof(self):
        state = {}
        error = b"".join(_native_stream_lines("anthropic", {
            "error": {"type": "server_error", "message": "upstream failed"},
        }, state)).decode()
        self.assertIn("event: error", error)
        self.assertNotIn("event: message_stop", error)

        async def collect():
            response = await _as_native_response(
                StreamingResponse(iter([
                    b'data: {"id":"eof","model":"fixture-model","choices":[{"delta":{"role":"assistant"}}]}\n\n',
                    b"data: [DONE]\n\n",
                ])), "anthropic", "rid-eof"
            )
            return b"".join([chunk async for chunk in response.body_iterator]).decode()

        eof = asyncio.run(collect())
        self.assertIn("event: error", eof)
        self.assertNotIn("event: message_stop", eof)

        async def collect_empty():
            response = await _as_native_response(
                StreamingResponse(iter([b"data: [DONE]\n\n"])), "anthropic", "rid-empty"
            )
            return b"".join([chunk async for chunk in response.body_iterator]).decode()

        empty = asyncio.run(collect_empty())
        self.assertIn("event: error", empty)
        self.assertNotIn("event: message_stop", empty)

        reasoning_only = b"".join(_native_stream_lines("anthropic", {
            "choices": [{"delta": {"reasoning_content": "thinking"}, "finish_reason": "stop"}],
        }, {})).decode()
        self.assertIn("event: error", reasoning_only)
        self.assertNotIn("event: message_stop", reasoning_only)

    def test_anthropic_route_wraps_openai_stream_with_native_lifecycle(self):
        received = []

        class OpenAIStreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(_read_request_body(self)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for payload in (
                    {"id": "chat_1", "model": "fixture-model", "choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]},
                    {"id": "chat_1", "model": "fixture-model", "choices": [{"delta": {"content": "OK"}, "finish_reason": None}]},
                    {"id": "chat_1", "model": "fixture-model", "choices": [{"delta": {}, "finish_reason": "stop"}]},
                    "[DONE]",
                ):
                    self.wfile.write(("data: " + (payload if isinstance(payload, str) else json.dumps(payload)) + "\n\n").encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), OpenAIStreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                admin, setup = self._admin(client)
                created = client.post("/api/channels", headers=admin, json={
                    "name": "stream-fixture", "type": "openai",
                    "base_url": f"http://127.0.0.1:{upstream.server_port}",
                    "api_key": "fixture-upstream", "models": "fixture-model",
                })
                key = client.post("/api/keys", headers=admin, json={
                    "name": "client", "user_id": setup["user"]["id"],
                }).json()["key"]
                response = client.post("/v1/messages", headers={"x-api-key": key}, json={
                    "model": "fixture-model", "max_tokens": 32,
                    "messages": [{"role": "user", "content": "hi"}], "stream": True,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [line[7:] for line in response.text.splitlines() if line.startswith("event: ")],
            ["message_start", "content_block_start", "content_block_delta",
             "content_block_stop", "message_delta", "message_stop"],
        )
        self.assertIn('"stop_reason": "end_turn"', response.text)
        self.assertEqual(received[0]["max_tokens"], 32)

    def test_thinking_is_mapped_only_for_configured_scnet_endpoint(self):
        scnet_body = {"thinking": {"type": "enabled"}}
        _prepare_openai_reasoning({"base_url": "https://api.scnet.cn/api/llm/v1"}, scnet_body)
        self.assertEqual(scnet_body, {"thinking": {"type": "enabled"}})
        other_body = {"thinking": {"type": "enabled"}}
        with self.assertRaises(ValueError):
            _prepare_openai_reasoning({"base_url": "https://example.test/v1"}, other_body)
        self.assertNotIn("thinking", other_body)

    def test_channel_test_explicit_budget_classifies_length_without_protocol_error(self):
        received = []

        class LengthResponseHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                received.append(json.loads(_read_request_body(self)))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "choices": [{"message": {"role": "assistant", "content": None, "reasoning_content": "thinking"},
                                  "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 32},
                }).encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), LengthResponseHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                admin, _ = self._admin(client)
                created = client.post("/api/channels", headers=admin, json={
                    "name": "length-200", "type": "openai", "base_url": f"http://127.0.0.1:{upstream.server_port}",
                    "api_key": "fixture-upstream", "models": "fixture-model",
                })
                result = client.post(f"/api/channels/{created.json()['id']}/test", headers=admin, json={
                    "model": "fixture-model", "max_tokens": 32,
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(result.status_code, 200, result.text)
        payload = result.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status_code"], 200)
        self.assertEqual(payload["max_tokens"], 32)
        self.assertEqual(payload["completion_state"], "incomplete")
        self.assertEqual(payload["error_category"], "incomplete_output")
        self.assertIn("输出预算耗尽", payload["message"])
        self.assertEqual(received[0]["max_tokens"], 32)

    def test_channel_test_rejects_http_200_without_chat_output(self):
        class EmptyResponseHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":[]}')

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "choices": [{"message": {"role": "assistant", "content": None,
                                               "reasoning_content": "thinking only"}}]
                }).encode())

            def log_message(self, *_):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), EmptyResponseHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with TestClient(app) as client:
                admin, _ = self._admin(client)
                created = client.post("/api/channels", headers=admin, json={
                    "name": "empty-200", "type": "openai",
                    "base_url": f"http://127.0.0.1:{upstream.server_port}",
                    "api_key": "fixture-upstream", "models": "fixture-model",
                })
                result = client.post(f"/api/channels/{created.json()['id']}/test", headers=admin, json={
                    "model": "fixture-model",
                })
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()
        self.assertEqual(result.status_code, 200, result.text)
        payload = result.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status_code"], 200)
        self.assertEqual(payload["error_category"], "protocol_error")
        self.assertIn("HTTP 200", payload["message"])

    def test_preview_and_duplicate_v1_models_alias_are_backward_compatible(self):
        with TestClient(app) as client:
            admin, _ = self._admin(client)
            preview = client.post("/api/channels/preview-url", headers=admin, json={
                "base_url": "https://api.scnet.cn/api/llm/v1",
            })
            created = client.post("/api/channels", headers=admin, json={
                "name": "scnet", "type": "mock", "base_url": "mock://local",
                "api_key": "fixture-upstream", "models": "fixture-model",
                "extra_headers": {"x-fixture": "secret"}, "forward_headers": ["x-client-*"],
            })
            duplicate = client.get("/v1/v1/models")
            channels = client.get("/api/channels", headers=admin)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["suggested_name"], "scnet")
        self.assertEqual(preview.json()["normalized_base_url"], "https://api.scnet.cn/api/llm/v1")
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertEqual(duplicate.headers.get("X-OBSESSIONAPI-Path-Warning"), "duplicated-v1")
        self.assertEqual(duplicate.json()["data"][0]["id"], "fixture-model")
        public_channel = channels.json()[0]
        self.assertEqual(public_channel["extra_headers"], ["x-fixture"])
        self.assertNotIn("secret", channels.text)

    def test_duplicate_v1_protocol_aliases_keep_native_shapes_and_streaming(self):
        with TestClient(app) as client:
            admin, setup = self._admin(client)
            client.post("/api/channels", headers=admin, json={
                "name": "mock", "type": "mock", "models": "fixture-model",
            })
            key = client.post("/api/keys", headers=admin, json={
                "name": "compat", "user_id": setup["user"]["id"],
            }).json()["key"]
            auth = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
            chat = client.post("/v1/v1/chat/completions", headers=auth, json={
                "model": "fixture-model", "messages": [{"role": "user", "content": "hi"}],
            })
            stream = client.post("/v1/v1/chat/completions", headers=auth, json={
                "model": "fixture-model", "messages": [{"role": "user", "content": "hi"}], "stream": True,
            })
            anthropic = client.post("/v1/v1/messages", headers={"x-api-key": key}, json={
                "model": "fixture-model", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}],
            })
            responses = client.post("/v1/v1/responses", headers=auth, json={
                "model": "fixture-model", "input": "hi",
            })
        for response in (chat, stream, anthropic, responses):
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers.get("X-OBSESSIONAPI-Path-Warning"), "duplicated-v1")
        self.assertIn("[DONE]", stream.text)
        self.assertEqual(anthropic.json()["type"], "message")
        self.assertEqual(responses.json()["object"], "response")


if __name__ == "__main__":
    unittest.main()
