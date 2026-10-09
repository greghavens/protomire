# Modelbuilder

A source-built Code - OSS model-development IDE, shared operation service, native
MCP server, and provisional ZenML 0.97.0 backend. The workbench is a permanent
built-in feature, not a chatbot or a replacement training framework.

## Implemented vertical slice

- One model project per workspace, with its research, data, training/evaluation
  recipe, runs, exports and native model-version lineage. Build the recipe or
  import an existing one; saved revisions retain one recipe identity.
- Typed DAG recipe editor plus JSON/native Monaco editing, revision history and
  explicit stale-write conflicts.
- One authenticated service dispatch for 49 operations used by both the IDE and
  the independent official-SDK MCP server. SQLite stores application/control
  records; ZenML owns execution, artifacts and model versions.
- Real custom PyTorch and pinned small pretrained-model CPU workloads, explicit
  frozen-reference KL settings, consumed datasets, checkpoint continuation,
  model comparison and validation-based selection.
- Content-addressed, inert portable recipe bundles; source/dependency validation;
  fail-closed permissions, unsupported targets and untrusted code execution.
- Actual source-built Linux IDE launch with Python, basedpyright diagnostics,
  debugpy, terminal, Git and Jupyter execution tests.

This is a development implementation. The [acceptance matrix](docs/ACCEPTANCE_MATRIX.md)
distinguishes verified results from remaining gates; no scaffold, simulated API,
policy unit test or earlier evaluation is counted as a GUI/backend acceptance pass.

## Start here

1. `./scripts/bootstrap.sh --backend` installs two deliberately separate Python
   environments and builds the workbench feature. Python 3.12.14 and the pinned
   Node toolchain are required.
2. Follow the [runbook](docs/RUNBOOK.md) to configure a local authorized actor and
   start the service/MCP server. No service token is embedded or generated for you.
3. `./scripts/build_ide.sh build` builds the pinned Code - OSS distribution with
   native OS build prerequisites. See [distribution instructions](distribution/README.md).
4. Connect the built-in workbench to the local service. Execution additionally
   requires operator approval for the repository's trusted CPU fixtures.
5. `MODELBUILDER_RUN_INTEGRATION=1 ./scripts/test_all.sh` runs contract, frontend,
   independent MCP and actual CPU/backend tests. GUI and clean-environment replay
   have separate explicit commands and evidence.

## Engineering references

- [Architecture](docs/ARCHITECTURE.md), [decisions](docs/DECISIONS.md), [backend decision](docs/BACKEND_DECISION.md)
- [Native MCP tools](docs/MCP_TOOLS.md), [OpenAPI](docs/openapi.json)
- [ZenML lifecycle and cancellation semantics](docs/ZENML_BACKEND.md)
- [Clean replay](docs/PORTABILITY.md)
- [Dependencies and licensing](docs/DEPENDENCIES_AND_LICENSES.md)
- [Acceptance matrix](docs/ACCEPTANCE_MATRIX.md)

No cloud/GPU spending, public release, Microsoft Marketplace redistribution,
production signing/updater, arbitrary untrusted-code execution, or modification
of the owner's existing model repositories is authorized or performed. Remote
execution and the broader product remain future milestones, not silently removed
requirements. See the matrix for exact assistant-extension compatibility gates.

First-party licensing has not been selected by the owner. Public source visibility
is not a grant of an open-source license. Retained third-party components keep
their own licenses and notices.
