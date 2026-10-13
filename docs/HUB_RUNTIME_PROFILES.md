# Isolated Hub worker profiles

The Hub worker has its own `requirements/hub.in` and hash-locked
`requirements/profiles/hub-<target>.lock`. It does not expand the service,
training/backend, Trainer or MLX environments. Supported binary closures are
`linux-x64`, `linux-arm64`, `darwin-x64`, `darwin-arm64`, `win32-x64`, and
`win32-arm64`. `linux-armhf` has no verified Hub profile and fails closed.

The exact top-level versions are Hugging Face Hub 0.36.2 and keyring 25.6.0.
Locks include the complete platform-specific dependency closure. The adapter
uses only supported APIs from this pinned SDK. Credentials are never needed
for dependency resolution, installation or offline acceptance.

## Local setup and offline acceptance

Use the repository's exact CPython pin from `.python-version`:

```sh
.venv/bin/python scripts/bootstrap_hub.py --plan
.venv/bin/python scripts/bootstrap_hub.py
.venv/bin/python scripts/bootstrap_hub.py --test
MODELBUILDER_REQUIRE_HUB_SDK=1 PYTHONPATH=src .venv-hub-test/bin/python -m pytest -c /dev/null tests/hub/test_huggingface_sdk.py -v
```

The installer creates a fresh `.venv-hub` (production) or `.venv-hub-test`
(test-only). It refuses to overwrite an existing environment, checks the exact
Python patch version, accepts wheels only, enforces all package hashes, and
runs `pip check` plus exact SDK/keyring import checks. It uses the official
PyPI index with index credential lookup disabled. For a previously verified
wheelhouse, add `--offline-wheelhouse /path/to/wheels`; that uses `--no-index`.
The test profile adds pinned pytest dependencies while constraining every
production package to the same production lock.

`scripts/test_all.sh` runs the actual SDK suite separately from service tests.
Set `MODELBUILDER_HUB_TEST_PYTHON` to override the separate test interpreter.
`MODELBUILDER_REQUIRE_HUB_SDK=1` makes a missing environment a hard failure.
Without it, the receipt explicitly records that the SDK selection was not run.
The SDK suite blocks socket connection/DNS APIs and uses a deterministic
requests adapter; it cannot establish live provider acceptance.

## Verified scope

Official binary wheels for all six production targets were downloaded with
hash enforcement. Wheel metadata was checked against every applicable
`Requires-Dist` constraint in that target's pinned closure. On Linux x64,
a fresh offline environment passed `pip check`, the SDK/keyring import probe,
and all 15 actual-SDK fake-HTTP tests. Cross-target wheel verification is not
native Mac, Windows or ARM runtime acceptance. Test-only profile resolution
is complete for all six targets; native test execution was Linux x64 only.

No account, token, secure-keyring backend, private/gated repository, model
weight download or real upload was used. Secure account provisioning is a
separate explicit setup. Installing keyring does not establish a working
secure OS credential store. A managed bundle must use its sealed Hub worker
path and app-owned configuration rather than ambient operator settings.
