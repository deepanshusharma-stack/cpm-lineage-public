#!/usr/bin/env python3
"""Build the designed how-it-works guide from its Markdown source.

    docs/how_it_works.md  +  docs/guide/fig-*.svg
        ->  docs/guide/how-it-works.html            (self-contained: inline CSS and SVG)
        ->  docs/guide/cpm-lineage-how-it-works.pdf (A4, backgrounds printed)

The Markdown file is the single source of the text; the HTML and the PDF are
generated and committed.  This is a documentation tool with its own needs,
none of which the library depends on:

    pip install markdown playwright
    python docs/guide/build_guide.py            # HTML only
    python docs/guide/build_guide.py --pdf      # HTML, then PDF through Playwright

The PDF step drives a Chromium-family browser.  It uses the system Chrome
when one is installed (``channel="chrome"``) and Playwright's own Chromium
otherwise (``playwright install chromium``).
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "how_it_works.md"
HTML_OUT = HERE / "how-it-works.html"
PDF_OUT = HERE / "cpm-lineage-how-it-works.pdf"

VERSION = "0.1.0"
AUTHOR = "Deepanshu Sharma"

# Step number -> accent class (matches Figure 1: diagnose, plan, ledger).
STEP_TONE = {1: "dg", 2: "dg", 3: "dg", 4: "pl", 5: "pl", 6: "pl", 7: "ld"}
# Sections that open on a fresh page in print.
PAGE_BREAK_BEFORE = {2, 3, 5, 7, 10, 11}

CSS = r"""
:root{
  --ink:#17202b; --mut:#5b6775; --line:#d5dbe2; --soft:#f4f6f8; --paper:#ffffff;
  --dg:#0f766e; --dg-soft:#e6f3f1; --pl:#3b4cc0; --pl-soft:#e8ebfb; --ld:#b45309; --ld-soft:#fdf1d8;
  --sans:Inter,system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;
  --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:#e9edf1;color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.55;
  font-feature-settings:"cv05","cv08","ss03"}
.sheet{max-width:880px;margin:0 auto;background:var(--paper);padding:0 56px 72px}
a{color:var(--pl);text-decoration:none}
a:hover{text-decoration:underline}

/* cover */
.cover{min-height:92vh;display:flex;flex-direction:column;padding:72px 0 40px;border-bottom:1px solid var(--line)}
.cover .kicker{font-size:12px;font-weight:700;letter-spacing:.14em;color:var(--dg);text-transform:uppercase}
.cover h1{font-size:54px;line-height:1.04;letter-spacing:-.025em;margin:18px 0 14px;font-weight:800;text-wrap:balance}
.cover .sub{font-size:19px;line-height:1.45;color:var(--mut);max-width:34em;margin:0}
.cover .meta{margin-top:26px;font-size:13.5px;color:var(--mut)}
.cover .meta b{color:var(--ink);font-weight:600}
.cover .steps{display:grid;grid-template-columns:repeat(7,1fr);gap:8px;margin:44px 0 0;padding:0;list-style:none}
.cover .steps li{border-top:3px solid var(--line);padding-top:10px;font-size:11.5px;line-height:1.35;color:var(--mut)}
.cover .steps li b{display:block;font-size:22px;line-height:1;color:var(--ink);margin-bottom:6px;font-weight:800}
.cover .steps li.dg{border-color:var(--dg)} .cover .steps li.pl{border-color:var(--pl)} .cover .steps li.ld{border-color:var(--ld)}
.cover .note{margin-top:auto;padding:16px 18px;background:var(--soft);border-left:3px solid var(--ld);font-size:13px;
  line-height:1.5;color:var(--ink);max-width:46em}
.cover .note b{font-weight:700}

/* contents */
.toc{padding:44px 0 8px}
.toc h2{font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:var(--mut);margin:0 0 14px;font-weight:700;border:0;padding:0}
.toc ol{list-style:none;margin:0;padding:0;columns:2;column-gap:40px}
.toc li{break-inside:avoid;padding:7px 0;border-bottom:1px solid var(--line);font-size:14px;display:flex;gap:12px}
.toc li span{color:var(--mut);font-variant-numeric:tabular-nums;min-width:1.6em}
.toc li a{color:var(--ink)}

/* body */
main h2{font-size:26px;line-height:1.2;letter-spacing:-.015em;margin:56px 0 14px;padding-top:18px;border-top:1px solid var(--line);
  font-weight:800;text-wrap:balance}
