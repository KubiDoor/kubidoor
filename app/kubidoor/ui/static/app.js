"use strict";
/* KubiDoor dashboard. Plain JavaScript, no build step, no external requests.
   All data is rendered with textContent (never innerHTML), so cluster-controlled strings
   such as pod names cannot inject markup. */

/* ---------------- DOM helpers ---------------- */
const SVG_NS = "http://www.w3.org/2000/svg";

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v; // CSSOM is allowed by the CSP; style attributes are not
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  append(el, kids);
  return el;
}
function append(el, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
}
const ICONS = {
  overview: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  incidents: "M12 3l10 18H2zM12 10v5M12 18v.01",
  recovery: "M4 12a8 8 0 0 1 14-5l2 2M20 4v5h-5M20 12a8 8 0 0 1-14 5l-2-2M4 20v-5h5",
  cluster: "M4 5h16v5H4zM4 14h16v5H4zM8 7.5h.01M8 16.5h.01",
  moon: "M20 14A8 8 0 0 1 10 4a8 8 0 1 0 10 10z",
  sun: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  close: "M6 6l12 12M18 6L6 18",
};
function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(SVG_NS, "path");
  p.setAttribute("d", ICONS[name]);
  svg.append(p);
  return svg;
}
const $ = (sel) => document.querySelector(sel);

/* ---------------- state & storage ---------------- */
function store(kind, key, value) {
  try {
    const s = kind === "session" ? window.sessionStorage : window.localStorage;
    if (value === undefined) return s.getItem(key);
    if (value === null) s.removeItem(key);
    else s.setItem(key, value);
  } catch (_) { /* storage can be unavailable (private mode); the UI still works */ }
  return null;
}
const state = {
  token: store("session", "kubidoor.token") || "",
  actor: store("local", "kubidoor.actor") || "dashboard",
  theme: store("local", "kubidoor.theme"), // "light" | "dark" | null (follow system)
  live: true,
  policy: null,
  incFilter: { status: "active", severity: "", namespace: "", limit: 50 },
  recFilter: "all",
  podFilter: { namespace: "", unhealthy: false },
  seq: 0,
  busy: false,
};
const ACTOR_RE = /^[A-Za-z0-9_.@:-]{1,64}$/;

/* ---------------- API ---------------- */
class ApiError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}
async function api(path, opts = {}) {
  const headers = { Accept: "application/json", "X-KubiDoor-Actor": state.actor };
  if (state.token) headers.Authorization = "Bearer " + state.token;
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch("/api/v1" + path, {
      method: opts.method || "GET", headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
  } catch (_) {
    throw new ApiError(0, "network", "Can't reach the KubiDoor server. Check that it is still running.");
  }
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    const e = data && data.error;
    throw new ApiError(res.status, e && e.code, (e && e.message) || res.statusText || "Request failed");
  }
  return data;
}
const post = (path, body) => api(path, { method: "POST", body: body === undefined ? {} : body });

/* ---------------- formatting ---------------- */
const SEV = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"];
const sevRank = (s) => { const i = SEV.indexOf(s); return i < 0 ? 99 : i; };
const sentence = (s) => (s ? s.charAt(0) + s.slice(1).toLowerCase().replaceAll("_", " ") : "");
const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;

function ago(iso) {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
const when = (iso) => (iso ? new Date(iso).toLocaleString() : "-");
function timeEl(iso) { return h("time", { datetime: iso || null, title: when(iso) }, ago(iso)); }
function gib(bytes) { return (bytes / 2 ** 30).toFixed(1) + " GiB"; }
function target(x) { return `${x.namespace ? x.namespace + "/" : ""}${x.resource_name}`; }

const sevBadge = (s) => h("span", { class: "sev " + s }, h("i"), sentence(s));
const statusEl = (s) => h("span", { class: "status " + s }, sentence(s));

function empty(title, text, action) {
  return h("div", { class: "empty-state" }, h("h3", {}, title), h("p", {}, text), action || null);
}
function notice(kind, title, text, ...extra) {
  return h("div", { class: "notice " + kind }, h("div", {}, h("b", {}, title), text, ...extra));
}
function skeleton() {
  return h("div", { class: "panel stack" }, h("div", { class: "skeleton", style: "width:40%" }),
    h("div", { class: "skeleton" }), h("div", { class: "skeleton", style: "width:80%" }));
}
function errorView(err, retry) {
  if (err.status === 401) {
    return notice("warn", "This server needs an access token",
      "Enter the token set in KUBIDOOR_API_TOKEN to see cluster data. ",
      h("button", { class: "link", type: "button", onclick: () => askIdentity() }, "Enter token"));
  }
  return notice("bad", "Couldn't load this page", err.message + " ",
    retry ? h("button", { class: "link", type: "button", onclick: retry }, "Try again") : null);
}

/* ---------------- toasts & dialogs ---------------- */
function toast(message, isError) {
  const t = h("div", { class: "toast" + (isError ? " err" : "") }, message);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), isError ? 7000 : 3500);
}
function ask({ title, text, fields = [], confirm = "Confirm", danger = false, cancel = "Cancel" }) {
  return new Promise((resolve) => {
    const dlg = $("#dlg");
    const inputs = {};
    const form = h("div", { class: "form" });
    for (const f of fields) {
      const input = f.type === "textarea"
        ? h("textarea", { id: "f-" + f.name, placeholder: f.placeholder || "", maxlength: f.maxlength || null })
        : h("input", { id: "f-" + f.name, type: f.type || "text", value: f.value || "", placeholder: f.placeholder || "", autocomplete: "off", maxlength: f.maxlength || null });
      if (f.type === "textarea") input.value = f.value || "";
      inputs[f.name] = input;
      form.append(h("label", { class: "field", for: "f-" + f.name }, f.label, input));
    }
    const done = (ok) => {
      const values = {};
      for (const [k, el] of Object.entries(inputs)) values[k] = el.value;
      dlg.close();
      dlg.replaceChildren();
      resolve(ok ? values : null);
    };
    const okBtn = h("button", { class: "btn " + (danger ? "danger" : "primary"), type: "button", onclick: () => done(true) }, confirm);
    dlg.replaceChildren(
      h("h2", {}, title), text ? h("p", {}, text) : null, form,
      h("div", { class: "row" }, h("button", { class: "btn", type: "button", onclick: () => done(false) }, cancel), okBtn),
    );
    dlg.oncancel = (e) => { e.preventDefault(); done(false); };
    dlg.onkeydown = (e) => { if (e.key === "Enter" && e.target.tagName === "INPUT") { e.preventDefault(); done(true); } };
    dlg.showModal();
    (Object.values(inputs)[0] || okBtn).focus();
  });
}
async function askIdentity() {
  const v = await ask({
    title: "Your name and access token",
    text: "Your name is written to the audit log next to every action you take. It isn't verified. The token stays in this browser tab only.",
    fields: [
      { name: "actor", label: "Your name", value: state.actor, maxlength: 64 },
      { name: "token", label: "Access token (leave empty if the server has none)", type: "password", value: state.token },
    ],
    confirm: "Save",
  });
  if (!v) return;
  const actor = v.actor.trim();
  if (!ACTOR_RE.test(actor)) { toast("Use letters, numbers and . _ @ : - only (up to 64 characters).", true); return; }
  state.actor = actor; state.token = v.token.trim();
  store("local", "kubidoor.actor", actor);
  store("session", "kubidoor.token", state.token || null);
  renderChrome();
  refresh({ manual: true });
}

