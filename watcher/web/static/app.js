/* Watcher web UI: fetches one profile's stored listings and does filtering, sorting and
   de-duplication in the browser. All view state lives in the URL so views can be bookmarked. */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const SORTS = [
  ["best", "Best match"],
  ["price", "Price: low to high"],
  ["-price", "Price: high to low"],
  ["-first_seen", "Newest found"],
  ["odometer", "Lowest km"],
  ["-year", "Newest year"],
  ["drop", "Biggest price drop"],
];
const OP_TEXT = { eq: "", ne: "≠", in: "", not_in: "not", lt: "<", lte: "≤", gt: ">", gte: "≥", contains: "contains", regex: "matches", exists: "" };
const SWATCH = {
  white: "#f4f4f2", black: "#1b1c1f", grey: "#6e737b", gray: "#6e737b", silver: "#c3c7cc", blue: "#2c5bc7",
  red: "#b3202c", green: "#2f7a4a", yellow: "#e8c22c", orange: "#e07b26", brown: "#7a4e2d", gold: "#c9a85b", purple: "#6a3fa0",
};
const NONE = "__none";
const HIDDEN_FACETS = new Set(["vin", "title"]);
const MAX_OPTIONS = 30;

const ICON = {
  out: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>',
  x: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  car: '<svg viewBox="0 0 120 52" aria-hidden="true"><path d="M6 36c0-4 2.5-6.5 7-7.5l14-3L41 15c3-2.2 6.5-3.4 10.5-3.4h19c4.5 0 8 1.4 11 4.3l10 9.6 12 2.2c4 .8 6.5 3.3 6.5 7.3v4.4c0 1.4-1 2.4-2.4 2.4H100a11 11 0 0 0-21.6 0H41.6a11 11 0 0 0-21.6 0H8.4C7 42.8 6 41.8 6 40.4z" fill="var(--car)" stroke="currentColor" stroke-opacity=".35" stroke-width="1.5"/><path d="M45 25l9-8.5c1.6-1.4 3.5-2.1 5.6-2.1h9.6c2.5 0 4.8 1 6.5 2.8L84 25z" fill="currentColor" opacity=".18"/><circle cx="30.8" cy="42" r="8" fill="currentColor" opacity=".55"/><circle cx="89.2" cy="42" r="8" fill="currentColor" opacity=".55"/></svg>',
};

const S = {
  profiles: [],
  data: null,
  pid: null,
  view: "matches",
  q: "",
  sort: "best",
  layout: "grid",
  cols: "display",
  onlyNew: false,
  hideSold: false,
  filters: {}, // field -> {type: "range", min, max} | {type: "set", values: Set}
  item: null,
  run: null,
};

/* ---------------- helpers ---------------- */

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const label = (f) => f.replace(/_/g, " ");

function fmt(field, v) {
  if (v === null || v === undefined || v === "") return "—";
  const d = S.data?.domain || {};
  if (isNum(v) && field !== "year") {
    let t = Math.round(v * 100) / 100;
    t = t.toLocaleString("en-AU");
    if ((d.money_fields || []).includes(field)) t = "$" + t;
    if (d.units?.[field]) t += " " + d.units[field];
    return t;
  }
  if (Array.isArray(v)) return v.join(", ");
  return String(v);
}
const money = (v) => (isNum(v) ? "$" + Math.round(v).toLocaleString("en-AU") : "—");

function day(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const opts = { day: "numeric", month: "short" };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString("en-AU", opts);
}
function ago(iso) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const days = Math.round(s / 86400);
  return days === 1 ? "yesterday" : days < 30 ? `${days} days ago` : day(iso);
}
function foundText(iso) {
  if (!iso) return "";
  const days = Math.floor((startOfDay(new Date()) - startOfDay(new Date(iso))) / 86400000);
  return days <= 0 ? "Found today" : days === 1 ? "Found yesterday" : `Found ${day(iso)}`;
}
const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();

function swatch(colour) {
  return SWATCH[String(colour || "").toLowerCase()] || null;
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `${res.status} ${res.statusText}`);
  return body;
}

function toast(msg, error = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (error ? " error" : "");
  t.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (t.hidden = true), error ? 8000 : 5000);
}

/* ---------------- URL state ---------------- */

function readUrl() {
  const u = new URLSearchParams(location.search);
  S.pid = u.get("p");
  S.view = u.get("view") === "all" ? "all" : "matches";
  S.q = u.get("q") || "";
  S.sort = u.get("sort") || "best";
  S.layout = u.get("layout") === "table" ? "table" : "grid";
  S.cols = u.get("cols") === "all" ? "all" : "display";
  S.onlyNew = u.get("new") === "1";
  S.hideSold = u.get("sold") === "hide";
  S.item = u.get("item");
  S.filters = {};
  for (const [k, v] of u) {
    if (!k.startsWith("f.")) continue;
    const f = k.slice(2);
    const m = v.match(/^(-?[\d.]*)~(-?[\d.]*)$/);
    if (m) S.filters[f] = { type: "range", min: m[1] === "" ? null : +m[1], max: m[2] === "" ? null : +m[2] };
    else S.filters[f] = { type: "set", values: new Set(v.split("|")) };
  }
}

