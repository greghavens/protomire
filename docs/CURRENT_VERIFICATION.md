# Current verification and remaining gates

## Functionality-family coverage and delivery update, 2026-10-18

Backend runtime source is `2dd294d831a5223fe982bbd8fe324c12832c76ea`, identical
to locally tested source commit `1494364a2ac8fcd07ab544902147b2165b43a526`.
Both shipping recipes now pin its **166-file** source/lock closure. This is newer
source, not a rebuilt or release-accepted Mac/Windows/Linux application.

- Actual standard local `scripts/test_all.sh` exited **0**: **3,211 Python passed**,
  217 skipped, 108 deselected; **586 frontend passed**; four HTTP suites passed.
  The optional offline Hub SDK environment was absent and explicitly not run.
  The complete native aggregate was not enabled; no hosted CI was run.
- Real native functionality-family training/evaluation tests include v1 prefix
  and streaming exposure, successful-update accounting, masked causal targets,
  v2 convergence/budget behavior and exact resume. **15 focused tests passed**.
- A registered, serialized ZenML pipeline passed **2 cases**, including live
  checkpoint evaluation with an explicitly missing family preserved in durable
  records and completion events. Independent native family evaluation/target
  checks passed **3 cases**, including token-weighted causal-LM measurements.
- Native scientific comparison/promotion/integrity regression passed after an
  actual schema-admission failure exposed unsupported regex keywords. The fix
  preserves semantic SHA validation without weakening inert-schema restrictions.
- Generated Claude/Codex/OpenCode/Kilo commands each cold-started the real MCP
  service after the IDE lease and service had stopped: **4 cases passed**.
  This is command/configuration transport acceptance, not vendor sign-in or
  upstream extension discovery/inference acceptance.
- The migration skill's read-only inventory and reviewed setup/rollback paths
  have local tests. Flatpak packager contracts use explicitly inert tool fixtures;
  **no installable Flatpak or Bazzite/GPU acceptance is claimed**.
- Additional Flatpak host/sandbox launcher routing, identity rejection and
  rollback tests passed after the standard run. They are unit-level routing
  evidence, not actual Flatpak execution.

The new family-exposure native contract currently supports **single-process CPU
float32 without AMP**. GPU/MLX/distributed exposure adapters remain unsupported;
required native guards reject unsupported producers. A dataset policy alone does
not intercept arbitrary custom training scripts. Use the explicit required guard
and full evaluation-cohort pin described in [the contract](functionality-family-coverage.md).

Linux delivery is now **Flatpak for Bazzite**, with mandatory bundled MCP. Native
packaging, host-terminal MCP launch, device access, first-open GUI and clean-machine
acceptance remain release gates; see [Flatpak status](../distribution/FLATPAK.md).
The prior Mac artifact does not contain these changes. Earlier failed/interrupted
runs and unrelated release gates below are retained, not retroactively marked passed.

## Mac acceptance update, 2026-10-14

Tested source is `7752040a3b003f03b7af9b77c57f39c06c2aeb61`; the standard
run tested the reviewed two-file portability patch subsequently committed there.
Frozen runtime remains `33397ea30361b9c51dfb6bde219fd5f58c59dc10`.
Both shipping recipes' 163-file closures matched declared hashes and Git source
bytes. These are scoped passes, not full-product completion.

- Standard non-native `scripts/test_all.sh`: **2,954 Python passed**, 306 skipped,
  106 deselected; **582 frontend passed** and all four HTTP suites passed
  (`test:service`, `test:workspace-http`, `test:source-pin-http`,
  `test:history-http`). Actual script exit was 0.
- Targeted `tests/integration/test_mcp_training.py::test_mcp_real_training_and_service_crash`
  passed after the host-profile fixture correction committed in `5343407`.
  This Mac result does not erase the historical Linux restart failure below.
- Explicit Mac native set: **5 passed, zero failures or skips**, pytest exit 0.
  `tests/components/test_transformers_training.py::test_required_accelerator_trainer_hardware`
  ran real MPS training and fresh-process export with required Trainer/device
  flags and MPS fallback disabled. The two CPU checks were
  `tests/placement/test_placement.py::test_real_cpu_probe_forward_backward_in_configured_interpreter`
  and `tests/placement/test_placement.py::test_real_device_aware_training_cpu_checkpoint_eval_and_cancel`.
  `tests/integration/test_mlx_service.py::test_registered_project_mlx_native_pipeline`
  passed both `full` and `lora`, including full-mode native deadline cleanup.
  Fixtures were synthetic and locally authored; test deadlines were unchanged.
  Ownership monitoring found no remaining processes after either completed set.

The **complete Linux native aggregate remains incomplete**; its queue requires
Linux process containment, and Linux PDF/OCR isolation is not Mac coverage.
The MPS-unavailable negative test was not selected on available MPS hardware.
The optional offline Hub suite was **not run in this Mac standard run** because
its reviewed environment was absent. Native GUI, signing/notarization and
clean-machine acceptance remain open. Public pretrained asset rights review
remains unresolved; no public pretrained acceptance was run in this Mac set.
Earlier failed and interrupted runs below remain historical evidence.

## Historical combined local CI, 2026-10-14 00:45 UTC

The canonical offline run on `89d180a` finished with **exit 1**, not a pass.
Pre-native checks passed: 3,087 Python tests (199 skips, 106 deselections),
18 offline Hub SDK checks, the frontend/HTTP checks, required local PDF/OCR,
three offline Modal SDK checks and native component authoring.

Native results: components **651 passed / 10 skipped**, concurrency **18 passed /
4 deselected**, integration **64 passed / 1 failed / 43 deselected**. Lifecycle
was terminated by SIGKILL with no JUnit, so its cases are not counted as passed.
The integration failure was a post-restart MCP status HTTP 503; the native worker
itself finished successfully. Its status RPC took about 58 seconds against MCP's
30-second default; exact transport exception and the cause of delay were not
recorded. OOM is not established for the separate lifecycle termination.

The queue finalized after 1,907.147 seconds, with cleanup complete and zero
signals. Prior results and copied failure state are preserved. A fresh run after
removing verified archived historical tmpfs test state is pending; assertions
and deadlines are unchanged. Local CI now explicitly disables npm registry
maintenance, using the same offline settings that passed the preceding compile.
The first attempt's blocked npm request remains recorded separately.

## Latest acceptance status, 2026-10-13 23:00 UTC

Runtime remains `33397ea30361b9c51dfb6bde219fd5f58c59dc10`, with both
163-file shipping closures verified. [Current receipt](../evidence/product-completion-20261013/current-33397ea-acceptance.json)
separates scoped passes from incomplete acceptance.

- Complete standard run: 2,939 Python passes, 199 skips, 106 deselections;
  18 offline Hub SDK passes and 582 frontend passes.
- Actual installed synthetic custom-PyTorch replay passed on the same Linux CPU
  host: four fresh noneditable copy installs, 64,574 installed entries unchanged,
  zero installed bytecode files, seven identical scientific outputs, and confirmed
  native worker/supervisor/process-group cleanup. All 117 sealed receipts verified.
  Strict comparison stays **FAIL, 34/35**: three original manifests differ only
  under `/rights`. The structural audit passes without rewriting provenance.
  Installed Trainer has now separately passed the workload-specific review below;
  installed CSV/JSONL streaming has also passed the scoped review below.
- Separate native groups: integration 65 passed; lifecycle 30 passed with one
  public pretrained rights-admission refusal; concurrency 18 passed (4 deselected).
  Components first completed with 649 passes, 11 skips and one fixture failure.
  An initial corrected rerun was killed without JUnit; its cause remains
  unconfirmed. After verified archival cleanup of temporary installations, the
  complete corrected component group passed **650 tests, with 11 skips**, in
  1,086.080 seconds. Pytest, runner and orchestration exited 0; cleanup completed
  with zero signals. Prior failed/incomplete evidence remains preserved.
  **No complete native aggregate pass is claimed.**
