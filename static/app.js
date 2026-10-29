const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let data = { jobs: [], threshold: 70, resume: null };
let reasons = {};
let tab = localStorage.getItem("tab") || "recommended";
let visible = [];          // jobs currently shown in the list, in order
let selectedId = null;
let detailTab = "jd";      // "jd" | "resume"
let detailKey = "";        // what the detail pane last rendered, to avoid needless re-renders
const detailCache = {};    // job id -> full job (with description)
const tailorCache = {};    // job id -> tailored resume markdown (null = none yet)
const openedPostings = new Set();  // undecided jobs whose posting you opened: ask "Did you apply?"

const TABS = [
  { id: "recommended", label: "Recommended", test: (j, t) => j.status === "new" && j.score != null && j.score >= t },
  { id: "low", label: "Low match", test: (j, t) => j.status === "new" && j.score != null && j.score < t },
  { id: "unscored", label: "Awaiting score", test: (j) => j.status === "new" && j.filter_status === "passed" && j.score == null },
  { id: "applied", label: "Applied", test: (j) => j.status === "applied" },
  { id: "declined", label: "Not interested", test: (j) => j.status === "not_interested" },
  { id: "filtered", label: "Filtered out", test: (j) => j.filter_status === "dropped" && j.status === "new" },
  { id: "insights", label: "Insights" },
];

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

// ------------------------------------------------------------------ formatting helpers

function scoreClass(s, t) {
  if (s == null) return "none";
  if (s >= t) return "good";
  return s >= t - 20 ? "mid" : "bad";
}

// Some sources only give a posting date (stored as midnight UTC); don't pretend to know the hour.
const isDateOnly = (iso) => /T00:00:00(\+00:00|Z)?$/.test(iso || "");

function ago(iso) {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (isDateOnly(iso)) {
    const today = new Date(); today.setUTCHours(0, 0, 0, 0);
    const days = Math.round((today.getTime() - t) / 86400000);
    if (days <= 0) return "today";
    if (days === 1) return "yesterday";
    return days < 30 ? `${days} days ago` : fullTime(iso);
  }
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) { const h = Math.floor(s / 3600); return `${h} hour${h === 1 ? "" : "s"} ago`; }
  if (s < 172800) return "yesterday";
  if (s < 30 * 86400) return `${Math.floor(s / 86400)} days ago`;
  return fullTime(iso);
}

