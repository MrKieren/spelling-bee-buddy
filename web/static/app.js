"use strict";

// How often to ask for a fresh sync while the page is open and visible.
const SYNC_EVERY_MS = 60_000;
const POLL_MS = 2_000;
const POLL_TIMEOUT_MS = 120_000;

let data = null;
let syncing = false;
let lastProblem = "";

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else n.setAttribute(k, v);
  }
  for (const c of children) n.append(c);
  return n;
}

// ---------- status line ----------

function ago(iso) {
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
}

function renderStatus() {
  const box = $("status");
  let cls = "ok", text;
  if (syncing) {
    cls = "syncing";
    text = data ? `Syncing… (last ${ago(data.updated_at)})` : "Syncing with NYT…";
  } else if (!data) {
    cls = lastProblem ? "err" : "warn";
    text = lastProblem || "Waiting for first sync…";
  } else {
    text = `Synced ${ago(data.updated_at)}`;
    if (lastProblem) { cls = "warn"; text += ` · ${lastProblem}`; }
    else if (!data.logged_in) { cls = "warn"; text += " · not logged in, totals only"; }
  }
  box.className = `status ${cls}`;
  $("status-text").textContent = text;
}

// ---------- rendering ----------

function render() {
  if (!data) return;
  const d = data;

  const hive = $("hive");
  hive.replaceChildren(el("span", { class: "center" }, d.center), ...d.outer.map((l) => el("span", {}, l)));
  $("date").textContent = d.print_date
    ? new Date(d.print_date + "T12:00:00").toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" })
    : "";

  // progress
  $("progress").hidden = false;
  $("rank").textContent = d.rank.name;
  $("points").textContent = `${d.found.points} / ${d.totals.points} pts`;
  $("bar-fill").style.width = `${(100 * d.found.points) / Math.max(1, d.totals.points)}%`;
  $("bar-genius").style.left = `${(100 * d.rank.genius_at) / Math.max(1, d.totals.points)}%`;
  $("s-words").textContent = `${d.found.words} / ${d.totals.words}`;
  const perfect = d.totals.perfect_pangrams ? ` (${d.totals.perfect_pangrams}★)` : "";
  $("s-pangrams").textContent = `${d.found.pangrams} / ${d.totals.pangrams}${perfect}`;
  $("s-next").textContent = d.rank.next ? `${d.rank.points_to_next} → ${d.rank.next}` : "Done!";

  renderTotals(d);
  renderGrid(d.grid);
  renderPangrams(d);
  renderTwo(d.two_letter);
  $("hints").hidden = false;
  sizeGrid(); // needs the section visible to measure it
  renderVisitors(d.visitors);
}

// Square cells: blank when there are no words, the count while some are
// left, and a filled square with a tick once they're all found.
function square(c, extra = "") {
  if (!c.total) return el("td", { class: `sq none ${extra}` }, "");
  if (c.remaining === 0) return el("td", { class: `sq done ${extra}`, "aria-label": "all found" }, "✓");
  return el("td", { class: `sq left ${extra}` }, String(c.remaining));
}

function renderTotals(d) {
  const t = d.totals;
  const perfect = t.perfect_pangrams ? ` (${t.perfect_pangrams} Perfect)` : "";
  $("totals").textContent =
    `WORDS: ${t.words}, POINTS: ${t.points}, PANGRAMS: ${t.pangrams}${perfect}`;
}

function renderGrid(g) {
  // Every length from the shortest to the longest, including ones with no words.
  const min = g.lengths[0], max = g.lengths[g.lengths.length - 1];
  const lengths = [];
  for (let n = min; n <= max; n++) lengths.push(n);
  const col = (cells, n) => {
    const i = g.lengths.indexOf(n);
    return i === -1 ? { total: 0, remaining: 0 } : cells[i];
  };
  const total = (c) => (!c.total ? "—" : c.remaining === 0 ? "✓" : String(c.remaining));

  const axis = el(
    "tr",
    { class: "axis" },
    el("th", { class: "corner" }, "First letter ↓"),
    el("th", { colspan: String(lengths.length), class: "across" }, "Letters in word →"),
    el("th", {}, ""),
  );
  const head = el(
    "tr",
    { class: "lengths" },
    el("th", {}, ""),
    ...lengths.map((n) => el("th", {}, String(n))),
    el("th", { class: "sum" }, "Σ"),
  );
  const rows = g.rows.map((r) =>
    el(
      "tr",
      {},
      el("th", { scope: "row" }, r.letter),
      ...lengths.map((n) => square(col(r.cells, n))),
      el("td", { class: "sum" }, total(r)),
    ),
  );
  const totalAll = g.rows.reduce((a, r) => a + r.total, 0);
  const remAll = g.rows.reduce((a, r) => a + r.remaining, 0);
  const foot = el(
    "tr",
    { class: "foot" },
    el("th", { scope: "row" }, "Σ"),
    ...lengths.map((n) => el("td", {}, total(col(g.column_totals, n)))),
    el("td", { class: "sum" }, total({ total: totalAll, remaining: remAll })),
  );
  $("grid").replaceChildren(el("thead", {}, axis, head), el("tbody", {}, ...rows, foot));
  sizeGrid();
}

