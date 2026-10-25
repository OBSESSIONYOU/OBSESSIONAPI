"""Offline regressions for SSE framing, terminal errors and caller cancellation."""
import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import anyio
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import database, gateway
from app.main import app


def chat(delta=None, finish=None):
    return {"id": "fixture", "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


class ChatStreamIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = database.DATA_DIR, database.DB_PATH
        database.DATA_DIR = Path(self.tmp.name)
        database.DB_PATH = database.DATA_DIR / "fixture.db"
        gateway._reset_runtime_state()

    def tearDown(self):
        gateway._reset_runtime_state()
        database.DATA_DIR, database.DB_PATH = self.old
        self.tmp.cleanup()

    def configure(self, client, protocol, base):
        setup = client.post("/api/setup", json={"username": "owner", "password": "fixture-password"}).json()
        admin = {"Authorization": "Bearer " + setup["token"]}
        response = client.post("/api/channels", headers=admin, json={"name": "fixture", "type": protocol,
            "base_url": base, "api_key": "fixture", "models": "m"})
        self.assertEqual(response.status_code, 200, response.text)
        key = client.post("/api/keys", headers=admin, json={"name": "fixture", "user_id": setup["user"]["id"]}).json()["key"]
        return key, database.query_one("SELECT id FROM channels")["id"]

    def call_http(self, protocol, raw):
        hits = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                hits.append(bool(body.get("stream")))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/json")
                self.end_headers()
                self.wfile.write(raw if body.get("stream") else json.dumps({
                    "choices": [{"message": {"role": "assistant", "content": "DUPLICATE"}, "finish_reason": "stop"}]
                }).encode())

            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with TestClient(app) as client:
                key, _ = self.configure(client, protocol, f"http://127.0.0.1:{server.server_port}")
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key},
                    json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True})
            return response, hits, [dict(row) for row in database.query("SELECT status,status_code FROM request_logs")]
        finally:
            server.shutdown()
            worker.join()
            server.server_close()

    def test_compact_bom_crlf_multiline_chat_is_one_generation(self):
        text = json.dumps(chat({"content": "你好"}), ensure_ascii=False, indent=2)
        raw = "\ufeff: heartbeat\r\n\r\n" + "\r\n".join("data:" + line for line in text.splitlines()) + "\r\n\r\n"
        raw += "data:" + json.dumps(chat(finish="stop")) + "\r\n\r\ndata:[DONE]\r\n\r\n"
        response, hits, logs = self.call_http("openai_chat", raw.encode())
        self.assertEqual(hits, [True])
        self.assertIn("你好", response.text)
        self.assertNotIn("DUPLICATE", response.text)
        self.assertEqual(logs[-1]["status"], "success")

    def test_multiline_anthropic_is_one_generation(self):
        events = [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, {"type": "message_stop"}]
        raw = "\ufeff: heartbeat\n\n" + "".join("\n".join("data:" + line for line in json.dumps(event, indent=2).splitlines()) + "\n\n" for event in events)
        response, hits, logs = self.call_http("anthropic", raw.encode())
        self.assertEqual(hits, [True])
        self.assertIn("hello", response.text)
        self.assertEqual(logs[-1]["status"], "success")

    def test_invalid_json_after_output_fails_and_stops_later_content(self):
        raw = "data: " + json.dumps(chat({"content": "partial"})) + "\n\ndata: not-json\n\n"
        raw += "data: " + json.dumps(chat({"content": "MUST_NOT_LEAK"}, "stop")) + "\n\ndata: [DONE]\n\n"
        response, hits, logs = self.call_http("openai_chat", raw.encode())
        self.assertEqual(hits, [True])
        self.assertIn('"error"', response.text)
        self.assertNotIn("data: not-json", response.text)
        self.assertNotIn("MUST_NOT_LEAK", response.text)
        self.assertEqual(logs[-1]["status"], "error")

    def test_explicit_error_is_terminal(self):
        events = [chat({"content": "partial"}), {"error": {"message": "fixture error"}},
                  chat({"content": "MUST_NOT_LEAK"}, "stop")]
        raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        response, _, logs = self.call_http("openai_chat", raw.encode())
        self.assertIn('"error"', response.text)
        self.assertNotIn("MUST_NOT_LEAK", response.text)
        self.assertEqual(logs[-1]["status"], "error")

    def test_malformed_anthropic_after_output_emits_error(self):
        event = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}}
        raw = "data:" + json.dumps(event) + "\n\ndata:not-json\n\n"
        response, hits, logs = self.call_http("anthropic", raw.encode())
        self.assertEqual(hits, [True])
        self.assertIn('"error"', response.text)
        self.assertEqual(logs[-1]["status"], "error")

    def test_buffered_chat_text_precedes_finish_and_late_usage(self):
        events = [chat({"content": "Bingo"}), chat(finish="stop"),
                  {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}]
        raw = "".join("data:" + json.dumps(event) + "\n\n" for event in events) + "data:[DONE]\n\n"
        response, hits, _ = self.call_http("openai_chat", raw.encode())
        self.assertEqual(hits, [True])
        parsed = [json.loads(line[5:].strip()) for line in response.text.splitlines()
                  if line.startswith("data:") and line[5:].strip() != "[DONE]"]
        content_index = next(i for i, event in enumerate(parsed) if event.get("choices")
            and event["choices"][0].get("delta", {}).get("content") == "Bingo")
        finish_index = next(i for i, event in enumerate(parsed) if event.get("choices")
            and event["choices"][0].get("finish_reason"))
        usage_index = next(i for i, event in enumerate(parsed) if event.get("usage"))
        self.assertLess(content_index, finish_index)
        self.assertLess(finish_index, usage_index)

    def test_buffered_anthropic_text_precedes_finish(self):
        events = [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Bingo"}},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, {"type": "message_stop"}]
        raw = "".join("data:" + json.dumps(event) + "\n\n" for event in events)
        response, _, _ = self.call_http("anthropic", raw.encode())
        self.assertLess(response.text.index('"content": "Bingo"'), response.text.index('"finish_reason": "stop"'))

    def _cancel_stream(self, protocol, cancel_during_read=False, prefetch=False, scope_cancel=False,
                       headers_pending=False, error_body=False):
        closed = []
        waiting = asyncio.Event()
        event = {
            "openai_chat": chat({"tool_calls": [{"index": 0, "id": "c", "type": "function",
                "function": {"name": "lookup", "arguments": "{}"}}]}),
            "anthropic": {"type": "content_block_start", "index": 0,
                "content_block": {"type": "tool_use", "id": "c", "name": "lookup", "input": {"x": 1}}},
            "openai_responses": {"type": "response.output_text.delta", "delta": "hello"},
            "gemini": {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]},
        }[protocol]
        class Response:
            status_code = 403 if error_body else 200
            headers = {"content-type": "text/event-stream"}

            async def aiter_lines(self):
                if not prefetch:
                    yield "data: " + json.dumps(event)
                    yield ""
                waiting.set()
                await asyncio.Event().wait()

            async def aclose(self):
                await asyncio.sleep(0)
                closed.append("response")

            async def aread(self):
                waiting.set()
                await asyncio.Event().wait()
        class Client:
            def build_request(self, *a, **kw):
                return object()

            async def send(self, *a, **kw):
                if headers_pending:
                    waiting.set()
                    await asyncio.Event().wait()
                return Response()

            async def aclose(self):
                await asyncio.sleep(0)
                closed.append("client")
        with TestClient(app) as client:
            key, channel_id = self.configure(client, protocol, "http://fixture.invalid")
            state = gateway._breaker_default()
            state["streak"] = 1
            gateway._BREAKER[channel_id] = state.copy()

            async def run():
                request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions",
                    "query_string": b"", "headers": [(b"authorization", ("Bearer " + key).encode())],
                    "client": ("127.0.0.1", 0), "scheme": "http", "server": ("127.0.0.1", 8000)})
                with mock.patch.object(gateway, "_http_client", return_value=Client()):
                    async def consume():
                        response = await gateway._chat(request, {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True})
                        if not cancel_during_read:
                            await anext(response.body_iterator)
                            await response.body_iterator.aclose()
                        else:
                            async for _ in response.body_iterator:
                                pass
                    if scope_cancel:
                        async with anyio.create_task_group() as group:
                            group.start_soon(consume)
                            await waiting.wait()
                            group.cancel_scope.cancel()
                        return
                    task = asyncio.create_task(consume())
                    if cancel_during_read:
                        await asyncio.wait_for(waiting.wait(), 2)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        await task
            asyncio.run(run())
            self.assertEqual(set(closed), {"client"} if headers_pending else {"response", "client"})
            self.assertEqual(gateway._BREAKER[channel_id], state)
            logs = database.query("SELECT status,status_code FROM request_logs")
            self.assertEqual([(row["status"], row["status_code"]) for row in logs], [("error", 499)])

    def test_chat_close_is_cancelled_without_breaker_recovery(self):
        self._cancel_stream("openai_chat")

    def test_anthropic_close_is_cancelled_without_breaker_recovery(self):
        self._cancel_stream("anthropic")

    def test_chat_cancel_pending_read_closes_resources(self):
        self._cancel_stream("openai_chat", cancel_during_read=True)

    def test_anthropic_cancel_pending_read_closes_resources(self):
        self._cancel_stream("anthropic", cancel_during_read=True)

    def test_chat_cancel_prefetch_closes_resources(self):
        self._cancel_stream("openai_chat", cancel_during_read=True, prefetch=True)

    def test_anthropic_cancel_prefetch_closes_resources(self):
        self._cancel_stream("anthropic", cancel_during_read=True, prefetch=True)

    def test_chat_server_cancel_scope_closes_resources(self):
        self._cancel_stream("openai_chat", cancel_during_read=True, scope_cancel=True)

    def test_anthropic_server_cancel_scope_closes_resources(self):
        self._cancel_stream("anthropic", cancel_during_read=True, scope_cancel=True)

    def test_chat_cancel_before_headers_closes_client(self):
        self._cancel_stream("openai_chat", cancel_during_read=True, scope_cancel=True, headers_pending=True)

    def test_anthropic_cancel_before_headers_closes_client(self):
        self._cancel_stream("anthropic", cancel_during_read=True, scope_cancel=True, headers_pending=True)

    def test_chat_cancel_during_error_body_closes_resources(self):
        self._cancel_stream("openai_chat", cancel_during_read=True, scope_cancel=True, error_body=True)

    def test_anthropic_cancel_during_error_body_closes_resources(self):
        self._cancel_stream("anthropic", cancel_during_read=True, scope_cancel=True, error_body=True)

    def test_responses_close_is_cancelled_without_breaker_recovery(self):
        self._cancel_stream("openai_responses")

    def test_gemini_close_is_cancelled_without_breaker_recovery(self):
        self._cancel_stream("gemini")

    def test_responses_cancel_pending_read_closes_resources(self):
        self._cancel_stream("openai_responses", cancel_during_read=True, scope_cancel=True)

    def test_gemini_cancel_pending_read_closes_resources(self):
        self._cancel_stream("gemini", cancel_during_read=True, scope_cancel=True)

    def test_responses_cancel_prefetch_closes_resources(self):
        self._cancel_stream("openai_responses", cancel_during_read=True, scope_cancel=True, prefetch=True)

    def test_gemini_cancel_prefetch_closes_resources(self):
        self._cancel_stream("gemini", cancel_during_read=True, scope_cancel=True, prefetch=True)

    def test_responses_cancel_before_headers_closes_client(self):
        self._cancel_stream("openai_responses", cancel_during_read=True, scope_cancel=True, headers_pending=True)

    def test_gemini_cancel_before_headers_closes_client(self):
        self._cancel_stream("gemini", cancel_during_read=True, scope_cancel=True, headers_pending=True)

    def test_responses_cancel_during_error_body_closes_resources(self):
        self._cancel_stream("openai_responses", cancel_during_read=True, scope_cancel=True, error_body=True)

    def test_gemini_cancel_during_error_body_closes_resources(self):
        self._cancel_stream("gemini", cancel_during_read=True, scope_cancel=True, error_body=True)


if __name__ == "__main__":
    unittest.main()
