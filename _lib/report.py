#!/usr/bin/env python3
"""report.py — shared HTML report emitter for CC skills.

Skills keep printing to the terminal as usual, and *additionally* call this to
drop a self-contained, dated HTML artifact under
``~/Github/CC/reports/<skill>/<date>.html`` — archived and served by cc-docs at
``:8090/reports/<skill>/``. Stdlib only, so every skill that already imports
from ``_lib`` can use it with no new deps.

Two ways to use it:

  Python skills (have structured data) — build a report:
      from report import Report                 # via the _lib sibling bootstrap
      r = (Report("weather", "Ranch forecast", subtitle="next 12h + 7-day")
           .badge("frost watch", "warn")
           .section("Now").md("**61°F**, wind NW 8 mph ..."))
      info = r.save()                            # -> {path, url, rel}
      print("archived:", info["url"])

  LLM / headless skills (emit markdown text) — pipe the answer in:
      some_skill | python3 ~/Github/CC/_lib/report.py \\
          --skill triage --title "Inbox triage" --badge "3 need reply:warn"
  (prints the cc-docs URL of the saved report)

Live embeds: a ````` ```html preview ````` fence (or ``r.embed(html, height=…)``) renders
as a sandboxed, theme-injected iframe instead of escaped text — so a skill can drop a live
Chart.js/Leaflet/`<canvas>` widget straight into its report. The frame is null-origin and a
CSP forbids ``eval``; network stays open so CDN libs and ``fetch`` work when viewed online.

Archive hygiene: ``save(keep=N)`` prunes to the last N reports per skill.
Indices (root + per-skill) are regenerated on every save as a self-healing
projection of whatever files are present on disk.
"""
import argparse
import datetime
import hashlib
import html
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))            # .../CC/_lib
REPORTS = os.path.join(os.path.dirname(HERE), "reports")     # .../CC/reports
LAN = "http://{{HOST_IP}}:8090/reports"                    # cc-docs base (Tailscale works too)

_STATUS = {                       # badge / status -> (bg, border, ink)
    "good": ("#10231a", "#22c55e", "#86efac"), "ok": ("#10231a", "#22c55e", "#86efac"),
    "warn": ("#2a1f0e", "#f59e0b", "#fcd34d"), "bad": ("#2a1414", "#ef4444", "#fca5a5"),
    "info": ("#0f1b2e", "#3B82F6", "#93c5fd"), "muted": ("#16181d", "#3a4150", "#9aa3b2"),
}

