# Native Modal provider implementation and remaining admission blockers

## Status

The repository now contains a real optional provider integration, using the
existing ZenML **0.97.0 ModalOrchestrator** and public **modal 1.6.1** SDK. It is
not a new orchestrator, scheduler, training framework, or simulated provider.
The native submission bridge constructs the actual ZenML controller request;
status, ownership lookup, termination, rates, usage reports and billing summaries
use public provider SDK methods.

**Production admission remains blocked.** `cost.quote.create`, `cost.reserve`,
`ModalProviderAdapter.quote`, and `ModalProviderAdapter.submit` cannot enable a
paid run. There is no HTTP/MCP provider-registration, invoice-upload or dispatch
override. No configuration boolean, estimated price, manually asserted limit,
or successful offline test can satisfy financial admission.

No real account, credentials, paid API call, remote resource, remote run, cleanup
or invoice was used in this implementation's verification. The committed
evidence is local SDK construction and safety testing, not remote acceptance.

## Why Modal, and why it is not yet financially admissible

The existing [ZenML Modal orchestrator](https://docs.zenml.io/stacks/stack-components/orchestrators/modal)
reuses native snapshots and runs, runs a controller and children in Modal
Sandboxes, and requires a remote artifact store and container registry.

[Modal's current budget documentation](https://modal.com/docs/guide/budgets)
describes a workspace-wide usage cap and a separate net-charge spend limit.
Environment budgets cover compute rather than all workspace charges. These
controls make Modal a promising target, but they are not evidence that this
particular application's all-in total is bounded. Workspace isolation and the
actual configured limits must still be verified for an explicitly authorized
account.

The implementation fails closed for these specific reasons:

0. There is no verified build receipt linking an OCI image's contents to the
   approved source and environment lock. This is an explicit preflight and
   native-submission blocker, even when the local checkout/interpreter passes.
1. Modal 1.6.1's public Workspace settings API does not expose a spend-limit
   readback or an enforcement receipt. `WorkspaceSettings` contains the default
   environment and image-builder version. A screenshot or operator-supplied
   number is not a machine-verifiable provider guarantee.
2. ZenML's required external artifact store and registry can charge separately.
   A Modal workspace budget cannot cap another provider's storage, egress,
   requests or cleanup. Tax and plan-fee treatment must also be established.
3. The pinned native orchestrator calls `Sandbox.create` without a name or
   caller idempotency token. A durable local claim prevents the controller from
   submitting twice, but cannot establish provider-side idempotency.
4. Controller and step timeout settings are per sandbox. A late-created child
   can outlive the controller's deadline. This is not a provider-enforced global
   wall-clock deadline.
5. The SDK does not provide job-scoped final-invoice identity/finality or a
   provider storage-deletion receipt. Completion and an empty active-sandbox
   listing cannot release an accounting hold.

These are capability gaps, not merely missing credentials. Funding a smoke test
does not resolve them. Meeting the existing exact all-in contract requires
provider/account guarantees covering all charged systems plus native identity
and deadline support, or an explicit product decision to change the contract.
This implementation does not make that decision or silently weaken admission.

### Bounded alternative review

- SageMaker: [StoppingCondition](https://docs.aws.amazon.com/sagemaker/latest/APIReference/API_StoppingCondition.html)
  supports job runtime limits, including termination grace behavior; it does not
  establish an all-services final-charge cap. [AWS Budgets](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-best-practices.html)
  relies on delayed billing data.
- Google: current [spend-cap budgets](https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps)
  apply to one project/service, are not instantaneous, allow billed overages,
  permit in-flight work to finish, and do not eliminate retained-resource costs.
- Azure: [credit spending limits](https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/spending-limit)
  apply to eligible subscriptions, are not customizable, and exclude some
  separately billed services.

No reviewed existing ZenML target was established as an exact prepaid/all-in
substitute. Modal was selected for genuine SDK integration, not represented as
having passed the financial gate.

## Native plan, identity and recovery

`remote/modal_runtime.py` contains the optional SDK bridge. Construction and
imports never resolve credentials or contact a provider. Reads require an
explicit operator configuration; every read verifies the authenticated Modal
workspace against the configured workspace. Environment and ZenML identities
are bound separately.

`NativeModalPlan` records:

- Native run, project, snapshot, stack, orchestrator and artifact-store IDs
- Snapshot, recipe, source, environment-lock and runtime-environment digests
- Controller/step native-settings digest and immutable OCI SHA256 image references
- The exact submit key, workspace/environment and maximum per-sandbox runtime

`prepare_from_project` reuses canonical recipe normalization/digest checks,
the operator-owned `SourceWorkspaces` binding and committed file verification,
and the existing isolated installed-environment/lock verifier. It accepts no
public claimed build receipt or caller-selected source path. Lower-level
`prepare` can bind declared digests for diagnostic construction, but does not
mark local inputs verified. Neither route marks image/source linkage verified.
There is currently no accepted image-build receipt path: `_submit_native`
blocks before calling the transport bridge. This implementation does not
build/push remote images or attest their contents. Mutable image
tags, scheduled/dynamic pipelines, local artifacts, wrong stacks/environments,
and changed plans are rejected. The native submission bridge additionally
requires the same explicit preconfigured client used to verify the workspace;
it cannot verify one account and submit via another default profile.

`ModalJournal` binds one submit key to one native run and one immutable plan.
The exact approval is stored before the first possible side effect. SQLite's
transactional claim admits only one caller across connections. Process death,
timeouts and missing metadata leave `submission_unknown`; there is no reset or
automatic resubmit. Recovery requires the native controller ID to be positively
returned by the provider's approved-app listing, and refuses changed identities.
Mutable native metadata alone is not provider ownership proof. Finished IDs
omitted by the SDK's active listing remain unverified rather than being queried
by arbitrary ID or treated as safely owned. Native IDs are not replaced by
fabricated application IDs.

The internal transaction and native bridge are implemented for future admitted
dispatch. Their product entry point refuses at the financial gate today. They
are not exposed as an alternative public launch API.

## Status, termination and storage

[Public Sandbox APIs](https://modal.com/docs/sdk/py/latest/Sandbox) supply native
lookup, exit-code polling, active listing and termination. Modal 1.6.1's list
implementation excludes finished sandboxes, and its public lookup API does not
provide a separate app-membership query for an arbitrary finished ID. The adapter
polls only positively app-scoped handles. Unverified metadata IDs remain unknown
and are excluded from job billing attribution.

Operator termination requires the exact immutable plan, native run, workspace,
environment, actor and request ID, in addition to the operator enable setting.
Before any mutation, every initial native controller/child ID must appear in the
provider's exact approved-app listing. Missing ownership fails closed even if
an arbitrary ID lookup would report exit code zero. It then terminates the
positively owned controller first and re-reads native child identities and app
members. Only handles whose ownership was positively established during that
cancellation call may be re-polled after leaving the active list. Late children
require their own positive app membership; changed controller identity and
unattributed active children block completion. Errors remain unknown.

Persistent model/dataset artifacts and registry images are never deletion
targets. The native Modal path creates no managed ephemeral volumes here.
The adapter does not fabricate a storage-deletion receipt from sandbox exit or
attempt dangerous whole-bucket/whole-volume cleanup. `cleanup` reports the
specific missing storage proof; a successful termination does not settle costs.

## Billing evidence

[Workspace public APIs](https://modal.com/docs/sdk/py/latest/Workspace) return
current rates, object/time usage rows, and a monthly workspace summary. The
adapter stores decimal values as exact strings, including signed adjustments,
and attaches the source, provider/workspace, observation time and query interval.
It filters unrelated object/environment rows. App aggregates are retained
separately from directly identified sandbox rows; they are not counted twice or
silently assigned entirely to one job.

[Modal's billing documentation](https://modal.com/docs/guide/billing) explains
collection delays and adjustments. Empty or partial usage is `pending`, not a
zero invoice. Workspace `billed_cost` is preserved as provider-reported data,
but is neither assigned to one run nor labeled final without a finality signal.
`ProviderObservation` requires cumulative job money, so this adapter refuses to
manufacture one from incomplete usage; the financial hold remains retained.

## Optional installation and local verification

The new profile is intentionally separate from service and CPU defaults.
Only Linux x64/Python 3.12 is resolved and locally tested here. The lock contains
the complete 29-package SDK dependency closure, resolved against the existing
backend constraints; it is not a bare `modal` wheel.

```sh
UV_CACHE_DIR=.cache/uv uv venv --python .venv-backend/bin/python .venv-remote-modal
UV_CACHE_DIR=.cache/uv uv pip sync --python .venv-remote-modal/bin/python \
  --require-hashes --only-binary :all: \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  --index-strategy unsafe-best-match \
  requirements/profiles/backend-linux-x64.lock \
  requirements/profiles/remote-modal-linux-x64.lock
UV_CACHE_DIR=.cache/uv uv pip check --python .venv-remote-modal/bin/python
.venv/bin/python -m pip --python .venv-remote-modal/bin/python check
MODELBUILDER_REQUIRE_MODAL_SDK=1 PYTHONPATH=src \
  .venv-remote-modal/bin/python -m pytest \
  tests/test_remote_modal.py tests/test_remote_modal_sdk.py
```

This installs code only; it neither creates an account nor connects credentials.
Do not run account-backed methods without authorizing the specific account and
data access. The native default service/MCP environments remain unchanged.

Verification recorded under `evidence/remote-modal/`:

- The independent environment installed 113 total backend/SDK packages
- Both uv and pip dependency checks passed
- Real pinned ZenML-to-Modal request construction passed with sockets forbidden
- Local tests cover cross-connection claims, unknown recovery, plan mutation,
  workspace mismatch, exact termination authorization, unknown child ownership,
  Decimal precision, missing billing and no false cleanup/invoice assertions
- Real remote acceptance, funded concurrency, global deadline enforcement,
  actual storage cleanup and invoice finality: **NOT RUN / BLOCKED**
