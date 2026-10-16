/* Dcode Manager: the admin pages. Loaded after app.js, which calls window.DM_ADMIN(DM) before it boots. */
window.DM_ADMIN = (DM) => {
  "use strict";
  const { h, s, api, attempt, ask, toast, drawer, closeDrawer, field, text, numberInput, select, periodSelect, check, toggle,
    intOrNull, usageBar, table, td, emptyState, icon, num, short, ms, when, until, host, enc, PERIOD, PAGES, S, render } = DM;
  const COLORS = ["#2563EB", "#0891B2", "#16A34A", "#D97706", "#9333EA", "#DB2777", "#64748B", "#0D9488"];
  let FAMILIES = [];
  let MODELS = [];

  // ---------- monitoring ----------
  PAGES.monitoring = async (page) => {
    const range = sessionStorage.getItem("dm-range") || "24h";
    const [data, clients, listed] = await Promise.all([api("GET", `/api/admin/monitoring?range=${range}`), api("GET", "/api/admin/clients"),
      api("GET", "/api/admin/models")]);
    const live = { models: (S.live && S.live.models) || [] };  // last known; refreshed below without holding up the page
    const seg = h("div", { class: "seg", role: "group", "aria-label": "Time range" }, ["1h", "24h", "7d", "30d"].map((r) =>
      h("button", { class: r === range ? "on" : null, text: r, onclick: () => { sessionStorage.setItem("dm-range", r); render(); } })));
    page.append(h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Monitoring" }),
      h("p", { text: "Every request through the gateway: who used which model, how much, and how fast it answered." })),
      h("div", { class: "actions" }, seg, h("button", { class: "btn small", title: "Refresh", onclick: () => render() }, icon("refresh")))));
    const t = data.total;
    page.append(h("div", { class: "tiles" },
      tile(num(t.requests), "requests"), tile(short(t.prompt_tokens), "tokens in"), tile(short(t.completion_tokens), "tokens out"),
      tile(num(t.users), "active users"), tile(num(t.errors), "errors", t.errors > 0), tile(num(t.refused), "refused (limits, guards)")));
    const liveBy = Object.fromEntries(live.models.map((m) => [m.model, m]));
    const models = data.models;
    const colorOf = Object.fromEntries(Object.keys(data.series).map((m, i) => [m, COLORS[i % COLORS.length]]));
    page.append(h("div", { class: "card" }, h("h2", {}, "Tokens over time", h("small", { text: `${range} · ${Math.round(data.bucket_seconds / 60)} min per bar` })),
      Object.keys(data.series).length ? [stackedBars(data, colorOf), h("div", { class: "legend" }, Object.entries(colorOf).map(([m, c]) =>
        h("span", {}, h("i", { style: { background: c } }), m)))] : h("div", { class: "empty" }, emptyState("No traffic in this range"))));
    page.append(h("div", {}, h("h2", { style: { fontSize: "15px", margin: "0 0 10px" }, text: "Models" }),
      table(["", "Model", h("th", { class: "right", text: "Requests" }), h("th", { class: "right", text: "Tokens in / out" }),
        h("th", { class: "right", text: "p50 / p95" }), h("th", { class: "right", text: "First token" }), h("th", { class: "right", text: "Tokens/s" }),
        h("th", { class: "right", text: "Errors" }), "Server now"],
      [...new Set([...models.map((m) => m.model), ...listed.models.map((m) => m.id)])].map((name) => {
        const m = models.find((x) => x.model === name) || {};
        const l = liveBy[name] || { checking: true };
        return h("tr", { "data-model": name }, td(h("span", { class: "legend" }, h("i", { style: { background: colorOf[name] || "var(--border-strong)" } }))),
          td(h("b", { text: name })), td(num(m.requests || 0), "right num"),
          td(`${short(m.prompt_tokens || 0)} / ${short(m.completion_tokens || 0)}`, "right num"),
          td(`${ms(m.p50_ms)} / ${ms(m.p95_ms)}`, "right num"), td(ms(m.ttft_ms), "right num"),
          td(m.tokens_per_s ? m.tokens_per_s.toFixed(0) : "—", "right num"),
          td(m.requests ? `${m.errors} (${(100 * m.error_rate).toFixed(1)}%)` : "—", "right num"),
          td(liveCell(l), "live"));
      }), emptyState("No models yet", "Add one in Models."))));
    api("GET", "/api/admin/monitoring/live").then((fresh) => {
      S.live = fresh;
      for (const l of fresh.models) {
        const cell = page.querySelector(`tr[data-model="${CSS.escape(l.model)}"] td.live`);
        if (cell) cell.replaceChildren(liveCell(l));
      }
    }).catch(() => {});
    page.append(h("div", { class: "cols" },
      h("div", {}, h("h2", { style: { fontSize: "15px", margin: "0 0 10px" }, text: "Top users" }),
        table(["User", h("th", { class: "right", text: "Requests" }), h("th", { class: "right", text: "Tokens" }), "Models", "Last"],
          data.users.slice(0, 12).map((u) => h("tr", {}, td(h("b", { text: u.user })), td(num(u.requests), "right num"), td(short(u.tokens), "right num"),
            td(u.models.join(", "), "wrap"), td(when(u.last)))), emptyState("Nobody used a model in this range"))),
      h("div", {}, h("h2", { style: { fontSize: "15px", margin: "0 0 10px" }, text: "Recent problems" }),
        table(["When", "Who", "What"], data.problems.slice(0, 10).map((p) => h("tr", {}, td(when(p.ts)), td(p.user || "—"),
          td(h("span", {}, h("span", { class: "badge " + (p.status >= 500 ? "red" : "amber"), text: p.guard ? p.guard.split(" ")[0] : String(p.status) }), " ",
            h("span", { class: "muted small", text: `${p.model}: ${p.error}`.slice(0, 140) })), "wrap"))), emptyState("No errors or refusals")))));
    page.append(h("div", {}, h("h2", { style: { fontSize: "15px", margin: "0 0 10px" }, text: "Devices" }),
      table(["User", "Key", "Client", "Last used", "Made", ""], clients.clients.slice(0, 30).map((c) => h("tr", {},
        td(h("b", { text: c.username })), td(h("span", {}, c.name, " ", h("code", { class: "muted", text: c.prefix + "…" }), c.revoked ? h("span", { class: "badge", text: "revoked" }) : null)),
        td(c.client || "—"), td(when(c.last_used)), td(when(c.created)),
        td(c.revoked ? "" : h("button", { class: "btn small danger", text: "Revoke", onclick: async () => {
          if (!(await ask(`Revoke ${c.username}'s key?`, `“${c.name}” stops working at once; that device needs \`dcode login\` again.`, "Revoke", true))) return;
          if (await attempt(() => api("DELETE", `/api/me/keys/${c.id}`), "Key revoked")) render();
        } }), "right"))), emptyState("No devices yet"))));
    S.refresh = setInterval(() => { if (document.visibilityState === "visible" && !document.querySelector(".drawer")) render(); }, 30000);
  };
  function liveCell(l) {
    if (l.checking) return h("span", { class: "muted small", text: "checking…" });
    const metrics = l.metrics || {};
    const state = l.enabled === false ? h("span", { class: "badge", text: "Off" }) : l.up ? h("span", { class: "badge green" }, h("span", { class: "dot up" }), `up · ${ms(l.probe_ms)}`)
      : h("span", { class: "badge red", title: l.error || "" }, h("span", { class: "dot down" }), "down");
    const load = metrics.running !== undefined ? ` · ${metrics.running} running, ${metrics.waiting || 0} waiting` + (metrics.kv_cache !== undefined ? `, KV ${Math.round(metrics.kv_cache * 100)}%` : "") : "";
    return h("span", {}, state, h("span", { class: "muted small", text: load }));
  }
  function tile(value, label, bad) { return h("div", { class: "tile" + (bad ? " bad" : "") }, h("b", { text: value }), h("span", { text: label })); }
  function stackedBars(data, colorOf) {
    const W = 760, H = 190, L = 44, B = 22, T = 8;
    const n = data.requests_series.length;
    const totals = Array.from({ length: n }, (_, i) => Object.values(data.series).reduce((a, sr) => a + sr[i], 0));
    const max = niceMax(Math.max(1, ...totals));
    const y = (v) => T + (H - T - B) * (1 - v / max);
    const bw = (W - L - 4) / n;
    const svg = s("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Tokens per time bucket, stacked by model" });
    for (let k = 0; k <= 4; k++) {
      const v = (max * k) / 4;
      svg.append(s("line", { class: "grid", x1: L, x2: W, y1: y(v), y2: y(v) }), s("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, short(v)));
    }
    for (let i = 0; i < n; i++) {
      let base = 0;
      for (const [model, series] of Object.entries(data.series)) {
        const v = series[i];
        if (!v) continue;
        svg.append(s("rect", { x: L + i * bw + 1, y: y(base + v), width: Math.max(1, bw - 2), height: y(base) - y(base + v), fill: colorOf[model], rx: 1.5 },
          s("title", {}, `${model}: ${num(v)} tokens`)));
        base += v;
      }
    }
    const fmt = (ts) => new Date(ts * 1000).toLocaleString(undefined, data.range === "1h" ? { hour: "2-digit", minute: "2-digit" }
      : data.range === "24h" ? { weekday: "short", hour: "2-digit", minute: "2-digit" } : { month: "short", day: "numeric" });
    [0, Math.floor(n / 2), n - 1].forEach((i, k) => svg.append(s("text", { x: L + i * bw + (k === 2 ? bw : 0), y: H - 5, "text-anchor": ["start", "middle", "end"][k] },
      fmt(data.since + i * data.bucket_seconds + (k === 2 ? data.bucket_seconds : 0)))));
    return svg;
  }
  function niceMax(v) { const p = Math.pow(10, Math.floor(Math.log10(v))); return Math.ceil(v / p / 2) * p * 2 || 1; }

  // ---------- models ----------
  PAGES.models = async (page) => {
    const data = await api("GET", "/api/admin/models");
    FAMILIES = data.families; MODELS = data.models;
    page.append(h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Models" }),
      h("p", { text: "Each model is a name users ask for and the server that answers it. Turn models off, set their context length, limits, guards, and who may use them." })),
      h("div", { class: "actions" }, h("a", { class: "btn", href: "#/discovery" }, icon("radar"), "Find on the cluster"),
        h("button", { class: "btn primary", onclick: () => modelDrawer(null) }, icon("plus"), "Add model"))));
    page.append(table(["On", "Model", "Server", "Family", h("th", { class: "right", text: "Context" }), "Limit per user", "Who", "Guard", "Deployment"],
      data.models.map((m) => h("tr", { class: "click", onclick: () => modelDrawer(m) },
        td(toggle(m.enabled, async (on, input) => {
          if (!on && !(await ask(`Turn ${m.id} off?`, "Requests to it are refused until you turn it on again. Its server keeps running.", "Turn off", true))) { input.checked = true; return; }
          if (await attempt(() => api("PATCH", `/api/admin/models/${enc(m.id)}`, { enabled: on }), on ? `${m.id} is on` : `${m.id} is off`)) render(); else input.checked = !on;
        }, `${m.id} on`)),
        td(h("div", {}, h("b", { text: m.id }), m.description ? h("div", { class: "muted small", text: m.description }) : null), "wrap"),
        td(h("span", { class: "muted", text: host(m.upstream_url) })), td(m.family === "auto" ? m.detected_family : h("span", {}, m.family, " ", h("span", { class: "badge", text: "set" }))),
        td(m.context_length ? num(m.context_length) : "—", "right num"),
        td(m.token_limit === null ? h("span", { class: "muted", text: "none" }) : `${short(m.token_limit)} ${PERIOD[m.period]}`),
        td(m.access === "all" ? "everyone" : h("span", { class: "badge blue", text: `${m.users} chosen` })),
        td(guardSummary(m.guard)), td(m.deployment && m.deployment.name ? h("code", { text: `${m.deployment.namespace}/${m.deployment.name}` }) : "—"))),
      emptyState("No models yet", "Add one by hand, or find the model servers already running on the cluster.",
        h("a", { class: "btn primary", href: "#/discovery" }, "Find on the cluster"))));
  };
  function guardSummary(g) {
    const parts = [];
    if ((g.block || []).length) parts.push(`${g.block.length} block`);
    if (g.redact_secrets) parts.push("secrets");
    if ((g.mask_output || []).length) parts.push("mask");
    if (g.rpm) parts.push(`${g.rpm}/min`);
    if (g.guard_model) parts.push("guard model");
    if (g.system_prompt) parts.push("policy");
    return parts.length ? h("span", { class: "badge amber", text: parts.join(" · ") }) : h("span", { class: "muted", text: "—" });
  }

  function modelDrawer(model, prefill) {
    const isNew = !model;
    const m = model || { id: "", kind: "chat", upstream_url: "", upstream_model: "", enabled: true, context_length: null, max_output_tokens: null,
      access: "all", token_limit: null, period: "month", guard: {}, deployment: {}, metrics_url: "", description: "", family: "auto", ...(prefill || {}) };
    const g = m.guard || {};
    const f = {
      id: text(m.id, { placeholder: "e.g. qwen3-coder", spellcheck: "false" }), description: text(m.description, { placeholder: "What it's good for" }),
      kind: select([["chat", "Chat"], ["embedding", "Embeddings"], ["rerank", "Rerank"], ["other", "Other"]], m.kind),
      upstream_url: text(m.upstream_url, { placeholder: "http://vllm-qwen.vllm.svc:8010/v1", spellcheck: "false" }),
      upstream_model: text(m.upstream_model, { placeholder: "the name its server uses (blank = same)", spellcheck: "false" }),
      upstream_key: h("input", { type: "password", placeholder: m.has_upstream_key ? "Saved — leave empty to keep it" : "Only if its server needs one", autocomplete: "off" }),
      family: select([["auto", "Detect from the name" + (m.detected_family ? ` (${m.detected_family})` : "")], ...FAMILIES.map((x) => [x.key, x.label])], m.family || "auto"),
      metrics_url: text(m.metrics_url, { placeholder: "http://…:8010/metrics (vLLM)", spellcheck: "false" }),
      context_length: numberInput(m.context_length, { placeholder: "from its server" }), max_output_tokens: numberInput(m.max_output_tokens, { placeholder: "no cap" }),
      access: select([["all", "Everyone"], ["selected", "Only users I choose (in Users)"]], m.access),
      token_limit: numberInput(m.token_limit, { placeholder: "no limit" }), period: periodSelect(m.period),
      block: h("textarea", { rows: 4, placeholder: "One per line: name: pattern\nprod database: drop\\s+table", spellcheck: "false" }),
      redact: check("Mask secrets before the model sees them", !!g.redact_secrets, "API keys, tokens, passwords, private keys and connection-string passwords."),
      mask_output: h("textarea", { rows: 3, placeholder: "One pattern per line, masked in answers\ndb-prod-\\d+\\.corp\\.local", spellcheck: "false" }),
      rpm: numberInput(g.rpm, { placeholder: "no limit" }), max_input_chars: numberInput(g.max_input_chars, { placeholder: "no limit" }),
      system_prompt: h("textarea", { rows: 3, placeholder: "Put in front of every conversation, e.g. company rules" }),
      guard_model: select([["", "None"], ...MODELS.filter((x) => x.id !== m.id).map((x) => [x.id, x.id])], g.guard_model || ""),
      policy: text(g.policy || "", { placeholder: "What the guard model should refuse (optional)" }),
      ns: text((m.deployment || {}).namespace || "", { placeholder: "vllm" }), dep: text((m.deployment || {}).name || "", { placeholder: "vllm-qwen" }),
      container: text((m.deployment || {}).container || "", { placeholder: "first container" }),
    };
    f.block.value = (g.block || []).map((b) => (typeof b === "string" ? b : b.name && b.name !== b.pattern ? `${b.name}: ${b.pattern}` : b.pattern)).join("\n");
    f.mask_output.value = (g.mask_output || []).map((p) => (typeof p === "string" ? p : p.pattern)).join("\n");
    f.system_prompt.value = g.system_prompt || "";
    const result = h("div");
    const deploymentBox = h("div", { style: { display: "grid", gap: "12px" } });
    const sections = {
      General: [h("div", { class: "grid2" }, field("Name users ask for", f.id), field("Kind", f.kind)), field("Description", f.description),
        field("Server URL", f.upstream_url, "Its OpenAI-compatible base, ending in /v1."), h("div", { class: "grid2" }, field("Name on its server", f.upstream_model), field("Server API key", f.upstream_key)),
        h("div", { class: "grid2" }, field("Model family", f.family, "How Dcode and the gateway handle its reasoning and tool calls."), field("Metrics URL", f.metrics_url)),
        h("div", { class: "grid2" }, field("Context length (tokens)", f.context_length, "Longer requests are refused; clients get this as the model's window."), field("Max reply (tokens)", f.max_output_tokens)),
        isNew ? null : h("div", { class: "actions" }, h("button", { class: "btn", text: "Test it", onclick: () => testModel(m, result) })), result],
      "Access & limits": [field("Who can use it", f.access), h("div", { class: "grid2" }, field("Tokens per user", f.token_limit, "Each user's own limit on this model; set one user's in Users."), field("Counted", f.period)),
        h("div", { class: "note info", text: "A user's total limit across all models is set on the user (and a default in Settings). Admins can use every model." })],
      Guard: [field("Refuse requests that match", f.block, "Regular expressions, case-insensitive. The name is shown to the user and logged."), f.redact,
        field("Mask in answers", f.mask_output), h("div", { class: "grid2" }, field("Requests per minute, per user", f.rpm), field("Largest request (characters)", f.max_input_chars)),
        field("Policy prompt", f.system_prompt), h("div", { class: "grid2" }, field("Guard model", f.guard_model, "Checks each user message first; “unsafe” refuses it."), field("Guard policy", f.policy))],
      Deployment: [h("p", { class: "muted", style: { margin: 0 }, text: "Link the model to its Kubernetes deployment to start and stop it and change its --max-model-len from here." }),
        h("div", { class: "grid3" }, field("Namespace", f.ns), field("Deployment", f.dep), field("Container", f.container)), deploymentBox],
    };
    const tabs = h("div", { class: "tabs", role: "tablist" });
    const pane = h("div", { style: { display: "grid", gap: "14px" } });
    const show = (name) => {
      [...tabs.children].forEach((b) => b.classList.toggle("on", b.textContent === name));
      pane.replaceChildren(...sections[name].filter(Boolean));
      if (name === "Deployment" && !isNew && m.deployment && m.deployment.name) loadDeployment(m, deploymentBox);
    };
    Object.keys(sections).forEach((name) => tabs.append(h("button", { role: "tab", text: name, onclick: () => show(name) })));
    show("General");
    const values = () => {
      const lines = (el) => el.value.split("\n").map((x) => x.trim()).filter(Boolean);
      const block = lines(f.block).map((line) => { const at = line.indexOf(": "); return at > 0 ? { name: line.slice(0, at), pattern: line.slice(at + 2) } : line; });
      const out = {
        id: f.id.value.trim(), description: f.description.value, kind: f.kind.value, upstream_url: f.upstream_url.value.trim(),
        upstream_model: f.upstream_model.value.trim() || f.id.value.trim(), family: f.family.value, metrics_url: f.metrics_url.value.trim(),
        context_length: intOrNull(f.context_length.value), max_output_tokens: intOrNull(f.max_output_tokens.value), access: f.access.value,
        token_limit: intOrNull(f.token_limit.value), period: f.period.value,
        guard: { block, redact_secrets: f.redact.input.checked, mask_output: lines(f.mask_output), rpm: intOrNull(f.rpm.value),
          max_input_chars: intOrNull(f.max_input_chars.value), system_prompt: f.system_prompt.value.trim(), guard_model: f.guard_model.value, policy: f.policy.value.trim() },
        deployment: { namespace: f.ns.value.trim(), name: f.dep.value.trim(), container: f.container.value.trim() },
      };
      if (f.upstream_key.value) out.upstream_key = f.upstream_key.value;
      if (isNew) out.enabled = true;
      return out;
    };
    const save = h("button", { class: "btn primary", text: isNew ? "Add model" : "Save", onclick: async () => {
      const body = values();
      const done = isNew ? await attempt(() => api("POST", "/api/admin/models", body), `${body.id} added`)
        : await attempt(() => api("PATCH", `/api/admin/models/${enc(m.id)}`, body), "Saved");
      if (done) { closeDrawer(); render(); }
    } });
    const del = isNew ? null : h("button", { class: "btn danger", text: "Delete", onclick: async () => {
      if (!(await ask(`Delete ${m.id}?`, "Users can no longer ask for it. Its usage history stays. The server itself is not touched.", "Delete", true))) return;
      if (await attempt(() => api("DELETE", `/api/admin/models/${enc(m.id)}`), `${m.id} deleted`)) { closeDrawer(); render(); }
    } });
    drawer(isNew ? "Add a model" : m.id, [tabs, pane], [h("div", { class: "left" }, del), h("button", { class: "btn", text: "Cancel", onclick: closeDrawer }), save],
      isNew ? "" : `${host(m.upstream_url)} · ${m.upstream_model}`);
  }
  async function testModel(m, box) {
    box.replaceChildren(h("div", { class: "muted", text: "Asking its server…" }));
    const r = await attempt(() => api("POST", `/api/admin/models/${enc(m.id)}/test`));
    if (!r) { box.replaceChildren(); return; }
    const ok = !r.list_error && (r.status === undefined || r.status === 200);
    box.replaceChildren(h("div", { class: "note " + (ok ? "info" : "bad") }, h("dl", { class: "kv" },
      h("dt", { text: "Model list" }), h("dd", { text: r.list_error ? `failed: ${r.list_error}` : `${r.listed.map((x) => x.id + (x.max_model_len ? ` (${num(x.max_model_len)})` : "")).join(", ") || "empty"} · ${ms(r.list_ms)}` }),
      r.status !== undefined ? [h("dt", { text: "A short chat" }), h("dd", { text: r.status === 200 ? `“${r.answer.trim()}” · ${ms(r.chat_ms)}` : `failed (${r.status}): ${r.error}` })] : null,
      r.server_max_model_len ? [h("dt", { text: "Its server allows" }), h("dd", { text: `${num(r.server_max_model_len)} tokens of context` })] : null)));
  }
  async function loadDeployment(m, box) {
    box.replaceChildren(h("div", { class: "muted", text: "Reading the deployment…" }));
    let d;
    try { d = await api("GET", `/api/admin/models/${enc(m.id)}/deployment`); } catch (err) { box.replaceChildren(h("div", { class: "note bad", text: err.message })); return; }
    const running = d.replicas > 0;
    const len = numberInput(m.context_length || d.args.max_model_len || "", { placeholder: "e.g. 32768" });
    const apply = check("Also restart its server with this --max-model-len", false, "The deployment restarts: the model is unavailable until it has loaded again.");
    box.replaceChildren(
      h("div", { class: "card" }, h("dl", { class: "kv" }, h("dt", { text: "Replicas" }), h("dd", { text: `${d.ready} ready of ${d.replicas}` }),
        h("dt", { text: "Model path" }), h("dd", {}, h("code", { text: d.args.model_path || "—" })),
        h("dt", { text: "--max-model-len" }), h("dd", { text: d.args.max_model_len ? num(d.args.max_model_len) : "not set (the model's own)" }),
        d.args.gpu_memory_utilization ? [h("dt", { text: "GPU memory share" }), h("dd", { text: d.args.gpu_memory_utilization })] : null)),
      h("div", { class: "actions" }, h("button", { class: "btn " + (running ? "danger" : "primary"), text: running ? "Stop the server (0 replicas)" : "Start the server (1 replica)", onclick: async () => {
        const go = await ask(running ? `Stop ${m.id}'s server?` : `Start ${m.id}'s server?`, running
          ? `Scales ${d.link.namespace}/${d.link.name} to 0. Its GPU memory is freed and every request to ${m.id} fails until it is started again.`
          : `Scales ${d.link.namespace}/${d.link.name} to 1. It needs its GPU memory free; loading takes a few minutes.`, running ? "Stop it" : "Start it", running);
        if (go && await attempt(() => api("POST", `/api/admin/models/${enc(m.id)}/scale`, { replicas: running ? 0 : 1 }), running ? "Stopping…" : "Starting…")) setTimeout(() => loadDeployment(m, box), 1500);
      } })),
      h("div", { class: "card", style: { display: "grid", gap: "12px" } }, h("h2", { text: "Context length" }), field("Tokens", len, "The gateway refuses longer requests and tells clients this window."), apply,
        h("div", { class: "actions" }, h("button", { class: "btn", text: "Set", onclick: async () => {
          const value = intOrNull(len.value);
          if (apply.input.checked && !(await ask("Restart the model server?", `${d.link.namespace}/${d.link.name} restarts with --max-model-len ${value}. ${m.id} is unavailable until it has loaded.`, "Restart it", true))) return;
          const r = await attempt(() => api("POST", `/api/admin/models/${enc(m.id)}/max-model-len`, { value, apply: apply.input.checked }), apply.input.checked ? "Saved; the server is restarting" : "Saved");
          if (r) { m.context_length = value; setTimeout(() => loadDeployment(m, box), 1500); }
        } }))));
  }

  // ---------- discovery ----------
  PAGES.discovery = async (page) => {
    page.append(h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Discovery" }),
      h("p", { text: "Model servers running on the cluster and model weights in the models folder. Add a server as a model in one click." })),
      h("div", { class: "actions" }, h("a", { class: "btn", href: "#/settings" }, icon("gear"), "Where to look"), h("button", { class: "btn primary", onclick: () => render() }, icon("refresh"), "Scan again"))));
    const busy = h("div", { class: "card muted", text: "Scanning the cluster…" });
    page.append(busy);
    const [data, models] = await Promise.all([api("GET", "/api/admin/discovery"), api("GET", "/api/admin/models")]);
    FAMILIES = models.families; MODELS = models.models;
    busy.remove();
    page.append(h("div", { class: "muted small", text: `Namespaces: ${data.namespaces.join(", ") || "none"} · models folder: ${data.models_path || "not set"}` }));
    if (data.error) page.append(h("div", { class: "note bad", text: data.error }));
    page.append(h("div", {}, h("h2", { style: { fontSize: "15px", margin: "0 0 10px" }, text: "Model servers" }),
      table(["Deployment", "Image", "Replicas", "Serves", h("th", { class: "right", text: "Context" }), "Address", ""],
        data.servers.map((d) => {
          const served = d.served.length ? d.served : [{ id: d.args.served_name || d.service || d.deployment, max_model_len: d.args.max_model_len, guessed: true }];
          return served.map((sv) => h("tr", {},
            td(h("div", {}, h("b", { text: d.deployment }), h("div", { class: "muted small", text: d.namespace }))), td(h("code", { class: "muted", text: d.image.split("/").pop() })),
            td(d.replicas ? h("span", { class: "badge " + (d.ready ? "green" : "amber"), text: `${d.ready}/${d.replicas}` }) : h("span", { class: "badge", text: "stopped" })),
            td(h("div", {}, h("b", { text: sv.id }), sv.guessed && d.error ? h("div", { class: "muted small", title: d.error, text: "not answering from here" }) : null)),
            td(sv.max_model_len ? num(sv.max_model_len) : "—", "right num"),
            td(d.url ? h("code", { class: "small", title: d.url, text: d.url.replace(/^https?:\/\//, "").replace(/\.svc(:\d+)\/v1$/, "$1") }) : h("span", { class: "muted", text: "no service" })),
            td(d.added.length ? h("span", { class: "badge blue", text: "added as " + d.added.join(", ") }) : h("button", { class: "btn small primary", text: "Add", onclick: () => modelDrawer(null, {
              id: sv.id, upstream_url: d.url, upstream_model: sv.id, context_length: sv.max_model_len || d.args.max_model_len || null,
              description: `${d.namespace}/${d.deployment}`, metrics_url: d.url ? d.url.replace(/\/v1$/, "/metrics") : "",
              deployment: { namespace: d.namespace, name: d.deployment, container: d.container } }) }))));
        }), emptyState("No model servers found", data.error ? "Fix the connection above first." : "Check the namespaces in Settings."))));
    page.append(h("div", {}, h("h2", { style: { fontSize: "15px", margin: "0 0 10px" }, text: "Weights in the models folder" }),
      data.weights_error ? h("div", { class: "note", text: data.weights_error }) : null,
      table(["Model", "Format", h("th", { class: "right", text: "Size" }), "Architecture", h("th", { class: "right", text: "Trained context" }), "Path", ""],
        data.weights.map((w) => h("tr", {}, td(h("b", { text: w.name })), td(w.format), td(short(w.size) + "B", "right num"), td(w.architecture || "—"),
          td(w.context ? num(w.context) : "—", "right num"), td(h("code", { class: "muted small", text: w.path })),
          td(w.served ? h("span", { class: "badge green", text: "served" }) : h("span", { class: "badge", text: "not served" })))),
        emptyState("Nothing here", data.models_path ? "No model folders (a config.json with weights) or .gguf files." : "Set the models folder in Settings, mounted into the manager's pod."))));
  };

  // ---------- users ----------
  PAGES.users = async (page) => {
    const [data, models] = await Promise.all([api("GET", "/api/admin/users"), api("GET", "/api/admin/models")]);
    MODELS = models.models;
    page.append(h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Users" }),
      h("p", { text: "Who can sign in, which models they may use, and how many tokens. Reset someone's usage when they need more before the period ends." })),
      h("div", { class: "actions" }, h("button", { class: "btn primary", onclick: () => newUser() }, icon("plus"), "Add user"))));
    page.append(table(["User", "Role", "Status", "Usage (all models)", "Keys", "Last seen"], data.users.map((u) => h("tr", { class: "click", onclick: () => userDrawer(u.id) },
      td(h("div", {}, h("b", { text: u.username }), u.name ? h("div", { class: "muted small", text: u.name }) : null)),
      td(h("span", { class: "badge " + (u.role === "admin" ? "blue" : ""), text: u.role })),
      td(u.enabled ? h("span", { class: "badge green", text: "active" }) : h("span", { class: "badge red", text: "disabled" })),
      td(h("div", { style: { display: "grid", gap: "4px", minWidth: "170px" } }, h("span", { class: "small num", text: u.usage.limit === null ? `${short(u.usage.used)} tokens · no limit` : `${short(u.usage.used)} of ${short(u.usage.limit)} ${PERIOD[u.usage.period]}` }), usageBar(u.usage.used, u.usage.limit))),
      td(num(u.keys), "num"), td(when(u.last_seen)))), emptyState("No users")));
  };
  function newUser() {
    const f = { username: text("", { autocomplete: "off", spellcheck: "false" }), name: text(""), password: text(randomPassword(), { autocomplete: "off" }),
      role: select([["user", "User"], ["admin", "Admin"]], "user"), limit: numberInput("", { placeholder: "the default from Settings" }), period: periodSelect("", "the default from Settings") };
    drawer("Add a user", [h("div", { class: "grid2" }, field("Username", f.username, "One word; what they sign in with."), field("Full name", f.name)),
      field("Password", f.password, "Give it to them; they can change it after signing in."), field("Role", f.role, "Admins manage models and users, and can use every model."),
      h("div", { class: "grid2" }, field("Total tokens (all models)", f.limit), field("Counted", f.period))],
    [h("button", { class: "btn", text: "Cancel", onclick: closeDrawer }), h("button", { class: "btn primary", text: "Add user", onclick: async () => {
      const body = { username: f.username.value.trim(), name: f.name.value, password: f.password.value, role: f.role.value };
      if (f.limit.value !== "") body.token_limit = intOrNull(f.limit.value);
      if (f.period.value) body.period = f.period.value;
      const r = await attempt(() => api("POST", "/api/admin/users", body), `${body.username} added`);
      if (r) { closeDrawer(); render(); }
    } })]);
  }
  function randomPassword() {
    const bytes = new Uint8Array(12); crypto.getRandomValues(bytes);
    return Array.from(bytes, (b) => "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"[b % 56]).join("");
  }
  async function userDrawer(userId) {
    const data = await attempt(() => api("GET", `/api/admin/users/${userId}/access`));
    if (!data) return;
    const u = data.user;
    const f = { name: text(u.name), role: select([["user", "User"], ["admin", "Admin"]], u.role), password: text("", { placeholder: "leave empty to keep", autocomplete: "off" }),
      limit: numberInput(u.token_limit, { placeholder: "the default from Settings" }), period: periodSelect(u.period || "", "the default from Settings") };
    const enabled = check("Can sign in and use models", u.enabled);
    const rows = data.access.map((a) => {
      const own = check("", a.explicit);
      const allowed = h("input", { type: "checkbox", checked: a.explicit ? a.allowed : a.allowed, "aria-label": `${a.model} allowed` });
      const limit = numberInput(a.own_limit, { placeholder: a.model_limit === null ? "no limit" : short(a.model_limit), style: { width: "110px", height: "30px" } });
      const period = periodSelect(a.own_period || "", "model's");
      Object.assign(period.style, { height: "30px", width: "130px" });
      const sync = () => { [allowed, limit, period].forEach((el) => { el.disabled = !own.input.checked; }); };
      own.input.addEventListener("change", sync); sync();
      const row = h("tr", {}, td(h("div", {}, h("b", { text: a.model }), h("div", { class: "muted small", text: (a.model_access === "all" ? "everyone" : "chosen users") + (a.enabled ? "" : " · off") }))),
        td(own), td(allowed), td(limit), td(period),
        td(h("div", { style: { display: "grid", gap: "3px", minWidth: "120px" } }, h("span", { class: "small num", text: a.usage.limit === null ? `${short(a.usage.used)}` : `${short(a.usage.used)} / ${short(a.usage.limit)}` }), usageBar(a.usage.used, a.usage.limit))));
      row.read = () => [a.model, own.input.checked ? { allowed: allowed.checked, token_limit: intOrNull(limit.value), period: period.value || null } : null];
      return row;
    });
    const total = data.total;
    drawer(u.username, [
      h("div", { class: "card usage" }, h("div", { class: "top" }, h("b", { text: "All models" }), h("span", { class: "badge", text: total.limit === null ? "no limit" : PERIOD[total.period] })),
        h("div", { class: "big" }, short(total.used), h("small", { text: total.limit === null ? " tokens" : ` of ${short(total.limit)}` })), usageBar(total.used, total.limit),
        h("div", { class: "foot" }, h("span", { text: total.resets_at ? "Resets " + until(total.resets_at) : total.limit !== null ? "Resets only when you reset it" : "" }),
          h("button", { class: "btn small", text: "Reset usage now", onclick: async () => {
            if (!(await ask(`Reset ${u.username}'s usage?`, "Their token counts start from zero now, on every model. The history stays in Monitoring.", "Reset"))) return;
            if (await attempt(() => api("POST", `/api/admin/users/${u.id}/reset-usage`), "Usage reset")) userDrawer(u.id);
          } }))),
      h("div", { class: "grid2" }, field("Full name", f.name), field("Role", f.role)), enabled,
      h("div", { class: "grid2" }, field("Total tokens (all models)", f.limit), field("Counted", f.period)),
      field("New password", f.password, "Signs them out of the web UI. Their Dcode keys keep working; revoke those in Monitoring → Devices if needed."),
      h("div", {}, h("h2", { style: { fontSize: "15px", margin: "6px 0 4px" }, text: "Models" }),
        h("p", { class: "muted small", style: { margin: "0 0 8px" }, text: "Tick “Own rules” to allow or deny a model for this user, or give them their own limit. Otherwise the model's settings apply." }),
        table(["Model", "Own rules", "Allowed", "Tokens", "Counted", "Used"], rows, emptyState("No models yet")))],
    [h("div", { class: "left" }, h("button", { class: "btn danger", text: "Delete user", onclick: async () => {
      if (!(await ask(`Delete ${u.username}?`, "They can't sign in and their keys stop working. Their usage history stays.", "Delete", true))) return;
      if (await attempt(() => api("DELETE", `/api/admin/users/${u.id}`), `${u.username} deleted`)) { closeDrawer(); render(); }
    } })), h("button", { class: "btn", text: "Cancel", onclick: closeDrawer }), h("button", { class: "btn primary", text: "Save", onclick: async () => {
      const body = { name: f.name.value, role: f.role.value, enabled: enabled.input.checked, token_limit: intOrNull(f.limit.value), period: f.period.value || null };
      if (f.password.value) body.password = f.password.value;
      const ok = await attempt(() => api("PATCH", `/api/admin/users/${u.id}`, body));
      if (!ok) return;
      const access = Object.fromEntries(rows.map((r) => r.read()));
      if (await attempt(() => api("PUT", `/api/admin/users/${u.id}/access`, access), "Saved")) { closeDrawer(); render(); }
    } })], u.name || (u.role === "admin" ? "Admin" : "User"));
  }

  // ---------- requests ----------
  PAGES.requests = async (page) => {
    const params = new URLSearchParams((location.hash.split("?")[1]) || "");
    const [data, users, models] = await Promise.all([api("GET", `/api/admin/requests?limit=200&${params}`), api("GET", "/api/admin/users"), api("GET", "/api/admin/models")]);
    const go = (key, value) => { if (value) params.set(key, value); else params.delete(key); location.hash = "#/requests?" + params; };
    const userSel = select([["", "Everyone"], ...users.users.map((u) => [u.username, u.username])], params.get("user") || "", { onchange: (e) => go("user", e.target.value), style: { width: "170px" } });
    const modelSel = select([["", "Every model"], ...models.models.map((m) => [m.id, m.id])], params.get("model") || "", { onchange: (e) => go("model", e.target.value), style: { width: "190px" } });
    const problems = check("Only problems", !!params.get("problems"));
    problems.input.addEventListener("change", () => go("problems", problems.input.checked ? "1" : ""));
    page.append(h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Requests" }), h("p", { text: "The latest requests through the gateway, newest first." })),
      h("div", { class: "actions" }, userSel, modelSel, problems)));
    page.append(table(["When", "User", "Model", "Status", h("th", { class: "right", text: "In" }), h("th", { class: "right", text: "Out" }),
      h("th", { class: "right", text: "Time" }), h("th", { class: "right", text: "First token" }), "Client", "Note"],
    data.requests.map((r) => h("tr", {}, td(when(r.ts)), td(r.username || (r.endpoint === "guard" ? "guard" : "—")), td(h("span", {}, r.model_id, r.endpoint !== "chat" ? h("span", { class: "badge", text: r.endpoint }) : null)),
      td(h("span", { class: "badge " + (r.status === 200 ? "green" : r.status >= 500 ? "red" : "amber"), text: String(r.status) })),
      td(num(r.prompt_tokens) + (r.estimated ? "~" : ""), "right num"), td(num(r.completion_tokens) + (r.estimated ? "~" : ""), "right num"),
      td(ms(r.latency_ms), "right num"), td(ms(r.ttft_ms), "right num"), td(h("span", { class: "muted small", text: r.client || "" })),
      td(h("span", { class: "muted small", text: [r.guard, r.error].filter(Boolean).join(" · ").slice(0, 160) }), "wrap"))),
    emptyState("No requests yet")));
    page.append(h("div", { class: "muted small", text: "~ = estimated from the text length (the server sent no token counts)." }));
  };

  // ---------- settings ----------
  PAGES.settings = async (page) => {
    const [st, audit] = await Promise.all([api("GET", "/api/admin/settings"), api("GET", "/api/admin/audit")]);
    const zones = (Intl.supportedValuesOf && Intl.supportedValuesOf("timeZone")) || ["UTC", "Asia/Tehran", "Europe/Berlin", "America/New_York"];
    const f = { name: text(st.name), tz: text(st.timezone, { list: "dm-zones" }), limit: numberInput(st.default_user_limit, { placeholder: "no limit" }),
      period: periodSelect(st.default_user_period), hours: numberInput(st.session_hours), ns: text((st.discovery_namespaces || []).join(", ")),
      path: text(st.models_path, { placeholder: "/models (mounted into the pod)" }), kube: text(st.kubernetes_api, { placeholder: "empty = in-cluster" }) };
    page.append(h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Settings" }), h("p", { text: "The manager's name, when usage periods start, defaults for new users, and where to find models." }))));
    page.append(h("datalist", { id: "dm-zones" }, zones.map((z) => h("option", { value: z }))));
    page.append(h("div", { class: "cols" },
      h("div", { class: "card", style: { display: "grid", gap: "14px" } }, h("h2", { text: "General" }),
        h("div", { class: "grid2" }, field("Name", f.name, "Shown in the UI and to Dcode when people sign in."), field("Time zone", f.tz, "Days, weeks and months start here.")),
        h("div", { class: "grid2" }, field("Default tokens per user (all models)", f.limit), field("Counted", f.period)),
        field("Web sign-in lasts (hours)", f.hours),
        h("h2", { style: { marginTop: "6px" }, text: "Discovery" }),
        field("Namespaces to search", f.ns, "Comma-separated, e.g. vllm, ai-models."), field("Models folder", f.path, "A folder of model weights the manager can read."),
        field("Kubernetes API", f.kube, "Leave empty in the cluster. Elsewhere, a `kubectl proxy` URL such as http://127.0.0.1:8001."),
        h("div", { class: "actions" }, h("button", { class: "btn primary", text: "Save settings", onclick: async () => {
          const body = { name: f.name.value, timezone: f.tz.value.trim(), default_user_limit: intOrNull(f.limit.value), default_user_period: f.period.value,
            session_hours: intOrNull(f.hours.value) || 12, discovery_namespaces: f.ns.value, models_path: f.path.value, kubernetes_api: f.kube.value };
          const saved = await attempt(() => api("PUT", "/api/admin/settings", body), "Settings saved");
          if (saved) { S.info.name = saved.name; render(); }
        } }))),
      h("div", { style: { display: "grid", gap: "16px" } },
        h("div", { class: "card", style: { display: "grid", gap: "10px" } }, h("h2", { text: "Reset everyone's usage" }),
          h("p", { class: "muted", style: { margin: 0 }, text: `Every user's token counts start from zero now. Last done: ${when(st.global_reset_at)}.` }),
          h("div", {}, h("button", { class: "btn danger", text: "Reset everyone's usage", onclick: async () => {
            if (!(await ask("Reset everyone's usage?", "All users start from zero on every model and in total. The history stays in Monitoring.", "Reset everyone", true))) return;
            if (await attempt(() => api("POST", "/api/admin/reset-usage"), "Everyone's usage was reset")) render();
          } }))),
        h("div", {}, h("h2", { style: { fontSize: "15px", margin: "0 0 10px" }, text: "Changes" }),
          table(["When", "Who", "What"], audit.audit.slice(0, 40).map((a) => h("tr", {}, td(when(a.ts)), td(a.username || "—"),
            td(h("span", {}, h("b", { text: a.action.replace(/_/g, " ") }), " ", h("span", { class: "muted small", text: a.detail.slice(0, 120) })), "wrap"))), emptyState("No changes yet"))))));
  };
};
