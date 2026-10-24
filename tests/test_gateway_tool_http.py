"""Native clients must complete a function-tool round trip through a real HTTP upstream."""
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


def events(response):
    return [json.loads(line[5:].strip()) for line in response.text.splitlines()
            if line.startswith("data:") and line[5:].strip() != "[DONE]"]


class GatewayToolHTTPTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        captured = self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                captured.append(body)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if any(item.get("role") == "tool" for item in body["messages"]):
                    deltas = [{"content": "tool result accepted"}]
                    finish = "stop"
                else:
                    deltas = [{"tool_calls": [
                        {"index": 0, "id": "call_a", "type": "function", "function": {"name": "lookup", "arguments": '{"q":'}},
                        {"index": 1, "id": "call_b", "type": "function", "function": {"name": "lookup", "arguments": '{"q":'}},
                    ]}, {"tool_calls": [
                        {"index": 1, "function": {"arguments": '"b"}'}},
                        {"index": 0, "function": {"arguments": '"a"}'}},
                    ]}]
                    finish = "tool_calls"
                for delta in deltas + [{}]:
                    chunk = {"id": "chat_fixture", "object": "chat.completion.chunk", "model": "fixture",
                             "choices": [{"index": 0, "delta": delta, "finish_reason": finish if not delta else None}]}
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n')
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *_):
                pass

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.upstream.shutdown)
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(database, "DATA_DIR", Path(temporary)))
        self.enterContext(mock.patch.object(database, "DB_PATH", Path(temporary) / "test.db"))
        gateway._reset_runtime_state()
        self.addCleanup(gateway._reset_runtime_state)
        self.client = self.enterContext(TestClient(app))
        setup = self.client.post("/api/setup", json={"username": "owner", "password": "a-long-password"}).json()
        admin = {"Authorization": "Bearer " + setup["token"]}
        channel = self.client.post("/api/channels", headers=admin, json={"name": "fixture", "type": "openai",
            "protocol": "openai_chat", "base_url": f"http://127.0.0.1:{self.upstream.server_port}",
            "api_key": "fixture", "models": "fixture"})
        self.assertEqual(channel.status_code, 200, channel.text)
        key = self.client.post("/api/keys", headers=admin,
                               json={"name": "fixture", "user_id": setup["user"]["id"]}).json()["key"]
        self.auth = {"Authorization": "Bearer " + key}

    def assert_roundtrip(self):
        self.assertEqual(len(self.requests), 2)
        messages = self.requests[-1]["messages"]
        tools = [item for item in messages if item.get("role") == "tool"]
        self.assertEqual([item["tool_call_id"] for item in tools], ["call_a", "call_b"])
        self.assertEqual(self.requests[0]["tools"][0]["function"]["name"], "lookup")
        logs = database.query("SELECT status,prompt_tokens,completion_tokens,usage_source FROM request_logs")
        self.assertEqual(len(logs), 2)
        self.assertTrue(all(row["status"] == "success" and row["usage_source"] == "upstream" for row in logs))
        self.assertTrue(all(row["prompt_tokens"] == 5 and row["completion_tokens"] == 3 for row in logs))

    def test_responses_tool_loop(self):
        body = {"model": "fixture", "input": "use tools", "stream": True,
                "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]}
        first = self.client.post("/v1/responses", headers=self.auth, json=body)
        self.assertEqual(first.status_code, 200, first.text)
        final = events(first)[-1]
        self.assertEqual(final["type"], "response.completed", first.text)
        calls = final["response"]["output"]
        self.assertEqual([item["call_id"] for item in calls], ["call_a", "call_b"])
        body["input"] = calls + [{"type": "function_call_output", "call_id": item["call_id"], "output": "OK"} for item in calls]
        second = self.client.post("/v1/responses", headers=self.auth, json=body)
        final = events(second)[-1]
        self.assertEqual(final["type"], "response.completed", second.text)
        self.assertEqual(final["response"]["output"][0]["content"][0]["text"], "tool result accepted")
        self.assertEqual(final["response"]["usage"]["total_tokens"], 8)
        self.assert_roundtrip()

    def test_anthropic_tool_loop(self):
        body = {"model": "fixture", "messages": [{"role": "user", "content": "use tools"}], "max_tokens": 100,
                "stream": True, "tools": [{"name": "lookup", "input_schema": {"type": "object"}}]}
        first = self.client.post("/v1/messages", headers=self.auth, json=body)
        self.assertEqual(first.status_code, 200, first.text)
        calls = [item["content_block"] for item in events(first) if item.get("type") == "content_block_start"]
        self.assertEqual([item["id"] for item in calls], ["call_a", "call_b"])
        body["messages"] += [{"role": "assistant", "content": calls}, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": item["id"], "content": "OK"} for item in calls]}]
        second = self.client.post("/v1/messages", headers=self.auth, json=body)
        self.assertIn("tool result accepted", second.text)
        self.assertEqual(events(second)[-1]["type"], "message_stop")
        self.assert_roundtrip()

    def test_gemini_tool_loop(self):
        body = {"contents": [{"role": "user", "parts": [{"text": "use tools"}]}],
                "tools": [{"functionDeclarations": [{"name": "lookup", "parameters": {"type": "object"}}]}]}
        url = "/v1beta/models/fixture:streamGenerateContent"
        first = self.client.post(url, headers=self.auth, json=body)
        self.assertEqual(first.status_code, 200, first.text)
        calls = events(first)[-1]["candidates"][0]["content"]["parts"]
        self.assertEqual([item["functionCall"]["id"] for item in calls], ["call_a", "call_b"])
        body["contents"] += [{"role": "model", "parts": calls}, {"role": "user", "parts": [
            {"functionResponse": {"id": item["functionCall"]["id"], "name": "lookup", "response": {"output": "OK"}}}
            for item in calls]}]
        second = self.client.post(url, headers=self.auth, json=body)
        self.assertIn("tool result accepted", second.text)
        self.assertEqual(events(second)[-1]["usageMetadata"]["totalTokenCount"], 8)
        self.assert_roundtrip()


if __name__ == "__main__":
    unittest.main()