_CSS = """
:root{--bg:#0c0d10;--panel:#15171c;--panel2:#1a1d24;--line:#262a33;--ink:#e8eaed;
 --dim:#8b909b;--accent:#3B82F6;--good:#22c55e;--warn:#f59e0b;--bad:#ef4444;
 --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(900px 480px at 85% -10%,rgba(59,130,246,.10),transparent 60%),var(--bg);
 color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:920px;margin:0 auto;padding:34px 22px 64px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.crumb{font-family:var(--mono);font-size:12.5px;color:var(--dim);margin-bottom:18px}
.eyebrow{font-family:var(--mono);font-size:11.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--dim)}
h1{font-size:27px;margin:8px 0 4px;letter-spacing:-.01em}
.sub{color:var(--dim);font-size:14.5px;margin:0 0 14px}
.meta{font-family:var(--mono);font-size:12px;color:var(--dim);display:flex;flex-wrap:wrap;gap:6px 18px;margin:0 0 16px}
.badges{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 22px}
.badge{font-family:var(--mono);font-size:12px;font-weight:600;border-radius:999px;padding:4px 12px;border:1px solid}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);font-weight:600;
 margin:30px 0 12px;display:flex;align-items:center;gap:10px}
h2::after{content:"";flex:1;height:1px;background:var(--line)}
h3{font-size:16px;margin:18px 0 6px}
p{margin:0 0 12px}
ul,ol{margin:0 0 13px;padding-left:22px}li{margin:4px 0}
code{font-family:var(--mono);font-size:.88em;background:#0f1116;border:1px solid var(--line);border-radius:5px;padding:1px 6px;color:#c7d0e0}
pre{background:#0c0e13;border:1px solid var(--line);border-radius:10px;padding:14px 16px;overflow:auto}
pre code{background:none;border:none;padding:0}
table{border-collapse:collapse;width:100%;margin:0 0 16px;font-size:14px}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line)}
th{font-family:var(--mono);font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--dim);font-weight:600}
tr:hover td{background:var(--panel)}
.kv{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:1px;background:var(--line);
 border:1px solid var(--line);border-radius:10px;overflow:hidden;margin:0 0 18px}
.kv .cell{background:var(--panel);padding:12px 14px}
.kv .k{font-family:var(--mono);font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--dim)}
.kv .v{font-size:17px;font-weight:600;margin-top:3px}
.note{background:var(--panel2);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:8px;padding:12px 15px;margin:0 0 16px;font-size:14.5px}
.foot{color:#565b66;font-size:11.5px;margin-top:46px;border-top:1px solid var(--line);padding-top:16px;font-family:var(--mono)}
.rows a.row{display:flex;justify-content:space-between;gap:14px;align-items:center;text-decoration:none;color:var(--ink);
 background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px 16px;margin-bottom:9px;transition:.15s}
.rows a.row:hover{border-color:var(--accent);background:var(--panel2);transform:translateY(-1px)}
.rows .when{font-family:var(--mono);font-size:12.5px;color:var(--dim)}
.rows .cnt{font-family:var(--mono);font-size:12px;color:var(--dim)}
"""


# ---------- sandboxed live-HTML embed (the OpenKnowledge ```html preview idea) ----------
# A ```html preview fence (or Report.embed(...)) renders as a sandboxed iframe rather
# than escaped <pre>. The iframe is null-origin (sandbox without allow-same-origin → it
# can't touch the parent DOM, cookies, or localStorage), a CSP forbids eval, and CC's
# dark theme tokens are injected so charts/maps inherit the palette. Network is left open
# on purpose so CDN libs (Chart.js, Leaflet) and live fetch() work when viewed online.
_EMBED_THEME = (
    ":root{--chart-1:#3B82F6;--chart-2:#22c55e;--chart-3:#f59e0b;--chart-4:#a78bfa;"
    "--chart-5:#ec4899;--chart-6:#14b8a6;--foreground:#e8eaed;--background:#0c0e13;"
    "--muted:#8b909b;--line:#262a33;--accent:#3B82F6;--good:#22c55e;--warn:#f59e0b;--bad:#ef4444;}"
    "html,body{margin:0;background:transparent;color:var(--foreground);"
    'font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;line-height:1.5;}'
    "*{box-sizing:border-box}"
)
_EMBED_CSP = ("default-src 'self' data: blob: https:; "
              "script-src 'unsafe-inline' https: blob:; "      # inline ok, eval denied (no 'unsafe-eval')
              "style-src 'unsafe-inline' https:; "
              "img-src 'self' data: blob: https:; "
              "connect-src 'self' https: data: blob:; "
              "font-src https: data:;")
# Parent-side listener that grows each embed to its content height (idempotent — the
# window flag means duplicate copies across multiple embeds install only once).
_EMBED_LISTENER = (
    "<script>if(!window.__okEmbedInit){window.__okEmbedInit=1;"
    "window.addEventListener('message',function(e){var d=e.data;"
    "if(!d||!d.okEmbed||!d.h)return;"
    "var f=document.querySelector('iframe[data-okembed=\"'+d.okEmbed+'\"]');"
    "if(f)f.style.height=(d.h+2)+'px';});}</script>"
)


