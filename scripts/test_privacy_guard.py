"""Exercise real Git hooks against local temporary repositories; never use GitHub."""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ENV = {**os.environ, "GIT_AUTHOR_NAME": "Privacy Guard Test", "GIT_COMMITTER_NAME": "Privacy Guard Test",
       "GIT_AUTHOR_EMAIL": "test@example.invalid", "GIT_COMMITTER_EMAIL": "test@example.invalid"}


class PrivacyGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        override = os.environ.get("PRIVACY_GUARD_TEST_SCANNER")
        if override:
            cls.scanner = Path(override).resolve()
        else:
            common = subprocess.check_output(["git", "rev-parse", "--git-common-dir"], cwd=HERE).decode().strip()
            common = Path(common) if Path(common).is_absolute() else HERE / common
            cls.scanner = common.resolve() / "privacy-guard" / ("gitleaks.exe" if os.name == "nt" else "gitleaks")
        if not cls.scanner.is_file():
            raise RuntimeError("Install Privacy Guard before running its integration tests.")

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="privacy-guard-test-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name) / "repo"
        self.remote = Path(self.scratch.name) / "remote.git"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.root / "scripts").mkdir()
        for name in ("privacy_guard.py", "gitleaks.toml"):
            shutil.copyfile(HERE / name, self.root / "scripts" / name)
        self.policy = {"approved_data": {}, "allow_sparkbox_encrypted_backups": False}
        self.write_policy()
        result = self.guard("install", "--scanner", str(self.scanner))
        self.assertEqual(result.returncode, 0, result.stderr)
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True, env=ENV)
        self.git("remote", "add", "origin", str(self.remote))

    def git(self, *args, data=None, success=True):
        result = subprocess.run(["git", "-c", "commit.gpgsign=false", "-C", str(self.root), *args],
                                input=data.encode("utf-8") if data is not None else None,
                                capture_output=True, env=ENV)
        result.stdout = result.stdout.decode("utf-8", "replace")
        result.stderr = result.stderr.decode("utf-8", "replace")
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def guard(self, *args):
        return subprocess.run([sys.executable, "scripts/privacy_guard.py", *args], cwd=self.root,
                              text=True, capture_output=True, env=ENV)

    def write_policy(self):
        (self.root / "scripts" / "privacy-policy.json").write_text(json.dumps(self.policy), encoding="utf-8")

    def reinstall(self):
        self.assertEqual(self.guard("install", "--scanner", str(self.scanner)).returncode, 0)

    def add(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.git("add", "-f", "--", path)

    def commit(self, bypass=False):
        args = ["-c", "core.hooksPath=" + str(self.root / "empty-hooks")] if bypass else []
        return self.git(*args, "commit", "-qm", "test change", success=False)

    def assert_remote_empty(self):
        result = subprocess.check_output(["git", "--git-dir", str(self.remote), "show-ref"], env=ENV) if any((self.remote / "refs" / "heads").iterdir()) else b""
        self.assertFalse(result, "A rejected push unexpectedly uploaded a ref.")

    def test_normal_commit_and_push_pass(self):
        self.add("app.py", "answer = 42\n")
        self.assertEqual(self.commit().returncode, 0)
        self.assertEqual(self.git("push", "origin", "HEAD:main", success=False).returncode, 0)

    def test_forced_environment_file_is_blocked(self):
        self.add(".env", "MODE=local\n")
        self.assertNotEqual(self.commit().returncode, 0)

    def test_staged_bytes_cannot_be_hidden_by_worktree_edit(self):
        token = "ghp_" + secrets.token_hex(20)
        self.add("app.py", 'access_token = "' + token + '"\n')
        (self.root / "app.py").write_text("answer = 42\n", encoding="utf-8")
        result = self.commit()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(token not in result.stdout + result.stderr, "Secret value reached hook output.")

    def test_database_csv_and_raw_capture_are_blocked(self):
        for path in ("runtime.db", "people.csv", "capture.bin", "backups/export.json", "capture.zip", "accounts.yaml", "backup-old/private.json"):
            with self.subTest(path=path):
                self.add(path, "private sample\n")
                self.assertNotEqual(self.commit().returncode, 0)
                self.git("rm", "--cached", "--", path)

    def test_schema_migration_is_allowed(self):
        self.add("migrations/0001_schema.sql", "CREATE TABLE demo (id TEXT PRIMARY KEY);\n")
        self.assertEqual(self.commit().returncode, 0)

    def test_reviewed_demo_requires_same_path_and_bytes(self):
        data = "sample_id,result\ndemo,allow\n"
        self.policy["approved_data"]["data/demo.csv"] = [hashlib.sha256(data.encode()).hexdigest()]
        self.write_policy()
        self.reinstall()
        self.add("data/demo.csv", data)
        self.assertEqual(self.commit().returncode, 0)
        self.add("data/demo.csv", "sample_id,result\nprivate,allow\n")
        self.assertNotEqual(self.commit().returncode, 0)
        self.git("restore", "--staged", "--worktree", "data/demo.csv")
        self.add("data/new.csv", data)
        self.assertNotEqual(self.commit().returncode, 0)

    def test_added_then_deleted_data_is_still_blocked_on_push(self):
        self.add("people.csv", "name\nprivate\n")
        self.assertEqual(self.commit(bypass=True).returncode, 0)
        self.git("rm", "people.csv")
        self.add("app.py", "answer = 42\n")
        self.assertEqual(self.commit(bypass=True).returncode, 0)
        self.assertNotEqual(self.git("push", "origin", "HEAD:main", success=False).returncode, 0)
        self.assert_remote_empty()

    def test_deleted_secret_in_outgoing_history_is_blocked_and_redacted(self):
        token = "ghp_" + secrets.token_hex(20)
        self.add("app.py", 'access_token = "' + token + '"\n')
        self.assertEqual(self.commit(bypass=True).returncode, 0)
        self.add("app.py", "answer = 42\n")
        self.assertEqual(self.commit(bypass=True).returncode, 0)
        result = self.git("push", "origin", "HEAD:main", success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(token not in result.stdout + result.stderr, "Secret value reached hook output.")
        self.assert_remote_empty()

    def test_rfc_exception_does_not_hide_other_test_credentials(self):
        if "RFC 6238" not in (HERE / "gitleaks.toml").read_text(encoding="utf-8"):
            self.skipTest("This repository has no RFC-vector exception.")
        seed = base64.b32encode(b"12345678901234567890").decode()
        sample = 'const RFC_SECRET = "' + seed + '";\n'
        self.add("tests/totp.test.mjs", sample)
        self.assertEqual(self.commit().returncode, 0)
        token = "ghp_" + secrets.token_hex(20)
        self.add("tests/totp.test.mjs", sample + 'const access_token = "' + token + '";\n')
        self.assertNotEqual(self.commit().returncode, 0)

    def test_plumbing_backup_push_is_blocked(self):
        blob = self.git("hash-object", "-w", "--stdin", data="INSERT INTO links VALUES ('private');\n").stdout.strip()
        tree = self.git("mktree", data="100644 blob " + blob + "\td1-backup.sql\n").stdout.strip()
        commit = self.git("commit-tree", tree, data="temporary backup\n").stdout.strip()
        self.assertNotEqual(self.git("push", "origin", commit + ":refs/heads/backups", success=False).returncode, 0)
        self.assert_remote_empty()

    def test_renamed_same_blob_is_checked_at_destination(self):
        self.add("app.txt", "private sample\n")
        self.assertEqual(self.commit().returncode, 0)
        self.git("mv", "app.txt", "people.csv")
        self.assertEqual(self.commit(bypass=True).returncode, 0)
        self.assertNotEqual(self.git("push", "origin", "HEAD:main", success=False).returncode, 0)
        self.assert_remote_empty()

    def test_control_character_cannot_hide_a_backup_extension(self):
        blob = self.git("hash-object", "-w", "--stdin", data="private\n").stdout.strip()
        tree = self.git("mktree", data="100644 blob " + blob + "\td1-backup.sql\r\n").stdout.strip()
        commit = self.git("commit-tree", tree, data="temporary backup\n").stdout.strip()
        self.assertNotEqual(self.git("push", "origin", commit + ":refs/heads/backups", success=False).returncode, 0)
        self.assert_remote_empty()

    def test_missing_scanner_fails_closed(self):
        scanner = self.root / ".git" / "privacy-guard" / self.scanner.name
        scanner.unlink()
        self.add("app.py", "answer = 42\n")
        self.assertNotEqual(self.commit().returncode, 0)

    def test_existing_unmanaged_hook_is_preserved(self):
        hook = self.root / ".git" / "hooks" / "pre-commit"
        original = "#!/bin/sh\necho existing-hook\n"
        hook.write_text(original, encoding="utf-8")
        self.assertNotEqual(self.guard("install", "--scanner", str(self.scanner)).returncode, 0)
        self.assertEqual(hook.read_text(encoding="utf-8"), original)

    def test_ref_deletion_remains_available(self):
        self.add("app.py", "answer = 42\n")
        self.assertEqual(self.commit().returncode, 0)
        self.git("push", "origin", "HEAD:refs/heads/temporary")
        self.assertEqual(self.git("push", "origin", ":refs/heads/temporary", success=False).returncode, 0)

    def test_plaintext_cannot_masquerade_as_encrypted_backup(self):
        self.policy["allow_sparkbox_encrypted_backups"] = True
        self.write_policy()
        self.reinstall()
        self.add("backups/d1-private.encrypted.json", json.dumps({"links": ["private"]}))
        self.assertNotEqual(self.commit().returncode, 0)

    def test_authenticated_envelope_shape_is_allowed(self):
        self.policy["allow_sparkbox_encrypted_backups"] = True
        self.write_policy()
        self.reinstall()
        envelope = {"format": "obsession-sparkbox-d1-backup-encrypted.v1", "database": "ai-links-db",
                    "encryptedAt": "2026-01-01T00:00:00Z", "cipher": "AES-256-GCM",
                    "kdf": {"name": "PBKDF2", "hash": "SHA-256", "iterations": 310000,
                            "salt": base64.b64encode(secrets.token_bytes(16)).decode()},
                    "iv": base64.b64encode(secrets.token_bytes(12)).decode(),
                    "ciphertext": base64.b64encode(secrets.token_bytes(64)).decode()}
        self.add("backups/demo.encrypted.json", json.dumps(envelope))
        self.assertEqual(self.commit().returncode, 0)


if __name__ == "__main__":
    unittest.main()
