"""Lay out a tailored resume as a one-page PDF (headless Chrome) and a Word file.

The LLM only writes the content as structured data; layout is a fixed template here.
fit() tries layouts from roomy to tight, counting the PDF's pages, and trims content
only when even the tightest readable layout overflows.
"""
import html
import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from pypdf import PdfReader


class RenderError(RuntimeError):
    pass


# Roomiest first. The last one is the smallest we accept as readable.
LEVELS = [
    {"font": 11.0, "line": 1.35, "margin": 0.6, "gap": 10, "head_gap": 18},
    {"font": 10.5, "line": 1.3, "margin": 0.55, "gap": 8, "head_gap": 15},
    {"font": 10.5, "line": 1.2, "margin": 0.5, "gap": 7, "head_gap": 13},
    {"font": 10.0, "line": 1.18, "margin": 0.45, "gap": 6, "head_gap": 11},
    {"font": 10.0, "line": 1.12, "margin": 0.4, "gap": 5, "head_gap": 9},
]
LAYOUT = 2  # bump when the template changes, so saved resumes get their page fit re-checked
PAGE_SIZES = {"letter": (8.5, 11.0), "a4": (8.27, 11.69)}
FONT = "Arial"  # same font in the PDF and the Word file, so both lay out alike


# ---------------------------------------------------------------- content

def _s(v) -> str:
    return " ".join(str(v).split()) if v not in (None, "null") else ""


def _strs(v) -> list[str]:
    if isinstance(v, str):
        v = [v]
    return [s for s in (_s(x) for x in v or []) if s]


def _dicts(v) -> list[dict]:
    return [x for x in v or [] if isinstance(x, dict)]


def normalize(raw: dict) -> dict:
    """Coerce the model's JSON into the shape the templates expect."""
    skills = []
    for s in raw.get("skills") or []:
        if isinstance(s, str):
            s = {"label": "", "items": s}
        if isinstance(s, dict):
            items = s.get("items")
            items = ", ".join(_strs(items)) if isinstance(items, list) else _s(items)
            if items:
                skills.append({"label": _s(s.get("label")), "items": items})
    return {
        "name": _s(raw.get("name")),
        "headline": _s(raw.get("headline")),
        "contact": _strs(raw.get("contact")),
        "summary": _s(raw.get("summary")),
        "skills": skills,
        "experience": [
            {"title": _s(e.get("title")), "company": _s(e.get("company")), "location": _s(e.get("location")),
             "dates": _s(e.get("dates")), "bullets": _strs(e.get("bullets"))}
            for e in _dicts(raw.get("experience")) if e.get("title") or e.get("company")
        ],
        "projects": [
            {"name": _s(p.get("name")), "detail": _s(p.get("detail")), "dates": _s(p.get("dates")),
             "bullets": _strs(p.get("bullets"))}
            for p in _dicts(raw.get("projects")) if p.get("name")
        ],
        "education": [
            {"school": _s(e.get("school")), "degree": _s(e.get("degree")), "location": _s(e.get("location")),
             "dates": _s(e.get("dates")), "detail": _s(e.get("detail"))}
            for e in _dicts(raw.get("education")) if e.get("school") or e.get("degree")
        ],
        "extra": [
            {"label": _s(x.get("label")), "items": _s(x.get("items"))}
            for x in _dicts(raw.get("extra")) if x.get("items")
        ],
        "notes": _strs(raw.get("notes")),
        "changes": _strs(raw.get("changes")),
    }