function fullTime(iso) {
  if (!iso) return "";
  return isDateOnly(iso)
    ? new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium", timeZone: "UTC" })
    : new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** A relative time ("3 hours ago") that shows the exact time on hover and refreshes every minute. */
function timeTag(iso, prefix = "") {
  if (!iso) return "";
  return `<time datetime="${esc(iso)}" data-prefix="${esc(prefix)}" title="${esc(fullTime(iso))}">${esc(prefix + ago(iso))}</time>`;
}

setInterval(() => {
  document.querySelectorAll("time[datetime]").forEach((el) => {
    el.textContent = el.dataset.prefix + ago(el.getAttribute("datetime"));
  });
}, 60000);

function shortDate(iso) {
  return iso ? new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "";
}

function badges(j) {
  const t = data.threshold;
  let b = "";
  if (j.status === "applied") b += `<span class="badge applied">✓ Applied ${esc(shortDate(j.decided_at))}</span>`;
  if (j.status === "not_interested") {
    b += `<span class="badge declined">${esc(reasons[j.decline_reason] || j.decline_reason)}</span>`;
    if (j.score != null && j.score >= t) b += `<span class="badge warn">Matched but declined</span>`;
  }
  if (j.sponsorship === "not_offered") b += `<span class="badge declined">No sponsorship</span>`;
  if (j.years_required) b += `<span class="badge">${j.years_required}+ yrs</span>`;
  if (j.tailored_count) b += `<span class="badge">Tailored resume</span>`;
  return b;
}

// Phrases worth seeing at a glance in a job description.
const HIGHLIGHTS = [
  ["warn", /\b(visa|sponsor(?:ship|ed|ing)?|work authori[sz]ation|authori[sz]ed to work|(?:us |u\.s\. )?citizen(?:ship)?|green card|security clearance|clearance)\b/gi],
  ["years", /\b\d{1,2}\s*\+?(?:\s*(?:-|–|to)\s*\d{1,2})?\+?\s*years?\b/gi],
  ["pay", /(?:[$€£]\s?\d[\d,.]*\s*[kKmM]?(?:\s*(?:-|–|to)\s*[$€£]?\s?\d[\d,.]*\s*[kKmM]?)?(?:\s*(?:USD|EUR|GBP|CAD))?)/g],
];

// Same heading words app/jd_parse.py uses to split must-haves from nice-to-haves.
const BONUS = ["stand out", "nice to have", "bonus", "preferred", "plus"];
const REQ = ["you bring", "requirement", "qualification", "must have", "you have", "about you", "what we look"];

function inline(text, highlight) {
  let s = esc(text);
  // Skip highlighting on lines with links so a <mark> can never land inside a URL.
  if (highlight && !/https?:\/\//.test(text)) {
    for (const [cls, re] of HIGHLIGHTS) s = s.replace(re, (m) => `<mark class="${cls}">${m}</mark>`);
  }
  s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  return s;
}

function headingTag(title) {
  const t = title.toLowerCase().replace(/’/g, "'");
  if (BONUS.some((w) => t.includes(w))) return `<span class="req-tag nice">nice to have</span>`;
  if (REQ.some((w) => t.includes(w))) return `<span class="req-tag must">must-have</span>`;
  return "";
}

/** Small, safe Markdown renderer: everything is escaped first, then formatted. */
function renderMarkdown(md, { highlight = false, tagSections = false } = {}) {
  const out = [];
  let list = null;   // "ul" | "ol"
  let para = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${para.map((l) => inline(l, highlight)).join("<br>")}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const openList = (type) => { if (list !== type) { flushList(); out.push(`<${type}>`); list = type; } };

  // Descriptions saved before the <li><p> fix have the bullet dash on its own line; rejoin it.
  md = (md || "").replace(/^[-•]\s*\n+(?=[^\n-])/gm, "- ");
  for (const raw of md.split("\n")) {
    const line = raw.trim();
    let m;
    if (!line) { flushPara(); continue; }  // a list continues across blank lines until other text appears
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line)) { flushPara(); flushList(); out.push("<hr>"); continue; }
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      flushPara(); flushList();
      const level = m[1].length <= 2 ? "h3" : "h4";
      out.push(`<${level}>${inline(m[2].replace(/\*\*/g, ""), false)}${tagSections ? headingTag(m[2]) : ""}</${level}>`);
      continue;
    }
    // A line that is only bold text is a section heading (that's how job boards format them).
    if ((m = line.match(/^\*\*(.+?)\*\*:?$/))) {
      flushPara(); flushList();
      out.push(`<h4>${inline(m[1], false)}${tagSections ? headingTag(m[1]) : ""}</h4>`);
      continue;
    }
    if ((m = line.match(/^[-*•·]\s+(.*)$/))) { flushPara(); openList("ul"); out.push(`<li>${inline(m[1], highlight)}</li>`); continue; }
    if ((m = line.match(/^\d+[.)]\s+(.*)$/))) { flushPara(); openList("ol"); out.push(`<li>${inline(m[1], highlight)}</li>`); continue; }
    flushList();
    para.push(line);
  }
  flushPara(); flushList();
  return out.join("\n");
}

// ------------------------------------------------------------------ list (left)

function renderTabs() {
  $("#tabs").innerHTML = TABS.map((t) => {
    const n = t.test ? data.jobs.filter((j) => t.test(j, data.threshold)).length : "";
    return `<button class="tab ${t.id === tab ? "active" : ""}" data-tab="${t.id}">${t.label}${n !== "" ? `<span class="count">${n}</span>` : ""}</button>`;
  }).join("");
}

function row(j) {
  const sc = scoreClass(j.score, data.threshold);
  const scoreTxt = j.score != null ? j.score : j.filter_status === "dropped" ? "drop" : "—";
  const meta = [j.company, j.location].filter(Boolean).map(esc).join(" · ");
  const meta2 = [j.salary && esc(j.salary), timeTag(j.posted_at, "posted "), timeTag(j.fetched_at, "fetched "), esc(j.source)]
    .filter(Boolean).join(" · ");
  const b = badges(j);
  return `<article class="job-row ${j.id === selectedId ? "selected" : ""}" data-id="${j.id}">
    <div class="score ${sc}" title="${esc(j.verdict || "")}">${scoreTxt}</div>
    <div>
      <div class="title">${esc(j.title)}</div>
      <div class="meta">${meta}</div>
      <div class="meta">${meta2}</div>
      ${b ? `<div class="badges">${b}</div>` : ""}
    </div>
  </article>`;
}

