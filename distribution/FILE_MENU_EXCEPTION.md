# Native File-menu composition exception

## Documented upstream blocker

The product originally built and ran Code OSS 1.105.0 with zero editor-core
patches. The owner subsequently required recipe import/export in the actual
application File menu, rather than workspace buttons, a command-palette
substitute or a webview menu imitation.

At pinned upstream commit `03c265b1adee71ac88f833e065f7bb956b60550a`,
`src/vs/workbench/services/actions/common/menusExtensionPoint.ts` exposes no
extension contribution key for the top-level File menu. `file/newFile` and
`file/share` are different menus. The internal `MenuId.MenubarFileMenu` exists,
but cannot be reached through a supported public extension menu contribution.

The implementation handoff's foundational rule explicitly requires zero core
patches *initially* and permits a patch for a demonstrated blocker. This is that
narrow exception; historical zero-core evidence is not the current patch count.

## Exact change boundary

1. Add the permanent product module
   `src/vs/workbench/contrib/modelbuilder/browser/modelbuilderMenus.contribution.ts`,
   copied byte-for-byte from
   `packages/ide_feature/integration/modelbuilderMenus.contribution.ts`.
2. Append exactly one import line to the otherwise unchanged upstream
   `src/vs/workbench/workbench.common.main.ts`:

   `import './contrib/modelbuilder/browser/modelbuilderMenus.contribution.js';`

Current accounting: **one composition/core patch, one added product workbench
module**, plus the pre-existing distribution `product.json` branding overlay.
There are no other upstream edits. No parser, editor behavior, command service,
authentication, sandbox, native dialog, or Electron security implementation is
patched.

The module only registers File-menu commands. Import/export remain in the
permanent feature, use native file dialogs, invoke the shared operation service,
preserve raw `bundle_json` bytes, and respect current project context and scopes.
The menu adds no privileged bridge or alternate business logic.

## Reproduction and verification

`distribution/apply_overlay.py` checks the exact upstream SHA, rejects arbitrary
tracked and untracked source changes, and permits only the pristine composition
file or its exact baseline-plus-one-import form. During preparation only, an installed older module may be replaced when its
bytes exactly match a regular-file blob at the product module's source path in
the ancestry of the product repository's current HEAD. The index, unmerged
branches, dangling objects, Git replacement refs, and the upstream/cache
repository cannot authorize old bytes. Unknown existing module bytes are
rejected rather than overwritten. Applying the same overlay twice is idempotent. `distribution/verify_build.py` repeats these checks and hashes the
composition file, added module and compiled JavaScript as well as existing
artifacts. Updating this small module requires an explicit reviewed source
change and fresh validation; the verifier never silently accepts a stale or
modified cached module.

The build script supports `MODELBUILDER_BUILD_EVIDENCE` so the File-menu rebuild
and actual-menu GUI evidence are recorded separately from historical proofs.
