# New Project, project layout and optional assistants

Implementation baseline: `5b38d9602b6f8ae36fc0dcaf28f0a6da8c8f7d4a`.
This work is staged separately from the running acceptance checkout. The parent
wiring patch must be reviewed and integrated before it is an enabled app feature.

## New Project

A dedicated native New Project page supplements the existing opened workspace.
It asks the owner for a model name/slug, optional goal, starting point, assistant,
and explicitly selected local folder. Research, an optional base model, and code/
training from scratch are equal choices. Skipping quickstart creates a blank
project. All paths invoke the existing atomic `project.create` once: one project,
one model, one stable recipe identity and actual revision 1. No download, training,
vendor model call, Git initialization/commit, remote creation or publication occurs.

The strict `ProjectIntent` v1 schema is optional within the existing project v0
schema. It contains owner-authored `goal`, `starting_point` and `assistant`, with
no permission or run-state fields. `project.update` uses the existing expected
project revision and immutable versions/audit history. HTTP and MCP expose the
same validation and service dispatch. The operation count stays 111 on this base;
the bundled catalog is generated from the live contract when contracts are exported.

The page has explicit review, Back, Cancel and repeated-click guards. Only an
approved create request is persisted to native SecretStorage before dispatch;
ordinary app state indexes its workspace path only. A lost response/tab close/
restart can restore the exact reviewed payload and idempotency key. Recovery shows
Review/Retry and never auto-submits. Closing a submitted operation retains recovery.
The owner can also start a different project while keeping pending requests.
Filesystem failure after service acceptance retries the same project; it never
creates a replacement project or recipe.

## Versioned Git/provider split

Layout v1 creates:

- `modelbuilder.project.json`: schema/layout version, real project/recipe identity,
  current revisions/digest and canonical project metadata
- `src/`, `tests/`: model-owned code and tests, with no generated training model
- `recipe/current.modelrecipe.json` and `recipe/revisions/000001.modelrecipe.json`:
  the actual service recipe, retaining one stable identity
- `research/` and `research/evidence/`: Markdown notes and JSON provenance manifests
- `manifests/models/`, `manifests/datasets/` and a schema for verified HF asset refs
- `.modelbuilder/layout.v1.json`: versioned managed file/hash receipt
- `MODELBUILDER.md`: format/projection guidance and bounded ignore override policy

A managed `.gitignore` block excludes large/local directories (`data`, `models`,
`checkpoints`, `logs`, `runs`, `cache`) and private runtime state. It does not use
blanket data/model extension rules: small fixtures under `tests/fixtures` remain
trackable. Existing tracked blobs are not untracked. Owners can add narrow reviewed
negation rules for deliberate small fixtures. Existing user files are not replaced;
scaffold conflicts fail before project dispatch where detectable, and actual writes
use a per-folder lock, exclusive creation, hashes and rollback of unchanged writes.

Suggested formats are Markdown notes/model cards; schema-validated JSON recipe/
project/artifact metadata; Parquet for tabular data, JSONL where a trainer expects
records; safetensors for compatible weights. Framework-native checkpoint state,
image/audio/video data and other model formats stay native. No conversion happens.

The built-in artifact reference schema is a Hugging Face specialization of the
Hub worker's `modelbuilder.hub-asset/v1` schema: actual provider/repository, immutable
40-hex commit, exact files/byte sizes/SHA-256/Git blob identities, and resource
lineage. License provenance must preserve actual text/source/hash/scope through
the Hub contract extension; a license label alone is not redistribution approval.
It contains no account alias, local path or credential. Other providers
require registered adapter validation and a corresponding explicit schema extension;
there is no pretend generic `other` provider with Git-shaped IDs. When the Hub
contract is present, `scripts/export_contracts.py` generates the scaffold schema
as a specialization of that canonical schema, retaining every provenance/license
field and constraint. The generated-file check prevents silent schema drift. The Hub workflow
owns reviewed transfers/export/publication and is opened with `modelbuilder.openHub`.
Large files are not created by New Project.

## Canonical projection after edits

The initial review also discloses local projection of later canonical service
edits. `attachProjectProjection` decorates the existing operation client. It consults
the actual catalog for mutations and projects after successful edits or project
refresh, including native recipe-source saves. It reads the real `project.open`
and `recipe.history` results, checks matching current revisions, then rereads the
project/recipe to detect concurrent writers before projection. Changed snapshots
are retried up to three times; continuing changes leave the local projection
untouched with a warning. It preserves the recipe identity, updates the current
JSON/project metadata and adds immutable revision exports.