- Native queue receipts now preserve atomic progress, completed-group evidence
  and explicit unfinished state. The interrupted rerun retained actual exit and
  confirmed cleanup; 128 integrated tooling tests passed (one deselected).
- The v19 unsigned Mac development DMG contains this runtime. Mounted checks
  initially had five passes and one external-fixture failure; the corrected
  fixture passed separately. Native GUI acceptance remains blocked on unlock.
  Signing, clean-machine installation and other platform/integration gates remain.

## Installed streaming acceptance, 2026-10-13 23:54 UTC

Both fresh installed roles passed [scoped CSV/JSONL streaming acceptance](../evidence/product-completion-20261013/installed-streaming-33397ea.json).
Eight preparation cases independently verified proportional/equal quotas,
selected IDs/content/order and lineage on 120,000-row inputs. Four real five-node
native runs consumed exactly **4 of 12 selected rows**; two cancellation cases
retained one step-1 checkpoint with **2 consumed rows**. Prepared, selected and
consumed counts are deliberately distinct. All model tensors changed and remained
finite, selected-checkpoint evaluation/export matched, and fresh-process export
prediction parity was exact on the authored fixture input.

Public rights review, source/index tamper rejection, stricter cache caps,
preexisting cancellation, restart/cache checks and native cleanup passed.
All eight total jobs (six streaming plus two baseline jobs) terminated. Four
installed environments' 64,574 entries, all 163 runtime files and 126 sealed
receipts remained verified. Actual harness exit was 0. The generic hook stays
`REPORTED_UNVERIFIED`; the independent scientific review supplies this scoped
PASS without altering the original rights-only strict comparison failure.

This does not cover Parquet in installed environments, streaming warm start or
exact-state continuation, in-flight ingestion/ranking cancellation, cross-role
checkpoint equality, GUI or installers. Ten-second sampling observed maximum
disk growth of approximately 3.373 GiB and minimum free disk of 1.844 GiB; these
are sampled observations, not guaranteed instantaneous peaks.

## Installed Trainer acceptance, 2026-10-13 23:28 UTC

The real standalone driver passed under **both fresh installed roles** on the
same Linux CPU host. [Independent scientific receipt](../evidence/product-completion-20261013/installed-trainer-33397ea.json)
records native model/optimizer/scheduler/RNG equality between uninterrupted and
resumed training, four-epoch history and eight-step horizon, genuine optimization,
immutable parent contract, selected-checkpoint evaluation/export, and all six
unsafe-checkpoint rejections before optimizer entry. All 14 owned jobs terminated
with worker/supervisor/process-group cleanup confirmed. All 126 sealed receipts
and 64,574 installed pre/post entries verified; runtime and imported source did
not change. Actual harness exit was 0.

This is scoped acceptance of the static five-node CPU float32 eager Trainer v2
workflow. It does not establish streaming or partial-epoch continuation, GPU,
cross-platform or general model-quality acceptance. The generic hook's
`REPORTED_UNVERIFIED` label is preserved; this separate scientific review supplies
the scoped acceptance. The custom replay's strict rights-manifest comparison
remains its original **FAIL**, not rewritten to produce equality.

## Shared-service Trainer continuation, 2026-10-13

The strengthened real service test passed on Linux (**232.20 seconds**) and Mac
(**106.89 seconds**). It submits a real run, cancels after completed epoch 2,
discovers that checkpoint and resumes to the original four-epoch horizon.
Model/optimizer/scheduler/RNG state, row order, held-out metrics and the exported
selected model match the uninterrupted control. Exact per-case rejection and
Trainer/optimizer entry markers establish that unsafe checkpoints fail before
optimization. Every created operation and owned process group is checked during
teardown. See [the scoped receipt](../evidence/product-completion-20261013/trainer-service-resume.json).

This proves the tested static five-node CPU/eager DAG, not arbitrary DAGs,
iteration state, streaming training or other accelerator continuation. The runtime
remains `33397ea` with 163 pinned files. Full native aggregate, untested streaming modes and graphical acceptance gates remain open. The Mac
execution connection recovered and the v19 unsigned app was built. The latest
GUI preflight found the screen locked and launched no IDE window.

## Streaming dataset safeguards, 2026-10-13

Runtime source `33397ea30361b9c51dfb6bde219fd5f58c59dc10` combines the
reviewed Trainer implementation below with bounded streaming stratified sampling.
Both shipping recipes pin its **163-file** runtime closure. See
[dataset contracts](DATASET_LIFECYCLE.md) and
[scoped receipt](../evidence/product-completion-20261013/streaming-sampling.json).

Linux dataset/rule tests passed **308**, with **8 platform/interpreter skips** and
**17 integration deselections**, including real pinned Parquet parity and
cancellation. The integrated dataset/rule/source-pin/structural-audit selection
passed **383**, with the same skips and deselections. These counts overlap.
Generated inputs of **120,001 and 240,001 rows** crossed the actual eager limits;
peak process RSS was approximately **38.0 and 38.2 MB**, respectively. Input and
SQLite disk usage still scale with dataset size. SQLite temporary sort scratch
is outside the main index-byte cap.

Cold, warm and restarted compilation use identical canonical UTF-8 report-budget
accounting. Physical index allocation is recorded and enforced separately from
logical dataset identity. Stalled and partial Parquet output observes the existing
cancellation signal while retaining decoder limits, containment and owned-child
cleanup. User-selected proportional/equal allocation and explicit representation
constraints reuse the same deterministic quota planner as eager preparation.

The combined standard local aggregate exited **1**: **2,936 passed, 1 failed,
199 skipped and 106 deselected**. The failure is the streaming memory check: its
child reported 235 MB high-water RSS under the aggregate versus approximately
38 MB in isolated execution. A controlled probe reproduced inherited Linux
parent high-water RSS across exec. A test-only clean intermediary now excludes
that inherited peak while retaining real child allocation peaks. Under a 280 MB
parent, both real fixtures remained near 38 MB; all **3 measurement regressions
passed**. The 192 MiB absolute and 16 MiB growth limits are unchanged. Later
script stages did not run in the failed attempt. The complete corrected rerun
then exited **0**: **2,939 Python passed, 199 skipped, 106 deselected**, **18 Hub SDK**
and **582 frontend tests**, plus all loopback HTTP stages. The original failed
measurement receipt remains preserved.
Fresh full native queue,
installed-wheel replay and rebuilt application acceptance remain pending. This
feature does not establish streaming Trainer exact-state continuation.

## Policy-driven Trainer epochs baseline, 2026-10-13

Runtime source `81708511b7c5c47444326aa15c67a187188cb3f6` adds real
Transformers Trainer v2 epoch decisions and verified exact-state continuation.
Both shipping recipes pin its complete **163-file** closure. See
[the supported contract](TRAINER_EPOCH_RULES.md) and
[scoped acceptance receipt](../evidence/product-completion-20261013/trainer-epoch-rules.json).

The final focused Trainer and exact budget-math suite passed **69 tests**;
source-pin and budget coverage passed **59**. The complete standard local script
exited **0** with npm offline: **2,864 Python passed, 198 skipped, 104 deselected**,
**18 offline Hub SDK** and **582 frontend tests**, plus all service/workspace/
source-pin/history loopback HTTP stages. Optional interpreter/platform skips are
not acceptance evidence; the separate native Trainer suite supplies the scoped
CPU/eager evidence. All **57 added skips** are new Trainer tests requiring Torch;
each passed in that separate backend run. A test-only resource-tracker warning
was traced to budget tests invoking real descendant cleanup inside pytest. Their
empty-process cleanup is now isolated, with **66 ordered regression tests passed**
and resource-tracker warnings treated as errors. Production cleanup is unchanged.

