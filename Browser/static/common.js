/* Shared helpers for all DB Browser pages (window.UI). Vanilla JS, no build step. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const SVG_NS = "http://www.w3.org/2000/svg";

  /** Build an element safely: attrs, event handlers (onclick...), and text/node children. */
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "dataset") Object.assign(el.dataset, v);
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k in el && typeof v !== "string") el[k] = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat(Infinity)) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c instanceof Node ? c : String(c));
    }
    return el;
  }

  function icon(name, cls) {
    const svg = document.createElementNS(SVG_NS, "svg");
    if (cls) svg.setAttribute("class", cls);
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(SVG_NS, "use");
    use.setAttribute("href", "/static/icons.svg#i-" + name);
    svg.append(use);
    return svg;
  }

  const store = {
    get(key, fallback) {
      try { const v = localStorage.getItem("dbb:" + key); return v === null ? fallback : JSON.parse(v); }
      catch { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem("dbb:" + key, JSON.stringify(value)); } catch { /* ignore */ }
    },
  };

  const fmt = (n) => Number(n).toLocaleString();
  const fmtBytes = (b) => b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(1)} KB`
    : b < 1073741824 ? `${(b / 1048576).toFixed(1)} MB` : `${(b / 1073741824).toFixed(2)} GB`;
  function fmtDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }
  const initials = (name) => (name || "?").split(/\s+/).filter(Boolean).slice(0, 2)
    .map((p) => p[0].toUpperCase()).join("") || "?";

  function debounce(fn, ms) {
    let t;
    return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  }

  // ------------------------------------------------------------------ API
  class ApiError extends Error {
    constructor(message, status, code) { super(message); this.status = status; this.code = code; }
  }

  const UI = {};

  /** JSON fetch. Signed-out sessions go to /login; DB outages call UI.onUnavailable. */
  async function api(path, { method = "GET", body, signal } = {}) {
    const opts = { method, signal, credentials: "same-origin", headers: { Accept: "application/json" } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try { res = await fetch(path, opts); }
    catch (e) {
      if (e.name === "AbortError") throw e;
      throw new ApiError("Can’t reach the server. Is app.py still running?", 0);
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new ApiError(data.error || `Request failed (${res.status})`, res.status, data.code);
      if (data.code === "auth_required" && !UI.noAuthRedirect) {
        location.href = "/login";
      } else if (data.code === "database_unavailable" && UI.onUnavailable) {
        UI.onUnavailable(err.message);
      }
      throw err;
    }
    return data;
  }

  // ------------------------------------------------------------------ theme
  function effectiveTheme() {
    const t = document.documentElement.dataset.theme;
    if (t) return t;
    return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function applyTheme(theme, btn) {
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
    if (btn) btn.replaceChildren(icon(effectiveTheme() === "dark" ? "sun" : "moon"));
  }
  function bindTheme(btn) {
    applyTheme(store.get("theme", null), btn);
    if (!btn) return;
    btn.onclick = () => {
      const next = effectiveTheme() === "dark" ? "light" : "dark";
      store.set("theme", next); applyTheme(next, btn);
    };
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change",
      () => applyTheme(store.get("theme", null), btn));
  }

  // ------------------------------------------------------------------ toasts / dialogs
  function toast(message, type = "ok") {
    let box = $("toasts");
    if (!box) { box = h("div", { class: "toasts", id: "toasts", "aria-live": "polite" }); document.body.append(box); }
    const el = h("div", { class: "toast" + (type === "error" ? " error" : ""), role: "status" }, message);
    box.append(el);
    setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 250); }, type === "error" ? 6000 : 3200);
  }

  let activeDialog = null;
  /** Close the open dialog (used by Esc handlers). Returns true if one was open. */
  function closeDialog() {
    if (!activeDialog) return false;
    activeDialog(false);
    return true;
  }

  function modal(content, { onClose, labelledBy } = {}) {
    const box = h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-labelledby": labelledBy }, content);
    const scrim = h("div", { class: "modal-scrim" }, box);
    const close = (v) => {
      scrim.remove();
      if (activeDialog === close) activeDialog = null;
      if (onClose) onClose(v);
    };
    scrim.addEventListener("mousedown", (e) => { if (e.target === scrim) close(false); });
    activeDialog = close;
    document.body.append(scrim);
    return { box, close };
  }

  /** Promise<boolean> confirmation. `tone` is "danger" (default) or "primary". */
  function confirmDialog(title, text, okLabel = "Delete", tone = "danger") {
    return new Promise((resolve) => {
      const ok = h("button", { class: "btn " + (tone === "danger" ? "btn-danger" : "btn-primary") }, okLabel);
      const cancel = h("button", { class: "btn" }, "Cancel");
      const { close } = modal([
        h("div", { class: "modal-icon" + (tone === "danger" ? "" : " info") }, icon(tone === "danger" ? "trash" : "alert")),
        h("h3", { id: "dlgTitle" }, title), h("p", {}, text),
        h("div", { class: "modal-actions" }, cancel, ok),
      ], { onClose: resolve, labelledBy: "dlgTitle" });
      ok.onclick = () => close(true);
      cancel.onclick = () => close(false);
      setTimeout(() => ok.focus(), 0);
    });
  }

  /** Simple form dialog. fields: [{name, label, type, autocomplete}] -> Promise<values|null>.
   *  `submit(values)` may throw an Error to show it inline and keep the dialog open. */
  function formDialog(title, fields, okLabel, submit, intro) {
    return new Promise((resolve) => {
      const inputs = {};
      const err = h("div", { class: "form-error", role: "alert" });
      const ok = h("button", { class: "btn btn-primary", type: "submit" }, okLabel);
      const form = h("form", { class: "dialog-form", novalidate: true },
        h("h3", { id: "dlgTitle" }, title),
        intro ? h("p", {}, intro) : null,
        fields.map((f) => h("label", { class: "form-field" }, h("span", {}, f.label),
          (inputs[f.name] = h("input", { type: f.type || "text", autocomplete: f.autocomplete || "off", required: true })))),
        err,
        h("div", { class: "modal-actions" },
          h("button", { class: "btn", type: "button", onclick: () => close(null) }, "Cancel"), ok));
      const { close } = modal(form, { onClose: (v) => resolve(v || null), labelledBy: "dlgTitle" });
      form.onsubmit = async (e) => {
        e.preventDefault();
        const values = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value]));
        ok.disabled = true; err.textContent = "";
        try { await submit(values); close(values); }
        catch (ex) { err.textContent = ex.message; ok.disabled = false; }
      };
      setTimeout(() => inputs[fields[0].name].focus(), 0);
    });
  }

  function changePassword() {
    return formDialog("Change password", [
      { name: "current", label: "Current password", type: "password", autocomplete: "current-password" },
      { name: "next", label: "New password (min. 8 characters)", type: "password", autocomplete: "new-password" },
      { name: "confirm", label: "Confirm new password", type: "password", autocomplete: "new-password" },
    ], "Change password", async (v) => {
      if (v.next !== v.confirm) throw new Error("The new passwords don’t match.");
      await api("/api/auth/password", { method: "POST", body: { currentPassword: v.current, newPassword: v.next } });
    }).then((v) => { if (v) toast("Password changed"); });
  }

  // ------------------------------------------------------------------ user menu
  async function signOut() {
    try { await api("/api/auth/logout", { method: "POST", body: {} }); } catch { /* ignore */ }
    location.href = "/login";
  }

  /** Render the avatar button + dropdown into `container` for `user`. `page` is "data" or "admin". */
  function renderUserMenu(container, user, page) {
    if (!container || !user) return;
    const menu = h("div", { class: "menu", role: "menu", hidden: true },
      h("div", { class: "menu-head" },
        h("div", { class: "menu-name" }, user.fullName),
        h("div", { class: "menu-email" }, user.email),
        h("span", { class: "badge " + (user.role === "admin" ? "badge-accent" : "") }, user.role === "admin" ? "Administrator" : "User")),
      user.role === "admin" && page !== "admin"
        ? h("a", { class: "menu-item", href: "/admin", role: "menuitem" }, icon("users"), "User management") : null,
      page === "admin" ? h("a", { class: "menu-item", href: "/", role: "menuitem" }, icon("table"), "Browse data") : null,
      h("button", { class: "menu-item", role: "menuitem", onclick: () => { menu.hidden = true; changePassword(); } }, icon("key"), "Change password"),
      h("div", { class: "menu-sep" }),
      h("button", { class: "menu-item", role: "menuitem", onclick: signOut }, icon("logout"), "Sign out"));
    const btn = h("button", { class: "user-btn", "aria-haspopup": "menu", title: user.email,
      onclick: (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; } },
      h("span", { class: "avatar" }, initials(user.fullName)),
      h("span", { class: "user-name" }, user.fullName));
    container.replaceChildren(h("div", { class: "user-menu" }, btn, menu));
    document.addEventListener("mousedown", (e) => { if (!e.target.closest(".user-menu")) menu.hidden = true; });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") menu.hidden = true; });
  }

  Object.assign(UI, {
    $, h, icon, store, fmt, fmtBytes, fmtDate, initials, debounce, ApiError, api,
    effectiveTheme, applyTheme, bindTheme, toast, modal, confirmDialog, formDialog, closeDialog,
    changePassword, renderUserMenu, signOut,
  });
  window.UI = UI;
})();