/* ---------------- shell ---------------- */
const NAV = [
  { id: "overview", label: "Overview", icon: "overview" },
  { id: "incidents", label: "Incidents", icon: "incidents" },
  { id: "recovery", label: "Recovery", icon: "recovery" },
  { id: "cluster", label: "Cluster", icon: "cluster" },
];
const badges = { incidents: 0, recovery: 0 };

function renderNav(current) {
  $("#nav").replaceChildren(...NAV.map((n) => h("a", { href: "#/" + n.id, "aria-current": n.id === current ? "page" : null },
    icon(n.icon), h("span", { class: "label" }, n.label),
    badges[n.id] ? h("span", { class: "badge", "aria-label": `${badges[n.id]} waiting` }, badges[n.id]) : null)));
}
function effectiveTheme() {
  return state.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
function renderChrome() {
  if (state.theme) document.documentElement.dataset.theme = state.theme;
  else delete document.documentElement.dataset.theme;
  const dark = effectiveTheme() === "dark";
  $("#themeBtn").replaceChildren(icon(dark ? "sun" : "moon"), h("span", {}, dark ? "Light theme" : "Dark theme"));
  $("#refreshBtn").replaceChildren(icon("refresh"));
  $("#whoBtn").textContent = state.actor;
  $("#liveBtn").setAttribute("aria-pressed", String(state.live));
}
function setHeader(title, sub) {
  $("#pageTitle").textContent = title;
  $("#pageSub").textContent = sub || "";
  document.title = title + " - KubiDoor";
}

async function updatePills() {
  const pills = [];
  try {
    const res = await fetch("/ready", { headers: { Accept: "application/json" } });
    const d = await res.json();
    const k = d.checks && d.checks.kubernetes;
    pills.push(h("span", { class: "pill " + (k ? "ok" : "bad"), title: k ? "" : d.kubernetes_error || "" }, h("i"), k ? "Cluster " + d.kubernetes_version : "Cluster unreachable"));
    if (d.checks && d.checks.database === false) pills.push(h("span", { class: "pill bad" }, h("i"), "Database error"));
  } catch (_) {
    pills.push(h("span", { class: "pill bad" }, h("i"), "Server unreachable"));
  }
  try {
    state.policy = await api("/recovery/policy");
    const p = state.policy;
    let text = "Recovery off", cls = "";
    if (p.enabled) { text = p.dry_run ? "Recovery: dry run" : "Recovery: live"; cls = p.dry_run ? "brass" : "warn"; }
    pills.push(h("span", { class: "pill " + cls }, h("i"), text));
  } catch (_) { /* 401 shows on the page itself */ }
  $("#pills").replaceChildren(...pills);
}
async function updateBadges() {
  try {
    const counts = await Promise.all(["OPEN", "ACKNOWLEDGED", "INVESTIGATING"].map((s) => api(`/incidents?status=${s}&limit=1`)));
    badges.incidents = counts.reduce((n, p) => n + p.total, 0);
    badges.recovery = (await api("/recovery/actions?status=PENDING_APPROVAL&limit=1")).total;
  } catch (_) { /* leave previous values */ }
}

/* ---------------- shared data ---------------- */
async function fetchIncidents(f) {
  const q = (extra) => {
    const p = new URLSearchParams({ limit: String(f.limit), ...extra });
    if (f.severity) p.set("severity", f.severity);
    if (f.namespace) p.set("namespace", f.namespace);
    return api("/incidents?" + p);
  };
  let pages;
  if (f.status === "active") pages = await Promise.all(["OPEN", "ACKNOWLEDGED", "INVESTIGATING"].map((s) => q({ status: s })));
  else if (f.status === "all") pages = [await q({})];
  else pages = [await q({ status: f.status.toUpperCase() })];
  const items = pages.flatMap((p) => p.items);
  items.sort((a, b) => sevRank(a.severity) - sevRank(b.severity) || new Date(b.last_detected_at) - new Date(a.last_detected_at));
  return { items, total: pages.reduce((n, p) => n + p.total, 0) };
}

/* ---------------- threshold strip (overview) ---------------- */
function strip(parts) {
  const live = parts.filter((p) => p.value > 0);
  const bar = h("div", { class: "strip" + (live.length ? "" : " empty"), role: "img", "aria-label": parts.map((p) => `${p.value} ${p.label}`).join(", ") });
  live.forEach((p, i) => {
    const seg = h("span", { class: "seg " + p.tone });
    seg.style.setProperty("--w", String(p.value));
    seg.style.setProperty("--i", String(i));
    bar.append(seg);
  });
  const legend = h("ul", { class: "legend" }, ...parts.map((p) => h("li", {}, h("i", { class: p.tone }), h("b", { class: "num" }, p.value), " " + p.label)));
  return h("div", {}, bar, legend);
}
function stripRow(name, sub, parts) {
  return h("div", { class: "strip-row" }, h("div", { class: "strip-label" }, h("b", {}, name), h("span", {}, sub)), strip(parts));
}

/* ---------------- views ---------------- */
async function viewOverview(seq) {
  const [ov, active, pending] = await Promise.allSettled([
    api("/cluster/overview"),
    fetchIncidents({ status: "active", severity: "", namespace: "", limit: 100 }),
    api("/recovery/actions?status=PENDING_APPROVAL&limit=100"),
  ]);
  if (seq !== state.seq) return null;
  setHeader("Overview", "Health of your cluster right now, and what needs a person.");
  const root = h("div", { class: "stack" });

  if (active.status === "rejected") return errorView(active.reason, () => refresh({ manual: true }));
  const inc = active.value;
  const crit = inc.items.filter((i) => i.severity === "CRITICAL" || i.severity === "HIGH").length;

  let verdict, sub;
  if (ov.status === "rejected") {
    verdict = "Can't see the cluster";
    sub = ov.reason.message + (inc.total ? " Incidents below are the last known state." : "");
  } else if (inc.total === 0) {
    const o = ov.value;
    verdict = "All quiet";
    sub = `No active incidents. ${plural(o.pods.data ? o.pods.data.total : 0, "pod")} on ${plural(o.nodes.data ? o.nodes.data.total : 0, "node")} checked, Kubernetes ${o.kubernetes_version}.`;
  } else {
    verdict = `${plural(inc.total, "active incident")}`;
    sub = crit ? `${plural(crit, "of them is", "of them are")} critical or high severity.` : "None are critical or high severity.";
  }

  const strips = h("div", { class: "strips" });
  if (ov.status === "fulfilled") {
    const o = ov.value;
    if (o.nodes.available) {
      const d = o.nodes.data;
      strips.append(stripRow("Nodes", plural(d.total, "node"), [
        { label: "ready", value: d.ready, tone: "ok" }, { label: "not ready", value: d.not_ready, tone: "bad" }]));
    } else strips.append(unavailableRow("Nodes", o.nodes.error));
    if (o.pods.available) {
      const d = o.pods.data;
      strips.append(stripRow("Pods", plural(d.total, "pod"), [
        { label: "running", value: d.running || 0, tone: "ok" },
        { label: "completed", value: d.succeeded || 0, tone: "idle" },
        { label: "pending", value: d.pending || 0, tone: "warn" },
        { label: "failed", value: d.failed || 0, tone: "bad" },
        { label: "unknown", value: d.unknown || 0, tone: "bad" }]));
    } else strips.append(unavailableRow("Pods", o.pods.error));
    if (o.deployments.available) {
      const d = o.deployments.data;
      strips.append(stripRow("Deployments", plural(d.total, "deployment"), [
        { label: "fully available", value: d.fully_available, tone: "ok" }, { label: "degraded", value: d.degraded, tone: "bad" }]));
    } else strips.append(unavailableRow("Deployments", o.deployments.error));
  }
  root.append(h("section", { class: "panel threshold", "aria-labelledby": "verdict" },
    h("div", { class: "threshold-head" }, h("p", { class: "verdict", id: "verdict" }, verdict, h("small", {}, sub))),
    strips));

  const counts = Object.fromEntries(SEV.map((s) => [s, inc.items.filter((i) => i.severity === s).length]));
  root.append(h("div", { class: "sevcount" }, ...SEV.map((s) => h("button", {
    type: "button", class: counts[s] ? "" : "zero",
    onclick: () => { state.incFilter = { ...state.incFilter, status: "active", severity: s, namespace: "" }; location.hash = "#/incidents"; },
  }, h("strong", {}, counts[s]), h("span", { class: "sev " + s }, h("i"), sentence(s))))));

  const left = h("section", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", {}, "Needs attention"),
    h("a", { href: "#/incidents" }, "All incidents")));
  if (inc.items.length === 0) left.append(empty("Nothing needs you", "KubiDoor checks the cluster every few seconds and will list problems here as soon as it finds one."));
  else left.append(h("ul", { class: "attn" }, ...inc.items.slice(0, 6).map((i) => h("li", {},
    h("a", { href: "#/incidents/" + i.id },
      h("span", { class: "t" }, i.title),
      h("span", { class: "m" }, sevBadge(i.severity), h("span", { class: "mono" }, `${i.resource_type} ${target(i)}`), h("span", {}, "first seen ", ago(i.first_detected_at)), statusEl(i.status)))))));

  const right = h("div", { class: "stack" });
  const pend = pending.status === "fulfilled" ? pending.value : null;
  const pa = h("section", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", {}, "Recovery"), h("a", { href: "#/recovery" }, "Open")));
  if (state.policy && !state.policy.enabled) pa.append(h("p", { class: "muted", style: "margin:0" }, "Recovery is off. KubiDoor observes and explains problems but never changes the cluster."));
  else if (pend && pend.total) pa.append(h("div", { class: "metric" }, h("strong", {}, pend.total), h("span", {}, `${plural(pend.total, "action")} waiting for your approval`)));
  else pa.append(h("p", { class: "muted", style: "margin:0" }, "No actions are waiting for approval."));
  right.append(pa);

  const usage = h("section", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", {}, "Resource usage")));
  if (ov.status === "fulfilled" && ov.value.resource_metrics.available) {
    const d = ov.value.resource_metrics.data;
    usage.append(h("div", { class: "grid", style: "grid-template-columns:1fr 1fr" },
      h("div", { class: "metric" }, h("strong", {}, (d.cpu_millicores_total / 1000).toFixed(2)), h("span", {}, "CPU cores in use")),
      h("div", { class: "metric" }, h("strong", {}, gib(d.memory_bytes_total)), h("span", {}, `memory in use, ${plural(d.nodes_reporting, "node")} reporting`))));
  } else {
    const why = ov.status === "fulfilled" ? ov.value.resource_metrics.error : "The cluster is unreachable.";
    usage.append(h("p", { class: "muted", style: "margin:0" }, "Not available. ", why || "", " KubiDoor never estimates usage; install metrics-server to see it here."));
  }
  right.append(usage);
  root.append(h("div", { class: "grid cols-2" }, left, right));
  return root;
}
function unavailableRow(name, error) {
  return h("div", { class: "strip-row" }, h("div", { class: "strip-label" }, h("b", {}, name), h("span", {}, "unavailable")),
    h("div", { class: "muted" }, error || "Could not be read from the cluster."));
}

/* ----- incidents ----- */
const STATUS_TABS = [["active", "Active"], ["open", "Open"], ["acknowledged", "Acknowledged"], ["investigating", "Investigating"], ["resolved", "Resolved"], ["all", "All"]];

async function viewIncidents(seq, route) {
  const f = state.incFilter;
  setHeader("Incidents", "Problems KubiDoor found, newest and most severe first. Select one to see why it happened.");
  let result;
  try { result = await fetchIncidents(f); } catch (e) { return errorView(e, () => refresh({ manual: true })); }
  if (seq !== state.seq) return null;
  const rerender = () => refresh({ manual: true });

  const toolbar = h("div", { class: "toolbar" },
    h("div", { class: "seg-ctl", role: "group", "aria-label": "Status" }, ...STATUS_TABS.map(([id, label]) => h("button", {
      type: "button", "aria-pressed": String(f.status === id), onclick: () => { f.status = id; f.limit = 50; rerender(); } }, label))),
    h("select", { "aria-label": "Severity", onchange: (e) => { f.severity = e.target.value; rerender(); } },
      h("option", { value: "" }, "Any severity"), ...SEV.map((s) => h("option", { value: s, selected: f.severity === s ? true : null }, sentence(s)))),
    h("input", { type: "search", placeholder: "Namespace", "aria-label": "Namespace", value: f.namespace, style: "width:150px",
      onchange: (e) => { f.namespace = e.target.value.trim(); rerender(); } }),
    h("span", { class: "spacer" }),
    h("span", { class: "muted" }, `${result.total} ${result.total === 1 ? "incident" : "incidents"}`));

  const root = h("div", {}, toolbar);
  if (!result.items.length) {
    root.append(h("div", { class: "panel" }, empty(f.status === "resolved" ? "No resolved incidents yet" : "No incidents match",
      f.status === "active" ? "Nothing is wrong right now. Try the Resolved tab to see what was fixed." : "Change the status or severity filter to widen the list.")));
  } else {
    const rows = result.items.map((i) => h("tr", { class: `clickable s-${i.severity}`, tabindex: "0", onclick: () => { location.hash = "#/incidents/" + i.id; },
      onkeydown: (e) => { if (e.key === "Enter") location.hash = "#/incidents/" + i.id; } },
      h("td", { class: "sevcell" }, sevBadge(i.severity)),
      h("td", {}, h("div", { class: "cell-title" }, i.title), h("div", { class: "cell-sub mono" }, `${i.resource_type} ${i.resource_name}`)),
      h("td", {}, i.namespace || h("span", { class: "muted" }, "cluster-wide")),
      h("td", {}, statusEl(i.status)),
      h("td", { class: "num" }, timeEl(i.last_detected_at), h("div", { class: "cell-sub" }, `seen ${i.occurrence_count}×`))));
    root.append(h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Severity"), h("th", {}, "Problem"), h("th", {}, "Namespace"), h("th", {}, "Status"), h("th", {}, "Last seen"))),
      h("tbody", {}, ...rows))));
    if (result.items.length < result.total && f.limit < 500) root.append(h("p", {}, h("button", { class: "btn", type: "button", onclick: () => { f.limit = Math.min(500, f.limit * 2); rerender(); } }, "Show more")));
  }
  return root;
}

