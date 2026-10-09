# ModelBuilder development runbook

This is a local, single-researcher vertical slice. The operation service is authoritative for projects, canonical recipes, research, policies, revisions, and audit history. ZenML 0.97.0 owns native runs, artifacts, and model versions. The built-in Code OSS workbench and standalone MCP server both call the same authenticated service.

Use [ACCEPTANCE_MATRIX.md](ACCEPTANCE_MATRIX.md) for current gate results. A successful CPU demo or generic MCP client does not establish production readiness, cloud cost enforcement, or Claude/Codex extension compatibility.

## 1. Prerequisites and installation

Run commands from the repository root. Verified service and backend interpreter: **Python 3.12.14**. Python 3.12 is required; the service and backend use separate virtual environments because their pinned upstream dependency versions differ. Git is needed for immutable source verification.

```bash
bash scripts/bootstrap.sh
bash scripts/bootstrap.sh --backend
```

The first command creates `.venv`, installs `requirements/service.lock`, installs the repository package editable without resolving additional dependencies, checks Python dependencies, and type-checks/builds the workbench TypeScript when its lockfile is present. The second additionally calls `scripts/bootstrap_backend.sh`, which requires the official `uv` executable, installs the hash-locked CPU backend into `.venv-backend`, and downloads the pinned pretrained fixture. Neither command provisions credentials, accounts, paid compute, or remote targets.

Backend-only installation, after service setup:

```bash
PATH="$PWD/.venv/bin:$PATH" bash scripts/bootstrap_backend.sh
```

The backend lock pins ZenML **0.97.0**, CPU PyTorch, transformers, and their dependencies. `scripts/download_model.py` verifies the pinned `prajjwal1/bert-tiny` files and SHA256 values before storing them under `.cache/models/bert-tiny`. Training runs use offline Hugging Face/transformers settings. Install/download network access is distinct from permission to execute untrusted code.

The workbench package supports Node 20 or newer. The Code OSS build uses its own checksum-pinned **Node 22.19.0** toolchain; do not substitute an arbitrary global Node version for that build.

## 2. Configure identity and start the service

There is **no default password, anonymous resource access, or automatically issued administrator credential**. Supply an operator-approved token through the process environment. Do not put tokens in recipes, Git, screenshots, terminal transcripts, or JSON settings. The service does not persist bearer tokens in its database.

For a local owner identity, enter the token without recording its value in shell history:

```bash
read -rs -p 'Approved local service token: ' MODELBUILDER_TOKEN
printf '\n'
export MODELBUILDER_TOKEN
export MODELBUILDER_ACTOR='local-owner'
export MODELBUILDER_DB_PATH="$PWD/.runtime/modelbuilder/service.sqlite3"
export MODELBUILDER_SOURCE_ROOT="$PWD"
export MODELBUILDER_BACKEND_PYTHON="$PWD/.venv-backend/bin/python"
export MODELBUILDER_HOST='127.0.0.1'
export MODELBUILDER_PORT='8765'
.venv/bin/modelbuilder-service
```

`python -m modelbuilder.api` is equivalent. The single-token configuration grants `read`, `modify`, and `execute`; it does not enable remote spending or publication. `MODELBUILDER_SOURCE_ROOT` is the reviewed checkout used for recipe source verification and explicit source-file exports. Keep the service bound to loopback for this development setup.

For separate scoped identities, provide `MODELBUILDER_TOKENS_JSON` through an approved environment/secret provisioning mechanism instead. Its format is a JSON object mapping each token to `{ "id": "actor-name", "scopes": ["read"], "project_ids": ["actual-project-uuid"] }`. `project_ids` is optional: omitting it permits the actor's scopes across projects; an empty array permits no projects. Project-scoped actors cannot create new projects. Use an unscoped `read,modify,execute` identity for authorized project creation.

Unset `MODELBUILDER_TOKEN` when using only the scoped JSON configuration; otherwise that token also remains an owner credential. Environment configuration is read at service startup. To revoke or rotate a configured token, change the approved configuration and restart the service, then reconnect the relevant client. Do not log the JSON configuration.

Health and contracts:

```bash
curl --fail http://127.0.0.1:8765/health
```

`/health` is intentionally unauthenticated and reports whether authentication is configured. Resource routes and `/v1/catalog` require bearer authentication. `/openapi.json` contains generated operation-specific input schemas. The contract files are [openapi.json](openapi.json), [operation-catalog.json](operation-catalog.json), and [MCP_TOOLS.md](MCP_TOOLS.md).

