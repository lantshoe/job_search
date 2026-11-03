const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let data = { jobs: [], threshold: 70, resume: null };
let reasons = {};
let tab = localStorage.getItem("tab") || "recommended";
let visible = [];          // jobs currently shown in the list, in order
let selectedId = null;
let detailKey = "";        // what the detail pane last rendered, to avoid needless re-renders
const detailCache = {};    // job id -> full job (with description)
const tailorCache = {};    // job id -> GET /tailor reply
const tailorBusy = {};     // job id -> what the LLM is doing right now
const chatDrafts = {};     // job id -> unsent chat text
const answerDrafts = {};   // job id -> [{choice, detail}] for the open questions
const openedPostings = new Set();  // undecided jobs whose posting you opened: ask "Did you apply?"
const resumeDrafts = {};   // job id -> structured resume being edited (absent = not editing)
let listHidden = false;    // hide the job list to give the description + resume more room
try { listHidden = localStorage.getItem("listHidden") === "1"; } catch {}

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
  const key = [j.id, j.score, j.status, j.decline_reason, j.tailored_count, j.id in resumeDrafts, tailorBusy[j.id] || "", tailorCache[j.id]?.messages?.length ?? "", openedPostings.has(j.id)].join("|");
  if (key === detailKey) return;
  saveDraftFromForm();  // keep unsaved resume edits across re-renders
  saveConvoInputs();
  const sameJob = detailKey.split("|")[0] === String(j.id);
  detailKey = key;

  const scrollTop = sameJob ? pane.scrollTop : 0;
  pane.innerHTML = detailHtml(j, detailCache[j.id]);
  pane.scrollTop = scrollTop;
  pane.dataset.job = j.id;  // whose unsaved inputs are on screen
  applyListHidden();
  if (resumeDrafts[j.id]) mountEditor(j.id);
  restoreAnswers(j.id);
  const msgs = pane.querySelector(".msgs");
  if (msgs) msgs.scrollTop = msgs.scrollHeight;
  pane.style.setProperty("--pane-h", `${pane.clientHeight}px`);

  if (!detailCache[j.id]) {
    try {
      detailCache[j.id] = await api(`/api/jobs/${j.id}`);
    } catch (e) {
      detailCache[j.id] = { description: `Could not load the description: ${e.message}` };
    }
    if (selectedId === j.id) { detailKey = ""; renderDetail(); }
  }
  if (!(j.id in tailorCache)) {
    try {
      tailorCache[j.id] = await api(`/api/jobs/${j.id}/tailor`);
    } catch {
      tailorCache[j.id] = { content: null, messages: [], pending: [] };
    }
    if (selectedId === j.id) renderDetail();
  }
}

function fitLine(fit) {
  if (!fit) return "";
  if (fit.error) return `<p class="fit warn">PDF unavailable: ${esc(fit.error)} The Word download still works.</p>`;
  if (fit.pages > 1) return fit.edited
    ? `<p class="fit warn">⚠ Your version runs to ${fit.pages} pages even at ${fit.font}pt. Shorten something and save again.</p>`
    : `<p class="fit warn">⚠ Still ${fit.pages} pages at the smallest readable size (${fit.font}pt). Edit it down before sending.</p>`;
  const size = fit.page_size === "a4" ? "A4" : "Letter";
  const how = fit.edited ? " · your edits" : fit.shortened ? " · the LLM shortened it to fit" : "";
  return `<p class="fit ok">✓ Fits on one ${size} page · ${fit.font}pt text${how}</p>`;
}

