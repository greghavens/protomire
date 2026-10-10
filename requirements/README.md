# Python dependency profiles

The service/MCP environment and ZenML environment remain **separate**. Both use
Python 3.12.14 (`.python-version`). `scripts/bootstrap.py` detects the native
interpreter OS/architecture and installs the corresponding committed lock with
hash verification; it never resolves dependencies during installation. The
service keeps its existing exact versions, adding explicit platform dependencies
(colorama and pywin32 on Windows). `psutil==7.2.2` is a direct runtime dependency.

## Installation

Linux/macOS: `python3.12 scripts/bootstrap.py [--backend]` or
`bash scripts/bootstrap.sh [--backend]`.

Windows: `py -3.12 scripts/bootstrap.py` or `scripts\bootstrap.cmd`. No Bash,
POSIX virtualenv layout, shell activation, or Unix extraction utilities are
needed by the Python bootstrap. `MODELBUILDER_PYTHON` can select an explicit
interpreter for either shell wrapper. `--plan` prints detected paths/capabilities
without installing. Paths with spaces are passed as individual arguments.

Use `--service-venv` / `--backend-venv` (or `MODELBUILDER_SERVICE_VENV` /
`MODELBUILDER_BACKEND_VENV`) for explicit environment directories.
`PIP_CACHE_DIR` / `UV_CACHE_DIR` can relocate caches. Defaults remain under the
checkout. If relocating the backend, configure the running service's
`MODELBUILDER_BACKEND_PYTHON` to the printed absolute interpreter path. Windows
uses `Scripts/python.exe`; Linux/macOS use `bin/python`.

`--skip-frontend` installs only Python service/MCP dependencies; otherwise Node
20+ and npm are required and the workbench lockfile is installed/typechecked/built.
`--backend-only` installs only the backend. `--skip-model-download` explicitly
defers the verified pretrained fixture download; it is not a pretrained test pass.
The regular backend installation still verifies all pinned model file hashes.

## Capability matrix

| Native target | Service/MCP lock | Local pinned backend |
|---|---|---|
| Linux x64 | yes | ZenML 0.97.0, torch 2.9.0+cpu |
| Linux arm64 | yes | ZenML 0.97.0, torch 2.9.0+cpu |
| Linux armhf | shared architecture-independent Linux service closure; native source builds may be required | unavailable: no official torch 2.9.0 wheel |
| macOS arm64 | yes | ZenML 0.97.0, torch 2.9.0 |
| macOS Intel x64 | yes; native source builds may be required | unavailable: no official torch 2.9.0 wheel |
| Windows x64/arm64 | yes; native source builds may be required | unavailable: ZenML native Windows integration unsupported |

A hash-locked resolution is not native runtime acceptance. Linux x64 bootstrap
and selector tests can run here; Mac, Windows, ARM Linux, and armhf execution
require real target machines before their acceptance gates can be marked passed.
Source distributions are hash pinned too, but may need a compiler, Rust and OS
headers; their build toolchains are not established by a dependency lock alone.
Linux CPU Torch wheels require glibc 2.28+; Alpine/musl is not a tested backend.

