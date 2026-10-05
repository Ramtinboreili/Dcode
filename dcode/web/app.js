/* Dcode web UI — dependency-free, works fully offline. */
(() => {
  "use strict";

  // =====================================================================
  // Helpers
  // =====================================================================
  const $ = (sel, el = document) => el.querySelector(sel);
  const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function h(tag, attrs = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "html") el.innerHTML = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  async function api(method, path, body) {
    const res = await fetch(path, {
      method, credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Dcode": "1" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* empty body */ }
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }
  let toastTimer;
  function toast(msg, isError = false) {
    const t = $("#toast");
    t.replaceChildren(icon(isError ? "alert" : "check"), msg);
    t.className = "toast" + (isError ? " error" : "");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (t.hidden = true), 2600);
  }
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* private mode */ } },
  };
  const base = (p) => String(p || "").split("/").filter(Boolean).pop() || p || "";
  const ago = (iso) => {
    const t = Date.parse(iso);
    if (!t) return "";
    const s = (Date.now() - t) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  };
  // Mirror of dcode.config.slugify_thread_name — thread files are keyed by it.
  const threadId = (name) => String(name || "").trim().replace(/[^A-Za-z0-9_-]/g, "-").split("-").filter(Boolean).join("-");
  const prettyThread = (name) => /^\d{8}-\d{6}$/.test(name || "") ? "New chat" : name;
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); }
    catch (_) {
      const ta = h("textarea", { style: "position:fixed;opacity:0" }, text);
      document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove();
    }
    toast("Copied to clipboard");
  }

  // =====================================================================
  // Icons (inline SVG, lucide-style) and the Dcode logo
  // =====================================================================
  const P = {
    edit: '<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.4 2.6a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4Z"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    brain: '<path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/><path d="M12 5v13"/>',
    settings: '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    monitor: '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
    up: '<path d="M12 19V5M5 12l7-7 7 7" stroke-width="2.3"/>',
    stop: '<rect x="4" y="4" width="16" height="16" rx="3" fill="currentColor" stroke="none"/>',
    chevron: '<path d="m6 9 6 6 6-6"/>',
    right: '<path d="m9 6 6 6-6 6"/>',
    sidebar: '<rect x="3" y="3" width="18" height="18" rx="2.5"/><path d="M9 3v18"/>',
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    copy: '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
    file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    pencil: '<path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>',
    filePlus: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M12 12v6M9 15h6"/>',
    folder: '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.7-.9l-.8-1.2A2 2 0 0 0 7.9 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    terminal: '<path d="m4 17 6-6-6-6M12 19h8"/>',
    trash: '<path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
    git: '<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="8" r="2.5"/><path d="M6 8.5v7M18 10.5c0 4-6 3-10.5 6"/>',
    compass: '<circle cx="12" cy="12" r="10"/><path d="m16.2 7.8-2.1 6.3-6.3 2.1 2.1-6.3z"/>',
    code: '<path d="m16 18 6-6-6-6M8 6l-6 6 6 6"/>',
    eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
    flask: '<path d="M9 3h6M10 3v6L4.5 18.5A2 2 0 0 0 6.2 21.5h11.6a2 2 0 0 0 1.7-3L14 9V3"/><path d="M7 15h10"/>',
    shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/>',
    alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/>',
    info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
    bug: '<rect x="7" y="6" width="10" height="14" rx="5"/><path d="M12 20v-9M3 13h4M17 13h4M4 7l3 2M20 7l-3 2M4 19l3-2M20 19l-3-2M9 4l1.5 2M15 4l-1.5 2"/>',
    refresh: '<path d="M3 12a9 9 0 0 1 15.5-6.3L21 8M21 3v5h-5M21 12a9 9 0 0 1-15.5 6.3L3 16M3 21v-5h5"/>',
    review: '<path d="M11 6h10M11 12h10M11 18h10M3 6l1.5 1.5L7 5M3 12l1.5 1.5L7 11M3 18l1.5 1.5L7 17"/>',
    cpu: '<rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 9h6v6H9zM9 2v3M15 2v3M9 19v3M15 19v3M19 9h3M19 15h3M2 9h3M2 15h3"/>',
    users: '<circle cx="9" cy="8" r="4"/><path d="M2 21a7 7 0 0 1 14 0M16 3.1a4 4 0 0 1 0 7.8M22 21a7 7 0 0 0-5-6.7"/>',
    layers: '<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 17 10 5 10-5M2 12l10 5 10-5"/>',
    lock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    zap: '<path d="M13 2 3 14h9l-1 8 10-12h-9z"/>',
    globe: '<circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15 15 0 0 1 0 20 15 15 0 0 1 0-20"/>',
    palette: '<circle cx="12" cy="12" r="10"/><circle cx="8" cy="10" r="1.2" fill="currentColor"/><circle cx="12" cy="7.5" r="1.2" fill="currentColor"/><circle cx="16" cy="10" r="1.2" fill="currentColor"/><path d="M12 22a3 3 0 0 1 0-6h2a4 4 0 0 0 4-4"/>',
    bookmark: '<path d="M19 21l-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>',
    book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z"/><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/>',
    sparkles: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9z"/>',
    download: '<path d="M12 3v12M7 10l5 5 5-5M5 21h14"/>',
    page: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M7 6.5h.01M10 6.5h.01"/>',
    diagram: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/><path d="M6.5 10v4a3 3 0 0 0 3 3H14"/>',
    doc: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>',
    table: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M3 15h18M9 4v16"/>',
    link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
    external: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  };
  const svg = (name) => `<svg class="ic" viewBox="0 0 24 24" aria-hidden="true">${P[name] || P.info}</svg>`;
  const icon = (name, cls = "i") => h("span", { class: cls, html: svg(name) });
  // Fill static placeholders: <span class="i" data-icon="...">
  $$("[data-icon]").forEach((el) => (el.innerHTML = svg(el.dataset.icon)));
  [["#sb-close", "sidebar"], ["#sb-open", "sidebar"], ["#btn-review", "review"], ["#send", "up"], ["#stop", "stop"],
    ["#pane-copy", "copy"], ["#pane-download", "download"], ["#pane-close", "x"], ["#pane-link", "link"], ["#pane-page", "external"]]
    .forEach(([sel, name]) => ($(sel).innerHTML = svg(name)));
  $$("[data-close]").forEach((el) => { if (el.classList.contains("icon-btn")) el.innerHTML = svg("x"); });

  /** The Dcode mark: ╱────╲ │ D > │ ╲────╱ — a chamfered frame, a D and a prompt cursor. */
  function logo(cls = "") {
    const frame = "M11 2 H53 L62 11 V29 L53 38 H11 L2 29 V11 Z";
    return `<svg class="logo ${cls}" viewBox="0 0 64 40" aria-hidden="true">
      <path class="frame" d="${frame}"/>
      <path class="sweep" pathLength="200" d="${frame}"/>
      <path class="glyph" d="M14 12 H19.5 C25 12 28 15.6 28 20 C28 24.4 25 28 19.5 28 H14 Z"/>
      <path class="glyph" d="M33 14 L39 20 L33 26"/>
      <rect class="cursor" x="42" y="25" width="8" height="3.4" rx="1.7"/>
    </svg>`;
  }
  $("#brand-logo").innerHTML = logo("dyn");

  // =====================================================================
  // Markdown (minimal, safe — escapes first)
  // =====================================================================
  function inline(s) {
    return s
      .replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  }
  function md(src) {
    const lines = esc(src || "").split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      const fence = line.match(/^\s*```\s*([\w+#.-]*)/);
      if (fence) {
        const buf = [];
        i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
        i++;
        out.push(`<div class="codeblock"><div class="codeblock-head"><span>${fence[1] || "code"}</span>` +
          `<button type="button" data-copy><span class="i">${svg("copy")}</span>Copy</button></div>` +
          `<pre><code>${buf.join("\n")}</code></pre></div>`);
        continue;
      }
      const head = line.match(/^(#{1,4})\s+(.*)/);
      if (head) { const n = Math.min(3, head[1].length); out.push(`<h${n}>${inline(head[2])}</h${n}>`); i++; continue; }
      if (/^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
        const ordered = /^\s*\d+[.)]\s+/.test(line);
        const re = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*+]\s+/;
        const items = [];
        while (i < lines.length && re.test(lines[i])) items.push(`<li>${inline(lines[i++].replace(re, ""))}</li>`);
        out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
        continue;
      }
      if (/^&gt;\s?/.test(line)) {
        const buf = [];
        while (i < lines.length && /^&gt;\s?/.test(lines[i])) buf.push(lines[i++].replace(/^&gt;\s?/, ""));
        out.push(`<blockquote>${inline(buf.join("<br>"))}</blockquote>`);
        continue;
      }
      if (!line.trim()) { i++; continue; }
      const para = [];
      while (i < lines.length && lines[i].trim() && !/^(\s*```|#{1,4}\s|\s*[-*+]\s|\s*\d+[.)]\s|&gt;)/.test(lines[i])) para.push(lines[i++]);
      out.push(`<p>${inline(para.join("<br>"))}</p>`);
    }
    return out.join("");
  }
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-copy]");
    if (btn) copyText(btn.closest(".codeblock").querySelector("code").textContent);
  });

  // =====================================================================
  // Humanized tool steps
  // =====================================================================
  const short = (s, n = 60) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
  function parseArgs(a) { if (typeof a === "string") { try { return JSON.parse(a); } catch (_) { return {}; } } return a || {}; }
  /** [icon, present-tense label, past-tense label] for a tool call. Labels are trusted HTML (args escaped). */
  function describe(name, rawArgs) {
    const a = parseArgs(rawArgs);
    const c = (t) => `<code>${esc(short(t, 52))}</code>`;
    const where = !a.path || a.path === "." ? "the project" : c(a.path);
    const range = a.start_line ? ` · lines ${a.start_line}–${a.end_line || "…"}` : "";
    switch (name) {
      case "read_file": return ["file", `Reading ${c(a.path)}`, `Read ${c(a.path)}${range}`];
      case "edit_file": return ["pencil", `Editing ${c(a.path)}`, `Edited ${c(a.path)}`];
      case "write_file": return ["filePlus", `Writing ${c(a.path)}`, `Wrote ${c(a.path)}`];
      case "list_files": return ["folder", `Looking around ${where}`, `Looked around ${where}`];
      case "search_in_files": return ["search", `Searching for ${c(a.pattern)}`, `Searched for ${c(a.pattern)}`];
      case "run_command": return a.background
        ? ["terminal", `Starting ${c(a.command)} in the background`, `Started ${c(a.command)} in the background`]
        : ["terminal", `Running ${c(a.command)}`, `Ran ${c(a.command)}`];
      case "job_status": return ["terminal", `Checking job #${esc(a.id ?? "")}`, a.id ? `Checked job #${esc(a.id)}` : "Listed background jobs"];
      case "job_stop": return ["terminal", `Stopping job #${esc(a.id)}`, `Stopped job #${esc(a.id)}`];
      case "create_directory": return ["folder", `Creating ${c(a.path)}`, `Created folder ${c(a.path)}`];
      case "delete_file": return ["trash", `Deleting ${c(a.path)}`, `Deleted ${c(a.path)}`];
      case "git_status": return ["git", "Checking git status", "Checked git status"];
      case "git_diff": return ["git", "Looking at the changes", "Looked at the changes"];
      case "git_log": return ["git", "Reading recent commits", "Read recent commits"];
      case "git_commit": return ["git", `Committing “${esc(short(a.message, 50))}”`, `Committed “${esc(short(a.message, 50))}”`];
      case "memory_save": return ["bookmark", `Remembering ${c(a.name)}`, `Remembered ${c(a.name)}`];
      case "memory_search": return ["brain", `Searching memory for ${c(a.query)}`, `Searched memory for ${c(a.query)}`];
      case "artifact_save": return ["sparkles", `Creating “${esc(short(a.title, 50))}”`, `Created “${esc(short(a.title, 50))}”`];
      case "artifact_get": return ["sparkles", a.id ? `Opening artifact ${c(a.id)}` : "Listing artifacts", a.id ? `Opened artifact ${c(a.id)}` : "Listed artifacts"];
      case "skill_use": return ["zap", `Using the ${c(a.name)} skill`, `Used the ${c(a.name)} skill`];
      case "skill_save": return ["zap", `Writing skill ${c(a.name)}`, `Wrote skill ${c(a.name)}`];
      default: return ["zap", `Using ${c(name)}`, `Used ${c(name)}`];
    }
  }
  const MODES = {
    accept_edits: ["Accept edits", "Edit freely, ask before commands", "#2563EB"],
    auto: ["Auto", "Never ask", "#16A34A"],
    plan: ["Plan", "Read-only — propose a plan first", "#8B5CF6"],
    manual: ["Manual", "Ask before every edit and command", "#64748B"],
  };
  const ROLES = { explore: ["Explorer", "compass"], code: ["Coder", "code"], review: ["Reviewer", "eye"], test: ["Tester", "flask"] };
  const roleOf = (agentId) => String(agentId || "").replace(/-\d+$/, "");

  // =====================================================================
  // State
  // =====================================================================
  const S = {
    sessions: [], threads: [], mode: null, id: null, es: null, lastSeq: 0,
    busy: false, info: null, thread: null, ended: false, user: "",
    turn: null, activity: null, live: null, agents: {}, tools: {}, approvals: {}, hasContent: false, lastActions: null,
    cfg: null, tab: "general", skills: [], memories: [], models: null,
  };
  const T = () => $("#transcript");

  // =====================================================================
  // Theme
  // =====================================================================
  const THEMES = ["system", "light", "dark"];
  const themeIcon = { system: "monitor", light: "sun", dark: "moon" };
  function getTheme() { const t = store.get("dcode-theme"); return THEMES.includes(t) ? t : "system"; }
  function setTheme(t) {
    store.set("dcode-theme", t);
    if (t === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = t;
    $("#theme-toggle").innerHTML = svg(themeIcon[t]);
    $("#theme-toggle").title = `Theme: ${t} (click to change)`;
    if (!$("#settings-modal").hidden && S.tab === "general") renderSettings();
  }
  $("#theme-toggle").addEventListener("click", () => setTheme(THEMES[(THEMES.indexOf(getTheme()) + 1) % 3]));
  setTheme(getTheme());

  // =====================================================================
  // Sidebar
  // =====================================================================
  const app = $("#app");
  const isMobile = () => matchMedia("(max-width: 900px)").matches;
  function toggleSidebar(open) {
    if (isMobile()) app.classList.toggle("sb-open", open);
    else { app.classList.toggle("sb-collapsed", !open); store.set("dcode-sb", open ? "1" : "0"); }
  }
  if (store.get("dcode-sb") === "0") app.classList.add("sb-collapsed");
  $("#sb-close").addEventListener("click", () => toggleSidebar(false));
  $("#sb-open").addEventListener("click", () => toggleSidebar(true));
  $("#scrim").addEventListener("click", () => toggleSidebar(false));

  async function refreshSessions() {
    try { S.sessions = await api("GET", "/api/sessions"); } catch (_) { return; }
    $("#live-count").textContent = S.sessions.length;
    if (S.mode === "session" && S.id && !S.sessions.some((s) => s.id === S.id) && !S.ended) sessionEnded();
    const cur = S.sessions.find((s) => s.id === S.id);
    if (cur && S.mode === "session") { S.info = { ...S.info, ...cur, busy: S.busy }; renderHeader(); }
    renderSidebar();
    if (!S.mode && S.sessions.length && !location.hash.slice(1)) location.hash = "s=" + S.sessions[0].id;
  }
  async function refreshThreads() {
    try { S.threads = await api("GET", "/api/threads"); } catch (_) { return; }
    renderSidebar();
  }
  function bucket(iso) {
    const t = Date.parse(iso);
    if (!t) return "Older";
    const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diff = (day(new Date()) - day(new Date(t))) / 86400000;
    if (diff < 1) return "Today";
    if (diff < 2) return "Yesterday";
    if (diff < 7) return "Previous 7 days";
    if (diff < 30) return "Previous 30 days";
    return "Older";
  }
  function renderSidebar() {
    const live = $("#live-list");
    live.replaceChildren();
    if (!S.sessions.length) live.append(h("div", { class: "sb-empty" }, "No sessions running. Start one with ", h("code", {}, "dcode"), " in a terminal."));
    for (const s of S.sessions) {
      const dot = (s.approvals || []).length ? "live-dot wait" : s.busy ? "live-dot busy" : "live-dot";
      live.append(h("a", { class: "row" + (S.mode === "session" && S.id === s.id ? " active" : ""), href: "#s=" + s.id, title: s.cwd },
        h("span", { class: dot }),
        h("span", { class: "row-main" },
          h("div", { class: "row-title" }, prettyThread(s.thread_name) || s.id),
          h("div", { class: "row-sub" }, `${base(s.cwd)} · ${s.model || ""}`))));
    }
    const q = $("#thread-filter").value.trim().toLowerCase();
    const liveNames = new Set(S.sessions.map((s) => threadId(s.thread_name)));
    const groups = {};
    for (const t of S.threads) {
      if (liveNames.has(t.id)) continue; // already shown as a live session
      if (q && !`${t.name} ${t.cwd} ${t.model}`.toLowerCase().includes(q)) continue;
      (groups[bucket(t.updated_at)] ||= []).push(t);
    }
    const wrap = $("#thread-groups");
    wrap.replaceChildren();
    let any = false;
    for (const g of ["Today", "Yesterday", "Previous 7 days", "Previous 30 days", "Older"]) {
      if (!groups[g]) continue;
      any = true;
      wrap.append(h("div", { class: "sb-group" }, h("div", { class: "sb-label" }, g),
        ...groups[g].map((t) => {
          const del = h("button", { class: "icon-btn del", title: "Delete chat", "aria-label": "Delete chat", html: svg("trash"),
            onclick: async (e) => {
              e.preventDefault(); e.stopPropagation();
              if (!confirm(`Delete “${prettyThread(t.name)}”? This can't be undone.`)) return;
              try { await api("DELETE", "/api/threads/" + encodeURIComponent(t.id)); if (S.mode === "thread" && S.id === t.id) location.hash = ""; refreshThreads(); }
              catch (err) { toast(err.message, true); }
            } });
          return h("a", { class: "row" + (S.mode === "thread" && S.id === t.id ? " active" : ""), href: "#t=" + t.id, title: t.cwd },
            h("span", { class: "row-main" }, h("div", { class: "row-title" }, prettyThread(t.name)),
              h("div", { class: "row-sub" }, `${base(t.cwd) || "—"} · ${t.count} messages`)), del);
        })));
    }
    if (!any) wrap.append(h("div", { class: "sb-group" }, h("div", { class: "sb-label" }, "History"),
      h("div", { class: "sb-empty" }, q ? "No chats match your search." : "Your past chats will appear here.")));
  }
  $("#thread-filter").addEventListener("input", renderSidebar);

  // =====================================================================
  // Views
  // =====================================================================
  function route() {
    const hash = location.hash.slice(1);
    const params = new URLSearchParams(hash.includes("=") ? hash : "");
    app.classList.remove("sb-open");
    const [page, sub] = hash.split(":");
    if (["settings", "memory", "artifacts"].includes(page) || params.get("a")) {
      // Deep link to a dialog / artifact over the current (or first live) session.
      if (!S.mode && S.sessions.length) openSession(S.sessions[0].id);
      if (params.get("a")) return openArtifact(params.get("a"));
      return page === "settings" ? openSettings(sub) : page === "memory" ? openMemory() : openArtifacts();
    }
    if (params.get("s")) openSession(params.get("s"));
    else if (params.get("t")) openThread(params.get("t"));
    else if (S.sessions.length) location.hash = "s=" + S.sessions[0].id;
    else { closeStream(); Object.assign(S, { mode: null, id: null, info: null, thread: null }); resetTranscript(); renderHeader(); renderComposer(); renderSidebar(); }
  }
  function resetTranscript() {
    T().replaceChildren();
    Object.assign(S, { turn: null, activity: null, live: null, agents: {}, tools: {}, approvals: {}, hasContent: false, lastActions: null });
    renderWelcome();
  }
  function closeStream() { if (S.es) { S.es.close(); S.es = null; } }

  function openSession(id) {
    if (S.mode === "session" && S.id === id && S.es) return;
    closeStream();
    Object.assign(S, { mode: "session", id, lastSeq: 0, ended: false, busy: false, thread: null });
    S.info = S.sessions.find((s) => s.id === id) || null;
    $("#banner").hidden = true;
    resetTranscript();
    renderHeader(); renderComposer(); renderSidebar();
    connect();
    if (!isMobile()) setTimeout(() => $("#input").focus(), 50);
  }
  function connect() {
    const id = S.id;
    const es = new EventSource(`/api/s/${id}/events?since=${S.lastSeq}`);
    S.es = es;
    es.onmessage = (msg) => {
      let ev;
      try { ev = JSON.parse(msg.data); } catch (_) { return; }
      if (ev.seq <= S.lastSeq) return;
      S.lastSeq = ev.seq;
      render(ev, false);
    };
    es.onerror = () => {
      es.close();
      if (S.es !== es) return;
      S.es = null;
      setTimeout(() => { if (S.mode === "session" && S.id === id && !S.ended) connect(); }, 1500);
    };
  }
  function sessionEnded() {
    S.ended = true; S.busy = false;
    closeStream();
    finishTurn();
    const name = S.info?.thread_name;
    const banner = $("#banner");
    banner.replaceChildren(icon("info"), h("span", {}, "This session has ended. The conversation is saved. "),
      name ? h("a", { href: "#t=" + threadId(name) }, "Open saved chat") : "");
    banner.hidden = false;
    renderHeader(); renderComposer();
    refreshThreads();
  }
  async function openThread(id) {
    closeStream();
    Object.assign(S, { mode: "thread", id, ended: false, busy: false, info: null, thread: null });
    $("#banner").hidden = true;
    resetTranscript();
    renderSidebar();
    let data;
    try { data = await api("GET", "/api/threads/" + encodeURIComponent(id)); }
    catch (_) { T().replaceChildren(h("div", { class: "empty-state" }, icon("info"), "This chat couldn't be found.")); renderHeader(); renderComposer(); return; }
    S.thread = data;
    renderHeader();
    for (const item of data.items) render(item, true);
    finishTurn();
    renderComposer();
    thread.scrollTop = thread.scrollHeight;
  }

  // ---------- header ----------
  function ring(pct) {
    const c = 2 * Math.PI * 7;
    return `<svg viewBox="0 0 18 18"><circle class="track" cx="9" cy="9" r="7" fill="none" stroke-width="2.4"/>` +
      `<circle class="bar" cx="9" cy="9" r="7" fill="none" stroke-width="2.4" stroke-linecap="round" stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - pct / 100)}"/></svg>`;
  }
  function renderHeader() {
    const s = S.info;
    const live = S.mode === "session" && !S.ended;
    $("#model-name").textContent = s?.model || S.thread?.model || "Dcode";
    $("#model-btn").disabled = !live;
    $("#model-btn .chev").hidden = !live;
    const title = S.mode === "thread" ? (S.thread ? prettyThread(S.thread.name) : "") : s ? prettyThread(s.thread_name) : "";
    const cwd = s?.cwd || S.thread?.cwd;
    const tt = $("#topbar-title");
    tt.textContent = title ? `${title}${cwd ? "  ·  " + base(cwd) : ""}` : "";
    tt.title = live ? "Double-click to rename" : "";
    const waiting = live && (s?.approvals || []).length;
    const status = $("#status");
    status.hidden = !(live && (S.busy || waiting));
    status.className = "status" + (waiting ? " wait" : "");
    status.replaceChildren(h("span", { class: "dot" }), waiting ? "Needs your approval" : "Working");
    const ctx = $("#ctx");
    if (live && s?.context_window) {
      const pct = Math.min(100, Math.round((100 * (s.context_used || 0)) / s.context_window));
      ctx.hidden = false;
      ctx.innerHTML = ring(pct) + `<span>${pct}%</span>`;
      ctx.title = `Context used: ~${(s.context_used || 0).toLocaleString()} of ${s.context_window.toLocaleString()} tokens`;
    } else ctx.hidden = true;
    $("#btn-review").hidden = !(live && s?.git?.is_repo);
    $$(".logo.dyn").forEach((l) => l.classList.toggle("busy", !!(live && S.busy)));
  }
  $("#topbar-title").addEventListener("dblclick", async () => {
    if (S.mode !== "session" || S.ended) return;
    const name = prompt("Rename this chat", prettyThread(S.info?.thread_name) || "");
    if (name && name.trim()) {
      try { await api("POST", `/api/s/${S.id}/rename`, { name: name.trim() }); } catch (e) { toast(e.message, true); }
    }
  });
  $("#btn-review").addEventListener("click", () => sendText("/review"));

  // ---------- model menu ----------
  async function openModelMenu() {
    const menu = $("#model-menu");
    if (!menu.hidden) { menu.hidden = true; return; }
    menu.hidden = false;
    menu.replaceChildren(h("div", { class: "menu-label" }, h("span", { class: "spin" }), " Loading models…"));
    try { S.cfg = await api("GET", "/api/config"); } catch (_) { /* keep cached */ }
    let error = "";
    if (!S.models) {
      try { const r = await api("GET", "/api/models"); S.models = r.models || []; error = r.error || ""; }
      catch (e) { S.models = []; error = e.message; }
    }
    const current = S.info?.model;
    const locked = (S.cfg?.env_locked || []).includes("model");
    const all = [...new Set([current, ...S.models].filter(Boolean))];
    const pick = async (body) => {
      try { await api("POST", `/api/s/${S.id}/model`, body); Object.assign(S.info, body); renderHeader(); if (body.model) toast(`Now using ${body.model}`); }
      catch (e) { toast(e.message, true); }
    };
    const search = h("input", { placeholder: locked ? "Model is set by DCODE_MODEL" : `Search ${all.length} models or type a name…`, disabled: locked ? true : null });
    const list = h("div", { class: "menu-scroll" });
    const draw = () => {
      const q = search.value.trim().toLowerCase();
      const items = all.filter((m) => !q || m.toLowerCase().includes(q));
      list.replaceChildren(...items.slice(0, 200).map((m) => h("button", {
        class: "menu-item", onclick: () => { if (!locked && m !== current) pick({ model: m }); menu.hidden = true; },
      }, icon("cpu"), h("span", { class: "row-title" }, m), m === current ? icon("check", "i check") : "")));
      if (q && !all.some((m) => m.toLowerCase() === q) && !locked) {
        list.append(h("button", { class: "menu-item", onclick: () => { pick({ model: search.value.trim() }); menu.hidden = true; } },
          icon("plus"), h("span", { class: "row-title" }, `Use “${search.value.trim()}”`)));
      }
      if (!items.length && !q) list.append(h("div", { class: "menu-empty" }, error ? `Couldn't load the model list (${short(error, 70)}). Type a model name above.` : "No models found. Type a model name above."));
    };
    search.addEventListener("input", draw);
    search.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { const first = list.querySelector(".menu-item"); if (first) first.click(); }
    });
    draw();
    menu.replaceChildren(
      h("div", { class: "menu-search" }, icon("search"), search),
      list,
      h("div", { class: "menu-sep" }),
      h("div", { class: "menu-label" }, "Reasoning effort"),
      seg(["none", "low", "medium", "high"], S.info?.reasoning_effort || "medium", (v) => pick({ reasoning_effort: v })),
    );
    if (!locked) setTimeout(() => search.focus(), 30);
  }
  $("#model-btn").addEventListener("click", (e) => { e.stopPropagation(); if (S.mode === "session" && !S.ended) openModelMenu(); });
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#model-picker")) $("#model-menu").hidden = true;
    if (!e.target.closest(".mode-wrap")) $$(".mode-menu").forEach((m) => (m.hidden = true));
  });

  // ---------- welcome ----------
  function greeting() {
    const hr = new Date().getHours();
    const part = hr < 5 ? "Working late" : hr < 12 ? "Good morning" : hr < 18 ? "Good afternoon" : "Good evening";
    const name = S.user ? S.user.charAt(0).toUpperCase() + S.user.slice(1) : "";
    return name ? `${part}, ${name}` : part;
  }
  const SUGGESTIONS = [
    ["compass", "Explain this codebase", "A quick tour of the structure", "Give me a short tour of this codebase: the structure, main entry points, and how the pieces fit together."],
    ["bug", "Find likely bugs", "Spot risky code and edge cases", "Look through the code for likely bugs or risky patterns. List the top issues with file:line references."],
    ["flask", "Write tests", "Cover the most important logic", "Find the most important untested logic and write focused tests for it."],
    ["review", "Review my changes", "Check the current git diff", "/review"],
  ];
  function renderWelcome() {
    if (S.hasContent || S.mode === "thread") return;
    const live = S.mode === "session" && !S.ended;
    const folder = base(S.info?.cwd);
    T().replaceChildren(h("div", { class: "welcome", id: "welcome" },
      h("div", { html: logo("dyn" + (S.busy ? " busy" : "")) }).firstElementChild,
      h("h1", {}, live ? greeting() : "Welcome to Dcode"),
      live
        ? h("p", { html: `I'm ready to work in <code>${esc(folder || "your project")}</code>. What should we build today?` })
        : h("p", { html: "Start <code>dcode</code> in a terminal and your session shows up here. You can also open a past chat from the sidebar." }),
      live ? h("div", { class: "suggestions" }, ...SUGGESTIONS.map(([ic, title, sub, prompt]) =>
        h("button", { class: "suggestion", onclick: () => sendText(prompt) }, icon(ic), h("div", {}, h("b", {}, title), h("span", {}, sub))))) : ""));
  }
  function clearWelcome() { if (!S.hasContent) { S.hasContent = true; $("#welcome")?.remove(); } }

  // =====================================================================
  // Event rendering
  // =====================================================================
  const thread = $("#thread");
  function nearBottom() { return thread.scrollHeight - thread.scrollTop - thread.clientHeight < 160; }
  function scrollDown(force) { if (force || nearBottom()) thread.scrollTop = thread.scrollHeight; }

  function ensureTurn() {
    if (S.turn) return S.turn;
    clearWelcome();
    const body = h("div", { class: "turn-body" });
    const el = h("div", { class: "turn assistant" }, h("div", { class: "turn-avatar", html: logo("dyn" + (S.busy ? " busy" : "")) }), body);
    T().append(el);
    S.turn = { el, body };
    return S.turn;
  }
  function finishTurn() {
    closeActivity();
    removeThinking();
    if (S.live) { S.live.classList.remove("caret"); S.live = null; }
    // Only the newest assistant avatar stays "alive".
    const avatars = $$(".turn-avatar .logo", T());
    avatars.forEach((l, i) => { if (i < avatars.length - 1 || !S.busy) l.classList.remove("busy"); if (i < avatars.length - 1) l.classList.remove("dyn"); });
  }
  function showThinking() {
    if (!S.busy || S.mode !== "session") return;
    const turn = ensureTurn();
    if (!turn.body.querySelector(".thinking")) turn.body.append(h("div", { class: "thinking", "aria-label": "Thinking" }, h("i"), h("i"), h("i")));
  }
  function removeThinking() { $$(".thinking", T()).forEach((n) => n.remove()); }
  function closeActivity() {
    const a = S.activity;
    if (!a) return;
    a.done = true;
    updateActivity(a);
    S.activity = null;
  }
  function updateActivity(a) {
    const n = a.steps.length;
    // A step can outlive its group (e.g. a command waiting for approval).
    const running = a.steps.some((s) => s.running);
    const label = running
      ? (a.steps.filter((s) => s.running).pop()?.present || "Working") + "…"
      : n === 1 ? a.steps[0].past : `Worked through ${n} steps`;
    a.summary.replaceChildren(running ? h("span", { class: "spin" }) : icon("right", "i chev"), h("span", { html: label }));
  }
  function stepRow(name, args, history) {
    const [ic, present, past] = describe(name, args);
    const pre = h("pre", {}, "Waiting for output…");
    const res = h("span", { class: "res" });
    const el = h("details", { class: "step" + (history ? "" : " running") },
      h("summary", {}, history ? icon(ic, "si") : h("span", { class: "si", html: '<span class="spin"></span>' }), h("span", { class: "label", html: history ? past : present }), res), pre);
    return { el, pre, res, ic, present, past, running: !history };
  }
  function finishStep(step, output) {
    step.running = false;
    step.el.classList.remove("running");
    step.el.querySelector(".si").replaceWith(icon(step.ic, "si"));
    step.el.querySelector(".label").innerHTML = step.past;
    const out = output || "(no output)";
    step.pre.textContent = out;
    const lines = out.split("\n").length;
    step.res.textContent = /^(Blocked|Tool error|Command cancelled|Deletion cancelled|Git commit cancelled|Interrupted)/.test(out) ? "skipped" : lines > 1 ? `${lines} lines` : "";
  }

  function render(ev, history) {
    const agent = ev.agent || "main";
    const sub = agent !== "main" ? S.agents[agent] : null;
    switch (ev.type) {
      case "user_message": {
        finishTurn();
        S.turn = null;
        clearWelcome();
        T().append(h("div", { class: "turn user" }, h("div", { class: "bubble" }, ev.text),
          ev.source === "cli" ? h("span", { class: "via" }, "sent from the terminal") : ""));
        if (!history) { showThinking(); scrollDown(true); }
        break;
      }
      case "assistant_delta": {
        if (agent !== "main") break;
        const turn = ensureTurn();
        removeThinking();
        closeActivity();
        if (!S.live) { S.live = h("div", { class: "md live caret" }); turn.body.append(S.live); }
        S.live.textContent += ev.text;
        scrollDown();
        break;
      }
      case "assistant_note":
        if (agent !== "main") break;
        if (S.live) { S.live.remove(); S.live = null; }
        removeThinking();
        closeActivity();
        ensureTurn().body.append(h("div", { class: "md note", html: md(ev.text) }));
        break;
      case "assistant_message": {
        if (agent !== "main") break;
        if (S.live) { S.live.remove(); S.live = null; }
        removeThinking();
        closeActivity();
        if (!ev.text) break;
        const turn = ensureTurn();
        const stat = h("span", { class: "stat" });
        const actions = h("div", { class: "msg-actions" },
          h("button", { class: "icon-btn", title: "Copy", "aria-label": "Copy", html: svg("copy"), onclick: () => copyText(ev.text) }), stat);
        turn.body.append(h("div", { class: "md", html: md(ev.text) }), actions);
        S.lastActions = stat;
        scrollDown();
        break;
      }
      case "tool_call": {
        if (ev.name === "delegate" || ev.name === "todo_write") break; // shown as a helper / checklist card instead
        if (S.live && agent === "main") { S.live.remove(); S.live = null; }
        const step = stepRow(ev.name, ev.args, history);
        if (ev.id) S.tools[agent + "/" + ev.id] = step;
        if (sub) { sub.steps.append(step.el); sub.count++; break; }
        const turn = ensureTurn();
        removeThinking();
        if (!S.activity) {
          const summary = h("summary", {});
          const steps = h("div", { class: "steps" });
          const el = h("details", { class: "activity" }, summary, steps);
          turn.body.append(el);
          S.activity = { el, summary, list: steps, steps: [], done: false };
        }
        S.activity.steps.push(step);
        step.activity = S.activity;
        S.activity.list.append(step.el);
        updateActivity(S.activity);
        scrollDown();
        break;
      }
      case "tool_result": {
        const step = ev.id && S.tools[agent + "/" + ev.id];
        if (!step) break;
        finishStep(step, ev.output);
        if (step.activity) updateActivity(step.activity);
        if (ev.name === "artifact_save") {
          const m = /Saved artifact '(.*)' \(id: ([a-z0-9-]+), kind: (\w+), version (\d+)/.exec(ev.output || "");
          if (m) {
            const existing = S.cards && S.cards[m[2]];
            if (existing && existing.isConnected) {  // same artifact saved again: refresh its card, no new one
              existing.querySelector(".art-sub").textContent = `${kindLabel(m[3])} · v${m[4]} · click to open`;
            } else {
              (S.cards ||= {})[m[2]] = artifactCard({ title: m[1], id: m[2], kind: m[3], version: m[4] }, sub ? sub.body : ensureTurn().body);
            }
          }
        }
        break;
      }
      case "agent_start": {
        const turn = ensureTurn();
        removeThinking();
        closeActivity();
        const custom = String(ev.role || roleOf(ev.agent) || "Helper");
        const [roleName, ic] = ROLES[ev.role] || ROLES[roleOf(ev.agent)] || [custom.charAt(0).toUpperCase() + custom.slice(1).replace(/[-_]/g, " "), "users"];
        const state = h("span", { class: "agent-state" }, h("span", { class: "spin" }), "Working");
        const steps = h("div", { class: "steps" });
        const body = h("div", { class: "agent-body" }, steps);
        const title = h("div", { class: "agent-title" }, `${roleName} is on it`);
        const card = h("div", { class: "agent collapsed" },
          h("div", { class: "agent-head", role: "button", tabindex: "0", onclick: () => card.classList.toggle("collapsed") },
            h("span", { class: "agent-badge" }, icon(ic)),
            h("div", { class: "agent-meta" }, title, h("div", { class: "agent-task", title: ev.task }, ev.task)),
            state),
          body);
        S.agents[ev.agent] = { card, body, steps, state, title, count: 0, roleName };
        turn.body.append(card);
        scrollDown();
        break;
      }
      case "agent_end": {
        const a = S.agents[ev.agent];
        if (!a) break;
        a.title.textContent = ev.ok ? `${a.roleName} finished` : `${a.roleName} stopped`;
        a.state.className = "agent-state" + (ev.ok ? " ok" : "");
        a.state.replaceChildren(icon(ev.ok ? "check" : "info"), a.count ? `${a.count} step${a.count > 1 ? "s" : ""}` : ev.ok ? "Done" : "Stopped");
        if (ev.report && ev.report !== "interrupted") a.body.append(h("div", { class: "report" }, h("div", { class: "md", html: md(ev.report) })));
        if (!history) showThinking();
        break;
      }
      case "approval_request": {
        if (history) break;
        const turn = ensureTurn();
        removeThinking();
        closeActivity();
        const what = { run_command: "run a command", delete_file: "delete a file", git_commit: "make a git commit" }[ev.kind] || ev.kind;
        const who = agent !== "main" ? ` for the ${(ROLES[roleOf(agent)] || ["helper"])[0]}` : "";
        const verdict = h("span", { class: "verdict" });
        const decide = async (approved, scope) => {
          try { await api("POST", `/api/s/${S.id}/approve`, { id: ev.id, approved, scope }); } catch (e) { toast(e.message, true); }
        };
        const allow = h("button", { class: "btn primary sm", onclick: () => decide(true) }, icon("check"), "Allow");
        const always = h("button", { class: "btn sm", onclick: () => decide(true, "session") }, "Allow for this session");
        const deny = h("button", { class: "btn sm ghost", onclick: () => decide(false) }, "Deny");
        const subline = h("div", { class: "approval-sub" }, "Nothing runs until you allow it. You can also answer in the terminal.");
        const card = h("div", { class: "approval" },
          h("div", { class: "approval-head" }, icon("shield"), `Dcode wants to ${what}${who}`),
          subline, h("pre", {}, ev.detail),
          h("div", { class: "approval-actions" }, allow, always, deny, verdict));
        S.approvals[ev.id] = { card, allow, always, deny, verdict, subline };
        turn.body.append(card);
        if (S.info) { S.info.approvals = [...(S.info.approvals || []), ev]; renderHeader(); }
        scrollDown(true);
        break;
      }
      case "approval_resolved": {
        if (S.info) { S.info.approvals = (S.info.approvals || []).filter((x) => x.id !== ev.id); renderHeader(); }
        const a = S.approvals[ev.id];
        if (!a) break;
        a.card.classList.add("done");
        a.allow.remove(); a.always.remove(); a.deny.remove(); a.subline.remove();
        a.verdict.className = "verdict" + (ev.approved ? " ok" : "");
        const by = ev.by === "cli" ? " in the terminal" : ev.by === "interrupt" ? " (task stopped)" : "";
        a.verdict.replaceChildren(icon(ev.approved ? "check" : "x"), `${ev.approved ? "Allowed" : "Denied"}${by}`);
        if (!history && ev.approved) showThinking();
        break;
      }
      case "status":
        S.busy = !!ev.busy;
        if (S.info) S.info.busy = S.busy;
        if (!S.busy) finishTurn();
        else if (!history) showThinking();
        renderHeader(); renderComposer();
        break;
      case "usage": {
        if (agent !== "main") break;
        const pct = ev.context_window ? Math.min(100, Math.round((100 * ev.context_used) / ev.context_window)) : 0;
        if (S.lastActions) {
          const bits = [`${ev.elapsed}s`];
          const files = ev.files_changed || [];
          if (files.length) bits.push(`${files.length} file${files.length > 1 ? "s" : ""} changed`);
          if (ev.commands) bits.push(`${ev.commands} command${ev.commands > 1 ? "s" : ""}${ev.commands_failed ? ` (${ev.commands_failed} failed)` : ""}`);
          if (ev.helpers) bits.push(`${ev.helpers} helper${ev.helpers > 1 ? "s" : ""}`);
          bits.push(`${pct}% context`);
          S.lastActions.textContent = bits.join(" · ");
          if (files.length) S.lastActions.title = files.join("\n");
          if (!files.length) S.lastActions.title = `${(ev.prompt || 0).toLocaleString()} tokens in · ${(ev.completion || 0).toLocaleString()} out · ${ev.model}`;
        }
        if (S.info) { S.info.context_used = ev.context_used; renderHeader(); }
        break;
      }
      case "compaction":
        if (agent === "main") eventLine("", "refresh", `Tidied up the conversation to save space${ev.summarized ? " (older messages summarized)" : ""}`);
        break;
      case "mode":
        if (S.info) { S.info.mode = ev.mode; renderComposer(); }
        break;
      case "plan_ready": {
        if (history) break;
        const turn = ensureTurn();
        const actions = h("div", { class: "approval-actions" },
          h("button", { class: "btn primary sm", onclick: () => decidePlan("accept_edits") }, icon("check"), "Implement it"),
          h("button", { class: "btn sm", onclick: () => decidePlan("manual") }, "Implement, ask before edits"),
          h("button", { class: "btn sm ghost", onclick: () => decidePlan("keep") }, "Keep planning"));
        const card = h("div", { class: "approval plan-card" },
          h("div", { class: "approval-head" }, icon("review"), "Ready to implement this plan?"),
          h("div", { class: "approval-sub" }, "Dcode was in plan mode and hasn't changed anything yet."), actions);
        S.planCard = { card, actions };
        turn.body.append(card);
        scrollDown(true);
        break;
      }
      case "plan_resolved":
        if (S.planCard) {
          S.planCard.card.classList.add("done");
          S.planCard.actions.replaceChildren(h("span", { class: "verdict ok" }, icon(ev.decision === "keep" ? "info" : "check"),
            ev.decision === "keep" ? "Kept planning" : "Approved — implementing"));
          S.planCard = null;
        }
        break;
      case "job_started":
        eventLine("", "terminal", `Background job #${ev.id} started: ${short(ev.command, 90)}`);
        break;
      case "job_exited":
        eventLine(String(ev.state).startsWith("exited (0)") || ev.state === "stopped" ? "success" : "warning", "terminal",
          `Background job #${ev.id} ${ev.state}: ${short(ev.command, 80)}`);
        break;
      case "undo":
        eventLine("success", "refresh", `Undid “${ev.label}”: restored ${(ev.files || []).join(", ") || "nothing"}`);
        break;
      case "check":
        eventLine("warning", "alert", `Check found ${ev.count > 1 ? ev.count + " problems" : "a problem"} in ${ev.path} — Dcode will fix ${ev.count > 1 ? "them" : "it"}: ${short(String(ev.problems || "").split("\n")[0], 100)}`);
        break;
      case "todos": {
        if (agent !== "main") break;
        const items = ev.items || [];
        const done = items.filter((i) => i.status === "done").length;
        const card = h("div", { class: "todos" },
          h("div", { class: "todos-head" }, icon("check"), "Checklist", h("span", { class: "meta" }, `${done}/${items.length} done`)),
          ...items.map((i) => h("div", { class: "todo " + i.status }, h("span", { class: "todo-box" }, i.status === "done" ? icon("check") : ""), h("span", {}, i.text))));
        if (S.todoCard && S.todoCard.isConnected && S.turn && S.turn.body.contains(S.todoCard)) S.todoCard.replaceWith(card);
        else { closeActivity(); ensureTurn().body.append(card); }
        S.todoCard = card;
        scrollDown();
        break;
      }
      case "mentions": {
        const files = [...(ev.attached || [])];
        if (files.length) eventLine("", "file", `Attached ${files.join(", ")}`);
        break;
      }
      case "skill_saved":
        eventLine("success", "zap", `New skill saved: ${ev.name}`);
        break;
      case "approval_auto":
        eventLine("", "shield", `Allowed automatically for this session: ${short(ev.detail, 80)}`);
        break;
      case "artifact_saved":
        refreshArtifactCount();
        if (S.pane && S.pane.id === ev.id) openArtifact(ev.id, S.pane.tab);
        break;
      case "memory_saved":
        eventLine("success", "bookmark", `Saved to memory: ${ev.name}`);
        break;
      case "notice":
        if (/Recovered tool call/.test(ev.text)) break; // internal detail
        eventLine(ev.level, ev.level === "warning" ? "alert" : "info", ev.text.replace(/^Interrupt requested.*/, "Stopping…"));
        break;
      case "error":
        eventLine("error", "alert", ev.text);
        break;
      case "config":
        if (S.info) { Object.assign(S.info, { model: ev.model, reasoning_effort: ev.reasoning_effort }); if (ev.context_window) S.info.context_window = ev.context_window; renderHeader(); }
        S.models = null;
        break;
      case "thread":
        if (S.info) { S.info.thread_name = ev.name; renderHeader(); }
        if (ev.action === "new" || ev.action === "resume") {
          resetTranscript();
          if (ev.items) { for (const item of ev.items) render(item, true); finishTurn(); S.turn = null; }
          scrollDown(true);
        }
        refreshThreads(); refreshSessions();
        break;
      default:
        break;
    }
  }
  function eventLine(cls, ic, text) {
    closeActivity();
    (S.turn ? S.turn.body : T()).append(h("div", { class: "event " + (cls || "") }, icon(ic), h("span", {}, text)));
    scrollDown();
  }

  // =====================================================================
  // Composer
  // =====================================================================
  const input = $("#input");
  function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 240) + "px"; renderSend(); }
  function renderSend() {
    const live = S.mode === "session" && !S.ended;
    const hasText = !!input.value.trim();
    $("#stop").hidden = !(live && S.busy && !hasText);
    $("#send").hidden = !$("#stop").hidden;
    $("#send").disabled = !(live && hasText);
  }
  function renderComposer() {
    const live = S.mode === "session" && !S.ended;
    $("#composer").hidden = !live;
    $("#disclaimer").hidden = !live;
    const bar = $("#resume-bar");
    bar.hidden = live || S.mode !== "thread" || !S.thread;
    if (!bar.hidden) {
      bar.replaceChildren();
      if (!S.sessions.length) bar.append(icon("info"), h("span", { class: "grow" }, "This is a saved chat. Start ", h("code", {}, "dcode"), " in a terminal to continue it."));
      else {
        const sel = h("select", { class: "select" }, ...S.sessions.map((s) => h("option", { value: s.id }, `${prettyThread(s.thread_name)} — ${base(s.cwd)}`)));
        bar.append(icon("refresh"), h("span", { class: "grow" }, "Pick up where you left off in a live session"), sel,
          h("button", { class: "btn primary sm", onclick: async () => {
            try { await api("POST", `/api/s/${sel.value}/resume`, { thread_id: S.id }); location.hash = "s=" + sel.value; }
            catch (e) { toast(e.message.includes("busy") ? "That session is busy right now." : e.message, true); }
          } }, "Continue"));
      }
    }
    const left = $("#composer-left");
    left.replaceChildren();
    const mode = S.info?.mode || "accept_edits";
    $("#composer").style.setProperty("--mode", (MODES[mode] || MODES.accept_edits)[2]);
    $("#composer").dataset.mode = mode;
    if (live) {
      const wrap = h("div", { class: "mode-wrap" });
      const menu = h("div", { class: "menu mode-menu", hidden: true },
        h("div", { class: "menu-label" }, "Mode"),
        ...Object.entries(MODES).map(([key, [label, desc, color]]) => h("button", {
          type: "button", class: "menu-item", onclick: async () => {
            menu.hidden = true;
            try { await api("POST", `/api/s/${S.id}/mode`, { mode: key }); S.info.mode = key; renderComposer(); }
            catch (e) { toast(e.message, true); }
          } }, h("span", { class: "mode-dot", style: `background:${color}` }),
          h("span", { class: "row-main" }, h("div", {}, label), h("div", { class: "meta" }, desc)),
          key === mode ? icon("check", "i check") : "")));
      const chip = h("button", { type: "button", class: "chip-btn mode-chip", title: "Change mode (Shift+Tab in the terminal)",
        onclick: (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; } },
        h("span", { class: "mode-dot", style: `background:${(MODES[mode] || MODES.accept_edits)[2]}` }), (MODES[mode] || MODES.accept_edits)[0]);
      wrap.append(chip, menu);
      left.append(wrap);
    }
    if (live && S.info?.cwd) left.append(h("span", { class: "chip-btn", title: S.info.cwd }, icon("folder"), base(S.info.cwd)));
    if (live && S.info?.git?.is_repo) left.append(h("button", { type: "button", class: "chip-btn", onclick: () => sendText("/review") }, icon("review"), "Review changes"));
    input.placeholder = S.busy ? "Dcode is working… you can queue another message" : "Message Dcode…";
    renderSend();
  }
  input.addEventListener("input", autosize);

  // ---------- @file and /command suggestions ----------
  const suggest = $("#suggest");
  const WEB_COMMANDS = [["/review", "Review the uncommitted changes"], ["/compact", "Summarize the chat to free context"],
    ["/undo", "Undo the file changes from the last task"], ["/mode", "Switch mode: manual, accept, auto, plan"],
    ["/new", "Start a new chat"], ["/rename", "Rename this chat"], ["/init", "Write AGENTS.md for this project"]];
  const sug = { items: [], sel: 0, token: "", kind: "", seq: 0 };
  function hideSuggest() { suggest.hidden = true; sug.items = []; }
  async function updateSuggest() {
    if (S.mode !== "session" || S.ended) return hideSuggest();
    const before = input.value.slice(0, input.selectionStart);
    const seq = ++sug.seq;
    if (/^\/\S*$/.test(before) && before === input.value) {
      const custom = ((S.info && S.info.commands) || []).map((c) => ["/" + c.name, c.description || "Custom command"]);
      sug.kind = "command"; sug.token = before;
      return showSuggest([...WEB_COMMANDS, ...custom].filter(([c]) => c.startsWith(before)).slice(0, 10));
    }
    const m = /(?:^|\s)@(\S*)$/.exec(before);
    if (!m) return hideSuggest();
    sug.kind = "file"; sug.token = m[1];
    try {
      const r = await api("GET", `/api/s/${S.id}/files?q=${encodeURIComponent(m[1])}`);
      const atStart = input.value.trimStart() === before.trimStart() && before.trimStart() === "@" + m[1];
      const helpers = atStart ? (r.helpers || []).map((a) => ["@" + a.name, "helper · " + short(a.description, 50)]) : [];
      if (seq === sug.seq) showSuggest([...helpers, ...(r.files || []).map((f) => ["@" + f, f.endsWith("/") ? "folder" : "file"])].slice(0, 10));
    } catch (_) { hideSuggest(); }
  }
  function showSuggest(items) {
    sug.items = items; sug.sel = 0;
    if (!items.length) return hideSuggest();
    drawSuggest();
    suggest.hidden = false;
  }
  function drawSuggest() {
    suggest.replaceChildren(...sug.items.map(([label, meta], i) => h("button", {
      type: "button", role: "option", class: "menu-item" + (i === sug.sel ? " active" : ""),
      onmousedown: (e) => { e.preventDefault(); sug.sel = i; pickSuggest(); } },
      icon(sug.kind === "file" ? (meta === "folder" ? "folder" : meta.startsWith("helper") ? "users" : "file") : "zap"),
      h("span", { class: "row-main" }, label), h("span", { class: "meta" }, meta))));
    suggest.children[sug.sel]?.scrollIntoView({ block: "nearest" });
  }
  function pickSuggest() {
    const [label] = sug.items[sug.sel] || [];
    if (!label) return;
    const caret = input.selectionStart;
    const before = input.value.slice(0, caret), after = input.value.slice(caret);
    const head = sug.kind === "command" ? label + " "
      : before.slice(0, before.length - sug.token.length - 1) + label + (label.endsWith("/") ? "" : " ");
    input.value = head + after;
    input.selectionStart = input.selectionEnd = head.length;
    hideSuggest(); autosize(); input.focus();
    if (label.endsWith("/")) updateSuggest();  // keep drilling into the folder
  }
  input.addEventListener("input", updateSuggest);
  input.addEventListener("click", updateSuggest);
  input.addEventListener("blur", () => setTimeout(hideSuggest, 120));

  input.addEventListener("keydown", (e) => {
    if (!suggest.hidden && sug.items.length) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        sug.sel = (sug.sel + (e.key === "ArrowDown" ? 1 : -1) + sug.items.length) % sug.items.length;
        return drawSuggest();
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey && !e.isComposing && sug.kind === "file")) { e.preventDefault(); return pickSuggest(); }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); return hideSuggest(); }
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing && sug.kind === "command" && input.value.trim() !== sug.items[sug.sel][0]) {
        e.preventDefault(); pickSuggest(); input.value = input.value.trim(); hideSuggest(); return $("#composer").requestSubmit();
      }
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); hideSuggest(); $("#composer").requestSubmit(); }
  });
  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); const t = input.value.trim(); if (t) { input.value = ""; autosize(); sendText(t); } });
  $("#stop").addEventListener("click", () => api("POST", `/api/s/${S.id}/interrupt`, {}).catch((e) => toast(e.message, true)));
  async function decidePlan(decision) {
    try { await api("POST", `/api/s/${S.id}/plan`, { decision }); } catch (e) { toast(e.message, true); }
  }
  async function sendText(text) {
    if (S.mode !== "session" || S.ended) return;
    try {
      const [cmd, ...rest] = text.split(" ");
      const arg = rest.join(" ").trim();
      if (cmd === "/compact") return void (await api("POST", `/api/s/${S.id}/compact`, { focus: arg }));
      if (cmd === "/init") return void (await api("POST", `/api/s/${S.id}/init`, {}));
      if (cmd === "/undo") {
        const r = await api("POST", `/api/s/${S.id}/undo`, {});
        return toast(r.ok ? `Restored ${r.files.length} file(s)` : "Nothing to undo", !r.ok);
      }
      if (cmd === "/mode") {
        const key = arg.toLowerCase().replace(/[\s-]+/g, "_");
        await api("POST", `/api/s/${S.id}/mode`, { mode: key === "accept" ? "accept_edits" : key });
        return;
      }
      if (text === "/new" || text === "/reset") await api("POST", `/api/s/${S.id}/new`, {});
      else if (text.startsWith("/rename ")) await api("POST", `/api/s/${S.id}/rename`, { name: text.slice(8) });
      else if (text.startsWith("/") && text !== "/review") toast("Here you can use /review, /init, /compact, /undo, /mode, /new, /rename and your own commands. Everything else is in Settings.");
      else await api("POST", `/api/s/${S.id}/message`, { text });
    } catch (e) { toast(e.message, true); input.value = text; autosize(); }
  }
  async function newChat() {
    const id = S.mode === "session" && !S.ended ? S.id : S.sessions[0]?.id;
    if (!id) return toast("Start dcode in a terminal first", true);
    try { await api("POST", `/api/s/${id}/new`, {}); if (S.id !== id || S.mode !== "session") location.hash = "s=" + id; input.focus(); }
    catch (e) { toast(e.message.includes("busy") ? "Dcode is busy. Stop the current task first." : e.message, true); }
  }
  $("#new-chat").addEventListener("click", newChat);
  $("#brand").addEventListener("click", (e) => { e.preventDefault(); if (S.sessions[0]) location.hash = "s=" + (S.mode === "session" && !S.ended ? S.id : S.sessions[0].id); });

  // =====================================================================
  // Dialogs
  // =====================================================================
  function openModal(id) { $(id).hidden = false; }
  function closeModals() { $$(".modal").forEach((m) => (m.hidden = true)); }
  $$(".modal").forEach((m) => {
    m.addEventListener("mousedown", (e) => { if (e.target === m) m.hidden = true; });
    $$("[data-close]", m).forEach((b) => b.addEventListener("click", () => (m.hidden = true)));
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!$$(".modal").every((m) => m.hidden) || !$("#model-menu").hidden) { closeModals(); $("#model-menu").hidden = true; }
      else if (S.pane) closePane();
    }
    if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "o") { e.preventDefault(); newChat(); }
    if (e.key === "/" && document.activeElement === document.body && !$("#composer").hidden) { e.preventDefault(); input.focus(); }
  });

  // ---------- controls ----------
  function seg(options, value, onpick, icons) {
    const wrap = h("div", { class: "seg", role: "radiogroup" });
    for (const opt of options) {
      const b = h("button", { type: "button", class: String(opt) === String(value) ? "on" : "", role: "radio" },
        icons ? icon(icons[opt]) : "", String(opt).charAt(0).toUpperCase() + String(opt).slice(1));
      b.addEventListener("click", () => { $$("button", wrap).forEach((x) => x.classList.remove("on")); b.classList.add("on"); onpick(opt); });
      wrap.append(b);
    }
    return wrap;
  }
  function toggle(checked, onchange) {
    const el = h("input", { type: "checkbox", role: "switch" });
    el.checked = !!checked;
    el.addEventListener("change", () => onchange(el.checked));
    return h("label", { class: "switch" }, el, h("span"));
  }
  const setRow = (title, desc, control) => h("div", { class: "set-row" }, h("div", {}, h("div", { class: "t" }, title), desc ? h("div", { class: "d" }, desc) : ""), h("div", { class: "ctl" }, control));
  const section = (title, desc, ...kids) => h("div", { class: "set-section" }, h("h3", {}, title), desc ? h("p", { class: "desc" }, desc) : "", ...kids);
  const field = (label, control, helpEl) => h("div", { class: "field" }, h("label", {}, label), control, helpEl || "");
  const help = (text) => h("div", { class: "help" }, text);
  function textInput(value, onchange, attrs = {}) {
    const el = h("input", { class: "input", value: value ?? "", ...attrs });
    el.addEventListener("input", () => onchange(attrs.type === "number" ? Number(el.value) : el.value));
    return el;
  }
  function lockable(key, el) {
    if (!(S.cfg?.env_locked || []).includes(key)) return el;
    $$("input,button", el).forEach((x) => (x.disabled = true));
    if (el.tagName === "INPUT") el.disabled = true;
    return h("div", {}, el, h("div", { class: "help lock" }, icon("lock"), `Set by DCODE_${key.toUpperCase()} in the environment`));
  }

  // ---------- settings ----------
  const TABS = [
    ["general", "General", "palette"], ["model", "Model", "cpu"], ["agents", "Helpers", "users"],
    ["memory", "Memory & context", "layers"], ["permissions", "Permissions", "shield"], ["skills", "Skills", "zap"], ["ui", "Web UI", "globe"],
  ];
  async function openSettings(tab) {
    if (tab) S.tab = tab;
    openModal("#settings-modal");
    $("#settings-body").replaceChildren(h("div", { class: "empty-state" }, h("span", { class: "spin" })));
    try {
      S.cfg = await api("GET", "/api/config"); S.skills = await api("GET", "/api/skills");
      S.helpers = await api("GET", "/api/agents" + (S.info?.cwd ? "?cwd=" + encodeURIComponent(S.info.cwd) : ""));
    }
    catch (e) { toast(e.message, true); return; }
    renderSettings();
  }
  function renderSettings() {
    const c = S.cfg;
    if (!c) return;
    c.permissions ||= {}; c.experimental ||= {}; c.agents ||= {}; c.memory ||= {}; c.ui ||= {}; c.enabled_skills ||= [];
    $("#settings-tabs").replaceChildren(...TABS.map(([id, label, ic]) =>
      h("button", { class: S.tab === id ? "active" : "", onclick: () => { S.tab = id; renderSettings(); } }, icon(ic), label)));
    const body = $("#settings-body");
    body.replaceChildren();
    body.scrollTop = 0;
    if (S.tab === "general") {
      const cur = getTheme();
      body.append(
        section("Appearance", "Choose how Dcode looks. System follows your computer's setting.",
          h("div", { class: "theme-cards" }, ...THEMES.map((t) => h("button", { class: "theme-card" + (t === cur ? " on" : ""), onclick: () => setTheme(t) },
            h("div", { class: `prev prev-${t}` }, h("i", { class: "a" }), h("i", { class: "b" })),
            h("div", { class: "lbl" }, icon(themeIcon[t]), t.charAt(0).toUpperCase() + t.slice(1)))))),
        section("Keyboard shortcuts", null,
          setRow("New chat", null, h("span", { class: "tag" }, "⌘/Ctrl ⇧ O")),
          setRow("Focus the message box", null, h("span", { class: "tag" }, "/")),
          setRow("Send · new line", null, h("span", { class: "tag" }, "Enter · Shift Enter")),
          setRow("Close dialogs and menus", null, h("span", { class: "tag" }, "Esc"))));
    } else if (S.tab === "model") {
      const models = h("datalist", { id: "model-options" });
      const fetchBtn = h("button", { class: "btn", type: "button", onclick: async () => {
        fetchBtn.disabled = true;
        try {
          const r = await api("GET", "/api/models");
          models.replaceChildren(...(r.models || []).map((m) => h("option", { value: m })));
          toast(r.error ? "Couldn't list models: " + r.error : `${(r.models || []).length} models available`, !!r.error);
        } finally { fetchBtn.disabled = false; }
      } }, icon("refresh"), "Fetch list");
      body.append(
        section("Connection", "Any OpenAI-compatible server works: Ollama, vLLM, llama.cpp, LM Studio, TGI and more.",
          field("Server URL", lockable("base_url", textInput(c.base_url, (v) => (c.base_url = v), { placeholder: "http://localhost:11434/v1" }))),
          field("API key", lockable("api_key", textInput("", (v) => (c.api_key = v), { type: "password", placeholder: c.has_api_key ? `${c.api_key} (saved)` : "Any value works for local servers" })),
            help("Leave empty to keep the saved key."))),
        section("Model", null,
          field("Model name", lockable("model", h("div", { class: "with-btn" }, textInput(c.model, (v) => (c.model = v), { list: "model-options" }), fetchBtn))),
          models,
          field("Reasoning effort", lockable("reasoning_effort", seg(c.reasoning_efforts || ["none", "low", "medium", "high"], c.reasoning_effort, (v) => (c.reasoning_effort = v))),
            help("Servers that don't support this are detected automatically.")),
          h("div", { class: "grid2" },
            field("Context window", lockable("context_window", textInput(c.context_window, (v) => (c.context_window = v), { type: "number", min: 2048, step: 1024 })),
              help("In tokens. Match your server, e.g. 32768.")),
            field("Max reply length", lockable("max_output_tokens", textInput(c.max_output_tokens, (v) => (c.max_output_tokens = v), { type: "number", min: 256, step: 256 })),
              help("Tokens reserved for each answer."))),
          setRow("Stream replies", "Show words as they arrive.", toggle(c.stream !== false, (v) => (c.stream = v)))));
    } else if (S.tab === "agents") {
      body.append(section("Helpers", "Dcode can hand focused jobs to helpers that start with a clean slate. Only their short report comes back, which keeps small-context models sharp.",
        setRow("Use helpers", "Explorer, Coder, Reviewer and Tester.", toggle(c.agents.enabled !== false, (v) => (c.agents.enabled = v))),
        setRow("Helpers working at once", "Read-only helpers can run in parallel if your server handles concurrent requests.", seg([1, 2, 3, 4], c.agents.max_parallel ?? 2, (v) => (c.agents.max_parallel = v))),
        setRow("Steps per helper", "How long a helper may work before reporting back.", textInput(c.agents.max_steps ?? 16, (v) => (c.agents.max_steps = v), { type: "number", min: 2, max: 100, style: "width:90px" })),
        field("Helper model", textInput(c.agents.helper_model || "", (v) => (c.agents.helper_model = v.trim()), { placeholder: `Same as the main model (${c.model})` }),
          help("Run helpers on another model on the same server, e.g. a small fast one for searching code."))));
      const list = h("div");
      for (const a of S.helpers || []) {
        const tags = [a.scope === "builtin" ? h("span", { class: "tag blue" }, "built-in") : h("span", { class: "tag" }, a.scope),
          a.model ? h("span", { class: "tag" }, a.model) : "", a.read_only ? h("span", { class: "tag" }, "read-only") : h("span", { class: "tag" }, "can change files")];
        list.append(h("div", { class: "skill" },
          h("div", { class: "skill-head" }, icon((ROLES[a.name] || [0, "users"])[1]), h("span", { class: "skill-name" }, "@" + a.name, ...tags),
            a.scope === "user" ? h("button", { class: "btn sm ghost", title: "Delete", "aria-label": "Delete", onclick: async () => {
              if (!confirm(`Delete the ${a.name} helper?`)) return;
              await api("DELETE", "/api/agents/" + encodeURIComponent(a.name)); openSettings("agents");
            } }, icon("trash")) : ""),
          h("div", { class: "skill-desc" }, a.description),
          a.path ? h("div", { class: "skill-desc mono-path" }, a.path) : ""));
      }
      const nName = textInput("", () => {}, { placeholder: "ansible-reviewer" });
      const nDesc = textInput("", () => {}, { placeholder: "When should Dcode use this helper?" });
      const nTools = textInput("", () => {}, { placeholder: "read-only (default), edit, or e.g. read_file, run_command" });
      const nModel = textInput("", () => {}, { placeholder: "optional, e.g. qwen2.5-coder:7b" });
      const nBody = h("textarea", { class: "textarea", rows: 5, placeholder: "You review Ansible code. Check every task for…" });
      body.append(
        section("Your helpers", "Start a message with @name to use one directly, or let Dcode pick. Add your own as Markdown files in ~/.dcode/agents or a project's .dcode/agents folder; a file named like a built-in (explore, code, review, test) changes it.", list),
        section("New helper", null,
          h("div", { class: "grid2" }, field("Name", nName), field("Model", nModel)),
          field("Description", nDesc), field("Tools", nTools), field("Instructions", nBody),
          h("button", { class: "btn", onclick: async () => {
            if (!nName.value.trim() || !nDesc.value.trim()) return toast("Give the helper a name and a description", true);
            try {
              await api("POST", "/api/agents", { name: nName.value, description: nDesc.value, tools: nTools.value, model: nModel.value, prompt: nBody.value });
              toast("Helper added"); openSettings("agents");
            } catch (e) { toast(e.message, true); }
          } }, icon("plus"), "Add helper")));
    } else if (S.tab === "memory") {
      body.append(
        section("Memory", "Dcode remembers project conventions, your preferences and what happened in past sessions, all stored locally as plain text.",
          setRow("Remember things", "Let Dcode save and look up memories.", toggle(c.memory.enabled !== false, (v) => (c.memory.enabled = v))),
          setRow("Recall automatically", "Bring relevant memories into each new task.", toggle(c.memory.auto_recall !== false, (v) => (c.memory.auto_recall = v))),
          setRow("Recall budget", "Max tokens of memory per task.", textInput(c.memory.budget_tokens ?? 1200, (v) => (c.memory.budget_tokens = v), { type: "number", min: 0, step: 100, style: "width:100px" })),
          h("div", { style: "margin-top:12px" }, h("button", { class: "btn", onclick: () => { closeModals(); openMemory(); } }, icon("brain"), "Manage memories"))),
        section("Context", "When a chat gets long, Dcode first trims old tool output, then summarizes older messages, so it never runs out of room. Set the window size on the Model tab."));
    } else if (S.tab === "permissions") {
      const perms = { read_file: ["Read files", "Look at files in the project."], write_file: ["Edit files", "Create and change files."], run_command: ["Run commands", "Always asks before running."], delete_file: ["Delete files", "Always asks before deleting."], create_directory: ["Create folders", null], search_in_files: ["Search the code", null] };
      const exp = { auto_approve_commands: ["Run commands without asking", "Only for trusted, sandboxed machines."], auto_commit: ["Offer a commit after edits", null], multi_file_edit: ["Multi-file edits", "Hint the model it may change several files at once."], streaming_tools: ["Full tool output in the terminal", null] };
      body.append(
        section("What Dcode may do", "Abilities you turn off are hidden from the model completely.",
          ...Object.entries(perms).map(([k, [t, d]]) => setRow(t, d, toggle(c.permissions[k] !== false, (v) => (c.permissions[k] = v))))),
        section("Checks after edits", "Dcode checks each file right after changing it (syntax errors, undefined names) and fixes what it finds. Uses tools you already have, like ruff, node or bash.",
          setRow("Check files after edits", null, toggle((c.checks || {}).enabled !== false, (v) => ((c.checks ||= {}).enabled = v))),
          setRow("Also format them", "Runs ruff or black, gofmt, rustfmt, or the project's own prettier.", toggle(!!(c.checks || {}).format, (v) => ((c.checks ||= {}).format = v)))),
        section("Artifacts", null, setRow("Create artifacts", "Let Dcode save pages, diagrams and reports to the Artifacts library.", toggle((c.artifacts || {}).enabled !== false, (v) => ((c.artifacts ||= {}).enabled = v)))),
        section("Advanced", null, ...Object.entries(exp).map(([k, [t, d]]) => setRow(t, d, toggle(!!c.experimental[k], (v) => (c.experimental[k] = v))))));
    } else if (S.tab === "skills") {
      const pinned = new Set(c.enabled_skills.map((n) => n.replace(/\.md$/, "")));
      const list = h("div");
      if (!S.skills.length) list.append(h("div", { class: "empty-state" }, icon("zap"), "No skills yet."));
      for (const sk of S.skills) {
        const desc = textInput(sk.description, () => {}, { placeholder: "One line: when should Dcode use this?" });
        const ta = h("textarea", { class: "textarea", rows: 8 }, sk.content);
        const editor = h("div", { class: "skill-edit", hidden: true }, desc, ta,
          h("div", { class: "with-btn", style: "justify-content:flex-end" },
            h("button", { class: "btn sm ghost", onclick: () => (editor.hidden = true) }, "Cancel"),
            h("button", { class: "btn sm primary", onclick: async () => {
              try { await api("POST", "/api/skills", { name: sk.name, description: desc.value, content: ta.value }); toast("Skill saved"); S.skills = await api("GET", "/api/skills"); renderSettings(); }
              catch (e) { toast(e.message, true); }
            } }, "Save")));
        list.append(h("div", { class: "skill" },
          h("div", { class: "skill-head" }, icon("zap"), h("span", { class: "skill-name" }, sk.name,
            sk.builtin ? h("span", { class: "tag blue" }, "built-in") : "", sk.scope === "project" ? h("span", { class: "tag" }, "project") : ""),
            h("button", { class: "btn sm ghost", onclick: () => (editor.hidden = !editor.hidden) }, "Edit"),
            h("button", { class: "btn sm ghost", title: "Delete", "aria-label": "Delete", onclick: async () => {
              if (!confirm(`Delete the ${sk.name} skill?`)) return;
              await api("DELETE", "/api/skills/" + encodeURIComponent(sk.name)); S.skills = await api("GET", "/api/skills"); renderSettings();
            } }, icon("trash")),
            h("span", { title: "Pin: always include in the prompt" }, toggle(pinned.has(sk.name), (on) => {
              const names = new Set(c.enabled_skills.map((n) => n.replace(/\.md$/, "")));
              on ? names.add(sk.name) : names.delete(sk.name);
              c.enabled_skills = [...names];
            }))),
          h("div", { class: "skill-desc" }, sk.description || "No description"),
          editor));
      }
      const live = S.mode === "session" && !S.ended;
      const ask = textInput("", () => {}, { placeholder: "e.g. a skill for writing Ansible playbooks the way my team does" });
      const nName = textInput("", () => {}, { placeholder: "skill-name" });
      const nDesc = textInput("", () => {}, { placeholder: "When should Dcode use it?" });
      const nBody = h("textarea", { class: "textarea", rows: 5, placeholder: "- Step or rule one\n- Step or rule two" });
      body.append(
        section("Skills", "Playbooks that shape how Dcode works. Dcode picks a matching skill by itself; pinned skills are always included (keep pins few on small models).", list),
        section("Ask Dcode to write a skill", live ? "Describe what the skill should do and Dcode writes it for you." : "Open a live session to have Dcode write a skill.",
          h("div", { class: "with-btn" }, ask, h("button", { class: "btn primary", disabled: live ? null : true, onclick: () => {
            const what = ask.value.trim(); if (!what) return;
            closeModals();
            sendText(`Create a new reusable skill with skill_save: ${what}. Keep it short (5–10 bullet points), practical and specific.`);
          } }, icon("sparkles"), "Create"))),
        section("Write one yourself", "Or drop a .md file into ~/.dcode/skills (or a project's .dcode/skills folder).",
          h("div", { class: "grid2" }, field("Name", nName), field("Description", nDesc)),
          field("Instructions (Markdown)", nBody),
          h("button", { class: "btn", onclick: async () => {
            if (!nName.value.trim()) return toast("Give the skill a name", true);
            try { await api("POST", "/api/skills", { name: nName.value, description: nDesc.value, content: nBody.value }); toast("Skill added"); S.skills = await api("GET", "/api/skills"); renderSettings(); }
            catch (e) { toast(e.message, true); }
          } }, icon("plus"), "Add skill")));
    } else if (S.tab === "ui") {
      body.append(section("Web UI", "Opens with your first dcode session and closes after the last one exits. Your chats and memory stay on disk.",
        setRow("Enable the web UI", "Or start dcode with --no-ui.", toggle(c.ui.enabled !== false, (v) => (c.ui.enabled = v))),
        setRow("Open the browser automatically", null, toggle(!!c.ui.open_browser, (v) => (c.ui.open_browser = v))),
        setRow("Port", "Takes effect next time the UI starts. Empty means automatic.", textInput(c.ui.port || "", (v) => (c.ui.port = v), { type: "number", placeholder: "auto", style: "width:110px" }))));
    }
  }
  $("#settings-save").addEventListener("click", async () => {
    const c = { ...S.cfg };
    if (!c.api_key || c.api_key.includes("•")) delete c.api_key;
    for (const k of [...(c.env_locked || []), "has_api_key", "reasoning_efforts", "env_locked"]) delete c[k];
    try {
      const r = await api("PUT", "/api/config", c);
      toast(r.sessions_reloaded ? `Saved · ${r.sessions_reloaded} live session${r.sessions_reloaded > 1 ? "s" : ""} updated` : "Saved");
      closeModals(); S.models = null; refreshSessions();
    } catch (e) { toast(e.message, true); }
  });
  $("#open-settings").addEventListener("click", () => openSettings());

  // ---------- memory ----------
  const projLabel = (key) => key === "global" ? "Everywhere" : key.replace("projects/", "").replace(/-[0-9a-f]{8}$/, "");
  async function openMemory() {
    openModal("#memory-modal");
    $("#memory-form").hidden = true;
    try { S.memories = await api("GET", "/api/memory"); } catch (e) { toast(e.message, true); return; }
    const scopes = [...new Set(S.memories.map((m) => m.scope_key))];
    const sel = $("#memory-scope");
    const prev = sel.value;
    sel.replaceChildren(h("option", { value: "" }, "All memories"), ...scopes.map((s) => h("option", { value: s }, s === "global" ? "Everywhere (global)" : "Project · " + projLabel(s))));
    sel.value = scopes.includes(prev) ? prev : "";
    renderMemory();
  }
  function renderMemory() {
    const q = $("#memory-filter").value.trim().toLowerCase();
    const scope = $("#memory-scope").value;
    const list = $("#memory-list");
    list.replaceChildren();
    const items = S.memories.filter((m) => (!scope || m.scope_key === scope) && (!q || `${m.name} ${m.description} ${m.body} ${(m.tags || []).join(" ")}`.toLowerCase().includes(q)));
    if (!items.length) list.append(h("div", { class: "empty-state" }, icon("brain"), S.memories.length ? "No memories match." : "Nothing remembered yet. Dcode saves things as it learns, or you can add one."));
    for (const m of items) {
      const bodyEl = h("div", { class: "mem-body", hidden: true }, m.body);
      list.append(h("div", { class: "mem" },
        h("div", { class: "mem-head" },
          h("span", { class: "mem-name" }, m.name),
          h("span", { class: "tag" + (m.scope_key === "global" ? " blue" : "") }, projLabel(m.scope_key)),
          ...(m.tags || []).map((t) => h("span", { class: "tag" }, t)),
          h("span", { class: "grow" }), h("span", { class: "when" }, ago(m.updated)),
          h("button", { class: "btn sm ghost", onclick: () => (bodyEl.hidden = !bodyEl.hidden) }, "View"),
          h("button", { class: "btn sm ghost", title: "Forget", "aria-label": "Forget", onclick: async () => {
            if (!confirm(`Forget “${m.name}”?`)) return;
            await api("DELETE", `/api/memory/${encodeURIComponent(m.scope_key)}/${encodeURIComponent(m.name)}`);
            openMemory();
          } }, icon("trash"))),
        m.description ? h("div", { class: "mem-desc" }, m.description) : "",
        bodyEl));
    }
  }
  $("#memory-filter").addEventListener("input", renderMemory);
  $("#memory-scope").addEventListener("change", renderMemory);
  $("#memory-add").addEventListener("click", () => {
    const form = $("#memory-form");
    if (!form.hidden) { form.hidden = true; return; }
    const scopes = [...new Set(["global", ...S.memories.map((m) => m.scope_key)])];
    const data = { scope_key: "global", name: "", description: "", content: "" };
    const scopeSel = h("select", { class: "select" }, ...scopes.map((s) => h("option", { value: s }, s === "global" ? "Everywhere (global)" : "Project · " + projLabel(s))));
    scopeSel.addEventListener("change", () => (data.scope_key = scopeSel.value));
    const content = h("textarea", { class: "textarea", rows: 4, placeholder: "What should Dcode remember?" });
    content.addEventListener("input", () => (data.content = content.value));
    form.replaceChildren(
      h("div", { class: "grid2" }, field("Name", textInput("", (v) => (data.name = v), { placeholder: "e.g. test-command" })), field("Applies to", scopeSel)),
      field("Memory", content),
      h("div", { class: "with-btn", style: "justify-content:flex-end" },
        h("button", { class: "btn ghost", onclick: () => (form.hidden = true) }, "Cancel"),
        h("button", { class: "btn primary", onclick: async () => {
          try { await api("POST", "/api/memory", data); toast("Saved to memory"); openMemory(); }
          catch (e) { toast(e.message, true); }
        } }, "Save")));
    form.hidden = false;
  });
  $("#open-memory").addEventListener("click", openMemory);

  // =====================================================================
  // Artifacts
  // =====================================================================
  const KIND = {
    html: ["page", "Web page"], svg: ["diagram", "Diagram"], markdown: ["doc", "Document"], csv: ["table", "Table"], code: ["code", "Code"],
  };
  const EXT_KIND = { html: "html", htm: "html", svg: "svg", md: "markdown", markdown: "markdown", txt: "markdown", csv: "csv" };
  const kindIcon = (k) => (KIND[k] || KIND.code)[0];
  const kindLabel = (k, lang) => k === "code" && lang ? `${lang} code` : (KIND[k] || KIND.code)[1];
  const artifactLink = (id) => `${location.origin}/a/${encodeURIComponent(id)}`;
  const liveSession = () => S.mode === "session" && !S.ended;

  function artifactCard(meta, target) {
    const card = h("div", { class: "artifact-card", role: "button", tabindex: "0", onclick: () => openArtifact(meta.id) },
      h("span", { class: "art-ico" }, icon(kindIcon(meta.kind))),
      h("div", { class: "art-meta" }, h("div", { class: "art-title" }, meta.title),
        h("div", { class: "art-sub" }, `${kindLabel(meta.kind)}${meta.version ? ` · v${meta.version}` : ""} · click to open`)),
      h("a", { class: "icon-btn", href: artifactLink(meta.id), target: "_blank", rel: "noopener", title: "Open as a page",
        html: svg("external"), onclick: (e) => e.stopPropagation() }),
      h("span", { class: "btn sm" }, "Open"));
    target.append(card);
    scrollDown();
    return card;
  }
  // Sandboxed preview: no same-origin access, no network (CSP), scripts allowed for interactivity.
  const CSP = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data: blob:; media-src data: blob:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; font-src data:">`;
  function sandboxDoc(kind, content) {
    if (kind === "svg") return `<!doctype html><html><head><meta charset="utf-8">${CSP}<style>html,body{margin:0;height:100%;background:#fff}body{display:grid;place-items:center;padding:16px;box-sizing:border-box}svg{max-width:100%;max-height:100%;height:auto}</style></head><body>${content}</body></html>`;
    if (/<head[^>]*>/i.test(content)) return content.replace(/<head[^>]*>/i, (m) => m + CSP);
    return `<!doctype html><html><head><meta charset="utf-8">${CSP}</head><body>${content}</body></html>`;
  }
  function sandboxFrame(kind, content, title) {
    const frame = h("iframe", { sandbox: "allow-scripts allow-modals", title: title || "artifact", referrerpolicy: "no-referrer" });
    frame.srcdoc = sandboxDoc(kind, content);
    return frame;
  }
  function parseCSV(text) {
    const rows = []; let row = [], cell = "", q = false;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (q) { if (ch === '"' && text[i + 1] === '"') { cell += '"'; i++; } else if (ch === '"') q = false; else cell += ch; }
      else if (ch === '"') q = true;
      else if (ch === ",") { row.push(cell); cell = ""; }
      else if (ch === "\n") { row.push(cell); rows.push(row); row = []; cell = ""; }
      else if (ch !== "\r") cell += ch;
    }
    if (cell || row.length) { row.push(cell); rows.push(row); }
    return rows;
  }
  function csvTable(content) {
    const [headRow, ...rest] = parseCSV(content).slice(0, 2000);
    return h("div", { class: "csv-wrap" }, h("table", { class: "csv" },
      h("thead", {}, h("tr", {}, ...(headRow || []).map((c) => h("th", {}, c)))),
      h("tbody", {}, ...rest.map((r) => h("tr", {}, ...r.map((c) => h("td", {}, c)))))));
  }

  // ---------- viewer pane ----------
  async function openArtifact(id, tab, version) {
    let a;
    try { a = await api("GET", `/api/artifacts/${encodeURIComponent(id)}${version ? `?version=${version}` : ""}`); }
    catch (e) { return toast("That artifact is gone: " + e.message, true); }
    closeModals();
    const visual = ["html", "svg", "markdown", "csv"].includes(a.kind);
    const older = version && version !== a.versions;
    S.pane = { id, tab: tab || (visual ? "preview" : "code"), data: a, version: older ? version : null };
    if (older && S.pane.tab === "edit") S.pane.tab = visual ? "preview" : "code";
    app.classList.add("pane-open");
    $("#artifact-pane").hidden = false;
    $("#pane-ico").replaceChildren(icon(kindIcon(a.kind)));
    $("#pane-title").textContent = a.title;
    $("#pane-sub").textContent = `${kindLabel(a.kind, a.language)} · v${a.versions} · ${ago(a.updated)}`;
    // Link back to the chat that made it (live session if it's still open, else the saved thread).
    const chat = $("#pane-chat");
    const liveFor = S.sessions.find((x) => a.thread && x.thread_name === a.thread);
    chat.hidden = !a.thread;
    if (a.thread) {
      chat.textContent = ` · from “${prettyThread(a.thread)}”`;
      chat.href = liveFor ? `#s=${liveFor.id}` : `#t=${threadId(a.thread)}`;
    }
    const sel = $("#pane-version");
    sel.hidden = a.history.length < 2;
    sel.replaceChildren(...a.history.slice().reverse().map((v) => h("option", { value: v.version },
      `v${v.version}${v.current ? " · latest" : ""} · ${ago(v.updated)}`)));
    sel.value = String(older ? version : a.versions);
    const banner = $("#pane-banner");
    banner.hidden = !older;
    if (older) banner.replaceChildren(icon("refresh"), h("span", { class: "grow" }, `Viewing v${version} of ${a.versions}.`),
      h("button", { class: "btn sm primary", onclick: async () => {
        try { await api("POST", `/api/artifacts/${encodeURIComponent(id)}/restore`, { version }); toast(`Restored v${version} as a new version`); openArtifact(id); refreshArtifactCount(); }
        catch (e) { toast(e.message, true); }
      } }, "Restore this version"),
      h("button", { class: "btn sm ghost", onclick: () => openArtifact(id, S.pane.tab) }, "Back to latest"));
    $("#pane-download").href = `/api/artifacts/${encodeURIComponent(id)}/raw`;
    $("#pane-page").href = artifactLink(id);
    const tabs = $("#pane-tabs");
    const names = (visual ? ["preview", "code"] : ["code"]).concat(older ? [] : ["edit"]);
    tabs.replaceChildren(...names.map((t) => h("button", { type: "button", class: S.pane.tab === t ? "on" : "",
      onclick: () => openArtifact(id, t, S.pane.version) }, { preview: "Preview", code: "Code", edit: "Edit" }[t])));
    const body = $("#pane-body");
    body.replaceChildren();
    if (S.pane.tab === "edit") {
      const ta = h("textarea", { class: "pane-editor", spellcheck: "false" }, a.content);
      body.append(h("div", { class: "pane-edit" }, ta, h("div", { class: "pane-edit-bar" },
        h("span", { class: "grow muted" }, "Saving creates a new version; older ones stay in the history."),
        h("button", { class: "btn sm ghost", onclick: () => openArtifact(id, visual ? "preview" : "code") }, "Cancel"),
        h("button", { class: "btn sm primary", onclick: async () => {
          try { await api("PUT", `/api/artifacts/${encodeURIComponent(id)}/content`, { content: ta.value }); toast("Saved as a new version"); openArtifact(id, visual ? "preview" : "code"); }
          catch (e) { toast(e.message, true); }
        } }, "Save"))));
      ta.focus();
    } else if (S.pane.tab === "code" || !visual) body.append(h("pre", { class: "code" }, h("code", {}, a.content)));
    else if (a.kind === "markdown") body.append(h("div", { class: "md", html: md(a.content) }));
    else if (a.kind === "csv") body.append(csvTable(a.content));
    else body.append(sandboxFrame(a.kind, a.content, a.title));
    $("#pane-ask").hidden = !liveSession() || !!older;
  }
  function closePane() { S.pane = null; $("#artifact-pane").hidden = true; app.classList.remove("pane-open"); }
  $("#pane-close").addEventListener("click", closePane);
  $("#pane-copy").addEventListener("click", () => S.pane && copyText(S.pane.data.content));
  $("#pane-link").addEventListener("click", () => S.pane && copyText(artifactLink(S.pane.id)));
  $("#pane-version").addEventListener("change", (e) => S.pane && openArtifact(S.pane.id, S.pane.tab, Number(e.target.value)));
  $("#pane-title").addEventListener("click", () => S.pane && renameArtifact(S.pane.id, S.pane.data.title));
  $("#pane-ask").addEventListener("submit", (e) => {
    e.preventDefault();
    const inp = $("#pane-ask-input");
    const ask = inp.value.trim();
    if (!ask || !S.pane) return;
    inp.value = "";
    sendText(`Update the artifact "${S.pane.data.title}" (id: ${S.pane.id}): ${ask}. Read it with artifact_get, then save it with artifact_save using the same id.`);
    toast("Sent to Dcode — the artifact updates when it's done");
  });
  async function renameArtifact(id, current) {
    const title = prompt("Rename artifact", current);
    if (!title || title.trim() === current) return;
    try { await api("PATCH", `/api/artifacts/${encodeURIComponent(id)}`, { title: title.trim() }); if (S.pane?.id === id) openArtifact(id, S.pane.tab); refreshArtifactCount().then(renderArtifactGrid); }
    catch (e) { toast(e.message, true); }
  }

  // ---------- library ----------
  async function refreshArtifactCount() {
    try { S.artifacts = await api("GET", "/api/artifacts"); } catch (_) { return; }
    const pill = $("#artifact-count");
    pill.textContent = S.artifacts.length;
    pill.hidden = !S.artifacts.length;
  }
  async function openArtifacts() {
    openModal("#artifacts-modal");
    await refreshArtifactCount();
    renderArtifactGrid();
  }
  const thumbCache = new Map();
  const thumbObserver = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
    for (const entry of entries) if (entry.isIntersecting) { thumbObserver.unobserve(entry.target); fillThumb(entry.target); }
  }, { rootMargin: "120px" }) : null;
  async function fillThumb(el) {
    const { id, kind } = el.dataset;
    let content = thumbCache.get(id);
    if (content === undefined) {
      try { content = (await api("GET", `/api/artifacts/${encodeURIComponent(id)}`)).content; } catch (_) { return; }
      thumbCache.set(id, content);
    }
    if (kind === "html" || kind === "svg") {
      const frame = sandboxFrame(kind, content, "preview");
      frame.className = "thumb-frame";
      frame.tabIndex = -1;
      el.replaceChildren(frame);
    } else {
      el.replaceChildren(h("pre", { class: "thumb-text" }, content.slice(0, 600)));
    }
  }
  function tileMenu(a) {
    const close = () => menu.remove();
    const item = (ic, label, fn, danger) => h("button", { type: "button", class: "menu-item" + (danger ? " danger" : ""),
      onclick: (e) => { e.stopPropagation(); close(); fn(); } }, icon(ic), label);
    const menu = h("div", { class: "menu tile-menu" },
      item("eye", "Open", () => openArtifact(a.id)),
      item("external", "Open as a page", () => window.open(artifactLink(a.id), "_blank", "noopener")),
      item("copy", "Copy link", () => copyText(artifactLink(a.id))),
      item("pencil", "Rename", () => renameArtifact(a.id, a.title)),
      item("plus", "Duplicate", async () => { await api("POST", `/api/artifacts/${encodeURIComponent(a.id)}/duplicate`, {}); toast("Duplicated"); await refreshArtifactCount(); renderArtifactGrid(); }),
      item("download", "Download", () => { location.href = `/api/artifacts/${encodeURIComponent(a.id)}/raw`; }),
      h("div", { class: "menu-sep" }),
      item("trash", "Delete", async () => {
        if (!confirm(`Delete “${a.title}” and all its versions?`)) return;
        await api("DELETE", `/api/artifacts/${encodeURIComponent(a.id)}`);
        if (S.pane?.id === a.id) closePane();
        thumbCache.delete(a.id);
        await refreshArtifactCount(); renderArtifactGrid();
      }, true));
    setTimeout(() => document.addEventListener("click", close, { once: true }), 0);
    return menu;
  }
  function renderArtifactGrid() {
    const q = $("#artifact-filter").value.trim().toLowerCase();
    const kind = $("#artifact-kind").value;
    const sort = $("#artifact-sort").value;
    const grid = $("#artifact-grid");
    grid.replaceChildren();
    let items = (S.artifacts || []).filter((a) => (!kind || a.kind === kind) && (!q || `${a.title} ${a.id} ${a.thread}`.toLowerCase().includes(q)));
    items = items.slice().sort((x, y) => sort === "name" ? x.title.localeCompare(y.title)
      : sort === "created" ? String(y.created).localeCompare(String(x.created)) : String(y.updated).localeCompare(String(x.updated)));
    if (!items.length) grid.append(h("div", { class: "empty-state", style: "grid-column:1/-1" }, icon("sparkles"),
      (S.artifacts || []).length ? "No artifacts match." : "No artifacts yet. Click New, or ask Dcode in any chat for a page, diagram or report."));
    for (const a of items) {
      const thumb = h("div", { class: "art-thumb", "data-id": a.id, "data-kind": a.kind }, icon(kindIcon(a.kind)));
      const more = h("button", { type: "button", class: "icon-btn", title: "More", "aria-label": "More actions", html: svg("chevron") });
      const tile = h("div", { class: "art-tile", role: "button", tabindex: "0", onclick: () => openArtifact(a.id) },
        thumb,
        h("div", { class: "art-tile-body" },
          h("span", { class: "art-ico small" }, icon(kindIcon(a.kind))),
          h("div", { class: "art-meta" }, h("div", { class: "art-title" }, a.title),
            h("div", { class: "art-sub" }, `${kindLabel(a.kind, a.language)} · v${a.versions} · ${ago(a.updated)}`)),
          more));
      more.addEventListener("click", (e) => { e.stopPropagation(); $$(".tile-menu").forEach((m) => m.remove()); tile.append(tileMenu(a)); });
      grid.append(tile);
      if (thumbObserver) thumbObserver.observe(thumb); else fillThumb(thumb);
    }
  }
  $("#artifact-filter").addEventListener("input", renderArtifactGrid);
  $("#artifact-kind").addEventListener("change", renderArtifactGrid);
  $("#artifact-sort").addEventListener("change", renderArtifactGrid);
  $("#open-artifacts").addEventListener("click", openArtifacts);

  // ---------- new artifact ----------
  let newTab = "ask";
  function openNewArtifact(tab) {
    newTab = tab || newTab;
    openModal("#new-artifact-modal");
    renderNewArtifact();
  }
  function renderNewArtifact() {
    $$("#new-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === newTab));
    const body = $("#new-body");
    body.replaceChildren();
    const kindSel = (value) => h("select", { class: "select" }, ...Object.entries(KIND).map(([k, [, label]]) => {
      const o = h("option", { value: k }, label); if (k === value) o.selected = true; return o;
    }));
    if (newTab === "ask") {
      const live = liveSession() || S.sessions.length;
      const what = h("textarea", { class: "textarea", rows: 5, placeholder: "e.g. A dashboard of the services in docker-compose.yml with their ports and health checks" });
      const kind = kindSel("html");
      body.append(
        h("p", { class: "lead" }, live ? "Describe what you want. Dcode builds it in the current session and it appears in your library." : "Start dcode in a terminal to have it build artifacts."),
        field("What should Dcode build?", what), field("Kind", kind),
        h("div", { class: "with-btn", style: "justify-content:flex-end" },
          h("button", { class: "btn primary", disabled: live ? null : true, onclick: async () => {
            const text = what.value.trim(); if (!text) return toast("Describe the artifact first", true);
            if (!liveSession()) location.hash = "s=" + S.sessions[0].id;
            closeModals();
            setTimeout(() => sendText(`Create a new ${KIND[kind.value][1].toLowerCase()} artifact (kind: ${kind.value}) with artifact_save: ${text}. Make it polished and self-contained.`), liveSession() ? 0 : 600);
            toast("Dcode is building it — watch the chat");
          } }, icon("sparkles"), "Build it")));
      setTimeout(() => what.focus(), 30);
    } else if (newTab === "write") {
      const title = textInput("", () => {}, { placeholder: "Title" });
      const kind = kindSel("markdown");
      const lang = textInput("", () => {}, { placeholder: "e.g. python (for code)" });
      const content = h("textarea", { class: "textarea mono", rows: 12, placeholder: "Paste or write the content…", spellcheck: "false" });
      body.append(field("Title", title), h("div", { class: "grid2" }, field("Kind", kind), field("Language", lang)), field("Content", content),
        h("div", { class: "with-btn", style: "justify-content:flex-end" },
          h("button", { class: "btn primary", onclick: () => createArtifact(title.value, kind.value, lang.value, content.value) }, "Create")));
      setTimeout(() => title.focus(), 30);
    } else {
      const file = h("input", { type: "file", class: "input", accept: ".html,.htm,.svg,.md,.markdown,.txt,.csv,.json,.yaml,.yml,.py,.js,.ts,.sh,.go,.rs,.java,.tf,.sql,.css,.xml,.ini,.toml,.conf" });
      const hint = h("p", { class: "lead" }, "Pick a file from your computer. HTML, SVG, Markdown and CSV get a live preview; anything else is stored as code.");
      body.append(hint, field("File", file), h("div", { class: "with-btn", style: "justify-content:flex-end" },
        h("button", { class: "btn primary", onclick: async () => {
          const f = file.files && file.files[0];
          if (!f) return toast("Choose a file first", true);
          if (f.size > 2_000_000) return toast("That file is larger than 2 MB", true);
          const ext = (f.name.split(".").pop() || "").toLowerCase();
          const kind = EXT_KIND[ext] || "code";
          createArtifact(f.name.replace(/\.[^.]+$/, ""), kind, kind === "code" ? ext : "", await f.text());
        } }, "Upload")));
    }
  }
  async function createArtifact(title, kind, language, content) {
    if (!content.trim()) return toast("The content is empty", true);
    try {
      const meta = await api("POST", "/api/artifacts", { title: title.trim() || "Untitled", kind, language: language.trim(), content });
      closeModals(); toast("Artifact created"); await refreshArtifactCount(); openArtifact(meta.id);
    } catch (e) { toast(e.message, true); }
  }
  $$("#new-tabs button").forEach((b) => b.addEventListener("click", () => { newTab = b.dataset.tab; renderNewArtifact(); }));
  $("#artifact-new").addEventListener("click", () => openNewArtifact("ask"));

  // =====================================================================
  // Boot
  // =====================================================================
  async function boot() {
    try {
      const hub = await api("GET", "/api/hub");
      S.user = hub.user || "";
      $("#user-name").textContent = S.user || "Local user";
      $("#user-avatar").textContent = (S.user || "D").charAt(0).toUpperCase();
    } catch (_) { /* unauthorized → server shows its own page */ }
    await Promise.all([refreshSessions(), refreshThreads(), refreshArtifactCount()]);
    window.addEventListener("hashchange", route);
    route();
    setInterval(refreshSessions, 2500);
    setInterval(refreshThreads, 10000);
  }
  boot();
})();