This includes real dropout-sensitive uninterrupted/resumed comparisons of model,
optimizer, scheduler, RNG and row order, with the original full scheduler horizon.
Custom and combined policies use the existing versioned Method registry; resource
admission remains distinct from scientific convergence. No inferred thresholds or
silent warm-start substitution are introduced.

Actual shared-service `run.submit` / checkpoint discovery / `run.resume` acceptance
is still in progress. Streaming sampling remains in review and is not integrated.
The fresh full native queue, installed-wheel replay and rebuilt app acceptance
remain pending. The earlier failed full queue below remains failed; these scoped
passes do not replace it.

## Deterministic rules engine baseline, 2026-10-13

Runtime source `4e458d632ba23c42145d3afbe507a7cea4a43e76` adds versioned,
source-pinned rule methods, durable project policy activation, native decision
attestations, exact selected-checkpoint action guards, and configurable eager
stratified sampling. Both shipping recipes pin its complete **160-file** closure.
See [rule contracts and limits](DETERMINISTIC_RULES.md) and the
[scoped acceptance receipt](../evidence/product-completion-20261013/deterministic-rules.json).

The focused rules/native/queue suite passed **102 tests**; shipping source-pin
coverage passed **51 tests**. After fixing test-module isolation, the local Python
aggregate passed **2,845**, with **141 skipped** and **101 deselected**, and the
offline Hub SDK suite passed **18**. A blocked npm update request interrupted the
aggregate script; its remaining workbench stages were rerun offline and passed:
**582 frontend tests**, type checks, compilation and real loopback HTTP checks.
The full native queue exited **124** at its **2,100-second deadline**; descendant
cleanup completed. Lifecycle had four failures, integration one, and components
two; concurrency was canceled at the deadline. The known pretrained rights
rejection remains. Invalid mocked component descriptors have been corrected
without relaxing production validation (**2 focused and 90 placement unit tests
passed**). All previously timed-out selections passed unchanged in quiet sequential reruns:
**1 calibrated lifecycle, 3 discovery, 1 conditional live evaluation and 18
concurrency tests**. That does not replace the failed aggregate. Measured added
coverage exceeds the former aggregate allowance; the documented queue budget is
now **2,700 seconds**, retaining two processes and every individual deadline
(**43 runner unit tests passed**). A fresh full run is still required. Fresh installed-wheel replay,
service-level Trainer epoch continuation, streaming sampling and native graphical
acceptance are not claimed complete. Earlier runtime receipts below remain
historical evidence.

## Guided quickstart correction, 2026-10-13

The current extension replaces the worksheet with separate assistant, goals,
starting-point, project-details and review screens. Eligible empty first sessions
open this wizard once; existing tabs/workspaces and pending project recovery are
preserved. A restored stock Welcome tab is not automatically closed or migrated.
The existing extension, branding overlay and single upstream composition-import
boundary are unchanged: this correction adds **no upstream Code - OSS edits**.

Encrypted unfinished drafts and approved creation recovery have distinct durable
identities. Regression coverage includes failed and queued saves, lost responses,
close/reopen, retrying A while preserving unfinished B, and cross-window cleanup
and discard races. Obsolete encrypted snapshots/markers do not yet have garbage
collection. These are setup-metadata tests, not OS Keychain or graphical acceptance.

Final affected type checks, compilation, **582 frontend tests**, and real loopback
project creation/recovery passed in the integration checkout. The independent
read-only review passed eight additional lifecycle checks. See the accompanying
 [quickstart evidence](../evidence/product-completion-20261013/guided-quickstart.json)
for the final local aggregate and precise limitations. The standard local aggregate
exited 0: **2,780 Python passed, 141 skipped, 89 deselected**, **18 offline Hub SDK**
and **582 frontend passed**, plus service/workspace/source-pin/history HTTP checks.
Native integration was not rerun for these extension-only changes; the previous
full native aggregate failure is still reported below.
The native backend remains the frozen 154-file `6323bfd` runtime below.

**Native wizard GUI acceptance is pending the Mac being unlocked.** All test IDE
instances were closed normally. Mac DOM/host-mocked checks do not establish native
Code - OSS behavior; the integration-host Chromium attempt was blocked at launch
by sandbox socket permissions. No browser security or Workspace Trust was disabled.
The updated **v15 unsigned Mac DMG** contains committed extension source `f683e28`
with the unchanged runtime. The Mac build task reports clean source restoration,
45,381 sealed runtime files, staged and read-only mounted strict inventory,
service/backend/Hub/MLX imports, and **6 core tests passed, 7 deselected, in
110.54 seconds**. DMG verification and detach passed without launching the GUI.
The reviewed pinned Code - OSS binary was reused; this was not another complete
editor build. [Build-host report](../evidence/product-completion-20261013/mac-f683e28-build-host-report.json)
records the 1,163,397,720-byte DMG and exact hashes. Raw Mac evidence remains local
to that machine and is not independently retrieved by the integration host.
The v12 artifact is preserved as historical evidence. Native wizard/clean-machine,
signing/notarization and full-product/release gates remain open.

## Current frozen candidate 34fe6c7 / 6323bfd

Candidate `34fe6c75dda74e07eb8c6bf048d03e6694e0c886` pins runtime
`6323bfdbce9dd9bb55f8403e69d5e2dd5f234b92` and identical **154-file** source/lock
closures in both shipping recipes. **The complete local aggregate exited 1**.
Its sole aggregate failure is the public pretrained lifecycle admission gate,
`license_rights_unreviewed`. Full-product and release acceptance remain incomplete.

### Completed local Linux aggregate

- Light: **2,780 passed, 141 skipped, 89 deselected** in 209.85 seconds;
  frontend: **530 passed** with type checks and compilation; offline Hub SDK:
  **18 passed**; offline Modal SDK: **3 passed**.
- Native lifecycle: **30 passed, 1 failed** in 1,170.74 seconds; integration:
  **53 passed, 43 deselected** in 1,614.13 seconds; components: **589 passed,
  10 skipped** in 676.14 seconds; concurrency: **18 passed, 4 deselected** in
  401.40 seconds.
- All four groups were attempted. The native queue completed in
  **2,017.170 seconds**, below its **2,100-second** deadline.
  Cleanup completed with **0 signaled processes**. Skips remain recorded.
  No rights approval was fabricated, and no rights check was weakened or excluded.

[Compact terminal evidence](../evidence/product-completion-20261013/acceptance-6323bfd/README.md)
retains raw receipt hashes, independently verified source closures and exact
scope. Overlapping selections are not added as unique coverage.

### Integrated Moonshiner public-workflow correction

This frozen runtime includes the corrected dictionary dataset port and actual
registered split/consumption validation with ordered row hashes. Source-matched
focused verification passed **71 tests in 42.67 seconds**, including official
Moonshiner 0.8.23 service-to-ZenML source-bound execution and explicit prepared
output validation/registration/policy binding/dataset binding, preserving inherited
teacher/data/source rights. The final targeted cancellation/deadline checks
passed **2 tests, 66 deselected, in 2.18 seconds**.

The initial **70 passed, 1 failed** attempt remains recorded: its fixture rejected
Linux's terminal `dead` state. Terminal-state handling and the disappearance race
were corrected without accepting live descendants. Prior d573fe0 adapter defects
remain historical. These scoped checks do not claim provider generation, actual
teacher execution, training/token masking, GUI, full-flow HTTP/MCP transport or
whole-service cancellation. [Focused checks and hashes](../evidence/product-completion-20261013/acceptance-6323bfd/focused-source-checks.json)
separate those results from the aggregate.

