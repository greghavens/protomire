# Exact native-reviewed operations

The managed MCP bridge keeps a project-scoped read-only bearer. Catalog reads
use the existing API. Mutating or otherwise privileged calls submit an immutable
request to the local core. Caller-supplied confirmation flags cannot approve that
queue entry. The native `ModelBuilder: Review Agent Requests` command shows the
requester, selected project, operation, exact JSON, content digests and relevant
service-owned material (recipe, proposal, watch configuration, actual license
terms, download plan or complete frozen publication).

The native owner can approve, deny or revoke. Approval dispatches once through
OperationService, using a request-local Actor capability that never becomes a
bearer credential. Its identity is the requesting assistant for operation audit;
the queue separately records the native approver and correlated request ID.
Project/recipe/source context, exact payload and referenced review material are
checked before admission. Expired, changed, withdrawn, denied or already-decided
requests cannot execute. A transport timeout or restart is never an automatic
retry. The native host can read a durable result by request ID.

A request waits up to 15 minutes for approval. Its action capability is bounded
to 30 minutes. Hub transfers retain only their captured exact request capability
until terminal status; the ordinary assistant token never acquires it. Revocation
or expiry causes the existing provider cancellation checks to stop further work.
An upload already in flight may require reconciliation, and already-submitted
native jobs use their existing cancellation operation. Closing the editor is
not an undo operation. Uncertain execution is recorded for inspection.

One `desktop.publish.reviewed` request binds the complete stored preview: exact
repository, visibility, license/rights/usage context, file hashes, source lineage
and expected destination parent. One native confirmation invokes the existing
approve operation, checks that its generated receipt matches the frozen intent,
then invokes start. A standalone `hub.publish.approve` still only approves.
License policy review/revocation similarly receives only an exact native grant.
The separate Hub runtime, local account references and publication roots must be
configured before the real Hub capability is available; these tests do not create
accounts, configure credentials or upload anything.

Manual bounded research refresh uses a request-local capability context. No
scheduler thread or same-ID readonly bearer inherits that context. Recurring
schedule enable is unavailable through this one-shot path until a separately
reviewed standing authorization policy exists.

## Security and acceptance limits

These rules constrain the managed API/MCP route. They do not sandbox an installed
assistant with unrestricted same-user shell/filesystem access. Unix 0600/0700
storage and Windows owner DACLs are not isolation from another same-UID process.
Vendor permissions and trust remain necessary. The developer external-service
mode retains its explicitly configured authorization policy.

Tests cover the real operation service, local HTTP, actual stdio MCP, inert
native admission, host-native dialog transitions, and the real Hub service with
a fake provider. They do not establish native installer acceptance, authenticated
vendor-agent operation, external upload acceptance, or OS sandboxing.
