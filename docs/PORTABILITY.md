# Portable recipe replay (T19)

`scripts/replay_bundle.py` separates three boundaries:

1. `export` verifies selected source against an existing Git commit and produces an integrity-checked recipe archive.
2. `import` validates and extracts only declared, hash-checked text into a new directory. `wheel` packages those bytes as ordinary wheels without importing bundled modules or running build hooks. Neither installs dependencies, fetches resources, or executes model code.
3. `run --trust-repository-source` is a separate, explicit action for reviewed repository-owned CPU fixtures. It uses the same `OperationService` import/submit/status operations as the IDE and MCP. It is not an untrusted-code sandbox.

The archive includes the full application Python source, both independent dependency locks, `pyproject.toml`, and the replay command. No original absolute paths, datasets, model weights, secrets, installed environments, or `.pth` path-sharing files are included. Git revision plus per-file SHA256 identify the source; a bundle hash alone does not authenticate its publisher.

## One model project, one recipe identity

The replay creates a model project, reads its already-owned recipe, and imports the portable training/evaluation pipeline as the next immutable revision of that same recipe. It sends `bundle_json` through the strict raw-JSON operation contract with `expected_revision`. Import remains inert; the explicit run action executes the imported revision.

The harness verifies that the project retains its recipe ID, exactly one recipe remains listed, the original revision is unchanged, and execution provenance identifies the exact imported recipe ID, revision and digest. Origin and replay have different project IDs because their application/native stores are independent; each project has one stable recipe identity throughout its own lifecycle. The `model.initialize` node explicitly selects the custom or pretrained fixture. Dataset compatibility is validated separately.

## Why there are two environments per execution

The service and ZenML have independently pinned dependency graphs. In the measured locks, FastAPI, Pydantic and JSONSchema versions differ. Combining these locks or ignoring a failed dependency check would not be a reproducible deployment.

- Service wheel: `modelbuilder`, all application Python source, service dependency metadata.
- Execution wheel: `modelbuilder-runtime`, the same source snapshot's `modelbuilder/__init__.py` and `modelbuilder/backend/*.py`, backend dependency metadata.

These wheels are installed into separate environments. No implementation is duplicated or changed by packaging. The standard-library wheel writer emits deterministic ZIP members, `METADATA`, `WHEEL`, and a valid hashed `RECORD`; no setup hooks or bundle-supplied packaging commands run. Native SDK/worker processes use the runtime environment's installed packages and an explicitly supplied extracted source directory for source-integrity verification.

## Reproduce

Run from a committed checkout with Python 3.12.14 and `uv` installed. The export commit must contain the replay script, all packaged application modules, `pyproject.toml` and both lockfiles. Dirty or missing source fails export. Paths below are local CLI arguments, not manifest fields.

