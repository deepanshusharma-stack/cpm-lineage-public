"""Build ``paper.html`` from ``results.json`` and the library's config dataclasses.

    python docs/paper/experiments.py      # writes results.json
    python docs/paper/build_paper.py      # writes paper.html
    node docs/paper/render_pdf.js         # writes cpm-lineage-paper.pdf (needs Playwright)

Every number in the evaluation section, every figure that shows data and the
threshold appendix are generated here, so the paper cannot drift from the
measured results or from the defaults in the code.  The prose lives in
``paper_body.html`` with ``[[name]]`` placeholders.
"""

from __future__ import annotations

import dataclasses
import html
import importlib
import importlib.util
import inspect
import json
import math
import pkgutil
import re
import sys
from pathlib import Path
from typing import Any

import cpm_lineage
from cpm_lineage.budget import probe as budget_probe

HERE = Path(__file__).resolve().parent
R = json.loads((HERE / "results.json").read_text())

BLUE, ORANGE, INK, MUTED, GRID, FILL = "#2a78d6", "#eb6834", "#0b0b0b", "#52514e", "#d9d8d3", "#f3f2ee"
SANS = "Liberation Sans, Arial, Helvetica, sans-serif"


def esc(x: Any) -> str:
    return html.escape(str(x), quote=False)


def num(x: float, d: int = 0) -> str:
    return f"{x:,.{d}f}"


def sci(x: float) -> str:
    if x == 0:
        return "0"
    e = int(math.floor(math.log10(abs(x))))
    m = x / 10 ** e
    return f"{m:.1f} &times; 10<sup>&minus;{abs(e)}</sup>" if e < 0 else f"{m:.1f} &times; 10<sup>{e}</sup>"


def frac(d: dict[str, Any]) -> str:
    return f"{d['hits']}/{d['n']}"


# --------------------------------------------------------------------------- SVG helpers

def text(x: float, y: float, s: str, *, size: float = 11, anchor: str = "start", fill: str = INK,
         weight: str = "normal", style: str = "") -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" text-anchor="{anchor}" fill="{fill}" '
            f'font-weight="{weight}" paint-order="stroke" stroke="#ffffff" stroke-width="2.6" stroke-linejoin="round" '
            f'{style}>{esc(s)}</text>')


def box(x: float, y: float, w: float, h: float, title: str, lines: list[str], *, fill: str = "#ffffff",
        stroke: str = INK, note: str | None = None) -> str:
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="5" fill="{fill}" stroke="{stroke}" stroke-width="1"/>',
           text(x + w / 2, y + 17, title, size=11, anchor="middle", weight="bold")]
    for i, ln in enumerate(lines):
        out.append(text(x + w / 2, y + 32 + 12.5 * i, ln, size=9.6, anchor="middle", fill=MUTED))
    if note:
        out.append(text(x + w / 2, y + h + 13, note, size=9.4, anchor="middle", fill=BLUE, weight="bold"))
    return "".join(out)


def arrow(x1: float, y1: float, x2: float, y2: float, *, color: str = INK, dash: str = "") -> str:
    d = f' stroke-dasharray="{dash}"' if dash else ""
    mk = "ah" if color == INK else "ahb"
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="1.2"{d} marker-end="url(#{mk})"/>'


DEFS = ('<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="{INK}"/></marker>'
        '<marker id="ahb" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="{BLUE}"/></marker></defs>')


def svg(w: int, h: int, body: str, label: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="100%" role="img" '
            f'aria-label="{esc(label)}" font-family="{SANS}">{DEFS}{body}</svg>')


# --------------------------------------------------------------------------- figure 1: pipeline

