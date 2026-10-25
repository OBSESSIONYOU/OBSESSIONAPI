import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import database, gateway
from app.main import app


def chunk_payload(chunk):
    return json.loads(chunk.decode().split("data:", 1)[1])


class ProtocolStreamEventsTests(unittest.TestCase):
    def test_responses_completion_is_a_successful_terminal_event(self):
        state = {}
        gateway._protocol_event({"type": "response.output_text.delta", "delta": "OK"}, "openai_responses", state, "m")
        chunk, has_output, is_error, detail = gateway._protocol_event({
            "type": "response.completed", "response": {"id": "r", "usage": {"input_tokens": 3, "output_tokens": 2}},
        }, "openai_responses", state, "m")
        self.assertFalse(is_error, detail)
        self.assertFalse(has_output)
        self.assertTrue(state["completed"])
        self.assertEqual(chunk_payload(chunk)["choices"][0]["finish_reason"], "stop")
        self.assertEqual(state["usage"]["completion_tokens"], 2)

    def test_gemini_finish_only_and_usage_only_frames_are_preserved(self):
        state = {}
        gateway._protocol_event({"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}, "gemini", state, "m")
        chunk, has_output, is_error, detail = gateway._protocol_event({
            "candidates": [{"finishReason": "STOP"}],
        }, "gemini", state, "m")
        self.assertFalse(is_error, detail)
        self.assertFalse(has_output)
        self.assertTrue(state.get("completed"))
        self.assertEqual(chunk_payload(chunk)["choices"][0]["finish_reason"], "stop")
        usage_chunk, _, _, _ = gateway._protocol_event({"usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 4}}, "gemini", state, "m")
        self.assertEqual(state["usage"], {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11})
        self.assertEqual(chunk_payload(usage_chunk)["usage"]["total_tokens"], 11)

    def test_gemini_text_in_terminal_frame_is_not_an_error(self):
        chunk, output, error, detail = gateway._protocol_event({"candidates": [{
            "content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP",
        }]}, "gemini", {}, "m")
        self.assertTrue(output)
        self.assertFalse(error, detail)
        self.assertEqual(chunk_payload(chunk)["choices"][0]["delta"]["content"], "OK")

    def test_parallel_responses_calls_keep_independent_argument_indices(self):
        state = {}
        for index in range(2):
            chunk, _, error, _ = gateway._protocol_event({"type": "response.output_item.added", "output_index": index,
                "item": {"id": f"item_{index}", "type": "function_call", "call_id": f"call_{index}", "name": f"lookup_{index}"},
            }, "openai_responses", state, "m")
            self.assertFalse(error)
            self.assertEqual(chunk_payload(chunk)["choices"][0]["delta"]["tool_calls"][0]["index"], index)
        for index in (1, 0):
            chunk, _, error, _ = gateway._protocol_event({"type": "response.function_call_arguments.delta",
                "item_id": f"item_{index}", "output_index": index, "delta": '{"x":1}',
            }, "openai_responses", state, "m")
            self.assertFalse(error)
            self.assertEqual(chunk_payload(chunk)["choices"][0]["delta"]["tool_calls"][0]["index"], index)

    def test_nested_responses_failure_keeps_its_message(self):
        _, _, error, detail = gateway._protocol_event({"type": "response.failed",
            "response": {"error": {"message": "fixture failure"}},
        }, "openai_responses", {}, "m")
        self.assertTrue(error)
        self.assertEqual(detail, "fixture failure")

    def test_gemini_tools_in_separate_frames_get_distinct_stream_indices(self):
        state = {}
        for index in range(2):
            chunk, _, error, detail = gateway._protocol_event({"candidates": [{"content": {"parts": [
                {"functionCall": {"name": f"lookup_{index}", "args": {"x": index}}},
            ]}}]}, "gemini", state, "m")
            self.assertFalse(error, detail)
            call = chunk_payload(chunk)["choices"][0]["delta"]["tool_calls"][0]
            self.assertEqual(call["index"], index)
            self.assertEqual(call["id"], f"gemini_call_{index}")
        chunk, _, error, _ = gateway._protocol_event({"candidates": [{"finishReason": "STOP"}]}, "gemini", state, "m")
        self.assertFalse(error)
        self.assertEqual(chunk_payload(chunk)["choices"][0]["finish_reason"], "tool_calls")

    def test_closing_relay_releases_resources_without_yielding_during_close(self):
        class Closable:
            closed = False

            async def aclose(self):
                self.closed = True

        async def run():
            async def lines():
                yield 'data: {"type":"response.completed","response":{"id":"r"}}'

            client, response = Closable(), Closable()
            prefetched = [{"type": "response.output_text.delta", "delta": "OK"}]
            relay = gateway._protocol_stream_relay((client, response, lines(), prefetched),
                {"name": "fixture", "api_key": "fixture"}, "openai_responses", "m", lambda *a, **k: None, 1)
            await anext(relay)
            await relay.aclose()
            self.assertTrue(client.closed)
            self.assertTrue(response.closed)

        asyncio.run(run())

    def test_closing_after_native_terminal_records_its_outcome_once(self):
        for protocol in ("openai_responses", "gemini", "access_gemini"):
            for terminal in ("success", "incomplete", "error"):
                with self.subTest(protocol=protocol, terminal=terminal):
                    records, closed = [], []
                    class Closable:
                        async def aclose(self):
                            await asyncio.sleep(0)
                            closed.append(True)

                    if protocol == "openai_responses":
                        initial = {"type": "response.output_text.delta", "delta": "OK"}
                        final = {"type": "response.completed", "response": {}}
                        if terminal == "incomplete":
                            final = {"type": "response.incomplete", "response": {
                                "incomplete_details": {"reason": "max_output_tokens"}}}
                    else:
                        initial = {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}
                        final = {"candidates": [{"finishReason": "MAX_TOKENS" if terminal == "incomplete" else "STOP"}]}
                        if protocol == "access_gemini":
                            initial, final = {"response": initial}, {"response": final}
                    if terminal == "error":
                        final = {"error": {"message": "fixture failure"}}

                    async def run():
                        async def lines():
                            yield "data: " + json.dumps(final)
                            await asyncio.Event().wait()

                        relay = gateway._protocol_stream_relay((Closable(), Closable(), lines(), [initial]),
                            {"name": "fixture", "api_key": "fixture"}, protocol, "m",
                            lambda *a, **kw: records.append((a, kw)), 1)
                        await anext(relay)
                        await anext(relay)
                        await relay.aclose()

                    asyncio.run(run())
                    self.assertEqual(len(closed), 2)
                    self.assertEqual(len(records), 1)
                    expected = ("success", 200) if terminal == "success" else ("error", 502 if terminal == "error" else 200)
                    self.assertEqual(records[0][0][3:5], expected)

    def test_cancelling_stream_prefetch_closes_owned_client_and_response(self):
        class Response:
            status_code = 200
            headers = {}
            closed = False

            async def aclose(self):
                self.closed = True

            async def aiter_lines(self):
                raise asyncio.CancelledError()
                yield "unreachable"

        class Client:
            closed = False

            def build_request(self, *args, **kwargs):
                return object()

            async def send(self, *args, **kwargs):
                return response

            async def aclose(self):
                self.closed = True

        response, client = Response(), Client()

        async def run():
            with mock.patch.object(gateway, "_protocol_request", return_value=("http://fixture", {}, {})), \
                    mock.patch.object(gateway, "_http_client", return_value=client):
                with self.assertRaises(asyncio.CancelledError):
                    await gateway._begin_protocol_stream({"name": "fixture", "api_key": "fixture"},
                        "openai_responses", {"model": "m"}, "m")
            self.assertTrue(client.closed)
            self.assertTrue(response.closed)

        asyncio.run(run())


class ProtocolStreamIntegrationTests(unittest.TestCase):
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

    def call_fixture(self, protocol, events):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in events:
                    self.wfile.write(("data: " + (event if isinstance(event, str) else json.dumps(event)) + "\n\n").encode())

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with TestClient(app) as client:
                setup = client.post("/api/setup", json={"username": "owner", "password": "fixture-password"}).json()
                admin = {"Authorization": "Bearer " + setup["token"]}
                channel = client.post("/api/channels", headers=admin, json={"name": "fixture", "type": protocol,
                    "base_url": f"http://127.0.0.1:{server.server_port}", "api_key": "fixture", "models": "m"})
                self.assertEqual(channel.status_code, 200, channel.text)
                key = client.post("/api/keys", headers=admin, json={"name": "client", "user_id": setup["user"]["id"]}).json()["key"]
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key},
                    json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True})
            logs = database.query("SELECT status,status_code,prompt_tokens,completion_tokens FROM request_logs")
            return response, logs
        finally:
            server.shutdown()
            worker.join()
            server.server_close()

    def test_responses_normal_completion_does_not_emit_error_or_trip_breaker(self):
        response, logs = self.call_fixture("openai_responses", [
            {"type": "response.output_text.delta", "delta": "OK"},
            {"type": "response.completed", "response": {"id": "r", "usage": {"input_tokens": 3, "output_tokens": 2}}},
        ])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('"error"', response.text)
        self.assertIn('"finish_reason": "stop"', response.text)
        self.assertEqual([(r["status"], r["completion_tokens"]) for r in logs], [("success", 2)])
        self.assertEqual(gateway._BREAKER[1]["streak"], 0)

    def test_gemini_finish_only_with_later_usage_succeeds(self):
        response, logs = self.call_fixture("gemini", [
            {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]},
            {"candidates": [{"finishReason": "STOP"}]},
            {"usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 4}},
        ])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('"error"', response.text)
        self.assertIn('"finish_reason": "stop"', response.text)
        self.assertEqual([(r["status"], r["prompt_tokens"], r["completion_tokens"]) for r in logs], [("success", 7, 4)])

    def test_gemini_single_terminal_text_frame_succeeds(self):
        response, logs = self.call_fixture("gemini", [{"candidates": [{
            "content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP",
        }]}])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('"error"', response.text)
        self.assertEqual(logs[0]["status"], "success")

    def test_truncated_responses_stream_remains_an_error(self):
        response, logs = self.call_fixture("openai_responses", [{"type": "response.output_text.delta", "delta": "partial"}])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"error"', response.text)
        self.assertEqual(logs[0]["status"], "error")

    def test_empty_completed_responses_stream_is_rejected_before_committing(self):
        response, logs = self.call_fixture("openai_responses", [{"type": "response.completed", "response": {"id": "r"}}])
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(logs[0]["status"], "error")

    def test_responses_failure_after_partial_text_is_not_success(self):
        response, logs = self.call_fixture("openai_responses", [
            {"type": "response.output_text.delta", "delta": "partial"},
            {"type": "response.failed", "response": {"error": {"message": "fixture failure"}}},
        ])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("failure", response.text)
        self.assertEqual(logs[0]["status"], "error")

    def test_malformed_sse_after_partial_text_emits_error(self):
        response, logs = self.call_fixture("openai_responses", [
            {"type": "response.output_text.delta", "delta": "partial"}, "not-json",
        ])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"error"', response.text)
        self.assertEqual(logs[0]["status"], "error")

    def test_gemini_empty_finish_is_rejected(self):
        response, logs = self.call_fixture("gemini", [{"candidates": [{"finishReason": "STOP"}]}])
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(logs[0]["status"], "error")

    def test_done_marker_without_native_terminal_event_is_truncated(self):
        response, logs = self.call_fixture("openai_responses", [
            {"type": "response.output_text.delta", "delta": "partial"}, "[DONE]",
        ])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"error"', response.text)
        self.assertEqual(logs[0]["status"], "error")

    def test_responses_budget_truncation_keeps_length_without_breaker_failure(self):
        response, logs = self.call_fixture("openai_responses", [
            {"type": "response.output_text.delta", "delta": "partial"},
            {"type": "response.incomplete", "response": {"id": "r", "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"}, "usage": {"input_tokens": 3, "output_tokens": 32}}},
        ])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('"error"', response.text)
        self.assertIn('"finish_reason": "length"', response.text)
        self.assertEqual((logs[0]["status"], logs[0]["status_code"]), ("error", 200))
        self.assertEqual(gateway._BREAKER.get(1, {}).get("streak", 0), 0)

    def test_responses_empty_budget_truncation_is_not_a_protocol_failure(self):
        response, logs = self.call_fixture("openai_responses", [
            {"type": "response.incomplete", "response": {"id": "r", "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"}}},
        ])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"finish_reason": "length"', response.text)
        self.assertNotIn('"error"', response.text)
        self.assertEqual((logs[0]["status"], logs[0]["status_code"]), ("error", 200))


if __name__ == "__main__":
    unittest.main()
