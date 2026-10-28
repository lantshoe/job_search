const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let data = { jobs: [], threshold: 70, resume: null };
let reasons = {};
let tab = localStorage.getItem("tab") || "recommended";

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

// ------------------------------------------------------------------ rendering

function scoreClass(s, t) {
  if (s == null) return "none";
  if (s >= t) return "good";
  return s >= t - 20 ? "mid" : "bad";
}

function renderTabs() {
  $("#tabs").innerHTML = TABS.map((t) => {
    const n = t.test ? data.jobs.filter((j) => t.test(j, data.threshold)).length : "";
    return `<button class="tab ${t.id === tab ? "active" : ""}" data-tab="${t.id}">${t.label}${n !== "" ? `<span class="count">${n}</span>` : ""}</button>`;
  }).join("");
}

function card(j) {
  const t = data.threshold;
  const sc = scoreClass(j.score, t);
  const scoreTxt = j.score != null ? j.score : j.filter_status === "dropped" ? "drop" : "—";
  const meta = [j.company, j.location, j.salary, j.source, j.posted_at && new Date(j.posted_at).toLocaleDateString()]
    .filter(Boolean).map(esc).join(" · ");

  let badges = "";
  if (j.status === "applied") badges += `<span class="badge applied">Applied</span>`;
  if (j.status === "not_interested") {
    badges += `<span class="badge declined">${esc(reasons[j.decline_reason] || j.decline_reason)}</span>`;
    if (j.score != null && j.score >= t) badges += `<span class="badge warn">Matched but declined</span>`;
  }
  if (j.sponsorship === "not_offered") badges += `<span class="badge declined">No sponsorship</span>`;
  if (j.years_required) badges += `<span class="badge">${j.years_required}+ yrs</span>`;
  if (j.tailored_count) badges += `<span class="badge">Tailored resume</span>`;

  const matched = j.matched.length
    ? `<div class="chips">${j.matched.map((m) => `<span class="chip ok">✓ ${esc(m)}</span>`).join("")}</div>` : "";
  const gaps = j.gaps.length
    ? `<ul class="gaps">${j.gaps.map((g) => `<li><span class="sev ${g.severity}">${g.severity}</span><strong>${esc(g.category.replace("_", " "))}:</strong> ${esc(g.detail)}</li>`).join("")}</ul>` : "";
  const filterNote = j.filter_status === "dropped" ? `<p class="summary muted">Dropped by keyword filter: ${esc(j.filter_reason)}</p>` : "";
  const declineNote = j.decline_note ? `<p class="summary muted">Your note: ${esc(j.decline_note)}</p>` : "";

  let actions = `<button class="btn small" data-act="jd" data-id="${j.id}">View JD</button>`;
  if (j.status === "new") {
    actions += `<button class="btn small primary" data-act="apply" data-id="${j.id}">Apply ↗</button>
                <button class="btn small danger" data-act="decline" data-id="${j.id}">Not interested</button>`;
  } else {
    actions += `<button class="btn small" data-act="undo" data-id="${j.id}">Undo</button>`;
  }
  actions += `<button class="btn small" data-act="tailor" data-id="${j.id}">${j.tailored_count ? "Tailored resume" : "Tailor resume"}</button>`;

  return `<article class="card">
    <div class="score ${sc}" title="${esc(j.verdict || "")}">${scoreTxt}</div>
    <div>
      <h3><a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a>${badges}</h3>
      <div class="meta">${meta}</div>
      ${j.summary ? `<p class="summary">${esc(j.summary)}</p>` : ""}
      ${filterNote}${declineNote}${matched}${gaps}
      <div class="card-actions">${actions}</div>
    </div>
  </article>`;
}

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
    barPanel("Most common gaps in low matches", countBy(lowGaps, (g) => g.category.replace("_", " ")), "No gaps recorded."),
    barPanel("Keyword filter drops", countBy(jobs.filter((j) => j.filter_status === "dropped"), (j) => j.filter_reason.replace(/'.*'/, "…").replace(/\d+/g, "N")), "Nothing filtered."),
  ].join("");
}