def fig_pipeline() -> str:
    c = R["figure_run"]["counts"]
    fam = sum(c["telemetry"]["families"].values())
    sweep = c["cards"] - fam
    b: list[str] = []
    w, h, gap = 150, 74, 22
    xs = [8 + i * (w + gap) for i in range(4)]
    y1, y2, y3 = 26, 166, 306
    b.append(text(8, 16, "diagnose(): what happened and why", size=10.5, weight="bold", fill=MUTED))
    b.append(box(xs[0], y1, w, h, "Feeds via adapter", ["delivery rows (required)", "budget, shelf, market,", "sell-out, change log"], fill=FILL))
    b.append(text(xs[0] + 34, y1 + h + 14, f"{c['perf_rows']:,} rows, {c['entities']} entities, {c['days']} days", size=9.4, fill=BLUE, weight="bold"))
    b.append(box(xs[1], y1, w, h, "Layer 1: exact", ["CPM identity, shift-share,", "entry / exit, Pareto drag", "(no model, residual 0)"]))
    b.append(box(xs[2], y1, w, h, "Layer 2: estimated", ["lever attribution + jury,", "budget probe + curve,", "CPM views, keyword scans"]))
    b.append(box(xs[3], y1, w, h, "Typed findings", ["kind, subject, size,", "source module,", "evidence numbers"], fill=FILL,
                 note=f"{c['findings']} findings"))
    for i in range(3):
        b.append(arrow(xs[i] + w, y1 + h / 2, xs[i + 1] - 2, y1 + h / 2))
    b.append(text(xs[0] + 34, y2 - 10, "plan(): what should be done", size=10.5, weight="bold", fill=MUTED))
    b.append(box(xs[0], y2, w, h, "Rule families (8)", ["threshold rules emit", "candidate action cards;", "holdout drawn first"], note=f"{fam} family cards"))
    b.append(box(xs[1], y2, w, h, "Plan pipeline", ["scrubs and vetoes,", "conflict table, confidence,", "run guardrail, caps"]))
    b.append(box(xs[2], y2, w, h, "Verdict spine", ["ordered gates type each", "card; sweeps add cards", "the families missed"], note=f"+{sweep} sweep card"))
    st = ", ".join(f"{k} {v}" for k, v in c["verdict_states"].items())
    b.append(box(xs[3], y2, w, h, "Reviewable plan", ["ACT / OBSERVE / HOLD /", "ESCALATE / NO-ACTION,", "nothing auto-applied"], fill=FILL))
    b.append(text(xs[3] + w / 2 - 12, y2 + h + 14, f"{c['cards']} cards: {st}", size=9.4, anchor="end", fill=BLUE, weight="bold"))
    for i in range(3):
        b.append(arrow(xs[i] + w, y2 + h / 2, xs[i + 1] - 2, y2 + h / 2))
    b.append(arrow(xs[0] + 20, y1 + h, xs[0] + 20, y2 - 2))
    b.append(box(xs[3], y3, w, h, "Action ledger", ["one row per card and", "horizon (7 / 14 / 30 d),", "losers included"]))
    b.append(box(xs[2], y3, w, h, "Outcome pairing", ["before / after ROAS vs", "tide-adjusted expectation;", "difference-in-differences"]))
    b.append(box(xs[1], y3, w, h, "Family win rates", ["On Track share at day 7,", "used once n >= 5"]))
    b.append(arrow(xs[3] + w / 2, y2 + h, xs[3] + w / 2, y3 - 2, color=BLUE))
    b.append(arrow(xs[3], y3 + h / 2, xs[2] + w + 2, y3 + h / 2, color=BLUE))
    b.append(arrow(xs[2], y3 + h / 2, xs[1] + w + 2, y3 + h / 2, color=BLUE))
    b.append(arrow(xs[1] + w / 2 + 40, y3, xs[1] + w / 2 + 40, y2 + h + 2, color=BLUE))
    b.append(text(xs[1] + w / 2 + 34, y3 - 20, "confidence, P(success)", size=9.4, anchor="end", fill=BLUE))
    b.append(text(xs[3] + w / 2 + 8, y3 - 20, "save plan", size=9.4, fill=BLUE))
    b.append(text(8, y3 + h + 18, "Feedback loop: needs an operator to mark cards and later data to arrive; not exercised by the single-shot runs in this paper.", size=9.6, fill=MUTED))
    return svg(690, y3 + h + 26, "".join(b), "Pipeline from delivery rows to a typed plan and the feedback loop")


# --------------------------------------------------------------------------- figure 2: waterfall

