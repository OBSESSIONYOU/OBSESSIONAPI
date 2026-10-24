"""Regression coverage for protocol streams consumed by Responses/Gemini clients."""
import asyncio
import json
import unittest

from starlette.responses import StreamingResponse

from app.gateway import _as_native_response, _native_stream_lines


def chunk(delta=None, finish=None, usage=None):
    value = {"id": "reply-1", "model": "public-model", "choices": [
        {"index": 0, "delta": delta or {}, "finish_reason": finish}
    ]}
    if usage is not None:
        value["usage"] = usage
    return value


def events(raw):
    return [json.loads(line[5:].strip()) for line in raw.decode().splitlines()
            if line.startswith("data:")]


def translate(protocol, chunks):
    state = {}
    return events(b"".join(frame for value in chunks
                           for frame in _native_stream_lines(protocol, value, state)))


class NativeStreamingTests(unittest.TestCase):
    def test_responses_text_has_ordered_item_and_content_lifecycle(self):
        result = translate("openai_responses", [chunk({"content": "hello"}), chunk(finish="stop")])
        kinds = [event["type"] for event in result]
        self.assertEqual(kinds, ["response.created", "response.output_item.added",
            "response.content_part.added", "response.output_text.delta", "response.output_text.done",
            "response.content_part.done", "response.output_item.done", "response.completed"])
        self.assertEqual(result[-1]["response"]["output"][0]["content"][0]["text"], "hello")
        self.assertEqual(result[3]["item_id"], result[1]["item"]["id"])
        self.assertEqual([item["sequence_number"] for item in result], list(range(len(result))))

    def test_responses_parallel_tools_keep_identity_and_arguments(self):
        result = translate("openai_responses", [
            chunk({"tool_calls": [
                {"index": 0, "id": "call-a", "type": "function", "function": {"name": "first", "arguments": '{"a":'}},
                {"index": 1, "id": "call-b", "type": "function", "function": {"name": "second", "arguments": '{"b":'}}]}),
            chunk({"tool_calls": [
                {"index": 1, "function": {"arguments": "2}"}},
                {"index": 0, "function": {"arguments": "1}"}}]}), chunk(finish="tool_calls")])
        items = result[-1]["response"]["output"]
        self.assertEqual([(item["call_id"], item["arguments"]) for item in items],
                         [("call-a", '{"a":1}'), ("call-b", '{"b":2}')])
        self.assertEqual(len([event for event in result if event["type"] == "response.output_item.added"]), 2)
        self.assertEqual(len([event for event in result if event["type"] == "response.function_call_arguments.done"]), 2)

    def test_responses_length_reports_incomplete(self):
        result = translate("openai_responses", [chunk({"content": "partial"}), chunk(finish="length")])
        self.assertEqual(result[-1]["type"], "response.incomplete")
        self.assertEqual(result[-1]["response"]["incomplete_details"]["reason"], "max_output_tokens")
        self.assertNotIn("response.completed", [event["type"] for event in result])

    def test_errors_are_terminal_in_both_protocols(self):
        for protocol in ("openai_responses", "gemini"):
            with self.subTest(protocol=protocol):
                result = translate(protocol, [chunk({"content": "partial"}),
                    {"error": {"type": "upstream_error", "message": "upstream broke"}}, chunk(finish="stop")])
                self.assertIn("upstream broke", json.dumps(result))
                self.assertNotEqual(result[-1].get("type"), "response.completed")
                self.assertIn("error", result[-1])

    def test_gemini_fragmented_tools_and_usage_survive(self):
        result = translate("gemini", [
            chunk({"tool_calls": [{"index": 0, "id": "call-a", "function": {"name": "lookup", "arguments": '{"q":'}}]}),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"x"}'}}]}),
            chunk(finish="tool_calls", usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})])
        self.assertEqual(result[-1]["candidates"][0]["content"]["parts"],
                         [{"functionCall": {"id": "call-a", "name": "lookup", "args": {"q": "x"}}}])
        self.assertEqual(result[-1]["candidates"][0]["finishReason"], "STOP")
        self.assertEqual(result[-1]["usageMetadata"]["totalTokenCount"], 10)

    def test_gemini_finish_only_and_length_are_not_dropped(self):
        result = translate("gemini", [chunk({"content": "partial"}), chunk(finish="length")])
        self.assertEqual(result[-1]["candidates"][0]["finishReason"], "MAX_TOKENS")

    def test_invalid_tool_arguments_fail_instead_of_becoming_empty_object(self):
        for protocol in ("gemini", "openai_responses"):
            with self.subTest(protocol=protocol):
                result = translate(protocol, [chunk({"tool_calls": [{"index": 0, "id": "c",
                    "function": {"name": "lookup", "arguments": "not json"}}]}), chunk(finish="tool_calls")])
                self.assertIn("error", result[-1])

    def test_relay_buffers_split_utf8_frames_and_keeps_late_usage(self):
        async def run(protocol):
            raw = ("data: " + json.dumps(chunk({"content": "你好"}), ensure_ascii=False) + "\r\n\r\n"
                   + "data:" + json.dumps(chunk(finish="stop")) + "\n\n"
                   + "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 2,
                       "completion_tokens": 1, "total_tokens": 3}}) + "\n\n"
                   + "data: [DONE]\n\n").encode()
            async def source():
                for index in range(0, len(raw), 7):
                    yield raw[index:index + 7]
            response = await _as_native_response(StreamingResponse(source()), protocol, "rid")
            return events(b"".join([part async for part in response.body_iterator]))
        responses = asyncio.run(run("openai_responses"))
        self.assertEqual(responses[-1]["response"]["output"][0]["content"][0]["text"], "你好")
        self.assertEqual(responses[-1]["response"]["usage"]["total_tokens"], 3)
        gemini = asyncio.run(run("gemini"))
        self.assertEqual(gemini[-1]["usageMetadata"]["totalTokenCount"], 3)

    def test_relay_malformed_or_truncated_frames_fail_explicitly(self):
        async def run(protocol, raw):
            async def source():
                yield raw
            response = await _as_native_response(StreamingResponse(source()), protocol, "rid")
            return events(b"".join([part async for part in response.body_iterator]))
        for protocol in ("openai_responses", "gemini"):
            for raw in (b'data: {bad}\n\n', b'data: {"choices": []}'):
                with self.subTest(protocol=protocol, raw=raw):
                    result = asyncio.run(run(protocol, raw))
                    self.assertIn("error", result[-1])

    def test_closing_native_relay_closes_source_generator(self):
        async def run(protocol):
            closed = []
            async def source():
                try:
                    yield ("data: " + json.dumps(chunk({"content": "hello"})) + "\n\n").encode()
                    await asyncio.Event().wait()
                finally:
                    closed.append(True)
            response = await _as_native_response(StreamingResponse(source()), protocol, "rid")
            await anext(response.body_iterator)
            await response.body_iterator.aclose()
            self.assertEqual(closed, [True])
        for protocol in ("anthropic", "openai_responses", "gemini"):
            with self.subTest(protocol=protocol):
                asyncio.run(run(protocol))

    def test_cancelling_native_relay_closes_source_generator(self):
        async def run():
            closed = []
            waiting = asyncio.Event()
            async def source():
                try:
                    yield ("data: " + json.dumps(chunk({"content": "hello"})) + "\n\n").encode()
                    waiting.set()
                    await asyncio.Event().wait()
                finally:
                    closed.append(True)
            response = await _as_native_response(StreamingResponse(source()), "gemini", "rid")
            async def consume():
                async for _ in response.body_iterator:
                    pass
            task = asyncio.create_task(consume())
            await waiting.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(closed, [True])
        asyncio.run(run())

    def test_invalid_frame_after_finish_cannot_become_success(self):
        async def run(protocol):
            async def source():
                for value in (chunk({"content": "hello"}), chunk(finish="stop")):
                    yield ("data: " + json.dumps(value) + "\n\n").encode()
                yield b"data: invalid\n\ndata: [DONE]\n\n"
            response = await _as_native_response(StreamingResponse(source()), protocol, "rid")
            result = events(b"".join([part async for part in response.body_iterator]))
            self.assertIn("error", result[-1])
            self.assertNotIn("response.completed", [event.get("type") for event in result])
        for protocol in ("openai_responses", "gemini"):
            with self.subTest(protocol=protocol):
                asyncio.run(run(protocol))


if __name__ == "__main__":
    unittest.main()
