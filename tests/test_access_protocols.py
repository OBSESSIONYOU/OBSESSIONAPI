"""Account-reverse-proxy protocol unit tests (offline, no quota).

Mirrors the reference plugin testing style: mount the real protocol module
and assert what the user actually sees - session shapes, gate headers,
credential refresh guards, and login wire forms.
"""
import json
import re
import time
from unittest import mock

from app import gemini_access, minimax_access, cline_access, opencode_free, lobsterai
import unittest


class OpencodeFreeProtocolTests(unittest.TestCase):
    def test_minted_session_matches_gate_shape(self):
        sid = opencode_free.mint_session_id()
        self.assertTrue(re.fullmatch(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}", sid), sid)
        self.assertTrue(0xFC0 <= (int(sid[4:16], 16) & 0xFFF) <= 0xFFF)

    def test_session_for_is_stable_per_conversation(self):
        a = opencode_free.session_for("conv-1")
        b = opencode_free.session_for("conv-1")
        c = opencode_free.session_for("conv-2")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_gateway_headers_satisfy_v2_gate(self):
        headers = opencode_free.gateway_headers("ses_e920cb842ffe3FWJqsz05vQbUW", stream=True)
        self.assertEqual(headers["user-agent"], opencode_free.CLIENT_UA)
        self.assertTrue(headers["user-agent"].startswith("opencode/"))
        self.assertIn("x-opencode-session", headers)
        self.assertIn("x-opencode-session-id", headers)
        self.assertNotIn("x-opencode-request", headers)

    def test_apply_fingerprint_injects_read_and_shell(self):
        body = opencode_free.apply_fingerprint({"model": "m", "messages": [{"role": "user", "content": "hi"}], "tool_choice": "none"})
        names = [t["function"]["name"] for t in body["tools"]]
        self.assertIn("read", names)
        self.assertIn("shell", names)
        self.assertNotIn("tool_choice", body)
        first = body["messages"][0]
        self.assertEqual(first["role"], "system")
        self.assertIn("placeholders", first["content"])

    def test_apply_fingerprint_renames_caller_read(self):
        caller_tool = {"type": "function", "function": {"name": "read", "description": "mine", "parameters": {"type": "object", "properties": {}}}}
        body = opencode_free.apply_fingerprint({"model": "m", "messages": [{"role": "user", "content": "x"}], "tools": [caller_tool]})
        names = [t["function"]["name"] for t in body["tools"]]
        self.assertIn("read_client", names)
        self.assertEqual(names.count("read"), 1)


class GeminiProtocolTests(unittest.TestCase):
    def test_auth_url_carries_offline_consent(self):
        url = gemini_access.build_auth_url(8845, "st-123")
        self.assertIn("accounts.google.com", url)
        self.assertIn("access_type=offline", url)
        self.assertIn("prompt=consent", url)
        self.assertIn("state=st-123", url)
        self.assertIn("localhost%3A8845", url)

    def test_envelope_roles_and_session(self):
        body = {"messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]}
        url, envelope = gemini_access.build_stream_request(body, "gemini-2.5-pro", "proj-1", "seed")
        self.assertIn("streamGenerateContent?alt=sse", url)
        roles = [c["role"] for c in envelope["contents"]]
        self.assertEqual(roles, ["user", "user", "model"])
        self.assertEqual(envelope["project"], "proj-1")
        self.assertTrue(envelope["sessionId"])

    def test_sse_chunk_translation(self):
        line = 'data: {"candidates":[{"content":{"parts":[{"text":"OK"}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":2,"candidatesTokenCount":3,"totalTokenCount":5}}'
        chunk = gemini_access.sse_chunk_to_openai(line, "gemini-2.5-pro")
        self.assertEqual(chunk["choices"][0]["delta"]["content"], "OK")
        self.assertEqual(chunk["choices"][0]["finish_reason"], "stop")
        self.assertEqual(chunk["usage"]["total_tokens"], 5)
        self.assertIsNone(gemini_access.sse_chunk_to_openai("data: [DONE]", "m"))
        self.assertIsNone(gemini_access.sse_chunk_to_openai(": keepalive", "m"))

    def test_expired_detection_and_refresh_guard(self):
        cred = {"access_token": "a", "refresh_token": "", "expires_at": int(time.time() * 1000) - 1000}
        self.assertTrue(gemini_access.is_expired(cred))
        import asyncio
        with self.assertRaises(ValueError):
            asyncio.run(gemini_access.refresh(cred))


class MinimaxProtocolTests(unittest.TestCase):
    def test_pending_vs_error_poll(self):
        import asyncio
        with mock.patch.object(minimax_access.httpx, "AsyncClient") as fake:
            resp = mock.Mock(status_code=200)
            resp.json.return_value = {"error": "authorization_pending"}
            ctx = fake.return_value.__aenter__.return_value
            ctx.post = mock.AsyncMock(return_value=resp)
            self.assertEqual(asyncio.run(minimax_access.poll_token("dc")), {"pending": True})

    def test_openai_to_anthropic_body(self):
        body = minimax_access.openai_to_anthropic_body(
            {"messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}], "max_tokens": 100}, "MiniMax-M2")
        self.assertEqual(body["system"], "sys")
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(body["max_tokens"], 100)
        self.assertIn("/v1/messages", minimax_access.INFER_PATH)


class ClineProtocolTests(unittest.TestCase):
    def test_token_prefix_applied(self):
        import asyncio
        with mock.patch.object(cline_access.httpx, "AsyncClient") as fake:
            resp = mock.Mock(status_code=200)
            resp.json.return_value = {"access_token": "abc", "expires_in": 3600}
            ctx = fake.return_value.__aenter__.return_value
            ctx.post = mock.AsyncMock(return_value=resp)
            cred = asyncio.run(cline_access.poll_token("dc"))
        self.assertTrue(cred["access_token"].startswith("workos:"))
        self.assertGreater(cred["expires_at"], time.time() * 1000)

    def test_chat_headers_carry_client_identity(self):
        headers = cline_access.chat_headers({"access_token": "workos:x"})
        self.assertEqual(headers["Authorization"], "Bearer workos:x")
        self.assertEqual(headers["HTTP-Referer"], "https://cline.bot")
        self.assertEqual(headers["X-CLIENT-TYPE"], "cline-sdk")


class LobsteraiProtocolTests(unittest.TestCase):
    def test_new_login_session_fields(self):
        session = lobsterai.new_login_session()
        self.assertIn("uuid", session)
        self.assertTrue(session["first_keyfrom"].isdigit())

    def test_login_url_shape(self):
        url = lobsterai.build_login_url(9753, "st")
        self.assertIn("lobsterai.youdao.com/portal#/login", url)
        self.assertIn("source=electron", url)
        self.assertIn("127.0.0.1%3A9753", url)

    def test_parse_credential_rejects_garbage(self):
        with self.assertRaises(ValueError):
            lobsterai.parse_credential("not-json")
        with self.assertRaises(ValueError):
            lobsterai.parse_credential(json.dumps({"refresh_token": "x"}))
