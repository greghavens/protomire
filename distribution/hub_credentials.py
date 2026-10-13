#!/usr/bin/env python3
"""Native stdin-only credential storage. No network, plaintext fallback or logs."""
from __future__ import annotations

import importlib.metadata
import json
import re
import sys

SECURE_BACKENDS = ("keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.SecretService", "keyring.backends.kwallet")


def store_credential(request):
    writing = False
    try:
        if not isinstance(request, dict) or set(request) != {"service", "username", "secret"}:
            return {"status": "not_stored", "code": "invalid_request"}
        if request["service"] != "ModelBuilder.HuggingFace" or not isinstance(request["username"], str) or not re.fullmatch(r"modelbuilder-[a-f0-9]{16}-[a-f0-9]{32}", request["username"]):
            return {"status": "not_stored", "code": "invalid_reference"}
        secret = request["secret"]
        if not isinstance(secret, str) or not 8 <= len(secret) <= 512 or any(ord(c) < 33 for c in secret):
            return {"status": "not_stored", "code": "invalid_secret"}
        if importlib.metadata.version("keyring") != "25.6.0":
            return {"status": "not_stored", "code": "dependency_mismatch"}
        import keyring
        backend = keyring.get_keyring()
        if not type(backend).__module__.startswith(SECURE_BACKENDS):
            return {"status": "not_stored", "code": "secure_store_unavailable"}
        # Never overwrite an existing account. Each connection uses a fresh name.
        if backend.get_password(request["service"], request["username"]) is not None:
            return {"status": "not_stored", "code": "reference_exists"}
        writing = True
        backend.set_password(request["service"], request["username"], secret)
        if backend.get_password(request["service"], request["username"]) != secret:
            return {"status": "uncertain", "code": "verification_failed"}
        return {"status": "stored", "code": "stored_unverified_by_provider"}
    except Exception:
        # Exceptions from a backend may contain the credential; never echo them.
        return {"status": "uncertain" if writing else "not_stored", "code": "secure_store_failed"}


def main():
    try:
        raw = sys.stdin.buffer.read(4097)
        request = json.loads(raw) if len(raw) <= 4096 else None
        result = store_credential(request)
    except Exception:
        result = {"status": "not_stored", "code": "invalid_request"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
