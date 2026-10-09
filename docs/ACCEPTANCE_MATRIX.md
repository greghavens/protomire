# Acceptance matrix: live implementation evidence

Last updated 2026-10-09. Development branch, Linux x64 CPU only. Status names are
literal: PASS is demonstrated, FAIL is a tested unmet requirement, BLOCKED needs
an external prerequisite/authorization, UNVERIFIED has not finished testing.
No aggregate below is a full-product completion claim.

## Tested versions

Code - OSS1.105.0 /03c265b1adee71ac88f833e065f7bb956b60550a; Electron37.6.0;
Python3.12.14; native MCP SDK1.30.0; ZenML0.97.0; Torch2.9.0+cpu;
Transformers4.57.1. Extension versions and checksums are in the distribution lock.
Published runtime source checkpoint `4d545df2b111de7f2365e8121a65717b37713054`
is the complete source closure used by both final clean replays and shipping
examples. Later UI/test-only changes record their revisions independently.

| ID | Status | Test and evidence |
|---|---|---|
| T01 | PASS | Actual branded source-build launch; zero core patches. `scripts/build_ide.sh`, `distribution/verify_build.py`; `evidence/gui/distribution/build-manifest.json`, source integrity and launch screenshots. Linux development distribution only. |
| T02 | PASS | Actual IDE Python/basedpyright diagnostics, debugpy program, Jupyter output42, terminal and Git. `tests/gui/smoke.cjs`, `scripts/smoke_ide.sh`; distribution GUI results/screenshots. The debugger stopped at a real breakpoint and evaluated answer=42 before continuing. |
| T03 | PASS | Service tests and real backend project isolation; multiple recipe/native model versions; cross-project lookups denied. `tests/service/`, `tests/security/`, `evidence/backend/service-integration.json`. |
| T04 | PASS | Canonical recipe roundtrip/schema-version rejection/typed DAG/source and dependency validation. `tests/recipe/`; schema generated from same runtime contract. |
| T05 | PASS | Actual typed data-to-custom-Python graph authored in the running fork, parameters and Python node preserved. `workbench-dom-results.json`, graph screenshot; typed-schema and frontend regression suites. |
| T06 | PASS | Actual native Monaco save, GUI-to-independent-MCP same UUID/revision/digest, MCP edit visible in running GUI. `evidence/gui/final-caption/workbench-host-results.json` and `workbench-dom-results.json`, screenshots. Final rerun includes correct dropdown name/revision after remote edits. |
| T07 | PASS | Actual stale GUI revision4 versus server5 shows conflict/diff; local draft retained and newer server value untouched. Invalid JSON also survives graphical-edit attempts. Workbench GUI results and conflict screenshot. |
| T08 | PASS | Official SDK independent stdio MCP client, no IDE process; full authoring/import/export/validation and service restart. `tests/mcp/`, `evidence/mcp/`. |
| T09 | PASS | Scoped actor/read-only denial across mutation tools, audit records, project isolation and denied hidden paths. MCP/service/adversarial suites. |
| T10 | PASS | Actual native run/artifact UUIDs persisted through service/MCP and a real GUI Start local fixture click/confirmation. GUI status, logs, metrics and artifact IDs agree with API; `evidence/gui/execution/` holds the actual run. `tests/service/test_real_backend.py`, `tests/integration/test_mcp_training.py`; corresponding backend JSON. |
| T11 | PASS | Consumed dataset artifact hash checked against upstream stage, tamper rejected. CPU/native suite plus clean-replay dataset equality. |
| T12 | PASS | Actual gated PyTorch width variants through native DAG; `test_native_custom_dag_variants_negative_and_models`; `native-custom-variants.json`. |
| T13 | PASS | Executed explicit KL coefficient/direction/frozen reference, manifest and training-contract checks, real loss/parameter effect; custom and pretrained suites. |
| T14 | PASS | Negative comparison plus deliberately failed native run and actual workerSIGKILL retained/queryable; native lifecycle JSON/logs. |
| T15 | PASS | Actual validation-based selection, promoted native model version and selected-parent checkpoint lineage; `native-pretrained-selection.json`, service integration decision records. |
| T16 | PASS | Deterministic model/optimizer/scheduler/Python+TorchRNG/dataloader parity after exact continuation; separate warm-start semantics. CPU and real cancellation/native-resume tests. |
| T17 | PASS | Running native worker and live descendants terminate before acknowledgement; real native metadata carries proof. ZenML static pipeline remains nativefailed, separately exposed effectivecancelled. `native-cancel-exact-resume.json`, adversarial mapping tests. Early pre-native cancellation and pending-cancel resume rejection also pass real-process tests; no native UUID is invented. |
| T18 | PASS | Independent MCP submits actual training; application service isSIGKILLed and restarted while run active; authoritative UUID/status/artifacts recovered. `tests/integration/test_mcp_training.py`, `evidence/backend/mcp-training.json`. |
| T19 | PASS | Both custom and pretrained actual replay: independent installed service/backend environments and native stores, installed source hashes verified, no inheritedPYTHONPATH/source imports, identical metrics/dataset/output hashes. `scripts/replay_bundle.py`; `evidence/portability/*comparison.json` and identity records. Both final fixture pairs were rerun against the published shipping source after boundary fixes. |
| T20 | PASS | Actual OpenAlex paper metadata retrieval, explicit evidence/claim/hypothesis linked to real native run and decision; `tests/service/test_real_backend.py`, literature tests, `evidence/research/openalex-live.json`, service integration evidence. Retrieval is not scientific claim verification. |
| T21 | BLOCKED | Both exact Linux VSIXs installed and activated in the fork: Codex26.5908.31748 and Claude Code2.1.295. Both reach actual vendor sign-in screens. Their bundled clients connect to native MCP; Codex discovers all 48 tools. No authorized vendor session or paid inference is available, and the Codex thread/tool-call probe also hits a vendor sandbox mount-lock failure. Authenticated assistant MCP calls remain unverified. Missing API-proposal warnings are also recorded; engine-range compatibility alone is not a pass. `distribution/ASSISTANT_COMPATIBILITY.md` and `evidence/gui/distribution/` assistant report and actual sign-in screenshots. |
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

