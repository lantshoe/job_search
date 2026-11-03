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
    return f'<div class="row"><div>{left}</div><div class="right">{_e(right)}</div></div>'


def to_html(r: dict, level: dict, page_size: str = "letter") -> str:
    L = level
    parts = [f'<h1>{_e(r["name"])}</h1>' if r["name"] else ""]
    if r["headline"]:
        parts.append(f'<div class="headline">{_e(r["headline"])}</div>')
    if r["contact"]:
        parts.append(f'<div class="contact">{" &nbsp;|&nbsp; ".join(_e(c) for c in r["contact"])}</div>')
    if r["summary"]:
        parts.append(f'<h2>Professional Summary</h2><p>{_e(r["summary"])}</p>')
    if r["skills"]:
        lines = "".join(
            "<p>" + (f"<b>{_e(s['label'])}:</b> " if s["label"] else "") + _e(s["items"]) + "</p>" for s in r["skills"]
        )
        parts.append(f"<h2>Technical Skills</h2>{lines}")

    def bullets(items):
        return f'<ul>{"".join(f"<li>{_e(b)}</li>" for b in items)}</ul>' if items else ""

    if r["experience"]:
        items = []
        for e in r["experience"]:
            left = f'<b>{_e(e["title"])}</b>' + (f' · {_e(e["company"])}' if e["company"] else "")
            left += f' <span class="muted">· {_e(e["location"])}</span>' if e["location"] else ""
            items.append(f'<div class="entry">{_row(left, e["dates"])}{bullets(e["bullets"])}</div>')
        parts.append("<h2>Experience</h2>" + "".join(items))
    if r["projects"]:
        items = []
        for p in r["projects"]:
            left = f'<b>{_e(p["name"])}</b>' + (f' <span class="muted">· {_e(p["detail"])}</span>' if p["detail"] else "")
            items.append(f'<div class="entry">{_row(left, p["dates"])}{bullets(p["bullets"])}</div>')
        parts.append("<h2>Projects</h2>" + "".join(items))
    if r["education"]:
        items = []
        for e in r["education"]:
            left = f'<b>{_e(e["school"])}</b>' + (f' · {_e(e["degree"])}' if e["degree"] else "")
            left += f' <span class="muted">· {_e(e["location"])}</span>' if e["location"] else ""
            detail = f'<p class="muted">{_e(e["detail"])}</p>' if e["detail"] else ""
            items.append(f'<div class="entry">{_row(left, e["dates"])}{detail}</div>')
        parts.append("<h2>Education</h2>" + "".join(items))
    for x in r["extra"]:
        parts.append(f'<h2>{_e(x["label"] or "Other")}</h2><p>{_e(x["items"])}</p>')

    w, h = PAGE_SIZES.get(page_size, PAGE_SIZES["letter"])
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
</style></head><body>{"".join(parts)}</body></html>"""


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