def _embed_iframe(code, height=None):
    """Wrap a standalone HTML/CSS/JS snippet as a sandboxed, theme-injected iframe."""
    eid = "okemb" + hashlib.md5((code or "").encode("utf-8")).hexdigest()[:10]
    # measure body (content-driven) not documentElement (which floors at the iframe
    # viewport height, so the frame could only ever grow, never shrink to fit).
    resize = ("<script>(function(){function r(){try{var b=document.body,"
              "h=Math.ceil((b&&b.scrollHeight)||document.documentElement.scrollHeight);"
              "parent.postMessage({okEmbed:%r,h:h},'*')}catch(e){}}"
              "addEventListener('load',r);addEventListener('resize',r);"
              "setTimeout(r,250);setTimeout(r,1200);})();</script>") % eid
    doc = ('<!DOCTYPE html><html><head><meta charset="utf-8">'
           '<meta http-equiv="Content-Security-Policy" content="%s">'
           "<style>%s</style></head><body>%s%s</body></html>"
           ) % (_EMBED_CSP, _EMBED_THEME, code or "", resize)
    srcdoc = doc.replace("&", "&amp;").replace('"', "&quot;")   # only what the attr needs
    h = int(height) if height else 380
    iframe = ('<iframe class="embed" data-okembed="%s" sandbox="allow-scripts allow-popups" '
              'loading="lazy" referrerpolicy="no-referrer" '
              'style="width:100%%;height:%dpx;border:1px solid var(--line);border-radius:10px;'
              'background:#0c0e13;margin:0 0 16px;display:block" srcdoc="%s"></iframe>'
              ) % (eid, h, srcdoc)
    return _EMBED_LISTENER + iframe


def _page(title, body, crumb=""):
    return ("<!DOCTYPE html>\n<html lang=\"en\"><head>\n"
            "<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<title>%s</title>\n<style>%s</style>\n</head>\n<body><div class=\"wrap\">\n%s%s\n"
            "</div></body></html>\n") % (html.escape(title), _CSS, crumb, body)


# ---------- minimal, stdlib markdown -> html (headings, lists, tables, code, inline) ----------
def _inline(s):
    s = html.escape(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\*\w])\*([^*\n]+)\*(?![\*\w])", r"<em>\1</em>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    return s


def md_to_html(text):
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out, i, n = [], 0, len(lines)
    para = []

    def flush():
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    while i < n:
        ln = lines[i]
        st = ln.strip()
        # fenced code
        if st.startswith("```"):
            flush()
            info = st[3:].strip().lower()                # info string after the backticks
            buf, i = [], i + 1
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i]); i += 1
            i += 1
            code = "\n".join(buf)
            # ```html preview  → live sandboxed embed (optional `height=NNN`)
            if info.startswith("html preview") or info in ("html-preview", "htmlpreview"):
                m = re.search(r"height\s*=\s*(\d+)", info)
                out.append(_embed_iframe(code, int(m.group(1)) if m else None))
            else:
                out.append("<pre><code>" + html.escape(code) + "</code></pre>")
            continue
        # blank
        if not st:
            flush(); i += 1; continue
        # heading
        m = re.match(r"(#{1,6})\s+(.*)", st)
        if m:
            flush()
            lvl = min(len(m.group(1)), 3) + 1   # # -> h2 (h1 is the page title)
            out.append("<h%d>%s</h%d>" % (lvl, _inline(m.group(2)), lvl))
            i += 1; continue
        # pipe table: header row + |---| separator
        if "|" in st and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1].strip()):
            flush()
            def cells(row):
                row = row.strip().strip("|")
                return [c.strip() for c in row.split("|")]
            head = cells(st); i += 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(cells(lines[i])); i += 1
            t = ["<table><thead><tr>"] + ["<th>%s</th>" % _inline(h) for h in head] + ["</tr></thead><tbody>"]
            for r in rows:
                t.append("<tr>" + "".join("<td>%s</td>" % _inline(c) for c in r) + "</tr>")
            t.append("</tbody></table>")
            out.append("".join(t)); continue
        # lists
        if re.match(r"[-*]\s+", st) or re.match(r"\d+[.)]\s+", st):
            flush()
            ordered = bool(re.match(r"\d+[.)]\s+", st))
            tag = "ol" if ordered else "ul"
            items = []
            while i < n and lines[i].strip() and (re.match(r"[-*]\s+", lines[i].strip()) or re.match(r"\d+[.)]\s+", lines[i].strip())):
                items.append(re.sub(r"^([-*]|\d+[.)])\s+", "", lines[i].strip())); i += 1
            out.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % _inline(x) for x in items), tag))
            continue
        para.append(st); i += 1
    flush()
    return "\n".join(out)


