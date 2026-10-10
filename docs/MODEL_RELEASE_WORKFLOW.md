# Compare, decide, promote and save a model

A project still owns one model and one evolving recipe. Runs and model versions
remain authoritative ZenML records; export packages are disposable snapshots,
not a second run registry.

## Workbench

In **Runs**, select two or more completed runs, enter the metric and select its
direction. The comparison shows measured values, compatibility diagnostics and
a diff of the immutable configurations actually executed. Incompatible data,
splits, protocols or metric identities produce no winner. Create a measurable
hypothesis in Research, then record an accepted, rejected or inconclusive
decision in the comparison panel. Accepted decisions pin the comparison digest.

In **Versions**, load native versions, select an accepted decision and promote
the winning version to a native stage. Promotion rechecks the evidence and
project revision; it cannot promote a different winner. Native stages are local
metadata, not deployment or publication. A stale project revision is refreshed
for review instead of automatically retried.

**Save model package…** is available on a completed run and each version. It
lists verified existing export artifacts and their selected checkpoints, then
uses the IDE's native Save dialog. Cancelling the picker or Save dialog does not
create a package. An in-progress transfer is cancellable. Project changes,
modified destinations, symlinks and integrity failures prevent finalization;
incomplete files are removed where the original directory remains accessible.
The destination is checked immediately before the final local rename; this is
not a cross-process filesystem lock. Avoid editing or moving the destination
while a save is finishing.

Recipe bundle import/export remain native **File** actions. Model Save never
starts training, re-runs an exporter, publishes, enables online tracking or
contacts a paid/cloud provider. Read-only tokens may save verified artifacts;
recording decisions and promotion require modify scope.

## Shared API and MCP

The authenticated `/v1/operations/{operation}` dispatcher exposes the same
operations to the native MCP server (`.` becomes `_` in tool names):

- `model.export.list`: project ID plus native run ID or model-version ID. Lists
  verified export descriptors, declared assets, selected-checkpoint identity,
  source/recipe provenance and diagnostics for invalid exports.
- `model.export.inspect`: the same selection plus an exact native artifact ID.
  Returns an `export_digest` over manifest, asset sizes and provenance.
- `model.export.package`: the selection and `expected_export_digest`. Rechecks
  native ownership and all declared bytes, then creates a ZIP containing only
  declared assets, `export.manifest.json` and `modelbuilder.provenance.json`.
- `model.export.read`: project, run, version, artifact and package IDs plus a
  zero-based `chunk_index`. Returns at most 4 MiB of bytes, base64 encoded,
  with its hash. No endpoint accepts a filesystem path or returns an
  unauthenticated download URL.

The package response pins the archive SHA256, size, chunk hashes and the
content-addressed transfer descriptor. A client must verify each chunk and the
complete archive before exposing a completed destination. Each request is
independently authenticated and project scoped before cache access. The cache
contains no credentials and is not an authority for runs or versions.

Limits: export manifests are at most 4 MiB, 10,000 declared assets and 16 GiB
of asset bytes. Native Path archives are manually extracted with bounded sizes;
symlinks, hard links, devices, traversal and duplicate entries are rejected
before extraction. Only the pinned native Path format in the owned local
artifact store is supported; arbitrary/custom materializers are never invoked
by these operations. Assets are read through no-follow directory-relative file
handles and copied/hashed from the same handle. Undeclared files are never
included in the final package.

The selected checkpoint is reverified against the export step's actual native
inputs. A best/selected checkpoint is never silently replaced with the last
checkpoint. Provenance contains native IDs, immutable recipe/source identity,
declared dependencies and the recorded selection decision, never process
environment variables or service credentials. Saving does not execute the
loader. Review packaged source before running it yourself.

## Verification

`tests/integration/test_model_release_workflow.py` performs real local native
training of independent runs, compatible comparison, decision recording,
promotion, bounded retrieval, ZIP verification and a fresh isolated Python
process reload with zero prediction difference. A third changed-data run has
no compatible winner and cannot be promoted. The test also checks stale
promotion, wrong-version and cross-project rejection.

`tests/service/test_model_exports.py` covers declared-only packaging, content
and descriptor tampering, path/symlink/device/archive attacks, bounds, scope and
HTTP authentication. Frontend tests cover selectors, readonly controls,
comparison diagnostics, preserved drafts and native Save cancellation,
project/destination changes and integrity failures.

## Portable export file reads

The export/package reader now uses native secure file access on both OS families:

- Linux/macOS keep directory-relative `os.open` with `O_NOFOLLOW`, regular-file
  checks and pre-read size bounds.
- Windows reuses `platform_file_access.py`: a drive/UNC anchor is identified by
  a native HANDLE, every child is opened relative to its verified parent, and
  reparse points, alternate data streams, device names and unsafe components
  fail closed. All ancestors stay open with read-only sharing until the stream
  closes. The file HANDLE is transferred to Python's CRT/FileIO exactly once,
  never reopened by path. Streaming, hashing and random-access package chunks
  therefore read the same verified file without buffering the entire export.
- Windows metadata, identity and size are checked around native reads/seeks and
  again at close. Reads are at most 64 KiB at the underlying file boundary;
  transfer chunks remain at most 4 MiB. Unsupported filesystem identity queries
  or sharing semantics fail closed; there is no ordinary-path fallback.

This portable reader does not remove the pinned native ZenML execution backend's
separate Windows limitation. Windows IDE clients can save from a supported
service host. The portable package reader can also operate independently on a
Windows host; no claim of native Windows ZenML training is made.

Verification for this change: Linux tests exercise the real POSIX implementation
and portable/mock Windows dispatch, ownership, streaming, bounds, seeks, cleanup,
root replacement and reparse/metadata failures. These are not real Windows
execution. The Windows-only tests in
`tests/service/test_export_platform_access.py` cover actual HANDLE/CRT ownership,
sharing enforcement, inspect/package/chunk round trips and exact hashes; they
remain unrun/skipped here. Run them alongside
`tests/datasets/test_platform_file_access.py` on each supported Windows runner
before marking Windows accepted. Real macOS acceptance is also separate.

The same-handle adoption follows Python's
[`msvcrt.open_osfhandle` API](https://docs.python.org/3.12/library/msvcrt.html#msvcrt.open_osfhandle)
and [Microsoft's CRT handle ownership contract](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/open-osfhandle?view=msvc-170).
No additional packages, security settings or OS privileges are installed or changed.