def to_markdown(r: dict) -> str:
    out = [f"# {r['name']}" if r["name"] else ""]
    if r["headline"]:
        out.append(r["headline"])
    if r["contact"]:
        out.append(" | ".join(r["contact"]))
    if r["summary"]:
        out += ["", "## Professional Summary", r["summary"]]
    if r["skills"]:
        out += ["", "## Technical Skills"] + [f"- **{s['label']}:** {s['items']}" if s["label"] else f"- {s['items']}"
                                    for s in r["skills"]]
    if r["experience"]:
        out += ["", "## Experience"]
        for e in r["experience"]:
            head = " — ".join(filter(None, [e["title"], e["company"]]))
            sub = " | ".join(filter(None, [e["location"], e["dates"]]))
            out += ["", f"**{head}**" + (f"  \n{sub}" if sub else "")] + [f"- {b}" for b in e["bullets"]]
    if r["projects"]:
        out += ["", "## Projects"]
        for p in r["projects"]:
            head = " — ".join(filter(None, [p["name"], p["detail"]]))
            out += ["", f"**{head}**" + (f"  \n{p['dates']}" if p["dates"] else "")] + [f"- {b}" for b in p["bullets"]]
    if r["education"]:
        out += ["", "## Education"]
        for e in r["education"]:
            line = ", ".join(filter(None, [e["degree"], e["school"]]))
            rest = " | ".join(filter(None, [e["location"], e["dates"], e["detail"]]))
            out.append(f"- **{line}**" + (f" — {rest}" if rest else ""))
    for x in r["extra"]:
        out += ["", f"## {x['label'] or 'Other'}", x["items"]]
    return "\n".join(out).strip() + "\n"


def drop_one(r: dict) -> Optional[str]:
    """Remove the single least costly bullet; returns its text, or None when nothing is left to cut.

    Prefers the entry with the most bullets, and among equals the older one (later in the list).
    """
    entries = [*r["experience"], *r["projects"]]
    candidates = [(len(e["bullets"]), i) for i, e in enumerate(entries) if len(e["bullets"]) > 1]
    if candidates:
        _, i = max(candidates)
        return entries[i]["bullets"].pop()
    if r["projects"]:
        p = r["projects"].pop()
        return f"Project: {p['name']}"
    return None


# ---------------------------------------------------------------- HTML / PDF

def _e(s: str) -> str:
    return html.escape(s or "")


def _row(left: str, right: str) -> str:
    return f'<div class="row"><div>{left}</div><div class="right">{right}</div></div>'


ENTRY_SECTIONS = {
    # section -> (heading, fields as (key, placeholder), what the add button adds)
    "experience": ("Experience", [("title", "Title"), ("company", "Company"), ("location", "Location")], "job"),
    "projects": ("Projects", [("name", "Project"), ("detail", "Tech / context")], "project"),
    "education": ("Education", [("school", "School"), ("degree", "Degree"), ("location", "Location")], "school"),
}


