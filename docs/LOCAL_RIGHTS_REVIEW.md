# Review local source, model and dataset rights

The native Hub workflow uses the same project-scoped operations as HTTP and MCP.
A local folder, filename, account, repository visibility or license identifier is
never treated as proof of ownership or permission. ModelBuilder records an
explicit authorized review of actual terms; it does not supply legal advice or
infer a copyright grant.

## Choose and inspect exact files

1. Register the relevant folder with **ModelBuilder: Configure Hub**. This is a
   local file-access decision, separate from rights review. No Hub provider,
   account, download or upload is required for local review.
2. Open the Hub workflow and choose **Review local source, model or dataset
   rights**. Choose model files, dataset files, or a source origin for future
   scratch outputs.
3. Select the registered folder and exact asset files. For a source origin, bind
   and pin the project's source first; the complete pinned source inventory is
   included automatically. The selected folder must be that bound checkout.
4. Select the actual license, custom terms and notice documents separately. They
   are captured even when their filenames are not LICENSE or NOTICE. Captured
   documents must be bounded UTF-8 text. Documents are included in the exact
   byte budget and evidence manifest.
5. Inspect the captured text, paths, hashes and source identity in the native
   document. Missing, empty or uncertain terms stay unresolved. A license label
   is optional knowledge: `unknown` is a valid uninterpreted label, never a grant.

## Record only the reviewed decision

No permitted uses are preselected. Choose the uses covered by the actual terms,
review basis, specific purpose, commercial or noncommercial use, deployment,
conditions, files to retain and permitted destination licenses. If additional
attribution or complex conditions apply, open the advanced policy JSON; no
condition is silently simplified or removed. Leave the workflow unresolved if
the obligations cannot be established.

Review the exact final policy and confirm it. In the managed native application,
the existing exact-request approval controller is still required. Editing the
policy draft, setting `confirm_review`, a webview message or an assistant request
alone does not grant publisher authority. The final approval binds the exact
stored evidence digest. Project navigation invalidates the open workflow.

A review can be retained without copying files. This supports training-only
policies on already-registered datasets: use **Link reviewed policies to a
registered dataset** and map every source ID to its exact reviewed policy. The
service checks dataset revision, each source hash, intended use and usage context.
A verified asset copy additionally requires the explicit `download` permission.
Registering or binding model files never chooses a pretrained initializer.

## Source origins for scratch training

A source origin is `repo_type: source`, not a model asset. The review captures the
bound repository identity, pinned Git commit and complete path/SHA-256 source
inventory. **Bind origin to future scratch outputs** checks the current recipe
and project revisions, all reviewed source and terms bytes, the current policy,
and exact usage context. It creates one recipe revision with the reviewed origin.
It neither runs code nor creates weights.

Future generated artifacts inherit that reviewed origin plus their actual
model, dataset and checkpoint parents. Checkpoints do not need a new review for
each training step. Existing checkpoints with unresolved lineage are not rewritten
or retroactively approved. Each later execution rechecks policy status, context,
commit and source-file closure. Parameter-only changes that leave source bytes
and usage context unchanged can reuse the reviewed source origin.

Direct recipe origins must be reviewed source. Actual model/dataset/checkpoint
parents belong in their verified declared or native inputs, not in an arbitrary
recipe-level rights assertion. Unknown parents and all existing unresolved
lineage remain visible and continue to block use.

A new code commit requires another source review. Choose **Replace selected direct
source origins** when a prior direct binding refers to old code. Select exactly
which old direct policy IDs to replace, inspect their original source closures
beside the new source evidence, and review the current recipe revision/digest and
consequence preview. The separate publisher-scoped native approval binds those
exact old/new identities. A stale recipe, changed terms, unselected old origin or
non-source replacement is rejected without a recipe write.

Only the explicitly selected direct source entries are replaced. Actual model,
dataset and checkpoint inputs and every unresolved obligation stay intact. The
prior rights digest, immutable recipe revisions and owner-review receipt retain
history. This never retroactively rewrites existing generated outputs or waives
inherited input rights. The ordinary bind action cannot silently replace a prior
origin; it instead explains when this explicit replacement review is needed.

## Verification limits

Offline tests exercise real shared-service, exact-byte capture/binding, HTTP/MCP
contracts, native approval queues and simulated native dialogs with synthetic
owner-authored fixtures. These tests do not establish real GUI, legal adequacy,
third-party account/keyring, provider, upload or cross-platform acceptance.