The current **51 source-pin/wheel checks passed in 4.30 seconds**. A separate
fresh installed replay now covers this exact **154-file runtime**: four offline
noneditable environments and four dependency checks passed; two independently
reviewed Git-free native runs succeeded and exact retries reconciled their
existing operations. Both workers exited 0, were reaped, and their process
groups terminated.

All **64,322 installed files** remained byte-identical from ordinary installation
through execution, with **zero bytecode files before and after** and no explicit
precompilation or warmup. All **866 tracked checkout files**, 154 runtime files
and extracted source bytes remained unchanged. The real identity-subprocess
regression separately passed one test. This verifies the `-I -B` probe correction
without the historical precompilation workaround.

The unchanged strict comparator still reports **FAIL, 34/35**, solely at
`output_hashes_equal`: **seven scientific outputs**, deterministic metrics and
evaluation records match exactly, while three manifests differ exclusively
under `/rights` with legitimate independent review identities. No provenance or
comparator was normalized. This same-host Linux CPU synthetic-fixture result
does not grant third-party redistribution or public pretrained rights, or imply
full-product, GUI, GPU or provider acceptance. All **182 indexed raw receipts**
and their archive members were independently hash-verified.
[Current installed replay receipt](../evidence/product-completion-20261013/acceptance-6323bfd/installed-wheel-replay.json)
records exact hashes and scope. Earlier 151-file evidence remains unchanged.

### Current Mac unsigned development DMG: v12

The Mac build host reports clean exact-candidate source, both **154-file**
closures matching and **45,381 sealed payload files**. Current scoped passes:

- Frontend **530**; source/rights/packaging/assistant **151 passed, 68 Linux-only
  cases skipped**; mandatory actual MPS **1 passed**
- Mandatory MLX **6 passed in 349.74 seconds**, including full/LoRA training,
  evaluation, checkpoints, warm start, fresh-process export and cancellation;
  the full pipeline also verifies deadline cleanup
- Bundled ModelBuilder-side Triggerfish preparation **33 passed**; packaged core
  **6 passed**; read-only mounted-DMG core **6 passed in 112.89 seconds**
- Mounted strict inventory, four runtime module locations and Code - OSS CLI
  passed; detach exited **0**

These are **build-host reported** results. The actual current unsigned arm64
**v12 DMG is 1,166,352,946 bytes**, reported SHA256
`04695dbb092599ec3868480f675e27d6c5dd4b0f72b236f161a6f1a368b59178`.
It supersedes v9 as the current candidate artifact; the older v9 and held v11
records below retain their historical scope. The full **55,260,489-byte** raw Mac
archive remains on the build host, reported SHA256
`fc9fc931b70012436297c73ce44b90fd429e74af727f7ed441bd8052e0b0c31c`.
The evidence-backup upload route is blocked and has not been retried; raw archive
and artifact bytes have **not been independently retrieved or hashed here**.
[Mac scope and run identities](../evidence/product-completion-20261013/acceptance-6323bfd/mac-build-host-report.json)
retain this distinction. These overlapping selections are not summed.

### Remaining gates

Current native GUI remains blocked by the locked Mac. Signing/notarization and
clean-machine acceptance have not been performed; no product release is published.
Moonshiner remains Linux-only; the Mac platform gate does not establish Mac
composition. Bundled Triggerfish preparation does not establish upstream
client/agent/provider execution. Linux AppImage inputs, Windows/Linux ARM64/actual
CUDA, live providers, authenticated assistants, online tracking, hostile-code
isolation and redistribution remain unresolved or unverified. Local checks are
not a hosted-CI pass. Historical sections below apply only to their named source
checkpoints; their status wording does not describe the current candidate.

## Current frozen candidate d573fe0: aggregate completed with one rights failure

Candidate `d573fe0e3656cdcb77608c0c3ff0bb841756d8f4` pins runtime
`edca4154021e7b1fc68b3503803dac67e0ba1ae6`, the identical **154-file** source/lock
closure in both shipping recipes. **The full local aggregate exited 1**. Its only
aggregate failure is the unchanged public pretrained lifecycle admission gate,
`license_rights_unreviewed`; full-product acceptance remains incomplete.

- Light: **2,759 passed, 141 skipped, 88 deselected** in 205.08 seconds;
  frontend: **530 passed** with type checks and compilation; offline Hub SDK:
  **18 passed**; offline Modal SDK: **3 passed**.
- Native lifecycle: **30 passed, 1 failed** in 1,214.29 seconds; integration:
  **53 passed, 43 deselected** in 1,657.97 seconds; components: **569 passed, 9
  skipped** in 675.55 seconds; concurrency: **18 passed, 4 deselected** in
  412.86 seconds.
- All four native groups were attempted. The queue completed in
  **2,072.671 seconds**, below its **2,100-second** deadline.
  Cleanup completed with **0 signaled processes**. Skips remain recorded;
  no approval was fabricated and no rights check was weakened or excluded.

[Compact terminal evidence](../evidence/product-completion-20261013/acceptance-edca415/README.md)
records original raw receipt hashes, independently verified source closures and
exact scopes. Overlapping selections are not added as unique coverage. The
previous 151-file aggregate and strict replay remain at their named checkpoint;
no full installed-wheel replay result is inherited by this 154-file candidate.

### Mac scope: resealed build-host checks, no new DMG

The Mac build host reports both **154-file** closures matching and **45,381 sealed
payload files**. Scoped checks passed: **6 MLX**, **1 MPS**, **6 core/MCP/snapshot**,
**33 bundled ModelBuilder-side Triggerfish**, **150 combined tests with 48
Linux-only Moonshiner skips**, and **530 frontend tests**. These selections may
overlap. They do not establish upstream Triggerfish client/agent execution.

These are **build-host reported** results. The raw 60,984,605-byte evidence archive
remains on the Mac and has not been independently retrieved here; its reported
SHA256 is recorded in the [Mac scope receipt](../evidence/product-completion-20261013/acceptance-edca415/mac-build-host-report.json).
**DMG packaging is held. There is no d573fe0/v11 mounted-DMG acceptance.** The
actual existing development DMG remains the older **v9 / f99c5d5 / 1dc9698**
artifact. Its checks below are not relabeled as current-candidate results.

### Moonshiner public-workflow gap and separate staged correction

The frozen candidate includes Linux-only Moonshiner 0.8.23 composition through
the existing native process ABI. Earlier 97 focused checks and one genuine
installed-release native-process check establish only their lower-level scope.
A later actual public service-to-ZenML workflow found two adapter gaps:
`DatasetArtifact` input typing rejected ordinary dictionary dataset bindings,
and a fictitious top-level schema guard rejected the actual registered contract.
The full aggregate selection does not establish that public workflow.

A **separate five-file staged correction**, based on d573fe0, changes only the
production Moonshiner adapter plus tests/docs. Its public registration/review/
binding → service → ZenML → official 0.8.23 composition → prepared validation/
registration/policy binding/binding path passed **71 focused tests** (zero skips).
The default selection separately passed **69 with 1 skip**. Earlier failing
attempts are retained. **Those passes are not d573fe0 runtime or aggregate
acceptance.** The [separate correction receipt](../evidence/product-completion-20261013/acceptance-edca415/later-moonshiner-correction.json)
records exact staged file hashes and evidence scope. It does not establish
provider generation, real-teacher execution, downstream training/loss masking,
GUI, full-flow HTTP/MCP transport or full-service cancellation.

### Triggerfish, replay probe and remaining gates

The frozen source includes the flat MCP transport and reviewed inactive project
preparation. Earlier 164 focused Python, 40 managed-core/packaging and 530 frontend
checks remain overlapping adapter-stage evidence. Mac bundled-side checks above
do not establish actual upstream client/agent or provider acceptance. Audited
fixed port 18789 and command-allowlist/configuration limits remain; preparation
is inactive and grants no install, daemon, credential or activation authority.
See [compatibility scope](../distribution/TRIGGERFISH_COMPATIBILITY.md).

