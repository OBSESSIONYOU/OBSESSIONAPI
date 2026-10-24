"""Invalid console bodies and nondeterministic requests must fail safely."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from starlette.requests import Request

from app import database, gateway
from app.main import app


class RequestBoundaryTests(unittest.TestCase):
    def test_native_close_reaches_upstream_through_chat_entry(self):
        async def run():
            closed = []

            async def upstream(*args):
                try:
                    yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
                    await asyncio.Event().wait()
                finally:
                    closed.append(True)

            channel = {"id": 1, "name": "fixture", "type": "openai_responses",
                       "protocol": "openai_responses", "model_mapping": "{}", "tool_policy": "native"}
            request = Request({"type": "http", "method": "POST", "path": "/v1/responses",
                               "headers": [], "client": ("127.0.0.1", 0)})
            with mock.patch.object(gateway, "_authorize_request", return_value=({"id": 1}, None)), \
                 mock.patch.object(gateway, "_resolve_candidates", return_value=([channel], None)), \
                 mock.patch.object(gateway, "_cache_lookup", return_value=None), \
                 mock.patch.object(gateway, "_begin_protocol_stream", new=mock.AsyncMock(return_value=(object(), 200, "", ""))), \
                 mock.patch.object(gateway, "_protocol_stream_relay", side_effect=upstream):
                response = await gateway._chat_inner(request, {"model": "fixture", "messages": [], "stream": True})
                native = await gateway._as_native_response(response, "openai_responses", "rid")
                await anext(native.body_iterator)
                await native.body_iterator.aclose()
                self.assertEqual(closed, [True])

        asyncio.run(run())

    def test_console_rejects_non_object_json_before_using_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(database, "DATA_DIR", Path(temporary)), \
                 mock.patch.object(database, "DB_PATH", Path(temporary) / "test.db"):
                with TestClient(app, raise_server_exceptions=False) as client:
                    for endpoint in ("/api/setup", "/api/login"):
                        for payload in ([], ["owner"], "owner", 1, None):
                            with self.subTest(endpoint=endpoint, payload=payload):
                                response = client.post(endpoint, content=json.dumps(payload),
                                                       headers={"Content-Type": "application/json"})
                                self.assertEqual(response.status_code, 400, response.text)

    def test_cache_requires_explicit_numeric_zero_temperature(self):
        settings = {"cache_enabled": True, "cache_ttl_seconds": 60}
        with mock.patch.object(gateway, "_settings", return_value=settings), \
             mock.patch.object(gateway, "_CACHE", {}):
            for extra in ({}, {"temperature": None}, {"temperature": False}, {"temperature": 1}):
                with self.subTest(extra=extra):
                    body = {"model": "fixture", **extra}
                    gateway._cache_store("fixture", body, 1, {"id": "fixture"}, 2, 3)
                    self.assertIsNone(gateway._cache_lookup("fixture", body))
            self.assertEqual(gateway._CACHE, {})
            for temperature in (0, 0.0):
                body = {"model": "fixture", "temperature": temperature}
                gateway._cache_store("fixture", body, 1, {"id": "fixture"}, 2, 3)
                self.assertEqual(gateway._cache_lookup("fixture", body)["result"]["id"], "fixture")


if __name__ == "__main__":
    unittest.main()