def fig_waterfall() -> str:
    panels = [("Account", R["figure_run"]["account"]), ("Entity cmp-generic (carries the planted CPM spike)", R["figure_run"]["cmp_generic"])]
    b: list[str] = []
    pw, ph, top, left = 300, 190, 46, 52
    for pi, (title, d) in enumerate(panels):
        ox = 10 + pi * 345
        ch = d["channels"]
        steps = [("Δ ln ICR", ch["icr1k"]["log_points"]), ("Δ ln ASP", ch["asp"]["log_points"]), ("− Δ ln CPM", ch["cpm"]["log_points"])]
        cum, levels = 0.0, [0.0]
        for _, v in steps:
            cum += v
            levels.append(cum)
        lo, hi = min(levels) - 0.03, max(levels) + 0.03
        ys = lambda v: top + (hi - v) / (hi - lo) * ph
        b.append(text(ox, 14, title, size=11, weight="bold"))
        b.append(text(ox, 29, f"ROAS {d['roas_prior']:.2f} → {d['roas_current']:.2f};  ln ratio {d['raw_dln']:+.4f}", size=10, fill=MUTED))
        # grid
        tick = 0.05
        t = math.ceil(lo / tick) * tick
        while t <= hi + 1e-9:
            y = ys(t)
            b.append(f'<line x1="{ox + left}" y1="{y:.1f}" x2="{ox + left + pw - 60}" y2="{y:.1f}" stroke="{INK if abs(t) < 1e-9 else GRID}" stroke-width="{1 if abs(t) < 1e-9 else 0.7}"/>')
            b.append(text(ox + left - 6, y + 3.5, f"{t:+.2f}" if abs(t) > 1e-9 else "0", size=9.2, anchor="end", fill=MUTED))
            t += tick
        bw, slot = 38, (pw - 60) / 4
        prev = 0.0
        for i, (lab, v) in enumerate(steps):
            x = ox + left + slot * i + (slot - bw) / 2
            y0, y1 = ys(prev), ys(prev + v)
            col = BLUE if v >= 0 else ORANGE
            b.append(f'<rect x="{x:.1f}" y="{min(y0, y1):.1f}" width="{bw}" height="{max(abs(y1 - y0), 1.2):.1f}" rx="2" fill="{col}"/>')
            b.append(text(x + bw / 2, (min(y0, y1) - 5) if v >= 0 else (max(y0, y1) + 12), f"{v:+.4f}", size=9.6, anchor="middle"))
            b.append(text(x + bw / 2, top + ph + 15, lab, size=9.6, anchor="middle"))
            if i < 3:
                b.append(f'<line x1="{x + bw:.1f}" y1="{y1:.1f}" x2="{x + slot:.1f}" y2="{y1:.1f}" stroke="{MUTED}" stroke-width="0.8" stroke-dasharray="3 2"/>')
            prev += v
        x = ox + left + slot * 3 + (slot - bw) / 2
        y0, y1 = ys(0), ys(prev)
        b.append(f'<rect x="{x:.1f}" y="{min(y0, y1):.1f}" width="{bw}" height="{max(abs(y1 - y0), 1.2):.1f}" rx="2" fill="#8a8984"/>')
        b.append(text(x + bw / 2, (max(y0, y1) + 12) if prev < 0 else (min(y0, y1) - 5), f"{prev:+.4f}", size=9.6, anchor="middle", weight="bold"))
        b.append(text(x + bw / 2, top + ph + 15, "Δ ln ROAS", size=9.6, anchor="middle", weight="bold"))
        b.append(text(ox + left, top + ph + 32, f"sum of parts minus ln ratio = {d['residual']:.1e}", size=9.4, fill=MUTED))
    b.append(f'<rect x="12" y="{top + ph + 44}" width="10" height="10" rx="2" fill="{BLUE}"/>' + text(27, top + ph + 53, "raises ROAS", size=9.6))
    b.append(f'<rect x="100" y="{top + ph + 44}" width="10" height="10" rx="2" fill="{ORANGE}"/>' + text(115, top + ph + 53, "lowers ROAS", size=9.6))
    b.append(f'<rect x="192" y="{top + ph + 44}" width="10" height="10" rx="2" fill="#8a8984"/>' + text(207, top + ph + 53, "net move (log points)", size=9.6))
    return svg(690, top + ph + 62, "".join(b), "Waterfall of the exact identity decomposition for the account and one entity")


# --------------------------------------------------------------------------- figure 3: conflict flow

def diamond(cx: float, cy: float, w: float, h: float, lines: list[str]) -> str:
    pts = f"{cx},{cy - h / 2} {cx + w / 2},{cy} {cx},{cy + h / 2} {cx - w / 2},{cy}"
    out = [f'<polygon points="{pts}" fill="#ffffff" stroke="{INK}" stroke-width="1"/>']
    for i, ln in enumerate(lines):
        out.append(text(cx, cy - 6 * (len(lines) - 1) + 12.5 * i + 3.5, ln, size=9.6, anchor="middle"))
    return "".join(out)


def fig_conflict() -> str:
    b: list[str] = []
    b.append(box(8, 14, 150, 62, "Pending cards", ["after supply gate and", "duplicate merge"], fill=FILL))
    b.append(diamond(245, 45, 130, 72, ["same entity,", "budget up AND", "budget down?"]))
    b.append(arrow(158, 45, 178, 45))
    b.append(box(350, 14, 150, 62, "Winner", ["card of the family with the", "highest table priority"]))
    b.append(arrow(310, 45, 348, 45)); b.append(text(318, 39, "yes", size=9.4, fill=MUTED))
    b.append(diamond(590, 45, 150, 80, ["opposing stake", "> 3 × winner", "stake?"]))
    b.append(arrow(500, 45, 513, 45))
    b.append(arrow(245, 81, 245, 122)); b.append(text(251, 104, "no: all stay pending", size=9.4, fill=MUTED))
    b.append(box(170, 124, 150, 50, "Coherent sequence", ["ordered by waves 1–3"]))
    b.append(box(350, 124, 150, 50, "Superseded", ["loser keeps its record:", "NO-ACTION(conflict)"]))
    b.append(box(520, 124, 150, 50, "Tension group", ["table abstains; a human", "may apply exactly one"], stroke=ORANGE))
    b.append(f'<polyline points="545,72 425,105 425,122" fill="none" stroke="{INK}" stroke-width="1.2" marker-end="url(#ah)"/>')
    b.append(text(448, 92, "no", size=9.4, fill=MUTED))
    b.append(arrow(600, 85, 600, 122)); b.append(text(606, 106, "yes", size=9.4, fill=MUTED))
    # priority strip
    pri = sorted(cpm_lineage.actions.config.FAMILY_PRIORITY.items(), key=lambda kv: -kv[1])
    b.append(text(8, 200, "Family priority (fixed table):  " + "  >  ".join(f"{k} {v:g}" for k, v in pri), size=9.3, fill=MUTED))
    # measured sweep
    b.append(text(8, 226, "Measured (experiment H): a budget cut (efficiency-cut, stake 1,000) against a raise (efficiency-scale) at increasing stake ratio", size=9.8, weight="bold"))
    rows = R["H_valve"]["rows"]
    x0, step = 60, 86
    b.append(f'<line x1="{x0 - 20}" y1="262" x2="{x0 + step * (len(rows) - 1) + 20}" y2="262" stroke="{GRID}" stroke-width="1"/>')
    for i, r in enumerate(rows):
        x = x0 + step * i
        ten = r["cut_status"] == "tension"
        b.append(f'<circle cx="{x}" cy="262" r="7" fill="{ORANGE if ten else BLUE}" stroke="#ffffff" stroke-width="2"/>')
        b.append(text(x, 247, f"{r['opposing_stake_ratio']:g}×", size=10, anchor="middle", weight="bold"))
        b.append(text(x, 284, "tension" if ten else "cut wins,", size=9.4, anchor="middle"))
        b.append(text(x, 296, "(both held)" if ten else "raise superseded", size=9.4, anchor="middle", fill=MUTED))
    return svg(690, 304, "".join(b), "Decision flow of the budget conflict table and the measured stake-ratio valve")