Defaults when not explicitly configured:

| Variable | Default / purpose |
|---|---|
| `MODELBUILDER_DB_PATH` | `.modelbuilder/modelbuilder.sqlite3` |
| `MODELBUILDER_HOST`, `MODELBUILDER_PORT` | `127.0.0.1`, `8765` |
| `MODELBUILDER_BACKEND` | `zenml`; another value disables backend creation |
| `MODELBUILDER_BACKEND_PYTHON` | `.venv-backend/bin/python` under the source checkout |
| `MODELBUILDER_SOURCE_ROOT` | Explicitly configure for reproducible execution/export |
| `MODELBUILDER_ACTOR` | `local-owner` for the single-token configuration |

Keep a separate data-directory parent for independent service instances. SQLite and its WAL contain application records; the adjacent `backend/` directory contains the supervisor journal, ZenML configuration, metadata, and artifacts. Neither directory belongs in Git. A backup must include both application and backend state; copying only the application SQLite file does not copy models or native lineage.

## 3. Run the verified CPU demo

Leave the service running. In another terminal, make the same approved token available as `MODELBUILDER_TOKEN`. The default demo creates a project, recipe, citation, and proposed hypothesis without training:

```bash
.venv/bin/python scripts/demo.py --url http://127.0.0.1:8765
```

Explicitly execute the repository-owned custom CPU fixture:

```bash
.venv/bin/python scripts/demo.py --url http://127.0.0.1:8765 --fixture custom-pytorch --run
```

Or run the pinned pretrained CPU fixture after its verified download:

```bash
.venv/bin/python scripts/demo.py --url http://127.0.0.1:8765 --fixture pretrained-small --run
```

Each invocation creates a new project using a unique slug. `--run` explicitly supplies fixture trust consent and polls until a terminal state; submission alone is never presented as training success. The JSON result contains native run/model/artifact IDs, measured metrics, and the canonical recipe digest. The demo's research hypothesis describes a future KL comparison; a single demo run does **not** complete that comparison or automatically accept the hypothesis. The full comparison/decision/promotion workflow is exercised by the service integration suite.

The fresh-service custom demo was actually run for this delivery: ZenML run `b5155888-8663-4f3b-a5f3-74c09650771b`, five native artifacts, validation loss `0.24631698429584503`. [Sanitized result](../evidence/demo/result.json), [full result](../evidence/demo/stdout.json), and [service log](../evidence/demo/service.log) retain the evidence. That verification used a disposable process-only test identity, which was not saved. No persistent credentials or remote resources were created.

## 4. Connect the permanent Code OSS workbench

Build the Linux x64 development distribution:

```bash
bash scripts/build_ide.sh build
.runtime/bin/modelbuilder-ide /absolute/path/to/model-workspace
```

The build verifies the pinned Code OSS source and Node toolchain, uses upstream lockfiles, compiles the permanent built-in feature, and permits only the documented product metadata overlay. See [distribution/README.md](../distribution/README.md) for native dependencies, extension hashes, optional packaging, and license boundaries. `prepare` fetches/verifies source without building; `compile` reuses installed build dependencies; `package` attempts the separate upstream Linux packaging step. A development launch does not prove production packaging, signing, or updater support.

In the actual IDE:

1. Open the intended repository and review the regular workspace-trust prompt. Do not trust an unfamiliar workspace merely to enable execution.
2. Run **ModelBuilder: Configure Service Connection** from the Command Palette.
3. Enter `http://127.0.0.1:8765` and the approved scoped token. The token is bound to that service URL in VS Code SecretStorage; it is not written into workspace settings or delivered to the webview. This configuration command intentionally stores the credential for future IDE use.
4. Open the **ModelBuilder** activity bar and **ModelBuilder: Open Model Workbench**, or create a project using **ModelBuilder: Create Model Project**.
5. Select a recipe to inspect its normalized JSON, visual nodes/edges, revision, and digest. **Open native JSON** opens the service-backed `modelrecipe:` document. Save with the revision you read; a conflicting GUI/MCP edit must produce an explicit conflict rather than overwrite it.
6. Use Research for evidence/hypotheses and Runs for actual IDs, metrics, artifacts, and cancellation status. Starting a run requires trusted workspace and explicit fixture execution consent.

HTTP is accepted only for loopback service URLs; non-loopback clients require HTTPS. URL changes do not silently reuse a credential bound to a different service. The visual graph preserves explicit arbitrary-Python node references; it does not reverse engineer arbitrary Python source into a graph.

