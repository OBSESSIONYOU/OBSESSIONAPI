#!/usr/bin/env python3
"""Offline Git checks for credentials and accidental data uploads (Python 3.9+)."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

VERSION = "8.30.1"
ASSETS = {
    "Windows": ("windows_x64.zip", "d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e"),
    "Linux": ("linux_x64.tar.gz", "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"),
}
HERE = Path(__file__).resolve().parent
MARKER = "# privacy-guard-managed"
OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
RAW_SUFFIXES = {
    ".sql", ".db", ".sqlite", ".sqlite3", ".csv", ".tsv", ".jsonl", ".log",
    ".bin", ".npy", ".npz", ".mat", ".pt", ".pth", ".h5", ".hdf5", ".onnx",
    ".pcap", ".pcapng", ".bag", ".mcap", ".parquet", ".zip", ".7z", ".rar",
    ".tar", ".tgz", ".gz", ".pem", ".key", ".p12", ".pfx", ".secret", ".kdbx",
}
DATA_DIRS = {"data", "dataset", "datasets", "captures", "recordings", "uploads", "exports", "backups", "logs"}
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}


class Blocked(Exception):
    pass


def git(root, *args, input_data=None):
    result = subprocess.run(["git", "-C", str(root), *args], input=input_data, capture_output=True)
    if result.returncode:
        raise Blocked("Git inspection failed; refusing an unchecked operation.")
    return result.stdout


def repository():
    return Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())


def state_dir(root):
    common = Path(git(root, "rev-parse", "--git-common-dir").decode().strip())
    return (common if common.is_absolute() else root / common).resolve() / "privacy-guard"


def scanner_path(root):
    return state_dir(root) / ("gitleaks.exe" if os.name == "nt" else "gitleaks")


def sensitive_path(path):
    # Git plumbing can create names that a Windows checkout cannot represent.
    if any(ord(char) < 32 or ord(char) == 127 for char in path) or path.endswith((" ", ".")):
        return True
    p = PurePosixPath(path.lower())
    name = p.name
    if name in {".gitkeep", ".env.example", ".env.sample", ".env.template", ".dev.vars.example"}:
        return False
    if name == ".env" or name.startswith((".env.", ".dev.vars")):
        return True
    if name in {"id_rsa", "id_ed25519", ".netrc", ".pypirc", ".npmrc", "credentials.json", "accounts.json", "tokens.json", "config.local.json"}:
        return True
    if p.suffix in {".json", ".yaml", ".yml", ".toml", ".ini", ".txt"} and re.match(r"^(?:accounts?|credentials|tokens|secrets)(?:[._-]|$)", name):
        return True
    if name.endswith((".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm")):
        return True
    if p.suffix == ".sql" and "migrations" in p.parts[:-1]:
        return False
    if p.suffix in RAW_SUFFIXES:
        return True
    if name.startswith("d1-") and p.suffix == ".json":
        return True
    # Application source modules under src/data are code; their secrets are scanned.
    if p.parts[:2] == ("src", "data") and p.suffix in SOURCE_SUFFIXES:
        return False
    return bool(DATA_DIRS.intersection(p.parts[:-1])) or any(part.startswith(("backup-", "backup_")) for part in p.parts[:-1])


def encrypted_backup(data):
    """Accept only the existing authenticated SparkBox envelope, never plaintext."""
    try:
        obj = json.loads(data)
        if set(obj) != {"format", "database", "encryptedAt", "kdf", "cipher", "iv", "ciphertext"}:
            return False
        if obj["format"] != "obsession-sparkbox-d1-backup-encrypted.v1" or obj["cipher"] != "AES-256-GCM":
            return False
        kdf = obj["kdf"]
        return (set(kdf) == {"name", "hash", "iterations", "salt"}
                and kdf["name"] == "PBKDF2" and kdf["hash"] == "SHA-256"
                and isinstance(kdf["iterations"], int) and kdf["iterations"] >= 310000
                and len(base64.b64decode(kdf["salt"], validate=True)) == 16
                and len(base64.b64decode(obj["iv"], validate=True)) == 12
                and len(base64.b64decode(obj["ciphertext"], validate=True)) > 16)
    except (ValueError, TypeError, KeyError):
        return False


def inspect_entries(root, entries, policy, seen):
    allowed = policy.get("approved_data", {})
    for path, oid in entries:
        if (path, oid) in seen or not sensitive_path(path):
            continue
        seen.add((path, oid))
        approved_hashes = allowed.get(path, [])
        encrypted = policy.get("allow_sparkbox_encrypted_backups") and path.endswith(".encrypted.json")
        data = b""
        if approved_hashes or encrypted:
            if int(git(root, "cat-file", "-s", oid)) > 64 * 1024 * 1024:
                raise Blocked("Data file exceeds the review limit: " + ascii(path))
            data = git(root, "cat-file", "blob", oid)
        if hashlib.sha256(data).hexdigest() in approved_hashes:
            continue
        if encrypted and encrypted_backup(data):
            continue
        raise Blocked("Data or credential file blocked: " + ascii(path) + ". Keep it outside Git; only reviewed demo bytes or schema migrations are allowed.")


def staged_entries(root):
    for record in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        _, oid, stage = meta.split()
        if stage != b"0":
            raise Blocked("Resolve index conflicts before committing.")
        yield path.decode("utf-8", "surrogateescape"), oid.decode()


def tree_entries(root, commit):
    for record in git(root, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if record:
            meta, path = record.split(b"\t", 1)
            _, kind, oid = meta.split()
            if kind == b"blob":
                yield path.decode("utf-8", "surrogateescape"), oid.decode()


def scan_secrets(root, tips=None):
    scanner = scanner_path(root)
    manifest = state_dir(root) / "scanner.json"
    if not scanner.is_file() or not manifest.is_file():
        raise Blocked("Scanner is missing. Run: python scripts/privacy_guard.py install")
    expected = json.loads(manifest.read_text(encoding="utf-8"))["sha256"]
    if hashlib.sha256(scanner.read_bytes()).hexdigest() != expected:
        raise Blocked("Scanner checksum changed; reinstall it before committing or pushing.")
    with tempfile.TemporaryDirectory(prefix="privacy-guard-") as scratch:
        report = Path(scratch) / "findings.json"
        ignore = Path(scratch) / ".gitleaksignore"
        ignore.write_text("", encoding="utf-8")
        args = [str(scanner), "git", str(root), "--config", str(HERE / "gitleaks.toml"),
                "--redact=100", "--no-banner", "--no-color", "--ignore-gitleaks-allow",
                "--gitleaks-ignore-path", str(ignore), "--report-format=json", "--report-path", str(report)]
        if tips is None:
            args += ["--pre-commit", "--staged"]
        else:
            args += ["--log-opts=--full-history -m " + " ".join(tips)]
        result = subprocess.run(args, capture_output=True)
        if result.returncode:
            findings = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else []
            for item in findings:
                print("[privacy-guard] %s at %s:%s (value redacted)" %
                      (item["RuleID"], ascii(item["File"]), item["StartLine"]), file=sys.stderr)
            raise Blocked("Secret scan blocked this operation." if findings else "Secret scanner failed; refusing an unchecked operation.")


def check(root, mode, updates=""):
    policy = json.loads((HERE / "privacy-policy.json").read_text(encoding="utf-8"))
    seen = set()
    if mode == "staged":
        inspect_entries(root, staged_entries(root), policy, seen)
        scan_secrets(root)
        return
    if mode == "pre-push":
        tips = []
        for line in updates.splitlines():
            fields = line.split()
            if len(fields) != 4 or not OID.fullmatch(fields[1]) or not OID.fullmatch(fields[3]):
                raise Blocked("Invalid pre-push input; refusing an unchecked push.")
            if set(fields[1]) != {"0"}:
                tips.append(fields[1])
        if not tips:  # Ref deletion does not upload objects.
            return
    else:
        tips = [git(root, "rev-parse", "HEAD").decode().strip()]
    tips = sorted(set(tips))
    for tip in tips:
        # Peel annotated tags; a direct blob/tag upload must not bypass inspection.
        git(root, "rev-parse", "--verify", tip + "^{commit}")
    for commit in git(root, "rev-list", *tips).decode().splitlines():
        inspect_entries(root, tree_entries(root, commit), policy, seen)
    scan_secrets(root, tips)


def download_scanner(destination):
    system = platform.system()
    if system not in ASSETS or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise Blocked("Automatic scanner installation supports Windows/Linux x64. Supply --scanner for another platform.")
    suffix, expected = ASSETS[system]
    url = "https://github.com/gitleaks/gitleaks/releases/download/v%s/gitleaks_%s_%s" % (VERSION, VERSION, suffix)
    with urllib.request.urlopen(url, timeout=40) as response:
        archive = response.read(64 * 1024 * 1024 + 1)
    if len(archive) > 64 * 1024 * 1024 or hashlib.sha256(archive).hexdigest() != expected:
        raise Blocked("Downloaded scanner failed the pinned release checksum.")
    if system == "Windows":
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            data = bundle.read("gitleaks.exe")
    else:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            data = bundle.extractfile("gitleaks").read()
    destination.write_bytes(data)
    destination.chmod(0o755)


def install(root, source=None):
    configured = subprocess.run(["git", "-C", str(root), "config", "--get", "core.hooksPath"], capture_output=True)
    if configured.returncode == 0:
        raise Blocked("Existing core.hooksPath is preserved. Integrate the guard with that hook setup explicitly.")
    state = state_dir(root)
    hooks = state.parent / "hooks"
    for event in ("pre-commit", "pre-push"):
        path = hooks / event
        if path.exists() and MARKER not in path.read_text(encoding="utf-8", errors="replace"):
            raise Blocked("Existing " + event + " hook is preserved; integrate it explicitly before installation.")
    state.mkdir(parents=True, exist_ok=True)
    destination = scanner_path(root)
    if source:
        source = Path(source).resolve()
        version = subprocess.run([str(source), "version"], capture_output=True, text=True, check=True).stdout.strip()
        if version != VERSION:
            raise Blocked("Expected Gitleaks " + VERSION)
        if source != destination:
            shutil.copyfile(source, destination)
        destination.chmod(0o755)
    elif not destination.exists() or not (state / "scanner.json").exists():
        download_scanner(destination)
    elif hashlib.sha256(destination.read_bytes()).hexdigest() != json.loads((state / "scanner.json").read_text(encoding="utf-8"))["sha256"]:
        raise Blocked("Existing scanner checksum changed; supply a verified --scanner to replace it.")
    (state / "scanner.json").write_text(json.dumps({"version": VERSION, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}), encoding="utf-8")
    for name in ("privacy_guard.py", "privacy-policy.json", "gitleaks.toml"):
        if (HERE / name).resolve() != (state / name).resolve():
            shutil.copyfile(HERE / name, state / name)
    hooks.mkdir(exist_ok=True)
    for event, mode in (("pre-commit", "staged"), ("pre-push", "pre-push")):
        command = "exec %s %s %s \"$@\"\n" % (
            shlex.quote(Path(sys.executable).as_posix()),
            shlex.quote((state / "privacy_guard.py").as_posix()), mode)
        path = hooks / event
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write("#!/bin/sh\n" + MARKER + "\n" + command)
        path.chmod(0o755)
    print("[privacy-guard] Installed local pre-commit/pre-push hooks; scanner runs offline.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("install", "staged", "pre-push", "ci"))
    parser.add_argument("hook_args", nargs="*")
    parser.add_argument("--scanner", help="Copy an already verified Gitleaks 8.30.1 binary during installation")
    args = parser.parse_args()
    try:
        root = repository()
        if args.mode == "install":
            install(root, args.scanner)
        else:
            check(root, args.mode, sys.stdin.read() if args.mode == "pre-push" else "")
            print("[privacy-guard] Checks passed.")
    except (Blocked, OSError, ValueError, subprocess.SubprocessError) as error:
        if isinstance(error, Blocked):
            print("[privacy-guard] " + str(error), file=sys.stderr)
        else:
            print("[privacy-guard] Check failed; refusing an unchecked operation.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
