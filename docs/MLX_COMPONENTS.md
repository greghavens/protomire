# Isolated MLX components

Modelbuilder provides source-bound MLX full training, LoRA training, evaluation,
generation and safetensors export components. The shared operation service and
ZenML retain run, scheduling, artifact, lineage and cancellation ownership. MLX
runs in a separate operator-configured interpreter, within the supervised native
run process group.

## Supported profile and setup

The reviewed profile is **macOS ARM64, Python 3.12.14**, with MLX/MLX-Metal
0.32.3, MLX-LM 0.32.0 and Transformers 5.7.0. All 34 worker distributions are
version- and hash-locked. This environment is separate from the service and the
Torch/Transformers 4.57.1/ZenML backend; do not install its lock into either of
those environments. Linux, Intel macOS and Windows MLX execution are unsupported.

Using the repository's pinned Python and official bootstrap tools:

```sh
./scripts/bootstrap.sh --backend --trainer --mlx --skip-model-download
export MODELBUILDER_MLX_PYTHON="$PWD/.venv-mlx/bin/python"
```

`--mlx-venv /absolute/path` selects another isolated worker environment. The
bootstrap rejects an unsupported native platform before installation. `--plan`
prints availability and paths without installing anything.

For persistent operator configuration, add the interpreter to the existing
operator JSON and start the service with `MODELBUILDER_OPERATOR_CONFIG` pointing
to that file:

```json
{
  "mlx_python_executable": "/absolute/modelbuilder/.venv-mlx/bin/python"
}
```

Keep the existing source roots, backend interpreter and other approved operator
settings. The interpreter is an operator setting, never a recipe-supplied
executable. The environment variable is an alternative; the explicit operator
setting takes precedence.

## Versioned recipe and source binding

Choose an explicit recipe device and matching target:

- `environment.device: "mlx-metal"` with `local-mlx-metal`
- `environment.device: "mlx-cpu"` with `local-mlx-cpu`

Generic `cpu` placement does not admit MLX component recipes. Both explicit MLX
routes require the same separate reviewed profile; `mlx-cpu` is still the
macOS ARM64 MLX interpreter, not a Linux or Torch fallback.

Keep the primary backend `environment.lock_file` and `requirements` intact, and
add this separate worker profile to the source-bound recipe:

```json
{
  "mlx_profile": {
    "path": "requirements/profiles/mlx-darwin-arm64.lock",
    "sha256": "f408bef35d4c867e2b634ac066cc73a4197bc60214dfd1f22cb57683fb2b68bb"
  }
}
```

This is an `environment` field, not a replacement for the backend lock. Commit
and pin the profile and adapter source through the ordinary source-workspace
review. Planning checks the profile and configured interpreter; submission also
requires a real selected-device forward/backward probe. A metadata or allocation
probe alone is not evidence that a model component completed.

The component catalog exposes `modelbuilder.mlx.train`, `.evaluate`, `.generate`
and `.export`. It supplies their full parameter/port contracts and capabilities.
The existing trusted-source admission still applies. Model manifests bind
individual, hashed file dependencies; recipes cannot direct the worker to an
arbitrary model directory or install packages. Model/tokenizer loading is local,
disallows remote code, rejects tokenizer metadata file-path overrides, and checks
weight-index references against the verified local asset directory.

## Scope, evaluation and continuation

- Architecture budgeting currently reviews **Llama**. Full and LoRA modes use
  official MLX-LM loaders and training APIs. This is not a claim of arbitrary
  architecture support, arbitrary PyTorch conversion or GGUF export.
- Training and evaluation use registered dataset versions and explicit split
  identities. Native acceptance checks disjoint train/evaluation sample IDs,
  actual training-batch consumption and one-supervised-token evaluation rows.
- Live checkpoint evaluation is CPU-only, using **MLX CPU**. Immutable evaluation
  records retain the actual `mlx-cpu` device, framework, all worker package
  versions and profile SHA256. This does not claim accelerator reservation or
  isolated performance measurement.
- Continuation supports explicit **warm start**. Compatible checkpoints can be
  used with changed data, update budget and MLX device. Exact optimizer/RNG-state
  continuation is rejected. Full checkpoints retain their config/tokenizer and
  weights; LoRA checkpoints still need their admitted, verified base assets.
- Static and conditional pipelines remain sequential by default. MLX and macOS
  reject `max_parallel_stages > 1`; the separately verified Linux CPU concurrency
  route is unchanged.
- Cancellation publishes a safe completed-update checkpoint where available and
  the native supervisor owns process-group cleanup. Per-attempt node deadlines
  and first-writer cancellation reasons remain the shared runtime's responsibility.

## Resource bounds

The catalog exposes a positive `limits.max_asset_bytes` capped at **512 MiB**.
Staging rejects larger requested budgets before copying assets. Existing reviewed
architecture, parameter-count, row, update, batch and token-length limits also
apply; increasing one limit does not disable the others.

Export compares native logits with a fresh-process safetensors loader. The
complete parity batch × sequence × vocabulary must contain at most **1,000,000
logits**, checked before loading/evaluation. Parity JSON is capped at **32 MiB**.
Generation rejects token IDs outside the verified model vocabulary. These
bounds are fail-closed input/resource contracts, not a hostile-code sandbox.

## Verification status and required native gate

The original Mac handoff reported six required-hardware passes on an M3 Max,
covering full/LoRA service pipelines, independent registered evaluation data,
live evaluation, warm start, export parity, cancellation and boundary checks.
That evidence predates the final Linux/G11 integration and subsequent boundary
fixes. **It does not certify this merged source on Metal.**

The merged source has Linux contract/import/wheel and frontend checks. Those
checks do not establish Metal or MPS execution. On an accessible, unlocked Apple
Silicon host, rerun the final source with the reviewed separate interpreter:

```sh
MODELBUILDER_REQUIRE_MLX=1 \
MODELBUILDER_MLX_PYTHON="$PWD/.venv-mlx/bin/python" \
PYTHONPATH=src .venv-backend/bin/python -m pytest \
  tests/integration/test_mlx_service.py tests/components/test_mlx_boundaries.py -v \
  --basetemp="$PWD/evidence/mlx-merged-native-new-run" \
  --junitxml="$PWD/evidence/mlx-merged-native-new-run.xml"
```

Use a fresh basetemp for each run. Tests author tiny local official-model assets;
no Hub download, account, credential or paid compute is needed. The full-mode
service test now includes an instrumented export helper that pauses **after real
native logits are computed**, so the actual node deadline can verify descendant
termination and preservation of its cancellation reason. This scenario also
requires a fresh Mac execution; its Linux code-generation tests are insufficient.

The ordinary Torch MPS gate and native IDE GUI acceptance remain separate. Mac
GUI follow-up reached real File-menu/import checks and partial workbench checks,
but full native GUI acceptance is still incomplete. See the
[acceptance matrix](ACCEPTANCE_MATRIX.md) and [device-placement notes](LOCAL_DEVICE_PLACEMENT.md).