This runtime also includes the replay-probe `-I -B` correction and its focused
regression. It does not change the older 1dc9698 strict comparison or establish
new full replay/installer acceptance. Current native GUI remains blocked by the
locked Mac; signing/notarization, clean-machine acceptance, Linux AppImage inputs,
Windows/Linux ARM64/actual CUDA, live providers, authenticated assistants, online
tracking, hostile-code isolation and redistribution remain unresolved or
unverified. No hosted-CI pass or product release is claimed. Historical sections
below are preserved byte-for-byte at their named source checkpoints.

## Latest frozen candidate: aggregate complete with one rights-gate failure

Candidate `f99c5d5024090a20c30ba9077b9614ac2ea86475` pins runtime
`1dc969898d8c30a8a0c0a0e590e696639138e588` and the identical **151-file**
source/lock closure in both shipping recipes. It implements reviewed Git-free
source intake and inherited rights through generation, judging, selected-subgraph
reuse and prepared-dataset registration. **The full local aggregate finished
with exit 1; full-product acceptance remains incomplete.** Source publication
does not establish a product release.

### Completed frozen Linux aggregate

- Light: **2,675 passed, 140 skipped, 88 deselected** in 198.96 seconds;
  frontend: **528 passed** with type checks and compilation; offline Hub SDK:
  **18 passed**; offline Modal SDK: **3 passed**.
- Native lifecycle: **30 passed, 1 failed** in 1,183.43 seconds; integration:
  **53 passed, 43 deselected** in 1,628.16 seconds; components: **522 passed,
  8 skipped** in 681.86 seconds; concurrency: **18 passed, 4 deselected** in
  424.72 seconds. All four native groups were attempted.
- The queue completed in **2,054.807 seconds**, below the 2,100-second deadline.
  Cleanup completed with **zero signaled processes**. Eight component skips
  remain: four MLX boundary cases, two MLX service cases, one accelerator-Trainer
  gate and one actual-CUDA gate.
- The sole aggregate failure is
  `test_pretrained_validation_selection_parent_and_export`, blocked at admission
  by `license_rights_unreviewed`. The public pretrained rights issue remains
  unresolved; no approval was fabricated and no check was weakened or excluded.
  Component and concurrency groups now pass at this checkpoint; their older
  failed attempts remain historical failures.
- Service/workspace/source-pin/history HTTP checks, real local PDF/OCR HTTP/MCP
  and native component-authoring checks completed. HTTP/dialog-double checks
  and offline SDK tests do not establish native GUI or live-provider acceptance.

The [compact acceptance record](../evidence/product-completion-20261013/acceptance-1dc9698/README.md)
contains independently checked Linux receipt hashes, terminal counts, source
inventory verification and explicit evidence scope. Overlapping selections are
not summed as unique coverage.

### Current installed-wheel Git-free public-review replay

Two native executions passed through the public receiving-project source bind,
local evidence review, source-policy bind and prepared-run workflow using
Git-free imported snapshots. **The unchanged strict comparator failed, 34/35
checks**, only at `output_hashes_equal`: **seven scientific output files, metrics
and evaluation records match exactly**; three manifests differ exclusively
under `/rights` with genuine independent policy/evidence and derived-lineage
identities. No identity or comparator was normalized to force equality.

Four fresh noneditable wheel environments and all four dependency checks passed.
All 821 originally tracked checkout files, 151 frozen runtime files, imported
source bytes and installed application bytes remained unchanged. Explicit
installation-phase precompilation preceded the replay baseline; afterward no
runtime/dependency bytecode changed. The unchanged final backend identity probe
at `scripts/replay_bundle.py:535` uses `-I` without `-B`, so this precompiled
baseline is **not sealed-payload immutability proof**. Both workers exited 0,
were reaped with terminated process groups, and exact retries reconciled one
operation/job per project.

The synthetic review terms cover only self-authored fixture inputs and outputs.
They do not clear third-party runtime/dependency redistribution, upstream data,
public pretrained inputs or external publication. Each imported tree has the
151 frozen files plus separately authored `TERMS.txt`; its synthetic author
commit is transport provenance, not a new shipping-runtime identity. The first
staging verifier incorrectly treated the ordinary `editables` dependency as an
editable application; its failure is retained. The corrected verifier checks
actual wheel metadata, editable hooks and `.pth` paths, without runtime changes.

[Current replay receipt](../evidence/product-completion-20261013/acceptance-1dc9698/installed-wheel-replay.json)
records the scoped result, unchanged strict comparison, exact output hashes,
source/installation checks, cleanup and failed attempts. All **146 indexed raw
receipt files** and their archive members were independently hash-verified here.
The older 2e60975 replay is not inherited as current evidence. Scientific equality
does not make the strict result or full product pass.

### Exact-candidate Mac development DMG

The native Mac build host verified both 151-file closures and clean source, sealed
**45,375 payload files**, and built an **unsigned arm64 development DMG** for this
candidate. Verified local outcomes are **6 MLX passes in one run**, **1 bundled
MPS pass/24 deselected**, **6 mounted core/MCP/snapshot passes/7 deselected** in
111.99 seconds (including Git-free registration and restart), **116
snapshot/rights/packaging passes** and **528 frontend passes**. The mounted test
process exited 0; CLI, four runtime import locations, `hdiutil verify` and detach
passed.

These results are **build-host verified**. The terminal report is retained here;
the full raw Mac archive has **not been independently retrieved or hashed in
this verification workspace**:
the evidence-backup upload route is explicitly blocked. The complete 60,976,956-byte
archive remains on the Mac build host. The
[scoped Mac report](../evidence/product-completion-20261013/acceptance-1dc9698/mac-native-executor-report.json)
records the build-host-reported DMG/archive hashes without publishing private paths.
The DMG is 1,153,628,971 bytes; its filename is
`modelbuilder-ide-0.1.0-darwin-arm64-dev-unsigned.dmg`.
The earlier 2e60975 development DMG remains historical evidence.

### Remaining gates

Current native GUI remains blocked by the locked Mac. Signing/notarization,
clean-machine acceptance and a canonical reproducible installer are unverified;
no product release is published. Linux AppImage assembly still lacks offline
editor/backend/toolchain inputs. Windows, Linux ARM64, actual CUDA, live providers,
authenticated vendor assistants and online tracking remain unverified.
Hostile-code isolation and redistribution clearance remain unresolved. Hosted CI
remains disabled; these local checks are not a hosted-CI or full-product pass.
Historical sections below preserve earlier attempts and their original scope.

## Historical 2e60975 runtime: failed aggregate and later repairs

Published checkpoint `8996355f8a3273da8f8adee6649f36667f39b993` pins runtime
`2e609759ab5e86912f43a9a979e446030927c9a8` and its **150-file** source/lock
closure. The corrected full local aggregate **exited 1**. Later uncommitted
source and fixture changes have separate focused results below; they do not
turn that aggregate into a pass or establish full-product completion.
[Compact receipts and original-evidence hashes](../evidence/product-completion-20261013/acceptance-2e60975/README.md)
record the distinct scopes. Historical evidence below is unchanged.

### Corrected full local aggregate

- Light: **2,606 passed, 140 skipped, 88 deselected** in 165.33 seconds;
  frontend: **480 passed**; offline Hub SDK: **18 passed**; offline Modal SDK:
  **3 passed**. Offline SDK tests make no live-provider claim.
- Native lifecycle: **30 passed, 1 failed**; integration: **53 passed,
  43 deselected**; components: **468 passed, 24 failed, 8 skipped**;
  concurrency: **17 passed, 1 failed, 4 deselected**.
- Native queue status: `group_failed`, exit 1; total elapsed **1,978.149
  seconds**. The last group finished at 1,978.044 seconds. Cleanup completed
  with **zero signaled processes**.
