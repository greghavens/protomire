# Native checkpoint evaluation verification

Evaluation records remain visible in run status, including historical or incorrect
records. Status adds `checkpoint_verification` with `verified: false` and
`checkpoint_verification_pending`, and does not assert a model-version owner.
Ordinary polling never hashes or materializes checkpoint archives.

Explicit comparison and promotion verify the evaluator's actual native checkpoint
inputs. Source components use `identity(read_checkpoint(path))`, including the
validated asset hashes. The built-in fixture evaluator uses the selected
`best.pt` or `checkpoint.pt` file hash. Neither is the native artifact-store
`content_sha256`; that separate hash continues to pin admitted boundary inputs.
The owner comes from the matching native input, not every teacher/reference
checkpoint attached to the evaluator or a blanket current-run fallback.

A mismatched, ambiguous, foreign-project, changed, missing or unsupported input
returns `evaluation_checkpoint_unverified`, with no winner or promotion. Supported
legacy full-run evidence can still pass when immutable native recipe provenance,
connected training output and actual checkpoint identity establish the owner.
Unknown historical evidence is not silently upgraded: rerun evaluation using a
supported checkpoint adapter. Dictionary/custom checkpoint materializers and
boundary checkpoint relays without their original training ownership are not
supported for promotion. Native input storage verification currently requires
secure POSIX descriptor-relative reads. This does not claim hostile-code isolation
or authenticate arbitrary evaluator predictions or scientific metadata.

## Responsive validation and guarded promotion

Comparison and decision validation run outside the service SQLite transaction.
Promotion snapshots its project revision, accepted decision, candidate provenance
and authority, then performs full native/content verification without that lock.
A local, five-minute evidence handle binds the exact request and result. It is
internal execution evidence, not an external credential or access grant.

Before native stage mutation, the service rechecks current authority, stable
project/decision/run/source/execution state and idempotency. The backend re-fetches
native ownership and securely checks inode, size, mode, mtime, ctime and path
ancestry witnesses captured around initial content verification. It rejects
replacement or modification, including same-size writes with restored mtime.
This final phase does bounded metadata reads, not checkpoint byte hashing or
custom materialization. Existing archive size/member/path and native store bounds
remain in force. These checks assume the trusted local runtime; they cannot defend
against a privileged OS administrator rewriting the runtime or its metadata.

The final native stage write and the service receipt are in different stores.
There is no claim of cross-store atomicity or exactly-once ZenML writes. An
execution attempt journal precedes native mutation. After a lost reply, retry
checks the current native stage and exact selection-operation metadata: an
already-applied identical target is reconciled without repeating writes. An
unproven outcome returns `promotion_reconciliation_required` for inspection,
rather than false success or a blind repeat. Concurrent successful service
requests using the same idempotency key replay the one receipt. A process crash
after the native write but before the SQLite commit may leave a native change
awaiting this reconciliation. No automatic promotion is introduced.