Official [Torch CPU wheel index](https://download.pytorch.org/whl/cpu/torch/)
has CPython 3.12 torch 2.9.0+cpu wheels for Linux x64/arm64 and Windows x64/arm64,
and torch 2.9.0 for macOS arm64. It has no corresponding macOS Intel or Linux
armhf wheel. [PyTorch's Intel Mac deprecation announcement](https://dev-discuss.pytorch.org/t/pytorch-macos-x86-builds-deprecation-starting-january-2024/1690)
explains the upstream macOS x64 boundary. This does not disable the IDE or service
on those machines and does not silently downgrade Torch.

For Windows local training, run the complete **service and backend** inside
[WSL2](https://learn.microsoft.com/windows/wsl/install), with a separate Linux
checkout/environments, and connect the native IDE to that service. Alternatively,
connect to the service on a supported Linux/macOS host using its authorized HTTPS
configuration. A Windows service cannot launch a WSL interpreter merely by
setting `MODELBUILDER_BACKEND_PYTHON`; no WSL process-supervision bridge is claimed.
`--backend` still installs the native service/workbench before reporting the
backend capability error (exit 2). Plain bootstrap succeeds without the backend.

The Linux-authored fixture recipe's torch `+cpu` requirement is not interchangeable
with macOS torch. Runtime recipe lock/profile selection must match the selected
backend explicitly. Do not edit model hashes, loosen validation, or claim a native
training pass based solely on successful installation.

## Reproducible regeneration

`python scripts/lock_dependencies.py` uses uv 0.12.23 and Python 3.12.14 target
resolution. Select `--target darwin-arm64 --component backend` to regenerate one
profile. `requirements/service.in` retains exact original service/notebook/test
versions plus Windows-only pins and the macOS appnope pin. `backend.constraints` retains all original
transitive backend versions except Torch; the existing backend input selects
Linux CPU, and `backend-darwin-arm64.in` selects the official macOS build at the
same release. Each lock records the regeneration command and includes SHA256
hashes. Review generated diffs and run native acceptance before upgrading.

`requirements/backend.lock` remains the original fixture lock for compatibility
with existing recipe digests. It is not repurposed as a universal lock. Per-target
locks live in `requirements/profiles/`. The original `service.lock` is retained
as the baseline/tool pin record; bootstrap uses the hash-enforced profiles.


## Optional Parquet profile

`--parquet` overlays the exact hash-locked `pyarrow==25.0.0` wheel on each selected
service/backend environment. The service and backend still remain separate;
`--backend-only --parquet` touches only the selected backend. Tracking remains
separate and is unaffected. Use explicit fresh venv paths to keep a previous
frozen installation unchanged. A later backend sync without `--parquet` removes
the optional adapter, which causes Parquet execution admission to fail closed.

PyArrow 25.0.0 has no Python distribution dependencies. Baseline locks and their
existing fixture digests are not changed. Optional profiles cover official
CPython 3.12 wheels for Linux x64/arm64 (glibc 2.28+), macOS x64/arm64 (12+), and
Windows x64. Windows arm64 and Linux armhf fail before installation because no
reviewed official wheel exists. Installation is wheel-only with SHA256 checks;
resolution for other targets is not native runtime acceptance. Real runtime
verification here is Linux x64 only. The memory-contained Parquet reader is
currently Linux-only: macOS/Windows installation does not enable Parquet decoding
or execution admission until equivalent hard process limits are verified. A
structured capability diagnostic offers JSONL/CSV or a supported Linux backend;
the IDE and non-Parquet paths remain available. Native Windows training remains
subject to the backend limitations above.

Regenerate using `python scripts/lock_dependencies.py --component parquet`, or
add `--target linux-x64` for one profile. See
[the dataset adapter contract](../docs/DATASET_LIFECYCLE.md#optional-real-parquet-adapter).
Official artifacts and APIs: [PyPI release](https://pypi.org/project/pyarrow/25.0.0/),
[Apache Arrow ParquetFile](https://arrow.apache.org/docs/python/generated/pyarrow.parquet.ParquetFile.html).


## CPU CI coverage

The existing Linux CPU lifecycle job bootstraps `--backend --tracking --parquet`
using the committed profiles. `MODELBUILDER_REQUIRE_PARQUET=1` makes missing
PyArrow a failure in both service registration and native dataset execution tests.
The backend-only phase of `scripts/test_all.sh` sets
`MODELBUILDER_REQUIRE_LOCAL_INFERENCE=1` and runs the complete component suite,
including real generation → separate judge → explicitly reviewed dataset
preparation native pipelines. Missing Torch, Transformers or ZenML fails that
phase rather than silently skipping. These model libraries are already pinned
in the backend profile; they are not added to the service environment.
Generation/judge tests construct tiny independent local safetensors assets, so
this coverage adds no provider calls, online model fetches or large model download.
The existing verified pretrained fixture bootstrap is unchanged. This is one
existing CPU job, not a claim of native acceptance on other target platforms.


`bootstrap.py --plan` distinguishes optional wheel metadata from decoder support:
`parquet_wheel_available` reports whether a reviewed lock profile exists, while
`parquet_available` reports whether that profile's target supports the contained
runtime (currently Linux only). `parquet_unavailable` explains unsupported
runtime targets; `parquet_lock` remains available for reviewed macOS/Windows
wheels. This is a pre-installation platform plan, not proof that PyArrow is
installed, hard limits can be established, or a dataset will pass validation.
Actual use still verifies those conditions. Linux arm64 is not native-tested here.
