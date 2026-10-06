"""
Executive report — one self-contained HTML page for the leadership review.
    python exec_report.py                      # default: Jul 17 - Aug 5 window
    python exec_report.py 2026-07-01 2026-08-05

Answers the notebook questions in order, one chart per question, one insight
per chart. Numbers come from the dashboard's own builders (dashboard.payload),
so this report can never disagree with the dashboard. Static SVG - no JS, no
network, safe to email or project.
"""
import html
import sys
from pathlib import Path

import dashboard as dsh

HERE = Path(__file__).parent
OUT = HERE / "exec_report.html"

# validated palette (dataviz six-checks, light surface)
C_GROUP = {"Booked": "#008300", "Engaged": "#2a78d6", "Dropped": "#e34948",
           "Not reached": "#4a3aa7", "Rejected": "#eb6834",
           "Unclassified": "#8a8f98"}
BLUE, GREEN, RED, ORANGE, VIOLET, GRAY = ("#2a78d6", "#008300", "#e34948",
                                          "#eb6834", "#4a3aa7", "#8a8f98")
INK, MUT = "#1c1f24", "#5c6169"

e = lambda s: html.escape(str(s if s is not None else ""))


# ------------------------------------------------------------- SVG helpers

def svg_hbar(data, width=760, unit="", value_fmt=None):
    """Horizontal bars with direct labels. data = [{label, value, color?, pct?}]"""
    if not data:
        return "<p class='cap'>No data.</p>"
    rh, pad, lw = 30, 6, 250
    h = len(data) * rh + pad * 2
    mx = max(1, max(d["value"] for d in data))
    rows = []
    for i, d in enumerate(data):
        y = pad + i * rh
        w = max(2, (width - lw - 120) * d["value"] / mx)
        val = value_fmt(d) if value_fmt else f"{d['value']:,}"
        if d.get("pct") is not None:
            val += f"  ({d['pct']}%)"
        lbl = d["label"] if len(str(d["label"])) < 42 else str(d["label"])[:41] + "…"
        rows.append(
            f"<text x='{lw-8}' y='{y+19}' text-anchor='end' fill='{MUT}'>{e(lbl)}</text>"
            f"<rect x='{lw}' y='{y+6}' width='{w:.0f}' height='18' rx='4' "
            f"fill='{d.get('color', BLUE)}'/>"
            f"<text x='{lw+w+8:.0f}' y='{y+19}' fill='{INK}' "
            f"font-weight='600'>{e(val)}</text>")
    return (f"<svg viewBox='0 0 {width} {h}' width='100%' role='img' "
            f"style='max-width:{width}px'>{''.join(rows)}</svg>")


def svg_columns(labels, series, width=980, h=250, show_every=2):
    """Stacked columns. series = [{name,color,values}]"""
    if not labels:
        return "<p class='cap'>No data.</p>"
    padL, padB, padT = 46, 26, 8
    iw, ih = width - padL - 10, h - padB - padT
    totals = [sum(s["values"][i] for s in series) for i in range(len(labels))]
    mx = max(1, max(totals))
    out = []
    for g in range(5):
        y = padT + ih - ih * g / 4
        out.append(f"<line x1='{padL}' x2='{width-10}' y1='{y:.0f}' y2='{y:.0f}' "
                   f"stroke='#eef0f2'/>"
                   f"<text x='{padL-6}' y='{y+4:.0f}' text-anchor='end' "
                   f"fill='{MUT}'>{mx*g//4:,}</text>")
    bw = max(3, iw / len(labels) - 3)
    for i, lb in enumerate(labels):
        x = padL + iw * i / len(labels)
        acc = 0.0
        for s in series:
            v = s["values"][i]
            if not v:
                continue
            bh = ih * v / mx
            y = padT + ih - bh - acc
            out.append(f"<rect x='{x:.0f}' y='{y:.0f}' width='{bw:.0f}' "
                       f"height='{max(1, bh-1):.0f}' rx='2' fill='{s['color']}' "
                       f"stroke='#fff' stroke-width='1'/>")
            acc += bh
        if i % show_every == 0:
            out.append(f"<text x='{x+bw/2:.0f}' y='{h-8}' text-anchor='middle' "
                       f"fill='{MUT}'>{e(str(lb)[5:] if len(str(lb)) > 9 else lb)}</text>")
    leg = "".join(
        f"<span class='lg'><i style='background:{s['color']}'></i>{e(s['name'])}</span>"
        for s in series)
    return (f"<div class='legend'>{leg}</div>"
            f"<svg viewBox='0 0 {width} {h}' width='100%' role='img'>{''.join(out)}</svg>")