/* ----- incident drawer ----- */
const drawer = { id: null, el: null, body: null, foot: null, head: null };

function closeDrawer() {
  if (drawer.el) { drawer.scrim.remove(); drawer.el.remove(); }
  drawer.id = null; drawer.el = null;
}
function ensureDrawer(id) {
  if (drawer.id === id && drawer.el) return;
  closeDrawer();
  drawer.id = id;
  drawer.head = h("div", { class: "drawer-head" });
  drawer.body = h("div", { class: "drawer-body" }, skeleton());
  drawer.foot = h("div", { class: "drawer-foot" });
  drawer.scrim = h("div", { class: "scrim", onclick: () => { location.hash = "#/incidents"; } });
  drawer.el = h("aside", { class: "drawer", role: "dialog", "aria-label": "Incident details", "aria-modal": "true" }, drawer.head, drawer.body, drawer.foot);
  $("#drawerMount").append(drawer.scrim, drawer.el);
}
async function loadDrawer(id) {
  ensureDrawer(id);
  let inc, diag = null, diagErr = null;
  try { inc = await api("/incidents/" + id); } catch (e) {
    drawer.head.replaceChildren(h("div", { class: "row" }, h("h2", {}, "Incident not found"), closeBtn()));
    drawer.body.replaceChildren(errorView(e));
    drawer.foot.replaceChildren();
    return;
  }
  try { diag = await api(`/diagnostics/${inc.resource_type.toLowerCase()}/${encodeURIComponent(inc.namespace || "_")}/${encodeURIComponent(inc.resource_name)}`); } catch (e) { diagErr = e; }
  if (drawer.id !== id) return;
  const scroll = drawer.body.scrollTop;
  renderDrawer(inc, diag, diagErr);
  drawer.body.scrollTop = scroll;
}
const closeBtn = () => h("button", { class: "ghost icon-btn", type: "button", "aria-label": "Close", onclick: () => { location.hash = "#/incidents"; } }, icon("close"));