- The public pretrained lifecycle fixture remains blocked by unresolved
  license/rights evidence. No rights approval was fabricated. Test-only repairs
  for the 24 component failures and one concurrency failure passed isolated
  groups of **119, 76 and 4 tests**; a successful full corrected aggregate
  covering those repairs has not been established.

### Installed-wheel replay on the frozen runtime

The `9caa54f` source candidate uses the same `2e60975` runtime. Four fresh
installed environments, independent native stores, source/installed-byte checks,
locked offline installs and all four worker terminations were verified on the
same Linux CPU host.

- Supported Git-backed public source review reached two successful native runs.
  The unchanged strict comparator returned **FAIL, 34/35 checks**: seven
  scientific output files, metrics and evaluation records match exactly;
  three rights-provenance manifests differ with genuine independent review
  policy IDs/timestamps. Those identities were not rewritten to force equality.
- A lower-level, explicitly test-owned authority path passed **35/35 checks**
  and ten exact output hashes. This is not acceptance of the complete public
  review/replay workflow.
- The frozen runtime has no supported Git-free snapshot review intake.
  Unreviewed shipping custom and public pretrained recipes stopped before
  submission, creating no operation/policy records or native jobs.

### Later uncommitted changes

Git-free source-snapshot review intake is now integrated in the working tree:
**62 focused tests and 528 frontend tests passed**. These changes are not in the
published 150-file runtime or its installed-wheel replay receipts. The broader
light diagnostic still **failed: 2,638 passed, 2 failed, 140 skipped,
90 deselected**, in 196.75 seconds. The two native-cache failures now have test-fixture corrections with **28 focused tests passing** in
the working tree; the failed diagnostic remains recorded. The subsequent
combined light diagnostic passed **2,673 tests, with 140 skipped and 90
deselected**, in 197.88 seconds. Its selection deliberately excluded the two
shipping source-pin tests until the new source is committed and repinned; it is
not a full aggregate pass. Generated-data lineage is now implemented in the working
tree: exact candidate/judgment content and parent rights survive selected-subgraph
reuse and prepared-dataset registration. Final-source focused checks passed
133 rights/input-boundary tests, 181 service/dataset/installed-wheel tests and an
actual CPU generation/judge/preparation test that rejects revoked and foreign-
project parents. Native inspection reads inert JSON without invoking nested
materializers. The full integrated aggregate remains an open gate. Counts from these overlapping selections are
not added as unique coverage.

### Packaging and remaining gates

Separate Mac verification reports an actual **unsigned arm64 development DMG** for
`9caa54f` / `2e60975`, with mounted payload/CLI/module checks, five managed-core
lifecycle/MCP tests, bundled MPS and six MLX cases passing. These are provisional
reported checks pending verification of the final evidence archive. A fresh invocation of the unchanged committed runtime builder reproduced the
sealed payload. The AppleDouble correction uses a reviewed Git input archive
created with Python tarfile, without implicit BSD tar xattr sidecars. The latest
evidence archive upload is blocked; both archives remain on the Mac build host. Current native
GUI is blocked by the locked Mac; signing and notarization are not done. No
canonical reproducible installer or signed release is claimed.

Linux AppImage assembly remains blocked by missing offline editor, backend and
toolchain inputs; recovered wheels are not AppImage or Bazzite acceptance.
Windows, Linux ARM64, actual CUDA, live providers, authenticated vendor assistants
and online tracking remain unverified. Hostile-code isolation and redistribution
clearance remain unresolved. Local checks do not establish hosted-CI or
full-product acceptance.

## Historical e658e32 implementation and partial results

Candidate `e658e32a4c3e20750b3f389ddf2b3886d6bcc343` is published on `main`.
Both shipping recipes pin runtime `9ade0e893a4ffaa3ba81c78d463fd7dbc70735ec`
and its complete **149-file** source/lock closure.

The newest source adds guided project creation, native Hub repository workflows,
managed Hub credentials/runtime packaging, exact asset/source rights review,
reviewed MCP mutations, extensible training runners, scientific operations and
citation-bound research retrieval. These are implemented source paths, but the
complete product and installer acceptance remain unfinished.

Local combined frontend: **480 passed**, zero failures/skips, with TypeScript
and build checks. After source repinning, the frozen local aggregate light stage
passed **2,546 tests**, with **140 skipped and 86 deselected**, in 158.26 seconds.
The separate socket-blocked Hub SDK stage passed **16 tests**. Native component
authoring executed its new source-pinned component through ZenML, learned the
expected affine state after 100 updates and left native owners quiescent.
The native queue later failed; the explicit test-owned custom installed-wheel
replay passed 35/35 comparisons and 10 exact output hashes. These completed stages
do not establish a successful aggregate or installer.
Focused native CPU training, evaluation, warm-start, scientific blend/gate and
export tests also ran in isolated integration stages.

The anonymous locked Hub SDK public probe failed at DNS resolution before any
provider bytes arrived. Socket-blocked SDK tests are not online provider
acceptance. The pinned public `prajjwal1/bert-tiny` fixture is blocked by unresolved
rights review: its card declares MIT while linked upstream model material states
Apache-2.0, and the pinned repository has no standalone license/notice. No rights
approval was fabricated to make the fixture pass.

Mac build inputs survived and were hash-verified on October 13. The Mac is locked;
current native GUI, full managed app lifecycle and DMG acceptance remain pending.
Earlier MLX/MPS and aggregate results below apply only to their named old runtime.
Hub transfer budgets are configurable with conservative 16 GiB defaults; the
separate native export packager still has its existing 16 GiB limit.

## Historical accepted runtime: local aggregate and Mac runtime gates passed

The full product remains incomplete. Candidate
`5b38d9602b6f8ae36fc0dcaf28f0a6da8c8f7d4a` pins runtime
`0e825487ef94fa613e1606f458177ad009b1a5cf`; both shipping recipes and the
working-tree runtime match the complete **114-file** source/lock closure.
This candidate was pushed to `main` and the remote SHA was verified at
**2026-10-13 02:33:08 UTC**; publication of source does not establish an installer
or release. Integrated changes include bounded native stage concurrency, isolated MLX
training/evaluation/export, and verified legacy pretrained tokenizer assets.

### Linux aggregate and installed-wheel replay

- The complete local `scripts/test_all.sh` run **exited 0**. Light selection:
  **2,058 passed, 137 skipped, 84 deselected** in **86.29 seconds**. Original
  frontend: **357 passed**; TypeScript/compile and four workbench HTTP harnesses
  passed. Required local PDF/OCR HTTP/MCP and native component-authoring harnesses
  passed. Offline, socket-blocked Modal SDK construction: **3 passed**.
- The four-group native queue completed in **1,923.375 seconds**, below its
  2,100-second deadline: lifecycle **30 passed** (1,232.05 seconds); integration
  **50 passed, 43 deselected** (1,526.02 seconds); components **373 passed,
  8 skipped** (346.54 seconds); concurrency **18 passed, 4 deselected**
  (395.53 seconds). Every group exited 0; cleanup completed with **zero signaled
  processes**. Component skips are four required MLX boundary cases, two MLX
  service cases, one accelerator-Trainer gate and one actual-CUDA gate.
- Independent installed-wheel replay passed **35/35 comparisons per fixture**,
  including exact hashes for **8 custom and 12 pretrained outputs**. Four fresh
  environments/native stores, before/after source and installed-byte checks,
  and all four worker terminations were verified. The actual complete portability
  suite passed **224 tests, zero skips**. Linux replay imports MLX inertly;
  native MLX hardware evidence is the separately scoped Mac result below.

See the [compact acceptance evidence](../evidence/product-completion-20261013/acceptance-0e82548/README.md)
for terminal counts, cleanup, output hashes, source inventory and original receipt
hashes. Light, native and focused selections overlap; counts are not added as
unique coverage. Later uncommitted distribution/frontend changes and their
separate test results are not covered by this original 357-test aggregate.

