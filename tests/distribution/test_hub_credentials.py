"""Only fake backends and fake secrets. Never contacts an OS credential store."""
import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest

spec = importlib.util.spec_from_file_location("hub_credentials", Path(__file__).resolve().parents[2] / "distribution/hub_credentials.py")
credentials = importlib.util.module_from_spec(spec)
spec.loader.exec_module(credentials)
FAKE = "fake-test-secret-never-real"
REQUEST = {"service": "ModelBuilder.HuggingFace", "username": "modelbuilder-" + "a" * 16 + "-" + "b" * 32, "secret": FAKE}


@pytest.fixture
def backend(monkeypatch):
    class FakeBackend:
        __module__ = "keyring.backends.SecretService"
        def __init__(self):
            self.value = None
            self.writes = []
        def get_password(self, service, username):
            return self.value
        def set_password(self, service, username, secret):
            self.writes.append((service, username, secret))
            self.value = secret
    instance = FakeBackend()
    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_keyring=lambda: instance))
    monkeypatch.setattr(credentials.importlib.metadata, "version", lambda name: "25.6.0")
    return instance


def test_supported_os_backend_stores_and_verifies_fake_secret_without_echo(backend):
    receipt = credentials.store_credential(REQUEST)
    assert receipt == {"status": "stored", "code": "stored_unverified_by_provider"}
    assert backend.value == FAKE and FAKE not in json.dumps(receipt)


def test_plaintext_and_null_fallbacks_are_refused_before_write(backend):
    for module in ["keyrings.alt.file", "keyring.backends.null", "keyring.backends.chainer"]:
        type(backend).__module__ = module
        assert credentials.store_credential(REQUEST)["code"] == "secure_store_unavailable"
        assert backend.writes == []


def test_existing_reference_never_overwritten(backend):
    backend.value = "another-fake-value"
    assert credentials.store_credential(REQUEST)["code"] == "reference_exists"
    assert backend.writes == []


def test_partial_write_error_is_uncertain_and_never_echoes_secret(backend):
    def partial(*args):
        backend.value = FAKE
        raise RuntimeError(FAKE)
    backend.set_password = partial
    receipt = credentials.store_credential(REQUEST)
    assert receipt["status"] == "uncertain" and FAKE not in json.dumps(receipt)


def test_invalid_reference_secret_and_dependency_fail_without_writes(backend, monkeypatch):
    for change in [{"service": "arbitrary"}, {"username": "existing-user"}, {"secret": "bad\nsecret"}, {"secret": "x" * 513}]:
        assert credentials.store_credential({**REQUEST, **change})["status"] == "not_stored"
    monkeypatch.setattr(credentials.importlib.metadata, "version", lambda name: "1.0")
    assert credentials.store_credential(REQUEST)["code"] == "dependency_mismatch"
    assert backend.writes == []