function renderDrawer(inc, diag, diagErr) {
  drawer.head.replaceChildren(
    h("div", { class: "row" }, h("span", {}, sevBadge(inc.severity), " ", statusEl(inc.status)), closeBtn()),
    h("h2", {}, inc.title),
    h("div", { class: "muted mono" }, `${inc.resource_type} ${target(inc)}`));

  const body = [];
  body.push(h("section", { class: "section card-in" },
    h("dl", { class: "kv" },
      h("dt", {}, "What happened"), h("dd", {}, inc.description),
      h("dt", {}, "Probable cause"), h("dd", {}, inc.probable_cause),
      h("dt", {}, "Suggested fix"), h("dd", {}, steps(inc.recommended_action)),
      h("dt", {}, "First seen"), h("dd", {}, when(inc.first_detected_at), " (", ago(inc.first_detected_at), ")"),
      h("dt", {}, "Last seen"), h("dd", {}, ago(inc.last_detected_at), `, ${plural(inc.occurrence_count, "check")} in total`, inc.reopen_count ? `, reopened ${plural(inc.reopen_count, "time")}` : ""),
      inc.acknowledged_by ? [h("dt", {}, "Acknowledged"), h("dd", {}, `${inc.acknowledged_by}, ${ago(inc.acknowledged_at)}`)] : null,
      inc.resolved_at ? [h("dt", {}, "Resolved"), h("dd", {}, `${inc.resolved_by || "KubiDoor"}, ${ago(inc.resolved_at)}${inc.resolution_note ? ": " + inc.resolution_note : ""}`)] : null)));

  const ds = h("section", { class: "section" }, h("h3", {}, "Live diagnosis"));
  if (diagErr) ds.append(notice("info", "Diagnosis unavailable", diagErr.message));
  else if (diag.healthy) ds.append(notice("info", "The resource looks healthy now", "Nothing is wrong at the moment. If it stays that way, KubiDoor will resolve this incident on its own."));
  else {
    for (const d of diag.diagnoses) {
      ds.append(h("div", { class: "card-in", style: "margin-bottom:10px" },
        h("div", {}, h("b", {}, d.title), h("span", { class: "conf " + d.confidence }, d.confidence + " confidence")),
        h("p", { style: "margin:8px 0 4px" }, d.probable_cause),
        h("ul", { class: "facts" }, ...d.observed_facts.map((x) => h("li", {}, x))),
        d.recommended_actions.length ? h("p", { style: "margin:10px 0 4px", class: "muted" }, "What to do") : null,
        d.recommended_actions.length ? h("ul", { class: "facts" }, ...d.recommended_actions.map((x) => h("li", {}, x))) : null));
    }
    if (!diag.resource_metrics_available) ds.append(h("p", { class: "muted" }, diag.resource_metrics_note));
  }
  body.push(ds);

  body.push(recoverySection(inc));

  const ev = Object.entries(inc.evidence || {});
  if (ev.length) body.push(h("details", { class: "section" }, h("summary", {}, `Evidence collected (${ev.length})`), h("pre", {}, JSON.stringify(inc.evidence, null, 2))));

  body.push(h("section", { class: "section" }, h("h3", {}, "History"),
    h("ol", { class: "timeline" }, ...inc.history.slice().reverse().map((e) => h("li", {},
      h("div", {}, h("b", {}, sentence(e.event_type.replaceAll(".", " "))), " by ", e.actor),
      h("div", { class: "when" }, when(e.timestamp)),
      e.details && e.details.note ? h("div", {}, String(e.details.note)) : null)))));
  drawer.body.replaceChildren(...body);

  const foot = [];
  if (inc.status !== "RESOLVED") {
    if (!inc.acknowledged_at) foot.push(h("button", { class: "btn", type: "button", onclick: () => mutate(() => post(`/incidents/${inc.id}/acknowledge`), "Incident acknowledged") }, "Acknowledge"));
    foot.push(h("button", { class: "btn", type: "button", onclick: async () => {
      const v = await ask({ title: "Mark as resolved", text: "If the problem is still there, KubiDoor will reopen this incident on the next check.",
        fields: [{ name: "note", label: "Note (optional)", type: "textarea", maxlength: 500 }], confirm: "Resolve" });
      if (v) mutate(() => post(`/incidents/${inc.id}/resolve`, { note: v.note.trim() || null }), "Incident resolved");
    } }, "Resolve"));
  }
  foot.push(h("span", { style: "flex:1" }), h("button", { class: "btn", type: "button", onclick: () => { location.hash = "#/incidents"; } }, "Close"));
  drawer.foot.replaceChildren(...foot);
}

