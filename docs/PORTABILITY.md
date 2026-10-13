# Portable recipe replay (T19)

`scripts/replay_bundle.py` separates three boundaries:

1. `export` verifies selected source against an existing Git commit and produces an integrity-checked recipe archive.
2. `import` validates and extracts only declared, hash-checked text into a new directory. `wheel` packages those bytes as ordinary wheels without importing bundled modules or running build hooks. Neither installs dependencies, fetches resources, or executes model code.
3. `prepare` imports into a new receiving project and records a non-executing plan. `plan` rechecks that preparation. `run-prepared --trust-repository-source` separately submits an exact current reviewed revision/digest through the normal service. Source trust never grants asset rights. The original `run` convenience command still creates a fresh project, and now records a structured blocked result before submission when rights are missing. None of these commands is an untrusted-code sandbox.

The archive includes the full application Python source, both base dependency locks, all target-specific and optional profile locks, `pyproject.toml`, and the replay command. No original absolute paths, datasets, model weights, secrets, installed environments, or `.pth` path-sharing files are included. Git revision plus per-file SHA256 identify the source; a bundle hash alone does not authenticate its publisher.

## Receiving-project rights review and replay

An unreviewed custom fixture now blocks in `run.plan` before launch: its connected builtin dataset/checkpoint outputs would carry unresolved local-origin lineage. A synthetic label, repository ownership claim, source-trust flag, or a policy object inside recipe extensions does not authorize use. Public pretrained resources still require exact reviewed license evidence for every declared upstream file. `prepare` and `plan` never submit, grant rights, download resources, or launch native workers; `BLOCKED` is a useful preview result, not execution success.

```sh
SERVICE_PYTHON -I SOURCE/scripts/replay_bundle.py prepare SOURCE \
  --data-dir NEW_DATA --backend-python BACKEND_PYTHON \
  --trust-repository-source --output prepared.json
SERVICE_PYTHON -I SOURCE/scripts/replay_bundle.py plan prepared.json \
  --trust-repository-source --output plan.json
# Only after a separate authorized review in the SAME receiving project:
SERVICE_PYTHON -I SOURCE/scripts/replay_bundle.py run-prepared prepared.json \
  --expected-revision REVIEWED_REVISION --expected-digest REVIEWED_DIGEST \
  --trust-repository-source --output execution.json
```

Use `execution_revision` and `execution_digest` from a fresh executable plan. Preparation records project, recipe, source, interpreter and store identity in that receiving store; editing the JSON receipt cannot supply policy authority. Reopening checks the original immutable transport and allows only explicit rights/usage-context changes to the owned recipe. Source, graph, environment, parameters or other changes require a new preparation. Normal submission checks current project policies, exact source closure, use context and revocation again, including at native dispatch. The CLI actor retains only read/modify/execute scopes.

Both committed checkouts and explicitly registered Git-free imported snapshots use the same public operations: `project.source.bind`, `hub.license.local.inspect` (every inventoried source file plus the actual selected terms), `hub.license.review` (an independently authorized publish-scope reviewer explicitly supplies terms, uses, context and obligations), then `hub.source.policy.bind`. Configure the receiving service's source alias and project-specific Hub publication source alias to the same exact root. This is local evidence review; it does not publish to a provider. Only receiving-project policies and future recipe lineage change; historical revisions remain intact.

### Register and review an imported snapshot

In the native app, **Register Trusted Project Source** asks explicitly whether the selected folder is a Git checkout or an imported snapshot. Choose **Imported snapshot** for a directory produced by bundle import. The native confirmation identifies the source kind and explains the separate source-trust decision; registration never infers ownership or license rights. Configure that same folder as a project Hub source, then use **Review local source, model or dataset rights → Source origin for scratch outputs**. The guided review displays imported byte evidence, local identity, snapshot digest, original declared commit, exact receiving recipe revision/digest and selected terms before the existing exact native owner approval.

An operator-configured alias uses `kind: "imported_snapshot"` in `source_roots`; the default remains `git`, with no automatic Git-free fallback. `prepare` returns `source_review_setup` as configuration guidance only, with `trusted: false`. It neither applies those settings nor records a policy. An imported alias requires a validated extracted `recipe.json`, a nonempty hash-pinned source inventory covering every implementation/lock/profile reference, ordinary non-symlink paths and a nonzero declared upstream commit. Inspection verifies the actual current bytes and binds the local root identity; it does not verify an upstream Git object or authenticate the publisher.

