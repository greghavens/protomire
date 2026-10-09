/** Change layout only: keep the source editor and graph mounted with their drafts intact. */
export function bindRecipeSourcePane(
  grid: HTMLElement,
  pane: HTMLElement,
  toggle: HTMLButtonElement
): void {
  const setExpanded = (expanded: boolean) => {
    // A programmatic collapse must not leave keyboard focus in hidden content.
    if (!expanded && pane.contains(pane.ownerDocument.activeElement)) {
      toggle.focus();
    }
    pane.hidden = !expanded;
    grid.classList.toggle('source-collapsed', !expanded);
    toggle.setAttribute('aria-expanded', String(expanded));
    toggle.textContent = expanded ? 'Hide JSON' : 'Show JSON';
  };
  setExpanded(!pane.hidden);
  toggle.addEventListener('click', () => setExpanded(pane.hidden));
}