function steps(text) {
  // The backend stores numbered advice as one string: "1. do a 2. do b". Show it as a list.
  const parts = (text || "").split(/\s+(?=\d+\.\s)/).map((x) => x.replace(/^\d+\.\s*/, "").trim()).filter(Boolean);
  return parts.length > 1 ? h("ol", { class: "facts" }, ...parts.map((x) => h("li", {}, x))) : text;
}
const ACTION_LABEL = { deployment_restart: "Restart deployment", pod_delete: "Delete pod" };
function proposable(inc) {
  if (inc.status === "RESOLVED") return null;
  if (inc.resource_type === "Deployment") return "deployment_restart";
  if (inc.resource_type === "Pod") return "pod_delete";
  return null;
}
function recoverySection(inc) {
  const sec = h("section", { class: "section" }, h("h3", {}, "Recovery"));
  const p = state.policy;
  const kind = proposable(inc);
  if (!kind) sec.append(h("p", { class: "muted", style: "margin:0 0 8px" }, "KubiDoor has no safe automatic action for this kind of resource. Follow the suggested fix above."));
  else if (p && !p.enabled) sec.append(notice("info", "Recovery is off", "Actions can't be requested while recovery.enabled is false. This is the default."));
  else if (kind) {
    const mode = p ? `${p.dry_run ? "Dry run, nothing will change. " : ""}${p.require_approval ? "A person must approve it first." : ""}` : "";
    sec.append(h("div", { class: "card-in", style: "margin-bottom:10px" },
      h("div", { class: "line", style: "display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap" },
        h("span", {}, h("b", {}, ACTION_LABEL[kind]), h("div", { class: "muted" }, mode || "Checked against the recovery policy.")),
        h("button", { class: "btn primary small", type: "button", onclick: async () => {
          const ok = await ask({ title: ACTION_LABEL[kind] + "?", text: `${ACTION_LABEL[kind]} for ${target(inc)}. This only requests the action; the recovery policy is checked now and again before it runs.`, confirm: "Request action" });
          if (ok) mutate(() => post("/recovery/actions", { incident_id: inc.id, action_type: kind }), "Action requested");
        } }, "Request action"))));
  }
  if (inc.recovery_actions.length) sec.append(h("div", { class: "card-in" }, ...inc.recovery_actions.slice().reverse().map((a) => actionRow(a))));
  return sec;
}
function actionRow(a) {
  const buttons = [];
  if (a.status === "PENDING_APPROVAL") {
    buttons.push(h("button", { class: "btn small primary", type: "button", onclick: async () => {
      const ok = await ask({ title: "Approve this action?", text: `${ACTION_LABEL[a.action_type] || a.action_type} on ${target(a)}. Approving does not run it; a second step, Run, does.`, confirm: "Approve" });
      if (ok) mutate(() => post(`/recovery/actions/${a.action_id}/approve`), "Action approved");
    } }, "Approve"));
    buttons.push(h("button", { class: "btn small", type: "button", onclick: async () => {
      const v = await ask({ title: "Reject this action?", fields: [{ name: "reason", label: "Reason (optional)", type: "textarea", maxlength: 500 }], confirm: "Reject", danger: true });
      if (v) mutate(() => post(`/recovery/actions/${a.action_id}/reject`, { reason: v.reason.trim() || null }), "Action rejected");
    } }, "Reject"));
  }
  if (a.status === "APPROVED") {
    buttons.push(h("button", { class: "btn small primary", type: "button", onclick: async () => {
      const real = !a.dry_run;
      const ok = await ask({ title: real ? "Run this action now?" : "Run this action as a dry run?",
        text: real ? `This will really ${a.action_type === "pod_delete" ? "delete the pod" : "restart the deployment"} ${target(a)}. KubiDoor re-checks the policy and the target first.` : "Dry run: KubiDoor checks everything but changes nothing in the cluster.",
        confirm: real ? "Run action" : "Run dry run", danger: real });
      if (ok) mutate(() => post(`/recovery/actions/${a.action_id}/execute`), real ? "Action started" : "Dry run recorded");
    } }, a.dry_run ? "Run (dry run)" : "Run"));
  }
  return h("div", { class: "action-row" },
    h("div", { class: "line" }, h("span", {}, h("b", {}, ACTION_LABEL[a.action_type] || a.action_type), " ", h("span", { class: "mono" }, target(a))), statusEl(a.status)),
    h("div", { class: "muted" }, `Requested by ${a.requested_by} ${ago(a.requested_at)}${a.approved_by ? `, approved by ${a.approved_by}` : ""}${a.dry_run ? ", dry run" : ""}`),
    a.error_message ? h("div", { class: "sev CRITICAL", style: "font-weight:500" }, a.error_message) : null,
    a.result && a.result.message ? h("div", {}, String(a.result.message)) : null,
    buttons.length ? h("div", { style: "display:flex;gap:8px" }, buttons) : null);
}

