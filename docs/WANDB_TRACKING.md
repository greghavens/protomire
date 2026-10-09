# Optional Weights & Biases tracking

ZenML remains the canonical execution, artifact, and model-version store. W&B is
an optional mirror of **allowlisted live checkpoint-evaluation metrics and final
scalar metrics**, not a new training framework or a second lifecycle authority. Tracking is disabled by default; local execution does not require
W&B, credentials, an account, or an internet connection.

## Explicit sharing controls

Set `tracking` on `project.create` / `project.update` (inside `changes` for updates)
or override it on `run.plan`, `run.submit`, or `run.resume`. A run override replaces
the complete project setting, rather than accidentally inheriting upload options.
Tracking is deliberately outside the portable recipe: importing a recipe cannot
turn on data sharing. Each submission records its effective `tracking_config`.

```json
{
  "mode": "offline",
  "entity": "your-team",
  "project": "your-experiments",
  "credential_env": "MODELBUILDER_WANDB_API_KEY",
  "metric_names": ["validation_loss", "test_loss"],
  "artifact_policy": "none"
}
```

- `disabled`: no SDK import or process, no tracking files, no networking.
- `offline`: real SDK run data is saved under the local job's `tracking` directory.
  No credentials are forwarded. Nothing automatically syncs later.
- `online`: requires an explicit entity and project plus
  `tracking_online_consent: true` on **each** submit/resume request. Planning
  discloses the destination, metric allowlist and artifact policy without uploading.
  Consent is part of the idempotent request, so changing destination or policy is a
  different request, not reuse of prior approval. Creating project defaults alone
  does not authorize online transmission.

Only allowlisted finite numbers are logged. Booleans, arrays, free text, unselected
metrics and credential-like metric names are excluded or rejected. Empty allowlist
means no scientific metrics. Opaque application-operation and ZenML-run UUIDs link
the mirror; model/project display names, recipe contents, source references, data,
checkpoint paths and checkpoint bytes are not passed to the SDK. W&B's protocol
still contains ordinary run/SDK bookkeeping (for example IDs, SDK version, timing).

Artifacts default to `none`. Explicit `metrics_snapshot` permits one generated JSON
artifact containing exactly those same selected numeric metrics. It does **not**
authorize source, data, images, model checkpoints, arbitrary files or a directory
upload. Those upload modes are intentionally unavailable.

Code/git capture, console capture, system metadata, machine information, system
metrics, requirement capture, TensorBoard sync and automatic job creation are
disabled. The SDK gets a fresh local HOME and a minimal environment; inherited
W&B settings cannot change the mode, destination or privacy switches. SDK diagnostic
logs are discarded and never returned by the service. Privacy settings using W&B
internal `x_` names are checked against the exact SDK pin; another SDK version is
reported unavailable rather than silently weakening them.

## Optional installation

The mandatory service/backend locks do not include W&B. Optional hash-locked
profiles are `requirements/profiles/tracking-wandb-{linux-x64,linux-arm64,darwin-arm64}.lock`.
They are compiled against the existing backend constraints, with Python 3.12.14,
uv 0.12.23 and `wandb==0.25.0`. Regenerate with `python scripts/lock_tracking.py`.
Only Linux x64 execution has been verified here; the other locks are resolved
profiles, not native-runtime acceptance.

Prefer a dedicated interpreter (replace the profile for your supported host):

```sh
uv venv --python 3.12.14 .venv-tracking
uv pip sync --python .venv-tracking/bin/python --require-hashes requirements/profiles/tracking-wandb-linux-x64.lock
export MODELBUILDER_TRACKING_PYTHON="$PWD/.venv-tracking/bin/python"
```

This is an operator-controlled interpreter path, never a caller-supplied execution
path. If omitted, the backend interpreter is used; without the exact optional SDK,
tracking reports `unavailable` and canonical execution remains successful.

Online authentication uses only the fixed operator-supplied environment variable
`MODELBUILDER_WANDB_API_KEY`. The stored `credential_env` is a literal reference,
not a key or an arbitrary selector for other environment secrets. The adapter
passes it only to an explicitly consented online supervisor, which removes it
before starting any training component. The isolated mirror maps it to the SDK's
`WANDB_API_KEY`; no login call writes credentials to a profile. Never put key values
in recipes, project config, request JSON, shell command arguments, or logs.

## Status and failure behavior