function filteredJobs() {
  const def = TABS.find((t) => t.id === tab);
  const q = $("#search").value.toLowerCase().trim();
  const days = Number($("#posted-filter").value);
  let jobs = data.jobs.filter((j) => def.test(j, data.threshold));
  if (q) {
    // Every word must appear somewhere: "python remote" matches jobs with both.
    const words = q.split(/\s+/);
    jobs = jobs.filter((j) => {
      const hay = [j.title, j.company, j.location, j.summary, j.salary, ...j.matched].join(" ").toLowerCase();
      return words.every((w) => hay.includes(w));
    });
  }
  if (days) {
    const cutoff = Date.now() - days * 86400000;
    jobs = jobs.filter((j) => new Date(j.posted_at || j.fetched_at).getTime() >= cutoff);
  }
  const sort = $("#sort").value;
  if (sort === "date") jobs.sort((a, b) => (b.posted_at || b.fetched_at).localeCompare(a.posted_at || a.fetched_at));
  if (sort === "fetched") jobs.sort((a, b) => b.fetched_at.localeCompare(a.fetched_at) || b.id - a.id);
  return jobs;
}

function render() {
  renderTabs();
  $("#resume-name").textContent = data.resume || "none — upload one";
  const isInsights = tab === "insights";
  $("#main").classList.toggle("insights-mode", isInsights);
  $("#insights").hidden = !isInsights;
  if (isInsights) { renderInsights(); return; }

  visible = filteredJobs();
  if (!visible.some((j) => j.id === selectedId)) selectedId = visible[0]?.id ?? null;
  $("#list-count").textContent = `${visible.length} job${visible.length === 1 ? "" : "s"}`;
  $("#list").innerHTML = visible.length ? visible.map(row).join("") : `<p class="empty">No jobs here.</p>`;
  renderDetail();
}

function select(id, { scroll = false, open = true } = {}) {
  if (id == null) return;
  selectedId = id;
  document.querySelectorAll(".job-row.selected").forEach((el) => el.classList.remove("selected"));
  const el = document.querySelector(`.job-row[data-id="${id}"]`);
  if (el) {
    el.classList.add("selected");
    if (scroll) el.scrollIntoView({ block: "nearest" });
  }
  if (open) $("#main").classList.add("show-detail");
  renderDetail();
}

// ------------------------------------------------------------------ detail (right)

async function renderDetail() {
  const pane = $("#detail");
  const j = data.jobs.find((x) => x.id === selectedId);
  if (!j) {
    detailKey = "";
    pane.innerHTML = `<p class="empty">${visible.length ? "Select a job to see its description." : "No jobs in this tab."}</p>`;
    return;
  }
  // Skip re-rendering when nothing visible changed (status polling reloads the list every 2s).
  const key = [j.id, j.score, j.status, j.decline_reason, j.tailored_count, detailTab, openedPostings.has(j.id)].join("|");
  if (key === detailKey) return;
  const sameJob = detailKey.split("|")[0] === String(j.id);
  detailKey = key;

  const scrollTop = sameJob ? pane.scrollTop : 0;
  pane.innerHTML = detailHtml(j, detailCache[j.id]);
  pane.scrollTop = scrollTop;

  if (!detailCache[j.id]) {
    try {
      detailCache[j.id] = await api(`/api/jobs/${j.id}`);
    } catch (e) {
      detailCache[j.id] = { description: `Could not load the description: ${e.message}` };
    }
    if (selectedId === j.id) { detailKey = ""; renderDetail(); }
  }
  if (detailTab === "resume" && !(j.id in tailorCache)) {
    try {
      tailorCache[j.id] = (await api(`/api/jobs/${j.id}/tailor`)).content;
    } catch {
      tailorCache[j.id] = null;
    }
    if (selectedId === j.id) { detailKey = ""; renderDetail(); }
  }
}

