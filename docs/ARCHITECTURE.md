# Modelbuilder implementation architecture

## One authority for operations

The standalone FastAPI process exposes a catalog-generated operation API. OperationService.invoke validates payloads, authenticates the transport-provided scoped actor, checks project ownership and revisions, deduplicates mutation requests, dispatches, and records audits. The HTTP adapter and official-SDK stdio MCP adapter are thin transports. The IDE host calls HTTP; its webview never receives tokens or a direct ZenML client.

SQLite holds model-projects, immutable recipe revisions, evidence/claims/hypotheses/decisions, policy and operation correlation records. Model projects represent one continuing model. A process-control journal in the backend records supervision ownership; it is not another authoritative training registry. ZenML owns native pipeline-run, step, artifact and model-version UUIDs. Responses expose native identities and reconciliation timestamps rather than translating protocol success into training success.

## Source and graph

Recipe v0 is canonical JSON. Typed nodes/edges and explicit Python entrypoints are validated centrally. Cycles, incompatible ports, unpinned resources, inconsistent KL references, unsafe paths and secret-bearing URLs are rejected. Revision conflicts return409 with the current revision and a diff. Graph, split-source and native JSON edits use the same operation path and preserve local drafts on conflict. Arbitrary Python is retained as a code node; no AST reverse-engineering is promised.

## Runtime and package boundaries

The service environment and ZenML environment have independent exact locks. ZenML0.97.0 requires a different resolved FastAPI/Pydantic dependency set; merging the environments would violate the tested dependency contract. The service-side adapter uses subprocess RPC to public ZenML APIs. The official upstream ZenML MCP companion is not imported or patched: it lacks needed authoring/cancel functionality and pins a different SDK.

A detached local supervisor owns a process group. Cancellation and crash handling reconcile native status with process birth identity and process-group termination, including children. Neither a cancel request nor an unreachable worker proves termination. Checkpoints distinguish exact model/optimizer/scheduler/RNG/dataloader restoration from weights-only warm start. A continuation has its own immutable recipe/digest and parent references.

Repository-owned fixtures reuse PyTorch and Transformers. They are small example models/training code, not a replacement training framework. Their synthetic metrics prove execution mechanics only. External model bytes are fetched from a pinned immutable revision and verified; they are not committed or hosted by this project.

## Trust and permissions

Actors have read/modify/execute/spend/delete/publish scopes and optional project restrictions. Defaults do not create an anonymous admin or a persistent credential. Test identities are disposable. Worker environments exclude control-plane and unrelated provider credentials. Untrusted imports are inert; actual execution requires an explicit supported trust/isolation path. A trusted local subprocess is not advertised as a security sandbox. Without verified isolation, arbitrary untrusted execution is refused.

Production remote execution/spend and external recipe publication currently fail closed. Authorized local-private recipe publication, discovery and versioned import are implemented through the same service. Local cost output says only that there is no provider charge; it does not claim hardware/electricity are free or a remote budget enforced.

## Distribution

Pinned Code - OSS is built with a branding/defaults overlay and a built-in workbench package. The feature remains a modular extension contribution inside the product distribution; no initial core patches. Python, Jupyter, basedpyright and debugger binaries are separately pinned and audited. Proprietary assistant binaries are not bundled; exact-fork installation and runtime authorization are separate tests.

## Portability and evidence

Exports contain canonical manifests and explicitly selected, hash-verified source/lock bytes, never credentials or automatic executable import hooks. Import validates all contents before installation or execution. Clean replay uses new installed environments and stores, with module paths and native IDs recorded. Workload-only source pins during development are distinguished from the final complete replay inventory.

See operation-catalog.json, openapi.json, MCP_TOOLS.md and schemas/ for executable contracts; ACCEPTANCE_MATRIX.md records actual gate outcomes. Baseline evaluation findings are not reused as passing product tests.
