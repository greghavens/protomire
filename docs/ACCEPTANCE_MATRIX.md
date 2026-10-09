# Acceptance matrix: live implementation evidence

Last updated 2026-10-09. Development branch, Linux x64 CPU only. Status names are
literal: PASS is demonstrated, FAIL is a tested unmet requirement, BLOCKED needs
an external prerequisite/authorization, UNVERIFIED has not finished testing.
No aggregate below is a full-product completion claim.

## Tested versions

Code - OSS1.105.0 /03c265b1adee71ac88f833e065f7bb956b60550a; Electron37.6.0;
Python3.12.14; native MCP SDK1.30.0; ZenML0.97.0; Torch2.9.0+cpu;
Transformers4.57.1. Extension versions and checksums are in the distribution lock.
Runtime source checkpoint614ef11496c27d0677c3c9926828eb4d27ea509c established
initial clean replay. Later tests record their source revisions independently.

| ID | Status | Test and evidence |
|---|---|---|
| T01 | PASS | Actual branded source-build launch; zero core patches. `scripts/build_ide.sh`, `distribution/verify_build.py`; `evidence/gui/distribution/build-manifest.json`, source integrity and launch screenshots. Linux development distribution only. |
| T02 | PASS | Actual IDE Python/basedpyright diagnostics, debugpy program, Jupyter output42, terminal and Git. `tests/gui/smoke.cjs`, `scripts/smoke_ide.sh`; distribution GUI results/screenshots. A stronger debugger breakpoint probe is being finalized. |
| T03 | PASS | Service tests and real backend project isolation; multiple recipe/native model versions; cross-project lookups denied. `tests/service/`, `tests/security/`, `evidence/backend/service-integration.json`. |
| T04 | PASS | Canonical recipe roundtrip/schema-version rejection/typed DAG/source and dependency validation. `tests/recipe/`; schema generated from same runtime contract. |
| T05 | UNVERIFIED | Typed graph/parameter/Python preservation unit tests and real HTTP parity pass; actual graphical interaction acceptance is running. `packages/ide_feature/test/`, pending GUI evidence. |
| T06 | UNVERIFIED | Independent MCP and TS HTTP client match IDs/revisions/digests; actual fork graph/Monaco/MCP cross-client test is running. No headless substitute is counted as GUI PASS. |
| T07 | UNVERIFIED | Real409 conflict and retained-draft unit/client tests pass; actual IDE diff/conflict interaction pending. |
| T08 | PASS | Official SDK independent stdio MCP client, no IDE process; full authoring/import/export/validation and service restart. `tests/mcp/`, `evidence/mcp/`. |
| T09 | PASS | Scoped actor/read-only denial across mutation tools, audit records, project isolation and denied hidden paths. MCP/service/adversarial suites. |
| T10 | PASS | Actual native run/artifact UUIDs persisted through service/MCP. `tests/service/test_real_backend.py`, `tests/integration/test_mcp_training.py`; corresponding backend JSON. |
| T11 | PASS | Consumed dataset artifact hash checked against upstream stage, tamper rejected. CPU/native suite plus clean-replay dataset equality. |
| T12 | PASS | Actual gated PyTorch width variants through native DAG; `test_native_custom_dag_variants_negative_and_models`; `native-custom-variants.json`. |
| T13 | PASS | Executed explicit KL coefficient/direction/frozen reference, manifest and training-contract checks, real loss/parameter effect; custom and pretrained suites. |
| T14 | PASS | Negative comparison plus deliberately failed native run and actual workerSIGKILL retained/queryable; native lifecycle JSON/logs. |
| T15 | PASS | Actual validation-based selection, promoted native model version and selected-parent checkpoint lineage; `native-pretrained-selection.json`, service integration decision records. |
| T16 | PASS | Deterministic model/optimizer/scheduler/Python+TorchRNG/dataloader parity after exact continuation; separate warm-start semantics. CPU and real cancellation/native-resume tests. |
| T17 | PASS | Running native worker and live descendants terminate before acknowledgement; real native metadata carries proof. ZenML static pipeline remains nativefailed, separately exposed effectivecancelled. `native-cancel-exact-resume.json`, adversarial mapping tests. Early pre-native startup cancellation is under additional review. |
| T18 | PASS | Independent MCP submits actual training; application service isSIGKILLed and restarted while run active; authoritative UUID/status/artifacts recovered. `tests/integration/test_mcp_training.py`, `evidence/backend/mcp-training.json`. |
| T19 | PASS | Both custom and pretrained actual replay: independent installed service/backend environments and native stores, installed source hashes verified, no inheritedPYTHONPATH/source imports, identical metrics/dataset/output hashes. `scripts/replay_bundle.py`; `evidence/portability/*comparison.json` and identity records. Final shipping checkpoint will be rerun after boundary fixes. |
| T20 | PASS | Actual OpenAlex paper metadata retrieval, explicit evidence/claim/hypothesis linked to real native run and decision; `tests/service/test_real_backend.py`, literature tests, `evidence/research/openalex-live.json`, service integration evidence. Retrieval is not scientific claim verification. |
| T21 | UNVERIFIED | Exact Codex/Claude IDE versions/license/engine metadata inspected; actual authorized installation and authenticated native-MCP calls remain separate gates. Generic MCP success does not satisfy this row. |
| T22 | BLOCKED | P5 expressly requires separate paid-provider authorization. Remote targets/spend fail closed; no provider invoice/cap enforcement claimed. |
| T23 | PASS | Inert import; tar/path/link/duplicate/collision/hash/schema/credentialURL/dependency rejection; untrusted execution denied without verified isolation. Recipe/security tests and portability helper tests. |
| T24 | PASS | Existing project was an independent read-only reference; no original-repository writes/training. Implementation uses separately authored fixtures; attached private evaluation archive is excluded. |

## Evidence interpretation

`evidence/` contains actual command output, native UUIDs, hashes and screenshots.
Historical unsuccessful build/test attempts are retained with their status;
a retry does not retroactively make their logs successful. Tests are not summed
across overlapping runs. The final aggregate transcript, clean replay and GUI
transcript are reported separately. Screenshots depict the actual running fork,
not a design mockup. Test credentials and private model material are excluded.

P0/P1/P3 and clean replay have working demonstrations. P2 GUI and P4 exact vendor
assistant gates are still being finalized. Full production scope, signed multi-OS
releases, verified untrusted sandboxes, remote budget/accounting, richer research
monitoring, external evaluation judges and marketplace remain explicit follow-on
requirements, not implemented features.