function detailHtml(j, full) {
  const sc = scoreClass(j.score, data.threshold);
  const scoreTxt = j.score != null ? j.score : j.filter_status === "dropped" ? "drop" : "—";
  const meta = [j.company, j.location, j.salary, j.source].filter(Boolean).map(esc).join(" · ");
  const times = [
    j.posted_at ? `Posted ${esc(fullTime(j.posted_at))} (${timeTag(j.posted_at)})` : "Posting date not given",
    `Fetched ${esc(fullTime(j.fetched_at))} (${timeTag(j.fetched_at)})`,
  ].join(" · ");

  // "Open posting" only opens the website; applying is recorded separately.
  let actions = `<button class="btn primary" data-act="open">Open posting ↗</button>`;
  let banner = "";
  if (j.status === "new") {
    actions += `<button class="btn success" data-act="mark-applied">✓ Mark as applied</button>
                <button class="btn danger" data-act="decline">Not interested</button>`;
    if (openedPostings.has(j.id)) {
      banner = `<div class="apply-prompt">
        <strong>Did you apply to this job?</strong>
        <button class="btn small success" data-act="mark-applied">Yes, I applied</button>
        <button class="btn small" data-act="dismiss-prompt">Not yet</button>
      </div>`;
    }
  } else if (j.status === "applied") {
    banner = `<p class="applied-note">✓ You applied on ${esc(new Date(j.decided_at).toLocaleDateString(undefined, { dateStyle: "medium" }))}</p>`;
    actions += `<button class="btn" data-act="undo">Undo applied</button>`;
  } else {
    actions += `<button class="btn" data-act="undo">Undo not interested</button>`;
  }

  let assessment = "";
  if (j.score != null) {
    const matched = j.matched.length
      ? `<h4>Matches</h4><div class="chips">${j.matched.map((m) => `<span class="chip ok">✓ ${esc(m)}</span>`).join("")}</div>` : "";
    const gaps = j.gaps.length
      ? `<h4>Gaps</h4><ul class="gaps">${j.gaps.map((g) => `<li><span class="sev ${g.severity}">${g.severity}</span><strong>${esc(g.category.replace(/_/g, " "))}:</strong> ${esc(g.detail)}</li>`).join("")}</ul>` : "";
    assessment = `<div class="assessment">
      <h4>Assessment · ${esc((j.verdict || "").replace(/_/g, " "))}</h4>
      ${j.summary ? `<p>${esc(j.summary)}</p>` : ""}${matched}${gaps}
    </div>`;
  }
  const notes = [
    j.filter_status === "dropped" ? `Dropped by keyword filter: ${j.filter_reason}` : "",
    j.decline_note ? `Your note: ${j.decline_note}` : "",
  ].filter(Boolean).map((n) => `<p class="muted">${esc(n)}</p>`).join("");

  let body;
  if (detailTab === "jd") {
    body = full
      ? `<div class="legend">Highlighted: <mark class="warn">visa / sponsorship</mark><mark class="years">years of experience</mark><mark class="pay">pay</mark></div>
         <article class="md">${renderMarkdown(full.description || "(no description)", { highlight: true, tagSections: true })}</article>`
      : `<p class="empty">Loading description…</p>`;
  } else {
    const content = tailorCache[j.id];
    if (content === undefined) body = `<p class="empty">Loading…</p>`;
    else if (content === "__writing__") body = `<p class="empty">Writing a resume tailored to this job description… this can take a minute or two.</p>`;
    else if (!content) body = `<div class="tool-row"><button class="btn primary" data-act="tailor">Write a tailored resume</button></div>
      <p class="muted">Rewrites your uploaded resume for this job description using your LLM. It won't invent experience; gaps it can't cover are listed at the end.</p>`;
    else body = `<div class="tool-row">
        <button class="btn small" data-act="copy-resume">Copy</button>
        <a class="btn small" href="/api/jobs/${j.id}/tailor.md">Download .md</a>
        <button class="btn small" data-act="tailor">Regenerate</button>
      </div>
      <article class="md">${renderMarkdown(content)}</article>`;
  }

  return `<div class="detail">
    <button class="btn small back-btn" data-act="back">← Jobs</button>
    <div class="detail-head">
      <div class="score lg ${sc}" title="${esc(j.verdict || "")}">${scoreTxt}</div>
      <div>
        <h2><a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a></h2>
        <div class="detail-meta">${meta}</div>
        <div class="detail-meta detail-times">${times}</div>
        <div class="badges" style="margin-top:6px">${badges(j)}</div>
      </div>
    </div>
    <div class="detail-actions">${actions}</div>
    ${banner}${notes}${assessment}
    <div class="subtabs">
      <button class="subtab ${detailTab === "jd" ? "active" : ""}" data-subtab="jd">Job description</button>
      <button class="subtab ${detailTab === "resume" ? "active" : ""}" data-subtab="resume">Tailored resume</button>
    </div>
    ${body}
  </div>`;
}