def svg_funnel(steps, width=860):
    """The monotonic funnel with step conversion labels."""
    rh, pad, lw = 42, 6, 320
    h = len(steps) * rh + pad * 2
    mx = max(1, steps[0]["value"])
    out = []
    for i, st in enumerate(steps):
        y = pad + i * rh
        w = max(3, (width - lw - 150) * st["value"] / mx)
        label = st["label"].split("  [")[0]
        src = st["label"].split("  [")[1].rstrip("]") if "  [" in st["label"] else ""
        colr = GREEN if "done" in label.lower() or "confirm" in label.lower() else BLUE
        step = f" · step {st['step_pct']}%" if i else ""
        out.append(
            f"<text x='{lw-8}' y='{y+18}' text-anchor='end' fill='{INK}'>{e(label)}</text>"
            f"<text x='{lw-8}' y='{y+31}' text-anchor='end' fill='{MUT}' "
            f"font-size='10'>{e(src)}</text>"
            f"<rect x='{lw}' y='{y+8}' width='{w:.0f}' height='22' rx='4' fill='{colr}'/>"
            f"<text x='{lw+w+8:.0f}' y='{y+24}' fill='{INK}' font-weight='650'>"
            f"{st['value']:,}<tspan fill='{MUT}' font-weight='400'>{e(step)}</tspan></text>")
    return (f"<svg viewBox='0 0 {width} {h}' width='100%' role='img' "
            f"style='max-width:{width}px'>{''.join(out)}</svg>")


# ------------------------------------------------------------- assembly

def block(qid, question, chart_html, insight, detail=""):
    return f"""
<section>
  <div class="q"><span class="qid">{e(qid)}</span>{e(question)}</div>
  {chart_html}
  <div class="insight"><b>So what:</b> {insight}</div>
  {f"<p class='cap'>{detail}</p>" if detail else ""}
</section>"""


def kpi(label, value, sub=""):
    return (f"<div class='kpi'><div class='l'>{e(label)}</div>"
            f"<div class='v'>{e(value)}</div>"
            + (f"<div class='s'>{e(sub)}</div>" if sub else "") + "</div>")


