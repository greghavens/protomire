# 2e60975 acceptance checkpoint, 2026-10-13

**The corrected full aggregate failed, exit 1. Full-product acceptance remains
incomplete.** Published checkpoint `8996355f8a3273da8f8adee6649f36667f39b993`
pins runtime `2e609759ab5e86912f43a9a979e446030927c9a8` and its 150-file
source/lock closure. The replay candidate `9caa54f` uses that same runtime.

- [Scoped receipt](receipt.json): terminal aggregate counts and cleanup,
  failed test identities, installed-wheel/source verification, isolated fixture
  repairs and later working-tree diagnostics. Later uncommitted source is
  explicitly excluded from the frozen runtime results.
- [Unchanged public strict comparison](public-strict-comparison.json): **FAIL,
  34/35 checks**, with only full output-hash equality failing.
- [Public replay interpretation](public-api-comparison.json): seven scientific
  output hashes, metrics and evaluations match; three rights-provenance
  manifests differ with genuine independent policy IDs/timestamps. Review
  identities were not changed to force comparison equality.
- [Original receipt hashes](source-receipt-sha256.json): hashes of retained
  original logs and JSON/XML receipts. Logical labels avoid executor-private
  paths; large logs are not duplicated in this compact record.

The aggregate light stage passed 2,606 tests, frontend 480, offline Hub SDK 18
and offline Modal SDK 3. Native lifecycle had 30 passes/1 failure, integration
53 passes/43 deselections, components 468 passes/24 failures/8 skips, and
concurrency 17 passes/1 failure/4 deselections. Queue elapsed time was 1,978.149
seconds (last group finished at 1,978.044); cleanup completed with zero signaled
processes. The public pretrained rights blocker remains unresolved.

The 119-, 76- and 4-pass fixture-repair groups and the later 62 focused/528
frontend passes are separate uncommitted-work results, not a successful rerun
of the aggregate. The latest broader light diagnostic still has two cache-test
failures alongside 2,638 passes, 140 skips and 90 deselections. Subsequent cache
fixture corrections passed 28 focused tests in the working tree; no aggregate
rerun passed. Generated-dataset production lineage is under repair.

The lower-level fixed test-authority replay passed 35/35 checks and ten exact
output hashes; it does not establish the complete public workflow. The frozen
runtime's Git-free source-review gate was unavailable. Later source-snapshot
review implementation is not covered by those frozen replay receipts.

The Mac app summary is **provisional**. The final archive is not yet
independently verified here, so no current Mac raw receipt
is published in this directory. Unsigned development-app checks do not establish
native GUI, signing/notarization or a canonical reproducible installer. Linux
AppImage prerequisites, other-platform/actual-CUDA acceptance, online providers,
hostile-code isolation and redistribution clearance remain separate open gates.

These are local checks on explicitly scoped source. Overlapping tests are not
summed into unique coverage; historical failures are retained as failures.