main h2 .num{color:var(--mut);font-weight:600;margin-right:.35em;font-variant-numeric:tabular-nums}
main h2.step{display:flex;align-items:center;gap:14px;border-top:0;padding-top:0}
main h2.step .badge{flex:none;display:inline-flex;flex-direction:column;align-items:center;justify-content:center;width:58px;height:58px;
  border-radius:14px;color:#fff;font-size:10px;letter-spacing:.12em;text-transform:uppercase;font-weight:700;line-height:1}
main h2.step .badge b{font-size:24px;letter-spacing:0;margin-top:4px;font-weight:800}
main h2.step.dg .badge{background:var(--dg)} main h2.step.pl .badge{background:var(--pl)} main h2.step.ld .badge{background:var(--ld)}
main h3{font-size:17.5px;line-height:1.3;margin:34px 0 8px;font-weight:700;letter-spacing:-.005em}
main p{margin:0 0 12px}
main ul,main ol{margin:0 0 12px;padding-left:22px}
main li{margin:3px 0}
main p.q{font-size:17px;line-height:1.45;font-weight:600;color:var(--ink);margin:4px 0 16px;padding-left:14px;border-left:3px solid var(--line)}
main p.q strong{display:block;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--mut);font-weight:700;margin-bottom:3px}
section.dg p.q{border-color:var(--dg)} section.pl p.q{border-color:var(--pl)} section.ld p.q{border-color:var(--ld)}
main p.lead strong:first-child{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--mut);font-weight:700;margin-right:.3em}

code{font-family:var(--mono);font-size:.86em;background:var(--soft);padding:.08em .32em;border-radius:4px;font-variant-ligatures:none}
pre{font-family:var(--mono);font-size:11.6px;line-height:1.5;background:var(--soft);border:1px solid var(--line);border-left:3px solid var(--mut);
  border-radius:6px;padding:12px 14px;margin:0 0 14px;overflow-x:auto;white-space:pre;font-variant-ligatures:none}
