import { OperationSpec, Recipe, RecordValue } from './types';

/** A read-only revision browser. It never replaces the live editor with an old body. */
export class RecipeHistoryView {
  private recipe?: Recipe;
  private catalog: OperationSpec[] = [];
  private opened = false;
  private requestId = 0;
  private pending = false;
  private history?: RecordValue;
  private inspected?: Recipe;
  private diff?: RecordValue;
  private selection?: number;
  private from?: number;
  private to?: number;
  private notice = '';
  constructor(private root: HTMLElement, private send: (type: string, body: RecordValue) => void) {}

  update(recipe: Recipe | undefined, catalog: OperationSpec[]) {
    const changedIdentity = recipe?.recipe_id !== this.recipe?.recipe_id || recipe?.project_id !== this.recipe?.project_id;
    if (changedIdentity) { this.opened = false; this.requestId++; this.pending = false; }
    if (changedIdentity || recipe?.revision !== this.recipe?.revision) {
      this.history = undefined; this.inspected = undefined; this.diff = undefined;
      this.selection = undefined; this.from = undefined; this.to = undefined; this.notice = '';
    }
    this.recipe = recipe; this.catalog = catalog; this.render();
  }
  private allowed(name: string) { return this.catalog.some(item => item.name === name && item.authorized !== false); }
  private request(action: string, body: RecordValue = {}) {
    if (!this.recipe || this.pending) return;
    this.pending = true;
    const request = {action, projectId: this.recipe.project_id, recipeId: this.recipe.recipe_id,
      expectedRevision: this.recipe.revision, requestId: ++this.requestId, ...body};
    this.render(); this.send('recipeHistory', request);
  }
  receive(message: RecordValue) {
    if (!this.opened || message.requestId !== this.requestId || message.projectId !== this.recipe?.project_id || message.recipeId !== this.recipe?.recipe_id) return;
    if (message.type === 'recipeHistorySettled') { this.pending = false; this.render(); return; }
    if (message.expectedRevision !== this.recipe?.revision) return;
    if (message.action === 'list' || message.action === 'restore') {
      this.history = message.result;
      if (message.action === 'restore') this.notice = `Restored revision ${message.sourceRevision} as saved revision ${message.restoredRevision}.${message.retainedDraft ? ' Your workbench draft keeps its original base revision; save will require conflict review.' : ''}${message.retainedNativeDraft ? ' Unsaved native JSON is preserved at its original base revision.' : ''}`;
      const revisions: Recipe[] = this.history?.revisions ?? [];
      this.selection = revisions.some(item => item.revision === this.selection) ? this.selection : revisions.at(-1)?.revision;
      this.from ??= revisions[0]?.revision;
      this.to = this.history?.current_revision;
      this.inspected = undefined; this.diff = undefined;
    } else if (message.action === 'inspect') this.inspected = message.result;
    else if (message.action === 'diff') this.diff = {...message.result, fromRevision: message.fromRevision, toRevision: message.toRevision};
    this.render();
  }
  private render() {
    const doc = this.root.ownerDocument;
    const element = <K extends keyof HTMLElementTagNameMap>(tag: K, text?: string) => {
      const item = doc.createElement(tag); if (text !== undefined) item.textContent = text; return item;
    };
    const button = (label: string, action: () => void, enabled = true) => {
      const item = element('button', label); item.type = 'button'; item.disabled = this.pending || !enabled;
      item.onclick = () => { if (!item.disabled) action(); }; return item;
    };
    this.root.replaceChildren();
    if (!this.recipe) return;
    const toggle = button(this.opened ? 'Close revision history' : 'Revision history', () => {
      this.opened = !this.opened;
      if (!this.opened) { this.requestId++; this.pending = false; this.render(); }
      else this.request('list');
    }, this.allowed('recipe.history'));
    toggle.setAttribute('aria-expanded', String(this.opened)); toggle.setAttribute('aria-controls', 'recipe-history-browser');
    this.root.append(toggle);
    if (!this.opened) return;
    const panel = element('section'); panel.id = 'recipe-history-browser'; panel.setAttribute('aria-label', 'Recipe revision history');
    panel.append(element('h3', 'Revision history'), element('p', 'Inspect saved revisions without changing your draft. Restore creates the next revision of this same recipe; previous revisions and runs remain available.'));
    if (this.notice) { const status = element('p', this.notice); status.setAttribute('role', 'status'); panel.append(status); }
    panel.append(button('Refresh history', () => this.request('list')));
    if (this.pending) { const status = element('p', 'Loading revision operation…'); status.setAttribute('role', 'status'); panel.append(status); }
    if (this.history) {
      const revisions: Recipe[] = this.history.revisions ?? [];
      panel.append(element('p', `Saved server revision ${this.history.current_revision} · Editor base revision ${this.recipe.revision} · ${revisions.length} immutable revisions`));
      const selector = (label: string, value: number | undefined, change: (value: number) => void) => {
        const wrapper = element('label', label), select = element('select'); select.setAttribute('aria-label', label); select.disabled = this.pending;
        for (const revision of revisions) { const option = element('option', `r${revision.revision} · ${revision.name}`); option.value = String(revision.revision); select.append(option); }
        select.value = String(value ?? ''); select.onchange = () => change(Number(select.value)); wrapper.append(select); return wrapper;
      };
      panel.append(selector('Revision to inspect or restore', this.selection, value => { this.selection = value; this.inspected = undefined; this.render(); }),
        button('Inspect saved revision', () => this.request('inspect', {sourceRevision: this.selection}), revisions.length > 0 && this.allowed('recipe.get')),
        button('Restore as next revision…', () => this.request('restore', {sourceRevision: this.selection}), revisions.length > 0 && this.allowed('recipe.clone')));
      if (!this.allowed('recipe.clone')) panel.append(element('p', 'Restoring revisions requires recipe modify access. History and inspection remain read-only.'));
      panel.append(selector('Compare from revision', this.from, value => { this.from = value; this.diff = undefined; this.render(); }),
        selector('Compare to revision', this.to, value => { this.to = value; this.diff = undefined; this.render(); }),
        button('Compare saved revisions', () => this.request('diff', {fromRevision: this.from, toRevision: this.to}), revisions.length > 0 && this.allowed('recipe.diff')));
      if (this.history.legacy_recipes?.length) panel.append(element('p', `${this.history.legacy_recipes.length} retained legacy recipe records are read-only provenance. This workspace continues using its one recipe.`));
    }
    if (this.inspected) { panel.append(element('h4', `Saved revision ${this.inspected.revision} · read-only`), element('pre', JSON.stringify(this.inspected, null, 2))); }
    if (this.diff) { panel.append(element('h4', `Revision ${this.diff.fromRevision} → ${this.diff.toRevision}`), element('pre', this.diff.equal ? 'These saved revisions are equal.' : typeof this.diff.diff === 'string' ? this.diff.diff : JSON.stringify(this.diff.diff, null, 2))); }
    this.root.append(panel);
  }
}