### Exact-candidate Mac runtime acceptance

The same `5b38d96` / `0e82548` candidate was materialized and all 114 pinned files
verified on an **Apple M3 Max, arm64** Mac, without runtime edits. The incoming
archive hash and all **107 manifest members** were independently checked here.
The pinned MLX/MLX-Metal 0.32.3 wheel tags require **macOS 26+ ARM64** for that
optional worker profile; this is not a global app OS minimum.

- **6 required MLX tests passed**, covering native full/LoRA service pipelines,
  actual export, warm-start, cancellation, export-descendant deadline termination
  and four boundary regressions. Training/export records **mlx-metal**; durable
  live-evaluation records correctly identify **mlx-cpu**.
- **1 mandatory MPS Trainer test passed, 24 deselected**. Affected service and
  placement/recipe selections: **363 passed, 13 skipped**. Exact-candidate
  frontend: **357 passed**. Optional-runtime skips remain recorded as skips.
- A fresh installed service wheel matched frozen runtime identity. A second
  independent **34-package MLX environment** loaded full and LoRA exports;
  both matched **48 logits exactly, maximum difference 0.0**. This is installed
  runtime/export portability evidence, not a single-app installer.
- Earlier missing-directory and nonexistent-test-file invocations remain failed
  attempts; corrected runs passed without runtime changes. Prior native GUI
  interaction stopped when the Mac relocked. Hardware results do not establish
  a current native GUI pass.

Details: [Mac runtime receipt](../evidence/product-completion-20261013/acceptance-0e82548/mac-runtime.json).

### Remaining release and product gates

- Single-app installer, managed service/MCP startup and current native GUI
  acceptance remain separate work. Later packaging changes need their own
  validation before any installer/release claim.
- Actual CUDA execution, Linux ARM64/Windows acceptance and complete
  cross-platform release verification remain unestablished. CPU queue passes and
  specific Mac MPS/MLX results do not imply general accelerator acceptance.
- The native Modal bridge is offline-tested. Production dispatch remains blocked
  by source-image provenance and authoritative all-in billing/runtime limits.
  Live authenticated vendor assistant calls and online tracking remain separate
  unverified gates.
- The host cannot establish required isolated worker namespaces. Resource limits
  and trusted-source worker isolation are not a hostile-code sandbox.
  Source licensing/redistribution clearance remains unresolved.
- Hosted CI remains disabled by owner instruction. These checks ran locally;
  no hosted-CI or paid-cloud pass is claimed.

Earlier results below are preserved only at their named source checkpoints.
They do not substitute for current runtime, GUI or packaging acceptance.

---

## Historical d51a90d / 106-file aggregate, recorded 2026-10-13

The full product remains incomplete. This frozen runtime checkpoint implements
Transformers Trainer/inference components, streaming datasets, conditional live
execution, bounded native iteration, PDF/OCR research ingestion, a locked CUDA
profile and an offline-tested native Modal bridge. The **complete local aggregate
passed with exit 0** for its declared Linux CPU selections. That result does not
establish the remaining product, platform, GUI or provider acceptance gates.

- The current combined light regression has **1,952 passed, 136 skipped and 63
  deselected** in **82.68 seconds**, including the repinned shipping source
  closures. The preceding 1,916-pass attempt had two stale-pin failures; those
  were corrected by
  committing the runtime and regenerating both manifests, not weakening tests.
- The final native queue completed in **1,561.526 seconds**: lifecycle
  **24 passed** in **1,219.55 seconds**; service/native integration **50 passed,
  43 deselected** in **1,484.27 seconds**; components **373 passed, 2 skipped** in
  **338.99 seconds**. All groups exited 0; `cleanup.complete` is true with
  **zero signaled processes**. The two component skips are the explicit
  accelerator-Trainer and actual-CUDA hardware gates, not CPU execution passes.
  See the [preserved final aggregate evidence](../evidence/product-completion-20261012/final-local-ci/summary.json)
  for the runner and per-group terminal receipts.
- Frontend checks have **349 passing tests**. The separate offline Modal SDK
  construction suite has **3 passes**; its socket-blocked tests make no live
  provider or billing claim. The preserved final aggregate evidence above
  records these terminal results alongside the native queue.
- Focused real CPU Trainer/native pipelines, streaming JSONL/Parquet consumption,
  general local inference and research HTTP/MCP extraction tests pass. Reports
  remain separately recorded; overlapping counts are not summed.
- The complete `tests/iterations/test_native_iterations.py` file passed:
  **12 passed, zero skipped, 339.737 seconds**. Its evidence verifies actual
  iterations, early exit, checkpoint ancestry, explicit warm-start and HTTP/MCP
  parity, including expected failure/cancellation paths. All **14 native job
  workers were reaped and their process groups terminated**, as recorded in
  `evidence/tests/native-iterations/combined-d51a90d.json`. Runtime and test hashes
  were unchanged during that run. General parallel scheduling within
  user-authored pipelines remains outstanding.
- The earlier complete CPU component preflight passed: **373 passed, 2 skipped**,
  exit **0**, with `cleanup.complete: true` in
  `evidence/product-completion-20261012/component-preflight/runner.json` and counts
  in the adjacent `results.xml`. This is one focused group, not a complete
  aggregate pass. These results overlap other selections and are not summed.
- The CUDA profile installs and passes CPU-side admission checks. Actual CUDA
  execution is unverified because this Linux environment exposes no GPU. The
  separate Mac task's mandatory MPS pass is scoped below; it does not establish
  general accelerator or other-platform acceptance.
- Fresh Linux Code - OSS source/toolchain and native-header preparation
  succeeded. The build stopped at an npm network policy denial. An offline-only
  retry then failed with `ENOTCACHED` for `vscode-uri-3.0.2.tgz`. Neither attempt
  establishes a current native build or GUI pass.
- The Modal SDK bridge is offline-tested; production dispatch remains blocked
  by source-image provenance and authoritative all-in billing/runtime limits.
  An account or spending approval alone does not resolve those design gates.
- The host cannot establish the required isolated worker namespace. No hostile
  code sandbox is claimed. Full native GUI and cross-platform runtime
  acceptance, live vendor assistant calls and online tracking remain separate
  unverified gates; the partial Mac results below do not close them.
- Hosted CI remains disabled by user instruction. All checks here run locally.

Runtime source is now frozen at `d51a90d93df4a150c088c2d9800c340168b911b4`;
shipping manifests are committed at `bdafebeb736a3839294755c2f2ba223a42cdd5b4`.
Both manifests pin the complete **106-file** runtime/lock closure. The aggregate
used test checkpoint `4154a2fdd6e5fed67f840ba6c9316cf06ccb3e46`, containing the test-only
held-discovery correction and documentation updates; runtime bytes and shipping
pins remain unchanged.

Independent installed-wheel replay passed for both fixtures: **35/35 checks
each**, exact hashes for eight custom and twelve pretrained output files, four
fresh environments and native stores, before/after source and installed-byte
verification, and verified worker termination. **182 portability tests** passed.
The complete local aggregate rerun has now **passed with exit 0**, including the
combined native queue above. Publication verification remains pending. An npm
registry policy denial interrupted the first aggregate before native tests; an explicit offline-only
retry passed that stage. Its native queue then failed an existing held-discovery
fixture after 23 lifecycle passes: the test asserted that every accelerator
probe was MPS, although discovery now also checks CUDA. Exact tracing reproduced
the stale assertion. The test-only fix retains lock, no-allocation, latency and
live-read/write assertions; all three real CPU cases and 180 related checks pass,
with one actual-CUDA hardware skip. Runtime bytes and shipping pins are unchanged.
The successful final rerun does not relabel the earlier canceled or not-started
groups as passes. Standalone iteration, preflight, light and native selections
intentionally overlap; their counts are not added as unique coverage. No hosted
CI ran.

