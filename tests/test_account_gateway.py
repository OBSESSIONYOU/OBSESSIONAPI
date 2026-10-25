"""Account lanes exercised through the real gateway with an offline HTTP transport."""
import asyncio
import json
import unittest
from contextlib import ExitStack
from unittest import mock

import httpx
import anyio
from starlette.requests import Request

from app import account_transport, gateway, opencode_free


def sse(*frames):
    return b"".join(("data: " + (frame if isinstance(frame, str) else json.dumps(frame)) + "\n\n").encode() for frame in frames)


CHAT = sse({"id": "c", "choices": [{"delta": {"content": "OK"}, "finish_reason": None}]},
           {"id": "c", "choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}, "[DONE]")
ANTHROPIC = sse({"type": "message_start", "message": {"id": "msg", "usage": {"input_tokens": 2}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
                {"type": "message_stop"})
GEMINI = sse({"response": {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}},
             {"response": {"candidates": [{"finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1}}})


class AccountGatewayTests(unittest.TestCase):
    def run_lane(self, kind, *, stream=False, extra=None, content=None, status=200, credential_extra=None, executed=None):
        calls, clients = [], []
        credential = {"access_token": "fixture-access", "refresh_token": "fixture-refresh", "project": "fixture-project"}
        if kind == "access_cline":
            credential["access_token"] = "workos:fixture-access"
        credential.update(credential_extra or {})
        ch = {"id": 101, "name": kind, "type": kind, "base_url": {
            "lobsterai": "https://lobsterai-server.youdao.com", "access_gemini": "https://daily-cloudcode-pa.googleapis.com",
            "access_minimax": "https://agent.minimax.cn", "access_cline": "https://api.cline.bot",
            "opencode_free": "https://opencode.ai"}[kind], "api_key": json.dumps(credential),
            "models": "m", "model_mapping": "", "tool_policy": "preserve", "extra_headers": "{}", "forward_headers": "[]",
            "user_agent": "", "rpm_limit": 0, "max_concurrency": 0}
        wire = content if content is not None else ANTHROPIC if kind == "access_minimax" else GEMINI if kind == "access_gemini" else CHAT

        def handle(req):
            calls.append({"url": str(req.url), "headers": dict(req.headers), "body": json.loads(req.content)})
            return httpx.Response(status, headers={"content-type": "text/event-stream"}, content=wire)

        def client_factory(*args, **kwargs):
            client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            clients.append(client)
            return client

        async def run():
            request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": [], "client": ("127.0.0.1", 1)})
            body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": stream, **(extra or {})}
            with ExitStack() as stack:
                for name, value in (("_authorize_request", ({}, None)), ("_resolve_candidates", ([ch], None)),
                                    ("_cache_lookup", None), ("_pricing", (0, 0))):
                    stack.enter_context(mock.patch.object(gateway, name, return_value=value))
                for name in ("_record", "_breaker_mark", "execute"):
                    patched = stack.enter_context(mock.patch.object(gateway, name))
                    if name == "execute":
                        execute_mock = patched
                stack.enter_context(mock.patch.object(gateway, "_http_client", side_effect=client_factory))
                response = await gateway._chat_inner(request, body)
                if hasattr(response, "body_iterator"):
                    data = b"".join([part async for part in response.body_iterator])
                else:
                    data = response.body
                if executed is not None:
                    executed.extend(execute_mock.call_args_list)
            closed = [client.is_closed for client in clients]
            for client in clients:
                await client.aclose()
            return response, data, closed

        result = asyncio.run(run())
        return (*result, calls)

    def test_account_lanes_use_native_path_and_access_token_only(self):
        for kind, path in (("access_gemini", "/v1internal:streamGenerateContent?alt=sse"),
                           ("access_minimax", "/mavis/api/v1/llm/v1/messages"),
                           ("access_cline", "/api/v1/chat/completions")):
            with self.subTest(kind=kind):
                response, data, closed, calls = self.run_lane(kind)
                self.assertEqual(response.status_code, 200, data)
                self.assertEqual(len(calls), 1)
                self.assertTrue(calls[0]["url"].endswith(path), calls)
                expected = "Bearer workos:fixture-access" if kind == "access_cline" else "Bearer fixture-access"
                self.assertEqual(calls[0]["headers"]["authorization"], expected)
                self.assertNotIn("fixture-refresh", json.dumps(calls))
                self.assertEqual(json.loads(data)["choices"][0]["message"]["content"], "OK")
                self.assertTrue(all(closed))

    def test_lobster_stream_posts_once_and_closes_client(self):
        response, data, closed, calls = self.run_lane("lobsterai", stream=True)
        self.assertEqual(response.status_code, 200, data)
        self.assertEqual(len(calls), 1, calls)
        self.assertTrue(calls[0]["url"].endswith("/api/proxy/v1/chat/completions"))
        self.assertTrue(calls[0]["body"]["stream"])
        self.assertEqual(closed, [True])

    def test_cloudcode_envelope_and_tools_are_preserved(self):
        tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
        response, data, _, calls = self.run_lane("access_gemini", extra={"tools": tools, "tool_choice": "required", "max_tokens": 42})
        self.assertEqual(response.status_code, 200, data)
        request = calls[0]["body"]["request"]
        self.assertEqual(request["generationConfig"]["maxOutputTokens"], 42)
        self.assertEqual(request["tools"][0]["functionDeclarations"][0]["name"], "lookup")
        self.assertEqual(request["toolConfig"]["functionCallingConfig"]["mode"], "ANY")

    def test_minimax_tool_history_and_choice_are_preserved(self):
        body = {"messages": [{"role": "assistant", "content": None, "tool_calls": [
            {"type": "function", "id": "call_1", "function": {"name": "lookup", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"}],
            "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}], "tool_choice": "required"}
        response, data, _, calls = self.run_lane("access_minimax", extra=body)
        self.assertEqual(response.status_code, 200, data)
        sent = calls[0]["body"]
        self.assertEqual(sent["tools"][0]["name"], "lookup")
        self.assertEqual(sent["messages"][0]["content"][0]["type"], "tool_use")
        self.assertEqual(sent["messages"][1]["content"][0]["type"], "tool_result")
        self.assertEqual(sent["tool_choice"]["type"], "any")

    def test_unmapped_account_parameters_fail_before_network(self):
        for kind in ("access_gemini", "access_minimax"):
            for extra in ({"response_format": {"type": "json_object"}}, {"tool_choice": "unsupported"},
                          {"messages": [{"role": "function", "name": "legacy", "content": "result"}]},
                          {"messages": [{"role": "assistant", "content": None, "function_call": {"name": "lookup", "arguments": "{}"}}]}):
                with self.subTest(kind=kind, extra=extra):
                    response, data, _, calls = self.run_lane(kind, extra=extra)
                    self.assertEqual(response.status_code, 400, data)
                    self.assertEqual(calls, [])

    def test_account_text_blocks_are_mapped_without_silently_dropping_text(self):
        for kind in ("access_gemini", "access_minimax"):
            with self.subTest(kind=kind):
                response, data, _, calls = self.run_lane(kind, extra={"messages": [
                    {"role": "system", "content": [{"type": "text", "text": "system"}]},
                    {"role": "user", "content": [{"type": "output_text", "text": "hello"}, {"type": "text", "text": " world"}]}]})
                self.assertEqual(response.status_code, 200, data)
                native = calls[0]["body"]
                if kind == "access_gemini":
                    self.assertEqual(native["request"]["systemInstruction"]["parts"], [{"text": "system"}])
                    self.assertEqual(native["request"]["contents"][0]["parts"], [{"text": "hello world"}])
                else:
                    self.assertEqual(native["messages"][0]["content"], [{"type": "text", "text": "hello world"}])

    def test_opencode_error_and_unterminated_partial_are_not_success(self):
        for content in (sse({"error": {"message": "quota exhausted"}}),
                        sse({"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]})):
            with self.subTest(content=content):
                response, data, _, _ = self.run_lane("opencode_free", content=content)
                self.assertEqual(response.status_code, 502, data)

    def test_openai_account_legacy_function_call_is_preserved(self):
        content = sse({"choices": [{"delta": {"function_call": {"name": "lookup", "arguments": '{"city":'}}, "finish_reason": None}]},
                      {"choices": [{"delta": {"function_call": {"arguments": '"Beijing"}'}}, "finish_reason": "function_call"}]}, "[DONE]")
        for kind in ("access_cline", "lobsterai", "opencode_free"):
            with self.subTest(kind=kind):
                response, data, _, _ = self.run_lane(kind, content=content)
                self.assertEqual(response.status_code, 200, data)
                choice = json.loads(data)["choices"][0]
                self.assertEqual(choice["message"]["function_call"], {"name": "lookup", "arguments": '{"city":"Beijing"}'})
                self.assertEqual(choice["finish_reason"], "function_call")

    def test_gemini_and_minimax_streaming_use_the_same_native_routes(self):
        for kind in ("access_gemini", "access_minimax", "access_cline"):
            with self.subTest(kind=kind):
                response, data, closed, calls = self.run_lane(kind, stream=True)
                self.assertEqual(response.status_code, 200, data)
                self.assertIn(b'"content": "OK"', data)
                self.assertNotIn(b'"error"', data)
                self.assertIn(b"data: [DONE]", data)
                self.assertTrue(all(closed))
                self.assertEqual(len(calls), 1)

    def test_malformed_native_sse_fails_and_closes_connections(self):
        for kind in ("access_gemini", "access_minimax", "access_cline", "lobsterai"):
            with self.subTest(kind=kind):
                response, data, closed, _ = self.run_lane(kind, stream=True, content=b"data: not-json\n\n")
                self.assertEqual(response.status_code, 502, data)
                self.assertTrue(all(closed))

    def test_account_nonstream_length_is_reported_incomplete(self):
        content = sse({"choices": [{"delta": {"content": "partial"}, "finish_reason": "length"}]})
        for kind in ("access_cline", "lobsterai", "opencode_free"):
            with self.subTest(kind=kind):
                response, data, _, _ = self.run_lane(kind, content=content)
                self.assertEqual(response.status_code, 200, data)
                self.assertEqual(response.headers.get("x-obsessionapi-completion-state"), "incomplete")
                self.assertEqual(json.loads(data)["choices"][0]["finish_reason"], "length")

    def test_empty_account_stream_never_replays_as_nonstream(self):
        for kind in ("access_cline", "lobsterai", "access_minimax", "access_gemini"):
            with self.subTest(kind=kind):
                response, data, closed, calls = self.run_lane(kind, stream=True, content=b"")
                self.assertEqual(response.status_code, 502, data)
                self.assertEqual(len(calls), 1, calls)
                self.assertTrue(all(closed))

    def test_refresh_persists_full_credential_but_only_sends_access_token(self):
        refreshed = {"access_token": "fixture-new", "refresh_token": "fixture-new-refresh", "expires_at": 99999999999}
        executed = []
        with mock.patch.object(account_transport.lobsterai, "is_expired", return_value=True), \
             mock.patch.object(account_transport.lobsterai, "refresh", new=mock.AsyncMock(return_value=refreshed)):
            response, data, _, calls = self.run_lane("lobsterai", executed=executed)
        self.assertEqual(response.status_code, 200, data)
        self.assertEqual(calls[0]["headers"]["authorization"], "Bearer fixture-new")
        self.assertNotIn("fixture-new-refresh", json.dumps(calls))
        updates = [call for call in executed if "UPDATE channels SET api_key=" in call.args[0]]
        self.assertEqual(len(updates), 1)
        self.assertEqual(json.loads(updates[0].args[1][0]), refreshed)

    def test_native_tool_responses_are_preserved(self):
        native = {
            "access_gemini": sse({"response": {"candidates": [{"content": {"parts": [{"functionCall": {"name": "lookup", "args": {"city": "Beijing"}}}]}, "finishReason": "STOP"}]}}),
            "access_minimax": sse({"type": "message_start", "message": {"id": "msg", "usage": {}}},
                                  {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {}}},
                                  {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"city":"Beijing"}'}},
                                  {"type": "content_block_stop", "index": 0},
                                  {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}, {"type": "message_stop"}),
        }
        for kind, content in native.items():
            with self.subTest(kind=kind):
                response, data, _, _ = self.run_lane(kind, content=content)
                self.assertEqual(response.status_code, 200, data)
                choice = json.loads(data)["choices"][0]
                self.assertEqual(choice["finish_reason"], "tool_calls")
                function = choice["message"]["tool_calls"][0]["function"]
                self.assertEqual(function["name"], "lookup")
                self.assertEqual(json.loads(function["arguments"]), {"city": "Beijing"})

    def test_native_nonstream_errors_and_unterminated_frames_fail(self):
        for kind in ("access_gemini", "access_minimax"):
            for content in (sse({"error": {"message": "upstream failure"}}), b'data: {"ignored":true}'):
                with self.subTest(kind=kind, content=content):
                    response, data, closed, _ = self.run_lane(kind, content=content)
                    self.assertEqual(response.status_code, 502, data)
                    self.assertTrue(all(closed))

    def test_nonstream_cancellation_closes_native_response(self):
        async def run(kind):
            closed = []
            scope = anyio.CancelScope()

            class CancelledStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    scope.cancel()
                    await anyio.sleep(0)
                    yield b""

                async def aclose(self):
                    await anyio.sleep(0)
                    closed.append(True)

            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=CancelledStream()))) as client:
                channel = await account_transport.prepare_channel({"type": kind, "api_key": json.dumps({"access_token": "fixture"})})
                with scope:
                    await account_transport.complete_nonstream(client, channel, {"model": "m", "messages": [{"role": "user", "content": "hi"}]}, "m")
            self.assertEqual(closed, [True])

        for kind in ("access_gemini", "access_minimax", "access_cline", "lobsterai"):
            with self.subTest(kind=kind):
                anyio.run(run, kind)


if __name__ == "__main__":
    unittest.main()