A mirror starts once a canonical run UUID exists. The supervisor polls completed
checkpoint evaluations without waiting on SDK/network calls, forwarding only
selected finite numeric metrics and the checkpoint training step. W&B uses
`checkpoint/step` as the scientific x-axis; its internal monotonic log sequence
tracks completion order only. An evaluation for checkpoint 20 finishing before
checkpoint 10 keeps those actual training steps. The run plan's metric allowlist
also controls these live results. Final successful canonical metrics are included
at finish, without inventing a checkpoint step for aggregate results.

The local event spool is capped at 8 MiB / 10,000 observations. Excess observations
are dropped without blocking training and counted in `dropped_events`. Event IDs
and journal cursors prevent duplicate completion records within a session; there
is no automatic remote reconnect/replay. If live-journal ingestion fails,
`live_journal_error` is exposed separately. Training that fails before a canonical
run exists is `not_mirrored`; if a mirror already exists, it finishes with the
training failure code and retains any earlier completed evaluation metrics. SDK failures, missing credentials, absent or
wrong SDK, and timeouts are separate tracking outcomes and do not relabel the
canonical ZenML result. The tracking object reports mode, status, stable W&B run
ID, and URL only after an online finish succeeds. Offline results include the local
tracking directory; they never invent an online URL. A metadata-only `tracking_readiness` plan field reports the optional SDK pin
and whether the configured online credential exists, without importing the SDK,
using credentials, contacting W&B, or blocking canonical training admission.

An overall runtime watchdog plus a 45-second finish bound terminates both the
process group and captured SDK descendants. The SDK worker also watches its parent
PID and exits if its supervisor disappears. Failure can leave a partial remote run and reports
`remote_may_be_partial`; the service does not silently retry or sync offline data.
A stopped supervisor with unfinished tracking is `interrupted`, never `synced`.
Service status reads do not initiate uploads. Training may already show `succeeded`
while the independent mirror remains `pending`/`running`.

## Verification and current limits

```sh
MODELBUILDER_TRACKING_TEST_PYTHON="$PWD/.venv-tracking/bin/python" .venv/bin/pytest tests/tracking -q
MODELBUILDER_TRACKING_TEST_PYTHON="$PWD/.venv-tracking/bin/python" PYTHONPATH=src .venv-backend/bin/python -m pytest tests/tracking/test_cpu_live_tracking.py -q
```

The real offline acceptance tests run the pinned SDK without credentials, decode
the actual `.wandb` transaction stream, verify selected metrics/artifact records,
stream two evaluations with out-of-order checkpoint steps, and check that private sentinel data, credentials, console/system records, source
metadata and requirements are absent. Policy/service tests cover default-off,
explicit replacement, rejected secret aliases, online consent and redacted errors.
A separate CPU fixture integration runs actual Torch training plus independent
checkpoint evaluators and verifies that the SDK acknowledges checkpoint step 4
while training is still running. Its correlation UUID is explicitly synthetic;
this fixture-level test is not a claim of full source-bound ZenML replay.

Kernel-level network syscall tracing was unavailable in this sandbox (ptrace
permission denied); no claim of a packet-capture audit is made.

No online account/authentication, remote upload, remote retention, organization
permissions, billing or URL reachability has been tested. Online validation needs
an explicitly approved account/destination and non-sensitive test data. Offline
verification is not evidence that an online project is correctly configured.

## Official implementation references

- [ZenML W&B experiment tracker](https://docs.zenml.io/stacks/stack-components/experiment-trackers/wandb)
- [Pinned W&B 0.25.0 settings source](https://github.com/wandb/wandb/blob/v0.25.0/wandb/sdk/wandb_settings.py)
- [Pinned SDK run implementation](https://github.com/wandb/wandb/blob/v0.25.0/wandb/sdk/wandb_run.py)

The direct official SDK is used here to keep sharing narrower than automatic
framework/tracker integration. The public ZenML run ID provides the correlation;
no private ZenML storage or shadow artifact/model implementation is introduced.

## Portable optional installation

`python scripts/bootstrap.py --tracking --skip-frontend` installs the checksum-pinned optional SDK into its own native-layout environment, selecting the current target profile. It does not enable tracking, log in, save credentials, or upload anything. Unsupported targets fail explicitly. `--tracking-venv` configures the destination; it cannot share the service or backend environment.
