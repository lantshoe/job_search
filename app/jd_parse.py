"""Job description parsing: HTML -> Markdown, and splitting requirements into must-have vs nice-to-have."""
import html
import re
from html.parser import HTMLParser


class _MD(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.out.append("\n- ")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("p", "div", "br", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "tr", "section"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("p", "div", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "tr", "section"):
            self.out.append("\n")

    def handle_data(self, d):
        self.out.append(re.sub(r"[ \t]+", " ", d))


def markdown(raw: str | None) -> str:
    """JD HTML -> Markdown, keeping bold headings and bullet lists."""
    if not raw:
        return ""
    # Greenhouse's boards API returns HTML-escaped markup (&lt;p&gt;); unescape it first.
    if "&lt;" in raw:
        raw = html.unescape(raw)
    p = _MD()
    p.feed(raw)
    md = "".join(p.out).replace("\xa0", " ")
    md = re.sub(r"\*\*\s*\*\*", "", md)
    md = re.sub(r"[ \t]*\n[ \t]*", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"(?m)^(- .*)\n\n(?=- )", r"\1\n", md)
    return md.strip()


_HEAD = re.compile(r"^\*\*(.+?)\*\*:?\s*$", re.M)

BONUS = ("stand out", "nice to have", "bonus", "preferred", "plus")
REQ = ("you bring", "requirement", "qualification", "must have",
       "you have", "about you", "what we look")


def sections(md: str) -> dict:
    """Split by bold subheadings and classify bullets as required / bonus / other.

    Missing a hard requirement and missing a nice-to-have are very different things,
    so the LLM gets them labeled separately.
    """
    marks = [(m.end(), m.group(1).strip().lower().replace("’", "'"))
             for m in _HEAD.finditer(md)]
    out = {"required": [], "bonus": [], "other": []}
    if not marks:
        out["other"] = bullets(md)
        return out

    starts = [m.start() for m in _HEAD.finditer(md)] + [len(md)]
    for i, (end, title) in enumerate(marks):
        items = bullets(md[end:starts[i + 1]])
        if not items:
            continue
        key = ("bonus" if any(h in title for h in BONUS)
               else "required" if any(h in title for h in REQ)
               else "other")
        out[key] += items
    return out


def bullets(text: str | None) -> list[str]:
    return [re.sub(r"^\s*-\s*", "", line).strip()
            for line in (text or "").splitlines() if line.lstrip().startswith("- ")]