P0–P3 and P4 clean replay have working demonstrations. P4 exact authenticated
vendor-assistant calls remain blocked, so P0–P4 is not declared fully complete. Full production scope, signed multi-OS
releases, verified untrusted sandboxes, remote budget/accounting, richer research
monitoring, external evaluation judges and marketplace remain explicit follow-on
requirements, not implemented features.

## Aggregate validation record

The complete local test script exited 0 after bootstrap fixes: 238 service/contract
tests passed, 3 real service/MCP/mapping integration tests passed, and 15 actual
CPU/native lifecycle tests passed. The service interpreter skips two Torch
collection modules intentionally; those execute in the separate backend profile.
The final 42 frontend unit tests and 7 real-service/external-MCP checks also passed.
Portability helper coverage subsequently grew to 17 tests, with both actual
clean-replay fixtures separately verified. These counts overlap where indicated;
they are not added to invent a larger total. The latest service/contract-only rerun confirms 238 passes. Final UI-polish tests (42 unit, 8 actual GUI parity checks plus native Monaco) and CI results
are recorded in the final evidence summary.

The first CI run failed because a clean editable install needed the missing
editables build dependency; the next exposed a test's unpublished local source
pin. Both were corrected rather than skipped. Final CI status must be read from
the linked GitHub run and its exact commit, not inferred from local success.

CI run [37947375390](https://github.com/greghavens/modelbuilder/actions/runs/37947375390) passed both contracts and CPU lifecycle jobs at commit `9f0732f803fb2cc4d79898e6292a7a159a644cbf`. Final evidence/UI-polish publication is checked separately; see `evidence/VALIDATION_SUMMARY.json`.
