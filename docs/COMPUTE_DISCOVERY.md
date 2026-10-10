# Compute discovery and its limits

`execution.targets` now returns a `hardware` observation alongside the existing
execution target list. HTTP, the native MCP tool `execution_targets`, and the
Compute workbench use this same operation. No additional target or write operation
is introduced. Project, model and recipe identity are unchanged.

## What is measured

- CPU: psutil logical/physical core counts, process affinity count where supported,
  and a real 100 ms aggregate utilization sample. Unsupported affinity is null.
- RAM: psutil total, available and used bytes plus utilization percent.
- NVIDIA: installed `nvidia-smi` from fixed system locations, using a read-only CSV query for GPU names, dedicated VRAM
  total/used/free, and utilization. Driver-reported unavailable metrics are null.
- Linux DRM: bounded sysfs inventory of device/vendor IDs, optional product name,
  and AMD-driver VRAM/utilization counters where exposed. Missing counters are null.
- macOS: bounded native display inventory. Apple unified memory is explicitly
  shared system RAM; it is never represented as independently available VRAM.
- Windows: local CIM display names/vendors. The 32-bit `AdapterRAM` property cannot
  establish actual VRAM capacity and is deliberately not queried or trusted.
- The configured backend interpreter optionally queries already-installed Torch
  for CUDA/ROCm device count and MPS availability. This is a separate runtime view;
  counts are never added to hardware counts or promoted to execution targets.
  Torch is not imported into the service. No tensor allocation, model import,
  training, kernel execution, dependency install, or device-properties query occurs.

Observations belong to the operation-service host, which may differ from the IDE
computer. No device serials, UUIDs, user names, processes, or network identifiers
are queried. Output is returned to the authenticated caller; no discovery data is
uploaded to any provider or tracking service.

## Bounds and degraded operation

Discovery dispatch runs after normal scope, project and schema checks, outside
the SQLite write transaction. Slow probes do not hold the store lock or prevent
other requests from reading checkpoint/status state or committing writes.
Native subprocesses use fixed arguments without a shell, a minimal environment,
a filesystem-root working directory (never a project/source checkout),
no PATH/current-directory driver discovery or request-supplied executable,
no stdin, discarded stderr, a 128 KiB stdout ceiling, and 2-second timeouts. Torch
has a 3-second timeout and runs with isolated, no-bytecode Python flags. Timeout
cleanup has bounded waits. Native inventories are capped at 64 reported adapters.
Probe failure is reported by status without raw error output, and cannot fabricate
capacity or turn a missing metric into zero. CPU/RAM observations survive failed
optional accelerator probes. A successful empty inventory means “not detected,”
not proof of physical absence; inaccessible probes report unknown coverage.

A single-flight, process-local 10-second cache prevents repeated/concurrent view
requests from spawning repeated native probes. The response includes sample time,
collection duration, cache age and TTL. “Refresh hardware and targets” refreshes
through the shared operation; a request inside the TTL returns the labeled cache.
There is no persistent polling service or background telemetry export.

## Observed versus executable versus enforced

Only `local-cpu` remains an executable target, subject to the existing source,
interpreter, platform and supervision checks. Native Windows remains unsupported
by the pinned ZenML execution backend even if its hardware inventory succeeds.
Observed GPUs have no execution-target ID and are not reservable. CUDA/ROCm/MPS
visibility does not imply model compatibility, a working training backend, or a
supported accelerator run.

CPU/RAM reservations, GPU execution and GPU reservations remain unsupported.
Resource-request admission continues to reject unsupported CPU, memory and GPU
requests. Host-visible counts and available RAM are not guaranteed free capacity;
container quotas and other workloads can reduce usable resources. Unknown effective
CPU/memory quotas remain null. Remote execution, charging and budget-enforcement
claims are unchanged and disabled.

## Verification scope

Focused tests exercise actual Linux CPU/RAM sampling and the real HTTP service plus
an independent official MCP stdio client, including read-only access, matching
inventory payloads, no submitted runs and unchanged project/recipe identity.
NVIDIA, AMD sysfs, macOS and Windows tests use explicitly synthetic fixtures.
Malformed output, absent tools, permissions, null metrics, timeout/output limits,
subprocess credential isolation, cache concurrency and unavailable backend behavior
are covered. TypeScript tests and a Chromium DOM check exercise rendering,
unknown values and unavailable targets. A Chromium DOM test is provided for repeat
refresh, busy state, project switching, old services and closing results with
simulated host messages. Its execution in this staging environment was blocked
at browser startup by a disallowed local process-singleton socket; the Chromium
sandbox was retained. No browser-rendering pass is claimed.

These checks do **not** establish actual GPU hardware behavior, native macOS or
Windows behavior, GPU training, cloud execution, or native Code - OSS GUI acceptance.
Those require their own real-host evidence after integration.

## Adapter references

- [psutil API](https://psutil.readthedocs.io/stable/index.html)
- [NVIDIA System Management Interface](https://docs.nvidia.com/deploy/nvidia-smi/index.html)
- [Linux AMD GPU driver sysfs](https://docs.kernel.org/gpu/amdgpu/driver-misc.html)
- [Windows Win32_VideoController](https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-videocontroller)
- [PyTorch CUDA device count](https://docs.pytorch.org/docs/stable/generated/torch.cuda.device_count.html)