async function mutate(fn, okMessage) {
  state.busy = true;
  try { await fn(); toast(okMessage); }
  catch (e) { toast(e.message, true); }
  finally { state.busy = false; }
  await refresh({ manual: true });
}

/* ----- recovery ----- */
async function viewRecovery(seq) {
  setHeader("Recovery", "Actions KubiDoor may take, and the ones waiting for a decision.");
  let policy, actions;
  const f = state.recFilter;
  try {
    [policy, actions] = await Promise.all([api("/recovery/policy"), api("/recovery/actions?limit=100" + (f !== "all" ? "&status=" + f : ""))]);
  } catch (e) { return errorView(e, () => refresh({ manual: true })); }
  if (seq !== state.seq) return null;
  state.policy = policy;
  const root = h("div", { class: "stack" });
  if (!policy.enabled) root.append(notice("info", "Recovery is off", "KubiDoor only observes and explains problems. To allow actions, set KUBIDOOR_RECOVERY__ENABLED=true and read docs/recovery-policy.md first."));
  else if (policy.dry_run) root.append(notice("warn", "Dry run is on", "Actions are checked and recorded but nothing in the cluster changes. Set KUBIDOOR_RECOVERY__DRY_RUN=false to allow real changes."));

  const yes = (v) => (v ? "Yes" : "No");
  root.append(h("section", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", {}, "Safety policy"), h("span", { class: "muted" }, "Read-only. Change it in the server configuration.")),
    h("dl", { class: "policy-grid" },
      ...[["Recovery enabled", yes(policy.enabled)], ["Dry run", yes(policy.dry_run)], ["Approval required", yes(policy.require_approval)],
        ["Allowed namespaces", policy.allowed_namespaces.join(", ") || "none"], ["Blocked namespaces", policy.blocked_namespaces.join(", ") || "none"],
        ["Allowed actions", policy.allowed_actions.map((x) => ACTION_LABEL[x] || x).join(", ") || "none"],
        ["Cooldown per target", `${policy.cooldown_seconds} s`], ["Max actions per hour", String(policy.max_actions_per_hour)],
        ["Verify after action", yes(policy.verify_after_action)]].map(([k, v]) => h("div", {}, h("dt", {}, k), h("dd", {}, v))))));

  const filters = ["all", "PENDING_APPROVAL", "APPROVED", "SUCCEEDED", "FAILED", "DRY_RUN", "REJECTED"];
  root.append(h("div", { class: "toolbar", style: "margin:4px 0 0" },
    h("div", { class: "seg-ctl", role: "group", "aria-label": "Status" }, ...filters.map((s) => h("button", { type: "button", "aria-pressed": String(f === s),
      onclick: () => { state.recFilter = s; refresh({ manual: true }); } }, s === "all" ? "All" : sentence(s)))),
    h("span", { class: "spacer" }), h("span", { class: "muted" }, plural(actions.total, "action"))));

  if (!actions.items.length) root.append(h("div", { class: "panel" }, empty("No actions yet", "When an incident has a safe fix, request it from the incident page. It will appear here for approval.")));
  else root.append(h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ["Action", "Target", "Status", "People", "When", ""].map((x) => h("th", {}, x)))),
    h("tbody", {}, ...actions.items.map((a) => h("tr", {},
      h("td", {}, h("div", { class: "cell-title" }, ACTION_LABEL[a.action_type] || a.action_type), a.dry_run ? h("div", { class: "cell-sub" }, "dry run") : null),
      h("td", {}, h("div", { class: "mono" }, target(a)), h("a", { class: "link", href: "#/incidents/" + a.incident_id }, "Open incident")),
      h("td", {}, statusEl(a.status), a.error_message ? h("div", { class: "cell-sub" }, a.error_message) : null),
      h("td", {}, h("div", {}, "Requested by ", a.requested_by), a.approved_by ? h("div", { class: "cell-sub" }, "Approved by " + a.approved_by) : null),
      h("td", {}, timeEl(a.requested_at)),
      h("td", { class: "right" }, ...actionButtons(a))))))));
  return root;
}
function actionButtons(a) {
  const row = actionRow(a);
  return Array.from(row.querySelectorAll("button"));
}