# --------------------------------------------------------------------------- figure 4: saturation

def fig_saturation() -> str:
    f = R["G_curve"]["figure"]
    bb, th, tb, tt = f["b"], f["theta"], f["true_b"], f["true_theta"]
    xstar = f["unit_marginal_spend"]
    cap = 8000.0
    lo, hi, rec = budget_probe.recommended_range(cap, xstar)
    X0, Y0, W, H = 62, 16, 560, 250
    xmax, ymax = 20000.0, 45000.0
    px = lambda x: X0 + x / xmax * W
    py = lambda y: Y0 + H - y / ymax * H
    b: list[str] = []
    for yt in range(0, 45001, 10000):
        b.append(f'<line x1="{X0}" y1="{py(yt):.1f}" x2="{X0 + W}" y2="{py(yt):.1f}" stroke="{GRID}" stroke-width="0.7"/>')
        b.append(text(X0 - 6, py(yt) + 3.5, f"{yt:,}", size=9.2, anchor="end", fill=MUTED))
    for xt in range(0, 20001, 4000):
        b.append(text(px(xt), Y0 + H + 14, f"{xt:,}", size=9.2, anchor="middle", fill=MUTED))
    b.append(f'<line x1="{X0}" y1="{Y0 + H}" x2="{X0 + W}" y2="{Y0 + H}" stroke="{INK}" stroke-width="1"/>')
    b.append(text(X0 + W / 2, Y0 + H + 30, "daily spend", size=10, anchor="middle"))
    b.append(f'<text x="14" y="{Y0 + H / 2}" font-size="10" text-anchor="middle" fill="{INK}" transform="rotate(-90 14 {Y0 + H / 2})">daily attributed sales</text>')
    # recommended range band
    b.append(f'<rect x="{px(lo):.1f}" y="{Y0}" width="{px(hi) - px(lo):.1f}" height="{H}" fill="{BLUE}" opacity="0.09"/>')
    for x, y in zip(f["spend"], f["sales"]):
        if y <= ymax:
            b.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="2.1" fill="#8a8984" opacity="0.75"/>')
    def path(B: float, T: float) -> str:
        pts = []
        for i in range(0, 201):
            x = xmax * i / 200
            pts.append(f"{px(x):.1f},{py(B * math.log1p(x / T)):.1f}")
        return " ".join(pts)
    b.append(f'<polyline points="{path(tb, tt)}" fill="none" stroke="{INK}" stroke-width="1" stroke-dasharray="4 3"/>')
    b.append(f'<polyline points="{path(bb, th)}" fill="none" stroke="{BLUE}" stroke-width="2"/>')
    ystar = bb * math.log1p(xstar / th)
    # slope-1 tangent
    dx = 3600
    b.append(f'<line x1="{px(xstar - dx):.1f}" y1="{py(ystar - dx):.1f}" x2="{px(xstar + dx):.1f}" y2="{py(ystar + dx):.1f}" stroke="{ORANGE}" stroke-width="1.4"/>')
    b.append(f'<line x1="{px(xstar):.1f}" y1="{py(ystar):.1f}" x2="{px(xstar):.1f}" y2="{Y0 + H}" stroke="{ORANGE}" stroke-width="1" stroke-dasharray="3 3"/>')
    b.append(f'<circle cx="{px(xstar):.1f}" cy="{py(ystar):.1f}" r="4.5" fill="{ORANGE}" stroke="#ffffff" stroke-width="2"/>')
    b.append(text(px(xstar) + 8, py(ystar) + 22, f"marginal ROAS = 1 at b − θ = {xstar:,.0f}", size=10, weight="bold"))
    b.append(text(px(xstar) + 8, py(ystar) + 35, "tangent has slope 1: beyond this point one more", size=9.4, fill=MUTED))
    b.append(text(px(xstar) + 8, py(ystar) + 47, "unit of spend returns less than one unit of sales", size=9.4, fill=MUTED))
    # cap
    ycap = bb * math.log1p(cap / th)
    b.append(f'<line x1="{px(cap):.1f}" y1="{Y0 + H}" x2="{px(cap):.1f}" y2="{py(ycap):.1f}" stroke="{INK}" stroke-width="1"/>')
    b.append(f'<circle cx="{px(cap):.1f}" cy="{py(ycap):.1f}" r="4" fill="{INK}" stroke="#ffffff" stroke-width="2"/>')
    b.append(text(px(cap) - 7, py(ycap) - 9, f"example cap {cap:,.0f}: marginal {bb / (th + cap):.2f}", size=9.6, anchor="end"))
    b.append(text(px(lo) + 4, Y0 + 12, f"raise range {lo:,.0f} – {hi:,.0f}", size=9.6, fill=BLUE, weight="bold"))
    b.append(text(px(lo) + 4, Y0 + 24, f"(recommended {rec:,.0f}; high end clamped", size=9.2, fill=MUTED))
    b.append(text(px(lo) + 4, Y0 + 35, f"from 2 × cap = {2 * cap:,.0f} to b − θ)", size=9.2, fill=MUTED))
    # legend
    ly = Y0 + H + 48
    b.append(f'<line x1="{X0}" y1="{ly}" x2="{X0 + 22}" y2="{ly}" stroke="{BLUE}" stroke-width="2"/>' + text(X0 + 27, ly + 3.5, f"fitted: b = {bb:,.0f}, θ = {th:,.0f}, in-sample R² = {f['r2']:.2f}", size=9.6))
    b.append(f'<line x1="{X0 + 265}" y1="{ly}" x2="{X0 + 287}" y2="{ly}" stroke="{INK}" stroke-width="1" stroke-dasharray="4 3"/>' + text(X0 + 292, ly + 3.5, f"planted: b = {tb:,.0f}, θ = {tt:,.0f}", size=9.6))
    b.append(f'<circle cx="{X0 + 452}" cy="{ly}" r="2.4" fill="#8a8984"/>' + text(X0 + 460, ly + 3.5, f"90 daily points (seed {f['seed']})", size=9.6))
    return svg(690, ly + 12, "".join(b), "Saturation curve fit with the marginal-ROAS-equals-one point and the clamped raise range")


