# Modelbuilder

A source-built Code - OSS model-development IDE, shared operation service, native
MCP server, and provisional ZenML 0.97.0 backend. The workbench is a permanent
built-in feature, not a chatbot or a replacement training framework.

## Implementation status

The delivery target is the complete model-development product. It is **not yet
complete**. P0–P4 was the initial integration sequence, not the final scope or a
reason to defer the remaining requirements. The implementation below is real;
test fixtures and accounting simulations do not establish production acceptance.

Linux delivery for Bazzite is a **Flatpak**, with the mandatory MCP service bundled.
It is not built or installation-accepted yet; see [delivery gates](distribution/FLATPAK.md).

- One model project per workspace, with its research, data, training/evaluation
  recipe, runs, exports and native model-version lineage. Build the recipe or
  import an existing one; saved revisions retain one recipe identity.
- Typed DAG recipe editor plus JSON/native Monaco editing, revision history and
  explicit stale-write conflicts. Nested conditions and bounded native iteration
  preserve canonical node identity and per-invocation artifact ancestry.
- One authenticated, catalog-generated service dispatch used by both the IDE and
  the independent official-SDK MCP server. SQLite stores application/control
  records; ZenML owns execution, artifacts and model versions.
- Reusable installed Transformers Trainer, generation, classifier-evaluation and
  export components with pinned user assets and explicit resource budgets.
- Real custom PyTorch and pinned small pretrained-model CPU workloads, explicit
  frozen-reference KL settings, consumed datasets, checkpoint continuation,
  model comparison and validation-based selection.
- Content-addressed, inert portable recipe bundles; source/dependency validation;
  fail-closed permissions, unsupported targets and untrusted code execution.
- A packaged repository-migration skill maps existing source, research, datasets,
  checkpoints and evaluations without replacing the training framework or silently
  uploading artifacts. Its bounded inventory helper is read-only; adoption still
  requires the selected native validators and guardrails.
- Configurable functionality-family policies distinguish final data composition,
  optimizer-committed checkpoint exposure and per-family evaluation.
  [Supported modes and limits](docs/functionality-family-coverage.md) are explicit.
- Streaming/sharded CSV, JSONL and optional Parquet datasets with immutable
  indexes, bounded previews and actual-consumption lineage.
- Reviewed local PDF/OCR ingestion with page hashes and exact citation offsets.
- Source-bound framework components, immutable dataset versions, live checkpoint
  evaluation and notifications, private recipe discovery/versioning, and scheduled
  research watches. Each has its own capability and verification limits.
- Optional Codex, Claude Code, Triggerfish, OpenCode and Kilo Code harness choices.
  OpenCode/Kilo have reviewed project-local skill/MCP configuration support; vendor
  installation and upstream acceptance remain separate [gates](docs/OPENCODE_KILOCODE.md). Triggerfish has a
  locally tested ModelBuilder-side flat MCP adapter and reviewed inactive
  project setup; upstream execution and complete harness acceptance remain
  unverified. The adapter is included in the current unsigned Mac development
  app; gateway-port, command-allowlist, provider and native GUI gates remain.
- Linux-only Moonshiner 0.8.23 local composition of registered, reviewed traces,
  verified through the native ZenML workflow and explicit prepared-dataset
  registration/binding. Final-target metadata and inherited rights are preserved.
  New provider trace generation and token-level training masks are not supplied
  by this adapter.
- Historical source-built Linux IDE launch with Python, basedpyright diagnostics,
  debugpy, terminal, Git and Jupyter execution tests. Current native GUI acceptance
  is recorded separately from those earlier passes.

This is a development implementation. The [acceptance matrix](docs/ACCEPTANCE_MATRIX.md)
distinguishes verified results from remaining gates; no scaffold, simulated API,
policy unit test or earlier evaluation is counted as a GUI/backend acceptance pass.

## App installation

The required product delivery is one desktop installation: AppImage on Linux,
a DMG containing the app on macOS, and a native Windows installer. The native
MCP server is mandatory and managed by the app alongside its service and isolated
runtime payloads. It is not an optional plugin or a separate end-user setup step.

