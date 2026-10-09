# ADR 0001 Permanent Code OSS workbench

Use pinned Code - OSS with a distribution/branding overlay and built-in feature extension. Zero core patches initially. The module is the permanent product mechanism, not a temporary external plugin. Reuse Python/notebook/debug/LSP tooling. No chatbot or bundled proprietary assistants. Exact binary compatibility and rights are separate acceptance gates.

## 2026-10-09: documented File-menu blocker

The initial zero-core-patch build passed its launch and editor smoke tests.
The owner subsequently required recipe import/export to be ordinary native File
menu commands, without workspace buttons. Code - OSS 1.105's public extension
menu API does not expose the top-level File menu. The specification permits a
minimal core exception for a documented blocker, so the distribution now adds
one composition import of a permanent product workbench menu contribution.
It registers two existing commands only; operation logic remains in the feature
module and shared service. The current patch count is one, not zero.

See [the exact boundary and reproducibility checks](../../distribution/FILE_MENU_EXCEPTION.md).