Every owned file hash and required ignore block is checked first. Missing/edited
files return explicit drift; they are never silently overwritten, rebased or
claimed unchanged. Stale service snapshots do not roll back newer local projections.
Projection failure does not turn a successful service edit into failure or retry
it. The native warning says the service result is saved and local projection needs
attention. Other application instances/MCP edits are caught on the next refresh.
Git commits/publication remain separate owner actions. No new top-level export/
import buttons are added by this work.

## Source admission for scratch/custom code

Selecting a folder and writing a scaffold do not trust executable source. Native
`registerProjectSource` shows the exact project/path and local-process consequences.
Only after explicit confirmation does it call `ManagedCore.registerSource`, then
bind the returned identity through existing `project.source.bind` with the reviewed
expected project revision. Cancellation never calls registration. The managed
operator state scopes the alias to that project and freezes checkout identity.

The selected folder must already be a committed Git root. Registration does not
initialize Git or make commits. A fresh research/scratch project remains authorable
without a binding; code execution still requires explicit registration, a full
committed Git SHA and source-file hash closure, pinned dependencies/resources and
run admission. The optional custom runner still uses an operator-registered
`environment_ref` plus profile hash; this scaffold never invents an interpreter path,
runner environment, valid run or training result.

## Assistant setup

Codex, Claude Code and Triggerfish can be selected. Codex/Claude installation uses
the existing pinned official extension preview/apply path. No installer call or
vendor login was performed in these tests. Triggerfish is a capability interface
only until its compatibility fixes are accepted; setup cannot fake a working protocol.

Project-local integration uses:

- Codex: `.codex/config.toml`, `.agents/skills/modelbuilder-project/SKILL.md`,
  an appended managed `AGENTS.md` block and skill references
- Claude: `.mcp.json`, `.claude/skills/modelbuilder-project/SKILL.md`,
  an appended managed `CLAUDE.md` block and skill references

The shared version-1 templates use live catalog discovery, project context, exact
research/document provenance, reviewed implementation and runtime permissions.
Papers, fetched material and current research notes are evidence, never owner
instructions. Runtime code enforces permissions/admission, not prompts.

Managed MCP gets the packaging worker's exact project-bound read-only command;
no service token or environment variable is copied from the IDE. External-service
setup retains explicit credential-environment-reference mode. Authentication,
installation and working MCP transport are reported separately; none is inferred
from a config file or executable discovery.

`assistant_setup.py preview-project/apply-project` and
`preview-rollback/rollback-project` produce exact reviewed plans with expected
hashes. Receipts contain managed public content and hashes, never old config
snapshots or credentials. Unmanaged config/skill conflicts fail closed. Related
files use a shared lock and guarded rollback; rollback removes only owned content
and preserves unrelated edits. Codex global config is never the default. Multiple
open roots require an explicit native choice. AGENTS.override.md, nested instructions,
`.claude/CLAUDE.md` and global approval/security settings remain intact. Override
warnings tell the owner when generated guidance may be shadowed.

Official configuration references inspected on 2026-10-13:
- https://developers.openai.com/codex/config-advanced
- https://developers.openai.com/codex/skills
- https://code.claude.com/docs/en/mcp
- https://code.claude.com/docs/en/memory
- https://code.claude.com/docs/en/skills

## Verification and remaining gates

Run locally from the staged tree:

    PYTHONPATH=src .venv/bin/pytest tests/assistant_setup tests/domain tests/contract -q
    npm --prefix packages/ide_feature run compile
    node --test packages/ide_feature/test/*.test.cjs
    node packages/ide_feature/test/newProjectHttp.cjs
    node packages/ide_feature/test/newProjectDom.cjs
    PYTHONPATH=src .venv/bin/python scripts/export_contracts.py --check

Real HTTP/MCP and native-controller/HTTP tests use actual service persistence and
temporary files, with no vendor/model/provider calls. Host dialogs are mocked, not
GUI acceptance. Sandboxed Chromium launch is currently blocked by process socket
EPERM (also after approved elevated invocation). No `--no-sandbox` or disabled
browser security was used. Real Code-OSS rendering, native multi-window close/reopen,
OS SecretStorage, installed vendor authentication/MCP acceptance, and the final
managed runtime/source-registration integration require acceptance after parent merge.

Full non-integration Python aggregate on the archive staging tree: 2089 passed,
139 skipped, 84 deselected; 2 existing portability source-closure tests fail because
this isolated archive has no Git history at their pinned source revision. This is
not a full aggregate pass. The parent must rerun final tests in a history-bearing
integration checkout. No source-closure baseline or fixture pin was altered to
hide these failures.