// Keep grid cells square: size them from the space available.
function sizeGrid() {
  const grid = $("grid");
  const cols = grid.querySelectorAll("tr.lengths th").length; // label + lengths + Σ
  if (!cols) return;
  const avail = $("grid-wrap").clientWidth;
  // The letter-label column has a fixed 32px; the rest is shared by the cells.
  const cell = Math.max(26, Math.min(48, Math.floor(((avail - 34) / (cols - 1)) * 0.9)));
  // Set on the whole section so the two-letter boxes use the same size.
  $("hints").style.setProperty("--cell", `${cell}px`);
}
window.addEventListener("resize", sizeGrid);

function renderPangrams(d) {
  const total = d.totals.pangrams, found = d.found.pangrams;
  const perfectTotal = d.totals.perfect_pangrams || 0;
  const perfectFound = d.found.perfect_pangrams || 0;
  const badge = (got, perfect) =>
    el(
      "span",
      { class: "badge" },
      el("span", { class: `hex ${got ? "got" : ""} ${perfect ? "perfect" : ""}`, "aria-hidden": "true" },
        el("span", { class: "hex-inner" }, perfect ? "★" : "")),
      el("span", { class: "badge-label" }, perfect ? "Perfect" : ""),
    );
  const badges = [];
  for (let i = 0; i < perfectTotal; i++) badges.push(badge(i < perfectFound, true));
  const regularFound = found - perfectFound;
  for (let i = 0; i < total - perfectTotal; i++) badges.push(badge(i < regularFound, false));
  $("pangrams").replaceChildren(
    el("span", { class: "pangram-text" }, `Your pangrams (${found}/${total})`),
    el("span", { class: "hexes" }, ...badges),
  );
}


function renderTwo(groups) {
  $("two").replaceChildren(
    ...groups.map((g) =>
      el(
        "li",
        {},
        ...g.items.map((i) =>
          el(
            "span",
            { class: "pair" },
            el("span", { class: "pair-label" }, i.prefix.toUpperCase()),
            i.remaining === 0
              ? el("span", { class: "sq done", "aria-label": "all found" }, "✓")
              : el("span", { class: "sq left" }, String(i.remaining)),
          ),
        ),
      ),
    ),
  );
}

function renderVisitors(v) {
  const box = $("visitors");
  if (!v || !v.rows || !v.rows.length) { box.hidden = true; return; }
  $("v-rows").replaceChildren(
    ...v.rows.map((r) => {
      const label = r.found
        ? r.word.charAt(0).toUpperCase() + r.word.slice(1)
        : `${r.letter} (${r.length})`;
      const bar = el("span", { class: "vbar" }, el("span", { class: "vfill" }), el("span", { class: "vpct" }, `${Math.floor(r.pct)}%`));
      bar.firstChild.style.width = `${Math.max(0, Math.min(100, r.pct))}%`;
      return el(
        "li",
        { class: r.found ? "found" : "" },
        el("span", { class: "vcheck", "aria-label": r.found ? "found" : "not found" }, r.found ? "✓" : ""),
        el("span", { class: "vword" }, label),
        bar,
      );
    }),
  );
  $("v-note").textContent = v.sample
    ? `From a random sample of ${v.sample.toLocaleString()} Bee Buddy visitors' responses.`
    : "";
  box.hidden = false;
}

// ---------- data + sync ----------

async function loadData() {
  try {
    const r = await fetch("/buddy.json", { cache: "no-store" });
    if (r.status === 404) return;
    if (!r.ok) throw new Error(r.status);
    const next = await r.json();
    if (!data || next.updated_at !== data.updated_at) {
      data = next;
      render();
    }
  } catch {
    lastProblem = "couldn't load data";
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForSync(st) {
  const until = Date.now() + POLL_TIMEOUT_MS;
  while (st.running && Date.now() < until) {
    await sleep(POLL_MS);
    const s = await fetch("/api/status", { cache: "no-store" });
    if (s.ok) st = await s.json();
  }
  return st;
}

async function sync() {
  if (syncing) return;
  syncing = true;
  renderStatus();
  try {
    const r = await fetch("/api/sync", { method: "POST" });
    if (r.status === 429) { lastProblem = ""; return; } // someone just synced; fine
    if (!r.ok) throw new Error(r.status);
    const st = await waitForSync(await r.json());
    lastProblem = st.status === "error" ? "last sync failed" : st.status === "needs_login" ? "NYT login needed" : "";
  } catch {
    lastProblem = "sync unavailable";
  } finally {
    syncing = false;
    await loadData();
    renderStatus();
  }
}

// ---------- wiring ----------

let timer = null;
function schedule() {
  clearInterval(timer);
  timer = setInterval(() => { if (!document.hidden) sync(); }, SYNC_EVERY_MS);
}

// Coming back to the tab (common on mobile) counts as a fresh page load.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { sync(); schedule(); }
});

setInterval(renderStatus, 5_000);

(async () => {
  await loadData();   // show the cached copy immediately
  renderStatus();
  sync();             // then check the live version
  schedule();         // and keep it fresh
})();