## 5. Headless native MCP

The service remains running even when the IDE is closed. An external MCP client should start this stdio server:

```bash
export MODELBUILDER_API_URL='http://127.0.0.1:8765'
export MODELBUILDER_TIMEOUT='100'
.venv/bin/modelbuilder-mcp
```

The client process also needs `MODELBUILDER_TOKEN`. The executable reads/writes MCP protocol frames on stdio; launching it by hand is not an interactive shell or a proof that tools were called. `python -m modelbuilder.mcp_server` is equivalent. Use an absolute executable path when configuring an external client, and supply its environment through that client's supported credential mechanism. Do not paste a real token into a shared client configuration example.

Tool names replace operation dots with underscores, for example `project_create`, `recipe_update`, `run_submit`, and `run_status`. Schemas and permissions come directly from the service catalog. HTTP 409 appears as a structured MCP error preserving `http_status: 409`; reread and explicitly merge before retrying. Reuse an idempotency key only when retrying the exact same mutation. Never change payloads under an existing key.

Run the real independent SDK-client smoke test:

```bash
.venv/bin/python scripts/mcp_smoke.py --output evidence/mcp
```

It starts disposable HTTP/MCP processes, uses temporary scoped test tokens, records sanitized protocol traces, checks API/MCP equality and permission denials, races edits, and kills/restarts the service to verify persistence. This is genuine external MCP interoperability. It does not substitute for authenticated Claude Code or Codex operation in the exact IDE distribution; those integrations have separate acceptance results and redistribution conditions.

## 6. Lifecycle, cancellation, and recovery

All calls use `POST /v1/operations/{operation}` with the catalog's payload. Common rules:

- A project represents one model's lifecycle. Its recipes, research, native runs, and versions share its project identity.
- Mutations require `idempotency_key`; editable resources also require `expected_revision`. Resource IDs and project membership cannot be changed by a body or URL mismatch.
- `run.plan` is a read-only validation/preview. `run.submit` additionally requires `execute`, the current recipe revision, a permitted target, and explicit trust for verified repository-owned code.
- Edit node parameters in the canonical recipe. Nonempty top-level execution `parameters` are rejected, so an ignored override cannot masquerade as an executed experiment.
- `run.status` reconciles the authoritative backend; `run.logs` and `run.artifacts` return actual output. An application `operation_id` is a correlation, not a second native run ID.
- `run.cancel` requires `execute` and a fresh mutation key. `cancel_requested` does not mean the worker or its descendants have stopped. Inspect the reconciled terminal state and termination evidence.
- `run.resume` requires explicit `mode` (`exact_state` or `warm_start`), final total `steps`, trust consent, and a project-owned parent operation/run. It creates a new canonical continuation recipe and digest; the parent recipe remains unchanged. Exact-state claims require the deterministic tensor/optimizer/scheduler/RNG tests, not merely a successful resume request.
- A recorded decision identifies actual native run IDs, an immutable recipe revision, measurable selection rule, outcome, and rationale. Promotion checks the declared measured winner before changing the native stage and active project version.

Closing the IDE, stopping MCP, or stopping the HTTP service does **not** automatically cancel a detached training supervisor. Before planned shutdown, inspect active runs and explicitly cancel any that should stop. After restarting with the same database/backend directory, poll the same operation IDs to reconcile current state. Do not delete journals or relabel a locally unreachable process as cancelled.

The default local runtime ceiling is 600 seconds; policy can reduce it or raise it up to 3600 seconds. This is a process runtime limit, not an enforced financial spending cap. Remote spend, publication, and untrusted-code execution cannot be enabled by changing the policy document.

## 7. Tests and contract regeneration

Fast checks, shared service, security, external MCP, and TypeScript workbench checks:

```bash
bash scripts/test_all.sh
```

Real CPU and lifecycle tests, including the independent MCP client killing/restarting the HTTP service while a native job runs:

```bash
MODELBUILDER_RUN_INTEGRATION=1 bash scripts/test_all.sh
```

Targeted commands:

```bash
.venv/bin/python -m pytest tests/service/test_operations.py tests/mcp tests/contract -q
.venv/bin/python -m pytest tests/service/test_real_backend.py tests/integration/test_mcp_training.py -m integration -v
PYTHONPATH="$PWD/src" .venv-backend/bin/python -m pytest tests/integration/test_cpu_fixtures.py tests/integration/test_native_lifecycle.py -v
.venv/bin/python scripts/export_contracts.py --check
bash scripts/validate_artifacts.sh
```