A separate authorized Mac task on **Apple M3 Max, arm64, macOS 26.6.2** has
reported **1 mandatory MPS test passed**, **45 backend passes**, and **64 service
passes after a Mac-local snapshot-path fix**. The source-built Code - OSS app
passed **8 native software smoke checks**. An earlier GUI attempt timed out at
workspace trust; the later smoke passes do not establish all native GUI gates.
Mac-local fixes and standalone MLX work are **not yet integrated into the frozen
runtime source**. These separate results are not promoted to this Linux
aggregate, and no integrated MLX or full Mac GUI acceptance is claimed.

The preceding report below is preserved as historical evidence, not applied
to this source.

---

# Historical 2026-10-10 verification

Runtime source: `decf680d96ed92e28170580c09bf388c17a1ff22`, with an **86-file verified source closure**. This is an implementation checkpoint, not full-product completion. The [preceding 84-file report and passing d14 local CI](https://github.com/greghavens/protomire/blob/5475d94145ae68bca06fd3bca05bd52e3f43143c/docs/CURRENT_VERIFICATION.md) remain historical evidence.

## Verified at this checkpoint

- **Four clean installed-wheel runs, four independent environments/stores, 20 uncached native steps and 70/70 comparison checks.** All eight custom and twelve pretrained output hashes match. Numerical metrics and complete semantic evaluation/binding evidence match; independent native artifact/run/model-version ownership is checked separately. Export reload difference is **0.0**. All **86 source files, 65 service modules and 49 backend modules** match Git and installed wheel bytes before/after execution.
- **167 portability tests** and **10 native ABI tests** pass. ABI tests use the fresh backend wheel outside the checkout. Baseline dependency locks are unchanged and do not install optional PyArrow.
- Separate fresh profiles install the same wheels plus hash-locked PyArrow 25.0.0: **72 service and 104 backend distributions** pass compatibility checks. Registered Parquet feeds an actual native pipeline producing **four local GPT-2 candidates and four separate BERT predictions**; **25 installed boundary tests** pass. Module bytes, immutable assets and worker termination are verified. This uses ASGI routing and an independent worker, not live TCP/MCP or GUI. Tiny synthetic models establish execution/provenance, not quality or ground truth.
- **Full local CI passed with exit 0** against this runtime source and the repinned shipping examples: **1,781 light tests** (62 skipped, 33 deselected; 62.36 seconds), including both shipping source-closure checks. Contracts and tracked-artifact validation pass.
- **319 frontend unit/host tests**, both TypeScript checks, four real HTTP harnesses and native component authoring pass. Nine regression cases cover cleanup ownership, late writes, evidence retention and the non-native path. Dialogs use explicit doubles. The combined source-built Code - OSS compilation/assembly and launcher **1.105.0** pass; native window interaction is a separate gate.
- All **309 distinct native cases** passed: **25 service tests** (941.34 seconds), **24 lifecycle tests** (1,223.16 seconds) and **260 component tests** (255.13 seconds). Serial/queue collection matches without missing or duplicate cases. The bounded two-process queue completed in **1,224.728 seconds**; all groups exited 0, cleanup completed and no descendants required a signal. Light/native selections intentionally overlap and must not be added as unique coverage.

The first full-CI attempt passed its light/frontend checks but exited 1 after the component-authoring test's functional native pass: deletion raced the detached supervisor's final configuration write and raised `ENOTEMPTY`. The native queue did not start. Test-only cleanup now waits for inherited launch leases, birth-identified owner exit and confirmed worker/group termination before deletion or PASS; uncertain cleanup retains evidence. Deterministic regressions, both real harness modes and the complete second CI attempt passed. Runtime source remained unchanged; the failed attempt remains recorded.

The first optional-profile setup attempt stopped before environment creation because uv selected a read-only default cache. The explicit workspace-cache retry passed without runtime changes. Its failed log remains preserved. A held-admission native test also initially resolved its venv interpreter to base Python without ZenML. A test-only path correction preserved the venv; controlled red-to-green verification and the actual native rerun passed, with runtime source unchanged. Earlier failed attempts and corrections remain in historical reports; retries do not change their original status.

## Admission and placement boundaries

Run planning/submission/resume now prepare source, datasets, environments, device checks, native bindings, parent checkpoints and backend initialization outside the service writer transaction. Short reservations and final dispatch checks retain the exact initiating grant, immutable revisions, cancellation and idempotency authority. Run reads/cancellation also release the writer for backend I/O and recheck response authority. Other operations may retain writer scopes; this is not an all-API latency guarantee. An uncertain launch is reconciled without automatically repeating training. There is no cross-store exactly-once claim. See [admission and dispatch](admission-dispatch.md).

CPU placement is demonstrated. MPS requires the reviewed macOS ARM64 profile and remains **hardware-unverified**. CUDA execution is **unavailable** without a reviewed hash-locked CUDA profile. A tiny allocation probe does not prove full-model fit or a trusted component's actual device use. Live evaluation remains CPU-only. Accelerator exact-state continuation is rejected; supported warm-start remains explicit. Placement reserves no hardware and supplies no hostile-code isolation. See [local device placement](LOCAL_DEVICE_PLACEMENT.md).

## Remaining platform, provider and product gates

- Linux Parquet children enforce a **512 MiB address-space cap before Arrow parsing**, CPU/output/slot limits and a parent kill/reap deadline. These are resource limits, not a sandbox or parent RSS guarantee. Optional contained Parquet **fails closed on macOS/Windows**; JSONL/CSV remain usable. Linux arm64 runtime is unverified. Windows HANDLE readers exist but are host-unverified; native process supervision/checkpoint-subgraph binding remain unavailable on Windows.
- Native GUI/notification acceptance remains blocked by display/local-socket restrictions. macOS GUI requires a connected user environment; none was connected at the latest check. Build, dialog doubles and HTTP tests do not establish native interaction or other-platform acceptance.
- Controlled research tests pass. The latest actual Europe PMC request failed with `source_unavailable`; the same helper identified temporary DNS failure (`errno -3`). No content was retained or security fallback attempted. Online W&B, authenticated Claude/Codex inference and production remote-provider execution/billing are not established. Production remote execution remains disabled.
- Broader gaps remain: a comprehensive reusable catalog, arbitrary publisher/PDF/OCR retrieval and scientific synthesis, general branching/iteration/concurrency beyond supported conditions, and hostile-code isolation.
- Hosted CI is intentionally disabled by the owner; workflows are manual-only and no hosted run is authorized. Historical [run 38013134412](https://github.com/greghavens/protomire/actions/runs/38013134412) was canceled at its 40-minute cap and is not a pass. Earlier hosted runs are not asserted to have been free. This checkpoint's complete CI was run locally and passed as recorded above.
- Raw reports, hash/comparison attestations and environment metadata remain local. Completed generated environments were archived with full comparison/integrity checks and restoration instructions; source trees, native stores and proof remain preserved. No raw evidence publication is implied.

## Reproduce from source

On the supported Linux CPU profile:

```sh
bash scripts/bootstrap.sh --backend --tracking --parquet
MODELBUILDER_RUN_INTEGRATION=1 MODELBUILDER_REQUIRE_PARQUET=1 \
  MODELBUILDER_NATIVE_TEST_PROCESSES=2 bash scripts/test_all.sh
.venv/bin/python -m pytest tests/portability -q
.venv/bin/python scripts/replay_bundle.py --help
```

Complete replay requires independent service/backend environments and stores. Baseline fixture and optional-profile proofs are separate. See [dataset lifecycle](DATASET_LIFECYCLE.md) and [dependency profiles](../requirements/README.md).