class Report:
    """Fluent builder for a single archived HTML report."""

    def __init__(self, skill, title, subtitle=None, meta=None):
        self.skill = re.sub(r"[^a-z0-9_-]", "-", str(skill).lower())
        self.title = title
        self.subtitle = subtitle
        self.meta = dict(meta or {})
        self._badges = []
        self._body = []

    def badge(self, label, status="info"):
        self._badges.append((label, status if status in _STATUS else "info"))
        return self

    def section(self, heading):
        self._body.append("<h2>%s</h2>" % html.escape(heading))
        return self

    def md(self, text):
        self._body.append(md_to_html(text))
        return self

    def p(self, text):
        self._body.append("<p>%s</p>" % _inline(text))
        return self

    def note(self, text):
        self._body.append('<div class="note">%s</div>' % _inline(text))
        return self

    def kv(self, pairs):
        cells = "".join('<div class="cell"><div class="k">%s</div><div class="v">%s</div></div>'
                        % (html.escape(str(k)), _inline(str(v))) for k, v in dict(pairs).items())
        self._body.append('<div class="kv">%s</div>' % cells)
        return self

    def table(self, headers, rows):
        h = "".join("<th>%s</th>" % html.escape(str(x)) for x in headers)
        b = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % _inline(str(c)) for c in r) for r in rows)
        self._body.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (h, b))
        return self

    def raw(self, html_str):
        self._body.append(html_str)
        return self

    def embed(self, html_str, height=None):
        """Embed a standalone HTML/JS snippet as a sandboxed, theme-injected iframe
        (charts, maps, live widgets). Same engine as a ```html preview markdown fence."""
        self._body.append(_embed_iframe(html_str, height))
        return self

    def _render(self, when):
        head = ['<div class="crumb"><a href="../">reports</a> / <a href="./">%s</a></div>' % html.escape(self.skill)]
        head.append('<div class="eyebrow">%s · CC report</div>' % html.escape(self.skill))
        head.append("<h1>%s</h1>" % html.escape(self.title))
        if self.subtitle:
            head.append('<p class="sub">%s</p>' % html.escape(self.subtitle))
        metabits = ['<span><b>generated</b> %s</span>' % when.strftime("%Y-%m-%d %H:%M %Z").strip()]
        for k, v in self.meta.items():
            metabits.append("<span><b>%s</b> %s</span>" % (html.escape(str(k)), html.escape(str(v))))
        head.append('<div class="meta">%s</div>' % "".join(metabits).replace("<b>", '<b style="color:#aab;font-weight:600">'))
        if self._badges:
            bs = []
            for label, st in self._badges:
                bg, bd, ink = _STATUS[st]
                bs.append('<span class="badge" style="background:%s;border-color:%s;color:%s">%s</span>'
                          % (bg, bd, ink, html.escape(label)))
            head.append('<div class="badges">%s</div>' % "".join(bs))
        foot = '<div class="foot">Generated by /%s · CC reports · %s · <a href="../">all reports</a></div>' % (
            html.escape(self.skill), when.strftime("%Y-%m-%d %H:%M"))
        return _page("%s · %s" % (self.skill, self.title),
                     "\n".join(head) + "\n" + "\n".join(self._body) + "\n" + foot)

    def save(self, slug=None, keep=30, when=None):
        when = when or datetime.datetime.now()
        d = os.path.join(REPORTS, self.skill)
        os.makedirs(d, exist_ok=True)
        name = when.strftime("%Y-%m-%d") + (("-" + re.sub(r"[^a-z0-9_-]", "-", slug.lower())) if slug else "") + ".html"
        path = os.path.join(d, name)
        with open(path, "w") as f:
            f.write(self._render(when))
        _prune(d, keep)
        _rebuild_skill_index(self.skill)
        _rebuild_root_index()
        rel = "reports/%s/%s" % (self.skill, name)
        return {"path": path, "rel": rel, "url": "%s/%s/%s" % (LAN, self.skill, name)}