function writeUrl(push = false) {
  const u = new URLSearchParams();
  if (S.pid && S.profiles.length > 1) u.set("p", S.pid);
  if (S.view === "all") u.set("view", "all");
  if (S.q) u.set("q", S.q);
  if (S.sort !== "best") u.set("sort", S.sort);
  if (S.layout === "table") u.set("layout", "table");
  if (S.cols === "all") u.set("cols", "all");
  if (S.onlyNew) u.set("new", "1");
  if (S.hideSold) u.set("sold", "hide");
  for (const [f, flt] of Object.entries(S.filters)) {
    if (flt.type === "range") u.set("f." + f, `${flt.min ?? ""}~${flt.max ?? ""}`);
    else if (flt.values.size) u.set("f." + f, [...flt.values].join("|"));
  }
  if (S.item) u.set("item", S.item);
  const qs = u.toString();
  history[push ? "pushState" : "replaceState"](null, "", qs ? "?" + qs : location.pathname);
}

/* ---------------- pipeline: view -> filters -> sort -> dedupe ---------------- */

function base() {
  const all = S.data?.listings || [];
  return S.view === "matches" ? all.filter((r) => r.passed) : all;
}

function facetValue(r, field) {
  return field === "source" ? r.source_label : r.fields[field];
}

function passesFilter(r, field, flt) {
  const v = facetValue(r, field);
  if (flt.type === "range") {
    if (flt.min == null && flt.max == null) return true;
    if (!isNum(v)) return false;
    return (flt.min == null || v >= flt.min) && (flt.max == null || v <= flt.max);
  }
  if (!flt.values.size) return true;
  return flt.values.has(v === null || v === undefined || v === "" ? NONE : String(v));
}

function haystack(r) {
  return (r._hay ??= [r.title, r.source_label, ...Object.values(r.fields)].join(" ").toLowerCase());
}

function filtered(rows, except = null) {
  const terms = S.q.toLowerCase().split(/\s+/).filter(Boolean);
  return rows.filter((r) => {
    if (S.onlyNew && !r.new) return false;
    if (S.hideSold && r.fields.status === "sold") return false;
    for (const [f, flt] of Object.entries(S.filters)) if (f !== except && !passesFilter(r, f, flt)) return false;
    return terms.every((t) => haystack(r).includes(t));
  });
}

function sortValue(r, name) {
  if (name === "score") return r.score;
  if (name === "passed") return r.passed ? 1 : 0;
  if (name === "first_seen") return r.first_seen;
  if (name === "drop") return isNum(r.first_price) && isNum(r.fields.price) ? r.first_price - r.fields.price : null;
  if (name === "source") return r.source_label;
  return r.fields[name];
}

function comparator(specs) {
  return (a, b) => {
    for (const spec of specs) {
      const desc = spec.startsWith("-");
      const name = spec.replace(/^[-+]/, "");
      const va = sortValue(a, name), vb = sortValue(b, name);
      const ma = va === null || va === undefined, mb = vb === null || vb === undefined;
      if (ma || mb) { if (ma !== mb) return ma ? 1 : -1; continue; } // missing always last
      let c = isNum(va) && isNum(vb) ? va - vb : String(va).localeCompare(String(vb), undefined, { numeric: true });
      if (desc) c = -c;
      if (c) return c;
    }
    return 0;
  };
}

function sortSpecs() {
  if (S.sort === "best") return S.view === "all" ? ["-passed", ...S.data.sort] : S.data.sort;
  if (S.sort === "drop") return ["-drop", "price"];
  return [S.sort];
}

function dedupe(rows) {
  const keys = S.data.domain.dedupe_keys || [];
  const out = [];
  const groups = new Map();
  for (const r of rows) {
    const vals = keys.map((k) => r.fields[k]);
    const known = keys.length && vals.every((v) => v !== null && v !== undefined);
    const key = JSON.stringify(vals);
    const g = known ? groups.get(key) : null;
    if (g && !g.copies.some((c) => c.source === r.source)) { g.copies.push(r); continue; }
    const ng = { r, copies: [r] };
    out.push(ng);
    if (known && !groups.has(key)) groups.set(key, ng);
  }
  for (const g of out) {
    g.key = g.r.key;
    g.isNew = g.copies.every((c) => c.new);
    g.found = g.copies.map((c) => c.first_seen).filter(Boolean).sort()[0];
    g.image = g.copies.map((c) => c.image).find(Boolean) || null;
  }
  return out;
}

function results() {
  // Rank first so each duplicate group is headed by its best listing, as in the terminal.
  return dedupe(filtered(base()).sort(comparator(sortSpecs())));
}

/* ---------------- facets ---------------- */

function facetDefs(rows) {
  const fields = new Map();
  for (const r of rows) for (const [k, v] of Object.entries(r.fields)) {
    if (k.endsWith("_raw") || HIDDEN_FACETS.has(k)) continue;
    if (!fields.has(k)) fields.set(k, []);
    fields.get(k).push(v);
  }
  const display = S.data.display || [];
  const order = (f) => (f === "source" ? -2 : f === "status" ? -1 : display.includes(f) ? display.indexOf(f) : 100);
  const defs = [{ field: "source", type: "set" }];
  for (const [f, vals] of fields) {
    const known = vals.filter((v) => v !== null && v !== undefined && v !== "");
    if (!known.length) continue;
    if (known.every(isNum)) defs.push({ field: f, type: "range", min: Math.min(...known), max: Math.max(...known) });
    else if (new Set(known.map(String)).size <= MAX_OPTIONS) defs.push({ field: f, type: "set" });
  }
  // Active filters on fields absent from this view stay visible so they can be cleared.
  for (const [f, flt] of Object.entries(S.filters)) if (!defs.some((d) => d.field === f)) defs.push({ field: f, type: flt.type });
  return defs.sort((a, b) => order(a.field) - order(b.field) || a.field.localeCompare(b.field));
}

