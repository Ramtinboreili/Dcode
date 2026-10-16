/* Dcode Manager web UI: sign in, your usage and keys; for admins monitoring, models, discovery, users,
   requests and settings. No libraries (air-gapped). Every user-provided string goes in as text, never HTML. */
(() => {
  "use strict";

  // ---------- DOM helpers ----------
  const $ = (sel, el = document) => el.querySelector(sel);
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key === "html") el.innerHTML = value; // trusted markup only (our own SVG icons)
      else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
      else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
      else if (key === "value") el.value = value;
      else if (key === "checked") el.checked = !!value;
      else el.setAttribute(key, value === true ? "" : value);
    }
    add(el, kids);
    return el;
  }
  function add(el, kids) {
    for (const kid of kids.flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  const SVG_NS = "http://www.w3.org/2000/svg";
  function s(tag, attrs, ...kids) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs || {})) if (value !== null && value !== undefined) el.setAttribute(key, value);
    for (const kid of kids.flat(Infinity)) if (kid !== null && kid !== undefined) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    return el;
  }

  const ICONS = {
    usage: "M4 19V9M10 19V5M16 19v-7M22 19H2",
    key: "M15 7a4 4 0 1 1-3.9 4.9L3 20v-3h3v-3h3l2.1-2.1A4 4 0 0 1 15 7Z",
    plug: "M9 2v6M15 2v6M6 8h12v3a6 6 0 0 1-12 0V8ZM12 17v5",
    pulse: "M3 12h4l3-8 4 16 3-8h4",
    cube: "M12 2 21 7v10l-9 5-9-5V7l9-5ZM3 7l9 5 9-5M12 12v10",
    radar: "M12 12 19 5M21 12a9 9 0 1 1-9-9M17 12a5 5 0 1 1-5-5",
    users: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75",
    list: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
    gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z",
    x: "M18 6 6 18M6 6l12 12",
    plus: "M12 5v14M5 12h14",
    copy: "M9 9h11v11H9zM5 15H4V4h11v1",
    out: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
    menu: "M4 6h16M4 12h16M4 18h16",
    refresh: "M21 12a9 9 0 1 1-2.6-6.4M21 3v6h-6",
  };
  function icon(name) {
    return h("span", { html: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${ICONS[name]}"/></svg>`, style: { display: "inline-flex" } });
  }
  function logo() {
    const frame = "M11 2 H53 L62 11 V29 L53 38 H11 L2 29 V11 Z";
    return h("span", { html: `<svg class="logo" viewBox="0 0 64 40" aria-hidden="true"><path class="frame" d="${frame}"/><path class="glyph" d="M14 12 H19.5 C25 12 28 15.6 28 20 C28 24.4 25 28 19.5 28 H14 Z"/><path class="glyph" d="M33 14 L39 20 L33 26"/><rect class="cursor" x="42" y="25" width="8" height="3.4" rx="1.7"/></svg>` }).firstElementChild;
  }

  // ---------- formatting ----------
  const nf = new Intl.NumberFormat();
  const num = (n) => (n === null || n === undefined ? "—" : nf.format(Math.round(n)));
  function short(n) {
    if (n === null || n === undefined) return "—";
    const a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(a >= 1e10 ? 0 : 1) + "B";
    if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
    if (a >= 1e3) return (n / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "K";
    return String(Math.round(n));
  }
  const ms = (v) => (v === null || v === undefined ? "—" : v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 1) + " s" : Math.round(v) + " ms");
  function when(ts) {
    if (!ts) return "never";
    const d = new Date(ts * 1000);
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff >= 0 && diff < 60) return "just now";
    if (diff >= 0 && diff < 3600) return Math.floor(diff / 60) + " min ago";
    if (diff >= 0 && diff < 86400) return Math.floor(diff / 3600) + " h ago";
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function until(ts) {
    if (!ts) return "only when an admin resets it";
    const d = new Date(ts * 1000);
    const diff = (d.getTime() - Date.now()) / 1000;
    const at = d.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    if (diff <= 0) return at;
    const rel = diff < 3600 ? Math.ceil(diff / 60) + " min" : diff < 86400 ? Math.round(diff / 3600) + " h" : Math.round(diff / 86400) + " days";
    return `${at} (in ${rel})`;
  }
  const PERIOD = { day: "per day", week: "per week", month: "per month", none: "until reset" };
  const PERIODS = ["day", "week", "month", "none"];
  const host = (url) => { try { return new URL(url).host; } catch { return url; } };

  // ---------- API ----------
  class ApiError extends Error { constructor(status, message) { super(message); this.status = status; } }
  async function api(method, path, body) {
    const response = await fetch(path, {
      method, credentials: "same-origin",
      headers: { "X-Dcode-Manager": "1", ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await response.json(); } catch { /* empty body */ }
    if (!response.ok) {
      const message = (data && (typeof data.error === "string" ? data.error : data.error && data.error.message)) || `Request failed (${response.status})`;
      if (response.status === 401 && S.me && !path.startsWith("/api/login")) { S.me = null; render(); }
      throw new ApiError(response.status, message);
    }
    return data;
  }
  const enc = encodeURIComponent;

  // ---------- feedback ----------
  function toast(text, bad = false) {
    const el = h("div", { class: "toast" + (bad ? " bad" : ""), role: bad ? "alert" : "status", text });
    $("#toasts").append(el);
    setTimeout(() => el.remove(), bad ? 6000 : 3200);
  }
  async function attempt(fn, done) {
    try { const result = await fn(); if (done) toast(done); return result; } catch (err) { toast(err.message, true); return undefined; }
  }
  function ask(title, text, ok = "Confirm", danger = false) {
    return new Promise((resolve) => {
      const scrim = h("div", { class: "scrim", style: { zIndex: 30 } });
      const close = (value) => { scrim.remove(); box.remove(); document.removeEventListener("keydown", onKey); resolve(value); };
      const onKey = (e) => { if (e.key === "Escape") close(false); };
      const okBtn = h("button", { class: "btn " + (danger ? "danger" : "primary"), text: ok, onclick: () => close(true) });
      const box = h("div", { class: "dialog", role: "dialog", "aria-modal": "true" },
        h("h3", { text: title }), h("p", { text }),
        h("div", { class: "actions" }, h("button", { class: "btn", text: "Cancel", onclick: () => close(false) }), okBtn));
      scrim.addEventListener("click", () => close(false));
      document.addEventListener("keydown", onKey);
      document.body.append(scrim, box);
      okBtn.focus();
    });
  }
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast("Copied"); } catch { toast("Select the text and copy it", true); }
  }
  function codeLine(text) {
    return h("div", { class: "code" }, h("code", { text }), h("button", { class: "btn small ghost", title: "Copy", onclick: () => copy(text) }, icon("copy")));
  }

  // ---------- drawer ----------
  let drawerOpen = null;
  function drawer(title, body, footer, subtitle) {
    closeDrawer();
    const scrim = h("div", { class: "scrim" });
    const panel = h("aside", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-label": title },
      h("header", {}, h("div", { style: { minWidth: 0 } }, h("h2", { text: title }), subtitle ? h("div", { class: "muted small", text: subtitle }) : null),
        h("button", { class: "btn ghost small", title: "Close", onclick: closeDrawer }, icon("x"))),
      h("div", { class: "body" }, body), footer ? h("footer", {}, footer) : null);
    scrim.addEventListener("click", closeDrawer);
    const onKey = (e) => { if (e.key === "Escape" && !$(".dialog")) closeDrawer(); };
    document.addEventListener("keydown", onKey);
    document.body.append(scrim, panel);
    drawerOpen = { scrim, panel, onKey };
    const first = panel.querySelector("input, select, textarea");
    if (first) first.focus();
    return panel;
  }
  function closeDrawer() {
    if (!drawerOpen) return;
    drawerOpen.scrim.remove(); drawerOpen.panel.remove();
    document.removeEventListener("keydown", drawerOpen.onKey);
    drawerOpen = null;
  }

  // ---------- form fields ----------
  let fieldId = 0;
  function field(label, input, hint) {
    if (!input.id) input.id = "f" + (++fieldId);
    return h("label", { class: "field", for: input.id }, h("span", { text: label }), input, hint ? h("small", { text: hint }) : null);
  }
  const text = (value, attrs = {}) => h("input", { type: "text", value: value ?? "", ...attrs });
  const numberInput = (value, attrs = {}) => h("input", { type: "number", min: "0", value: value ?? "", ...attrs });
  function select(options, value, attrs = {}) {
    return h("select", attrs, options.map(([v, label]) => h("option", { value: v, selected: String(v) === String(value ?? "") ? true : null, text: label })));
  }
  const periodSelect = (value, blank) => select([...(blank ? [["", blank]] : []), ...PERIODS.map((p) => [p, PERIOD[p]])], value ?? "");
  function check(label, on, hint) {
    const input = h("input", { type: "checkbox", checked: on });
    const el = h("label", { class: "check" }, input, h("span", {}, h("span", { text: label }), hint ? h("div", { class: "muted small", text: hint }) : null));
    el.input = input;
    return el;
  }
  function toggle(on, onchange, label) {
    const input = h("input", { type: "checkbox", checked: on, "aria-label": label || "On", onchange: () => onchange(input.checked, input) });
    return h("label", { class: "switch", onclick: (e) => e.stopPropagation() }, input, h("i"));
  }
  const intOrNull = (v) => (String(v).trim() === "" ? null : parseInt(v, 10));
  function usageBar(used, limit) {
    if (limit === null || limit === undefined) return h("div", { class: "bar" }, h("i", { style: { width: "0%" } }));
    const share = limit ? Math.min(100, (100 * used) / limit) : 100;
    return h("div", { class: "bar" + (share >= 100 ? " full" : share >= 80 ? " warn" : "") }, h("i", { style: { width: share + "%" } }));
  }
  function table(head, rows, empty) {
    if (!rows.length) return h("div", { class: "table-wrap" }, h("div", { class: "empty" }, empty));
    return h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, head.map((c) => (typeof c === "string" ? h("th", { text: c }) : c)))),
      h("tbody", {}, rows)));
  }
  const td = (content, cls) => h("td", cls ? { class: cls } : {}, content);
  const emptyState = (title, line, action) => [h("b", { text: title }), line ? h("span", { text: line }) : null, action || null];

  // ---------- state, routing ----------
  const S = { me: null, info: null, route: "", refresh: null };
  const USER_PAGES = [["usage", "Usage", "usage"], ["keys", "API keys", "key"], ["connect", "Connect Dcode", "plug"]];
  const ADMIN_PAGES = [["monitoring", "Monitoring", "pulse"], ["models", "Models", "cube"], ["discovery", "Discovery", "radar"],
    ["users", "Users", "users"], ["requests", "Requests", "list"], ["settings", "Settings", "gear"]];
  const isAdmin = () => S.me && S.me.role === "admin";
  function currentRoute() {
    const name = (location.hash || "").replace(/^#\/?/, "").split("/")[0];
    const allowed = [...USER_PAGES, ...(isAdmin() ? ADMIN_PAGES : [])].map((p) => p[0]);
    return allowed.includes(name) ? name : isAdmin() ? "monitoring" : "usage";
  }
  window.addEventListener("hashchange", () => render());

  async function boot() {
    try {
      const me = await api("GET", "/api/me");
      S.me = me.user; S.info = me;
    } catch {
      S.me = null;
      try { S.info = await api("GET", "/api/info"); } catch { S.info = { name: "Dcode Manager" }; }
    }
    render();
  }

  function render() {
    clearInterval(S.refresh); S.refresh = null;
    closeDrawer();
    const root = $("#root");
    if (!S.me) { root.replaceChildren(signIn()); return; }
    S.route = currentRoute();
    const main = h("main", { class: "main", id: "main" });
    const shell = h("div", { class: "app" }, sidebar(), h("div", { style: { flex: 1, minWidth: 0, display: "flex", flexDirection: "column" } },
      h("div", { class: "mobile-bar" }, h("button", { class: "btn ghost small", "aria-label": "Menu", onclick: () => shell.classList.toggle("nav-open") }, icon("menu")),
        logo(), h("b", { text: (S.info && S.info.name) || "Dcode Manager" })), main));
    root.replaceChildren(shell);
    document.title = `${PAGE_TITLES[S.route]} · ${(S.info && S.info.name) || "Dcode Manager"}`;
    const page = h("div", { class: "page" });
    const loading = h("div", { class: "loading", text: "Loading…" });
    const holder = h("div", { class: "page" }, loading);
    main.append(holder);
    PAGES[S.route](page).then(() => holder.replaceWith(page)).catch((err) => {
      holder.replaceWith(page);
      page.append(h("div", { class: "note bad", text: err.message }));
    });
  }

  function sidebar() {
    const link = ([key, label, ic]) => h("a", { href: "#/" + key, class: S.route === key ? "on" : null, onclick: () => $(".app").classList.remove("nav-open") }, icon(ic), label);
    return h("nav", { class: "sidebar", "aria-label": "Pages" },
      h("div", { class: "brand" }, logo(), h("div", {}, h("b", { text: (S.info && S.info.name) || "Dcode Manager" }), h("span", { text: "Models · users · usage" }))),
      h("div", { class: "nav-label", text: "You" }), h("div", { class: "nav" }, USER_PAGES.map(link)),
      isAdmin() ? [h("div", { class: "nav-label", text: "Admin" }), h("div", { class: "nav" }, ADMIN_PAGES.map(link))] : null,
      h("div", { class: "me" }, h("div", { class: "avatar", text: (S.me.name || S.me.username).slice(0, 1) }),
        h("div", { class: "who" }, h("b", { text: S.me.name || S.me.username }), h("span", { text: S.me.role === "admin" ? "Admin" : "User" })),
        h("button", { class: "btn ghost small", title: "Change password", onclick: changePassword }, icon("key")),
        h("button", { class: "btn ghost small", title: "Sign out", onclick: signOut }, icon("out"))));
  }

  // ---------- sign in ----------
  function signIn() {
    const user = text("", { autocomplete: "username", required: true });
    const pass = h("input", { type: "password", autocomplete: "current-password", required: true });
    const error = h("div", { class: "note bad", hidden: true });
    const button = h("button", { class: "btn primary", type: "submit", text: "Sign in" });
    const form = h("form", { onsubmit: async (e) => {
      e.preventDefault();
      button.disabled = true; error.hidden = true;
      try {
        const result = await api("POST", "/api/login", { username: user.value, password: pass.value });
        S.me = result.user;
        const me = await api("GET", "/api/me");
        S.info = me;
        location.hash = "";
        render();
      } catch (err) { error.textContent = err.message; error.hidden = false; pass.select(); }
      button.disabled = false;
    } }, logo(), h("div", {}, h("h1", { text: (S.info && S.info.name) || "Dcode Manager" }), h("p", { text: "Sign in to see your usage and keys, or to manage models and users." })),
      error, field("Username", user), field("Password", pass), button);
    setTimeout(() => user.focus(), 0);
    return h("div", { class: "signin" }, form);
  }
  async function signOut() {
    await attempt(() => api("POST", "/api/logout"));
    S.me = null; location.hash = ""; render();
  }
  function changePassword() {
    const current = h("input", { type: "password", autocomplete: "current-password" });
    const next = h("input", { type: "password", autocomplete: "new-password" });
    const again = h("input", { type: "password", autocomplete: "new-password" });
    drawer("Change your password", [field("Current password", current), field("New password", next, "At least 8 characters."), field("New password again", again)],
      [h("button", { class: "btn", text: "Cancel", onclick: closeDrawer }), h("button", { class: "btn primary", text: "Change password", onclick: async () => {
        if (next.value !== again.value) { toast("The new passwords don't match", true); return; }
        const ok = await attempt(() => api("POST", "/api/me/password", { current: current.value, new: next.value }), "Password changed. Sign in again.");
        if (ok) { S.me = null; render(); }
      } })]);
  }

  // ---------- page: usage ----------
  async function usagePage(page) {
    const data = await api("GET", "/api/me/usage");
    const total = data.total;
    page.append(h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Your usage" }),
      h("p", { text: `Tokens you used, counted by the manager on every request. Days, weeks and months start in ${data.timezone}.` }))));
    page.append(h("div", { class: "card usage" },
      h("div", { class: "top" }, h("b", { text: "All models" }), h("span", { class: "badge", text: total.limit === null ? "No total limit" : PERIOD[total.period] })),
      h("div", { class: "big" }, short(total.used), h("small", { text: total.limit === null ? " tokens" : ` of ${short(total.limit)} tokens` })),
      usageBar(total.used, total.limit),
      h("div", { class: "foot" }, h("span", { text: total.limit === null ? "Counted since " + (total.since ? when(total.since) : "the start") : `${num(total.left)} left` }),
        h("span", { text: total.period === "none" && total.limit !== null ? "Resets when an admin resets it" : total.resets_at ? "Resets " + until(total.resets_at) : "" }))));
    if (!data.models.length) {
      page.append(h("div", { class: "card" }, h("div", { class: "empty" }, emptyState("No models yet", "An admin hasn't opened any model to you. Ask them for access."))));
      return;
    }
    page.append(h("div", { class: "usage-cards" }, data.models.map((m) => h("div", { class: "card usage" },
      h("div", { class: "top" }, h("b", { text: m.model, title: m.model }),
        h("span", {}, m.enabled ? null : h("span", { class: "badge amber", text: "Off" }), " ", h("span", { class: "badge", text: m.limit === null ? "No limit" : PERIOD[m.period] }))),
      h("div", { class: "big" }, short(m.used), h("small", { text: m.limit === null ? " tokens" : ` of ${short(m.limit)}` })),
      usageBar(m.used, m.limit),
      h("div", { class: "foot" }, h("span", { text: m.limit === null ? "" : `${num(m.left)} left` }),
        h("span", { text: m.limit === null ? "" : m.resets_at ? "Resets " + until(m.resets_at) : "Resets when an admin resets it" }))))));
  }

  // ---------- page: keys ----------
  async function keysPage(page) {
    const data = await api("GET", "/api/me/keys");
    page.append(h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "API keys" }),
      h("p", { text: "Each device that signs in with Dcode gets its own key. Make one by hand for scripts or other OpenAI-compatible tools." })),
      h("div", { class: "actions" }, h("button", { class: "btn primary", onclick: newKey }, icon("plus"), "New key"))));
    page.append(table(["Name", "Key", "Made", "Last used", "Used by", ""], data.keys.map((k) => h("tr", {},
      td(h("b", { text: k.name })), td(h("code", { text: k.prefix + "…" })), td(when(k.created)), td(when(k.last_used)),
      td(h("span", { class: "muted", text: k.client || "—" })),
      td(h("button", { class: "btn small danger", text: "Revoke", onclick: async () => {
        if (!(await ask("Revoke this key?", `Anything using “${k.name}” stops working at once. Dcode on that device needs \`dcode login\` again.`, "Revoke", true))) return;
        if (await attempt(() => api("DELETE", `/api/me/keys/${k.id}`), "Key revoked")) render();
      } }), "right"))), emptyState("No keys", "Sign in with Dcode (`dcode login`) or make one here.")));
  }
  function newKey() {
    const name = text("", { placeholder: "e.g. CI pipeline" });
    const body = h("div", { style: { display: "grid", gap: "14px" } }, field("Name", name, "So you know later what uses it."));
    const make = h("button", { class: "btn primary", text: "Make key", onclick: async () => {
      const made = await attempt(() => api("POST", "/api/me/keys", { name: name.value || "key" }));
      if (!made) return;
      body.replaceChildren(h("div", { class: "note", text: "Copy the key now: it won't be shown again." }),
        field("API key", h("div", { class: "code" }, h("code", { class: "secret", text: made.api_key }), h("button", { class: "btn small ghost", onclick: () => copy(made.api_key) }, icon("copy")))),
        field("Base URL (OpenAI-compatible)", codeLine(made.base_url)));
      make.remove();
    } });
    drawer("New API key", body, [h("button", { class: "btn", text: "Done", onclick: () => { closeDrawer(); render(); } }), make]);
  }

  // ---------- page: connect ----------
  async function connectPage(page) {
    const models = (await api("GET", "/api/me/models")).data;
    const origin = location.origin;
    page.append(h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Connect Dcode" }),
      h("p", { text: "Dcode signs in here and gets its own key and your models. Any other OpenAI-compatible tool can use a key from API keys." }))));
    page.append(h("div", { class: "cols" },
      h("div", { class: "card" }, h("h2", { text: "Dcode" }), h("div", { class: "steps" },
        h("div", {}, h("div", {}, h("h3", { text: "On a new install" }), h("p", { class: "muted", text: "Run dcode and choose “Sign in to a Dcode Manager”, then enter this address:" }), codeLine(origin))),
        h("div", {}, h("div", {}, h("h3", { text: "Or from the command line" }), codeLine(`dcode login ${origin} -u ${S.me.username}`))),
        h("div", {}, h("div", {}, h("h3", { text: "Check your usage from Dcode" }), codeLine("/usage"))))),
      h("div", { class: "card" }, h("h2", { text: "Other tools" }), h("dl", { class: "kv" },
        h("dt", { text: "Base URL" }), h("dd", {}, h("code", { text: origin + "/v1" })),
        h("dt", { text: "API key" }), h("dd", {}, h("a", { href: "#/keys", text: "Make one in API keys" })),
        h("dt", { text: "Endpoints" }), h("dd", { text: "/v1/models, /v1/chat/completions, /v1/embeddings and other model routes" })))));
    page.append(h("div", {}, h("h2", { style: { fontSize: "15px" }, text: "Models you can use" }),
      table(["Model", "Kind", "Family", "Context", "Max reply"], models.map((m) => h("tr", {},
        td(h("b", { text: m.id })), td(m.kind), td(m.family || "—"), td(m.context_length ? num(m.context_length) + " tokens" : "—", "num"),
        td(m.max_output_tokens ? num(m.max_output_tokens) : "—", "num"))), emptyState("No models open to you yet"))));
  }

  // PAGES, admin pages and boot are defined below.
  const PAGES = { usage: usagePage, keys: keysPage, connect: connectPage };
  const PAGE_TITLES = { usage: "Usage", keys: "API keys", connect: "Connect Dcode", monitoring: "Monitoring", models: "Models",
    discovery: "Discovery", users: "Users", requests: "Requests", settings: "Settings" };
  window.DM = { h, s, $, api, attempt, ask, toast, drawer, closeDrawer, field, text, numberInput, select, periodSelect, check,
    toggle, intOrNull, usageBar, table, td, emptyState, icon, num, short, ms, when, until, host, enc, PERIOD, PERIODS, PAGES, S,
    render, codeLine, copy, isAdmin };
  document.addEventListener("DOMContentLoaded", () => { if (window.DM_ADMIN) window.DM_ADMIN(window.DM); boot(); });
})();