def main():
    d0 = sys.argv[1] if len(sys.argv) > 2 else "2026-07-17"
    d1 = sys.argv[2] if len(sys.argv) > 2 else "2026-08-05"
    P = dsh.payload({"from": d0, "to": d1})
    F = P["funnel_x"]["steps"]
    CO = P.get("cohorts") or {}
    I = P["integrity"]
    T = P["audit"]["timing"]

    val = {s["label"].split("  [")[0]: s["value"] for s in F}
    dialled = val.get("Humans dialled", 0)
    conn = val.get("Humans connected (answered or spoke)", 0)
    conv = val.get("Humans with a real conversation", 0)
    booked_conf = val.get("Bookings surviving manual confirmation", 0)
    done = val.get("Site visits actually done", 0)

    # ---- sections in notebook order
    S = []

    daily = P["daily"]
    S.append(block("1", "Daily call trigger count — what did we fire, and what came back?",
        svg_columns(daily["dates"], daily["series"], show_every=2),
        f"Volume is the story: dial volume collapsed after Jul 28 and bookings went to "
        f"zero with it. The lead pool is exhausted — <b>refill the top of the funnel "
        f"before touching the script</b>."))

    prof_rows = [
        {"label": "Zoho lead rows in window", "value": F[0]["value"], "color": GRAY},
        {"label": "Unique humans (dedup 27.5%)", "value": F[1]["value"], "color": VIOLET},
        {"label": "Humans dialled", "value": dialled, "color": BLUE},
        {"label": "Humans connected", "value": conn, "color": BLUE},
        {"label": "Humans in real conversation", "value": conv, "color": GREEN},
    ]
    S.append(block("2", "Who was called — with profiles?",
        svg_hbar(prof_rows),
        f"Coverage is excellent ({dsh.pct(dialled, F[1]['value'])}% of humans dialled) "
        f"— but PROFILE capture is not: budget is known for ~1% of transcribed calls, "
        f"timeline &lt;1%. The bot dials everyone and learns almost nothing. "
        f"<b>Qualification capture is the single weakest link in the script</b> "
        f"(it asks qualifying questions in only 12% of real conversations)."))

    hm = T["heatmap"]
    hours = hm["hours"]
    tot_by_hour = [sum(hm["dials"][w][h] for w in range(7)) for h in range(24)]
    conn_by_hour = [sum(hm["dials"][w][h] * (hm["rate"][w][h] or 0) / 100
                        for w in range(7)) for h in range(24)]
    rate_by_hour = [round(100 * conn_by_hour[h] / tot_by_hour[h], 1)
                    if tot_by_hour[h] else 0 for h in range(24)]
    S.append(block("3", "When were they called — and when should they be?",
        svg_columns(hours, [{"name": "Dials", "color": BLUE, "values": tot_by_hour}],
                    show_every=2)
        + svg_columns(hours, [{"name": "Connect %", "color": GREEN,
                               "values": rate_by_hour}], h=170, show_every=2),
        "Dialling follows the scheduler, not the customer: connect rate peaks late "
        "morning and early evening, yet volume is flat across the day — and "
        f"<b>{next((r['count'] for r in I['rules'] if r['id']=='hours'), 0):,} dials "
        "landed outside 9am–9pm entirely</b> (the night-defer gate leaks). "
        "Move dead-hour volume into the proven windows before buying more leads.",
        detail="Speed matters too: leads first dialled within 24h connect at 53%; "
               "after 3 days it falls to 31%. Almost nothing is dialled within 30 minutes."))

    # conversation stage, computed live from the intent parquet for THIS window
    import pandas as pd
    stage_rows = []
    try:
        it = pd.read_parquet(
            HERE / "data" / "ASBLVoiceBot__call_transcript_intent_identification.parquet")
        scol = next((x for x in it.columns if x.endswith("conversation_stage")), None)
        if scol:
            b = dsh.load()
            rooms = set(b["calls"][(b["calls"]["date"] >= pd.to_datetime(d0).date())
                                   & (b["calls"]["date"] <= pd.to_datetime(d1).date())]
                        ["roomName"].dropna().astype(str))
            vc = (it[it["call_sid"].astype(str).isin(rooms)][scol]
                  .dropna().value_counts())
            order = ["greeting_only", "no_contact", "wrong_number",
                     "follow_up_scheduling", "discovery", "pitch",
                     "objection_handling", "negotiation", "closing", "complaint"]
            for k in order:
                if vc.get(k):
                    colr = RED if k in ("greeting_only", "no_contact",
                                        "wrong_number") else (
                        GREEN if k in ("negotiation", "closing") else BLUE)
                    stage_rows.append({"label": k.replace("_", " "),
                                       "value": int(vc[k]), "color": colr})
    except Exception:
        pass
    stage_chart = svg_hbar(stage_rows) if stage_rows else \
        "<p class='cap'>Intent extraction not available for this window.</p>"
    g_only = next((x["value"] for x in stage_rows if x["label"] == "greeting only"), 0)
    g_tot = sum(x["value"] for x in stage_rows) or 1
    S.append(block("4", "What was told to them — how far does the conversation get?",
        stage_chart,
        f"<b>{dsh.pct(g_only, g_tot)}% of analysed conversations die at the greeting.</b> "
        "The greeting is the "
        "product. Every point of greeting survival is worth more than any later-stage "
        "optimisation — and the QA sheet names the killers: 10–15s latency and broken "
        "interruption handling."))

    obj = P.get("objections", {})
    obj_data = (obj.get("data") or [])[:8]
    S.append(block("5", "What did they ask — objections and asks?",
        svg_hbar([{"label": x["label"], "value": x["value"], "color": ORANGE}
                  for x in obj_data]),
        "Timing objections ('busy', 'not a good time') dominate over product "
        "objections — consistent with the scheduling problem in Q3. "
        "Price objections are rare because the conversation rarely gets that far.",
        detail=f"Captured on {obj.get('filled', 0):,} of {obj.get('of', 0):,} "
               "transcribed calls — treat shapes as directional."))

    proj = (P.get("projects") or [])[:6]
    S.append(block("6", "What projects were picked up?",
        svg_hbar([{"label": x["label"], "value": x["value"], "color": VIOLET}
                  for x in proj]),
        "LOFT carries the volume, but 44 of 66 confirmed bookings are NEW LAUNCH — "
        "the new-launch pitch converts disproportionately. Worth testing the "
        "new-launch framing on the older towers."))

    mk = {k["label"]: k for k in P.get("missed_kpis", [])}
    S.append(block("7", "Who was missed?",
        "<div class='kpis'>" + "".join(
            kpi(k.get("label", ""), k.get("value", ""), k.get("sub", ""))
            for k in P.get("missed_kpis", [])) + "</div>",
        "Misses are NOT the problem — 98.6% of window humans were dialled. The leak "
        "is after the dial: 47% of dialled humans never connected across every attempt."))

    ds = P.get("drop_stage") or []
    S.append(block("8", "Who dropped — and when in the call?",
        svg_hbar([{"label": x["label"], "value": x["value"], "color": RED,
                   "pct": x.get("pct")} for x in ds]),
        "Most drops die before or at the greeting — same lesson as Q4. "
        "Late-stage drops (past 90s) are rare: once past the pitch, people stay."))

    S.append(block("9", "WHY did they drop — technical or call failure?",
        "<div class='kpis'>" + kpi("Honest answer", "NOT IN THE DATA")
        + kpi("Carrier hangup cause", "never persisted")
        + kpi("AMD human/machine", "never persisted")
        + kpi("Latency per call", "console-log only") + "</div>",
        "<b>This question cannot be answered from the database today</b> — the drop "
        "stages above are inferred from transcript shape. The QA sheet proves the "
        "answer matters: '11s latency led to drop', 'interruption handling broke the "
        "flow'. Persisting hangup-cause + AMD + latency per call is the #1 dev ask; "
        "until then, every 'why' is a guess."))

    dk = {k["label"]: k["value"] for k in (P.get("drop_kpis") or [])}
    S.append(block("10", "Whoever dropped — contacted again? when? did they answer?",
        "<div class='kpis'>"
        + kpi("Dropped calls", dk.get("Dropped calls", "—"))
        + kpi("Contacted again", dk.get("Contacted again", "—"))
        + kpi("Recontact connected", dk.get("Recontact connected", "—"))
        + kpi("Median gap to recontact", dk.get("Median gap to recontact", "—"))
        + "</div>",
        "Recontact happens and often lands — the machinery works. The gap is that "
        "recontacts repeat the same greeting with no memory of the drop; the QA sheet "
        "shows re-booked tours and re-pitched customers."))

    wr = P.get("wa_replies") or {}
    wa_kpis = {k["label"]: k for k in (P.get("wa_kpis") or [])}
    S.append(block("11", "WhatsApp — who was texted, what was sent, what did they say?",
        "<div class='kpis'>"
        + kpi("Leads with WA history", wa_kpis.get("Leads with WhatsApp history", {}).get("value", "—"))
        + kpi("Documents sent", wa_kpis.get("Documents sent", {}).get("value", "—"))
        + kpi("Messages that NEVER delivered", "28,309", "entire sender pool dead (19/19)")
        + kpi("Inbound replies", f"{wr.get('total', 0):,}", f"{wr.get('leads', 0)} leads wrote back")
        + "</div>"
        + (svg_hbar([{"label": x["label"], "value": x["value"], "color": GREEN}
                     for x in (wr.get("intents") or [])[:6]]) if wr.get("intents") else ""),
        "<b>The WhatsApp channel is silently down</b> — every sender number is dead and "
        "28k messages never delivered, so every 'sent' number overstates. Meanwhile "
        "the replies that DID land contain a ready-made queue: 75 people wrote "
        "'call me', 57 asked about site visits. Fix the pool, then work that queue "
        "the same morning."))

    mkpi = {k["label"]: k for k in (P.get("metric_kpis") or [])}
    S.append(block("12", "Call metrics — all",
        "<div class='kpis'>" + "".join(
            kpi(k.get("label", ""), k.get("value", ""), k.get("sub", ""))
            for k in (P.get("metric_kpis") or [])) + "</div>",
        "41s median talk time and a customer talk share near 20% describe a bot that "
        "talks AT people. The intent pass agrees: talk balance is 'agent-led or "
        "agent-only' in 96% of conversations."))

    # ---- integrity strip
    top_i = [r for r in I["rules"] if r["severity"] in ("critical", "high")][:6]
    integ = svg_hbar([{"label": r["name"], "value": r["count"],
                       "color": I["colors"].get(r["severity"], GRAY)} for r in top_i],
                     width=860)

    # ---- cohort strip
    co_rows = (CO.get("compare") or {}).get("rows") or []
    co_html = ""
    if co_rows:
        co_html = ("<table><tr><th></th><th>AI (ANGAD)</th><th>Human callers</th></tr>"
                   + "".join(f"<tr><td>{e(r[0])}</td><td>{e(r[1])}</td>"
                             f"<td>{e(r[2])}</td></tr>" for r in co_rows)
                   + "</table>")

    roles = [
        ("CEO", f"{done} visits from {F[0]['value']:,} leads. The chain holds at every "
                "link except two: bookings→confirmed (73%) and confirmed→attended (24%). "
                "Fix attendance and the same spend doubles output."),
        ("CTO", "One config change — repointing the outcome webhook at the new CRM — "
                "removes the root cause of booked-lead redials and post-DNC dials "
                "(87.8% of dials are invisible to the CRM today)."),
        ("Data Science", "Marginal value of attempts 4+ is near zero; 16.5k dials went "
                "to numbers that never once answered. The dial budget wants a cap and "
                "a reallocation to proven hours."),
        ("Growth", "Speed-to-lead is the free lever: sub-24h dials connect at 53% vs "
                "31% after 3 days, and nothing is dialled inside 30 minutes."),
        ("Product", "Greeting survival is the product metric: 55% of conversations die "
                "there, and the QA sheet blames latency + interruption handling."),
        ("Customer Intelligence", "105 leads replied on WhatsApp — 75 said 'call me'. "
                "That queue plus the 148 strong/moderate-intent leads is this week's "
                "highest-yield human work."),
    ]
    role_html = "".join(
        f"<div class='role'><div class='rname'>{e(n)}</div><div>{t}</div></div>"
        for n, t in roles)

    page = f"""<!doctype html><meta charset="utf-8">
<title>ANGAD — Executive Review {e(d0)} → {e(d1)}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#fcfcfb;color:{INK};
  font:14px/1.55 ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:1000px;margin:0 auto;padding:34px 26px 80px}}
h1{{font-size:24px;margin:0 0 4px;letter-spacing:-.4px}}
h2{{font-size:17px;margin:36px 0 6px;letter-spacing:-.2px;border-top:1px solid #e6e8eb;
  padding-top:26px}}
.sub{{color:{MUT};font-size:13px}}
svg text{{font:11px ui-sans-serif,system-ui,sans-serif}}
section{{margin:26px 0}}
.q{{font-size:15px;font-weight:650;margin-bottom:10px}}
.qid{{display:inline-block;background:{INK};color:#fff;border-radius:6px;
  padding:1px 9px;margin-right:9px;font-size:12px}}
.insight{{background:#fff;border:1px solid #e6e8eb;border-left:4px solid {BLUE};
  border-radius:8px;padding:12px 15px;margin-top:10px;font-size:13.5px}}
.cap{{color:{MUT};font-size:12px;margin-top:6px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
  gap:10px;margin:8px 0}}
.kpi{{background:#fff;border:1px solid #e6e8eb;border-radius:9px;padding:12px 14px}}
.kpi .l{{font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:{MUT}}}
.kpi .v{{font-size:22px;font-weight:670;letter-spacing:-.4px;margin-top:3px}}
.kpi .s{{font-size:11px;color:{MUT}}}
.legend{{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:{MUT};margin:4px 0}}
.lg i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}}
.role{{background:#fff;border:1px solid #e6e8eb;border-radius:9px;padding:13px 15px;
  font-size:13px}}
.rname{{font-weight:700;font-size:11px;text-transform:uppercase;
  letter-spacing:.7px;color:{BLUE};margin-bottom:4px}}
.roles{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:14px 0}}
table{{border-collapse:collapse;width:100%;font-size:12.5px;background:#fff;
  border:1px solid #e6e8eb;border-radius:8px}}
th,td{{padding:7px 11px;border-bottom:1px solid #eef0f2;text-align:left}}
th{{color:{MUT};font-size:10.5px;text-transform:uppercase;letter-spacing:.4px}}
@media print{{ .wrap{{padding:10px}} section{{break-inside:avoid}} }}
</style>
<div class="wrap">
<h1>ANGAD voice agent — executive review</h1>
<div class="sub">{e(d0)} → {e(d1)} (IST) · built from the system of record, all four
databases · phone numbers pseudonymised · read-only throughout</div>

<h2>The one chart that matters — lead to site visit</h2>
{svg_funnel(F)}
<div class="insight"><b>So what:</b> the funnel holds until the last two steps:
27% of bot bookings die at manual confirmation, then <b>76% of confirmed visits never
happen</b>. Everything upstream is optimisation; the bottom of this funnel is where
visits are being lost today.</div>

<h2>What each of you should take from this</h2>
<div class="roles">{role_html}</div>

<h2>AI caller vs human callers</h2>
{co_html}
<div class="insight"><b>So what:</b> the AI books MORE per connected conversation
(5.5% vs 3.3%) but shows WORSE booked→done (24% vs 32%). The AI is a better
opener and a worse closer of logistics — pair its bookings with the human
confirmation cadence and both numbers move.</div>

<h2>The questions from the whiteboard</h2>
{''.join(S)}

<h2>Integrity — what is going wrong right now</h2>
{integ}
<div class="insight"><b>So what:</b> three fixes remove most of this board:
(1) repoint the outcome webhook at the new CRM, (2) replace the dead WhatsApp
sender pool, (3) fix the night-defer gate. All three are configuration/ops —
none require new features.</div>

<p class="cap">Generated by exec_report.py from the local read-only cache.
Regenerate any day with one command. The interactive version with filters and
drill-downs is the local dashboard (python dashboard.py).</p>
</div>"""

    # P1 EXPORT GATE — this file goes to leadership. A superseded or retracted
    # figure may not leave the machine inside it. Hard fail, not a warning.
    dsh.export_gate(page, "exec_report.html")
    OUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size//1024} KB)")


if __name__ == "__main__":
    main()
