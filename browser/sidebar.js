(function setupSidebar(root) {
  'use strict';
  const shell = document.getElementById('app-shell');
  const sidebar = document.getElementById('sidebar');
  const collapse = document.getElementById('sidebar-collapse');
  const expand = document.getElementById('sidebar-expand');
  const storageKey = 'mathlab.sidebarCollapsed';

  function setCollapsed(collapsed, {persist = true, moveFocus = false} = {}) {
    shell.classList.toggle('sidebar-collapsed', collapsed);
    sidebar.inert = collapsed;
    sidebar.setAttribute('aria-hidden', String(collapsed));
    collapse.setAttribute('aria-expanded', String(!collapsed));
    expand.setAttribute('aria-expanded', String(!collapsed));
    expand.classList.toggle('hidden', !collapsed);
    if (persist) {
      try { root.localStorage.setItem(storageKey, String(collapsed)); } catch (_) { /* Storage may be disabled. */ }
    }
    if (moveFocus) (collapsed ? expand : collapse).focus();
  }

  let saved = false;
  try { saved = root.localStorage.getItem(storageKey) === 'true'; } catch (_) { /* Use the expanded default. */ }
  setCollapsed(saved, {persist: false});
  function toggle(collapsed) {
    // Restore saved layout immediately; animate only explicit user actions.
    shell.classList.add('sidebar-motion');
    setCollapsed(collapsed, {moveFocus: true});
  }
  collapse.addEventListener('click', () => toggle(true));
  expand.addEventListener('click', () => toggle(false));
}(window));
