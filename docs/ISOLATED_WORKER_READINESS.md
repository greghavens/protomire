# Isolated worker readiness

## Status: blocked, not implemented or accepted

As checked on 2026-10-12, this executor cannot start the available sandbox
runtime with the required namespace boundaries. Modelbuilder continues to
refuse untrusted execution. No isolated execution target, managed environment
launcher, container build path, or relaxed trust gate was added.

The local CPU/accelerator paths remain **trusted local execution**. Source
snapshotting, dependency hashes, clean Python startup, filtered environment
variables, and process-group cancellation do not make those paths a sandbox.

Evidence: [host probe and regression results](../evidence/isolation/host-readiness-2026-10-12.json).

## Verified environment blocker

The only discovered sandbox runtime command was `/usr/bin/bwrap`, version
0.12.0. A bounded, harmless startup probe requested separate namespaces, a new
session, a read-only `/usr`, an empty environment except `PATH`, and the payload
`/usr/bin/true`. No project source, host home, credential, or backend store was
mounted. The full command is in the evidence file.

The runtime exited with status 1 before the payload started:

```text
bwrap: loopback: Failed to create NETLINK_ROUTE socket: Operation not permitted
```

Docker, Podman, runsc, Firecracker, Kata, runc, and crun were absent from `PATH`.
Neither `/sys/fs/cgroup` nor `/dev/kvm` was visible. The invoking process had no
Linux capabilities, `NoNewPrivs: 1`, and `Seccomp: 2`. Those last observations
do not prove which particular platform rule caused the denial.

No attempt was made to disable the network namespace, change security
settings, run privileged, mount control sockets, or otherwise bypass the denial.
Filesystem, network, process, environment, resource-limit, artifact-egress,
and cancellation/recovery sandbox acceptance tests consequently did **not** run.
A failed startup is not a passing boundary test.

## Separate implementation gaps

A capable host alone would not make the product feature complete:

- `backend/admission.py` requires per-run trust and an operator-trusted source
  root. OCI digests, non-default node environments, and CPU/memory reservations
  are refused. Capability discovery correctly reports `isolation: false` and
  `oci: false`.
- `backend/supervisor.py` starts the existing worker in a local process group.
  Its PID identity, wait, and descendant observations are local-process
  lifecycle evidence, not sandbox instance ownership or cgroup termination.
- `backend/worker.py` and `backend/components/runtime.py` import trusted source
  within the ZenML worker. The worker can access its native store and job
  directory. Sharing this whole state directory with hostile source would
  undermine the control-plane and provenance boundary.
- Interpreter/lock inspection verifies an already installed environment.
  There is no managed image resolution, build, immutable image admission,
  per-stage environment provisioning, or isolated artifact transfer adapter.

These are product implementation gaps, not capabilities secretly available
behind configuration. A new feature must retain ZenML's native orchestration,
typed steps, and artifact identities rather than replace them with a new
training framework or a subprocess-only imitation.

## Smallest suitable next environment

One operator-provided CPU Linux executor is sufficient to begin implementation
and real acceptance; a GPU, paid cloud service, or cluster is unnecessary.
It needs a supported, maintained sandbox runtime which is permitted to start
there, enforce network/process/filesystem separation, and apply bounded
CPU, memory, process-count, temporary-storage, and output limits. Aggregate
process-tree resource limits require an effective resource controller, such
as operator-delegated cgroup v2; per-process `rlimit` settings alone are not
equivalent. Provision this as a separate approved environment rather than
loosening this executor's restrictions.

A gVisor-backed OCI runtime is a candidate for review. It provides an
application-kernel boundary and integrates with existing OCI tooling; its
availability still does not prove this application's security or compatibility.
See the [official gVisor overview](https://gvisor.dev/docs/). The exact launch
mode must be checked: the simple `runsc --rootless` mode has documented
networking/cgroup limitations and is not automatically suitable for these
requirements. See the [rootless runtime documentation](https://gvisor.dev/docs/user_guide/rootless/).

Bubblewrap is another candidate only with a reviewed complete policy and
effective resource control. Its own documentation makes the caller responsible
for the sandbox policy and warns about exposed mounts and IPC. The presence of
its binary is not a verified execution target. See the
[official security model and limitations](https://github.com/containers/bubblewrap#sandbox-security).

The environment must also provide a reproducible, content-addressed runtime
image or root filesystem built from approved locked dependencies. That image
must contain only the required interpreter, platform adapter, and workload
dependencies, with no host home, credentials, service sockets, source checkout,
or native metadata store. No provider, account, runtime installation, or
security-policy change was configured during this assessment.

## Acceptance required before offering an isolated target

1. Keep trusted ZenML orchestration outside the hostile-code boundary. Stage
   only declared source, approved data, and typed input artifacts into an
   immutable per-invocation view. Bind runtime/image digest, source/data hashes,
   policy version, resource limits, and native step/run identity into provenance.
2. Use explicit default-deny network and filesystem policies. Demonstrate that
   synthetic host-home secrets, unrelated workspace data, host services,
   metadata endpoints, host processes, IPC, and inherited descriptors are
   unreachable. Verify immutable inputs cannot be modified or replaced.
3. Keep credentials, the native metadata store, supervisor records, and
   cancellation/provenance records outside the sandbox. Allow only bounded,
   validated result/artifact egress. Do not deserialize hostile Python objects
   or load attacker-selected materializers into the trusted process. Reject
   symlinks, hard-link escapes, devices, sockets, traversal, oversized trees,
   malformed metadata, and output races before native artifact ingestion.
4. Enforce and measure CPU, memory, process-count, temporary-storage, output,
   log-volume, and wall-time limits over the complete sandbox process tree.
   Tests must use small bounded adversarial fixtures, not host exhaustion.
5. Use durable sandbox-instance ownership for submit, cancellation, service
   restart, and recovery. Confirm all descendants terminated before claiming
   cancellation, retain unknown state when proof is missing, avoid duplicate
   launch after uncertain acknowledgements, and remove disposable state safely.
6. Exercise real typed ZenML steps, data consumption, artifact registration,
   checkpoint recovery, failure, cancellation, source/data drift, and policy
   mismatch through the normal HTTP/MCP contract. Never silently fall back to
   trusted local execution when the isolated target cannot start.

The current focused regression checks passed: 9 startup/credential-filtering/
untrusted-refusal tests and 42 admission/environment-lock tests. They confirm
existing fail-closed behavior only; they are not sandbox acceptance or a full
product test pass.
