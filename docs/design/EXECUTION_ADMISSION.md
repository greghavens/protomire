# Local execution admission

`run.plan` and `run.submit` share read-only admission. Planning inspects source
bytes, Git identity, dependency hashes, the actual backend Python/distribution
metadata and supported execution settings without importing user code, creating
ZenML runs or initializing the ZenML store. Submission repeats validation;
a plan is an observation, never an authorization token.

An executable plan requires `trusted:true`. Submit still needs its own explicit
consent. Arbitrary trusted source runs with the service account's local rights;
this is **not** an isolation sandbox.

## Operator configuration

Set `MODELBUILDER_OPERATOR_CONFIG` to a local JSON file before starting the
HTTP operation service (native MCP connects to this same service). The supported keys are `source_root`,
`python_executable`, `pretrained_path`, `trusted_code_roots` (an array), and
`dependency_paths` (manifest dependency ID to local path), and `dataset_roots`
(project ID to a map of root aliases/local paths). Dataset operations accept only
these project-scoped aliases and relative paths, never new root locations. Paths are relative
to the config file, unless absolute. Unknown keys and malformed values fail
startup instead of weakening admission.

Example:

```json
{
  "source_root": "./my-model",
  "python_executable": "../modelbuilder/.venv-backend/bin/python",
  "trusted_code_roots": ["./my-model"],
  "dependency_paths": {"training-data": "./data/train.json"}
}
```

This is operator-controlled configuration, never a recipe-request field. Every
resource file is checked against its manifest SHA256. Python implementation
commit/path/hash must occur in the source closure; declared files must match
both working-tree bytes and the immutable Git commit. Configuration is not a
permission to execute: each run still needs explicit trust consent.

## Honest execution settings

- CPU/memory reservation fields are rejected, including former example defaults.
  Local process supervision cannot reserve or isolate these resources. Edit a
  new recipe revision to omit them; saved revisions are not silently changed.
- GPU resource reservation requests must be zero. Explicit framework device
  placement is separate: CPU is the legacy default, MPS is profile/hardware/policy
  gated, and CUDA remains unavailable without a reviewed locked profile. Placement
  is not resource isolation or proof of actual accelerator execution; see
  [local device placement](../LOCAL_DEVICE_PLACEMENT.md). Remote execution remains disabled.
- Per-node environment must be `default`; per-stage isolation is unsupported.
  Caching defaults to `disabled`. Explicit `cache_policy: enabled` is accepted only
  for the deterministic, side-effect-independent JSON-value stages described in
  [bounded native reuse](../NATIVE_PIPELINE_CACHE.md). Training, evaluation, export,
  model-role components, Path/checkpoint ports and fixture built-ins reject it.
- Node timeout is supervised elapsed execution time. Expiry aborts the entire
  local process group; it is not a recoverable isolated step timeout. The
  supervisor's cancellation grace and escalation still apply.
- Retries use ZenML's native retry configuration. Backoff must be an integer;
  fractional delays are rejected rather than truncated.
- Standalone `training.kl_anchor` remains a schema representation, not an offered
  executable component. The train node's supported connected-initial-model KL
  configuration is separate.

`execution.targets` exposes capability details for the GUI and MCP. Clients
must use this result instead of assuming all schema node kinds are executable.

## Target-aware environments

`execution.targets.environment_profiles` offers content-addressed installed
profile references. Selecting one means an explicit recipe update/new revision,
including the profile in source closure. Linux profiles pin torch 2.9.0+cpu;
macOS arm64 pins torch 2.9.0. No run-time substitution or pin loosening occurs.
The environment verifier evaluates lock-file requirement markers in the actual
backend interpreter and checks the pinned lock's installed distribution closure.
Unsupported OS/architecture profiles are not offered. Discovery is not a claim
that native execution was tested on another OS or architecture.
