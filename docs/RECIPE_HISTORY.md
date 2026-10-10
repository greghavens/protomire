# Recipe revision history

Each model project still has one recipe identity. Open **Recipe → Revision history**
to browse its saved, immutable revisions without replacing the active graph or
JSON draft. The existing collapsible JSON pane is unchanged.

- **Inspect saved revision** shows read-only recipe JSON.
- **Compare saved revisions** compares explicit from/to revisions using the shared
  service's recipe diff. This is separate from the toolbar's draft-versus-server
  **Diff** action.
- **Restore as next revision…** reviews a selected historical revision and creates
  the next saved revision of the same recipe. Prior revisions and runs remain
  available. Restore never starts training, selects another recipe, imports a
  bundle, or changes the source checkout.

For example, restoring revision 2 while the editor is based on revision 5 creates
revision 6. The user can then modify a parameter and save revision 7. Revision 2
remains unchanged. This supports reconstructing earlier configurations and
continuing an experiment without introducing parallel recipe identities.

## Drafts, conflicts, and permissions

A native modal confirms restore. Cancel performs no mutation. If the workbench
has unsaved JSON, graph, node-panel, component, or live-evaluation edits, the modal
provides two explicit choices:

1. **Restore and keep drafts** retains the workbench draft and its original base
   revision. The restored revision appears in history; a later save of the old
   draft requires normal optimistic-conflict resolution.
2. **Discard workbench draft and restore** discards that draft only after restore
   succeeds. A canceled or conflicting request leaves it untouched.

Dirty native JSON documents are always preserved at their original base revision;
users save or revert them in the native editor. New edits during review abort the
restore. New workbench edits arriving after submission are retained even when an
earlier discard choice was confirmed.

Restore sends the reviewed `expected_revision`, never silently rereads or rebases
it, and makes one idempotent mutation request. A 409 requires explicit review of
the newer saved revision. Repeated clicks are blocked while the request is pending.
Responses after project navigation cannot replace the current model workspace.
Read-only roles can browse, inspect, and compare; restoring requires modify access
and is also enforced by the service.

## Implementation and verification boundary

The workspace uses the existing `recipe.history`, historical `recipe.get`,
`recipe.diff`, and `recipe.clone` APIs. `recipe.clone` retains its compatibility
meaning: restore to a new revision of the same recipe. The mutating operation is
available only through the typed, revision-checked confirmation flow, not the
webview's generic operation bridge. There is no new persistence layer, recipe
selector, or import/export button; bundle transfer remains in the File menu.

Reproducible checks from `packages/ide_feature`:

- `npm test`: host and lightweight DOM-controller tests, including canceled dialogs,
  dirty drafts, stale revisions, read-only access, navigation, and repeat clicks.
- `npm run test:history-http`: compiled host against an isolated real HTTP service.
  Dialogs are doubles. Set `MODELBUILDER_SERVICE_ROOT`, `MODELBUILDER_GUI_PYTHON`, and
  optionally `MODELBUILDER_HTTP_PYTHONPATH` when using a separate staged checkout.
- `npm run test:history-browser`: actual Chromium DOM with simulated host messages.
  Native Code-OSS GUI acceptance remains a separate gate.

The staged implementation passed TypeScript checks, all 268 frontend tests, and
seven real-HTTP scenario groups on 2026-10-10. The Chromium attempt was blocked
before a page was created: the environment rejected Chromium's Unix socket with
`EPERM`. No browser sandbox bypass was attempted. Browser screenshots and current
native GUI interaction are not claimed as verified by the host/controller tests.