function renderFacets() {
  const rows = base();
  const defs = facetDefs(rows);
  const html = [`
    <div class="facet">
      <label class="toggle">Only new on the last run <input type="checkbox" data-toggle="onlyNew" ${S.onlyNew ? "checked" : ""}></label>
      ${S.view === "all" ? `<label class="toggle">Hide sold <input type="checkbox" data-toggle="hideSold" ${S.hideSold ? "checked" : ""}></label>` : ""}
    </div>`];
  for (const d of defs) {
    const flt = S.filters[d.field];
    const active = flt && (flt.type === "range" ? flt.min != null || flt.max != null : flt.values.size);
    const clear = active ? `<button class="link-btn clear" data-clear="${esc(d.field)}">Clear</button>` : "";
    if (d.type === "range") {
      const step = d.field === "year" ? 1 : "any";
      html.push(`
        <div class="facet">
          <h3 class="facet-title">${esc(label(d.field))}${clear}</h3>
          <div class="range">
            <input type="number" inputmode="numeric" step="${step}" data-range="${esc(d.field)}" data-end="min" aria-label="${esc(label(d.field))} minimum"
              placeholder="${d.min != null ? esc(fmt(d.field, d.min).replace(/[^\d.-]/g, "")) : "Min"}" value="${flt?.min ?? ""}">
            <span>–</span>
            <input type="number" inputmode="numeric" step="${step}" data-range="${esc(d.field)}" data-end="max" aria-label="${esc(label(d.field))} maximum"
              placeholder="${d.max != null ? esc(fmt(d.field, d.max).replace(/[^\d.-]/g, "")) : "Max"}" value="${flt?.max ?? ""}">
          </div>
        </div>`);
      continue;
    }
    // Counts reflect every other active filter, so you see what each choice would leave.
    const counts = new Map();
    for (const r of filtered(rows, d.field)) {
      const v = facetValue(r, d.field);
      const k = v === null || v === undefined || v === "" ? NONE : String(v);
      counts.set(k, (counts.get(k) || 0) + 1);
    }
    const all = new Set([...rows.map((r) => facetValue(r, d.field)).map((v) => (v === null || v === undefined || v === "" ? NONE : String(v))), ...(flt?.values || [])]);
    const opts = [...all].sort((a, b) => (a === NONE) - (b === NONE) || (counts.get(b) || 0) - (counts.get(a) || 0) || a.localeCompare(b));
    html.push(`
      <div class="facet">
        <h3 class="facet-title">${esc(label(d.field))}${clear}</h3>
        <div class="opts">${opts.map((v) => {
          const on = flt?.values.has(v);
          const n = counts.get(v) || 0;
          const sw = d.field === "colour" && swatch(v) ? `<span class="swatch" style="background:${swatch(v)}"></span>` : "";
          return `<button class="opt" data-set="${esc(d.field)}" data-value="${esc(v)}" aria-pressed="${on ? "true" : "false"}" ${!n && !on ? "disabled" : ""}>${sw}${v === NONE ? "<em>Unknown</em>" : esc(v)} <span class="n">${n}</span></button>`;
        }).join("")}</div>
      </div>`);
  }
  $("#facets").innerHTML = html.join("");
}

function activeCount() {
  return Object.values(S.filters).filter((f) => (f.type === "range" ? f.min != null || f.max != null : f.values.size)).length + S.onlyNew + S.hideSold;
}

/* ---------------- render ---------------- */

function ruleChip(rule, kind) {
  const w = kind === "optional" && rule.weight !== 1 ? `<span class="w">×${rule.weight}</span>` : "";
  return `<span class="chip ${kind}">${ruleText(rule)}${w}</span>`;
}

function ruleText(rule) {
  const d = S.data.domain;
  const val = (v) => ((d.money_fields || []).includes(rule.field) && isNum(v) ? money(v) : fmt(rule.field, v));
  const parts = rule.ops.map(([op, v]) => {
    if (op === "exists") return v ? "known" : "unknown";
    const shown = Array.isArray(v) ? v.map(val).join(" / ") : val(v);
    return [OP_TEXT[op], esc(shown)].filter(Boolean).join(" ");
  });
  return `<span class="k">${esc(label(rule.field))}</span> ${parts.join(" and ")}`;
}

function renderHeader() {
  const d = S.data;
  document.title = `${d.name} · Watcher`;
  $("#title").textContent = d.name;
  const last = d.last_run;
  const total = d.listings.length;
  $("#subtitle").textContent = last
    ? `Last run ${ago(last.started_at)} · ${last.sources.length} sites · ${total} listings tracked`
    : "No runs recorded yet";
  $("#sources").innerHTML = (last?.sources || []).map((s) => {
    const cls = s.status === "ok" ? "ok" : s.status === "blocked" || s.status === "error" ? "bad" : "warn";
    const tail = s.status === "ok" ? `<b class="num">${s.fetched}</b>` : `<b>${esc(s.status)}</b>`;
    const title = s.status === "ok" ? `${s.fetched} fetched, ${s.matched} matched` : s.message || s.status;
    return `<span class="source-pill" title="${esc(title)}"><span class="dot ${cls}"></span>${esc(s.label)} ${tail}</span>`;
  }).join("");

  const sel = $("#profile");
  sel.hidden = S.profiles.length < 2;
  sel.innerHTML = S.profiles.map((p) => `<option value="${esc(p.id)}" ${p.id === S.pid ? "selected" : ""}>${esc(p.name)}</option>`).join("");
}

