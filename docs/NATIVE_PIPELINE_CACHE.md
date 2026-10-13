# Bounded native pipeline reuse

This increment uses the public ZenML 0.97 `CachePolicy` and native cached-step
records. It does not add another scheduler, run registry, artifact registry or
training loop. It implements a conservative part of evaluation G08: explicitly
cacheable completed value stages can survive a later stage's failure or actual
supervised cancellation, and be reused when resubmitting the exact same recipe
revision and selection with a new operation/idempotency key.

## Supported contract

Set the existing node field `cache_policy` to `enabled` only for a deterministic,
side-effect-independent stage:

- `code.python` with exclusively `dict`, `str`, `int`, `float`, `bool` or
  `DatasetArtifact` input/output ports
- `component.run` with the same value ports and role `dataset` or `utility`

Outputs must be plain finite JSON with string dictionary keys, maximum depth 32,
100,000 traversed values and 16 MiB per value. Cache reuse cannot reproduce a
stage's external writes. Trusted Python purity cannot be inferred automatically;
choosing `enabled` explicitly declares that its output depends only on pinned
source/dependencies, declared parameters and native inputs. All other nodes
remain uncached by default. Training, evaluation, export, model-role components,
Path/checkpoint ports and fixture built-ins reject `enabled` during both planning
and submission. This bounded capability is not a claim of general model caching.

No new API operation is needed. Existing human JSON recipe editing and HTTP/MCP
recipe updates can set this field. The existing trust, source closure,
installed-environment and dependency admission checks apply unchanged. Caching
is not an untrusted-code boundary.

## Identity and integrity

The application-owned SHA256 cache identity includes the exact canonical recipe
ID/revision/digest, project, node declaration, selected nodes and resolved
bindings, parent checkpoint/resume identity, installed package/interpreter/
platform identity and installed Modelbuilder execution-source digest. Job paths,
new operation IDs and run names are excluded. Every native cache lookup repeats
the existing read-only source/environment/dependency admission; a hit cannot
skip validation that normally precedes trusted code execution.

ZenML includes project, native artifact store, generated step implementation,
materializer and exact upstream artifact IDs in its own cache key. Artifact
values are deliberately disabled in this cache policy: a new native input ID
causes a miss even if its content appears equal. No MD5 content equality is used
as the application's immutable input authority. Changes to any recipe field,
source/data dependency, selected binding or actual runtime therefore invalidate
reuse. This is deliberately stricter than cross-revision selective reuse.

Each original successful step logs its plain-JSON output SHA256s in native step
metadata before native materialization. ZenML's public no-argument `cache_func`
checks matching completed candidates through the public SDK, verifies same
project and original output ownership, then reads bounded `data.json` through
nofollow descriptor-relative POSIX handles and checks its SHA256. It never
imports a project materializer or uses the container materializer's alternate
object reconstruction path. Missing, changed, symlinked, foreign, oversized or
unsupported cached values fail the run instead of being silently reused. The
query is bounded at 1,000 matching original steps; exceeding that bound requires
an uncached/new revision, not unbounded history scanning.

As with explicit native input bindings, these are checks at use time, not a lock
against another trusted process changing artifact storage immediately afterward.
No hostile concurrent-mutation isolation is claimed. Native Windows is already
unsupported by this backend; secure POSIX reads remain required.

## Native provenance and scientific decisions

`run.status` adds `cached_node_ids` and `node_statuses`; a cached stage retains
its actual native `cached` status. The existing `executed_node_ids` continues to
mean native participated/started steps, including cache hits, for compatibility.
Each run artifact and explicit binding descriptor adds `cache`, with `reused`,
`original_step_run_id`, `producer_step_run_id` and `producer_backend_run_id`.
The current run remains the consumer of a reused artifact; the original
completed producer step and its actual run are never relabeled. A completed
step from a failed/cancelled parent run can be reused natively. Its new completed
consumer run can supply an explicit selected-subgraph binding while retaining
both provenance levels.

Selected checkpoint bindings, comparison contracts, evaluator selectors and
promotion/export ownership are unchanged. Evaluation stages are never cached by
this increment, and cache reuse never treats duplicated observations as an
independent scientific experiment.

## Native SDK facilities versus product support

Actual installed ZenML 0.97 local probes established:

- A dynamic conditional true branch ran three increment steps; false ran only
  its gate. Native step records reflected the actual branch.
- A bounded dynamic loop produced three distinct native step invocations.
- Two dynamic `step.submit()` inline steps overlapped in time.
- Static independent local steps remain sequential.
- Identical native cache inputs reused the same artifact ID and a public
  `original_step_run_id`; changed input executed anew.

These historical SDK probes are not general Modelbuilder loop/concurrency
acceptance. The default adapter retains static DAGs and the synchronous
[branch/optional-join path](NATIVE_CONDITIONAL_EXECUTION.md). Current
[bounded iteration](NATIVE_BOUNDED_ITERATION.md) and
[opt-in parallel stages](NATIVE_PARALLEL_EXECUTION.md) have their own native
acceptance. Parallel stages use per-invocation deadline journals and fresh trusted
process bodies to avoid sharing source imports and framework-global state.
The native public step pool still owns scheduling, retries and cache artifacts.
The branch-condition document explains the dynamic-parameter cache-key
adaptation; declared typed input artifact IDs remain authoritative.

The installed local orchestrator does not honor resource reservations and does
not provide isolated dynamic steps. A real `runtime="isolated"` probe completed
in the orchestration process (identical PID); the native recorded configuration
still said `isolated`. Installed `dynamic/compilation.py:get_step_runtime`
explicitly overrides unsupported isolated execution to inline. Configuration
flags therefore are not evidence of process isolation. Per-stage environment, CPU/memory/GPU
reservations remain rejected. A container/remote stack requires a separately
authorized verified environment; this work neither changes Docker/security nor
spends on a provider.

Primary documentation and installed source inspected:

- https://docs.zenml.io/concepts/steps_and_pipelines/dynamic_pipelines
- https://docs.zenml.io/concepts/steps_and_pipelines/advanced_features
- ZenML 0.97 `config/cache_policy.py`, `orchestrators/cache_utils.py`,
  `orchestrators/local/local_orchestrator.py`,
  `execution/pipeline/dynamic/runner.py`, `materializers/built_in_materializer.py`

The current docs can evolve independently of the pinned package. The runtime
probe results are the authority for this installed 0.97 environment. None of the
above proves native GUI, Windows/macOS/ARM, GPU, remote or hostile-code acceptance.