function messageHtml(m) {
  const d = m.data || {};
  const list = (title, items) => (items?.length ? `<h5>${title}</h5><ul>${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "");
  let extra = "";
  if (m.kind === "review") extra = list("What I plan to emphasize", d.plan);
  if (m.kind === "written") extra = list("What I changed for this job", d.changes)
    + list("Still missing or worth checking", d.notes) + list("Left out to fit one page", d.dropped);
  if (m.kind === "chat" && d.updated) extra += `<p class="msg-tag">✓ Resume updated</p>`;
  if (m.kind === "chat" && d.facts?.length) extra += `<p class="msg-tag">Remembered for future jobs: ${esc(d.facts.join(", "))}</p>`;
  return `<div class="msg ${m.role}"><div class="msg-who">${m.role === "user" ? "You" : "LLM"}</div>
    <div class="msg-body">${esc(m.content)}${extra}</div></div>`;
}

function questionsHtml(questions) {
  const cards = questions.map((q, i) => `<div class="question" data-i="${i}">
      <p><span class="sev ${q.importance === "must_have" ? "high" : "low"}">${q.importance === "must_have" ? "must-have" : "nice to have"}</span>${esc(q.question)}</p>
      <div class="answer-row">
        <label><input type="radio" name="q${i}" value="yes"> Yes, I have it</label>
        <label><input type="radio" name="q${i}" value="no"> No</label>
        <label><input type="radio" name="q${i}" value="skip" checked> Skip</label>
      </div>
      <input class="q-detail" placeholder="If yes: where and how you used it. Only what you write here is used." aria-label="Details for ${esc(q.topic)}">
    </div>`).join("");
  return `<div class="questions"><h4>Please confirm</h4>${cards}
    <p class="muted">Anything left on Skip won't be remembered. Other answers are, so you won't be asked again.</p>
    <div class="tool-row"><button class="btn small primary" data-act="submit-answers">Write resume</button></div></div>`;
}

function convoHtml(j, t) {
  const messages = t?.messages || [];
  const log = messages.length
    ? `<details class="convo" open><summary>Conversation with the LLM (${messages.length})</summary>
       <div class="msgs">${messages.map(messageHtml).join("")}</div></details>` : "";
  const box = tailorBusy[j.id] ? "" : `<div class="chat-box">
      <textarea id="chat-input" rows="2" placeholder="Ask or tell the LLM anything, e.g. “I also used MySQL at Acme, add it” or “focus more on backend”">${esc(chatDrafts[j.id] || "")}</textarea>
      <button class="btn small primary" data-act="chat-send">Send</button>
    </div>`;
  return { log, box };
}

function tailoredHtml(j, t) {
  const { log, box } = convoHtml(j, t);
  if (tailorBusy[j.id]) return `${log}<p class="empty">${esc(tailorBusy[j.id])}</p>`;
  if (t === undefined) return `<p class="empty">Loading…</p>`;
  if (t?.pending?.length) return `${log}${questionsHtml(t.pending)}${box}`;
  if (!t?.content) return `${log}<div class="tool-row"><button class="btn primary" data-act="tailor">Write a tailored resume</button></div>${box}`;
  if (!t.has_layout) return `<div class="tool-row">
      <button class="btn small" data-act="copy-resume">Copy</button>
      <a class="btn small" href="/api/jobs/${j.id}/tailor.md">Download .md</a>
      <button class="btn small primary" data-act="tailor">Regenerate as PDF / Word</button>
    </div>
    <p class="muted">This one was written before PDF/Word export existed. Regenerate it to get a one-page PDF you can edit.</p>
    <article class="md">${renderMarkdown(t.content)}</article>`;
  if (resumeDrafts[j.id]) return editorHtml();

  const fit = t.fit || {};
  const pdf = `/api/jobs/${j.id}/tailor.pdf?v=${t.id}`;
  const preview = fit.error
    ? `<article class="md">${renderMarkdown(t.content)}</article>`
    : `<iframe class="resume-preview" src="${pdf}#toolbar=0&navpanes=0&view=FitH" title="Resume preview"></iframe>`;
  return `<div class="tool-row">
      <button class="btn small primary" data-act="edit-resume">✎ Edit</button>
      ${fit.error ? "" : `<a class="btn small" href="${pdf}&download=1">Download PDF</a>`}
      <a class="btn small" href="/api/jobs/${j.id}/tailor.docx?v=${t.id}"
         title="Same layout and font size as the PDF. Word can wrap lines a little differently, so check it is still one page after editing.">Download Word</a>
      <button class="btn small" data-act="copy-docs" title="Copies the resume with its formatting, ready to paste into a Google Doc">Copy for Google Docs</button>
      <button class="btn small" data-act="tailor" title="Compare again with all your resumes and answers, then write a new version">Regenerate</button>
    </div>
    ${fitLine(t.fit)}${log}${box}${preview}`;
}

// ---- resume editor: click and type on the resume itself. The server's template provides the
// page (same layout as the PDF); this code handles typing and reads the fields back.

const ENTRY_KEYS = {
  experience: ["title", "company", "location", "dates"],
  projects: ["name", "detail", "dates"],
  education: ["school", "degree", "location", "dates", "detail"],
};

function blankEntry(sec) {
  if (sec === "extra") return { label: "", items: "" };
  const e = Object.fromEntries(ENTRY_KEYS[sec].map((k) => [k, ""]));
  if (sec !== "education") e.bullets = [];
  return e;
}

function editorHtml() {
  return `<div class="tool-row">
      <button class="btn small primary" data-act="save-resume">Save &amp; check fit</button>
      <button class="btn small" data-act="cancel-edit">Cancel</button>
    </div>
    <p class="muted edit-hint">Click any text and type. Enter adds a bullet; Backspace on an empty one removes it. Hover a job to remove it. Dashed lines mark where pages end.</p>
    <p class="fit" id="fit-meter"></p>
    <div class="paper-wrap" id="paper-wrap"><iframe id="resume-frame" class="paper" title="Editable resume" scrolling="no"></iframe></div>`;
}

const postJson = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

function frameDoc() {
  const doc = $("#resume-frame")?.contentDocument;
  return doc?.querySelector("[data-f]") ? doc : null;
}

function readResume(doc, base) {
  const text = (el, key) => el.querySelector(`[data-f="${key}"]`)?.textContent.trim() ?? "";
  const r = {
    ...base,
    name: text(doc, "name"),
    headline: text(doc, "headline"),
    summary: text(doc, "summary"),
    contact: [...doc.querySelectorAll('[data-f="contact"]')].map((c) => c.textContent.trim()).filter(Boolean),
    skills: [...doc.querySelectorAll(".skill-row")]
      .map((row) => ({ label: text(row, "label"), items: text(row, "items") })).filter((x) => x.items),
    extra: [...doc.querySelectorAll('[data-sec="extra"]')].map((x) => ({ label: text(x, "label"), items: text(x, "items") })),
  };
  for (const [sec, keys] of Object.entries(ENTRY_KEYS)) {
    r[sec] = [...doc.querySelectorAll(`[data-sec="${sec}"] [data-entry]`)].map((el) => {
      const e = Object.fromEntries(keys.map((k) => [k, text(el, k)]));
      if (sec !== "education") e.bullets = [...el.querySelectorAll('[data-f="bullet"]')].map((b) => b.textContent.trim()).filter(Boolean);
      return e;
    });
  }
  return r;
}

function saveDraftFromForm() {
  const id = $("#detail").dataset.job;
  const doc = frameDoc();
  if (doc && id in resumeDrafts) resumeDrafts[id] = readResume(doc, resumeDrafts[id]);
}

function redrawEditor() {
  // The draft was just changed in code; drop the old page so renderDetail doesn't read it back.
  $("#resume-frame")?.remove();
  detailKey = "";
  renderDetail();
}

function focusEnd(el) {
  el.focus();
  const sel = el.ownerDocument.getSelection();
  sel.selectAllChildren(el);
  sel.collapseToEnd();
}

const editablesIn = (row) => (row.matches("[contenteditable]") ? [row] : [...row.querySelectorAll("[contenteditable]")]);

function addRowAfter(row, text = "") {
  const copy = row.cloneNode(true);
  editablesIn(copy).forEach((el) => { el.textContent = ""; });
  row.after(copy);
  const first = editablesIn(copy)[0];
  if (text) first.textContent = text;
  return first;
}

function editorKeydown(e) {
  const el = e.target;
  if (!el.matches?.("[contenteditable]")) return;
  const row = el.closest("[data-repeat]");
  if (e.key === "Enter") {
    e.preventDefault();
    if (row) focusEnd(addRowAfter(row));
    scheduleFitCheck();
  } else if (e.key === "Backspace" && row && editablesIn(row).every((x) => !x.textContent)) {
    const prev = row.previousElementSibling;
    if (!prev?.matches("[data-repeat]") && !row.nextElementSibling?.matches("[data-repeat]")) return;
    e.preventDefault();
    const target = prev?.matches("[data-repeat]") ? editablesIn(prev).pop() : editablesIn(row.nextElementSibling)[0];
    row.remove();
    focusEnd(target);
    scheduleFitCheck();
  }
}

function editorPaste(e) {
  const el = e.target;
  if (!el.matches?.("[contenteditable]")) return;
  e.preventDefault();
  const lines = e.clipboardData.getData("text/plain").split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  const doc = el.ownerDocument;
  const row = el.closest("[data-repeat]");
  if (!row || lines.length < 2) {
    doc.execCommand("insertText", false, lines.join(" "));
    return;
  }
  // Several pasted lines become several bullets.
  doc.execCommand("insertText", false, lines[0]);
  let last = row;
  lines.slice(1).forEach((line) => {
    const added = addRowAfter(last, line);
    last = added.closest("[data-repeat]");
  });
  focusEnd(editablesIn(last)[0]);
  scheduleFitCheck();
}

function editorClick(e, id, doc) {
  const add = e.target.closest("[data-add]");
  const remove = e.target.closest("[data-remove]");
  if (add) {
    const draft = readResume(doc, resumeDrafts[id]);
    draft[add.dataset.add].push(blankEntry(add.dataset.add));
    resumeDrafts[id] = draft;
    redrawEditor();
  } else if (remove) {
    remove.closest("[data-entry]").remove();
    sizePaper();
    scheduleFitCheck();
  }
}

// Lays out page breaks the way printing does: each block goes on the next page if it would cross
// the bottom edge, and a job's block is kept whole. Blocks are measured with non-printing parts hidden.
function pageLayout(doc, contentPx) {
  const bodyStyle = doc.defaultView.getComputedStyle(doc.body);
  const start = doc.body.getBoundingClientRect().top + parseFloat(bodyStyle.paddingTop);
  const blocks = [...doc.querySelectorAll("h1, .headline, .contact, h2, p, .entry")]
    .filter((b) => !b.parentElement.closest(".entry") && b.offsetHeight > 0);
  const breaks = [];
  let offset = start;
  let bottom = start;
  blocks.forEach((b) => {
    const rect = b.getBoundingClientRect();
    if (rect.bottom - offset > contentPx && rect.top > offset) {
      breaks.push(b);
      offset = rect.top;
    }
    bottom = Math.max(bottom, rect.bottom);
  });
  return { pages: breaks.length + 1, breaks, room: contentPx - (bottom - offset), used: bottom - start };
}

let editorPage = null;   // sizes of the page shown in the editor
let paperMinHeight = 0;

function drawPageLines(doc) {
  if (!editorPage) return null;
  doc.querySelectorAll(".page-line").forEach((el) => el.remove());
  doc.body.classList.add("measuring");
  const layout = pageLayout(doc, editorPage.content_px);
  doc.body.classList.remove("measuring");
  const top = doc.body.getBoundingClientRect().top;
  const line = (y, label, cls = "") => {
    const el = doc.createElement("div");
    el.className = `page-line edit-ui ${cls}`;
    el.style.top = `${y}px`;
    const tag = doc.createElement("span");
    tag.textContent = label;
    el.appendChild(tag);
    doc.body.appendChild(el);
  };
  layout.breaks.forEach((b, i) => line(b.getBoundingClientRect().top - top - 3, `Page ${i + 2} starts here`));
  // The end of the last page, so you can see how much room is left.
  const blocks = [...doc.body.children].filter((c) => !c.matches(".page-line, .add-section"));
  const lastBottom = Math.max(...blocks.map((c) => c.getBoundingClientRect().bottom)) - top;
  const endY = lastBottom + Math.max(0, layout.room);
  line(endY, `End of page ${layout.pages}`, "end");
  paperMinHeight = endY + editorPage.margin_px;
  return layout;
}

let paperWidth = 816;
function sizePaper() {
  const frame = $("#resume-frame");
  const wrap = $("#paper-wrap");
  const doc = frame?.contentDocument;
  if (!doc?.body || !wrap) return;
  const scale = Math.min(1, wrap.clientWidth / paperWidth);
  frame.style.width = `${paperWidth}px`;
  const height = Math.max(doc.body.offsetHeight, paperMinHeight);
  frame.style.height = `${height}px`;
  frame.style.transform = `scale(${scale})`;
  wrap.style.height = `${height * scale}px`;
}

async function mountEditor(id) {
  const frame = $("#resume-frame");
  if (!frame || frame.srcdoc) return;
  const level = tailorCache[id].fit?.level ?? 2;
  const page = await postJson("/api/resume/html", { resume: resumeDrafts[id], level, editable: true });
  if ($("#resume-frame") !== frame) return;
  paperWidth = page.page_px;
  editorPage = page;
  frame.onload = () => {
    const doc = frame.contentDocument;
    doc.addEventListener("keydown", editorKeydown);
    doc.addEventListener("paste", editorPaste);
    doc.addEventListener("click", (e) => editorClick(e, id, doc));
    doc.addEventListener("input", (e) => {
      if (e.target.matches?.("[contenteditable]") && !e.target.textContent) e.target.innerHTML = "";  // keeps the placeholder working
      sizePaper();
      scheduleFitCheck();
    });
    sizePaper();
    new ResizeObserver(sizePaper).observe($("#paper-wrap"));
    scheduleFitCheck(0);
  };
  paperMinHeight = 0;
  frame.srcdoc = page.html;
}

// A quick estimate while typing, from laying the page out in a hidden frame. Saving does the real check.
async function measure(resume, level) {
  const page = await postJson("/api/resume/html", { resume, level });
  let frame = $("#measure-frame");
  if (!frame) {
    frame = document.createElement("iframe");
    frame.id = "measure-frame";
    frame.setAttribute("aria-hidden", "true");
    frame.tabIndex = -1;
    frame.style.cssText = `position:absolute;left:-9999px;top:0;width:${page.page_px}px;height:800px;visibility:hidden`;
    document.body.appendChild(frame);
  }
  await new Promise((resolve) => { frame.onload = resolve; frame.srcdoc = page.html; });
  const layout = pageLayout(frame.contentDocument, page.content_px);
  return { ...page, pages: layout.pages, overBy: layout.used - page.content_px };
}

let fitTimer = null;
let fitRun = 0;
function scheduleFitCheck(delay = 300) {
  clearTimeout(fitTimer);
  fitTimer = setTimeout(checkFit, delay);
}

async function checkFit() {
  const doc = frameDoc();
  const id = $("#detail").dataset.job;
  if (!doc || !(id in resumeDrafts)) return;
  const run = ++fitRun;
  const shown = drawPageLines(doc);
  sizePaper();
  const resume = readResume(doc, resumeDrafts[id]);
  const current = await measure(resume, tailorCache[id].fit?.level ?? 2);
  const count = (n) => `${n} page${n === 1 ? "" : "s"}`;
  let text = `✓ ${count(current.pages)} at ${current.font}pt`;
  if (current.pages === 1 && shown) text += `, about ${Math.max(0, Math.floor(shown.room / current.line_px))} line(s) of room left`;
  let warn = false;
  if (current.pages > 1) {
    const tight = await measure(resume, null);
    if (tight.pages > 1) {
      text = `⚠ ${count(tight.pages)} even at ${tight.font}pt, the smallest size. Cut about ${Math.ceil(tight.overBy / tight.line_px)} line(s) to fit one page.`;
      warn = true;
    } else {
      text = `${count(current.pages)} at ${current.font}pt. Saving will shrink the text to fit one page (${tight.font}pt at the smallest).`;
    }
  }
  const meter = $("#fit-meter");
  if (run === fitRun && meter) {
    meter.textContent = text;
    meter.className = `fit ${warn ? "warn" : "ok"}`;
  }
}

function copyForDocs(id) {
  const t = tailorCache[id];
  const plain = (t.content || "").replace(/^#+\s*/gm, "").replace(/\*\*/g, "");
  const html = fetch(`/api/jobs/${id}/tailor.html`).then((r) => {
    if (!r.ok) throw new Error(r.statusText);
    return r.text();
  }).then((x) => new Blob([x], { type: "text/html" }));
  return navigator.clipboard.write([new ClipboardItem({
    "text/html": html,
    "text/plain": new Blob([plain], { type: "text/plain" }),
  })]).then(
    () => setStatus("Copied with formatting. Paste it into a Google Doc."),
    (e) => setStatus(`Could not copy: ${e.message}`, true),
  );
}

function applyListHidden() {
  // Editing needs the width, so the list steps aside until you save or cancel.
  $("#main").classList.toggle("list-hidden", listHidden || selectedId in resumeDrafts);
}

async function saveResume(id) {
  saveDraftFromForm();
  setStatus("Saving and checking the page fit…");
  try {
    tailorCache[id] = await api(`/api/jobs/${id}/tailor`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ resume: resumeDrafts[id] }),
    });
    delete resumeDrafts[id];
    pollStatus();  // puts the usual status line back
  } catch (e) {
    setStatus(`Could not save the resume: ${e.message}`, true);
    return;
  }
  detailKey = "";
  await load();
  renderDetail();
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

  const jd = full
    ? `<div class="legend">Highlighted: <mark class="warn">visa / sponsorship</mark><mark class="years">years of experience</mark><mark class="pay">pay</mark></div>
       <article class="md">${renderMarkdown(full.description || "(no description)", { highlight: true, tagSections: true })}</article>`
    : `<p class="empty">Loading description…</p>`;
  // With a tailored resume, show it beside the description so the two can be compared.
  const split = Boolean(j.tailored_count || tailorBusy[j.id] || tailorCache[j.id]?.messages?.length);
  if (!split) actions += `<button class="btn" data-act="tailor" title="Compare this job with all your resumes, ask about anything missing, then write a one-page resume">✎ Write tailored resume</button>`;
  if (!resumeDrafts[j.id]) actions += `<button class="btn ghost list-toggle" data-act="toggle-list">${listHidden ? "⇥ Show job list" : "⇤ Hide job list"}</button>`;
  const body = split
    ? `<div class="split ${resumeDrafts[j.id] ? "editing" : ""}">
        <section class="split-col resume-col"><h3 class="col-title">Tailored resume</h3>${tailoredHtml(j, tailorCache[j.id])}</section>
        <section class="split-col"><h3 class="col-title">Job description</h3>${jd}</section>
      </div>`
    : `<h3 class="col-title">Job description</h3>${jd}`;

  return `<div class="detail ${split ? "wide" : ""}">
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

async function tailorStep(id, busyText, path, body) {
  tailorBusy[id] = busyText;
  renderDetail();
  try {
    tailorCache[id] = await api(path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
    });
    return true;
  } catch (e) {
    setStatus(`The LLM step failed: ${e.message}`, true);
    // A failed step may still have recorded your answers; show what is stored.
    try { tailorCache[id] = await api(`/api/jobs/${id}/tailor`); } catch {}
    return false;
  } finally {
    delete tailorBusy[id];
    await load();
    detailKey = "";
    renderDetail();
  }
}

function tailor(id) {
  return tailorStep(id, "Comparing this job with all your resumes… this can take a minute or two.", `/api/jobs/${id}/tailor/start`);
}

function submitAnswers(id) {
  saveConvoInputs();
  const answers = tailorCache[id].pending.map((q, i) => {
    const a = (answerDrafts[id] || [])[i] || {};
    return { topic: q.topic, kind: q.kind, has: a.choice === "yes" ? true : a.choice === "no" ? false : null, detail: a.detail || "" };
  });
  delete answerDrafts[id];
  return tailorStep(id, "Writing your resume and fitting it to one page… this can take a minute or two.", `/api/jobs/${id}/tailor`, { answers });
}

async function sendChat(id) {
  const input = $("#chat-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  delete chatDrafts[id];
  const ok = await tailorStep(id, "The LLM is replying…", `/api/jobs/${id}/tailor/chat`, { message });
  if (!ok && selectedId === id && $("#chat-input")) $("#chat-input").value = message;  // nothing you typed is lost
}

// Typed answers and chat text survive the detail pane being redrawn.
function saveConvoInputs() {
  const id = $("#detail").dataset.job;
  if (!id) return;
  const chat = $("#chat-input");
  if (chat) chatDrafts[id] = chat.value;
  const cards = [...document.querySelectorAll("#detail .question")];
  if (cards.length) {
    answerDrafts[id] = cards.map((c) => ({
      choice: c.querySelector("input[type=radio]:checked")?.value,
      detail: c.querySelector(".q-detail").value,
    }));
  }
}

function restoreAnswers(id) {
  document.querySelectorAll("#detail .question").forEach((c, i) => {
    const a = (answerDrafts[id] || [])[i];
    if (!a) return;
    const radio = c.querySelector(`input[value="${a.choice}"]`);
    if (radio) radio.checked = true;
    c.querySelector(".q-detail").value = a.detail;
  });
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
    case "copy-docs": return copyForDocs(id);
    case "copy-resume": return navigator.clipboard.writeText(tailorCache[id]?.content || "");
    case "edit-resume":
      resumeDrafts[id] = structuredClone(tailorCache[id].resume);
      return renderDetail();
    case "cancel-edit":
      delete resumeDrafts[id];
      return renderDetail();
    case "save-resume": return saveResume(id);
    case "submit-answers": return submitAnswers(id);
    case "chat-send": return sendChat(id);
    case "add-entry":
      saveDraftFromForm();
      resumeDrafts[id][btn.dataset.sec].push(blankEntry(btn.dataset.sec));
      return redrawEditor();
    case "remove-entry":
      saveDraftFromForm();
      resumeDrafts[id][btn.dataset.sec].splice(Number(btn.dataset.i), 1);
      return redrawEditor();
    case "toggle-list":
      listHidden = !listHidden;
      try { localStorage.setItem("listHidden", listHidden ? "1" : ""); } catch {}
      applyListHidden();
      detailKey = "";
      return renderDetail();
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
    setStatus(`Resume saved (${r.chars} characters). Existing scores are kept; click "Re-rank" to score jobs against all your resumes.`);
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

// ---- resume versions and confirmed facts
async function loadResumesDialog() {
  const r = await api("/api/resumes");
  $("#resumes-size").textContent = r.total_chars > r.max_chars
    ? `⚠ ${r.total_chars} characters in use, over the ${r.max_chars} limit, so the oldest versions get cut. Untick the ones you don't need.`
    : `${r.total_chars} of ${r.max_chars} characters in use.`;
  $("#resumes-list").innerHTML = r.resumes.length ? r.resumes.map((x) => `<div class="manage-row">
      <label><input type="checkbox" data-include="${x.id}" ${x.included ? "checked" : ""}> ${esc(x.filename)}</label>
      <span class="muted">${esc(fullTime(x.created_at))} · ${x.chars} characters</span>
      <button type="button" class="btn small" data-view="${x.id}">View</button>
    </div>`).join("") : `<p class="muted">No resumes yet.</p>`;
  $("#facts-list").innerHTML = r.facts.length ? r.facts.map((f) => `<div class="manage-row">
      <span><span class="chip ${f.has ? "ok" : ""}">${f.has ? "✓ Have" : "✗ Don't have"}</span> <strong>${esc(f.topic)}</strong>${f.detail ? ` — ${esc(f.detail)}` : ""}</span>
      <button type="button" class="btn small danger" data-fact-del="${f.id}">Delete</button>
    </div>`).join("") : `<p class="muted">Nothing yet.</p>`;
}

$("#resumes-btn").onclick = async () => {
  $("#resume-view").hidden = true;
  await loadResumesDialog();
  $("#resumes-dialog").showModal();
};
$("#resumes-dialog").addEventListener("change", async (e) => {
  const id = e.target.dataset.include;
  if (!id) return;
  await api(`/api/resumes/${id}/included`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ included: e.target.checked }),
  });
  await loadResumesDialog();
  await load();
});
$("#resumes-dialog").addEventListener("click", async (e) => {
  const { view, factDel } = e.target.dataset;
  if (view) {
    $("#resume-view").textContent = (await api(`/api/resumes/${view}`)).content;
    $("#resume-view").hidden = false;
  }
  if (factDel) {
    await api(`/api/facts/${factDel}`, { method: "DELETE" });
    await loadResumesDialog();
  }
});
$("#fact-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  await api("/api/facts", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ topic: $("#fact-topic").value, detail: $("#fact-detail").value }),
  });
  e.target.reset();
  await loadResumesDialog();
});
$("#detail").addEventListener("keydown", (e) => {
  if (e.target.id === "chat-input" && e.key === "Enter" && (e.metaKey || e.ctrlKey) && selectedId != null) sendChat(selectedId);
});

applyListHidden();
// The sticky resume column is sized to the detail pane's height.
window.addEventListener("resize", () => $("#detail").style.setProperty("--pane-h", `${$("#detail").clientHeight}px`));
(async () => {
  reasons = await api("/api/reasons");
  loadLLM();
  await load();
  pollStatus();
})();
