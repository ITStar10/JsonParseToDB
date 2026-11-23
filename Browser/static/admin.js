/* User management page (admins only). */
(() => {
  "use strict";
  const { $, h, icon, fmt, fmtDate, initials, debounce, api, toast, confirmDialog, formDialog } = window.UI;

  const TABS = [["pending", "Pending"], ["approved", "Approved"], ["rejected", "Rejected"],
                ["disabled", "Disabled"], ["all", "All"]];
  const STATUS_LABEL = { pending: "Pending", approved: "Approved", rejected: "Rejected", disabled: "Disabled" };
  const ACCESS_HELP = "Same syntax as the filter boxes: austin, boston (any of) · =exact · !exclude · " +
    ">, <, >=, <= · 1980-01-01..1989-12-31 (range) · = (empty) · != (not empty)";

  const state = { me: null, users: [], counts: {}, tab: null, search: "" };

  // ------------------------------------------------------------------ list
  async function load() {
    const data = await api("/api/admin/users");
    state.users = data.users;
    state.counts = data.counts;
    if (!state.tab) state.tab = data.counts.pending ? "pending" : "approved";
    renderTabs();
    renderUsers();
  }

  function renderTabs() {
    const total = Object.values(state.counts).reduce((a, b) => a + b, 0);
    $("tabs").replaceChildren(...TABS.map(([key, label]) => {
      const n = key === "all" ? total : state.counts[key] || 0;
      return h("button", { role: "tab", class: state.tab === key ? "on" : "", "aria-selected": state.tab === key,
        onclick: () => { state.tab = key; renderTabs(); renderUsers(); } },
        label, h("span", { class: "tab-count" + (key === "pending" && n ? " hot" : "") }, fmt(n)));
    }));
  }

  function visibleUsers() {
    const q = state.search.toLowerCase();
    return state.users.filter((u) => (state.tab === "all" || u.status === state.tab)
      && (!q || u.fullName.toLowerCase().includes(q) || u.email.toLowerCase().includes(q)));
  }

  function renderUsers() {
    const list = visibleUsers();
    $("usersBody").replaceChildren(...list.map(userRow));
    $("usersEmpty").hidden = list.length > 0;
    if (!list.length) {
      $("emptyTitle").textContent = state.search ? "No matching users"
        : state.tab === "pending" ? "No pending requests" : "No users here";
      $("emptyText").textContent = state.search ? "Try a different name or e-mail."
        : state.tab === "pending" ? "New sign-up requests will show up here." : "";
    }
  }

  function userRow(u) {
    const self = state.me && u.id === state.me.id;
    const actions = h("div", { class: "row-btns" });
    const btn = (label, cls, fn, ic) => h("button", { class: "btn btn-sm " + (cls || ""), onclick: fn }, ic ? icon(ic) : null, label);
    if (u.status === "pending" || u.status === "rejected") {
      actions.append(btn("Approve…", "btn-primary", () => openAccess(u, true), "check"));
      if (u.status === "pending") actions.append(btn("Reject", "", () => setStatus(u, "rejected")));
    } else if (u.status === "approved") {
      if (u.role === "admin") actions.append(h("span", { class: "muted-note" }, "Full access"));
      else actions.append(btn("Data access", "", () => openAccess(u, false), "shield"));
    } else if (u.status === "disabled") {
      actions.append(btn("Enable", "", () => setStatus(u, "approved")));
    }
    if (!self) {
      actions.append(h("button", { class: "icon-btn", title: "More actions", "aria-label": `More actions for ${u.fullName}`,
        onclick: (e) => { e.stopPropagation(); openRowMenu(u, e.currentTarget); } }, icon("more")));
    }
    return h("tr", {},
      h("td", {}, h("div", { class: "user-cell" },
        h("span", { class: "avatar" + (u.role === "admin" ? " admin" : "") }, initials(u.fullName)),
        h("div", { class: "user-text" },
          h("div", { class: "user-full" }, u.fullName, self ? h("span", { class: "badge you" }, "You") : null),
          h("div", { class: "user-mail" }, u.email)))),
      h("td", {}, h("span", { class: "badge " + (u.role === "admin" ? "badge-accent" : "") }, u.role === "admin" ? "Admin" : "User")),
      h("td", {}, h("span", { class: "status status-" + u.status }, STATUS_LABEL[u.status])),
      h("td", { class: "date-cell" }, fmtDate(u.createdAt)),
      h("td", { class: "date-cell" }, u.lastLoginAt ? fmtDate(u.lastLoginAt) : h("span", { class: "null" }, "never")),
      h("td", { class: "act-col" }, actions));
  }

  // ------------------------------------------------------------------ row menu
  let rowMenu = null;
  function closeRowMenu() { if (rowMenu) { rowMenu.remove(); rowMenu = null; } }

  function openRowMenu(u, anchor) {
    closeRowMenu();
    const item = (label, ic, fn, danger) => h("button", { class: "menu-item" + (danger ? " danger" : ""), role: "menuitem",
      onclick: () => { closeRowMenu(); fn(); } }, icon(ic), label);
    const items = [];
    if (u.status === "approved" && u.role !== "admin") items.push(item("Data access", "shield", () => openAccess(u, false)));
    if (u.status === "disabled" && u.role !== "admin") items.push(item("Data access", "shield", () => openAccess(u, false)));
    if (u.status === "approved") {
      items.push(u.role === "admin" ? item("Make regular user", "users", () => setRole(u, "user"))
        : item("Make administrator", "users", () => setRole(u, "admin")));
    }
    items.push(item("Reset password", "key", () => resetPassword(u)));
    if (u.status === "approved") items.push(item("Disable account", "ban", () => setStatus(u, "disabled"), true));
    items.push(item("Delete account", "trash", () => deleteUser(u), true));
    rowMenu = h("div", { class: "menu floating", role: "menu" }, items);
    document.body.append(rowMenu);
    const r = anchor.getBoundingClientRect();
    const w = rowMenu.offsetWidth, hh = rowMenu.offsetHeight;
    rowMenu.style.left = Math.max(8, Math.min(r.right - w, innerWidth - w - 8)) + "px";
    rowMenu.style.top = (r.bottom + 6 + hh > innerHeight ? Math.max(8, r.top - hh - 6) : r.bottom + 6) + "px";
  }

  // ------------------------------------------------------------------ actions
  async function run(fn, message) {
    try { await fn(); if (message) toast(message); await load(); }
    catch (e) { toast(e.message, "error"); }
  }

  async function setStatus(u, status) {
    const texts = {
      rejected: [`Reject ${u.fullName}?`, "They won’t be able to sign in. You can still approve them later.", "Reject"],
      disabled: [`Disable ${u.fullName}?`, "They are signed out immediately and can’t sign in until you enable the account again.", "Disable"],
    };
    if (texts[status] && !(await confirmDialog(...texts[status]))) return;
    run(() => api(`/api/admin/users/${u.id}/status`, { method: "POST", body: { status } }),
      status === "approved" ? `${u.fullName} can sign in again` : `${u.fullName} ${status}`);
  }

  async function setRole(u, role) {
    const ok = role === "admin"
      ? await confirmDialog(`Make ${u.fullName} an administrator?`,
        "Administrators can see and edit all data and manage every user.", "Make administrator", "primary")
      : await confirmDialog(`Make ${u.fullName} a regular user?`,
        "They lose editing rights and user management, and their data access rules apply again.", "Make regular user", "primary");
    if (!ok) return;
    run(() => api(`/api/admin/users/${u.id}/role`, { method: "POST", body: { role } }), "Role updated");
  }

  async function deleteUser(u) {
    if (!(await confirmDialog(`Delete ${u.fullName}?`,
      "The account and its data access rules are removed permanently. They can request access again later."))) return;
    run(() => api(`/api/admin/users/${u.id}`, { method: "DELETE" }), "Account deleted");
  }

  function resetPassword(u) {
    formDialog(`Reset password for ${u.fullName}`, [
      { name: "password", label: "New password (min. 8 characters)", type: "password", autocomplete: "new-password" },
      { name: "confirm", label: "Confirm new password", type: "password", autocomplete: "new-password" },
    ], "Set password", async (v) => {
      if (v.password !== v.confirm) throw new Error("The passwords don’t match.");
      await api(`/api/admin/users/${u.id}/password`, { method: "POST", body: { password: v.password } });
    }, "Share the new password with them securely. Their existing sessions are signed out.")
      .then((v) => { if (v) toast("Password updated"); });
  }

  // ------------------------------------------------------------------ data access drawer
  // One person's rules; tables without a rule of their own follow the data shown to all users.
  let access = null; // { scope: "user", user, approve, tables, rules: {table: {mode, filters}}, defaults }

  function openDrawer({ kicker, title, sub, save }) {
    $("drawerKicker").textContent = kicker;
    $("drawerTitle").textContent = title;
    $("drawerSub").textContent = sub;
    $("drawerSave").textContent = save;
    $("drawerSave").disabled = true;
    $("accessBody").replaceChildren(h("div", { class: "picker-note" }, "Loading…"));
    $("drawerScrim").hidden = false;
    $("drawer").classList.add("open");
    $("drawer").setAttribute("aria-hidden", "false");
  }

  function loadFailed(e, extra) {
    $("accessBody").replaceChildren(h("div", { class: "notice warn" }, icon("alert"),
      h("div", {}, h("b", {}, "Data access can’t be edited right now. "), e.message, extra || "")));
  }

  async function openAccess(u, approve) {
    access = { scope: "user", user: u, approve, tables: [], rules: {}, defaults: {} };
    openDrawer({ kicker: approve ? "Approve sign-up" : "Data access", title: u.fullName, sub: u.email,
      save: approve ? "Approve & save" : "Save access" });
    try {
      const data = await api(`/api/admin/users/${u.id}/access`);
      if (!access || access.user?.id !== u.id) return;
      access.tables = data.tables;
      access.defaults = data.defaults;
      for (const t of data.tables) {
        const r = data.rules[t.name];  // no row = "use the default"
        access.rules[t.name] = r ? { mode: r.mode, filters: { ...r.filters }, sample: r.sample || null, hadSample: !!r.sample, reshuffle: false }
          : { mode: "inherit", filters: {}, sample: null, hadSample: false, reshuffle: false };
      }
      renderAccess();
    } catch (e) {
      loadFailed(e, approve ? " You can still approve the account; they will see the data shown to all users until you set rules." : "");
    }
    $("drawerSave").disabled = false;
  }

  function closeAccess() {
    $("drawer").classList.remove("open");
    $("drawer").setAttribute("aria-hidden", "true");
    $("drawerScrim").hidden = true;
    access = null;
  }

  function renderAccess() {
    const parts = [];
    {
      if (access.approve) {
        parts.push(h("div", { class: "notice" }, icon("check"),
          h("div", {}, `Approving lets ${access.user.fullName} sign in with read-only access. `,
            "They see the data you chose to show to users unless you choose something else below.")));
      }
      if (access.user.role === "admin") {
        parts.push(h("div", { class: "notice" }, icon("shield"),
          h("div", {}, "Administrators always see all data. These rules apply if they become a regular user.")));
      }
    }
    if (!access.tables.length) parts.push(h("div", { class: "picker-note" }, "The database has no tables."));
    for (const t of access.tables) parts.push(tableSection(t));
    $("accessBody").replaceChildren(...parts);
    for (const t of access.tables) if (access.rules[t.name].mode === "filtered") preview(t);
  }

  /** One-line description of a rule, e.g. "Only matching rows: state =TX, =CO · dateOfBirth 1970..1989". */
  function ruleSummary(rule) {
    const sample = rule && rule.sample ? ` · random ${fmt(rule.sample)} per user` : "";
    if (!rule || rule.mode === "all") return "All rows" + sample;
    if (rule.mode === "none") return "No access";
    const f = Object.entries(rule.filters || {}).map(([c, v]) => `${c} ${v}`).join(" · ");
    return "Only matching rows" + (f ? `: ${f}` : "") + sample;
  }

  function tableSection(t) {
    const rule = access.rules[t.name];
    const section = h("section", { class: "access-table", dataset: { table: t.name } });
    const modes = [["all", "All rows"], ["filtered", "Only matching rows"], ["none", "No access"]];
    modes.unshift(["inherit", "Same as all users"]);
    const seg = h("div", { class: "segmented", role: "radiogroup", "aria-label": `Access to ${t.name}` },
      modes.map(([m, label]) => h("button", { type: "button", role: "radio", class: rule.mode === m ? "on" : "",
        "aria-checked": rule.mode === m, onclick: () => { rule.mode = m; renderAccess(); } }, label)));
    section.append(h("div", { class: "access-head" },
      h("div", { class: "access-name" }, icon(t.kind === "view" ? "eye" : "table"), h("b", {}, t.name),
        t.rows != null ? h("span", { class: "muted-note" }, `${fmt(t.rows)} rows`) : null),
      seg));
    if (rule.mode === "inherit") {
      section.append(h("p", { class: "access-desc" }, "Follows the data shown to all users: ",
        h("b", {}, ruleSummary(access.defaults[t.name])), "."));
    }
    if (rule.mode === "all") section.append(h("p", { class: "access-desc" }, "Can see every row in this table."));
    const sampleRow = () => {
      const input = h("input", { type: "number", min: "1", step: "1", class: "sample-input", placeholder: "All",
        "aria-label": `Random rows for ${t.name}`, value: rule.sample ? String(rule.sample) : "",
        oninput: (e) => {
          const raw = e.target.value.trim(), n = Number(raw);
          rule.sample = raw !== "" && Number.isInteger(n) && n >= 1 ? n : null;
          e.target.setCustomValidity(raw !== "" && !rule.sample ? "Enter a whole number" : "");
          if (rule.mode === "filtered") debouncedPreview(t);
        } });
      return h("div", { class: "access-sample" },
        h("label", {}, h("span", {}, h("b", {}, "Random selection"), h("small", {}, "Show only this many random rows per user. Leave empty to show all.")),
          h("span", { class: "sample-field" }, input, h("span", {}, "rows"))),
        rule.hadSample ? h("label", { class: "publish-reshuffle" },
          h("input", { type: "checkbox", checked: rule.reshuffle, onchange: (e) => { rule.reshuffle = e.target.checked; } }),
          "Pick new random rows") : null);
    };
    if (rule.mode === "all") section.append(sampleRow());
    if (rule.mode === "none") section.append(h("p", { class: "access-desc" }, "This table is hidden."));
    if (rule.mode === "filtered") {
      section.append(h("p", { class: "access-desc" }, "Rows must match every filter below. ", h("span", { class: "help", title: ACCESS_HELP }, "Syntax help")));
      const grid = h("div", { class: "access-grid" });
      for (const col of t.columns) {
        if (col.kind === "blob") continue;
        const value = rule.filters[col.name] || "";
        const onChange = (v) => {
          if (v) rule.filters[col.name] = v; else delete rule.filters[col.name];
          input.classList.toggle("active", !!v);
          debouncedPreview(t);
        };
        let input;
        if (col.kind === "boolean") {
          input = h("select", { class: "col-filter" + (value ? " active" : ""), onchange: (e) => onChange(e.target.value) },
            h("option", { value: "" }, "Any"), h("option", { value: "1" }, "Yes"),
            h("option", { value: "0" }, "No"), h("option", { value: "null" }, "NULL"));
          input.value = value;
        } else {
          input = h("input", { class: "col-filter" + (value ? " active" : ""), autocomplete: "off", spellcheck: "false",
            placeholder: col.kind === "date" ? "e.g. 1980..1989" : "Any", title: ACCESS_HELP,
            oninput: (e) => onChange(e.target.value) });
          input.value = value;
        }
        grid.append(h("label", { class: "access-field" },
          h("span", {}, col.name, h("small", {}, col.kind === "date" ? "date" : (col.type || "").toLowerCase())), input));
      }
      section.append(grid, sampleRow(), h("div", { class: "access-preview", id: "pv-" + t.name }, " "));
    }
    return section;
  }

  const previewTimers = {};
  function debouncedPreview(t) {
    clearTimeout(previewTimers[t.name]);
    previewTimers[t.name] = setTimeout(() => preview(t), 350);
  }

  async function preview(t) {
    const el = document.getElementById("pv-" + t.name);
    if (!el || !access) return;
    const filters = access.rules[t.name].filters;
    if (!Object.keys(filters).length) {
      el.className = "access-preview warn";
      el.textContent = "Add at least one filter, or choose “All rows” or “No access”.";
      return;
    }
    try {
      const r = await api("/api/admin/access-preview", { method: "POST", body: { table: t.name, filters } });
      el.className = "access-preview";
      const rule = access.rules[t.name];
      const shown = rule.sample ? Math.min(rule.sample, r.matched) : r.matched;
      const detail = rule.sample && r.matched > shown ? ` — a random ${fmt(shown)} of the ${fmt(r.matched)} that match` : "";
      el.replaceChildren("They will see ", h("b", {}, fmt(shown)), " of ", fmt(r.total), " rows", detail, ".");
    } catch (e) {
      el.className = "access-preview warn";
      el.textContent = e.message;
    }
  }

  async function saveAccess(e) {
    e.preventDefault();
    if (!access) return;
    const { user, approve } = access;
    $("drawerSave").disabled = true;
    try {
      if (access.tables.length) {
        await api(`/api/admin/users/${user.id}/access`, { method: "PUT", body: { rules: access.rules, approve } });
      } else if (approve) {
        await api(`/api/admin/users/${user.id}/status`, { method: "POST", body: { status: "approved" } });
      }
      toast(approve ? `${user.fullName} approved` : "Data access saved");
      closeAccess();
      await load();
    } catch (err) {
      toast(err.message, "error");
      $("drawerSave").disabled = false;
    }
  }

  // ------------------------------------------------------------------ default access card
  /** "Random rows per user" setting for one table. Empty = every row the rule allows. */
  function sampleControl(t, rule) {
    const hidden = rule && rule.mode === "none";
    const input = h("input", { type: "number", min: "1", step: "1", class: "sample-input", placeholder: "All",
      "aria-label": `Random rows per user in ${t.name}`, disabled: hidden, value: rule && rule.sample ? String(rule.sample) : "" });
    const save = h("button", { class: "btn btn-sm btn-primary", type: "button", hidden: true }, "Save");
    const reshuffle = rule && rule.sample && !hidden
      ? h("button", { class: "link-btn", type: "button", title: "Give every user a new random selection" }, "Pick new random rows") : null;
    const parse = () => {
      const raw = input.value.trim(), n = Number(raw);
      if (raw === "") return null;
      if (!Number.isInteger(n) || n < 1) throw new Error("Enter a whole number of rows, or leave the box empty to show all.");
      return n;
    };
    const apply = async (reshuffleNow) => {
      try {
        const sample = parse();
        const { rules } = await api("/api/admin/access-defaults");
        rules[t.name] = { ...(rules[t.name] || { mode: "all", filters: {} }), sample, reshuffle: !!reshuffleNow };
        await api("/api/admin/access-defaults", { method: "PUT", body: { rules } });
        toast(reshuffleNow ? "New random rows picked"
          : sample ? `Users now see a random ${fmt(sample)} rows of ${t.name}` : `Users now see all allowed rows of ${t.name}`);
        loadDefaults();
      } catch (err) { toast(err.message, "error"); }
    };
    input.addEventListener("input", () => { save.hidden = false; });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); apply(false); } });
    save.onclick = () => apply(false);
    if (reshuffle) reshuffle.onclick = () => apply(true);
    return h("div", { class: "default-sample" },
      h("label", {}, h("span", {}, h("b", {}, "Random rows per user"),
        h("small", {}, hidden ? "This table is hidden from users." : "Each user sees this many random rows. Leave empty to show all.")),
        h("span", { class: "sample-field" }, input, h("span", {}, "rows"))),
      save, reshuffle);
  }

  async function loadDefaults() {
    const body = $("defaultsBody");
    try {
      const [data, edit, vis] = await Promise.all([api("/api/admin/access-defaults"),
        api("/api/admin/editable-columns"), api("/api/admin/visible-columns")]);
      const visible = Object.fromEntries(vis.tables.map((t) => [t.name, t]));
      if (!data.tables.length) { body.replaceChildren(h("span", { class: "muted-note" }, "The database has no tables.")); return; }
      const editable = Object.fromEntries(edit.tables.map((t) => [t.name, t]));
      body.replaceChildren(...data.tables.map((t) => {
        const rule = data.rules[t.name];
        const mode = rule ? rule.mode : "all";
        const e = editable[t.name];
        const can = !e || e.locked ? "no editing" : e.editable.length ? e.editable.join(", ") : "nothing";
        return h("div", { class: "default-block" },
          h("div", { class: "default-row" },
            h("span", { class: "default-table" }, icon(t.kind === "view" ? "eye" : "table"), t.name),
            h("span", { class: "default-rule " + mode }, ruleSummary(rule)),
            h("span", { class: "default-edit" }, icon("eye"), "Columns: ", h("b", {}, visibleSummary(visible[t.name]))),
            h("span", { class: "default-edit" }, icon("edit"), "Users can edit: ", h("b", {}, can),
              e && e.isDefault && e.editable.length ? h("small", {}, " (default)") : null)),
          sampleControl(t, rule));
      }));
    } catch (e) {
      body.replaceChildren(h("span", { class: "muted-note" }, e.message));
    }
  }

  function visibleSummary(t) {
    if (!t || t.isDefault) return "all";
    const hidden = t.columns.map((c) => c.name).filter((n) => !t.visible.includes(n));
    return hidden.length ? `${t.visible.length} of ${t.columns.length} (hidden: ${hidden.join(", ")})` : "all";
  }

  // ------------------------------------------------------------------ visible columns dialog
  async function openVisibleColumns() {
    let data;
    try { data = await api("/api/admin/visible-columns"); } catch (e) { toast(e.message, "error"); return; }
    const picks = {}; // table -> {checked:Set, reset:bool}
    const sections = data.tables.map((t) => {
      const st = picks[t.name] = { checked: new Set(t.visible), reset: t.isDefault };
      const boxes = h("div", { class: "edit-grid" });
      const note = h("p", { class: "edit-note" });
      const count = h("span", { class: "muted-note" });
      const sync = () => {
        count.textContent = `${st.checked.size} of ${t.columns.length} shown`;
        note.textContent = st.reset ? "All columns are shown (default). Columns added to the data later are shown too."
          : st.checked.size ? "Columns added to the data later stay hidden until you tick them." : "Tick at least one column.";
        note.classList.toggle("warn", !st.checked.size);
      };
      const render = () => boxes.replaceChildren(...t.columns.map((c) => h("label", { class: "edit-col" },
        h("input", { type: "checkbox", checked: st.checked.has(c.name), onchange: (e) => {
          e.target.checked ? st.checked.add(c.name) : st.checked.delete(c.name);
          st.reset = false; sync(); allBtn.hidden = false;
        } }), h("span", {}, c.name), h("small", {}, c.kind === "date" ? "date" : (c.type || "").toLowerCase()))));
      const allBtn = h("button", { type: "button", class: "link-btn", hidden: st.reset, onclick: () => {
        st.reset = true; st.checked = new Set(t.columns.map((c) => c.name)); render(); sync(); allBtn.hidden = true;
      } }, "Show all columns");
      render(); sync();
      return h("section", { class: "edit-table" },
        h("div", { class: "access-head" }, h("div", { class: "access-name" }, icon(t.kind === "view" ? "eye" : "table"),
          h("b", {}, t.name), count), allBtn), boxes, note);
    });
    const save = h("button", { class: "btn btn-primary", type: "button" }, "Save");
    const { box, close } = window.UI.modal([
      h("div", { class: "modal-icon info" }, icon("eye")),
      h("h3", { id: "vcTitle" }, "Columns users can see"),
      h("p", {}, "Regular users only receive these columns. They can’t view, filter, sort or search the others. Your data access filters may still use hidden columns. Administrators always see everything."),
      h("div", { class: "edit-tables" }, sections),
      h("div", { class: "modal-actions" }, h("button", { class: "btn", type: "button", onclick: () => close(false) }, "Cancel"), save),
    ], { labelledBy: "vcTitle" });
    box.classList.add("modal-wide");
    save.onclick = async () => {
      const rules = {};
      for (const t of data.tables) {
        const st = picks[t.name];
        if (!st.reset && !st.checked.size) { toast(`Tick at least one column in ${t.name}.`, "error"); return; }
        rules[t.name] = st.reset ? null : t.columns.map((c) => c.name).filter((n) => st.checked.has(n));
      }
      save.disabled = true;
      try {
        await api("/api/admin/visible-columns", { method: "PUT", body: { rules } });
        toast("Visible columns saved");
        close(true);
        loadDefaults();
      } catch (e) { toast(e.message, "error"); save.disabled = false; }
    };
  }

  // ------------------------------------------------------------------ editable columns dialog
  async function openEditableColumns() {
    let data;
    try { data = await api("/api/admin/editable-columns"); } catch (e) { toast(e.message, "error"); return; }
    const state = {}; // table -> {checked:Set, reset:bool}
    const sections = data.tables.map((t) => {
      const st = state[t.name] = { checked: new Set(t.editable), reset: t.isDefault };
      const boxes = h("div", { class: "edit-grid" });
      const note = h("p", { class: "edit-note" });
      const sync = () => {
        const defaultHidden = t.candidates.some((c) => c.hidden && data.defaultColumns.some((d) => d.toLowerCase() === c.name.toLowerCase()));
        note.textContent = st.reset ? `Default: ${data.defaultColumns.join(", ")} (only if the table has such a column)`
            + (defaultHidden ? " — currently hidden from users, so nothing is editable." : "")
          : st.checked.size ? "" : "Users can't edit anything in this table.";
      };
      const render = () => {
        boxes.replaceChildren(...t.candidates.map((c) => h("label", { class: "edit-col" + (c.hidden ? " off" : ""),
          title: c.hidden ? "Hidden from users (Visible columns), so it can't be edited by them" : null },
          h("input", { type: "checkbox", checked: st.checked.has(c.name) && !c.hidden, disabled: c.hidden, onchange: (e) => {
            e.target.checked ? st.checked.add(c.name) : st.checked.delete(c.name);
            st.reset = false; sync(); resetBtn.hidden = false;
          } }), h("span", {}, c.name),
          c.hidden ? h("small", { class: "hidden-tag" }, icon("eye-off"), "hidden") : h("small", {}, c.type ? c.type.toLowerCase() : ""))));
      };
      const resetBtn = h("button", { type: "button", class: "link-btn", hidden: st.reset, onclick: () => {
        const wanted = data.defaultColumns.map((d) => d.toLowerCase());
        st.reset = true; st.checked = new Set(t.candidates.map((c) => c.name).filter((n) => wanted.includes(n.toLowerCase())));
        render(); sync(); resetBtn.hidden = true;
      } }, "Reset to default");
      render(); sync();
      const hiddenNames = t.candidates.filter((c) => c.hidden).map((c) => c.name);
      const one = hiddenNames.length === 1;
      const hiddenNote = hiddenNames.length ? h("div", { class: "notice subtle" }, icon("eye-off"),
        h("div", {}, h("b", {}, hiddenNames.join(", ")), one ? " is" : " are",
          ` hidden from users, so users can’t edit ${one ? "it" : "them"}. To allow editing, first make ${one ? "it" : "them"} visible in `,
          h("button", { type: "button", class: "link-btn", onclick: () => { close(false); openVisibleColumns(); } }, "Visible columns"),
          ".")) : null;
      return h("section", { class: "edit-table" },
        h("div", { class: "access-head" }, h("div", { class: "access-name" }, icon(t.kind === "view" ? "eye" : "table"), h("b", {}, t.name)), resetBtn),
        t.locked ? h("p", { class: "edit-note" }, "Can't be edited by users (a view, or a table without a row key).") : [boxes, hiddenNote, note]);
    });
    const save = h("button", { class: "btn btn-primary", type: "button" }, "Save");
    const { box, close } = window.UI.modal([
      h("div", { class: "modal-icon info" }, icon("edit")),
      h("h3", { id: "ecTitle" }, "Columns users can edit"),
      h("p", {}, "Regular users can change only these columns, and only in rows they can see. They can never add or delete rows. Administrators can edit everything."),
      h("div", { class: "edit-tables" }, sections),
      h("div", { class: "modal-actions" }, h("button", { class: "btn", type: "button", onclick: () => close(false) }, "Cancel"), save),
    ], { labelledBy: "ecTitle" });
    box.classList.add("modal-wide");
    save.onclick = async () => {
      save.disabled = true;
      const rules = {};
      for (const t of data.tables) {
        if (t.locked) continue;
        const st = state[t.name];
        rules[t.name] = st.reset ? null : t.candidates.map((c) => c.name).filter((n) => st.checked.has(n));
      }
      try {
        await api("/api/admin/editable-columns", { method: "PUT", body: { rules } });
        toast("Editable columns saved");
        close(true);
        loadDefaults();
      } catch (e) { toast(e.message, "error"); save.disabled = false; }
    };
  }

  // ------------------------------------------------------------------ wiring
  async function init() {
    window.UI.bindTheme($("themeBtn"));
    $("userSearch").addEventListener("input", debounce(() => { state.search = $("userSearch").value.trim(); renderUsers(); }, 150));
    $("accessForm").onsubmit = saveAccess;
    $("editColsBtn").onclick = openEditableColumns;
    $("visibleColsBtn").onclick = openVisibleColumns;
    $("drawerClose").onclick = $("drawerCancel").onclick = $("drawerScrim").onclick = closeAccess;
    document.addEventListener("mousedown", (e) => { if (rowMenu && !e.target.closest(".menu.floating")) closeRowMenu(); });
    window.addEventListener("scroll", closeRowMenu, true);
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (window.UI.closeDialog()) return;
      if (rowMenu) closeRowMenu();
      else if (access) closeAccess();
    });
    try {
      const me = await api("/api/auth/me");
      state.me = me.user;
      window.UI.renderUserMenu($("userMenu"), me.user, "admin");
      loadDefaults();
      await load();
    } catch (e) { if (e.code !== "auth_required") toast(e.message, "error"); }
  }
  init();
})();