def _reports_in(d):
    return sorted((f for f in os.listdir(d) if f.endswith(".html") and f != "index.html"), reverse=True)


def _prune(d, keep):
    files = _reports_in(d)
    for f in files[keep:]:
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass


def _rebuild_skill_index(skill):
    d = os.path.join(REPORTS, skill)
    files = _reports_in(d)
    rows = []
    for f in files:
        label = f[:-5]
        rows.append('<a class="row" href="%s"><span>%s</span><span class="when">open ↗</span></a>'
                    % (f, html.escape(label)))
    body = ('<div class="eyebrow">%s</div><h1>%s reports</h1>'
            '<p class="sub">%d archived · newest first</p><div class="rows">%s</div>'
            '<div class="foot"><a href="../">← all reports</a></div>'
            ) % (html.escape(skill), html.escape(skill), len(files), "".join(rows) or "<p>None yet.</p>")
    crumb = '<div class="crumb"><a href="../">reports</a> / %s</div>' % html.escape(skill)
    with open(os.path.join(d, "index.html"), "w") as f:
        f.write(_page("%s reports" % skill, body, crumb))


def _rebuild_root_index():
    os.makedirs(REPORTS, exist_ok=True)
    skills = sorted(s for s in os.listdir(REPORTS)
                    if os.path.isdir(os.path.join(REPORTS, s)))
    rows = []
    for s in skills:
        files = _reports_in(os.path.join(REPORTS, s))
        if not files:
            continue
        latest = files[0][:-5]
        rows.append('<a class="row" href="%s/"><span>/%s</span>'
                    '<span class="cnt">%d report%s · latest %s</span></a>'
                    % (s, html.escape(s), len(files), "" if len(files) == 1 else "s", html.escape(latest)))
    body = ('<div class="eyebrow">cc-docs</div><h1>Skill reports</h1>'
            '<p class="sub">Archived HTML output from CC skills. Terminal stays the source of truth; '
            'these are the consumable, durable copies.</p><div class="rows">%s</div>'
            '<div class="foot">Served by cc-docs · :8090/reports/ · regenerated on each skill run</div>'
            ) % ("".join(rows) or "<p>No reports yet.</p>")
    with open(os.path.join(REPORTS, "index.html"), "w") as f:
        f.write(_page("Skill reports · CC", body))


def _main(argv=None):
    ap = argparse.ArgumentParser(description="Save a markdown report (stdin) as archived HTML.")
    ap.add_argument("--skill", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--subtitle", default=None)
    ap.add_argument("--slug", default=None)
    ap.add_argument("--keep", type=int, default=30)
    ap.add_argument("--badge", action="append", default=[],
                    help='"label:status" (status: good|warn|bad|info|muted); repeatable')
    ap.add_argument("--file", default="-", help="markdown file, or - for stdin")
    a = ap.parse_args(argv)
    text = sys.stdin.read() if a.file == "-" else open(a.file).read()
    r = Report(a.skill, a.title, subtitle=a.subtitle)
    for b in a.badge:
        label, _, st = b.rpartition(":")
        r.badge(label or b, (st or "info").strip())
    r.md(text)
    info = r.save(slug=a.slug, keep=a.keep)
    print(info["url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
