# T19 clean replay evidence

Final result: **PASS** for both genuine CPU fixtures.

Published source: `4d545df2b111de7f2365e8121a65717b37713054`.

- Four independent service/runtime install locations; own installed module paths and no original source imports or PYTHONPATH
- Independent native metadata/artifact stores and native run/artifact UUIDs
- Exact metrics, dataset hashes, checkpoint/export hashes and pinned resource hashes
- 17 packaging/security tests passed; combined recipe and portability suite: 56 passed
- All four normal wheel installs passed dependency checks; all native workers terminated and reconciled successfully

## Final native runs

- custom: origin `ad896910-a611-4bdf-a74b-99271fb37f29`; replay `330ed858-f0fe-482a-9ea5-cdca489362f9`
  - 29 checks passed; 6 output file hashes matched exactly
- pretrained: origin `45618d5c-00a9-4722-94c7-7a065b6c183d`; replay `762d1761-4801-4a9c-8ae5-b2927a876af0`
  - 29 checks passed; 10 output file hashes matched exactly

## Files

- `summary.json`: final source identity, counts, native IDs and numeric result
- `*-origin.json`, `*-replay.json`: complete environment, sys.path, installed module bytes, source identity, native artifacts, metrics and operation transcripts
- `*-comparison.json`: strict parity and environment/store separation assertions
- `bundles.json`, `*.recipe.tar`: inert source-only portable recipe archives and their SHA256 inventories
- `environment-install.log`, `service-lock-refresh.log`, `export-install.log`, `install-summary.json`: install history and final pinned dependency checks
- `pretrained-resource-resolution.json`: immutable external URL/revision/hash and independently resolved paths
- `termination.json`, `*-native.log`: final supervisor termination evidence and actual CPU training logs
- `unit.log`/`.xml`, `recipe-and-portability.log`/`.xml`: fast regression tests; these are distinct from the four real native CPU runs

See `docs/PORTABILITY.md` for reproduction. Tests use the same CPU host/OS; no GPU, cross-platform or untrusted-isolation claim. Source licensing is unresolved for redistribution. Pretrained weights and training checkpoints are not embedded in the archives or committed here.
