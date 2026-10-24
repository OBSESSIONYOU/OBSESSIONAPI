"""Offline function-tool history conversion contracts."""
import json
import unittest

from app.protocols import (
    anthropic_to_openai_request, gemini_request_to_openai, gemini_to_openai,
    openai_to_gemini, openai_to_gemini_response, openai_to_responses,
    openai_to_responses_response, responses_to_openai, responses_to_chat,
)


class ToolRoundtripTests(unittest.TestCase):
    def test_responses_function_history_and_explicit_choice_reach_chat(self):
        result = responses_to_openai({"model": "m", "input": [
            {"role": "user", "content": "weather?"},
            {"type": "function_call", "call_id": "call_1", "name": "weather", "arguments": '{"city":"北京"}'},
            {"type": "function_call_output", "call_id": "call_1", "output": "sunny"},
        ], "tools": [{"type": "function", "name": "weather", "parameters": {"type": "object"}, "strict": True}],
            "tool_choice": {"type": "function", "name": "weather"}, "parallel_tool_calls": False})
        self.assertEqual(result["messages"][1]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(result["messages"][2], {"role": "tool", "tool_call_id": "call_1", "content": "sunny"})
        self.assertEqual(result["tools"][0]["function"]["name"], "weather")
        self.assertTrue(result["tools"][0]["function"]["strict"])
        self.assertEqual(result["tool_choice"], {"type": "function", "function": {"name": "weather"}})
        self.assertFalse(result["parallel_tool_calls"])

    def test_chat_history_becomes_responses_function_items(self):
        source = {"model": "m", "messages": [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "weather", "arguments": "{}"}},
                {"id": "call_2", "type": "function", "function": {"name": "clock", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
            {"role": "tool", "tool_call_id": "call_2", "content": "10:00"},
        ], "tools": [{"type": "function", "function": {"name": "weather", "strict": False}}],
            "tool_choice": {"type": "function", "function": {"name": "weather"}}, "parallel_tool_calls": True}
        result = openai_to_responses(source)
        self.assertEqual([item.get("type") for item in result["input"]][1:],
                         ["function_call", "function_call", "function_call_output", "function_call_output"])
        self.assertEqual(result["input"][3]["call_id"], "call_1")
        self.assertEqual(result["input"][3]["output"], "sunny")
        self.assertEqual(result["tool_choice"], {"type": "function", "name": "weather"})
        self.assertFalse(result["tools"][0]["strict"])
        self.assertTrue(result["parallel_tool_calls"])

    def test_responses_rejects_unsupported_state_and_builtin_tools(self):
        for extra in ({"previous_response_id": "resp_1"},
                      {"tools": [{"type": "web_search_preview"}]},
                      {"input": [{"type": "computer_call", "call_id": "c"}]}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                responses_to_openai({"model": "m", "input": "hi", **extra})

    def test_anthropic_tool_results_keep_ids_and_order(self):
        result = anthropic_to_openai_request({"model": "m", "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tool_1", "name": "weather", "input": {"city": "北京"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tool_1", "content": [{"type": "text", "text": "sunny"}]},
                {"type": "text", "text": "continue"},
            ]},
        ], "tool_choice": {"type": "tool", "name": "weather", "disable_parallel_tool_use": True}})
        self.assertEqual(result["messages"][1], {"role": "tool", "tool_call_id": "tool_1", "content": "sunny"})
        self.assertEqual(result["messages"][2], {"role": "user", "content": "continue"})
        self.assertEqual(result["tool_choice"], {"type": "function", "function": {"name": "weather"}})
        self.assertFalse(result["parallel_tool_calls"])

    def test_anthropic_required_choice_reaches_chat(self):
        self.assertEqual(anthropic_to_openai_request({"messages": [], "tool_choice": {"type": "any"}})["tool_choice"], "required")

    def test_gemini_parallel_calls_and_results_keep_correspondence(self):
        result = gemini_request_to_openai({"contents": [
            {"role": "model", "parts": [{"functionCall": {"name": "weather", "args": {}}},
                                          {"functionCall": {"name": "clock", "args": {}}}]},
            {"role": "user", "parts": [{"functionResponse": {"name": "weather", "response": {"text": "sunny"}}},
                                         {"functionResponse": {"name": "clock", "response": {"time": "10:00"}}}]},
        ], "tools": [{"functionDeclarations": [{"name": "weather", "parameters": {"type": "object"}}]}],
            "toolConfig": {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["weather"]}}}, "m")
        calls = result["messages"][0]["tool_calls"]
        self.assertNotEqual(calls[0]["id"], calls[1]["id"])
        self.assertEqual([item["tool_call_id"] for item in result["messages"][1:]], [call["id"] for call in calls])
        self.assertEqual(json.loads(result["messages"][1]["content"]), {"text": "sunny"})
        self.assertEqual(result["tools"][0]["function"]["name"], "weather")
        self.assertEqual(result["tool_choice"], {"type": "function", "function": {"name": "weather"}})

    def test_chat_tool_result_is_a_gemini_function_response(self):
        result = openai_to_gemini({"messages": [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "function": {"name": "weather", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
        ], "tool_choice": "required", "top_p": 0.5, "stop": ["END"]})
        response = result["contents"][1]["parts"][0]["functionResponse"]
        self.assertEqual(response["name"], "weather")
        self.assertEqual(response["response"], {"output": "sunny"})
        self.assertEqual(result["toolConfig"]["functionCallingConfig"]["mode"], "ANY")
        self.assertEqual(result["generationConfig"]["topP"], 0.5)
        self.assertEqual(result["generationConfig"]["stopSequences"], ["END"])

    def test_gemini_response_parallel_tool_ids_are_unique(self):
        result = gemini_to_openai({"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "a", "args": {}}}, {"functionCall": {"name": "b", "args": {}}},
        ]}, "finishReason": "STOP"}]}, "m")
        calls = result["choices"][0]["message"]["tool_calls"]
        self.assertNotEqual(calls[0]["id"], calls[1]["id"])
        self.assertEqual(result["choices"][0]["finish_reason"], "tool_calls")

    def test_gemini_rejects_unmatched_tool_result_and_malformed_arguments(self):
        for body in ({"messages": [{"role": "tool", "tool_call_id": "missing", "content": "x"}]},
                     {"messages": [{"role": "assistant", "tool_calls": [{"id": "c", "function": {"name": "f", "arguments": "not-json"}}]}]}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                openai_to_gemini(body)

    def test_responses_truncation_is_incomplete_with_usage(self):
        result = openai_to_responses_response({"id": "r", "model": "m", "choices": [
            {"message": {"content": "partial"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}})
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["incomplete_details"], {"reason": "max_output_tokens"})
        self.assertEqual(result["usage"], {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7})

    def test_gemini_response_does_not_replace_bad_json_with_empty_object(self):
        with self.assertRaises(ValueError):
            openai_to_gemini_response({"choices": [{"message": {"tool_calls": [
                {"id": "c", "function": {"name": "f", "arguments": "not-json"}}]}}]})

    def test_gemini_response_keeps_function_id(self):
        result = openai_to_gemini_response({"choices": [{"message": {"tool_calls": [
            {"id": "c", "function": {"name": "f", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]})
        self.assertEqual(result["candidates"][0]["content"]["parts"][0]["functionCall"]["id"], "c")

    def test_responses_nested_refusal_survives_conversion(self):
        result = responses_to_chat({"output": [{"type": "message", "content": [
            {"type": "refusal", "refusal": "cannot comply"}]}]}, "m")
        self.assertEqual(result["choices"][0]["message"]["refusal"], "cannot comply")
        back = openai_to_responses_response(result)
        self.assertEqual(back["output"][0]["content"][0], {"type": "refusal", "refusal": "cannot comply"})

    def test_builtin_tools_are_rejected_instead_of_relabelled(self):
        with self.assertRaises(ValueError):
            anthropic_to_openai_request({"tools": [{"type": "web_search_20250305", "name": "web_search"}]})
        with self.assertRaises(ValueError):
            gemini_request_to_openai({"tools": [{"googleSearch": {}}]}, "m")

    def test_gemini_malformed_function_args_are_rejected(self):
        with self.assertRaises(ValueError):
            gemini_to_openai({"candidates": [{"content": {"parts": [
                {"functionCall": {"name": "f", "args": [1]}}]}}]}, "m")

    def test_responses_content_filter_is_incomplete(self):
        result = openai_to_responses_response({"choices": [{"message": {"content": "partial"}, "finish_reason": "content_filter"}]})
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["incomplete_details"]["reason"], "content_filter")

    def test_gemini_filter_reason_is_preserved(self):
        result = gemini_to_openai({"candidates": [{"content": {"parts": [{"text": "partial"}]}, "finishReason": "SAFETY"}]}, "m")
        self.assertEqual(result["choices"][0]["finish_reason"], "content_filter")
        result = openai_to_gemini_response(result)
        self.assertEqual(result["candidates"][0]["finishReason"], "SAFETY")


if __name__ == "__main__":
    unittest.main()