/* ----- cluster ----- */
async function viewCluster(seq, route) {
  const tab = ["nodes", "pods", "deployments"].includes(route.parts[1]) ? route.parts[1] : "nodes";
  setHeader("Cluster", "What is running, straight from the Kubernetes API. KubiDoor only reads these.");
  const f = state.podFilter;
  const q = new URLSearchParams({ limit: "200" });
  if (tab !== "nodes" && f.namespace) q.set("namespace", f.namespace);
  if (tab === "pods" && f.unhealthy) q.set("unhealthy_only", "true");
  if (tab === "deployments" && f.unhealthy) q.set("degraded_only", "true");
  let data;
  try { data = await api(`/${tab}?${q}`); } catch (e) { return errorView(e, () => refresh({ manual: true })); }
  if (seq !== state.seq) return null;

  const root = h("div", {});
  root.append(h("div", { class: "toolbar" },
    h("div", { class: "seg-ctl", role: "group", "aria-label": "Resource type" }, ...["nodes", "pods", "deployments"].map((t) => h("button", { type: "button", "aria-pressed": String(t === tab), onclick: () => { location.hash = "#/cluster/" + t; } }, sentence(t)))),
    tab === "nodes" ? null : h("input", { type: "search", placeholder: "Namespace", "aria-label": "Namespace", value: f.namespace, style: "width:150px", onchange: (e) => { f.namespace = e.target.value.trim(); refresh({ manual: true }); } }),
    tab === "nodes" ? null : h("button", { class: "chip" + (f.unhealthy ? " on" : ""), type: "button", "aria-pressed": String(f.unhealthy), onclick: () => { f.unhealthy = !f.unhealthy; refresh({ manual: true }); } }, tab === "pods" ? "Only not ready" : "Only degraded"),
    h("span", { class: "spacer" }), h("span", { class: "muted" }, `${data.total} ${tab}`)));

  if (!data.items.length) { root.append(h("div", { class: "panel" }, empty(`No ${tab} found`, f.unhealthy || f.namespace ? "Clear the filters to see everything." : "The cluster returned an empty list for this resource.")));
    return root; }
  let head, rows;
  if (tab === "nodes") {
    head = ["Node", "Ready", "Schedulable", "CPU", "Memory", "Problems"];
    rows = data.items.map((n) => {
      const bad = n.conditions.filter((c) => c.type !== "Ready" && c.status === "True");
      return h("tr", {}, h("td", { class: "mono" }, n.name),
        h("td", {}, h("span", { class: "pill " + (n.ready_status === "True" ? "ok" : "bad") }, h("i"), n.ready_status === "True" ? "Ready" : "Not ready")),
        h("td", {}, n.unschedulable ? "Cordoned" : "Yes"), h("td", { class: "num" }, n.capacity.cpu || "-"), h("td", { class: "mono" }, n.capacity.memory || "-"),
        h("td", {}, bad.length ? bad.map((c) => c.type).join(", ") : h("span", { class: "muted" }, "None")));
    });
  } else if (tab === "pods") {
    head = ["Pod", "Phase", "Containers", "Restarts", "Node"];
    rows = data.items.map((p) => h("tr", {},
      h("td", {}, h("div", { class: "mono" }, p.name), h("div", { class: "cell-sub" }, p.namespace)),
      h("td", {}, h("span", { class: "pill " + (p.phase === "Running" && p.ready ? "ok" : p.phase === "Pending" ? "warn" : p.phase === "Succeeded" ? "" : "bad") }, h("i"), p.phase + (p.phase === "Running" && !p.ready ? ", not ready" : ""))),
      h("td", {}, ...p.containers.filter((c) => !c.is_init).map((c) => h("div", { class: "cell-sub" }, h("span", { class: "mono" }, c.name), ": ", c.state_reason || c.state))),
      h("td", { class: "num" }, p.total_restarts), h("td", { class: "mono" }, p.node_name || "-")));
  } else {
    head = ["Deployment", "Available", "Ready", "Updated", "State"];
    rows = data.items.map((d) => h("tr", {},
      h("td", {}, h("div", { class: "mono" }, d.name), h("div", { class: "cell-sub" }, d.namespace)),
      h("td", { class: "num" }, `${d.available_replicas} / ${d.replicas}`), h("td", { class: "num" }, d.ready_replicas), h("td", { class: "num" }, d.updated_replicas),
      h("td", {}, d.paused ? h("span", { class: "pill warn" }, h("i"), "Paused") : d.available_replicas >= d.replicas ? h("span", { class: "pill ok" }, h("i"), "Healthy") : h("span", { class: "pill bad" }, h("i"), "Degraded"))));
  }
  root.append(h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, head.map((x) => h("th", {}, x)))), h("tbody", {}, ...rows))));
  return root;
}