`export_contracts.py` without `--check` regenerates OpenAPI, operation/MCP documentation, and domain schemas from the same implementation. Review and commit intentional generated-file changes together with the source change. Do not manually repair generated schemas to disguise a failing implementation.

The CPU fixture suite verifies tensor-level exact continuation separately from warm start, KL execution semantics, negative/failed experiments, and consumed dataset identity. The service integration verifies shared-operation IDs, continuation manifest identity, evidence/hypothesis/decision linkage, native promotion, and cross-project isolation. Read exact current counts from the JUnit/log evidence rather than interpreting a focused pass as an aggregate pass.

Real GUI verification is separate:

```bash
MODELBUILDER_GUI_PYTHON="$PWD/.venv/bin/python" bash scripts/smoke_ide.sh
```

Run it from a supported Linux desktop session, or a supported Xvfb host with working Electron sandbox prerequisites, with the pinned `ipykernel==6.30.1` dependency supplied by the service lock. This script must use the real editor, Extension Host, notebook, LSP, and debugger. Missing display/sandbox prerequisites are blockers; never add `--no-sandbox`, disable workspace trust, or change OS security policy to manufacture a pass. GUI unit/packaging tests alone do not establish GUI acceptance.

Recipe portability uses `scripts/replay_bundle.py` (`export`, `import`, `wheel`, `run`, `identity`, `compare`). Export requires a real Git commit whose inventoried source files exactly match the bytes being exported. Import and wheel assembly are inert; execution requires the explicit `--trust-repository-source` flag. Clean replay must use independently installed service/backend environments, source roots, metadata/artifact stores, and recorded module provenance. Another configuration pointing at the original editable installation is not a clean replay. See the portability evidence and acceptance matrix for the verified result and remaining platform limitations.

## 8. Troubleshooting and safe boundaries

| Symptom | Check / action |
|---|---|
| HTTP 401 or catalog unavailable | Confirm the service and client received the same approved token; configuration is loaded at startup. Do not print its value. Check `/health`'s `authentication_configured` field. |
| HTTP 403 on writes | Inspect the actor's scope/project list and project policy. A read-only token remains read-only through REST aliases and MCP. |
| HTTP 409 | Inspect `current_revision`, `current`, and the returned diff. Reload/merge. A changed mutation needs a new idempotency key. |
| `capability_unavailable` or missing interpreter | Install the pinned backend and set the absolute `MODELBUILDER_BACKEND_PYTHON`. Keep service and backend dependencies separate. |
| Source/dependency hash mismatch | Restore the reviewed committed bytes or intentionally create/review a new commit and repin/export. Never substitute an all-zero commit, suppress a hash check, or mark unresolved code trusted. |
| Pretrained fixture cannot load | Run the verified downloader in the backend environment; inspect exact filenames/hashes and configured source root. Training intentionally does not fetch arbitrary remote files. |
| Invalid recipe/typed edge | Use `recipe.validate` and its field errors; fix the canonical document. Cycles and unsupported schema versions are rejected. |
| Run failed before a native UUID appeared | Read `run.logs` and supervisor diagnostics. A process-start failure is a real failure, not a successful submission. Preserve the operation ID and logs. |
| Run remains unknown/cancel-requested | Reconcile after restoring the service/backend connection. Do not claim resources stopped without process/provider confirmation. |
| Stale GUI or dirty local document | Refresh the project/recipe. Preserve local edits and resolve the surfaced conflict before saving; polling must not overwrite dirty source. |
| Editor fails to launch | Read `evidence/gui/distribution/launch.log`; verify display, GTK/native libraries, and supported sandbox prerequisites. Report the blocker without weakening the sandbox. |
| MCP starts but shows no successful calls | Verify the independent-client trace, service URL, token scopes, and timeout. A process launch or tool list is not an execution acceptance result. |

This increment has no public recipe marketplace, paid remote provider, budget ledger/invoice reconciliation, automatic paper watch, enterprise tenancy, distributed training, or generic safe sandbox for untrusted Python. Custom Python remains a first-class recipe representation, but actual execution is limited to explicitly trusted, hash-verified operator-configured code or shipped fixtures. Import never grants execution trust. Models and datasets are not republished or mirrored into a new hosting service. No Biff repository is modified or trained.
