# Secure dataset reads on desktop platforms

The dataset registry now has two OS-native implementations. Linux and macOS retain the existing descriptor-relative `os.open(..., dir_fd=...)` / `O_NOFOLLOW` path. Native Windows uses the standard-library `ctypes` bridge to the Windows file APIs; it does not require WSL or a new third-party package.

## Windows containment and content contract

- Root registration resolves the operator-configured path and snapshots a native directory HANDLE's volume serial number plus 128-bit file ID. Every read reopens that root and compares the same native identity before opening any source child. A replaced root is rejected.
- Sources must remain canonical relative paths. Windows additionally rejects alternate data streams, DOS device aliases, namespace syntax, separators within components, control characters, lone surrogates, and trailing-dot/space aliases.
- Each child is opened by `NtCreateFile` using one component and the verified parent's `RootDirectory` HANDLE. The reader never concatenates a source to an absolute root path and then trusts a prior `resolve()` / `stat()` check.
- Every open uses `FILE_OPEN_REPARSE_POINT`; every child open additionally uses `OBJ_DONT_REPARSE`. Reparse-point attributes are rejected before reading a file or traversing a directory. This includes symbolic links, junctions, mount points, and cloud-placeholder reparse points; copy/materialize data to an ordinary approved directory first.
- Absolute root opens do not use `OBJ_DONT_REPARSE`: the DOS-device alias in `\??\C:\...` itself can trigger refusal on Windows 10. The root still uses no-follow at the final component, and its native identity must equal the startup snapshot before source traversal.
- Ancestor HANDLEs remain open until the bounded read completes. Only read sharing is allowed, preventing ordinary data-write and delete/rename opens. Attribute-only access is not excluded by Windows sharing rules, so no universal metadata lock or filesystem snapshot is claimed. Handle-relative no-follow traversal, before/after checks, and the immutable content hash remain mandatory.
- Metadata and contents come from the same opened file HANDLE. The reader requires a disk-backed non-directory file, rejects pending deletion and reparse attributes, checks the byte limit before reading, reads at most 64 KiB per call and at most the configured limit plus one byte, and compares native identity, exact byte count, size, last-write time, change time, and attributes afterward.
- `DatasetRegistry._read_source` hashes precisely the returned immutable bytes against the manifest SHA-256. Project root aliases remain project-scoped. Native failures become the existing path-redacted `dataset_source_unavailable` diagnostic; there is no insecure fallback.
- Filesystems or shares that do not support the required native handle metadata fail closed. A particular remote filesystem or third-party filter is not certified by these unit tests.

## Verification status and native acceptance

Linux algorithm and Windows ABI-contract mocks verify call flags, fixed-width structures, traversal order, identity changes, reparse refusal, limits, exact-handle reads, cleanup, content hashes, and project isolation. Existing real POSIX registry/HTTP/MCP tests run against the integrated implementation.

These checks are **not Windows acceptance**. Native Windows 10/11 x64 and Windows ARM64, and real macOS execution, require their own runners. The Windows-only tests exercise real ordinary reads, root replacement, junction refusal, sharing enforcement, checksums, project isolation, and a checked-directory reparse mutation attempt. Symlink tests need an already-authorized host capable of creating symlinks; tests never change Developer Mode, elevation, privileges, or OS security settings. Missing symlink-creation privilege is reported as a skip, not a pass.

Run in the repository's Python 3.12 environment:

```text
python -m pytest -q tests/datasets/test_registry.py tests/datasets/test_mcp_dataset.py tests/datasets/test_platform_file_access.py
```

The native Windows tests are skipped on other operating systems. Record operating system, architecture, filesystem, Python version, exact revision, passes, failures, and skips before declaring a Windows platform accepted. Real backend integration remains a separate suite.

## Primary API references

- [NtCreateFile user-mode API](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile) and [native create options](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/nf-ntifs-ntcreatefile)
- [OBJECT_ATTRIBUTES / RootDirectory / OBJ_DONT_REPARSE](https://learn.microsoft.com/en-us/windows/win32/api/ntdef/ns-ntdef-_object_attributes)
- [GetFileInformationByHandleEx](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex)
- [FILE_ID_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info), [FILE_BASIC_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_basic_info), and [FILE_STANDARD_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_standard_info)
- [ReadFile](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-readfile) and [CreateFile sharing semantics](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)
- [Microsoft handle-relative safe-file implementation](https://github.com/microsoft/hcsshim/blob/main/internal/safefile/safeopen.go) and [Windows 10 DOS-device alias compatibility fix](https://github.com/openai/codex/pull/47672)