// ------------------------------------------------------------------ insights

function countBy(items, key) {
  const m = {};
  items.forEach((x) => { const k = key(x); if (k) m[k] = (m[k] || 0) + 1; });
  return Object.entries(m).sort((a, b) => b[1] - a[1]);
}

function barPanel(title, rows, emptyText) {
  const max = Math.max(1, ...rows.map((r) => r[1]));
  const body = rows.length
    ? rows.slice(0, 10).map(([k, n]) => `<div class="bar-row"><div>${esc(k)}<div class="bar"><span style="width:${(n / max) * 100}%"></span></div></div><div>${n}</div></div>`).join("")
    : `<p class="muted">${emptyText}</p>`;
  return `<div class="panel"><h4>${title}</h4>${body}</div>`;
}

function renderInsights() {
  const t = data.threshold;
  const jobs = data.jobs;
  const scored = jobs.filter((j) => j.score != null);
  const declined = jobs.filter((j) => j.status === "not_interested");
  const matchedDeclined = declined.filter((j) => j.score != null && j.score >= t);
  const lowGaps = scored.filter((j) => j.score < t).flatMap((j) => j.gaps.filter((g) => g.severity !== "low"));

  const funnel = [
    ["Fetched", jobs.length],
    ["Passed keyword filter", jobs.filter((j) => j.filter_status === "passed").length],
    ["Scored by LLM", scored.length],
    [`Match (≥ ${t})`, scored.filter((j) => j.score >= t).length],
    ["Applied", jobs.filter((j) => j.status === "applied").length],
  ];
  $("#insights").innerHTML = [
    barPanel("Pipeline funnel", funnel, ""),
    barPanel("Why matched jobs were declined", countBy(matchedDeclined, (j) => reasons[j.decline_reason] || j.decline_reason), "No matched jobs declined yet."),
    barPanel("All decline reasons", countBy(declined, (j) => reasons[j.decline_reason] || j.decline_reason), "Nothing declined yet."),
    barPanel("Most common gaps in low matches", countBy(lowGaps, (g) => g.category.replace(/_/g, " ")), "No gaps recorded."),
    barPanel("Keyword filter drops", countBy(jobs.filter((j) => j.filter_status === "dropped"), (j) => j.filter_reason.replace(/'.*'/, "…").replace(/\d+/g, "N")), "Nothing filtered."),
  ].join("");
}

async function load() {
  data = await api("/api/jobs");
  render();
}

// ------------------------------------------------------------------ actions

function nextAfter(id) {
  const i = visible.findIndex((j) => j.id === id);
  return visible[i + 1]?.id ?? visible[i - 1]?.id ?? null;
}

