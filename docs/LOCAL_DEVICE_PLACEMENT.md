# Bounded local device placement

The existing ZenML local orchestrator and process supervisor remain authoritative
for native run/artifact identities and cancellation. This adapter chooses a
framework device; it does not reserve CPU, RAM, VRAM or GPUs and is not a sandbox.
Trusted Python can ignore its contract. Other processes can consume the same GPU.

## Explicit selection and profiles

`environment.device` is recipe-versioned: `cpu` (legacy default), `mps`, or an
explicit `cuda:<visible ordinal>`. No automatic selection, unindexed CUDA or
silent CPU fallback is accepted. Submission target must match the saved device.
The GUI asks for an available, project-policy-permitted target and rejects a
mismatch rather than changing the saved recipe. Enable a local target through
existing project policy controls; existing CPU-only policies are not widened.

- CPU: existing hash-locked profiles are unchanged.
- MPS: requires the reviewed `backend-darwin-arm64.lock` digest, exact installed
  versions, macOS ARM64 and an MPS-capable configured Torch 2.9.0 interpreter.
  Native MPS acceptance is not verified by Linux tests.
- CUDA: currently unavailable because no reviewed hash-locked CUDA environment
  profile has been established. The adapter rejects before execution, even when
  physical discovery reports NVIDIA hardware. No runtime or driver is installed.

Targets/discovery and planning do not allocate tensors. Authorized submission
performs a bounded tiny forward/backward and synchronization check in the selected
interpreter for MPS and built-in Torch CPU training. A passing probe is not proof
that the user's full model fits or all its operators are supported. Placement
metadata records the requested/validated runtime device separately from observed
component execution: `requested_device`, `allocation_verification_scope` set to
`tiny_tensor_probe_only`, and `component_execution_observed: false`. A trusted
component may ignore the supplied context; its declaration and the tiny probe do
not establish that its training ran on an accelerator.

Device metadata and allocation probes run outside the service writer transaction,
alongside prepared dataset reads. Immediately before allocation and at atomic
dispatch, the service rechecks the exact initiating grant, project/recipe/policy,
source bindings and settings, interpreter identity, and prepared immutable dataset
bytes. Probe results are deep-copied, request-local preparation, never a persistent
capability or authorization grant. A missing or changed preparation fails closed;
commit never retries a driver probe under the writer lock. Venv configuration
identity uses a bounded, no-follow, nonblocking regular-file descriptor check;
unsafe files or platforms lacking those primitives refuse placement.

The combined admission/dispatch path also prepares source inspection/hashing,
environment metadata, native bindings, continuation checkpoint materialization and
bounded backend initialization outside the writer. Short transactions reserve the
idempotency key and durable intent, recheck current authority/settings, commit a
resume revision with its final dispatch fence, and reconcile the native result.
Run status, logs, artifacts, evaluations, events and cancellation likewise release
the writer during backend I/O and recheck response authority afterwards. Other
operations, such as explicit checkpoint materialization and model export, may
retain their existing writer scopes; this is not a blanket all-API latency claim.

Matching keys never relaunch an existing intent. An unchanged correlation can
replay its original receipt; once correlation advances, retries return current
state without obsolete native liveness fields. Missing process/native metadata
keeps an attempted launch uncertain and reconcile-only. These local DB/filesystem/
process boundaries do not provide cross-store exactly-once execution. See
[admission and dispatch](admission-dispatch.md) for authorization fences, cancellation
and restart behavior.

## Framework execution

Built-in training moves the real PyTorch model, frozen KL reference and batches to
the selected device. Initialization, dataset preparation, evaluation and exports
remain CPU operations. Context-aware training components must declare their
supported device families in `component.capabilities.devices` and honor
`context.device` / `context.placement`; omitted devices mean CPU only.
`context.resume_modes` exposes effective capabilities; an accelerator component
checkpoint that still advertises exact-state continuation is rejected. Other
component roles receive CPU context. Plain `code.python` is rejected for an
accelerator recipe because it lacks this component context contract.

Actual fixture checkpoints include device/runtime identity, float32 precision,
CPU and accelerator RNG state. Device synchronization precedes publication, and
saved model/optimizer tensors are detached CPU copies. Framework architecture,
source hashes, source snapshots and native ZenML provenance remain in use.

## Live evaluation and continuation

Live evaluators are separate CPU-only processes: CUDA/HIP visibility is cleared,
MPS fallback is disabled, and component context explicitly selects CPU. This is a
trusted-component policy, not an OS security barrier. Existing immutable snapshot
verification, durable queue and checkpoint/score notifications are reused. No
shared-GPU concurrent evaluation or memory isolation is advertised.

CPU exact-state continuation retains its existing tested compatibility checks.
Accelerator exact-state continuation is rejected, including migration from an
accelerator checkpoint to CPU. Explicit `warm_start` is supported where a trainer
already supports it. Public checkpoint `resume_modes` are filtered by saved
request/recipe placement even for recovered checkpoints. A nested component
`descriptor` remains the original immutable declaration, with its scope labeled;
its hash is unchanged and its declarations are not effective capabilities.
A hardware probe does not promote exact-resume capability.
CPU/GPU numerical identity is not guaranteed by PyTorch across devices/releases.

Cancellation remains cooperative checkpointing, then process-group termination
with existing PID/termination proof and native status reconciliation. A force kill
may lose work after the last complete checkpoint. Process exit does not establish
driver health or instant global device-memory reclamation. Native Windows process
supervision is still unavailable; a native VSCode frontend does not change that.

## Evidence boundary

Real Linux CPU tests cover the tiny allocation probe, device-aware optimization,
checkpoint tensor copies, evaluation, immutable checkpoint publication,
cancellation and CPU exact resume. Real native CPU checkpoint/evaluation ingestion
and an independent service writer also progress while another actual submission
is held at environment validation; cancelling that pending intent prevents a
second spawn (`tests/integration/test_admission_live_native.py`).
Synthetic MPS probe responses test admission, profile/identity rejection and lack
of exact-resume advertising; they are not hardware execution evidence. Actual
CUDA/MPS training, OOM handling, driver cleanup, native cancellation and numerical
continuation require corresponding hardware before those acceptance gates pass.

Official references: [PyTorch MPS](https://docs.pytorch.org/docs/2.9/notes/mps.html),
[CUDA semantics](https://docs.pytorch.org/docs/2.9/notes/cuda.html),
[reproducibility](https://docs.pytorch.org/docs/2.9/notes/randomness.html),
[ZenML local orchestrator](https://docs.zenml.io/stacks/stack-components/orchestrators/local).