pre code{background:none;padding:0;font-size:inherit;border-radius:0}
pre.out{border-left-color:var(--ink);background:#f7f8fa}
section.dg pre.out{border-left-color:var(--dg)} section.pl pre.out{border-left-color:var(--pl)} section.ld pre.out{border-left-color:var(--ld)}
pre.formula{background:#fff;border-left-color:var(--line)}

.tbl{margin:0 0 16px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px;line-height:1.4}
th{text-align:left;font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--mut);font-weight:700;
  padding:7px 10px 7px 0;border-bottom:1.5px solid var(--ink);vertical-align:bottom}
td{padding:7px 10px 7px 0;border-bottom:1px solid var(--line);vertical-align:top}
td:last-child,th:last-child{padding-right:0}
td code{white-space:nowrap}
td:first-child code{font-weight:600}

figure{margin:20px 0 22px;padding:0}
figure svg{display:block;width:100%;height:auto}
figcaption{font-size:12.5px;color:var(--mut);margin-top:8px;line-height:1.4}
figcaption b{color:var(--ink)}

blockquote{margin:16px 0 18px;padding:12px 16px;background:var(--ld-soft);border-left:3px solid var(--ld);border-radius:0 6px 6px 0}
blockquote p{margin:0;font-size:13.5px;line-height:1.5}
blockquote strong:first-child{color:#8a4107}

ul.check{list-style:none;padding-left:0}
ul.check li{position:relative;padding-left:26px;margin:6px 0}
ul.check li::before{content:"";position:absolute;left:0;top:.28em;width:13px;height:13px;border:1.5px solid var(--ink);border-radius:3px}

.faq p.fq{font-weight:700;margin:20px 0 4px;font-size:15.5px}
footer.end{margin-top:56px;padding-top:16px;border-top:1px solid var(--line);font-size:12px;color:var(--mut)}

@media (max-width:720px){
  .sheet{padding:0 20px 48px}
  .cover h1{font-size:38px}
  .cover .steps{grid-template-columns:repeat(4,1fr)}
  .toc ol{columns:1}
  main h2{font-size:22px}
}

@page{size:A4;margin:17mm 15mm 18mm 15mm}
@media print{
  html,body{background:#fff}
  body{font-size:9.6pt;line-height:1.48}
  .sheet{max-width:none;padding:0;margin:0}
  a{color:inherit}
  .cover{min-height:0;height:258mm;padding:26mm 0 0;border:0;break-after:page}
  .cover h1{font-size:44pt}
  .cover .sub{font-size:14pt}
  .cover .meta{font-size:10pt}
  .cover .steps li{font-size:8.2pt}
  .cover .note{font-size:9.4pt}
  .toc{padding:0;break-after:page}
  .toc li{font-size:10.4pt;padding:6pt 0}
  main h2{font-size:18pt;margin:22pt 0 9pt;padding-top:11pt;break-after:avoid}
  main h2.brk{break-before:page;margin-top:0;border-top:0;padding-top:0}
  main h2.step .badge{width:40pt;height:40pt;border-radius:9pt;font-size:6.6pt}
  main h2.step .badge b{font-size:16pt;margin-top:2pt}
  main h3{font-size:12pt;margin:17pt 0 5pt;break-after:avoid}
  main p{margin:0 0 7pt;orphans:3;widows:3}
  main p.q{font-size:11.4pt;margin:2pt 0 10pt}
  main p.q strong{font-size:7.4pt}
  main p.lead strong:first-child{font-size:7.4pt}
  main p.lead{break-after:avoid}
  pre{font-size:7.35pt;line-height:1.45;padding:7pt 9pt;margin:0 0 9pt;overflow:visible;break-inside:avoid}
  table{font-size:8.5pt}
  th{font-size:6.9pt;padding:4pt 7pt 4pt 0}
  td{padding:4pt 7pt 4pt 0}
  tr{break-inside:avoid}
  thead{display:table-header-group}
  .tbl{overflow:visible;margin-bottom:10pt}
  figure{break-inside:avoid;margin:10pt 0 12pt}
  figcaption{font-size:8.2pt;margin-top:5pt}
  blockquote{break-inside:avoid;padding:8pt 11pt;margin:9pt 0 11pt}
  blockquote p{font-size:9pt}
  ul.check li{break-inside:avoid}
  .faq p.fq{font-size:10.4pt;margin:11pt 0 2pt;break-after:avoid}
  footer.end{font-size:8pt;margin-top:22pt}
}
"""


def slug(text: str) -> str:
    """GitHub-style heading anchor, so the Markdown file's own links keep working."""
    text = re.sub(r"<[^>]+>", "", text).lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.strip().replace(" ", "-")


def inline_svg(name: str) -> str:
    svg = (HERE / name).read_text(encoding="utf-8").strip()
    return re.sub(r"^<\?xml[^>]*\?>\s*", "", svg)


def build_html() -> str:
    try:
        import markdown
    except ImportError:  # pragma: no cover - documentation tool
        sys.exit("build_guide.py needs the 'markdown' package: pip install markdown")

    src = SOURCE.read_text(encoding="utf-8")
    # The cover and the contents page replace the Markdown title block and its link list.
    body_md = src[src.index("## 1. The flow in one picture"):]
    # Task-list items are not core Markdown: mark them and style them afterwards.
    body_md = re.sub(r"^- \[ \] ", "- CHECKBOX ", body_md, flags=re.M)

    body = markdown.markdown(body_md, extensions=["tables", "fenced_code", "sane_lists"], output_format="html5")

    # ---- figures: <p><img></p> -> <figure> with the SVG inline -------------------------------
    def figure(m: re.Match[str]) -> str:
        alt, path = html.unescape(m.group(1)), m.group(2)
        label, _, rest = alt.partition(". ")
        return (f'<figure>{inline_svg(Path(path).name)}'
                f'<figcaption><b>{html.escape(label)}.</b> {html.escape(rest)}</figcaption></figure>')
    body = re.sub(r'<p><img alt="([^"]*)" src="([^"]+)"\s*/?></p>', figure, body)

    # ---- headings: ids, numbers, step badges, sections --------------------------------------
    toc: list[tuple[str, str, str]] = []
    parts = re.split(r"(<h2>.*?</h2>)", body)
    out: list[str] = []
    open_section = False
    for part in parts:
        m = re.fullmatch(r"<h2>(.*?)</h2>", part)
        if not m:
            out.append(part)
            continue
        text = m.group(1)
        num, _, title = text.partition(". ")
        anchor = slug(text)
        n = int(num)
        classes = ["brk"] if n in PAGE_BREAK_BEFORE else []
        step = re.match(r"Step (\d): (.*)", title)
        if open_section:
            out.append("</section>")
        if step:
            k = int(step.group(1))
            tone = STEP_TONE[k]
            head = step.group(2)
            head = head[0].upper() + head[1:]
            classes += ["step", tone]
            out.append(f'<section class="{tone}">')
            out.append(f'<h2 id="{anchor}" class="{" ".join(classes)}">'
                       f'<span class="badge">Step<b>{k}</b></span><span>{head}</span></h2>')
            toc.append((num, f"Step {k}: {step.group(2)}", anchor))
        else:
            extra = " faq" if title == "FAQ" else ""
            out.append(f'<section class="plain{extra}">')
            out.append(f'<h2 id="{anchor}" class="{" ".join(classes)}"><span class="num">{num}</span>{title}</h2>')
            toc.append((num, title, anchor))
        open_section = True
    if open_section:
        out.append("</section>")
    body = "".join(out)
    body = re.sub(r"<h3>(.*?)</h3>", lambda m: f'<h3 id="{slug(m.group(1))}">{m.group(1)}</h3>', body)

    # ---- small structural touches -----------------------------------------------------------
    body = body.replace("<table>", '<div class="tbl"><table>').replace("</table>", "</table></div>")
    body = re.sub(r"<p><strong>Question\.</strong>\s*", '<p class="q"><strong>Question</strong>', body)
    body = re.sub(r"<p>(<strong>[^<]{3,60}\.</strong>)", r'<p class="lead">\1', body)
    body = re.sub(r'<pre><code class="language-text">', '<pre class="out"><code>', body)
    # A text block that is a formula, not program output, gets the quieter style.
    body = re.sub(r'<pre class="out"><code>(?=(ROAS = sales|contribution\[|capped day|before = \[|confidence = clamp|'
                  r'defense 4\.0|watch 25|classic raise))', '<pre class="formula"><code>', body)
    body = re.sub(r"<ul>\s*<li>CHECKBOX ", '<ul class="check"><li>CHECKBOX ', body)
    body = body.replace("<li>CHECKBOX ", "<li>")
    # FAQ questions are bold one-line paragraphs followed by the answer on the next line.
    faq_at = body.find('class="plain faq"')
    if faq_at >= 0:
        head, tail = body[:faq_at], body[faq_at:]
        tail = re.sub(r'<p(?: class="lead")?><strong>((?:(?!</strong>).)+\?)</strong>\s*\n',
                      r'<p class="fq">\1</p>\n<p>', tail)
        body = head + tail
    # A pipe inside a table cell has to be escaped in Markdown; show it plainly in code.
    body = re.sub(r"<code>[^<]*</code>", lambda m: m.group(0).replace("\\|", "|"), body)
    # Relative links to sibling docs work from docs/guide/.
    body = body.replace('href="guide/', 'href="')

    toc_html = "\n".join(f'<li><span>{n}</span><a href="#{a}">{html.escape(html.unescape(t))}</a></li>' for n, t, a in toc)
    steps = [("1", "dg", "Identity: which channel moved"), ("2", "dg", "Attribution: which lever"),
             ("3", "dg", "Budget, keyword and CPM checks"), ("4", "pl", "Rule families propose cards"),
             ("5", "pl", "Pipeline resolves and caps"), ("6", "pl", "Verdicts type every card"),
             ("7", "ld", "Ledger scores what happened")]
    steps_html = "".join(f'<li class="{tone}"><b>{n}</b>{label}</li>' for n, tone, label in steps)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>How cpm-lineage works</title>
<meta name="author" content="{AUTHOR}">
<meta name="description" content="A practitioner's guide to the cpm_lineage library: one worked example from input rows to scored outcomes.">
<style>{CSS}</style>
</head>
<body>
<div class="sheet">
<header class="cover">
  <div class="kicker">cpm-lineage {VERSION} &middot; practitioner's guide</div>
  <h1>How cpm-lineage works</h1>
  <p class="sub">One account, followed from the rows you feed in to the scored outcome of an action.
  What each step asks, the formula in one line, the thresholds that matter, and what the output looks like.</p>
  <p class="meta">Written by <b>{AUTHOR}</b> &nbsp;&middot;&nbsp; for performance marketers and engineers
  &nbsp;&middot;&nbsp; MIT licence</p>
  <ol class="steps">{steps_html}</ol>
  <div class="note"><b>About the numbers.</b> Every figure in this guide was produced by running the library on its own
  seeded synthetic dataset (seed 7, 90 days, five planted events). No data from a real account is in the
  repository, so nothing here is a claim about real-world performance. The commands that reproduce each
  number are given where it appears.</div>
</header>
<nav class="toc" aria-label="Contents">
  <h2>Contents</h2>
  <ol>
{toc_html}
  </ol>
</nav>
<main>
{body}
</main>
<footer class="end">How cpm-lineage works &middot; cpm-lineage {VERSION} &middot; {AUTHOR} &middot;
generated from <code>docs/how_it_works.md</code> by <code>docs/guide/build_guide.py</code></footer>
</div>
</body>
</html>
"""


def render_pdf() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - documentation tool
        sys.exit("the --pdf step needs Playwright: pip install playwright")
    footer = ('<div style="width:100%;font-family:Inter,Helvetica,Arial,sans-serif;font-size:7.5px;color:#5b6775;'
              'padding:0 15mm;display:flex;justify-content:space-between">'
              f'<span>How cpm-lineage works &middot; {VERSION}</span>'
              '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>')
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome")
        except Exception:
            browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(HTML_OUT.as_uri())
        page.emulate_media(media="print")
        page.pdf(path=str(PDF_OUT), format="A4", print_background=True, prefer_css_page_size=True,
                 display_header_footer=True, header_template="<span></span>", footer_template=footer)
        browser.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pdf", action="store_true", help="also render the PDF with Playwright")
    args = ap.parse_args()
    HTML_OUT.write_text(build_html(), encoding="utf-8")
    print(f"wrote {HTML_OUT.name}")
    if args.pdf:
        render_pdf()
        print(f"wrote {PDF_OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