# --------------------------------------------------------------------------- figure 5: dose-response

def fig_dose() -> str:
    C = R["C_dose"]
    panels = [("CPM multiplier on one keyword (planted: 1.6)", C["cpm_spike_multiplier"], "a finding needs a rise above 5%"),
              ("Availability level after the drop (planted: 0.45)", C["availability_level_after_drop"], "the geo-level window mean must fall by 0.05"),
              ("True ROAS of the conquest keyword (planted: 0.35)", C["bleed_keyword_true_roas"], "cut bar: ROAS below 1"),
              ("Daily budget as a multiple of demand (planted: 0.33)", C["winner_budget_factor"], "capped: spend at 90% of budget on 60% of days")]
    b: list[str] = []
    pw, ph, top, rowh = 270, 70, 34, 138
    for i, (title, rows, note) in enumerate(panels):
        ox = 40 + (i % 2) * 335
        oy = (i // 2) * rowh
        b.append(text(ox, oy + 13, title, size=9.8, weight="bold"))
        b.append(text(ox, oy + 25, note, size=9, fill=MUTED))
        for g in (0, 0.5, 1.0):
            y = oy + top + ph - g * ph
            b.append(f'<line x1="{ox}" y1="{y:.1f}" x2="{ox + pw}" y2="{y:.1f}" stroke="{GRID if g else INK}" stroke-width="{0.7 if g else 1}"/>')
            b.append(text(ox - 5, y + 3.5, f"{int(g * 100)}%", size=8.8, anchor="end", fill=MUTED))
        n = len(rows)
        xs = [ox + 14 + j * (pw - 28) / (n - 1) for j in range(n)]
        pts = " ".join(f"{x:.1f},{oy + top + ph - r['rate'] * ph:.1f}" for x, r in zip(xs, rows))
        b.append(f'<polyline points="{pts}" fill="none" stroke="{BLUE}" stroke-width="2"/>')
        for x, r in zip(xs, rows):
            y = oy + top + ph - r["rate"] * ph
            b.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{BLUE}" stroke="#ffffff" stroke-width="2"/>')
            b.append(text(x, oy + top + ph + 14, f"{r['level']:g}", size=9, anchor="middle", fill=MUTED))
            if 0 < r["rate"] < 1:
                b.append(text(x + 8, y + 3.5, f"{r['hits']}/{r['n']}", size=9.2))
    return svg(690, 2 * rowh - 12, "".join(b), "Detection rate against the size of each planted event")


# --------------------------------------------------------------------------- tables

def table(head: list[str], rows: list[list[str]], cls: str = "t", widths: list[str] | None = None) -> str:
    cg = "".join(f'<col style="width:{w}">' for w in widths) if widths else ""
    th = "".join(f"<th>{h}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="{cls}"><colgroup>{cg}</colgroup><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>'


def tbl_detection() -> str:
    A = R["A_detection"]
    names = {"cpm_spike": ("CPM spike", "CPM of one keyword &times;1.6 for the last 30 days; a rival's ad impressions &times;2.5", "<code>cpm_rise</code> on that keyword and entity"),
             "availability_drop": ("Availability drop", "one product in one geo falls 0.95 &rarr; 0.45 for the last 21 days", "<code>availability_drop</code> naming that geo and product"),
             "budget_capped_winner": ("Budget-capped winner", "ROAS above 4, budget covers about a third of demand", "<code>budget_bound_winner</code> on that entity"),
             "bleeding_keyword": ("Bleeding keyword", "competitor keyword at ROAS about 0.35 for 90 days", "<code>bleeding_keyword</code> on that keyword"),
             "own_overlap": ("Own-campaign overlap", "two own campaigns bid one keyword in every geo, every day", "<code>own_collision</code> on that keyword")}
    rows = []
    for k, (n, what, match) in names.items():
        pa = A["plan_act_card"].get(k)
        rows.append([n, what, match, frac(A["findings"][k]), frac(pa) if pa else "no card type"])
    return table(["Planted event", "What the generator does", "Counted as detected when", "Finding", "ACT card"], rows,
                 widths=["15%", "33%", "30%", "10%", "12%"])


def tbl_exact() -> str:
    rows = [[esc(r["decomposition"]), num(r["n"]), sci(r["max_abs_residual"]), sci(r["median_abs_residual"])]
            for r in R["D_exactness"]["rows"]]
    return table(["Decomposition (unit of the residual)", "Cases", "Max |residual|", "Median |residual|"], rows,
                 widths=["55%", "11%", "17%", "17%"])


def tbl_second() -> str:
    F = R["F_second_platform"]
    rows = [["CPM rise on the planted query", "<code>cpm_rise</code> finding on that query", frac(F["cpm_rise_trail_shoes"])],
            ["Budget-capped line item", "<code>budget_bound_winner</code> finding on that line item", frac(F["budget_bound_li_hero"])],
            ["Budget-capped line item", "ACT budget raise on it, classic profile", frac(F["plan_act_raise_li_hero"]["classic"])],
            ["Budget-capped line item", "ACT budget raise on it, verdict profile", frac(F["plan_act_raise_li_hero"]["verdict"])],
            ["Findings on anything not planted", "count over all seeds", str(F["extra_findings_total"])],
            ["Log identity, account", "max |residual| over seeds", sci(F["max_abs_identity_residual"])]]
    return table(["Planted event or check", "Measure", "Result"], rows, widths=["34%", "46%", "20%"])


PLANTED_LABELS = ("cmp-winner: Increase Budget", "cmp-conquest: Decrease Bid (first response)",
                  "cmp-generic: Hold Bid Under Pressure", "cmp-overlap: Resolve Keyword Collision",
                  "li-hero: Increase Budget", "li-generic: Hold Bid Under Pressure")


def tbl_cards() -> str:
    rows = []
    for world, key in (("Marketplace", "A_detection"), ("Second platform", "F_second_platform")):
        n = len(R[key]["seeds"])
        for prof in ("classic", "verdict"):
            tally = R[key]["cards_by_profile"][prof]
            for label, cnt in tally.items():
                ent_title, lane = label.split(" -> ")
                planted = ent_title in PLANTED_LABELS
                if planted and lane == "ACT":
                    continue
                rows.append([world, prof, esc(ent_title), esc(lane), f"{cnt}/{n}"])
    return table(["World", "Rule profile", "Card not aimed at a planted event, or not shipped", "State", "Plans"], rows,
                 widths=["15%", "12%", "45%", "18%", "10%"])


def tbl_attr() -> str:
    I = R["I_attribution"]
    def cell(d: dict[str, int]) -> str:
        return ", ".join(f"{esc(k)} {v}" for k, v in sorted(d.items(), key=lambda kv: -kv[1]))
    rows = []
    for lab, k in (("CPM spike planted (&times;1.6)", "with_spike_1.6x"), ("No spike planted", "without_spike")):
        d = I[k]
        rows.append([lab, cell(d["verdict"]), cell(d["top_lever"]), cell(d["first_read"])])
    return table(["World", "Attribution verdict", "Largest lever", "Layer-1 first read"], rows, widths=["18%", "24%", "30%", "28%"])


def tbl_curve() -> str:
    rows = [[f"{r['noise_sigma']:.2f}", f"{r['fitted']}/{r['n']}", f"{r['median_r2']:.2f}",
             f"{100 * r['median_rel_err_marginal_at_8000']:.1f}%",
             f"{100 * r['median_rel_err_unit_marginal_spend']:.1f}%", f"{100 * r['max_rel_err_unit_marginal_spend']:.0f}%"]
            for r in R["G_curve"]["rows"]]
    return table(["Noise &sigma; (log scale)", "Fits accepted", "Median R&sup2;", "Median error, marginal ROAS at 8,000",
                  "Median error, b &minus; &theta;", "Max error, b &minus; &theta;"], rows)


def tbl_determinism() -> str:
    E = R["E_determinism"]
    n = E["n"]
    rows = [["Same seed, dataset generated twice, each diagnosed and planned", "digest of findings, identity table, attribution verdicts, plan id and every card", f"{E['repeat_identical_digest']}/{n}"],
            ["Every feed's rows shuffled", "same digest", f"{E['shuffled_identical_digest']}/{n}"],
            ["Every feed's rows shuffled", "same plan id", f"{E['shuffled_identical_plan_id']}/{n}"]]
    return table(["Perturbation", "Compared", "Identical"], rows, widths=["38%", "50%", "12%"])


# --------------------------------------------------------------------------- appendix: thresholds from the dataclasses

SELECT: list[tuple[str, str, list[str]]] = [
    ("cpm_lineage.identity", "IdentityConfig", ["log_floor", "min_dln", "verdict_bid_flat_pct", "verdict_min_spend"]),
    ("cpm_lineage.decompose", "DecomposeConfig", ["pareto_cum_pct", "composition_min_spend"]),
    ("cpm_lineage.attribution.config", "AttributionConfig",
     ["coverage_min", "impute_max", "thin_min_levers", "vif_max", "lambdas", "short_lambda", "delta_lambda",
      "jury_materiality", "explained_floor_pct", "skill_floor", "confirmed_min_skill", "small_move_dln",
      "shapley_max_features"]),
    ("cpm_lineage.config", "TierConfig", ["warm_min_days", "mature_min_days", "full_min_days"]),
    ("cpm_lineage.config", "Economics", ["breakeven", "scale_bar_mult"]),
    ("cpm_lineage.config", "AvailabilityPolicy", ["block", "caution", "floor"]),
    ("cpm_lineage.budget.config", "BudgetConfig",
     ["sat_min_days", "sat_min_cv", "sat_min_corr", "sat_min_r2", "sat_decision_r2", "sat_ceiling_support",
      "cap_hit_fraction", "capped_day_share", "marginal_block", "marginal_ceiling", "range_low_mult",
      "range_rec_mult", "range_high_mult", "bleed_share_hold", "fd_veto_mult", "fd_halve_mult"]),
    ("cpm_lineage.cpm.config", "CpmConfig",
     ["explain_min_rise", "explain_position_step", "explain_rival_gain", "hot_cap", "low_hit", "blind_seen",
      "flat_pct", "overshoot_share", "self_floor_pct", "overpay_ratio", "overpay_ref_min_rows"]),
    ("cpm_lineage.actions.pipeline_config", "PipelineConfig",
     ["holdout_pct", "stake_ratio_valve", "contested_min_ratio", "conf_base", "conf_min", "conf_max",
      "winrate_min_n", "ev_prior", "pending_cap"]),
    ("cpm_lineage.verdict", "VerdictConfig",
     ["launch_grace_days", "evidence_min_impressions", "price_swing_pp", "lock_days", "bar_watch", "bar_bid",
      "bar_budget_up", "bar_cut", "ladder_bid_step_pct", "staircase_step_pct"]),
    ("cpm_lineage.outcomes", "OutcomeConfig",
     ["damp_min", "damp_max", "cut_hold", "raise_hold", "revenue_retention", "did_max_window_days", "did_band"]),
    ("cpm_lineage.store.ledger", "LedgerConfig", ["horizons"]),
    ("cpm_lineage.engine", "OrchestrationConfig", ["window_days", "availability_drop_min", "product_drop_min"]),
]


def field_comments(cls: type) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in inspect.getsource(cls).splitlines():
        m = re.match(r"\s+(\w+)\s*:\s*[^=]+=\s*.+?\s+#\s*(.+)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def default_of(f: dataclasses.Field) -> Any:
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:   # type: ignore[misc]
        return f.default_factory()                     # type: ignore[misc]
    return None


def tbl_thresholds() -> str:
    rows = []
    for mod, cname, names in SELECT:
        cls = getattr(importlib.import_module(mod), cname)
        comments = field_comments(cls)
        fields = {f.name: f for f in dataclasses.fields(cls)}
        for i, nm in enumerate(names):
            v = default_of(fields[nm])
            vs = ", ".join(f"{x:g}" for x in v) if isinstance(v, tuple) else (f"{v:g}" if isinstance(v, float) else str(v))
            c = comments.get(nm, "")
            c = re.sub(r"^(JUDGMENT|MONEY|money):\s*", "", c)
            rows.append([f"<code>{cname}</code>" if i == 0 else "", f"<code>{nm}</code>", esc(vs), esc(c)])
    return table(["Config dataclass", "Field", "Default", "Rationale as written next to the field"], rows, cls="t thr",
                 widths=["18%", "25%", "8%", "49%"])


def config_census() -> tuple[int, int]:
    """Classes and fields exactly as ``docs/thresholds.md`` counts them (same collector)."""
    spec = importlib.util.spec_from_file_location("gen_thresholds", HERE.parents[1] / "tools" / "gen_thresholds.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    classes = mod.collect()
    return len(classes), sum(len(c.fields) for c in classes)


# --------------------------------------------------------------------------- assemble

def main() -> None:
    A, B, C, F, I, G, E = (R[k] for k in ("A_detection", "B_null", "C_dose", "F_second_platform", "I_attribution", "G_curve", "E_determinism"))
    n_cls, n_fields = config_census()
    n = len(A["seeds"])
    fr = R["figure_run"]
    acc, ent = fr["account"], fr["cmp_generic"]
    drv = {d["lever"]: d for d in fr["attribution"]["drivers"]}
    ws = I["without_spike"]["verdict"]
    cpm5 = next(r for r in C["cpm_spike_multiplier"] if r["level"] == 1.05)
    ble1 = next(r for r in C["bleed_keyword_true_roas"] if r["level"] == 1.0)
    maxres = max(r["max_abs_residual"] for r in R["D_exactness"]["rows"] if "identity" in r["decomposition"] or "shift-share" in r["decomposition"])
    other = [r["max_abs_residual"] for r in R["D_exactness"]["rows"]
             if not ("identity" in r["decomposition"] or "shift-share" in r["decomposition"])]
    vals: dict[str, str] = {
        "fig_pipeline": fig_pipeline(), "fig_waterfall": fig_waterfall(), "fig_conflict": fig_conflict(),
        "fig_saturation": fig_saturation(), "fig_dose": fig_dose(),
        "tbl_detection": tbl_detection(), "tbl_exact": tbl_exact(), "tbl_second": tbl_second(),
        "tbl_cards": tbl_cards(), "tbl_attr": tbl_attr(), "tbl_curve": tbl_curve(),
        "tbl_determinism": tbl_determinism(), "tbl_thresholds": tbl_thresholds(),
        "n_seeds": str(n), "n_dose": str(len(C["seeds"])), "n_det": str(E["n"]),
        "rows_a": num(A["perf_rows_per_dataset"]), "rows_f": num(F["perf_rows_per_dataset"]),
        "extra_a": str(A["extra_findings_total"]), "null_any": str(B["datasets_with_any_finding"]),
        "null_cards": ", ".join(f"{v}/{B['n']}" for v in B["plan_cards"].values()),
        "cpm_class_a": ", ".join(f"&ldquo;{esc(k)}&rdquo; {v}/{n}" for k, v in A["cpm_rise_classification"].items()),
        "cpm_class_f": ", ".join(f"&ldquo;{esc(k)}&rdquo; {v}/{n}" for k, v in F["cpm_rise_classification"].items()),
        "cpm5": frac(cpm5), "ble1": frac(ble1),
        "ws_confirmed": str(ws.get("confirmed", 0)), "ws_directional": str(ws.get("directional", 0)),
        "ws_unexplained": str(ws.get("unexplained", 0)),
        "ws_story": str(ws.get("confirmed", 0) + ws.get("directional", 0)),
        "bid_share": f"{I['with_spike_1.6x']['median_bid_share_pct']:.1f}",
        "maxres": sci(maxres), "maxres_other": sci(max(other)),
        "acc_r0": f"{acc['roas_prior']:.2f}", "acc_r1": f"{acc['roas_current']:.2f}",
        "ent_r0": f"{ent['roas_prior']:.2f}", "ent_r1": f"{ent['roas_current']:.2f}",
        "ent_cpm_lp": f"{ent['channels']['cpm']['log_points']:+.3f}", "ent_dln": f"{ent['raw_dln']:+.3f}",
        "bid_contrib": f"{drv['bid']['contrib']:+.3f}", "bid_delta": f"{drv['bid']['delta_contrib']:+.3f}",
        "bid_pct": f"{drv['bid']['pct']:.0f}", "move_skill": f"{fr['attribution']['move_skill']:.2f}",
        "expl_net": f"{fr['attribution']['explained']['net']:.1f}",
        "n_cfg_cls": str(n_cls), "n_cfg_fields": f"{n_fields:,}", "n_thr": str(sum(len(s[2]) for s in SELECT)),
        "version": R["library_version"], "python": R["python"], "numpy": R["numpy"],
        "roas_generic_f": f"{F['median_entity_roas']['li-generic']:.1f}",
        "g_true_sat": num(G["true_b"] - G["true_theta"]),
    }
    body = (HERE / "paper_body.html").read_text()
    missing = set(re.findall(r"\[\[(\w+)\]\]", body)) - set(vals)
    if missing:
        raise SystemExit(f"unfilled placeholders: {sorted(missing)}")
    body = re.sub(r"<mi>(ln|arg|min|lo|hi|clamp)</mi>", r'<mi mathvariant="normal">\1</mi>', body)
    out = re.sub(r"\[\[(\w+)\]\]", lambda m: vals[m.group(1)], body)
    (HERE / "paper.html").write_text(out)
    print(f"wrote {HERE / 'paper.html'} ({len(out) / 1024:.0f} KB); config census: {n_cls} dataclasses, {n_fields} fields")


if __name__ == "__main__":
    main()