An unsigned arm64 Mac development DMG has passed mounted runtime/MCP checks
and actual bundled MLX/MPS tests on the build host. Graphical acceptance,
signing/notarization and clean-machine testing remain open. Linux/Windows
installer delivery is unfinished; there is no verified downloadable release. See
[packaging and installation status](distribution/INSTALLATION.md).

## Developer source build

The following commands are for development and testing, not the intended user
installation experience.

1. `./scripts/bootstrap.sh --backend --trainer` installs two deliberately separate Python
   environments and builds the workbench feature. Python 3.12.14 and the pinned
   Node toolchain are required.
2. Follow the [runbook](docs/RUNBOOK.md) to configure a local authorized actor and
   start the service/MCP server. No service token is embedded or generated for you.
3. `./scripts/build_ide.sh build` builds the pinned Code - OSS distribution with
   native OS build prerequisites. See [distribution instructions](distribution/README.md).
4. Connect the built-in workbench to the local service. Execution additionally
   requires explicit trust and a configured source binding for the model project;
   see [project-source setup](docs/PROJECT_SOURCE_WORKSPACES.md). The included CPU
   examples are test workloads, not a restriction to those model architectures.
5. `MODELBUILDER_RUN_INTEGRATION=1 ./scripts/test_all.sh` runs contract, frontend,
   independent MCP and actual CPU/backend tests. GUI and clean-environment replay
   have separate explicit commands and evidence. On Linux, the full integration
   suite also requires Poppler (`pdfinfo`, `pdftotext`, `pdftoppm`) and Tesseract
   with English language data for real PDF/OCR acceptance.

## Engineering references

- [New project and project structure](docs/PROJECT_QUICKSTART.md)
- [Hub repository interface and transfers](docs/HUB_TRANSFERS.md)
- [Exact local asset and source rights review](docs/LOCAL_RIGHTS_REVIEW.md)
- [Reproducible scientific operations](docs/SCIENTIFIC_WORKFLOWS.md)
- [Training runner compatibility](docs/TRAINING_RUNNERS.md)
- [Moonshiner local composition and provider-generation gates](docs/MOONSHINER_DATASETS.md)
- [Research retrieval and harness context](docs/RESEARCH_CONTEXT.md)
- [Triggerfish adapter, inactive setup and remaining gates](distribution/TRIGGERFISH_COMPATIBILITY.md)

- [Architecture](docs/ARCHITECTURE.md), [decisions](docs/DECISIONS.md), [backend decision](docs/BACKEND_DECISION.md)
- [Bounded native pipeline iteration](docs/NATIVE_BOUNDED_ITERATION.md)
- [Canonical stage-concurrency settings](docs/NATIVE_PARALLEL_STAGES.md)
- [Native MCP tools](docs/MCP_TOOLS.md), [OpenAPI](docs/openapi.json)
- [ZenML lifecycle and cancellation semantics](docs/ZENML_BACKEND.md)
- [Local device placement and hardware-verification limits](docs/LOCAL_DEVICE_PLACEMENT.md)
- [Reusable Transformers training/evaluation/export](docs/TRANSFORMERS_TRAINER.md)
- [Optional isolated MLX/Llama setup, bounds and verification limits](docs/MLX_COMPONENTS.md)
- [General local generation and judging](docs/TRANSFORMERS_INFERENCE.md)
- [Run admission, durable dispatch and retry semantics](docs/admission-dispatch.md)
- [Clean replay](docs/PORTABILITY.md)
- [Dependencies and licensing](docs/DEPENDENCIES_AND_LICENSES.md)
- [Acceptance matrix](docs/ACCEPTANCE_MATRIX.md)

No cloud/GPU spending, public release, Microsoft Marketplace redistribution,
production signing/updater, arbitrary untrusted-code execution, or modification
of the owner's existing model repositories is authorized or performed. Remote
execution, broad component coverage, verified isolation and remaining platform
capabilities are outstanding implementation/acceptance requirements, not completed
features. See the matrix for exact assistant-extension compatibility gates.

First-party licensing has not been selected by the owner. Public source visibility
is not a grant of an open-source license. Retained third-party components keep
their own licenses and notices.