After explicitly binding the alias, agents and humans capture the same review evidence using `hub.license.local.inspect` with `repo_type: "source"`, `source_repository_id`, `source_commit`, all source `files`, explicit `evidence_document_paths`, and these mandatory snapshot guards:

- `recipe_id` and `expected_revision`: the receiving project's current owned recipe
- `expected_recipe_digest`: its exact current digest
- `expected_project_revision`: the exact project revision after source binding

The service captures the original transport recipe digest, snapshot digest and selected file hashes itself; these are not caller-supplied policy authority. Changes to the project, recipe revision/digest, snapshot directory, source bytes or selected terms between capture, owner review and binding reject the decision. A source-origin policy cannot register a model/dataset asset. Missing or unknown terms remain unresolved; no permissions are inferred from a label, folder, source-trust flag, transport digest or policy JSON in a bundle.

At planning, submission, launch and native dispatch, current policies/revocation, snapshot identity, current source and selected terms are checked again. Replacing a directory with byte-identical copied files does not inherit its prior local binding. Ordinary recipe parameter edits after binding retain the reviewed source origin when the receiving project/recipe identity, source closure and licensed context remain unchanged, matching Git source review. Context changes or source replacement require the applicable new review. Reopening a preparation still permits only rights/context changes under that preparation's original replay contract; other recipe edits require a new preparation.

Do not write policy rows by hand or import policy JSON. The CLI actor still has no publish scope; source trust is never license clearance, and native review remains the existing owner-approved exact request.

Retries for the same preparation + execution revision + digest reconcile the same service idempotency key. Receipts state whether that operation was already recorded before the submit call. An explicitly selected later reviewed recipe revision has a different key; uncertain retries never silently choose a new one. Failures at preparation, import, plan or submit write the requested JSON, including the error and transcript. A submit exception records `submit_attempted: true`, `submitted: null` until acknowledgment, and any service operation ID/dispatch state; it never claims an uncertain launch did not happen. Timeout receipts preserve cancellation/termination uncertainty.

Strict output comparison includes provenance bytes. Independently created reviews can have different policy IDs and timestamps, so a successful authorized run does not by itself establish byte-for-byte equivalence with another independently reviewed run. Historical exact replay evidence used explicitly test-owned matching provenance, and is not new user or third-party rights approval.

## One model project, one recipe identity

The replay creates a model project, reads its already-owned recipe, and imports the portable training/evaluation pipeline as the next immutable revision of that same recipe. It sends `bundle_json` through the strict raw-JSON operation contract with `expected_revision`. Import remains inert; the explicit run action executes the selected current revision after independent receiving-project rights review.

The harness verifies that the project retains its recipe ID, exactly one recipe remains listed, the original revision is unchanged, and execution provenance identifies the exact selected execution recipe ID, revision and digest while retaining the imported identity separately. Origin and replay have different project IDs because their application/native stores are independent; each project has one stable recipe identity throughout its own lifecycle. The `model.initialize` node explicitly selects the custom or pretrained fixture. Dataset compatibility is validated separately.

## Why there are two environments per execution

The service and ZenML have independently pinned dependency graphs. In the measured locks, FastAPI, Pydantic and JSONSchema versions differ. Combining these locks or ignoring a failed dependency check would not be a reproducible deployment.

- Service wheel: `modelbuilder`, all application Python source, service dependency metadata.
- Execution wheel: `modelbuilder-runtime`, the same snapshot's backend and remote packages plus their shared recipe, execution, iteration, dataset/streaming, Parquet, evaluation, tracking, model/store and source-contract modules, with backend dependency metadata. Top-level `modelbuilder/service.py` and service-only research/document ingestion remain excluded.

These wheels are installed into separate environments. No implementation is duplicated or changed by packaging. The standard-library wheel writer emits deterministic ZIP members, `METADATA`, `WHEEL`, and a valid hashed `RECORD`; no setup hooks or bundle-supplied packaging commands run. Native SDK/worker processes use the runtime environment's installed packages and an explicitly supplied extracted source directory for source-integrity verification.