def to_html(r: dict, level: dict, page_size: str = "letter", editable: bool = False) -> str:
    """The resume page. editable=True makes every field clickable text on the same layout.

    The page's editing behaviour lives in static/app.js, which reads fields back through data-f.
    """
    L = level

    def field(key: str, value: str, placeholder: str = "", repeat: bool = False) -> str:
        if not editable:
            return _e(value)
        extra = " data-repeat" if repeat else ""
        return f'<span data-f="{key}" contenteditable="plaintext-only" data-ph="{_e(placeholder)}"{extra}>{_e(value)}</span>'

    def bullets(items: list[str]) -> str:
        items = items or ([""] if editable else [])
        if not items:
            return ""
        attr = " data-repeat" if editable else ""
        return "<ul>" + "".join(f'<li{attr}>{field("bullet", b, "Bullet")}</li>' for b in items) + "</ul>"

    def entry(sec: str, e: dict) -> str:
        _, fields, _ = ENTRY_SECTIONS[sec]
        bits = []
        for i, (key, ph) in enumerate(fields):
            if not (e.get(key) or editable):
                continue
            text = field(key, e.get(key, ""), ph)
            if i == 0:
                bits.append(f"<b>{text}</b>")
            elif i == len(fields) - 1:
                bits.append(f'<span class="muted">· {text}</span>')
            else:
                bits.append(f"· {text}")
        body = ""
        if sec == "education":
            if e.get("detail") or editable:
                body = f'<p class="muted">{field("detail", e.get("detail", ""), "Details (GPA, honors…)")}</p>'
        else:
            body = bullets(e.get("bullets", []))
        remove = '<button class="edit-ui rm" data-remove title="Remove">×</button>' if editable else ""
        return f'<div class="entry" data-entry>{remove}{_row(" ".join(bits), field("dates", e.get("dates", ""), "Dates"))}{body}</div>'

    parts = [f'<h1>{field("name", r["name"], "Your name")}</h1>' if r["name"] or editable else ""]
    if r["headline"] or editable:
        parts.append(f'<div class="headline">{field("headline", r["headline"], "Headline")}</div>')
    if editable:
        spans = [field("contact", c, "Contact", repeat=True) for c in r["contact"] or [""]]
        parts.append(f'<div class="contact">{"".join(spans)}</div>')
    elif r["contact"]:
        parts.append(f'<div class="contact">{" &nbsp;|&nbsp; ".join(_e(c) for c in r["contact"])}</div>')
    if r["summary"] or editable:
        parts.append(f'<h2>Professional Summary</h2><p>{field("summary", r["summary"], "Summary")}</p>')
    if r["skills"] or editable:
        lines = []
        for s in r["skills"] or [{"label": "", "items": ""}]:
            if editable:
                lines.append(f'<p class="skill-row" data-repeat><b>{field("label", s["label"], "Label")}:</b> {field("items", s["items"], "Skills")}</p>')
            else:
                lines.append("<p>" + (f"<b>{_e(s['label'])}:</b> " if s["label"] else "") + _e(s["items"]) + "</p>")
        parts.append("<h2>Technical Skills</h2>" + "".join(lines))
    for sec, (title, _, noun) in ENTRY_SECTIONS.items():
        if r[sec] or editable:
            add = f'<button class="edit-ui add" data-add="{sec}">+ Add {noun}</button>' if editable else ""
            parts.append(f'<div data-sec="{sec}"><h2>{title}</h2>{"".join(entry(sec, e) for e in r[sec])}{add}</div>')
    for x in r["extra"]:
        if editable:
            parts.append('<div data-sec="extra" data-entry><button class="edit-ui rm" data-remove title="Remove">×</button>'
                         f'<h2>{field("label", x["label"], "Section title")}</h2><p>{field("items", x["items"], "Items")}</p></div>')
        else:
            parts.append(f'<h2>{_e(x["label"] or "Other")}</h2><p>{_e(x["items"])}</p>')
    if editable:
        parts.append('<button class="edit-ui add add-section" data-add="extra">+ Add section</button>')

    w, h = PAGE_SIZES.get(page_size, PAGE_SIZES["letter"])
    edit_css = """
[contenteditable] { outline: none; border-radius: 2px; cursor: text; }
[contenteditable]:hover { background: #eef3fd; }
[contenteditable]:focus { background: #e3ecfc; box-shadow: 0 0 0 1px #5b8ff0; }
[contenteditable]:empty { display: inline-block; min-width: 3em; }
[contenteditable]:empty::before { content: attr(data-ph); color: #aaa; font-weight: normal; }
.contact [data-f="contact"] + [data-f="contact"]::before { content: "\\00a0|\\00a0"; color: #444; }
body { position: relative; }
.entry, [data-sec] { position: relative; }
.edit-ui { font: 12px Arial, sans-serif; border: 1px solid #c9cdd3; background: #fff; color: #333; border-radius: 4px; cursor: pointer; }
.edit-ui:hover { border-color: #5b8ff0; color: #2f6fde; }
.add { position: absolute; right: 0; top: -2px; padding: 1px 8px; background: #fff; }
.add-section { position: static; display: block; margin-top: 8px; }
.page-line { position: absolute; left: 0; right: 0; height: 0; border-top: 1.5px dashed #c0392b; pointer-events: none; }
.page-line span { position: absolute; right: 8px; top: -9px; padding: 0 6px; background: #fff; color: #c0392b; font: bold 11px Arial, sans-serif; }
.page-line.end { border-top-color: #8a9099; }
.page-line.end span { color: #6b7280; }
/* While counting pages, hide what will not print, so the count matches the PDF. */
.measuring .edit-ui, .measuring .page-line,
.measuring .headline:has([data-f]:empty), .measuring p:has(> [data-f]:empty), .measuring li:has(> [data-f]:empty),
.measuring h2:has(+ p > [data-f]:empty), .measuring .skill-row:has([data-f="items"]:empty),
.measuring [data-sec]:not([data-sec="extra"]):not(:has([data-entry])),
.measuring [data-sec="extra"]:has([data-f="items"]:empty),
.measuring [data-sec="experience"] .entry:has([data-f="title"]:empty):has([data-f="company"]:empty),
.measuring [data-sec="projects"] .entry:has([data-f="name"]:empty),
.measuring [data-sec="education"] .entry:has([data-f="school"]:empty):has([data-f="degree"]:empty) { display: none !important; }
.rm { position: absolute; left: -26px; top: 0; width: 20px; height: 20px; padding: 0; opacity: 0; color: #c0392b; }
.entry:hover > .rm, [data-sec="extra"]:hover > .rm, .rm:focus { opacity: 1; }
""" if editable else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ size: {w}in {h}in; margin: {L['margin']}in; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; }}
body {{ font-family: {FONT}, "Helvetica Neue", Helvetica, sans-serif; font-size: {L['font']}pt;
       line-height: {L['line']}; color: #111; -webkit-print-color-adjust: exact; }}
h1 {{ font-size: {L['font'] + 9}pt; line-height: 1.1; margin: 0; text-align: center; }}
.headline {{ font-size: {L['font'] + 1}pt; margin-top: 2pt; text-align: center; }}
.contact {{ color: #444; margin-top: 2pt; text-align: center; }}
h2 {{ font-size: {L['font'] + 0.5}pt; text-transform: uppercase; letter-spacing: 0.06em; margin: {L['head_gap']}pt 0 3pt;
      padding-bottom: 1.5pt; border-bottom: 0.75pt solid #999; }}
p {{ margin: 0 0 1.5pt; }}
.entry {{ margin-top: {L['gap'] * 0.45:.1f}pt; break-inside: avoid; }}
.row {{ display: flex; justify-content: space-between; gap: 12pt; }}
.right {{ white-space: nowrap; font-weight: bold; }}
.muted {{ color: #444; }}
ul {{ margin: 1pt 0 0; padding-left: 14pt; }}
li {{ margin: 0 0 1pt; }}
@media screen {{
  html {{ background: #fff; }}
  body {{ box-sizing: content-box; width: {w - 2 * L['margin']:.3f}in; margin: 0; padding: {L['margin']}in; }}
}}
{edit_css}</style></head><body>{"".join(parts)}</body></html>"""


def to_docs_html(r: dict) -> str:
    """Self-contained HTML for pasting into Google Docs, which keeps inline styles but no stylesheet."""
    base = f"font-family:{FONT};font-size:10.5pt;color:#111;margin:0"

    def center(text: str, size: float, bold: bool = False) -> str:
        return f'<p style="{base};font-size:{size}pt;text-align:center;{"font-weight:bold;" if bold else ""}">{_e(text)}</p>'

    def heading(text: str) -> str:
        return (f'<p style="{base};font-size:11pt;font-weight:bold;margin-top:14pt;margin-bottom:3pt;'
                f'border-bottom:1px solid #999">{_e(text.upper())}</p>')

    def para(inner: str, top: float = 0) -> str:
        return f'<p style="{base};margin-top:{top}pt">{inner}</p>'

    def bullets(items: list[str]) -> str:
        if not items:
            return ""
        return f'<ul style="margin:0">{"".join(f"""<li style="{base}">{_e(b)}</li>""" for b in items)}</ul>'

    def line(bold: str, rest: list[str], dates: str) -> str:
        text = " · ".join([f"<b>{_e(bold)}</b>", *[_e(x) for x in rest if x]])
        return para(text + (f" &nbsp;|&nbsp; <b>{_e(dates)}</b>" if dates else ""), 6)

    out = []
    if r["name"]:
        out.append(center(r["name"], 20, bold=True))
    if r["headline"]:
        out.append(center(r["headline"], 11.5))
    if r["contact"]:
        out.append(center("  |  ".join(r["contact"]), 10.5))
    if r["summary"]:
        out += [heading("Professional Summary"), para(_e(r["summary"]))]
    if r["skills"]:
        out.append(heading("Technical Skills"))
        out += [para((f"<b>{_e(s['label'])}:</b> " if s["label"] else "") + _e(s["items"])) for s in r["skills"]]
    if r["experience"]:
        out.append(heading("Experience"))
        for e in r["experience"]:
            out += [line(e["title"], [e["company"], e["location"]], e["dates"]), bullets(e["bullets"])]
    if r["projects"]:
        out.append(heading("Projects"))
        for x in r["projects"]:
            out += [line(x["name"], [x["detail"]], x["dates"]), bullets(x["bullets"])]
    if r["education"]:
        out.append(heading("Education"))
        for e in r["education"]:
            out.append(line(e["school"], [e["degree"], e["location"]], e["dates"]))
            if e["detail"]:
                out.append(para(_e(e["detail"])))
    for x in r["extra"]:
        out += [heading(x["label"] or "Other"), para(_e(x["items"]))]
    return "".join(out)


def find_chrome() -> Optional[str]:
    candidates = [
        os.getenv("CHROME_PATH", ""),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "msedge"):
        if found := shutil.which(name):
            return found
    return None


def render_pdf(r: dict, level: dict, page_size: str = "letter") -> bytes:
    chrome = find_chrome()
    if not chrome:
        raise RenderError("No Chrome/Chromium/Edge found for making PDFs. Install Chrome or set CHROME_PATH in .env.")
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "resume.html", Path(tmp) / "resume.pdf"
        src.write_text(to_html(r, level, page_size), encoding="utf-8")
        # A throwaway profile keeps this from touching (or waiting on) your open Chrome.
        cmd = [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
               f"--user-data-dir={tmp}/profile", "--no-pdf-header-footer", "--print-to-pdf-no-header",
               f"--print-to-pdf={out}", src.as_uri()]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            raise RenderError("Chrome took too long to make the PDF.")
        if not out.exists() or out.stat().st_size == 0:
            raise RenderError(f"Chrome did not produce a PDF: {(proc.stderr or '').strip()[-300:]}")
        return out.read_bytes()


def page_stats(pdf: bytes) -> tuple[int, float]:
    """(page count, share of the text that spilled past page 1)."""
    pages = PdfReader(io.BytesIO(pdf)).pages
    lengths = [len(p.extract_text() or "") for p in pages]
    total = sum(lengths) or 1
    return len(pages), (total - lengths[0]) / total


def fit(r: dict, page_size: str = "letter",
        shorten: Optional[Callable[[dict, float], dict]] = None, max_drops: int = 15) -> tuple[dict, dict]:
    """Find the roomiest layout that keeps the resume on one page; trim content only if none does.

    shorten(resume, overflow_share) asks the LLM for a shorter version; afterwards, single
    bullets are dropped mechanically as a last resort. Returns (resume, fit_info).
    """
    tight = len(LEVELS) - 1
    info = {"page_size": page_size, "shortened": False, "dropped": [], "layout": LAYOUT}

    pages, overflow = page_stats(render_pdf(r, LEVELS[tight], page_size))
    if pages > 1 and shorten:
        try:
            shorter = shorten(r, overflow)
        except Exception as e:  # the trim pass is a nicety; fall through to mechanical drops
            info["shorten_error"] = str(e)
        else:
            if shorter.get("experience") or shorter.get("education"):
                r, info["shortened"] = shorter, True
                pages, overflow = page_stats(render_pdf(r, LEVELS[tight], page_size))
    while pages > 1 and len(info["dropped"]) < max_drops:
        gone = drop_one(r)
        if gone is None:
            break
        info["dropped"].append(gone)
        pages, overflow = page_stats(render_pdf(r, LEVELS[tight], page_size))

    best = tight
    if pages == 1:
        lo, hi = 0, tight  # hi is known to fit
        while lo < hi:
            mid = (lo + hi) // 2
            if page_stats(render_pdf(r, LEVELS[mid], page_size))[0] == 1:
                hi = mid
            else:
                lo = mid + 1
        best = hi
    info.update(level=best, font=LEVELS[best]["font"], pages=pages)
    return r, info


# ---------------------------------------------------------------- Word

def to_docx(r: dict, level: dict, page_size: str = "letter") -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    L = level
    w, h = PAGE_SIZES.get(page_size, PAGE_SIZES["letter"])
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Inches(w), Inches(h)
    sec.left_margin = sec.right_margin = sec.top_margin = sec.bottom_margin = Inches(L["margin"])
    content_width = Inches(w - 2 * L["margin"])

    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = FONT, Pt(L["font"])
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    normal.paragraph_format.space_before = normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.line_spacing = L["line"]
    grey = RGBColor(0x44, 0x44, 0x44)

    def para(space_before=0.0, center=False):
        p = doc.add_paragraph()
        if center:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(space_before)
        return p

    def run(p, text, bold=False, size=None, color=None, italic=False):
        rn = p.add_run(text)
        rn.bold, rn.italic = bold, italic
        if size:
            rn.font.size = Pt(size)
        if color:
            rn.font.color.rgb = color
        return rn

    def heading(text):
        p = para(L["head_gap"])
        p.paragraph_format.space_after = Pt(3)
        p.paragraph_format.keep_with_next = True
        run(p, text.upper(), bold=True, size=L["font"] + 0.5)
        border = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        for k, v in {"w:val": "single", "w:sz": "6", "w:space": "1", "w:color": "999999"}.items():
            bottom.set(qn(k), v)
        border.append(bottom)
        p._p.get_or_add_pPr().append(border)

    def entry_line(bold_text, rest, muted_text, dates):
        p = para(L["gap"] * 0.45)
        p.paragraph_format.keep_with_next = True
        p.paragraph_format.tab_stops.add_tab_stop(content_width, WD_TAB_ALIGNMENT.RIGHT)
        run(p, bold_text, bold=True)
        if rest:
            run(p, f" · {rest}")
        if muted_text:
            run(p, f" · {muted_text}", color=grey)
        if dates:
            run(p, f"\t{dates}", bold=True)

    def bullets(items):
        for b in items:
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.space_after = Pt(1)
            p.paragraph_format.left_indent = Inches(0.2)
            p.paragraph_format.first_line_indent = Inches(-0.14)
            run(p, b)

    if r["name"]:
        run(para(center=True), r["name"], bold=True, size=L["font"] + 9)
    if r["headline"]:
        run(para(2, center=True), r["headline"], size=L["font"] + 1)
    if r["contact"]:
        run(para(2, center=True), "  |  ".join(r["contact"]), color=grey)
    if r["summary"]:
        heading("Professional Summary")
        run(para(), r["summary"])
    if r["skills"]:
        heading("Technical Skills")
        for s in r["skills"]:
            p = para()
            if s["label"]:
                run(p, f"{s['label']}: ", bold=True)
            run(p, s["items"])
    if r["experience"]:
        heading("Experience")
        for e in r["experience"]:
            entry_line(e["title"], e["company"], e["location"], e["dates"])
            bullets(e["bullets"])
    if r["projects"]:
        heading("Projects")
        for pj in r["projects"]:
            entry_line(pj["name"], "", pj["detail"], pj["dates"])
            bullets(pj["bullets"])
    if r["education"]:
        heading("Education")
        for e in r["education"]:
            entry_line(e["school"], e["degree"], e["location"], e["dates"])
            if e["detail"]:
                run(para(), e["detail"], color=grey)
    for x in r["extra"]:
        heading(x["label"] or "Other")
        run(para(), x["items"])

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
