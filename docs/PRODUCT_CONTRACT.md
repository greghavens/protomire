# Product contract

Modelbuilder is a temporary product name. The product is a dedicated Code - OSS IDE for the full lifecycle of one model per project. A project can hold research, hypotheses, datasets, multiple recipe revisions, runs, evaluations, checkpoints, versions and subsequent improvement decisions. It is not a chatbot, coding agent, notebook engine or new training framework.

## Permanent boundaries

- A built-in workbench contribution is the permanent UI implementation, with zero initial Code - OSS core patches.
- Humans and independent native MCP clients use one authoritative operation service and the same resource identities, revisions, permissions and errors.
- Canonical versioned JSON describes a typed recipe DAG. Arbitrary Python stays an explicit code node, never an inferred AST-to-graph promise.
- ZenML0.97.0 provisionally owns execution/artifact/model-version identities. SQLite owns the missing project/recipe/research/policy/operation semantics, not a second full run registry.
- Git and immutable resource manifests identify source/recipes; external hosts retain model weights and datasets. A future marketplace sells/shares recipes only.
- Scientific evidence is attributed and uncertain; a citation alone is not proof. Negative and failed outcomes remain queryable.
- Exact continuation and warm start are different operations. Requested cancellation is not confirmed termination. Unknown workers remain unknown.
- Imported code is inert. Arbitrary untrusted execution requires verified isolation; no unsafe fallback is allowed.

## Current delivery scope

P0–P4 covers a reproducible distribution, local shared service/MCP, recipe/research/run workbench, two independently executed CPU model fixtures, truthful job lifecycle and clean recipe replay. Acceptance is recorded individually in ACCEPTANCE_MATRIX.md. Tests blocked by the current environment remain blocked even when implementation files exist.

## Retained full product requirements

These are explicit future gates, not completed or abandoned features:

1. One authorized remote provider with real rate sources, reservations, concurrent total-budget checks, cancellation, delayed billing reconciliation and idle/storage cleanup. P5 requires separate spending authorization.
2. Wider local/remote/GPU and eventually distributed execution through reused runtimes, with reproducible environment and resource capability selection.
3. A broad built-in component catalog wrapping established data/model/evaluation frameworks, while retaining arbitrary architecture and loss functions.
4. External synthetic-data generators, contamination checks, adversarial judges, calibration and comparison compatibility contracts; general scientific quality needs validation beyond toy fixtures.
5. Ongoing literature monitoring and richer research-to-experiment evolution without an embedded autonomous agent.
6. Private recipe registry first, then discovery/fork/versioning and explicitly authorized publication/payment features after portability/trust/licensing tests.
7. Production security, packaging, migration, recovery, accessibility and performance hardening on supported operating systems.

The existing Biff repository is not modified, trained, mirrored or redistributed. Its private reference material is not a product template. Paid compute, public releases and model/data publication are not authorized by this slice.