async function decide(id, status, reason = null, note = null) {
  // The decided job usually leaves this tab, so move on to the next one.
  const next = nextAfter(id);
  await api(`/api/jobs/${id}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status, reason, note }),
  });
  await load();
  if (!visible.some((j) => j.id === id) && next != null) select(next, { scroll: true, open: false });
}

async function tailor(id) {
  tailorCache[id] = "__writing__";
  detailKey = "";
  renderDetail();
  try {
    tailorCache[id] = (await api(`/api/jobs/${id}/tailor`, { method: "POST" })).content;
  } catch (e) {
    tailorCache[id] = null;
    setStatus(`Tailored resume failed: ${e.message}`, true);
  }
  await load();
  detailKey = "";
  renderDetail();
}

function askDecline(id) {
  const job = data.jobs.find((j) => j.id === id);
  $("#decline-job").textContent = `${job.title} — ${job.company || ""}`;
  // Pre-select a reason that matches the LLM's biggest gap, when there is one.
  const guess = { experience_years: "experience_gap", sponsorship: "no_sponsorship", seniority: "seniority_too_high", skills: "tech_stack", domain: "domain", location: "location" };
  const topGap = job.gaps.find((g) => g.severity === "high") || job.gaps[0];
  const preselect = job.sponsorship === "not_offered" ? "no_sponsorship" : guess[topGap?.category];
  // Number keys 1–9, then 0 for the 10th reason.
  const keys = Object.keys(reasons).map((_, i) => (i < 9 ? String(i + 1) : i === 9 ? "0" : ""));
  $("#reason-list").innerHTML = Object.entries(reasons).map(([k, label], i) =>
    `<label class="reason-tile ${k === preselect ? "checked" : ""}" data-key="${keys[i]}">
       <input type="radio" name="reason" value="${k}" ${k === preselect ? "checked" : ""}>
       <span class="key">${keys[i]}</span><span>${esc(label)}</span>
     </label>`).join("");
  $("#decline-note").value = "";
  $("#decline-save").disabled = !preselect;
  const dlg = $("#decline");
  dlg.returnValue = "";
  dlg.onclose = () => {
    if (dlg.returnValue !== "ok") return;
    const reason = document.querySelector('input[name="reason"]:checked')?.value;
    if (reason) decide(id, "not_interested", reason, $("#decline-note").value.trim() || null);
  };
  dlg.showModal();
  $("#decline-save").focus();
}

function pickReason(tile) {
  if (!tile) return;
  tile.querySelector("input").checked = true;
  document.querySelectorAll(".reason-tile").forEach((t) => t.classList.toggle("checked", t === tile));
  $("#decline-save").disabled = false;
}

$("#reason-list").addEventListener("change", (e) => pickReason(e.target.closest(".reason-tile")));
$("#reason-list").addEventListener("dblclick", (e) => {
  const tile = e.target.closest(".reason-tile");
  if (!tile) return;
  pickReason(tile);
  $("#decline").close("ok");
});
$("#decline").addEventListener("keydown", (e) => {
  if (e.target.matches("textarea")) {
    // Cmd/Ctrl+Enter saves from inside the note.
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !$("#decline-save").disabled) $("#decline").close("ok");
    return;
  }
  const tile = document.querySelector(`.reason-tile[data-key="${e.key}"]`);
  if (tile) { e.preventDefault(); pickReason(tile); }
});

$("#list").addEventListener("click", (e) => {
  const rowEl = e.target.closest(".job-row");
  if (rowEl) select(Number(rowEl.dataset.id));
});

$("#detail").addEventListener("click", (e) => {
  const sub = e.target.closest("[data-subtab]");
  if (sub) { detailTab = sub.dataset.subtab; renderDetail(); return; }
  const btn = e.target.closest("[data-act]");
  if (!btn || selectedId == null) return;
  const id = selectedId;
  const job = data.jobs.find((j) => j.id === id);
  switch (btn.dataset.act) {
    case "open":
      window.open(job.url, "_blank", "noopener");
      if (job.status === "new") { openedPostings.add(id); renderDetail(); }
      return;
    case "mark-applied":
      openedPostings.delete(id);
      return decide(id, "applied");
    case "dismiss-prompt":
      openedPostings.delete(id);
      return renderDetail();
    case "decline": return askDecline(id);
    case "undo": return decide(id, "new");
    case "tailor": return tailor(id);
    case "copy-resume": return navigator.clipboard.writeText(tailorCache[id] || "");
    case "back": return $("#main").classList.remove("show-detail");
  }
});

// ↑/↓ (or k/j) move through the list when you're not typing.
document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, textarea, select, dialog") || e.metaKey || e.ctrlKey || e.altKey) return;
  const step = { ArrowDown: 1, j: 1, ArrowUp: -1, k: -1 }[e.key];
  if (!step || !visible.length) return;
  e.preventDefault();
  const i = visible.findIndex((j) => j.id === selectedId);
  const next = visible[Math.min(visible.length - 1, Math.max(0, i + step))];
  select(next.id, { scroll: true, open: false });
});

$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-tab]");
  if (!b) return;
  tab = b.dataset.tab;
  try { localStorage.setItem("tab", tab); } catch {}
  $("#main").classList.remove("show-detail");
  render();
});
$("#search").addEventListener("input", render);
$("#sort").addEventListener("change", render);
$("#posted-filter").addEventListener("change", () => {
  try { localStorage.setItem("postedFilter", $("#posted-filter").value); } catch {}
  render();
});
try { $("#posted-filter").value = localStorage.getItem("postedFilter") || ""; } catch {}
document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));

$("#resume-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  setStatus(`Uploading ${file.name}…`);
  try {
    const r = await api("/api/resumes", { method: "POST", body: fd });
    setStatus(`Resume loaded (${r.chars} characters). Click "Fetch & score" or "Re-rank" to score jobs against it.`);
    await load();
  } catch (err) {
    setStatus(err.message, true);
  }
  e.target.value = "";
});

// ------------------------------------------------------------------ LLM connection

async function loadLLM() {
  const s = await api("/api/llm");
  $("#llm-name").textContent = s.base_url ? new URL(s.base_url).host + (s.model ? ` · ${s.model}` : "") : "not set";
  return s;
}

$("#llm-btn").onclick = async () => {
  const s = await loadLLM();
  $("#llm-url").value = s.base_url;
  $("#llm-kind").value = s.kind;
  $("#llm-model").value = s.model;
  $("#llm-key").value = "";
  $("#llm-key").placeholder = s.has_api_key ? "saved — leave blank to keep" : "";
  $("#llm-result").textContent = "";
  $("#llm-result").className = "muted";
  $("#llm-dialog").showModal();
};

$("#llm-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const result = $("#llm-result");
  const key = $("#llm-key").value;
  result.className = "muted";
  result.textContent = "Saving and sending a test message…";
  try {
    await api("/api/llm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_url: $("#llm-url").value, kind: $("#llm-kind").value, model: $("#llm-model").value, api_key: key || null }),
    });
    await loadLLM();
    const t = await api("/api/llm/test", { method: "POST" });
    result.className = "ok";
    result.textContent = `Connected: ${t.kind} server, model "${t.model}". Reply: ${t.reply}`;
  } catch (err) {
    result.className = "error";
    result.textContent = err.message;
  }
});

// ------------------------------------------------------------------ pipeline

function setStatus(text, isError = false) {
  $("#status").textContent = text;
  $("#status").classList.toggle("error", isError);
}

let polling = null;
async function pollStatus() {
  const s = await api("/api/pipeline/status");
  $("#run-btn").disabled = $("#rescore-btn").disabled = s.running;
  if (s.running) {
    setStatus(`Running: ${s.stage} ${s.progress}`);
    load();
    if (!polling) polling = setInterval(pollStatus, 2000);
  } else {
    if (polling) { clearInterval(polling); polling = null; load(); }
    const last = s.runs[0];
    const when = last ? ` (last run ${new Date(last.started_at).toLocaleString()})` : "";
    if (s.last_error) setStatus(`${s.stage === "failed" ? "Failed" : "Done with warnings"}: ${s.last_error}`, true);
    else if (s.stage === "done") setStatus(`Done: ${s.progress}`);
    else setStatus(last ? `Last run: ${last.new_jobs} new, ${last.passed} passed filter, ${last.scored} scored${when}` : "No runs yet.");
  }
}

async function start(path, body = null) {
  try {
    await api(path, body
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : { method: "POST" });
    pollStatus();
  } catch (e) {
    setStatus(e.message, true);
  }
}

let fetchOptions = null;
$("#run-btn").onclick = async () => {
  if (!fetchOptions) {
    fetchOptions = await api("/api/pipeline/options");
    $("#fetch-queries").value = fetchOptions.queries.join("\n");
    $("#fetch-posted").innerHTML = Object.entries(fetchOptions.posted_within)
      .map(([k, label]) => `<option value="${k}">${esc(label)}</option>`).join("");
    $("#fetch-posted").value = fetchOptions.default_posted_within;
  }
  $("#fetch-dialog").showModal();
};
$("#fetch-form").addEventListener("submit", (e) => {
  e.preventDefault();
  $("#fetch-dialog").close();
  start("/api/pipeline/run", {
    queries: $("#fetch-queries").value.split("\n").map((s) => s.trim()).filter(Boolean),
    posted_within: $("#fetch-posted").value,
  });
});
$("#rescore-btn").onclick = () => start("/api/pipeline/rescore");

(async () => {
  reasons = await api("/api/reasons");
  loadLLM();
  await load();
  pollStatus();
})();