function render() {
  renderTabs();
  $("#resume-name").textContent = data.resume || "none — upload one";
  const isInsights = tab === "insights";
  $("#insights").hidden = !isInsights;
  if (isInsights) {
    renderInsights();
    $("#list").innerHTML = "";
    return;
  }
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
  if ($("#sort").value === "date") jobs.sort((a, b) => (b.posted_at || b.fetched_at).localeCompare(a.posted_at || a.fetched_at));
  $("#list").innerHTML = jobs.length ? jobs.map(card).join("") : `<p class="empty">No jobs here.</p>`;
}

async function load() {
  data = await api("/api/jobs");
  render();
}

// ------------------------------------------------------------------ actions

async function decide(id, status, reason = null, note = null) {
  await api(`/api/jobs/${id}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status, reason, note }),
  });
  await load();
}

function openViewer(title, text, actionsHtml = "") {
  $("#viewer-title").textContent = title;
  $("#viewer-body").textContent = text;
  $("#viewer-actions").innerHTML = actionsHtml;
  if (!$("#viewer").open) $("#viewer").showModal();
}

async function showJD(id) {
  const job = await api(`/api/jobs/${id}`);
  openViewer(`${job.title} — ${job.company || ""}`, job.description || "(no description)",
    `<a class="btn small" href="${esc(job.url)}" target="_blank" rel="noopener">Open posting ↗</a>`);
}

async function showTailor(id, regenerate = false) {
  const job = data.jobs.find((j) => j.id === id);
  const title = `Tailored resume — ${job.title}`;
  let content = regenerate ? null : (await api(`/api/jobs/${id}/tailor`)).content;
  if (!content) {
    openViewer(title, "Writing a resume tailored to this job description… (this can take a minute)");
    try {
      content = (await api(`/api/jobs/${id}/tailor`, { method: "POST" })).content;
    } catch (e) {
      openViewer(title, `Failed: ${e.message}`);
      return;
    }
    load();
  }
  openViewer(title, content, `
    <button class="btn small" id="copy-tailored">Copy</button>
    <a class="btn small" href="/api/jobs/${id}/tailor.md">Download .md</a>
    <button class="btn small" id="regen-tailored">Regenerate</button>`);
  $("#copy-tailored").onclick = () => navigator.clipboard.writeText(content);
  $("#regen-tailored").onclick = () => showTailor(id, true);
}

function askDecline(id) {
  const job = data.jobs.find((j) => j.id === id);
  $("#decline-job").textContent = `${job.title} — ${job.company || ""}`;
  // Pre-select a reason that matches the LLM's biggest gap, when there is one.
  const guess = { experience_years: "experience_gap", sponsorship: "no_sponsorship", seniority: "seniority_too_high", skills: "tech_stack", domain: "domain", location: "location" };
  const topGap = job.gaps.find((g) => g.severity === "high") || job.gaps[0];
  const preselect = job.sponsorship === "not_offered" ? "no_sponsorship" : guess[topGap?.category];
  $("#reason-list").innerHTML = Object.entries(reasons).map(([k, label]) =>
    `<label><input type="radio" name="reason" value="${k}" required ${k === preselect ? "checked" : ""}> ${esc(label)}</label>`).join("");
  $("#decline-note").value = "";
  const dlg = $("#decline");
  dlg.onclose = () => {
    if (dlg.returnValue !== "ok") return;
    const reason = document.querySelector('input[name="reason"]:checked')?.value;
    if (reason) decide(id, "not_interested", reason, $("#decline-note").value.trim() || null);
  };
  dlg.showModal();
}

$("#list").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const id = Number(btn.dataset.id);
  const job = data.jobs.find((j) => j.id === id);
  switch (btn.dataset.act) {
    case "jd": return showJD(id);
    case "apply":
      window.open(job.url, "_blank", "noopener");
      return decide(id, "applied");
    case "decline": return askDecline(id);
    case "undo": return decide(id, "new");
    case "tailor": return showTailor(id);
  }
});

$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-tab]");
  if (!b) return;
  tab = b.dataset.tab;
  localStorage.setItem("tab", tab);
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
