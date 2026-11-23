/* DB Browser front-end — vanilla JS, no build step. */
(() => {
  "use strict";

  // ------------------------------------------------------------------ helpers (static/common.js)
  const { $, h, icon, store, fmt, fmtBytes, debounce, api, toast, confirmDialog } = window.UI;
  const isNumericKind = (k) => k === "integer" || k === "number";

  const tablePath = (name) => "/api/tables/" + encodeURIComponent(name);

  // ------------------------------------------------------------------ state
  const state = {
    status: null,
    tables: [],
    table: null,          // current table name
    meta: null,           // current table schema from the API
    rows: [],
    total: 0, page: 1, pages: 1,
    pageSize: store.get("pageSize", 50),
    published: {},        // admin only: data shown to users, {table: {mode, filters}}
    usersHidden: {},      // admin only: {table: [columns hidden from regular users]}
    colFilters: {},       // column name -> text typed in that column's filter box
    sort: null, dir: "asc",
    selected: new Set(),
    hidden: new Set(),
  };
  let loadSeq = 0;
  let loadAbort = null;

  const FILTER_HELP = [
    "Type to filter (contains).",
    "a, b       any of several values",
    "=a         exact value     !a   exclude     !=a  not equal",
    ">  >=  <  <=   compare",
    "a..b  a..  ..b   range (inclusive)",
    "=   empty      !=  not empty",
    'Quote values containing commas: ="Main St, 2"',
  ].join("\n");

  /** Split "a, b, \"c, d\"" on commas that are not inside double quotes. */
  function splitTerms(text) {
    const out = [];
    let cur = "", quoted = false;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (ch === '"') {
        if (quoted && text[i + 1] === '"') { cur += '""'; i++; continue; }
        quoted = !quoted;
      } else if (ch === "," && !quoted) { out.push(cur); cur = ""; continue; }
      cur += ch;
    }
    out.push(cur);
    return out.map((t) => t.trim()).filter(Boolean);
  }
  const isQuoted = (v) => v.length >= 2 && v[0] === '"' && v[v.length - 1] === '"';
  const unquote = (v) => { v = v.trim(); return isQuoted(v) ? v.slice(1, -1).replace(/""/g, '"') : v; };
  const quoteValue = (v) => /[,"]|\.\.|^\s|\s$/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;

  /** Exclusive upper bound so "..1975" or "..1975-06" include the whole year/month/day. */
  function dateUpperBound(v) {
    let m;
    if ((m = /^(\d{4})$/.exec(v))) return String(+m[1] + 1);
    if ((m = /^(\d{4})-(\d{2})$/.exec(v))) {
      const y = +m[1], mo = +m[2];
      return mo === 12 ? `${y + 1}-01` : `${y}-${String(mo + 1).padStart(2, "0")}`;
    }
    if ((m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v))) {
      const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3] + 1));
      return d.toISOString().slice(0, 10);
    }
    return null;
  }

  const PREFIXES = [[">=", "gte"], ["<=", "lte"], ["!=", "not_equals"], [">", "gt"], ["<", "lt"],
                    ["=", "equals"], ["!", "not_contains"]];
  /** One comma-separated term -> API filter (or null). */
  function parseTerm(col, term) {
    const column = col.name;
    if (term === "=") return { column, op: "is_empty" };
    if (term === "!=" || term === "!") return { column, op: "not_empty" };
    for (const [p, op] of PREFIXES) {
      if (term.startsWith(p)) {
        const value = unquote(term.slice(p.length));
        return value === "" ? null : { column, op, value };
      }
    }
    if (!isQuoted(term) && term.includes("..")) {
      const i = term.indexOf("..");
      const lo = unquote(term.slice(0, i)), hi = unquote(term.slice(i + 2));
      const parts = [];
      if (lo) parts.push({ column, op: "gte", value: lo });
      if (hi) {
        const next = col.kind === "date" ? dateUpperBound(hi) : null;
        parts.push(next ? { column, op: "lt", value: next } : { column, op: "lte", value: hi });
      }
      if (!parts.length) return null;
      return parts.length === 1 ? parts[0] : { all: parts };
    }
    return { column, op: "contains", value: unquote(term) };
  }

  // The server parses the filter-box text (filters.py); here it is only used for
  // highlighting matches and pre-selecting values in the picker.
  const hasFilters = () => Object.keys(state.colFilters).length > 0;
  const isAdmin = () => !!(state.status && state.status.user && state.status.user.role === "admin");

  const columnByName = (name) => state.meta && state.meta.columns.find((c) => c.name === name);
  const visibleColumns = () => state.meta ? state.meta.columns.filter((c) => !state.hidden.has(c.name)) : [];
  // canEdit: full control (admin) = add, delete and edit every column.
  // canEditRows: may change values at all; regular users can update only the columns an admin allowed.
  const canEdit = () => state.meta && state.meta.editable && !(state.status && state.status.readOnly);
  const userEditColumns = () => (state.meta && !isAdmin() && state.meta.editableColumns) || [];
  const canEditRows = () => !!state.meta && (canEdit() || (userEditColumns().length > 0 && !(state.status && state.status.readOnly)));
  const columnEditable = (name) => canEdit() || userEditColumns().includes(name);

  // ------------------------------------------------------------------ status / tables
  function showError(message) {
    $("content").hidden = true;
    $("sidebar").hidden = true;
    $("errorScreen").hidden = false;
    $("errorMessage").textContent = message;
    $("errorHelp").hidden = !isAdmin();
    $("retryBtn").replaceChildren(icon("refresh"), isAdmin() ? "Reload .env & retry" : "Try again");
  }
  window.UI.onUnavailable = showError;

  function renderStatus(s) {
    $("dbPill").hidden = !s.file;
    $("dbFile").textContent = s.file || "";
    $("dbFile").title = s.path || "";
    $("dbEncrypted").hidden = !s.encrypted;
    $("dbReadonly").hidden = !s.readOnly;
    $("dbSize").textContent = s.sizeBytes != null ? fmtBytes(s.sizeBytes) : "";
    $("reloadBtn").hidden = !isAdmin();
    $("publishBtn").hidden = !isAdmin();
    if (s.user && !state.menuRendered) {
      window.UI.renderUserMenu($("userMenu"), s.user, "data");
      state.menuRendered = true;
    }
  }

  async function loadStatus(reload = false) {
    const useReload = reload && isAdmin();
    const s = await api(useReload ? "/api/reload" : "/api/status", useReload ? { method: "POST", body: {} } : {});
    state.status = s;
    renderStatus(s);
    if (!s.ok) { showError(s.error); return; }
    $("errorScreen").hidden = true;
    $("content").hidden = false;
    $("sidebar").hidden = false;
    await loadTables();
  }

  async function loadTables(keepCurrent = false) {
    const { tables } = await api("/api/tables");
    state.tables = tables;
    await loadPublished();
    renderTableList();
    if (keepCurrent && state.table && tables.some((t) => t.name === state.table)) return;
    if (!tables.length) {
      state.table = null; state.meta = null;
      $("tableTitle").textContent = isAdmin() ? "No tables" : "No data shared yet";
      $("tableSubtitle").textContent = isAdmin() ? "This database has no tables yet."
        : "An administrator hasn’t given you access to any data yet.";
      $("gridHead").replaceChildren(); $("gridBody").replaceChildren();
      $("addBtn").hidden = true;
      return;
    }
    const remembered = store.get("table:" + (state.status && state.status.path), null);
    const pick = tables.find((t) => t.name === remembered) || tables.find((t) => t.kind === "table") || tables[0];
    selectTable(pick.name);
  }

  function renderTableList() {
    $("tableList").replaceChildren(...state.tables.map((t) =>
      h("button", {
        class: "table-item" + (t.name === state.table ? " active" : ""),
        title: t.kind === "view" ? `${t.name} (view)` : t.name,
        onclick: () => { if (t.name !== state.table) selectTable(t.name); },
      }, icon(t.kind === "view" ? "eye" : "table"), h("span", { class: "name" }, t.name),
      h("span", { class: "num" }, t.rows == null ? "" : fmt(t.rows)))));
  }

  function selectTable(name) {
    state.table = name;
    state.meta = null;
    state.colFilters = {};
    state.sort = null; state.dir = "asc"; state.page = 1;
    state.selected.clear();
    state.hidden = new Set(store.get(hiddenKey(), []));
    store.set("table:" + (state.status && state.status.path), name);
    renderTableList();
    renderFilterState();
    loadRows();
  }
  const hiddenKey = () => `hidden:${state.status && state.status.path}:${state.table}`;

  // ------------------------------------------------------------------ rows
  async function loadRows({ keepSelection = false } = {}) {
    if (!state.table) return;
    const seq = ++loadSeq;
    if (loadAbort) loadAbort.abort();
    loadAbort = new AbortController();
    if (!keepSelection) state.selected.clear();
    const params = new URLSearchParams({ page: state.page, pageSize: state.pageSize, dir: state.dir });
    if (state.sort) params.set("sort", state.sort);
    if (hasFilters()) params.set("columnFilters", JSON.stringify(state.colFilters));
    const timer = setTimeout(() => { $("progress").hidden = false; $("tableScroll").classList.add("loading"); }, 120);
    try {
      const data = await api(`${tablePath(state.table)}/rows?${params}`, { signal: loadAbort.signal });
      if (seq !== loadSeq) return;
      const metaChanged = !state.meta || JSON.stringify(state.meta) !== JSON.stringify(data.table);
      state.meta = data.table;
      state.rows = data.rows; state.total = data.total; state.page = data.page; state.pages = data.pages;
      for (const k of [...state.selected]) if (!state.rows.some((r) => r.key === k)) state.selected.delete(k);
      if (metaChanged) renderMeta();
      renderHead(); renderBody(); renderFooter(); renderSelection();
    } catch (e) {
      if (e.name === "AbortError" || seq !== loadSeq) return;
      if (e.code !== "database_unavailable") toast(e.message, "error");
    } finally {
      if (seq === loadSeq) {
        clearTimeout(timer);
        $("progress").hidden = true; $("tableScroll").classList.remove("loading");
      }
    }
  }

  function renderMeta() {
    const m = state.meta;
    $("tableTitle").textContent = m.name;
    $("addBtn").hidden = !canEdit();
    renderColumnsList();
  }

  /** Short text for a data-shown-to-users rule, e.g. "only state =TX, =CO". */
  function summarizeRule(rule) {
    if (!rule || rule.mode === "all") return "all rows" + sampleNote(rule);
    if (rule.mode === "none") return "nothing (hidden)";
    const f = Object.entries(rule.filters || {}).map(([c, v]) => `${c} ${v}`).join(" · ");
    return "only " + f + sampleNote(rule);
  }
  const sampleNote = (rule) => (rule && rule.sample ? ` (a random ${fmt(rule.sample)} per user)` : "");

  // ------------------------------------------------------------------ show data to users (admin)
  async function loadPublished() {
    if (!isAdmin()) return;
    try { state.published = (await api("/api/admin/access-defaults")).rules; } catch { state.published = {}; }
    try {
      const vis = await api("/api/admin/visible-columns");
      state.usersHidden = Object.fromEntries(vis.tables.map((t) =>
        [t.name, t.columns.map((c) => c.name).filter((n) => !t.visible.includes(n))]));
    } catch { state.usersHidden = {}; }
    if (state.meta) renderSubtitle();
  }

  function openPublish() {
    const table = state.table;
    if (!table || !state.meta) return;
    const filters = { ...state.colFilters };
    const filtered = Object.keys(filters).length > 0;
    const current = state.published[table];
    const count = state.total;
    const sampleInput = h("input", { type: "number", min: "1", step: "1", inputmode: "numeric", class: "sample-input",
      placeholder: "All", "aria-label": "Number of random rows per user", value: current && current.sample ? String(current.sample) : "" });
    const reshuffle = h("input", { type: "checkbox" });
    const readSample = () => {
      const raw = sampleInput.value.trim();
      if (raw === "") return null;
      const n = Number(raw);
      if (!Number.isInteger(n) || n < 1) throw new Error("Enter a whole number of rows, or leave it empty to show all.");
      return n;
    };
    const save = async (mode) => {
      try {
        const sample = mode === "none" ? null : readSample();
        const { rules } = await api("/api/admin/access-defaults");
        rules[table] = { mode, filters: mode === "filtered" ? filters : {}, sample, reshuffle: reshuffle.checked };
        const res = await api("/api/admin/access-defaults", { method: "PUT", body: { rules } });
        state.published = res.rules;
        renderSubtitle();
        const per = sample ? `a random ${fmt(sample)}${mode === "filtered" ? " of " + fmt(count) : ""}` : null;
        toast(mode === "none" ? `${table} is now hidden from users`
          : mode === "all" ? `Users now see ${per ? per + " rows" : "all rows"} of ${table}`
          : `Users now see ${per ? per + " matching rows" : fmt(count) + " row" + (count === 1 ? "" : "s")} of ${table}`);
        close(true);
      } catch (e) { toast(e.message, "error"); }
    };
    const filterText = Object.entries(filters).map(([c, v]) => `${c}: ${v}`).join("  ·  ");
    const choice = (title, text, btn) => h("div", { class: "publish-choice" },
      h("div", {}, h("b", {}, title), h("p", {}, text)), btn);
    const { box, close } = window.UI.modal([
      h("div", { class: "modal-icon info" }, icon("shield")),
      h("h3", { id: "pubTitle" }, `Show “${table}” to users`),
      h("p", { class: "publish-current" }, "Right now users see ", h("b", {}, summarizeRule(current)), "."),
      h("label", { class: "publish-sample" },
        h("span", {}, h("b", {}, "Random selection"), h("small", {}, "Optional. Each user gets their own random rows, kept the same between visits.")),
        h("span", { class: "sample-field" }, sampleInput, h("span", {}, "rows"))),
      current && current.sample
        ? h("label", { class: "publish-reshuffle" }, reshuffle, "Pick new random rows for everyone") : null,
      filtered
        ? choice(`The ${fmt(count)} row${count === 1 ? "" : "s"} matching your filters`, filterText,
          h("button", { class: "btn btn-primary", disabled: count === 0, onclick: () => save("filtered") }, "Show these rows"))
        : choice("All rows", "No filters are active. To show only some rows, close this, set the column filters, then open this again.",
          h("button", { class: "btn btn-primary", onclick: () => save("all") }, "Show all rows")),
      filtered ? choice("All rows", "Remove the restriction.", h("button", { class: "btn", onclick: () => save("all") }, "Show all")) : null,
      choice("Nothing", "Hide this table from users.", h("button", { class: "btn btn-danger-soft", onclick: () => save("none") }, "Hide table")),
      h("p", { class: "publish-note" }, "Users can still filter, sort and search within the rows you show. Users with their own access rule (Users page) keep that rule. A random selection applies to the matching rows only."),
      h("div", { class: "modal-actions" }, h("button", { class: "btn", onclick: () => close(false) }, "Close")),
    ], { labelledBy: "pubTitle" });
    box.classList.add("modal-wide");
  }

  function renderSubtitle() {
    const m = state.meta;
    const t = state.tables.find((x) => x.name === state.table);
    const parts = [];
    if (t && t.rows != null) parts.push(`${fmt(t.rows)} rows`);
    parts.push(`${m.columns.length} columns`);
    if (m.kind === "view") parts.push("view");
    if (!canEdit()) parts.push(userEditColumns().length ? "you can edit: " + userEditColumns().join(", ") : "read-only");
    if (m.restricted) parts.push("limited by your data access");
    if (m.sample) parts.push(`a random selection of up to ${fmt(m.sample)} rows`);
    if (isAdmin()) parts.push("users see: " + summarizeRule(state.published[state.table]));
    const hiddenForUsers = isAdmin() ? (state.usersHidden[state.table] || []) : [];
    if (hiddenForUsers.length) parts.push(`${hiddenForUsers.length} column${hiddenForUsers.length === 1 ? "" : "s"} hidden from users`);
    $("tableSubtitle").textContent = parts.join(" · ");
  }

  function renderHead() {
    const tr = h("tr");
    const edit = canEdit();
    if (edit) {
      const all = h("input", { type: "checkbox", "aria-label": "Select all rows on this page",
        onchange: (e) => {
          for (const r of state.rows) e.target.checked ? state.selected.add(r.key) : state.selected.delete(r.key);
          renderBody(); renderSelection();
        } });
      all.id = "selectAll";
      tr.append(h("th", { class: "sel" }, all));
    }
    for (const col of visibleColumns()) {
      const sorted = state.sort === col.name;
      const ic = sorted ? (state.dir === "asc" ? "up" : "down") : "sort";
      const th = h("th", {
        class: [sorted && "sorted", isNumericKind(col.kind) && col.name !== state.meta.key && "num"].filter(Boolean).join(" "),
        "aria-sort": sorted ? (state.dir === "asc" ? "ascending" : "descending") : "none",
      }, h("button", { class: "th-btn", title: `Sort by ${col.name}`, onclick: () => toggleSort(col.name) },
        h("span", {}, col.name), col.type ? h("span", { class: "th-type" }, col.type) : null,
        isAdmin() && (state.usersHidden[state.table] || []).includes(col.name)
          ? h("span", { class: "th-hidden", title: "Hidden from users (Users → Visible columns)" }, icon("eye-off")) : null,
        icon(ic, "sort-ic")));
      tr.append(th);
    }
    if (canEditRows()) tr.append(h("th", { class: "act" }));

    // Per-column filter boxes, aligned under each header.
    const fr = h("tr", { class: "filter-row" });
    if (edit) fr.append(h("th", { class: "sel" }));
    for (const col of visibleColumns()) fr.append(h("th", {}, filterControl(col)));
    if (canEditRows()) fr.append(h("th", { class: "act" }));

    // Keep focus/caret if the user is typing in a filter box while the head re-renders.
    const active = document.activeElement;
    const focusCol = active && active.dataset && active.dataset.filterCol;
    const caret = focusCol && active.selectionStart != null ? [active.selectionStart, active.selectionEnd] : null;
    $("gridHead").replaceChildren(tr, fr);
    if (focusCol) {
      const el = $("gridHead").querySelector(`[data-filter-col="${CSS.escape(focusCol)}"]`);
      if (el) { el.focus(); if (caret && el.setSelectionRange) el.setSelectionRange(...caret); }
    }
    renderSubtitle();
  }

  const applyFilters = debounce(() => { state.page = 1; loadRows(); }, 300);

  function filterControl(col) {
    const value = state.colFilters[col.name] ?? "";
    const label = `Filter ${col.name}`;
    if (col.kind === "blob") return h("span");
    if (col.kind === "boolean") {
      const sel = h("select", { class: "col-filter" + (value ? " active" : ""), "aria-label": label,
        dataset: { filterCol: col.name },
        onchange: (e) => {
          setColumnFilter(col.name, e.target.value);
          e.target.classList.toggle("active", !!e.target.value);
          state.page = 1; loadRows();
        } },
        h("option", { value: "" }, "All"), h("option", { value: "1" }, "Yes"),
        h("option", { value: "0" }, "No"), h("option", { value: "null" }, "NULL"));
      sel.value = value;
      return sel;
    }
    const input = h("input", {
      class: "col-filter" + (value ? " active" : ""), type: "text", placeholder: col.kind === "date" ? "From..to" : "Filter…",
      size: 1, title: FILTER_HELP, "aria-label": label, autocomplete: "off", spellcheck: "false",
      dataset: { filterCol: col.name },
      oninput: (e) => {
        setColumnFilter(col.name, e.target.value);
        e.target.classList.toggle("active", !!e.target.value);
        applyFilters();
      },
      onkeydown: (e) => {
        if (e.key === "Escape" && e.target.value) {
          e.stopPropagation(); e.target.value = ""; setColumnFilter(col.name, "");
          e.target.classList.remove("active"); applyFilters();
        } else if (e.key === "ArrowDown" && e.altKey) {
          e.preventDefault(); openPicker(col, btn);
        }
      },
    });
    input.value = value;
    const isDate = col.kind === "date";
    const btn = h("button", {
      type: "button", class: "filter-btn", tabindex: "-1",
      title: isDate ? "Pick a date range" : "Pick values (Alt+↓)",
      "aria-label": isDate ? `Pick a date range for ${col.name}` : `Pick values for ${col.name}`,
      dataset: { pickerCol: col.name },
      onclick: (e) => { e.stopPropagation(); openPicker(col, btn); },
    }, icon(isDate ? "calendar" : "down"));
    return h("div", { class: "filter-cell" }, input, btn);
  }

  /** Write a new filter value for a column, update its box, and reload rows. */
  function commitColumnFilter(name, text) {
    setColumnFilter(name, text);
    const el = $("gridHead").querySelector(`[data-filter-col="${CSS.escape(name)}"]`);
    if (el) { el.value = text; el.classList.toggle("active", !!text); }
    state.page = 1;
    loadRows();
  }

  // ------------------------------------------------------------------ value / date pickers
  let picker = null; // { el, col, anchor }

  function closePicker() {
    if (!picker) return;
    picker.el.remove();
    picker = null;
  }

  function placePicker(el, anchor) {
    const r = anchor.closest(".filter-cell").getBoundingClientRect();
    if (!el.isConnected) document.body.append(el);
    const w = el.offsetWidth, hgt = el.offsetHeight;
    const left = Math.max(8, Math.min(r.left, innerWidth - w - 8));
    let top = r.bottom + 6;
    if (top + hgt > innerHeight - 8) top = Math.max(8, r.top - hgt - 6);
    el.style.left = left + "px";
    el.style.top = top + "px";
  }

  function openPicker(col, anchor) {
    const same = picker && picker.col === col.name;
    closePopovers();
    if (same) return; // second click toggles closed
    (col.kind === "date" ? openDatePicker : openValuePicker)(col, anchor);
  }

  /** Values currently selected via "=value" terms (null = empty). Returns null if the box has other syntax. */
  function selectedFromText(text) {
    const set = new Set();
    for (const term of splitTerms(text || "")) {
      if (term === "=") set.add(null);
      else if (term.startsWith("=") && !term.startsWith("==")) set.add(unquote(term.slice(1)));
      else return null;
    }
    return set;
  }

  function openValuePicker(col, anchor) {
    const current = state.colFilters[col.name] || "";
    const parsed = selectedFromText(current);
    const selected = parsed || new Set();
    const keyOf = (v) => (v === null ? null : String(v));
    const list = h("div", { class: "picker-list" }, h("div", { class: "picker-note" }, "Loading…"));
    const note = h("div", { class: "picker-note" });
    const countEl = h("span", { class: "picker-count" });
    let shown = [];
    const updateCount = () => { countEl.textContent = selected.size ? `${fmt(selected.size)} selected` : "None selected"; };
    const search = h("input", { type: "search", class: "picker-search", placeholder: "Search values…",
      autocomplete: "off", spellcheck: "false", oninput: debounce(() => load(), 200) });

    function renderList() {
      list.replaceChildren(...(shown.length ? shown.map(({ value, count }) => {
        const k = keyOf(value);
        return h("label", { class: "picker-item" },
          h("input", { type: "checkbox", checked: selected.has(k), onchange: (e) => {
            e.target.checked ? selected.add(k) : selected.delete(k); updateCount();
          } }),
          k === null ? h("span", { class: "picker-val null" }, "(empty)") : h("span", { class: "picker-val", title: k }, k),
          h("span", { class: "picker-num" }, fmt(count)));
      }) : [h("div", { class: "picker-note" }, "No values")]));
    }

    async function load() {
      const params = new URLSearchParams({ column: col.name });
      if (search.value.trim()) params.set("search", search.value.trim());
      const others = { ...state.colFilters };
      delete others[col.name];
      if (Object.keys(others).length) params.set("columnFilters", JSON.stringify(others));
      try {
        const data = await api(`${tablePath(state.table)}/values?${params}`);
        if (!picker || picker.el !== el) return;
        shown = data.values;
        note.textContent = data.more ? "Showing the first 300 values. Type to narrow the list." : "";
        renderList();
      } catch (e) {
        list.replaceChildren(h("div", { class: "picker-note" }, e.message));
      }
    }

    const el = h("div", { class: "picker", role: "dialog", "aria-label": `Values of ${col.name}` },
      h("div", { class: "picker-head" }, h("b", {}, col.name), countEl),
      search,
      h("div", { class: "picker-tools" },
        h("button", { type: "button", class: "link-btn", onclick: () => {
          shown.forEach((v) => selected.add(keyOf(v.value))); updateCount(); renderList(); } }, "Select shown"),
        h("button", { type: "button", class: "link-btn", onclick: () => {
          selected.clear(); updateCount(); renderList(); } }, "Clear")),
      list, note,
      parsed === null ? h("div", { class: "picker-note warn" }, "Applying replaces the text in this filter box.") : null,
      h("div", { class: "picker-foot" },
        h("button", { type: "button", class: "btn btn-sm", onclick: closePicker }, "Cancel"),
        h("button", { type: "button", class: "btn btn-sm btn-primary", onclick: () => {
          const text = [...selected].map((v) => (v === null ? "=" : "=" + quoteValue(v))).join(", ");
          closePicker();
          commitColumnFilter(col.name, text);
        } }, "Apply")));
    picker = { el, col: col.name, anchor };
    updateCount();
    placePicker(el, anchor);
    search.focus();
    load();
  }

  function openDatePicker(col, anchor) {
    const current = (state.colFilters[col.name] || "").trim();
    let from = "", to = "";
    const terms = splitTerms(current);
    if (terms.length === 1 && terms[0].includes("..")) {
      const i = terms[0].indexOf("..");
      from = terms[0].slice(0, i).trim(); to = terms[0].slice(i + 2).trim();
    } else if (terms.length === 1 && terms[0].startsWith(">=")) from = terms[0].slice(2).trim();
    else if (terms.length === 1 && terms[0].startsWith("<=")) to = terms[0].slice(2).trim();
    const isoDay = (v) => (/^\d{4}-\d{2}-\d{2}$/.test(v) ? v : "");
    const fromIn = h("input", { type: "date", value: isoDay(from) });
    const toIn = h("input", { type: "date", value: isoDay(to) });
    const apply = () => {
      const a = fromIn.value, b = toIn.value;
      closePicker();
      if (a && b && a > b) { commitColumnFilter(col.name, `${b}..${a}`); return; }
      commitColumnFilter(col.name, a || b ? `${a}..${b}` : "");
    };
    const year = new Date().getFullYear();
    const preset = (label, a, b) => h("button", { type: "button", class: "chip-btn",
      onclick: () => { fromIn.value = a; toIn.value = b; } }, label);
    const el = h("div", { class: "picker picker-date", role: "dialog", "aria-label": `Date range for ${col.name}` },
      h("div", { class: "picker-head" }, h("b", {}, col.name), h("span", { class: "picker-count" }, "Date range")),
      h("div", { class: "date-grid" },
        h("label", {}, "From", fromIn), h("label", {}, "To", toIn)),
      h("div", { class: "date-presets" },
        preset("Before 1960", "", "1959-12-31"),
        preset("1960s", "1960-01-01", "1969-12-31"), preset("1970s", "1970-01-01", "1979-12-31"),
        preset("1980s", "1980-01-01", "1989-12-31"), preset("1990s", "1990-01-01", "1999-12-31"),
        preset("2000+", "2000-01-01", ""), preset("This year", `${year}-01-01`, `${year}-12-31`)),
      h("div", { class: "picker-note" }, "Both ends are inclusive. Leave one empty for an open range."),
      h("div", { class: "picker-foot" },
        h("button", { type: "button", class: "btn btn-sm", onclick: () => {
          closePicker(); commitColumnFilter(col.name, ""); } }, "Clear"),
        h("span", { class: "spacer" }),
        h("button", { type: "button", class: "btn btn-sm btn-primary", onclick: apply }, "Apply")));
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); apply(); } });
    picker = { el, col: col.name, anchor };
    placePicker(el, anchor);
    fromIn.focus();
  }

  function setColumnFilter(name, value) {
    if (value) state.colFilters[name] = value; else delete state.colFilters[name];
    renderFilterState();
  }

  function renderFilterState() {
    const n = Object.keys(state.colFilters).length;
    $("clearFiltersBtn").hidden = n === 0;
    $("filterCount").textContent = n;
  }

  function toggleSort(name) {
    if (state.sort !== name) { state.sort = name; state.dir = "asc"; }
    else if (state.dir === "asc") state.dir = "desc";
    else { state.sort = null; state.dir = "asc"; }
    state.page = 1;
    loadRows();
  }

  function searchTerms(colName) {
    const col = columnByName(colName);
    if (!col || col.kind === "boolean") return [];
    return splitTerms(state.colFilters[colName] || "").map((t) => parseTerm(col, t))
      .filter((f) => f && f.op === "contains" && f.value).map((f) => f.value);
  }

  function highlight(text, terms) {
    if (!terms.length) return document.createTextNode(text);
    const escaped = terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    const re = new RegExp(`(${escaped.join("|")})`, "gi");
    const frag = document.createDocumentFragment();
    let last = 0;
    text.replace(re, (m, _g, idx) => {
      if (idx > last) frag.append(text.slice(last, idx));
      frag.append(h("mark", {}, m));
      last = idx + m.length;
      return m;
    });
    if (last < text.length) frag.append(text.slice(last));
    return frag;
  }

  function renderCell(col, value) {
    const td = h("td");
    if (value === null || value === undefined) {
      td.append(h("span", { class: "null" }, "NULL"));
      return td;
    }
    if (typeof value === "object" && "$blob" in value) {
      td.append(h("span", { class: "null" }, `BLOB · ${fmtBytes(value.$blob)}`));
      return td;
    }
    if (col.kind === "boolean" && (value === 0 || value === 1)) {
      td.append(h("span", { class: "bool " + (value ? "bool-yes" : "bool-no") }, value ? "Yes" : "No"));
      return td;
    }
    const text = String(value);
    if (text === "") {
      td.append(h("span", { class: "empty-str" }, "empty"));
      return td;
    }
    if (col.name === state.meta.key) td.classList.add("mono", "key");
    else if (typeof value === "number") td.classList.add("num");
    td.title = text.length > 30 ? text : "";
    td.append(highlight(text, searchTerms(col.name)));
    return td;
  }

  function renderBody() {
    const edit = canEdit();
    const cols = visibleColumns();
    const body = $("gridBody");
    body.replaceChildren(...state.rows.map((row) => {
      const selected = state.selected.has(row.key);
      const tr = h("tr", { class: selected ? "selected" : "", onclick: (e) => {
        if (e.target.closest("button, input, a")) return;
        if (window.getSelection().toString()) return; // allow text selection
        openEditor(row);
      } });
      if (edit) {
        tr.append(h("td", { class: "sel" }, h("input", {
          type: "checkbox", checked: selected, "aria-label": "Select row",
          onchange: (e) => {
            e.target.checked ? state.selected.add(row.key) : state.selected.delete(row.key);
            tr.classList.toggle("selected", e.target.checked);
            renderSelection();
          },
        })));
      }
      for (const col of cols) tr.append(renderCell(col, row.values[col.name]));
      if (canEditRows()) {
        tr.append(h("td", { class: "act" }, h("div", { class: "row-actions" },
          h("button", { class: "icon-btn", title: "Edit", "aria-label": "Edit row", onclick: () => openEditor(row) }, icon("edit")),
          edit ? h("button", { class: "icon-btn danger", title: "Delete", "aria-label": "Delete row", onclick: () => deleteRows([row.key]) }, icon("trash")) : null)));
      }
      return tr;
    }));
    const empty = state.rows.length === 0;
    $("emptyState").hidden = !empty;
    if (empty) {
      const filtered = hasFilters();
      $("emptyState").querySelector(".empty-title").textContent = filtered ? "No matching rows" : "This table is empty";
      $("emptyState").querySelector(".empty-text").textContent = filtered
        ? "Try different filter values." : (canEdit() ? "Add the first row to get started." : "");
      $("clearAllEmpty").hidden = !filtered;
    }
  }

  function renderSelection() {
    const n = state.selected.size;
    $("bulkDeleteBtn").hidden = n === 0;
    $("selectedCount").textContent = n ? `(${n})` : "";
    const all = $("selectAll");
    if (all) {
      const onPage = state.rows.filter((r) => state.selected.has(r.key)).length;
      all.checked = onPage > 0 && onPage === state.rows.length;
      all.indeterminate = onPage > 0 && onPage < state.rows.length;
    }
  }

  function renderFooter() {
    const start = state.total ? (state.page - 1) * state.pageSize + 1 : 0;
    const end = Math.min(state.total, state.page * state.pageSize);
    $("rangeText").replaceChildren(state.total
      ? h("span", {}, "Showing ", h("b", {}, `${fmt(start)}–${fmt(end)}`), " of ", h("b", {}, fmt(state.total)))
      : h("span", {}, "No rows"));
    const pages = $("pages");
    const btn = (label, page, opts = {}) => h("button", {
      class: "page-btn" + (opts.current ? " current" : ""), disabled: opts.disabled || false,
      "aria-label": opts.label || `Page ${page}`, "aria-current": opts.current ? "page" : null,
      onclick: () => { state.page = page; loadRows(); $("tableScroll").scrollTop = 0; },
    }, label);
    const items = [btn(icon("chev-l"), state.page - 1, { disabled: state.page <= 1, label: "Previous page" })];
    const p = state.page, n = state.pages;
    const nums = new Set([1, n, p - 1, p, p + 1]);
    if (p <= 3) [2, 3, 4].forEach((x) => nums.add(x));
    if (p >= n - 2) [n - 1, n - 2, n - 3].forEach((x) => nums.add(x));
    let prev = 0;
    for (const x of [...nums].filter((x) => x >= 1 && x <= n).sort((a, b) => a - b)) {
      if (x - prev > 1) items.push(h("span", { class: "page-gap" }, "…"));
      items.push(btn(String(x), x, { current: x === p }));
      prev = x;
    }
    items.push(btn(icon("chev-r"), state.page + 1, { disabled: state.page >= n, label: "Next page" }));
    pages.replaceChildren(...items);
  }

  // ------------------------------------------------------------------ filters
  function clearAll() {
    state.colFilters = {};
    state.page = 1;
    renderFilterState(); renderHead(); loadRows();
  }

  // ------------------------------------------------------------------ columns
  function renderColumnsList() {
    $("columnsList").replaceChildren(...state.meta.columns.map((c) => h("label", {},
      h("input", { type: "checkbox", checked: !state.hidden.has(c.name), onchange: (e) => {
        e.target.checked ? state.hidden.delete(c.name) : state.hidden.add(c.name);
        if (state.hidden.size >= state.meta.columns.length) { state.hidden.delete(c.name); e.target.checked = true; }
        store.set(hiddenKey(), [...state.hidden]);
        // A hidden column must not keep filtering rows invisibly.
        if (state.hidden.has(c.name) && state.colFilters[c.name] !== undefined) {
          setColumnFilter(c.name, ""); state.page = 1; renderHead(); loadRows(); return;
        }
        renderHead(); renderBody(); renderSelection();
      } }),
      h("span", {}, c.name), h("span", { class: "type" }, c.type || ""))));
  }

  // ------------------------------------------------------------------ popovers
  function togglePopover(id) {
    const pop = $(id);
    const open = pop.hidden;
    closePopovers();
    pop.hidden = !open;
  }
  function closePopovers() { $("columnsPopover").hidden = true; closePicker(); }

  // ------------------------------------------------------------------ editor drawer
  let editor = null; // { row, fields: Map(name -> {col, original, value, el}) }

  function inputValue(v) {
    if (v === null || v === undefined) return null;
    return String(v);
  }

  function openEditor(row) {
    const meta = state.meta;
    const edit = canEditRows();
    const isNew = !row;
    editor = { row, isNew, fields: new Map() };
    const keyCol = meta.key;
    $("drawerKicker").textContent = meta.name;
    $("drawerTitle").textContent = isNew ? "New row"
      : !edit ? "Row details"
      : keyCol ? `Edit row · ${keyCol} ${row.key}` : `Edit row #${row.key}`;
    $("drawerDelete").hidden = isNew || !canEdit();
    $("drawerSave").hidden = !edit;
    $("drawerSave").textContent = isNew ? "Create row" : "Save changes";
    $("drawerCancel").textContent = edit ? "Cancel" : "Close";

    const fields = meta.columns.map((col) => buildField(col, isNew ? undefined : row.values[col.name], isNew, edit));
    if (edit && !canEdit()) {  // regular user: say which fields they may change
      fields.unshift(h("div", { class: "notice" }, icon("edit"),
        h("div", {}, "You can edit ", h("b", {}, userEditColumns().join(", ")), ". The other fields are read-only.")));
    }
    $("drawerFields").replaceChildren(...fields);
    updateDirty();

    $("drawerScrim").hidden = false;
    $("drawer").classList.add("open");
    $("drawer").setAttribute("aria-hidden", "false");
    const first = $("drawerFields").querySelector("input:not([disabled]), textarea, .segmented button");
    setTimeout(() => (first || $("drawerClose")).focus(), 60);
  }

  function buildField(col, raw, isNew, edit) {
    const isBlob = raw && typeof raw === "object" && "$blob" in raw;
    const locked = !edit || !columnEditable(col.name) || col.readonly || col.kind === "blob" || isBlob;
    // A new row starts NULL unless the column is NOT NULL without a default.
    let original;
    if (isNew) original = col.notnull && col.default == null && !col.readonly ? (col.kind === "boolean" ? 0 : "") : null;
    else original = col.kind === "boolean" ? raw : inputValue(raw);
    const f = { col, original: isNew ? null : original, value: original, locked };
    editor.fields.set(col.name, f);

    const wrap = h("div", { class: "field" });
    const head = h("div", { class: "field-head" },
      h("label", { class: "field-label", for: "f-" + col.name }, col.name),
      col.type ? h("span", { class: "field-type" }, col.type.toLowerCase()) : null,
      col.pk ? h("span", { class: "field-flag" }, "primary key") : null,
      col.notnull && !col.pk ? h("span", { class: "field-flag" }, "required") : null);
    wrap.append(head);

    const setNullState = () => {
      wrap.classList.toggle("is-null", f.value === null);
      if (f.nullBtn) {
        f.nullBtn.classList.toggle("on", f.value === null);
        f.nullBtn.textContent = f.value === null ? "NULL" : "Set NULL";
        f.nullBtn.title = f.value === null ? "Value is NULL — type to set a value" : "Set this field to NULL";
      }
      if (f.input) f.input.placeholder = f.value === null ? (isNew && col.readonly ? "Auto" : "NULL") : "Empty string";
    };

    if (isBlob || col.kind === "blob") {
      wrap.append(h("input", { id: "f-" + col.name, disabled: true, value: isBlob ? `BLOB · ${fmtBytes(raw.$blob)}` : "" }),
        h("div", { class: "hint" }, "Binary values can’t be edited here."));
      return wrap;
    }

    if (col.kind === "boolean" && !col.readonly) {
      const options = [["1", "Yes", 1], ["0", "No", 0]];
      if (!col.notnull) options.push(["null", "NULL", null]);
      const seg = h("div", { class: "segmented", role: "radiogroup", "aria-label": col.name });
      const sync = () => seg.querySelectorAll("button").forEach((b) => {
        const on = String(f.value) === b.dataset.v || (f.value === null && b.dataset.v === "null");
        b.classList.toggle("on", on); b.setAttribute("aria-checked", on);
      });
      for (const [v, label, val] of options) {
        seg.append(h("button", { type: "button", role: "radio", dataset: { v }, disabled: locked,
          onclick: () => { f.value = val; sync(); updateDirty(); } }, label));
      }
      sync();
      wrap.append(seg);
      return wrap;
    }

    const long = typeof original === "string" && (original.length > 60 || original.includes("\n"));
    const input = long
      ? h("textarea", { id: "f-" + col.name, rows: Math.min(8, Math.max(3, Math.ceil(original.length / 52))), spellcheck: "false" })
      : h("input", { id: "f-" + col.name, autocomplete: "off", spellcheck: "false",
          inputmode: isNumericKind(col.kind) ? "decimal" : null });
    input.value = original ?? "";
    input.disabled = locked;
    input.addEventListener("input", () => { f.value = input.value; setNullState(); updateDirty(); });
    f.input = input;
    if (!locked && !col.notnull) {
      f.nullBtn = h("button", { type: "button", class: "null-toggle", onclick: () => {
        if (f.value === null) { f.value = ""; input.focus(); }
        else { f.value = null; input.value = ""; }
        setNullState(); updateDirty();
      } });
      head.append(f.nullBtn);
    }
    wrap.append(input);
    if (isNew && col.default != null && !col.readonly) {
      wrap.append(h("div", { class: "hint" }, `Leave NULL to use the default: ${col.default}`));
    }
    setNullState();
    return wrap;
  }

  function changedValues() {
    const values = {};
    for (const [name, f] of editor.fields) {
      if (f.locked) continue;
      if (editor.isNew ? f.value !== null : f.value !== f.original) values[name] = f.value;
    }
    return values;
  }

  function updateDirty() {
    if (!editor) return;
    for (const [name, f] of editor.fields) {
      const el = $("drawerFields").querySelector(`[for="f-${CSS.escape(name)}"]`);
      const changed = !editor.isNew && !f.locked && f.value !== f.original;
      if (el) el.closest(".field").classList.toggle("changed", changed);
    }
    if (!editor.isNew) $("drawerSave").disabled = Object.keys(changedValues()).length === 0;
    else $("drawerSave").disabled = false;
  }

  function closeEditor() {
    $("drawer").classList.remove("open");
    $("drawer").setAttribute("aria-hidden", "true");
    $("drawerScrim").hidden = true;
    editor = null;
  }

  async function saveEditor(e) {
    e.preventDefault();
    if (!editor || !canEditRows()) return;
    const values = changedValues();
    const btn = $("drawerSave");
    btn.disabled = true;
    try {
      if (editor.isNew) {
        await api(`${tablePath(state.table)}/rows`, { method: "POST", body: { values } });
        toast("Row created");
        closeEditor();
        await refreshCounts();
        await loadRows();
      } else {
        const { row } = await api(`${tablePath(state.table)}/rows/${encodeURIComponent(editor.row.key)}`,
          { method: "PATCH", body: { values } });
        toast(`Saved ${Object.keys(values).length} change${Object.keys(values).length === 1 ? "" : "s"}`);
        // Update in place so the row keeps its position while the user scans the page.
        const i = state.rows.findIndex((r) => r.key === editor.row.key);
        if (i >= 0) state.rows[i] = row;
        closeEditor();
        renderBody(); renderSelection();
      }
    } catch (err) {
      if (err.code !== "database_unavailable") toast(err.message, "error");
      btn.disabled = false;
    }
  }

  async function refreshCounts() {
    try { state.tables = (await api("/api/tables")).tables; renderTableList(); if (state.meta) renderSubtitle(); }
    catch { /* non-critical */ }
  }

  async function deleteRows(keys) {
    const n = keys.length;
    const ok = await confirmDialog(n === 1 ? "Delete this row?" : `Delete ${fmt(n)} rows?`,
      `This permanently removes ${n === 1 ? "the row" : "the selected rows"} from ${state.table}. This can’t be undone.`);
    if (!ok) return;
    try {
      const { deleted } = await api(`${tablePath(state.table)}/delete`, { method: "POST", body: { keys } });
      toast(`Deleted ${fmt(deleted)} row${deleted === 1 ? "" : "s"}`);
      if (editor) closeEditor();
      keys.forEach((k) => state.selected.delete(k));
      await refreshCounts();
      await loadRows({ keepSelection: true });
    } catch (err) {
      if (err.code !== "database_unavailable") toast(err.message, "error");
    }
  }

  // ------------------------------------------------------------------ wiring
  function bind() {
    window.UI.bindTheme($("themeBtn"));

    const reload = async () => {
      try { await loadStatus(true); if (isAdmin()) toast("Reloaded .env and reconnected"); }
      catch (e) { if (e.code !== "database_unavailable") toast(e.message, "error"); }
    };
    $("reloadBtn").onclick = reload;
    $("retryBtn").onclick = reload;

    $("clearFiltersBtn").onclick = clearAll;
    $("columnsBtn").onclick = (e) => { e.stopPropagation(); togglePopover("columnsPopover"); };
    $("showAllCols").onclick = () => {
      state.hidden.clear(); store.set(hiddenKey(), []);
      renderColumnsList(); renderHead(); renderBody(); renderSelection();
    };
    document.addEventListener("mousedown", (e) => {
      if (!e.target.closest(".popover-wrap, .picker, .filter-btn")) closePopovers();
    });
    const followAnchor = () => {
      if (!picker) return;
      if (!picker.anchor.isConnected) { closePicker(); return; }
      placePicker(picker.el, picker.anchor);
    };
    $("tableScroll").addEventListener("scroll", followAnchor);
    window.addEventListener("resize", followAnchor);

    $("pageSize").value = String(state.pageSize);
    $("pageSize").onchange = () => {
      state.pageSize = Number($("pageSize").value); store.set("pageSize", state.pageSize);
      state.page = 1; loadRows();
    };
    $("clearAllEmpty").onclick = clearAll;

    $("addBtn").onclick = () => openEditor(null);
    $("publishBtn").onclick = openPublish;
    $("bulkDeleteBtn").onclick = () => deleteRows([...state.selected]);
    $("rowForm").onsubmit = saveEditor;
    $("drawerClose").onclick = closeEditor;
    $("drawerCancel").onclick = closeEditor;
    $("drawerScrim").onclick = closeEditor;
    $("drawerDelete").onclick = () => editor && deleteRows([editor.row.key]);

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        if (window.UI.closeDialog()) return;
        if (editor) closeEditor();
        else closePopovers();
        return;
      }
      const typing = e.target.closest("input, textarea, select, [contenteditable]");
      if (e.key === "/" && !typing && !editor) {
        e.preventDefault();
        const first = $("gridHead").querySelector(".col-filter");
        if (first) first.focus();
      }
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && editor && canEditRows()) $("rowForm").requestSubmit();
    });
  }

  bind();
  loadStatus().catch((e) => { if (e.code !== "database_unavailable") showError(e.message); });
})();