function renderRules() {
  $$(".viewbar [data-view]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === S.view)));
  const { required, optional } = S.data.rules;
  const rejected = S.data.listings.filter((r) => !r.passed).length;
  if (S.view === "matches") {
    $("#rules").innerHTML = `
      ${required.length ? `<div class="rule-row"><span class="rule-row-label">Must have</span>${required.map((r) => ruleChip(r, "required")).join("")}</div>` : ""}
      ${optional.length ? `<div class="rule-row"><span class="rule-row-label">Nice to have</span>${optional.map((r) => ruleChip(r, "optional")).join("")}</div>` : ""}
      ${rejected ? `<div class="rules-note">${rejected} other stored listing${rejected === 1 ? "" : "s"} don't meet the must-haves. <button class="link-btn" data-goview="all">Show everything stored</button></div>` : ""}`;
  } else {
    $("#rules").innerHTML = `<div class="rules-note">Every listing stored for this profile, including sold ones and ones that miss the profile's rules. Narrow it down with the filters.
      <button class="link-btn" data-goview="matches">Back to profile matches</button></div>`;
  }
}

function renderStats(groups) {
  const merged = groups.reduce((n, g) => n + g.copies.length - 1, 0);
  const newN = groups.filter((g) => g.isNew).length;
  const priced = groups.filter((g) => isNum(g.r.fields.price)).sort((a, b) => a.r.fields.price - b.r.fields.price);
  const cheapest = priced[0];
  const drops = groups.filter((g) => isNum(g.r.first_price) && isNum(g.r.fields.price) && g.r.fields.price < g.r.first_price).length;
  const sold = S.data.listings.filter((r) => r.fields.status === "sold").length;
  const tile = (lbl, value, sub, cls = "") => `<div class="stat"><div class="stat-label">${lbl}</div><div class="stat-value ${cls}">${value}</div><div class="stat-sub">${sub}</div></div>`;
  $("#stats").innerHTML = [
    tile(S.view === "matches" ? "Matches" : "Listings", groups.length, merged ? `${merged} duplicate${merged === 1 ? "" : "s"} merged` : "no duplicates"),
    tile("New", newN, "first seen on the last run", newN ? "good" : ""),
    tile("Lowest price", cheapest ? money(cheapest.r.fields.price) : "—", cheapest ? esc(cheapest.r.title) : "nothing priced"),
    tile("Price drops", drops, `${S.data.listings.length} tracked · ${sold} sold`),
  ].join("");
}

function renderToolbar(groups) {
  const sel = $("#sort");
  const opts = [...SORTS];
  if (!opts.some(([v]) => v === S.sort)) opts.push([S.sort, `${label(S.sort.replace(/^-/, ""))} ${S.sort.startsWith("-") ? "↓" : "↑"}`]);
  sel.innerHTML = opts.map(([v, t]) => `<option value="${esc(v)}" ${v === S.sort ? "selected" : ""}>${esc(t)}</option>`).join("");
  $("#q").value = S.q;
  $$("[data-layout]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.layout === S.layout)));
  const n = activeCount();
  $("#filter-count").hidden = !n;
  $("#filter-count").textContent = n;

  const chips = [`<span class="result-count">${groups.length} result${groups.length === 1 ? "" : "s"}</span>`];
  if (S.onlyNew) chips.push(`<button class="active-chip" data-unset="onlyNew">Only new ${ICON.x}</button>`);
  if (S.hideSold) chips.push(`<button class="active-chip" data-unset="hideSold">Hiding sold ${ICON.x}</button>`);
  for (const [f, flt] of Object.entries(S.filters)) {
    let text;
    if (flt.type === "range") {
      if (flt.min == null && flt.max == null) continue;
      text = flt.min != null && flt.max != null ? `${fmt(f, flt.min)} – ${fmt(f, flt.max)}` : flt.min != null ? `≥ ${fmt(f, flt.min)}` : `≤ ${fmt(f, flt.max)}`;
    } else {
      if (!flt.values.size) continue;
      text = [...flt.values].map((v) => (v === NONE ? "Unknown" : v)).join(", ");
    }
    chips.push(`<button class="active-chip" data-clear="${esc(f)}"><span class="muted">${esc(label(f))}:</span> ${esc(text)} ${ICON.x}</button>`);
  }
  if (chips.length > 1) chips.push(`<button class="link-btn" data-reset>Clear all</button>`);
  $("#active").innerHTML = chips.length > 1 || S.q ? chips.join("") : "";
}

function photo(g, cls = "") {
  const colour = swatch(g.r.fields.colour) || "var(--surface-3)";
  const ph = `<div class="placeholder" style="--car:${colour}">${ICON.car}</div>`;
  if (!g.image) return ph;
  return `${ph}<img class="${cls}" src="${esc(g.image)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" onerror="this.remove()">`;
}

function deltaBadge(r) {
  const now = r.fields.price, first = r.first_price;
  if (!isNum(now) || !isNum(first) || now === first) return "";
  const down = now < first;
  return `<span class="delta ${down ? "down" : "up"}" title="was ${money(first)}">${down ? "▼" : "▲"} ${money(Math.abs(now - first))}</span>`;
}

function prettyFailure(f) {
  const m = f.match(/^(\w+)=(.*) fails (\S+(?: in)?) (.*)$/);
  if (!m) return esc(f.replace(/_/g, " "));
  const [, field, raw, op, want] = m;
  const strip = (s) => s.replace(/^'(.*)'$/, "$1").replace(/[[\]']/g, "");
  const val = (s) => { const n = Number(strip(s)); return Number.isFinite(n) && s.trim() !== "" ? fmt(field, n) : strip(s); };
  const words = { "=": "", "!=": "not", in: "one of", "not in": "none of", "<": "<", "<=": "≤", ">": ">", ">=": "≥", "~": "matching", contains: "containing" };
  return `<b>${esc(label(field))}</b> is ${esc(val(raw))}, needs ${esc(words[op] ?? op)} ${esc(val(want))}`;
}

function cardSpecs(r) {
  const title = r.title.toLowerCase();
  return (S.data.display || [])
    .filter((f) => !["price", "status", "make", "model"].includes(f))
    .filter((f) => r.fields[f] !== null && r.fields[f] !== undefined && !title.includes(String(r.fields[f]).toLowerCase()))
    .slice(0, 4)
    .map((f) => {
      const sw = f === "colour" && swatch(r.fields[f]) ? `<span class="swatch" style="background:${swatch(r.fields[f])}"></span>` : "";
      return `<span>${sw}${esc(fmt(f, r.fields[f]))}</span>`;
    }).join("");
}

function siteLinks(g) {
  return g.copies.map((c) => `<a class="site" href="${esc(c.url)}" target="_blank" rel="noopener noreferrer" data-stop title="Open on ${esc(c.source_label)}">${esc(c.source_label)} ${ICON.out}</a>`).join("");
}

function statusBadge(r) {
  const s = r.fields.status;
  if (!s || s === "available") return "";
  return `<span class="badge ${s === "sold" ? "bad" : "warn"}">${esc(s[0].toUpperCase() + s.slice(1))}</span>`;
}

function card(g) {
  const r = g.r;
  const hasOpt = S.data.rules.optional.length;
  return `
    <article class="card ${r.passed ? "" : "rejected"}" data-open="${esc(g.key)}" tabindex="0" role="button" aria-label="${esc(r.title)}">
      <div class="photo">
        ${photo(g)}
        <div class="badges">
          ${g.isNew ? '<span class="badge new">New</span>' : ""}
          ${statusBadge(r)}
          ${g.copies.length > 1 ? `<span class="badge">${g.copies.length} sites</span>` : ""}
          ${hasOpt && r.passed ? `<span class="badge score" title="Nice-to-haves met">★ ${r.score}/${r.max_score}</span>` : ""}
        </div>
      </div>
      <div class="card-body">
        <div class="card-title">${esc(r.title)}</div>
        <div class="price-row"><span class="price">${money(r.fields.price)}</span>${deltaBadge(r)}</div>
        <div class="specs">${cardSpecs(r)}</div>
        ${r.passed ? "" : `<div class="why">${prettyFailure(r.failures[0] || "")}${r.failures.length > 1 ? ` <span class="muted">+${r.failures.length - 1} more</span>` : ""}</div>`}
        <div class="card-foot">${siteLinks(g)}<span class="found">${foundText(g.found)}</span></div>
      </div>
    </article>`;
}

function tableCols(groups) {
  if (S.cols === "display") return S.data.display || [];
  const set = new Set();
  for (const g of groups) for (const k of Object.keys(g.r.fields)) set.add(k);
  const display = S.data.display || [];
  return [...display, ...[...set].filter((k) => !display.includes(k)).sort()];
}

function table(groups) {
  const cols = tableCols(groups);
  const d = S.data.domain;
  const right = (f) => (d.money_fields || []).includes(f) || d.units?.[f] || f === "score" || f === "year";
  const head = (f, text) => {
    const cur = S.sort.replace(/^-/, "") === f ? (S.sort.startsWith("-") ? " ↓" : " ↑") : "";
    return `<th class="${right(f) ? "r" : ""}"><button data-sortby="${esc(f)}">${esc(text)}<span class="arrow">${cur}</span></button></th>`;
  };
  const hasOpt = S.data.rules.optional.length;
  return `
    <div class="tbl-head"><button class="btn btn-ghost btn-sm" data-cols>${S.cols === "all" ? "Show profile columns" : "Show every field"}</button></div>
    <div class="table-wrap"><table>
      <thead><tr><th></th><th>Title</th>${hasOpt ? head("score", "Score") : ""}${cols.map((c) => head(c, label(c))).join("")}${head("first_seen", "Found")}<th>Sites</th></tr></thead>
      <tbody>${groups.map((g) => {
        const r = g.r;
        return `<tr class="${r.passed ? "" : "rejected"}" data-open="${esc(g.key)}">
          <td>${g.image ? `<img class="thumb" src="${esc(g.image)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.visibility='hidden'">` : '<div class="thumb"></div>'}</td>
          <td class="title-cell">${esc(r.title)}${g.isNew ? ' <span class="delta down">New</span>' : ""}</td>
          ${hasOpt ? `<td class="r num">${r.passed ? `${r.score}/${r.max_score}` : "—"}</td>` : ""}
          ${cols.map((c) => `<td class="${right(c) ? "r num" : ""}">${c === "price" ? `${money(r.fields.price)} ${deltaBadge(r)}` : esc(fmt(c, r.fields[c]))}</td>`).join("")}
          <td class="num">${day(g.found)}</td>
          <td>${siteLinks(g)}</td>
        </tr>`;
      }).join("")}</tbody>
    </table></div>`;
}

function renderList(groups) {
  const list = $("#list");
  if (!S.data.listings.length) {
    list.innerHTML = `<div class="empty"><h3>Nothing stored yet</h3><p>Press <b>Run now</b> to search the sites for this profile.</p></div>`;
    return;
  }
  if (!groups.length) {
    list.innerHTML = `<div class="empty"><h3>No listings match</h3><p>Try removing a filter${S.view === "matches" ? ", or look at everything stored" : ""}.</p>
      <p><button class="btn btn-outline" data-reset>Clear filters</button>${S.view === "matches" ? ` <button class="btn btn-ghost" data-goview="all">Everything stored</button>` : ""}</p></div>`;
    return;
  }
  list.innerHTML = S.layout === "table" ? table(groups) : `<div class="grid">${groups.map(card).join("")}</div>`;
}

let current = [];
function render() {
  if (!S.data) return;
  current = results();
  renderHeader();
  renderRules();
  renderStats(current);
  renderToolbar(current);
  renderFacets();
  renderList(current);
  writeUrl();
}

/* ---------------- detail sheet ---------------- */

function findGroup(key) {
  let g = current.find((g) => g.copies.some((c) => c.key === key));
  if (g) return g;
  const r = S.data.listings.find((r) => r.key === key);
  return r ? dedupe([r])[0] : null;
}

function checks(r) {
  const { required, optional } = S.data.rules;
  const failed = (field) => r.failures.some((f) => f.startsWith(field + "=") || f.startsWith(field + " "));
  const row = (ok, cls, rule, kind) => `<div class="check"><span class="check-mark ${cls}">${ok ? "✓" : "✕"}</span><div>${ruleText(rule)}
      ${!ok && kind === "required" ? `<small>${prettyFailure(r.failures.find((f) => f.startsWith(rule.field)) || "")}</small>` : ""}</div></div>`;
  return `
    ${required.length ? `<h3 class="section-title">Must have</h3><div class="checks">${required.map((c) => row(!failed(c.field), failed(c.field) ? "no" : "ok", c, "required")).join("")}</div>` : ""}
    ${optional.length ? `<h3 class="section-title" style="margin-top:16px">Nice to have · ${r.score}/${r.max_score}</h3><div class="checks">${optional.map((c) => { const ok = r.matched.includes(c.field); return row(ok, ok ? "ok" : "opt-no", c, "optional"); }).join("")}</div>` : ""}`;
}

function openDetail(key, push = true) {
  const g = findGroup(key);
  const dlg = $("#detail");
  if (!g) return;
  const r = g.r;
  S.item = key;
  writeUrl(push);
  const fields = Object.entries(r.fields).filter(([, v]) => v !== null && v !== undefined && v !== "");
  const display = S.data.display || [];
  fields.sort(([a], [b]) => (display.includes(a) ? display.indexOf(a) : 99) - (display.includes(b) ? display.indexOf(b) : 99) || a.localeCompare(b));
  dlg.innerHTML = `
    <button class="btn btn-icon sheet-close" data-close aria-label="Close">${ICON.x}</button>
    <div class="sheet-scroll" tabindex="-1" autofocus>
      <div class="sheet-photo">${photo(g)}<div class="badges">${g.isNew ? '<span class="badge new">New</span>' : ""}${statusBadge(r)}</div></div>
      <div class="sheet-body">
        <div class="sheet-head">
          <h2 id="detail-title">${esc(r.title)}</h2>
          <div class="price-row"><span class="price">${money(r.fields.price)}</span>${deltaBadge(r)}</div>
          <div class="sheet-meta">${esc(foundText(g.found))} (${esc(ago(g.found))}) · last seen ${esc(ago(r.last_seen))}${r.passed ? "" : " · <b style='color:var(--bad)'>doesn't match the profile</b>"}</div>
        </div>
        <div class="two-col">
          <div>
            <h3 class="section-title">Price history</h3>
            <div id="chart" class="chart"></div>
          </div>
          <div>
            <h3 class="section-title">Listed on</h3>
            <div class="offers">${g.copies.map((c) => `
              <a class="offer" href="${esc(c.url)}" target="_blank" rel="noopener noreferrer">
                <div><div class="offer-name">${esc(c.source_label)}</div><div class="offer-sub">since ${esc(day(c.first_seen))} · ${esc(c.fields.status || "status unknown")}</div></div>
                <div class="offer-price">${money(c.fields.price)}</div>${ICON.out}
              </a>`).join("")}</div>
          </div>
        </div>
        ${S.data.rules.required.length || S.data.rules.optional.length ? `<div>${checks(r)}</div>` : ""}
        <div>
          <h3 class="section-title">Everything we know</h3>
          <dl class="kv">${fields.map(([k, v]) => `<div><dt>${esc(label(k))}</dt><dd>${esc(fmt(k, v))}</dd></div>`).join("")}</dl>
        </div>
      </div>
    </div>`;
  if (!dlg.open) dlg.showModal();
  dlg.querySelector(".sheet-scroll").scrollTop = 0;
  requestAnimationFrame(() => drawChart($("#chart"), r));
}

function closeDetail() {
  const dlg = $("#detail");
  if (dlg.open) dlg.close();
}

/* Price history: one series, step line (a price holds until it changes), hover per change. */
function drawChart(el, r) {
  const pts = (r.prices || []).filter(([, p]) => isNum(p)).map(([t, p]) => ({ t: new Date(t).getTime(), p }));
  if (pts.length < 2) {
    const since = pts[0] ? day(new Date(pts[0].t).toISOString()) : day(r.first_seen);
    el.innerHTML = `<p class="muted" style="margin:0">${isNum(r.fields.price) ? `Unchanged at <b style="color:var(--text)">${money(r.fields.price)}</b> since ${esc(since)}.` : "No price recorded."}</p>`;
    return;
  }
  const end = Math.max(new Date(r.last_seen).getTime(), pts[pts.length - 1].t);
  const W = Math.max(260, el.clientWidth), H = 170, padL = 4, padR = 4, padT = 22, padB = 24;
  const t0 = pts[0].t, t1 = end > t0 ? end : t0 + 1;
  let lo = Math.min(...pts.map((d) => d.p)), hi = Math.max(...pts.map((d) => d.p));
  const pad = (hi - lo) * 0.25 || hi * 0.02 || 1;
  lo -= pad; hi += pad;
  const x = (t) => padL + ((t - t0) / (t1 - t0)) * (W - padL - padR);
  const y = (p) => padT + (1 - (p - lo) / (hi - lo)) * (H - padT - padB);
  let d = `M${x(pts[0].t)},${y(pts[0].p)}`;
  for (let i = 1; i < pts.length; i++) d += `H${x(pts[i].t)}V${y(pts[i].p)}`;
  d += `H${x(t1)}`;
  const area = `${d}V${H - padB}H${x(t0)}Z`;
  const maxP = Math.max(...pts.map((p) => p.p)), minP = Math.min(...pts.map((p) => p.p));
  el.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Price history from ${money(pts[0].p)} to ${money(pts[pts.length - 1].p)}">
      <line class="grid-line" x1="0" x2="${W}" y1="${y(maxP)}" y2="${y(maxP)}"/>
      <line class="grid-line" x1="0" x2="${W}" y1="${y(minP)}" y2="${y(minP)}"/>
      <text class="axis-label" x="0" y="${y(maxP) - 6}">${money(maxP)}</text>
      ${minP !== maxP ? `<text class="axis-label" x="0" y="${y(minP) - 6}">${money(minP)}</text>` : ""}
      <text class="axis-label" x="0" y="${H - 6}">${day(new Date(t0).toISOString())}</text>
      <text class="axis-label" x="${W}" y="${H - 6}" text-anchor="end">${end > pts[pts.length - 1].t ? "last seen " : ""}${day(new Date(t1).toISOString())}</text>
      <path class="area" d="${area}"/>
      <path class="line" d="${d}"/>
      ${pts.map((p, i) => `<circle class="pt" data-i="${i}" cx="${x(p.t)}" cy="${y(p.p)}" r="4.5"/>`).join("")}
      ${pts.map((p, i) => `<circle class="hit" data-i="${i}" cx="${x(p.t)}" cy="${y(p.p)}" r="14"/>`).join("")}
    </svg>
    <ul class="history-list">${pts.slice().reverse().map((p, i, a) => {
      const prev = a[i + 1];
      const diff = prev ? p.p - prev.p : 0;
      return `<li><span>${day(new Date(p.t).toISOString())}</span><span>${money(p.p)}${diff ? ` <span class="delta ${diff < 0 ? "down" : "up"}">${diff < 0 ? "▼" : "▲"} ${money(Math.abs(diff))}</span>` : prev ? "" : ' <span class="muted">first seen</span>'}</span></li>`;
    }).join("")}</ul>`;
  const tip = $("#tip");
  const show = (e) => {
    const i = e.target.dataset?.i;
    if (i === undefined) return;
    const p = pts[+i];
    $$(".pt", el).forEach((c) => c.classList.toggle("active", c.dataset.i === i));
    const box = e.target.getBoundingClientRect();
    tip.innerHTML = `<b>${money(p.p)}</b> · ${day(new Date(p.t).toISOString())}`;
    tip.style.left = `${box.left + box.width / 2}px`;
    tip.style.top = `${box.top + box.height / 2 - 4}px`;
    tip.hidden = false;
  };
  const hide = () => { tip.hidden = true; $$(".pt", el).forEach((c) => c.classList.remove("active")); };
  $$(".hit", el).forEach((c) => {
    c.addEventListener("pointerenter", show);
    c.addEventListener("pointerdown", show);
    c.addEventListener("pointerleave", hide);
  });
  $(".sheet-scroll").addEventListener("scroll", hide, { passive: true });
}

/* ---------------- running a scrape ---------------- */

function setRunning(state) {
  S.run = state;
  const btn = $("#run");
  const busy = state?.running;
  btn.disabled = !!busy;
  btn.querySelector(".icon").classList.toggle("spin", !!busy);
  const mine = state?.profile === S.pid;
  btn.querySelector(".run-label").textContent = busy ? (mine ? "Running…" : "Another run…") : "Run now";
  btn.title = busy ? `Started ${ago(state.started_at)} — this can take a minute or two` : "Search every site again now";
}

async function pollRun() {
  const state = await api("/api/run").catch(() => null);
  if (!state) return;
  const wasRunning = S.run?.running;
  setRunning(state);
  if (state.running) { setTimeout(pollRun, 2500); return; }
  if (wasRunning) {
    if (state.error) toast(`Run failed: ${state.error}`, true);
    else toast(`Run finished: ${state.summary}`);
    if (state.profile === S.pid) await loadProfile(S.pid);
  }
}

async function startRun() {
  try {
    setRunning(await api(`/api/profiles/${encodeURIComponent(S.pid)}/run`, { method: "POST" }));
    toast("Searching every site… results will refresh when it's done.");
    setTimeout(pollRun, 2500);
  } catch (e) {
    toast(e.message, true);
  }
}

/* ---------------- loading ---------------- */

async function loadProfile(pid) {
  try {
    S.data = await api(`/api/profiles/${encodeURIComponent(pid)}`);
  } catch (e) {
    $("#title").textContent = "Couldn't load profile";
    $("#subtitle").textContent = e.message;
    $("#list").innerHTML = `<div class="empty"><h3>Something went wrong</h3><p>${esc(e.message)}</p></div>`;
    return;
  }
  render();
  if (S.item) openDetail(S.item, false);
}

function skeleton() {
  $("#list").innerHTML = `<div class="grid">${Array.from({ length: 6 }, () => '<div class="skeleton" style="height:340px"></div>').join("")}</div>`;
  $("#stats").innerHTML = Array.from({ length: 4 }, () => '<div class="skeleton" style="height:92px"></div>').join("");
}

/* ---------------- events ---------------- */

function setFilter(field, flt) {
  if (!flt || (flt.type === "range" ? flt.min == null && flt.max == null : !flt.values.size)) delete S.filters[field];
  else S.filters[field] = flt;
  render();
}

function resetFilters() {
  S.filters = {};
  S.onlyNew = S.hideSold = false;
  S.q = "";
  render();
}

function openFilters(open) {
  $("#filters").classList.toggle("open", open);
  $("#scrim").hidden = !open;
  document.body.style.overflow = open ? "hidden" : "";
}

function bind() {
  document.addEventListener("click", (e) => {
    const t = e.target.closest("button, a, [data-open]");
    if (!t) return;
    if (t.matches("[data-stop]")) return; // site links open normally
    if (t.dataset.view) { S.view = t.dataset.view; render(); return; }
    if (t.dataset.goview) { S.view = t.dataset.goview; render(); window.scrollTo({ top: 0, behavior: "smooth" }); return; }
    if (t.dataset.layout) { S.layout = t.dataset.layout; render(); return; }
    if (t.dataset.set !== undefined) {
      const f = t.dataset.set, v = t.dataset.value;
      const values = new Set(S.filters[f]?.values || []);
      values.has(v) ? values.delete(v) : values.add(v);
      setFilter(f, { type: "set", values });
      return;
    }
    if (t.dataset.clear) { setFilter(t.dataset.clear, null); return; }
    if (t.dataset.unset) { S[t.dataset.unset] = false; render(); return; }
    if (t.hasAttribute("data-reset")) { resetFilters(); return; }
    if (t.dataset.sortby) {
      const f = t.dataset.sortby;
      S.sort = S.sort === f ? "-" + f : S.sort === "-" + f ? "best" : (f === "first_seen" || f === "score" || f === "year" ? "-" + f : f);
      render();
      return;
    }
    if (t.hasAttribute("data-cols")) { S.cols = S.cols === "all" ? "display" : "all"; render(); return; }
    if (t.hasAttribute("data-close")) { closeDetail(); return; }
    if (t.dataset.open) openDetail(t.dataset.open);
  });
  document.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches?.("article[data-open]")) { e.preventDefault(); openDetail(e.target.dataset.open); }
    if (e.key === "Escape" && $("#filters").classList.contains("open")) openFilters(false);
  });
  $("#facets").addEventListener("change", (e) => {
    if (e.target.dataset.toggle) { S[e.target.dataset.toggle] = e.target.checked; render(); }
  });
  $("#facets").addEventListener("input", debounce((e) => {
    const f = e.target.dataset.range;
    if (!f) return;
    const cur = S.filters[f]?.type === "range" ? { ...S.filters[f] } : { type: "range", min: null, max: null };
    cur[e.target.dataset.end] = e.target.value === "" ? null : +e.target.value;
    const focused = { f, end: e.target.dataset.end };
    setFilter(f, cur);
    const el = $(`[data-range="${CSS.escape(focused.f)}"][data-end="${focused.end}"]`);
    if (el) { el.focus(); const v = el.value; el.value = ""; el.value = v; }
  }, 450));
  $("#q").addEventListener("input", debounce((e) => { S.q = e.target.value.trim(); render(); }, 200));
  $("#sort").addEventListener("change", (e) => { S.sort = e.target.value; render(); });
  $("#reset").addEventListener("click", resetFilters);
  $("#open-filters").addEventListener("click", () => openFilters(true));
  $("#close-filters").addEventListener("click", () => openFilters(false));
  $("#apply-filters").addEventListener("click", () => openFilters(false));
  $("#scrim").addEventListener("click", () => openFilters(false));
  $("#run").addEventListener("click", startRun);
  $("#profile").addEventListener("change", (e) => { S.pid = e.target.value; S.filters = {}; S.item = null; skeleton(); loadProfile(S.pid); });
  $("#theme").addEventListener("click", cycleTheme);
  const dlg = $("#detail");
  dlg.addEventListener("click", (e) => { if (e.target === dlg) closeDetail(); });
  dlg.addEventListener("close", () => { $("#tip").hidden = true; if (S.item) { S.item = null; writeUrl(); } });
  window.addEventListener("popstate", () => {
    readUrl();
    if (!S.data) return;
    render();
    if (S.item) openDetail(S.item, false); else closeDetail();
  });
}

/* ---------------- theme ---------------- */

const THEMES = ["system", "light", "dark"];
function applyTheme(t) {
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  $("#theme").title = `Theme: ${t}`;
}
function cycleTheme() {
  let t = "system";
  try { t = localStorage.getItem("watcher-theme") || "system"; } catch {}
  t = THEMES[(THEMES.indexOf(t) + 1) % THEMES.length];
  try { localStorage.setItem("watcher-theme", t); } catch {}
  applyTheme(t);
  toast(`Theme: ${t}`);
}

/* ---------------- boot ---------------- */

(async function main() {
  try { applyTheme(localStorage.getItem("watcher-theme") || "system"); } catch { applyTheme("system"); }
  readUrl();
  bind();
  skeleton();
  try {
    S.profiles = await api("/api/profiles");
  } catch (e) {
    $("#title").textContent = "Can't reach the watcher server";
    $("#subtitle").textContent = e.message;
    return;
  }
  if (!S.profiles.length) { $("#title").textContent = "No profiles"; return; }
  if (!S.profiles.some((p) => p.id === S.pid)) S.pid = S.profiles[0].id;
  pollRun();
  await loadProfile(S.pid);
})();