/* ---------------- router & refresh ---------------- */
function parseRoute() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, qs] = raw.split("?");
  return { parts: path.split("/").filter(Boolean), query: new URLSearchParams(qs || "") };
}
const VIEWS = { overview: viewOverview, incidents: viewIncidents, recovery: viewRecovery, cluster: viewCluster };
let inflight = false;

async function refresh({ manual = false } = {}) {
  if (inflight && !manual) return;
  if (!manual && (document.hidden || state.busy || $("#dlg").open)) return;
  inflight = true;
  const seq = ++state.seq;
  const route = parseRoute();
  const name = VIEWS[route.parts[0]] ? route.parts[0] : "overview";
  const btn = $("#refreshBtn");
  btn.classList.remove("spin"); void btn.offsetWidth; btn.classList.add("spin");
  try {
    await Promise.all([updatePills(), updateBadges()]);
    renderNav(name);
    const view = $("#view");
    if (!view.firstChild) view.append(skeleton());
    const node = await VIEWS[name](seq, route);
    if (node && seq === state.seq) view.replaceChildren(node);
    if (name === "incidents" && route.parts[1]) await loadDrawer(route.parts[1]);
    else closeDrawer();
    renderNav(name);
  } catch (e) {
    $("#view").replaceChildren(errorView(e instanceof ApiError ? e : new ApiError(0, "client", e.message), () => refresh({ manual: true })));
  } finally {
    inflight = false;
  }
}
function onRoute() {
  const route = parseRoute();
  const name = VIEWS[route.parts[0]] ? route.parts[0] : "overview";
  if (name === "incidents" && route.query.has("severity")) {
    state.incFilter = { ...state.incFilter, status: "active", severity: route.query.get("severity") };
    history.replaceState(null, "", "#/incidents");
  }
  if (name !== "incidents" || !route.parts[1]) { /* drawer closes in refresh */ }
  if (!(name === "incidents" && route.parts[1] && drawer.id === route.parts[1])) window.scrollTo(0, 0);
  refresh({ manual: true });
}

/* ---------------- boot ---------------- */
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && drawer.el && !$("#dlg").open) location.hash = "#/incidents"; });
$("#themeBtn").addEventListener("click", () => {
  state.theme = effectiveTheme() === "dark" ? "light" : "dark";
  store("local", "kubidoor.theme", state.theme);
  renderChrome();
});
$("#liveBtn").addEventListener("click", () => { state.live = !state.live; renderChrome(); });
$("#refreshBtn").addEventListener("click", () => refresh({ manual: true }));
$("#whoBtn").addEventListener("click", askIdentity);
window.addEventListener("hashchange", onRoute);
setInterval(() => { if (state.live) refresh(); }, 15000);
renderChrome();
renderNav((parseRoute().parts[0]) || "overview");
onRoute();
