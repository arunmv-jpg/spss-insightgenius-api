/* Dataset library (persistent files): API calls and the shared datasets table.
   Used by Home, Datasets and the dataset detail page. Needs ic.js. */
(function () {
  'use strict';
  const { el, icon, api } = IC;

  const Datasets = {
    async list() { return (await api('/v1/library/files')).files || []; },
    async search(q) { return (await api('/v1/library/search/files?q=' + encodeURIComponent(q))).results || []; },
    get(id) { return api('/v1/library/' + encodeURIComponent(id)); },
    async variables(id) { return (await api('/v1/library/' + encodeURIComponent(id) + '/variables')).variables || []; },
    upload(file) {
      const form = new FormData();
      form.append('file', file);
      return api('/v1/library/upload', { method: 'POST', body: form });
    },
    rename(id, name) {
      const form = new FormData();
      form.append('display_name', name);
      return api('/v1/library/' + encodeURIComponent(id), { method: 'PATCH', body: form });
    },
    remove(id) { return api('/v1/library/' + encodeURIComponent(id), { method: 'DELETE' }); },

    name(f) { return f.display_name || f.filename || 'Untitled'; },
    href(f) { return '/datasets/' + encodeURIComponent(f.id); },
    chatHref(f) { return '/export-mcp?dataset=' + encodeURIComponent(f.id); },

    /* Parse value_labels, which the library stores as a JSON string. */
    valueLabels(v) {
      if (!v || !v.value_labels) return {};
      if (typeof v.value_labels === 'object') return v.value_labels;
      try { return JSON.parse(v.value_labels) || {}; } catch (_) { return {}; }
    },

    /* Rename with a prompt; resolves to the new name or null. */
    async promptRename(f) {
      const current = Datasets.name(f);
      const next = prompt('Rename dataset', current);
      if (!next || !next.trim() || next.trim() === current) return null;
      await Datasets.rename(f.id, next.trim());
      return next.trim();
    },
    async confirmRemove(f) {
      if (!confirm(`Delete “${Datasets.name(f)}” permanently?`)) return false;
      await Datasets.remove(f.id);
      return true;
    },

    /* Row actions menu: Open, Analyze in chat, Rename, Delete. */
    rowMenu(f, { onChange } = {}) {
      const menu = el('div', { class: 'ic-menu', role: 'menu', hidden: true });
      const btn = el('button', { class: 'ic-icon-btn', type: 'button', 'aria-label': 'Actions for ' + Datasets.name(f), 'aria-haspopup': 'menu' }, icon('more', 18));
      const close = () => { menu.hidden = true; };
      const run = (fn) => async (e) => {
        e.stopPropagation(); close();
        try { await fn(); } catch (err) { IC.toast(err.message, 'danger'); }
      };
      menu.append(
        el('a', { class: 'ic-menu-item', href: Datasets.href(f), role: 'menuitem' }, icon('eye', 16), 'Open'),
        el('a', { class: 'ic-menu-item', href: Datasets.chatHref(f), role: 'menuitem' }, icon('message', 16), 'Analyze in chat'),
        el('button', { class: 'ic-menu-item', type: 'button', role: 'menuitem', onclick: run(async () => {
          const n = await Datasets.promptRename(f);
          if (n) { f.display_name = n; IC.toast('Dataset renamed'); onChange && onChange(); }
        }) }, icon('pencil', 16), 'Rename'),
        el('button', { class: 'ic-menu-item', type: 'button', role: 'menuitem', style: { color: 'var(--ic-danger)' }, onclick: run(async () => {
          if (await Datasets.confirmRemove(f)) { IC.toast('Dataset deleted'); onChange && onChange(); }
        }) }, icon('trash', 16), 'Delete'));
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        document.querySelectorAll('.ic-row-menu .ic-menu').forEach(m => { if (m !== menu) m.hidden = true; });
        menu.hidden = !menu.hidden;
      });
      document.addEventListener('click', close);
      return el('div', { class: 'ic-row-menu', style: { position: 'relative', display: 'inline-block' } }, btn, menu);
    },

    /* Table of datasets. Columns: Name, Rows, Columns, Size, Last modified, actions. */
    table(files, { onChange } = {}) {
      const tbody = el('tbody');
      for (const f of files) {
        const name = Datasets.name(f);
        const nameCell = el('a', { class: 'ic-file-cell', href: Datasets.href(f), style: { textDecoration: 'none', color: 'inherit' } },
          el('span', { class: 'ic-file-icon' }, icon(IC.fileIcon(f.filename), 18)),
          el('span', { style: { minWidth: 0 } },
            el('span', { text: name, style: { display: 'block' } }),
            f.filename && f.filename !== name ? el('span', { class: 'ic-help', text: f.filename }) : null));
        const matches = (f.matched_variables || []).slice(0, 3).map(v => v.label || v.name).filter(Boolean);
        tbody.append(el('tr', {},
          el('td', {}, nameCell, matches.length ? el('div', { class: 'ic-help', text: 'Matches: ' + matches.join(', '), style: { marginTop: '2px', marginLeft: '28px' } }) : null),
          el('td', { class: 'num', text: IC.number(f.n_cases) }),
          el('td', { class: 'num', text: IC.number(f.n_variables) }),
          el('td', { class: 'num', text: IC.bytes(f.size_bytes) }),
          el('td', { class: 'ic-muted', text: IC.timeAgo(f.updated_at || f.created_at) }),
          el('td', { style: { width: '48px', textAlign: 'right' } }, Datasets.rowMenu(f, { onChange }))));
      }
      return el('div', { class: 'ic-table-wrap' }, el('table', { class: 'ic-table' },
        el('thead', {}, el('tr', {},
          el('th', { text: 'Name' }), el('th', { class: 'num', text: 'Rows' }), el('th', { class: 'num', text: 'Columns' }),
          el('th', { class: 'num', text: 'Size' }), el('th', { text: 'Last modified' }), el('th', { 'aria-label': 'Actions' }))),
        tbody));
    },

    /* Placeholder rows while loading. */
    skeleton(rows) {
      const wrap = el('div', { style: { padding: '16px 20px', display: 'grid', gap: '14px' } });
      for (let i = 0; i < (rows || 4); i++) wrap.append(el('div', { class: 'ic-skeleton', style: { width: (90 - i * 12) + '%' } }));
      return wrap;
    },
    empty(title, hint, action) {
      return el('div', { class: 'ic-empty' }, icon('database', 32), el('div', { text: title, style: { fontWeight: 600, color: 'var(--ic-text)' } }),
        hint ? el('div', { text: hint, style: { marginTop: '4px' } }) : null, action ? el('div', { style: { marginTop: '14px' } }, action) : null);
    },
    error(err, retry) {
      return el('div', { class: 'ic-empty' }, icon('alert', 28),
        el('div', { text: 'Couldn’t load datasets', style: { fontWeight: 600, color: 'var(--ic-text)' } }),
        el('div', { text: err.message, style: { marginTop: '4px' } }),
        retry ? el('button', { class: 'ic-btn ic-btn--secondary ic-btn--sm', type: 'button', style: { marginTop: '14px' }, onclick: retry }, icon('refresh', 14), 'Try again') : null);
    },
  };

  window.Datasets = Datasets;
})();