```bash
set -euo pipefail
ROOT="$PWD"
COMMIT="$(git rev-parse HEAD)"
WORK="$ROOT/.runtime/portability"
mkdir -p "$WORK" evidence/portability
PYTHONPATH="$ROOT/src" .venv/bin/python scripts/replay_bundle.py export \
  examples/custom-pytorch/recipe.json --root "$ROOT" --commit "$COMMIT" \
  --output "$WORK/custom.recipe.tar"
for copy in origin replay; do
  PYTHONPATH="$ROOT/src" .venv/bin/python scripts/replay_bundle.py import \
    "$WORK/custom.recipe.tar" --destination "$WORK/$copy-source"
done
PYTHONPATH="$ROOT/src" .venv/bin/python scripts/replay_bundle.py wheel \
  "$WORK/origin-source" --kind service --destination "$WORK/wheels"
PYTHONPATH="$ROOT/src" .venv/bin/python scripts/replay_bundle.py wheel \
  "$WORK/origin-source" --kind backend --destination "$WORK/wheels"

# Use independent install locations; an ordinary package download cache is fine.
export UV_CACHE_DIR="$ROOT/.cache/uv"
for name in .venv-portability-origin .venv-replay; do
  uv venv --python 3.12 "$name"
  uv pip sync --python "$name/bin/python" "$WORK/origin-source/requirements/service.lock"
  uv pip install --python "$name/bin/python" --no-deps "$WORK/wheels/modelbuilder-0.1.0-py3-none-any.whl"
  uv pip check --python "$name/bin/python"
done
for name in .venv-portability-origin-backend .venv-replay-backend; do
  uv venv --python 3.12 "$name"
  uv pip sync --python "$name/bin/python" --require-hashes \
    "$WORK/origin-source/requirements/backend.lock" \
    --extra-index-url https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match
  uv pip install --python "$name/bin/python" --no-deps "$WORK/wheels/modelbuilder_runtime-0.1.0-py3-none-any.whl"
  uv pip check --python "$name/bin/python"
done

# -I ignores caller PYTHONPATH/user site; neither environment is editable.
.venv-portability-origin/bin/python -I "$WORK/origin-source/scripts/replay_bundle.py" run \
  "$WORK/origin-source" --data-dir "$WORK/origin-data" \
  --backend-python "$ROOT/.venv-portability-origin-backend/bin/python" \
  --trust-repository-source --output evidence/portability/custom-origin.json
.venv-replay/bin/python -I "$WORK/replay-source/scripts/replay_bundle.py" run \
  "$WORK/replay-source" --data-dir "$WORK/replay-data" \
  --backend-python "$ROOT/.venv-replay-backend/bin/python" \
  --trust-repository-source --output evidence/portability/custom-replay.json
.venv-replay/bin/python -I "$WORK/replay-source/scripts/replay_bundle.py" compare \
  evidence/portability/custom-origin.json evidence/portability/custom-replay.json \
  --output evidence/portability/custom-comparison.json
```

Directories must be new; import and run intentionally refuse to overwrite them. Source packaging is inert, but installing even a reviewed wheel and executing Python require the operator's authorization. This example is local CPU only and creates no cloud resources.

## Evidence and limits

The comparison requires exact deterministic metrics, dataset SHA256 and every output-file SHA256; distinct backend run IDs; different service/runtime environment prefixes; own installed module paths; no original source/environment imports; no shared `.pth` paths; identical Python/package versions; independent ZenML stack, artifact-store and metadata-store identities; and preservation of each project's single recipe identity and immutable revision provenance. Raw run evidence records actual `sys.path`, module files, installed distributions, native artifact UUIDs, recipe identity history and source hashes.

`tests/portability/test_replay_bundle.py` verifies inert deterministic packaging, tamper rejection, separate dependency metadata, explicit trust, timeout cancellation through the real operation schema, retained failure evidence, shipping manifests' complete source closure, and rejection of contaminated or drifting replay evidence. Those unit tests alone do not establish T19. A successful comparison JSON plus its origin/replay records establishes the real CPU replay result.

The shipping manifests pin the published source revision `086908e3eb20ff025bbd50108e6b6563855b7512`, including the single-owned-recipe domain, lossless raw-JSON bundle transport, and bounded concurrent SQLite startup correction. `evidence/portability/summary.json` records the final measured custom and pretrained results; `bundles.json` inventories the portable archives. Each recipe includes 20 verified source/lock files. Both archives remain inert. The transport digest is retained separately from the project-owned recipe identity allocated at project creation and the exact immutable revision created by import. Superseded results under `evidence/portability/historical/` are historical observations, not acceptance evidence for this shipping source.

This matches the evaluated Kubeflow portability scope: independent Python environments and metadata/artifact stores on the same CPU host. It does not establish another operating system, another machine, GPUs, remote execution, per-stage container isolation, or untrusted recipe execution. Source license `NOASSERTION` remains a visible unresolved redistribution issue; local operator trust does not confer redistribution rights. External pretrained resources, if used, must remain immutable references and be separately resolved and SHA256-verified rather than embedded in the recipe archive.

### Pretrained replay

Use `examples/pretrained-small/recipe.json` in the export step and new extracted source/data directories. Resolve the three immutable `resources` entries separately into distinct origin/replay model directories, verifying every SHA256 before running. Pass the corresponding directory with `run --pretrained-path /path/to/bert-tiny`. The measured run copied already-downloaded, immutable resources into new directories and reverified their contents; it did not depend on an original model path or fetch unpinned weights. See `pretrained-resource-resolution.json` for the exact URLs, revision, hashes, byte counts and resolved paths. The same installed service/runtime wheels support both fixtures.