## Reproduce

Run from a committed checkout with Python 3.12.14 and `uv` installed. The export commit must contain every file returned by `source_inventory`, including the replay script, all packaged application modules, `pyproject.toml`, and base/profile lockfiles. Dirty or missing source fails export. Use the shipping recipe's runtime commit rather than assuming that the checkout's HEAD identifies the measured runtime; later test/documentation commits do not change that pin.

The [reproduction script](../evidence/product-completion-20261013/portability-0e82548/replay-exact-runtime.sh) builds both fixtures, independently builds each pair of wheels, runs installed-wheel import/PDF probes, and verifies source bytes and native termination afterward. It requires a prepopulated dependency cache, separately resolved immutable pretrained resources, pinned PyArrow in the existing test environment, and local Poppler tools for the PDF probe. All dependency installs in the measured run were offline. For a rerun, set `MODELBUILDER_PORTABLE_WORK` to a new absolute path; evidence defaults to its new `evidence` subdirectory, or a new absolute `MODELBUILDER_PORTABLE_EVIDENCE` path. The script refuses existing work/evidence directories and does not overwrite the saved acceptance receipts.

The corrected harness first runs the complete portability suite with a fresh XML/log and records the actual command, exit status, timestamps and XML SHA256. It derives test counts from that XML and rejects missing, empty, malformed, failing or unexpectedly skipped results. The equivalent explicit test command is:

```bash
MODELBUILDER_REQUIRE_PARQUET=1 PYTEST_ADDOPTS= .venv/bin/python -m pytest \
  tests/portability -q -o addopts= --junitxml="$NEW_EVIDENCE/final-portability.xml"
```

Here `NEW_EVIDENCE` must identify a new output directory. The harness checks its actual exit status before replay and verifies the matching XML receipt before writing a summary; it never substitutes the historical 182-test count. JUnit does not encode deselections, so regenerated summaries record the complete-directory command rather than inventing a deselection count. The earlier 182-test result remains historical; this exact-runtime replay measured 224 actual tests.

The separately captured `final-offline-installation.json` inventory is original-run evidence and is not regenerated by the reproduction script. Reruns retain fresh `uv` synchronization, installation and dependency-check logs under the new work directory, plus distribution inventories in the actual origin/replay records; a regenerated summary explicitly notes this structured-inventory limitation.

The following Linux x64 walkthrough prepares blocked shipping-fixture previews; it does not claim a reviewed replay succeeds. It uses the same hash-locked service/backend profiles and optional Trainer overlay. It requires those packages in the local cache. Paths are local CLI arguments, not manifest fields.

```bash
set -euo pipefail
unset PYTHONPATH VIRTUAL_ENV
ROOT="$PWD"
COMMIT="$(.venv/bin/python -c 'import json; print(json.load(open("examples/custom-pytorch/recipe.json"))["source"]["commit"])')"
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
  uv pip sync --offline --python "$name/bin/python" --require-hashes \
    "$WORK/origin-source/requirements/profiles/service-linux-x64.lock"
  uv pip install --offline --python "$name/bin/python" --no-deps "$WORK/wheels/modelbuilder-0.1.0-py3-none-any.whl"
  uv pip check --python "$name/bin/python"
done
for name in .venv-portability-origin-backend .venv-replay-backend; do
  uv venv --python 3.12 "$name"
  uv pip sync --offline --python "$name/bin/python" --require-hashes \
    "$WORK/origin-source/requirements/backend.lock" \
    --extra-index-url https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match
  uv pip install --offline --python "$name/bin/python" --require-hashes \
    --only-binary :all: --no-deps -r "$WORK/origin-source/requirements/profiles/trainer.lock"
  uv pip install --offline --python "$name/bin/python" --no-deps "$WORK/wheels/modelbuilder_runtime-0.1.0-py3-none-any.whl"
  uv pip check --python "$name/bin/python"
done

# -I ignores caller PYTHONPATH/user site; neither environment is editable.
.venv-portability-origin/bin/python -I "$WORK/origin-source/scripts/replay_bundle.py" prepare \
  "$WORK/origin-source" --data-dir "$WORK/origin-data" \
  --backend-python "$ROOT/.venv-portability-origin-backend/bin/python" \
  --trust-repository-source --output evidence/portability/custom-origin-prepared.json
.venv-replay/bin/python -I "$WORK/replay-source/scripts/replay_bundle.py" prepare \
  "$WORK/replay-source" --data-dir "$WORK/replay-data" \
  --backend-python "$ROOT/.venv-replay-backend/bin/python" \
  --trust-repository-source --output evidence/portability/custom-replay-prepared.json
# These previews remain blocked until the separate receiving-project owner review above.
# Compare only actual successful execution receipts, never preview receipts.
```

