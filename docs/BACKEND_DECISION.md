# Backend decision: retain ZenML, refine the adapter

## Decision

Retain ZenML0.97.0 provisionally for the trusted local CPU development slice.
Actual runs demonstrate the contract with public APIs and native identities.
This is not an unconditional production/backend endorsement or a claim that the
complete product is finished.

## Demonstrated reasons

- Native typed pipeline stages consume the dataset produced upstream and emit
  real artifact-version UUIDs, including full training-state checkpoints.
- Custom gated PyTorch architecture variants and a pinned small pretrained model
  execute real training, evaluation, export and explicit frozen-reference KL.
- Negative validation results and genuine failed/killed runs remain queryable.
- Selection uses an explicit validation metric; chosen model versions and their
  parent checkpoints remain linked across continuation.
- Exact continuation matches uninterrupted model/optimizer/scheduler/RNG/cursor
  state. Independent environments/stores reproduce metrics and output hashes.
- Detached supervision permits application-service restart and real termination
  reconciliation without copying ZenML's training registry into SQLite.

## Proven adapter refinements

ZenML0.97 static pipelines derive nativefailure after an interrupted step. Its
public store does not support transforming that static terminal failure into
nativecancelled. We preserve `backend_status: failed`, retain the real error,
and attach verified termination metadata using public APIs. The application
operation can separately be `cancelled` only after positive identity/process-group
termination proof. Unknown/unreachable or requested-only states are not stopped.

Cancellation before native creation has no nativeUUID to invent: durable
supervisor wait/identity/empty-group evidence supports the operation outcome.
A later status read still shows null nativeidentity. Tests cover this separately
from a real native-run cancellation and checkpoint recovery.

Python dependency conflicts require separate locked service/backend interpreters.
Clean replay installs a full service wheel and a runtime-only wheel built from
the same verified application source; it does not copy installed site-packages
or introduce an alternate backend implementation.

## Remaining gates and broader requirements

See the acceptance matrix for live status rather than inferring completion from
this decision. Exact Claude/Codex extension tool calls need their own authorized
runtime/account tests. No verified arbitrary-code sandbox is implemented;
untrusted imports remain inert and execution fails closed. Remote execution,
provider spending reservations/invoices, GPU idle cleanup and P5 require explicit
separate authorization.

Production release still needs a current supported editor security baseline,
ongoing dependency/security maintenance, multi-OS builds, signing/update policy,
transitive binary notices and owner-selected first-party licensing. Broader
research monitoring, adversarial external judges, richer model-evolution loops,
registry/marketplace and expanded component catalog require later functional
acceptance, not architecture-only promises.

No original model repository is modified or trained. All fixture results are
small mechanical execution/reproducibility demonstrations, not claims of useful
scientific model quality or reproduction of cited research conclusions.
