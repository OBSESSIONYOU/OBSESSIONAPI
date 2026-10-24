# Channel Secret Redaction Implementation Plan

> **For agentic workers:** Execute inline with test-driven development; no subagents are required.

**Goal:** Prevent upstream API keys from being returned by the channel-list API without changing channel editing behavior.

**Architecture:** Keep secrets in SQLite and server-side channel operations. Redact only at the admin response boundary so routing, testing, and model discovery keep using the stored value.

**Tech Stack:** Python 3, FastAPI 0.141.1, SQLite, unittest, Docker Compose.

## Global Constraints

- Preserve `data\obsessionapi.db`, accounts, channels, models, and every stored API key.
- Add no dependency and no schema migration.
- Reference `D:\reference\new-api\controller\channel.go:158-162`, which omits channel keys from list queries.
- The checkout has no Git repository, so no commit step is possible.

---

### Task 1: Lock the response contract with a failing test

**Files:**
- Modify: `tests/test_first_run.py`

- [x] Add `test_channel_list_does_not_return_upstream_api_keys` that creates a channel with a recognizable secret, fetches `/api/channels`, and asserts both the `api_key` property and the raw secret are absent.
- [x] Run that single test and confirm it fails because the current response includes `api_key`.

### Task 2: Redact at the API boundary

**Files:**
- Modify: `app/admin_api.py`

- [x] Change `channels_list` to remove `api_key` from every serialized row.
- [x] Run the new test and confirm it passes.
- [x] Run the full unittest suite and static checks.

### Task 3: Deploy and verify

- [x] Run `docker compose up -d --build`.
- [x] Confirm `docker compose ps` reports `healthy`.
- [x] Verify the live `/api/channels` response contains no upstream secret without printing any secret to the console.
- [x] Run one minimal `gpt-5.5` streaming smoke and verify exactly one non-empty `finish_reason` and one `[DONE]`.

## Result

- TDD red: the new test failed because `/api/channels` returned the literal upstream secret.
- TDD green: 39 tests passed after selecting only non-secret channel columns.
- Live: Docker healthy; three channels returned with no `api_key` field and no stored secret text.
- Live GPT stream: HTTP 200, `finish_reason=stop` once, `[DONE]` once, no error event.