Directories must be new; import and run intentionally refuse to overwrite them. Source packaging is inert, but installing even a reviewed wheel and executing Python require the operator's authorization. This example is local CPU only and creates no cloud resources.

## Current source checkpoint

Both shipping manifests pin `0e825487ef94fa613e1606f458177ad009b1a5cf` with
**114 source/lock files**, committed in candidate
`5b38d9602b6f8ae36fc0dcaf28f0a6da8c8f7d4a`. The closure includes bounded native
stage concurrency, isolated MLX components and their package-data profile,
verified pretrained tokenizer assets, bounded iteration, streaming datasets,
Trainer/inference adapters and service PDF ingestion. The backend wheel preserves
its independent dependency graph.

The [current compact replay receipt](../evidence/product-completion-20261013/acceptance-0e82548/linux-portability.json)
records four genuine CPU runs, four fresh non-editable service/backend
environments, four extracted source trees and independent application/native
stores:

- Custom PyTorch: **35/35 comparison checks**, exact hashes for **8 outputs**.
- Pretrained-small: **35/35 comparison checks**, exact hashes for **12 outputs**.
- All **114 files** match Git before/after execution; installed wheel bytes remain
  unchanged and the working-tree runtime still matches the pin.
- All four workers were reaped, their groups terminated and supervisors reconciled.
- The complete portability suite actually executed **224 tests, zero skips**.
  Counts and command come from fresh JUnit/log/exit receipts, not historical totals.

Fresh installs were offline and hash-locked, including the backend Trainer
overlay with `--no-deps`; dependency checks passed. Installed-wheel probes
imported the runtime modules and adjacent MLX profile, and both service wheels
executed actual self-authored PDF extraction. The original structured
`final-offline-installation.json` inventory was not regenerated; fresh install
logs and runtime distribution inventories support this run. Linux MLX probes are
inert import/package checks, not hardware execution.

Separate [Mac runtime evidence](../evidence/product-completion-20261013/acceptance-0e82548/mac-runtime.json)
verifies this exact candidate's installed service wheel and an independent
34-package MLX environment. Full/LoRA exports each reload with zero maximum
logit difference across 48 logits. This is a distinct hardware/runtime gate,
not a broad cross-platform recipe replay or single-app installer claim.

## Evidence and limits

The comparison requires exact deterministic metrics, dataset SHA256 and every output-file SHA256; distinct backend run IDs; different service/runtime environment prefixes; own installed module paths; no original source/environment imports; no shared `.pth` paths; identical Python/package versions; independent ZenML stack, artifact-store and metadata-store identities; and preservation of each project's single recipe identity and immutable revision provenance. Raw run evidence records actual `sys.path`, module files, installed distributions, native artifact UUIDs, recipe identity history and source hashes.

`tests/portability/test_replay_bundle.py` verifies inert deterministic packaging, tamper rejection, separate dependency metadata, explicit trust, timeout cancellation through the real operation schema, retained failure evidence, shipping manifests' complete source closure, static closure of internal imports including lazy imports, installed-path helper execution, and rejection of contaminated or drifting replay evidence. Those unit tests alone do not establish T19. The [current compact replay receipt](../evidence/product-completion-20261013/acceptance-0e82548/linux-portability.json)
retains both complete 35-check comparisons, matching output hashes, before/after
source verification and worker-termination proofs. [Original receipt hashes](../evidence/product-completion-20261013/acceptance-0e82548/source-receipt-sha256.json)
identify the locally retained raw evidence without exposing personal paths or
model weights.

