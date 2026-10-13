# Frozen edca415 / d573fe0 acceptance, 2026-10-13

**Full local aggregate: FAIL, exit 1. Full-product acceptance is incomplete.**
Candidate `d573fe0e3656cdcb77608c0c3ff0bb841756d8f4` pins runtime `edca4154021e7b1fc68b3503803dac67e0ba1ae6`
and identical **154-file** closures in both shipping recipes.

- [Terminal receipt](receipt.json): final counts, exact failure, queue and cleanup.
- [Source verification](source-verification.json): recipe hashes, shared inventory
  fingerprint, all 154 working-tree/runtime-commit hashes independently checked.
- [Original raw receipt hashes](source-receipt-sha256.json): named aggregate and
  per-stage logs/JSON/XML preserved separately before any subsequent aggregate.
  No giant raw logs or private executor paths are included in this compact record.
- [Mac build-host report](mac-build-host-report.json): scoped counts and reported
  archive hash, not independently retrieved raw Mac evidence or new DMG acceptance.
- [Later Moonshiner correction](later-moonshiner-correction.json): separate staged
  source/evidence, explicitly not included in the frozen candidate above.

## Terminal aggregate

Light: **2,759 passed, 141 skipped, 88 deselected** in 205.08 seconds; frontend
**530 passed** with type checks/build; offline Hub SDK **18 passed** and Modal SDK
**3 passed**. Native lifecycle: **30 passed, 1 failed** in 1,214.29 seconds; integration: **53 passed, 43 deselected** in 1,657.97 seconds; components: **569 passed, 9 skipped** in 675.55 seconds; concurrency: **18 passed, 4 deselected** in 412.86 seconds.

All four native groups were attempted. Queue elapsed **2,072.671
seconds**, below **2,100**; cleanup complete, **0 signaled processes**.
The sole aggregate failure is public pretrained lifecycle admission with
`license_rights_unreviewed`. No rights approval was fabricated or gate weakened.
The aggregate selection does not establish all public workflows. Counts overlap
and are not added as unique coverage; skips and earlier failures remain literal.

## Mac scope and actual artifact

Build-host reported: both 154-file closures, **45,381 sealed files**, **6 MLX**,
**1 MPS**, **6 core/MCP/snapshot**, **33 bundled-side Triggerfish**, combined
**150 passed/48 Linux-only Moonshiner skips**, and **530 frontend**. The
60,984,605-byte archive remains on the Mac; reported SHA256
`9f867c64cf54cab54164e93a324b0df7559fb3fa39f3c6398ec14537d0bad3ba`.
Raw bytes were not independently retrieved here. **DMG packaging is held; there
is no v11/current-candidate mounted pass.** The actual DMG remains historical
v9/f99c5d5/1dc9698. Native GUI, signing/notarization and clean-machine gates remain.

## Separate later Moonshiner correction

The later actual public service-to-ZenML workflow found that `DatasetArtifact`
input typing rejects ordinary dictionary dataset bindings and a fictitious
top-level schema guard rejects the real registered contract. A five-file staged
patch fixes the production adapter plus tests/docs. Its public registration/
review/binding → service → ZenML → official Moonshiner 0.8.23 composition →
prepared validation/registration/policy binding/binding workflow passed **71
focused tests**. A separate default selection passed **69 with 1 skip**.

Those results are **not d573fe0 acceptance**; the correction is outside this
frozen source. Earlier failing attempts remain preserved. The 192 indexed archive
members were independently hash-verified. No provider/real-teacher generation,
downstream training/loss masking, GUI, complete HTTP/MCP transport or full-service
cancellation pass is claimed.

## Historical and remaining scope

The earlier 1dc9698 aggregate retains its one licensing failure; its unchanged
strict replay remains **FAIL 34/35**, with seven exactly equal scientific outputs
and three rights-only provenance differences. The current runtime's `-I -B`
probe correction does not rewrite those results or establish a new full replay.
No 154-file installed-wheel replay, upstream Triggerfish client/agent/provider,
other-platform/actual-CUDA, hosted-CI or full release pass is inherited. Public
pretrained rights, hostile-code isolation and redistribution remain unresolved.
