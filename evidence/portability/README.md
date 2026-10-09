# T19 clean replay evidence

Final result: **PASS** for both genuine CPU fixtures under the corrected single-model domain.

Published source: `086908e3eb20ff025bbd50108e6b6563855b7512`.

- One owned recipe ID per project; raw `bundle_json` import advances r1 to r2, preserves r1 unchanged, and execution records the exact r2 ID/digest
- Four independent service/runtime install locations; own installed module paths and no original source imports or PYTHONPATH
- Independent native metadata/artifact stores and native run/artifact UUIDs
- Exact metrics, dataset hashes, checkpoint/export hashes and pinned resource hashes
- 24 packaging/security tests passed; combined recipe and portability suite: 63 passed
- All four normal wheel installs passed dependency checks; all native workers terminated and reconciled successfully

## Final native runs

- custom: origin `0ca7813f-fbd3-4d8b-97a4-fc71e7ea1709`; replay `677da5f0-f93e-475b-beca-47450ca5450c`
  - 31 checks passed; 6 output file hashes matched exactly; exported-logit difference 0.0
- pretrained: origin `34ff5340-e365-4550-9a62-d22c1a66f66d`; replay `de4a0253-0ed9-4ce5-ab00-b2aff767c1fe`
  - 31 checks passed; 10 output file hashes matched exactly; exported-logit difference 0.0

## Files

- `summary.json`: final source identity, counts, native IDs and numeric result
- `recipe-identity.json`: project-owned identity, r1 preservation, import r2 and exact execution provenance for all four runs
- `*-origin.json`, `*-replay.json`: complete environment, sys.path, installed module bytes, native artifacts, metrics and operation transcripts
- `*-comparison.json`: strict parity and environment/store/domain separation assertions
- `bundles.json`, `*.recipe.tar`: inert source-only portable recipe archives and their SHA256 inventories
- `environment-install.log`, `service-lock-refresh.log`, `export-install.log`, `install-summary.json`: install history and final pinned dependency checks
- `pretrained-resource-resolution.json`: immutable external URL/revision/hash and independently resolved paths
- `termination.json`, `*-native.log`: final supervisor termination evidence and actual CPU training logs
- `export-logit-parity.json`: actual exported model reload parity
- `unit.log`/`.xml`, `recipe-and-portability.log`/`.xml`: fast regressions, distinct from four real native CPU runs
- `historical/`: superseded source checkpoints; these are not current acceptance evidence

See `docs/PORTABILITY.md` for reproduction. Tests use the same CPU host/OS; no GPU, cross-platform or untrusted-isolation claim. Source licensing is unresolved for redistribution. Pretrained weights and training checkpoints are not embedded in the archives or committed here.