For native application assembly, `wheel --target <native-target>` explicitly
resolves direct dependency metadata from the corresponding inventoried target
lock. `--backend-variant cuda-cu128` selects a reviewed backend variant when
needed. The target, lock SHA256 and resulting pins are recorded in the wheel and
build receipt. Default wheel construction without `--target` retains the exact
recipe requirements and the existing replay behavior. These target-specific
metadata checks do not extend the measured same-host replay or native acceptance
claims. See [native wheel inputs](../distribution/INSTALLATION.md).

The replay scope is independent Python environments and metadata/artifact stores on the same Linux x64 CPU host. It does not verify macOS, MLX, another operating system or machine, GPUs, remote execution, per-stage container isolation, or untrusted recipe execution. The native replay workloads are the custom-PyTorch and pretrained-small fixtures; installing the Trainer overlay and importing its adapters does not establish general Trainer training acceptance. Optional PyArrow was absent from the fresh offline replay environments; eager and streaming Parquet helper subprocess tests ran separately in the existing pinned PyArrow environment. PDF tool versions are recorded by the installed-wheel probes, but Poppler is a local system prerequisite rather than a Python wheel dependency.

Source license `NOASSERTION` remains a visible unresolved redistribution issue; local operator trust does not confer redistribution rights. External pretrained resources must remain immutable references and be separately resolved and SHA256-verified rather than embedded in the recipe archive. The transport digest remains separate from each project's owned recipe identity and exact imported revision.

### Historical evidence

Earlier results, including the `086908e` checkpoint and the directories named [current-runtime](../evidence/portability/current-runtime/) and [final-runtime](../evidence/portability/final-runtime/), describe their own older source snapshots. Their directory names do not make them current acceptance evidence. The immediately preceding [49c0f34 replay](../evidence/product-completion-20261012/portability/historical-49c0f34/summary.json) also passed its comparisons but is explicitly marked historical because a later control-status correction changed the runtime. The [d51a90d / 106-file replay](../evidence/product-completion-20261012/portability/summary.json) is also historical: it measured 182 portability tests. None substitutes for the current `0e82548` / 114-file evidence above.

### Pretrained replay

Use `examples/pretrained-small/recipe.json` in the export step and new extracted source/data directories. Resolve the three immutable `resources` entries separately into distinct origin/replay model directories, verifying every SHA256 before running. Pass the corresponding directory with `run --pretrained-path /path/to/bert-tiny`. The measured run copied already-downloaded, immutable resources into new directories and reverified their contents; it did not depend on an original model path or fetch unpinned weights. The current raw resource-resolution record is retained locally and identified by SHA256 in the compact original-receipt index; resolved host paths are not published. The same installed service/runtime wheels support both fixtures.

## Isolated snapshot-intake regression

`tests/hub/test_snapshot_source_review.py` exercises public-API capture/review/bind with synthetic terms, restart, exact review revision/digest guards, root substitution and symlinks, byte/terms tampering, native owner approval, parameter-only edits and execution revocation. `tests/distribution/test_managed_core.py` also checks the real native registration HTTP endpoint and persisted source identity. Frontend tests cover explicit kind selection and guided dialog contracts; these are not live native-GUI acceptance.

For an actual offline CPU reproduction, provide existing pinned Python 3.12 Linux service/backend dependency environments and a new work directory:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python \
  tests/portability/reproduce_snapshot_review.py /new/absolute/snapshot-check \
  --service-dependencies /existing/pinned-service-env \
  --backend-dependencies /existing/pinned-backend-env
```

The script creates an isolated authored test commit and archive, extracts two Git-free trees, constructs and installs conventional wheels, and uses two independent receiving stores and explicit self-authored synthetic-fixture reviews through the public API. No shipping manifest, existing policy, dependency template, provider or network service is changed. Dependency files may share immutable hardlinks, as with an ordinary package cache; imports and application/native stores remain independent. Logs, exact commands, wheel hashes and raw execution receipts are retained. It requires local `git`, `uv`, Python 3.12 and the already installed dependencies; it downloads nothing.

A successful scientific comparison requires seven identical metrics/weight/loader/parity outputs and three different manifests carrying independent review provenance. The ordinary strict comparison is preserved and must fail only its all-output-hashes check. Distinct review IDs/timestamps are never normalized away or reported as byte-identical. This regression is synthetic same-host CPU evidence for the additive intake, not shipping release, native installer, cross-platform, GPU or redistribution acceptance.
