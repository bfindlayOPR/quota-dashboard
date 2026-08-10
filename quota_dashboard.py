"""
quota_dashboard.py  (self-contained / cloud version) - UK
---------------------------------------------------------
Builds the UK steel quota dashboard + daily/weekly movers + historical trends,
with a full-history CSV export. The quota list (44 order numbers) is baked in.
For each order number it pulls the live remaining balance from the HMRC
trade-tariff API and writes index.html, movers.html, trends.html + history.json.

Requires: requests   ->   pip install requests
"""

import os
import re
import json
import time
import html as htmllib
from datetime import datetime, date, timedelta

try:
    from zoneinfo import ZoneInfo
    UK_TZ = ZoneInfo("Europe/London")
except Exception:
    UK_TZ = None

import requests

# --------------------------------------------------------------- settings
OUTPUT_HTML = os.path.join("public", "index.html")
YEAR        = 2026
WARN_PCT    = 0.20
CRIT_PCT    = 0.10
API  = "https://www.trade-tariff.service.gov.uk/uk/api/quotas/search"
HEAD = {"Accept": "application/vnd.hmrc.2.0+json"}

# where the published dashboard lives (used to read back yesterday's snapshot)
PAGES_BASE   = "https://bfindlayopr.github.io/quota-dashboard/"
HISTORY_URL  = PAGES_BASE + "history.json"
HISTORY_FILE = os.path.join("public", "history.json")
MOVERS_FILE  = os.path.join("public", "movers.html")
TRENDS_FILE  = os.path.join("public", "trends.html")
HISTORY_KEEP_DAYS = 500

CATEGORY_NAMES = {
    "1A": "Non Alloy and Other Alloy Hot Rolled Sheets and Strips",
    "4": "Metallic Coated Sheets",
    "5": "Organic Coated Sheets",
    "6": "Tin Mill Products",
    "7": "Non-Alloy and Other Alloy Quarto Plates",
    "12A": "Alloy Merchant Bars and Light Sections",
    "12B": "Non Alloy Merchant Bars and Light Sections",
    "13": "Rebars",
    "14": "Stainless Bars and Light Sections",
    "16": "Non-alloy and other alloy wire rod",
    "17": "Angles, shapes, and sections of iron or non-alloy steel",
    "20": "Gas Pipes",
    "21": "Hollow Sections",
}

# order, category, origin (country/allocation), quarterly base (MT) = annual / 4
QUOTAS = [
    ("058600", "1A", "EU", 93750),
    ("058601", "1A", "India", 8364),
    ("058602", "1A", "Korea (the Republic of)", 2196),
    ("058603", "1A", "Residual", 12440),
    ("058604", "4", "EU", 127568),
    ("058605", "4", "India", 31449),
    ("058606", "4", "Korea (the Republic of)", 25188),
    ("058607", "4", "Vietnam", 43591),
    ("058608", "4", "Residual", 25029),
    ("058609", "5", "EU", 12459),
    ("058610", "5", "Korea (the Republic of)", 4923),
    ("058611", "5", "Residual", 1498),
    ("058612", "6", "EU", 9948),
    ("058613", "6", "Japan", 78),
    ("058614", "6", "Korea (the Republic of)", 633),
    ("058615", "6", "Residual", 6787),
    ("058616", "7", "EU", 50217),
    ("058617", "7", "Korea (the Republic of)", 8448),
    ("058618", "7", "United States of America (the)", 191),
    ("058619", "7", "Residual", 3603),
    ("058620", "12A", "EU", 20889),
    ("058621", "12A", "Residual", 5585),
    ("058622", "12B", "EU", 11904),
    ("058623", "12B", "Turkey", 4663),
    ("058624", "12B", "Residual", 1135),
    ("058625", "13", "EU", 37256),
    ("058626", "13", "Turkey", 12645),
    ("058627", "13", "Residual", 17093),
    ("058628", "14", "EU", 4135),
    ("058629", "14", "United States of America (the)", 445),
    ("058630", "14", "Residual", 590),
    ("058634", "16", "EU", 42117),
    ("058635", "16", "Residual", 2626),
    ("058636", "17", "EU", 63419),
    ("058637", "17", "Korea (the Republic of)", 750),
    ("058638", "17", "United States of America (the)", 213),
    ("058639", "17", "Residual", 3307),
    ("058642", "20", "EU", 4474),
    ("058643", "20", "India", 2194),
    ("058644", "20", "Turkey", 7479),
    ("058645", "20", "Residual", 1252),
    ("058646", "21", "EU", 8809),
    ("058647", "21", "Turkey", 24849),
    ("058648", "21", "Residual", 2874),
]

# ------------------------------------------------------------------- fetch

def current_quarter_index():
    """EU steel quota year runs Jul->Jun. Returns 0..3 for Q1..Q4."""
    m = date.today().month
    if 7 <= m <= 9:   return 0   # Jul-Sep
    if 10 <= m <= 12: return 1   # Oct-Dec
    if 1 <= m <= 3:   return 2   # Jan-Mar
    return 3                     # Apr-Jun


def quarter_bounds():
    """Return (start_date, end_date, total_days) for the current quota quarter."""
    t = date.today()
    base_year = t.year if t.month >= 7 else t.year - 1
    periods = [
        (date(base_year, 7, 1),   date(base_year, 9, 30)),
        (date(base_year, 10, 1),  date(base_year, 12, 31)),
        (date(base_year + 1, 1, 1), date(base_year + 1, 3, 31)),
        (date(base_year + 1, 4, 1), date(base_year + 1, 6, 30)),
    ]
    s, e = periods[current_quarter_index()]
    return s, e, (e - s).days + 1


def add_pace(row, qstart, qdays, today):
    """Compute drawdown metrics for a row (needs balance + base)."""
    row["pace"] = None
    row["consumed"] = None
    row["daily_rate"] = None
    row["days_left"] = max(0, qdays - ((today - qstart).days + 1))
    row["proj"] = "-"
    if row["balance"] is None or not row["base"]:
        return
    consumed = max(0.0, row["base"] - row["balance"])
    elapsed = min(max((today - qstart).days + 1, 1), qdays)
    row["consumed"] = consumed
    row["elapsed"] = elapsed
    row["qdays"] = qdays
    row["daily_rate"] = consumed / elapsed
    consumed_frac = consumed / row["base"]
    elapsed_frac = elapsed / qdays
    row["pace"] = (consumed_frac / elapsed_frac) if elapsed_frac > 0 else None
    if row["balance"] <= 0:
        row["proj"] = "exhausted"
    elif row["daily_rate"] > 0:
        days_to_go = row["balance"] / row["daily_rate"]
        if days_to_go >= row["days_left"]:
            row["proj"] = "lasts the quarter"
        else:
            from datetime import timedelta
            d = today + timedelta(days=round(days_to_go))
            row["proj"] = "runs out ~" + d.strftime("%d %b")
    else:
        row["proj"] = "no drawdown yet"


def pace_band(row):
    if row.get("pace") is None:
        return "none"
    if row["balance"] is not None and row["balance"] <= 0:
        return "crowded"
    if row["pace"] >= 1.3:
        return "crowded"
    if row["pace"] <= 0.7:
        return "open"
    return "steady"


def fetch_periods(order_number):
    r = requests.get(API,
                     params={"order_number": order_number, "status": "not_blocked"},
                     headers=HEAD, timeout=20)
    r.raise_for_status()
    return [d["attributes"] for d in r.json().get("data", [])]


def covers_today(a):
    s = (a.get("validity_start_date") or "")[:10]
    e = (a.get("validity_end_date") or "")[:10] or "9999-12-31"
    return s <= date.today().isoformat() <= e


def to_tonnes(a):
    raw = a.get("balance")
    if raw is None:
        return None
    val = float(raw)
    if (a.get("measurement_unit") or "").lower().startswith("kilogram"):
        val /= 1000.0
    return round(val, 3)


def pick_period(periods):
    live = [p for p in periods if covers_today(p)]
    if live:
        return live[0]
    openish = [p for p in periods if p.get("status") == "Open"]
    if openish:
        return openish[0]
    return periods[0] if periods else None


def build_rows():
    qstart, qend, qdays = quarter_bounds()
    today = date.today()
    rows = []
    for order, cat, origin, base in QUOTAS:
        row = {"order": order, "cat": cat, "origin": origin,
               "category": CATEGORY_NAMES.get(cat, cat), "base": base}
        try:
            period = pick_period(fetch_periods(order))
            bal = to_tonnes(period) if period else None
            row["balance"] = bal
            row["error"] = None if bal is not None else "no balance returned"
        except Exception as e:
            row["balance"] = None
            row["error"] = str(e)
        if row["balance"] is not None and base:
            row["pct"] = max(0.0, row["balance"] / base)
        elif row["balance"] is not None:
            row["pct"] = 0.0
        else:
            row["pct"] = None
        add_pace(row, qstart, qdays, today)
        rows.append(row)
        time.sleep(0.2)
    return rows


def band(pct):
    if pct is None: return "err"
    if pct < CRIT_PCT: return "crit"
    if pct < WARN_PCT: return "warn"
    return "ok"


def fmt(v, dp=0):
    if v is None: return "-"
    return "{:,.{}f}".format(v, dp)


PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<script>
(function(){
  var p = location.pathname;
  if(location.search.indexOf('fresh')===-1){ location.replace(p+'?fresh='+Date.now()); return; }
  setTimeout(function(){ location.replace(p+'?fresh='+Date.now()); }, 1800000);
})();
</script>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UK Steel Quota Dashboard</title>
<style>
:root{--ok:#1a7f37;--ok-bg:#e6f4ea;--warn:#b26a00;--warn-bg:#fff4e0;--crit:#c62828;--crit-bg:#fdecea;--ink:#1a1f26;--mut:#5b6572;--line:#e3e7ec;--card:#fff;--bg:#f4f6f8;}
*{box-sizing:border-box;}
body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 60px;}
header{display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:8px;border-bottom:2px solid var(--ink);padding-bottom:14px;}
h1{font-size:22px;margin:0;letter-spacing:-.2px;}
.sub{color:var(--mut);font-size:13px;}
.summary{display:flex;gap:12px;margin:20px 0 8px;flex-wrap:wrap;}
.stat{flex:1;min-width:120px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;}
.stat .n{font-size:26px;font-weight:700;}
.stat .l{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px;}
.stat.ok .n{color:var(--ok);}.stat.warn .n{color:var(--warn);}.stat.crit .n{color:var(--crit);}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.5px;color:var(--mut);margin:26px 0 10px;}
.chips{display:flex;flex-wrap:wrap;gap:8px;}
.chip{display:flex;flex-direction:column;gap:2px;border-radius:9px;padding:9px 12px;border:1px solid var(--line);min-width:150px;}
.chip.warn{background:var(--warn-bg);border-color:#f0d9ac;}
.chip.crit{background:var(--crit-bg);border-color:#f3c0bb;}
.chip-ctry{font-weight:700;font-size:13px;}
.chip-cat{font-size:11px;color:var(--mut);}
.chip-pct{font-size:20px;font-weight:800;}
.chip.warn .chip-pct{color:var(--warn);}.chip.crit .chip-pct{color:var(--crit);}
.chip-mt{font-size:11px;color:var(--mut);}
.none{color:var(--ok);font-weight:600;}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13px;}
th{text-align:left;padding:9px 12px;font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--mut);border-bottom:1px solid var(--line);}
td{padding:8px 12px;border-bottom:1px solid var(--line);}
tr:last-child td{border-bottom:none;}
.grouphead td{background:#eef1f5;font-weight:700;font-size:12px;text-transform:uppercase;letter-spacing:.3px;color:var(--ink);}
.num{text-align:right;font-variant-numeric:tabular-nums;}
.strong{font-weight:700;}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--mut);}
.ctry{font-weight:600;}
.barcell{width:150px;}
.bar{position:relative;height:18px;background:#eef1f5;border-radius:5px;overflow:hidden;}
.bar .fill{position:absolute;left:0;top:0;bottom:0;}
.bar.ok .fill{background:var(--ok);}.bar.warn .fill{background:var(--warn);}
.bar.crit .fill{background:var(--crit);}.bar.err .fill{background:#bbb;}
.pctlabel{position:absolute;right:6px;top:1px;font-size:11px;font-weight:700;color:var(--ink);}
.r-crit td{background:#fef7f6;}.r-warn td{background:#fffaf0;}
.errbox{margin-top:20px;background:var(--crit-bg);border:1px solid #f3c0bb;border-radius:10px;padding:12px 16px;font-size:13px;}
.errbox ul{margin:6px 0 0;padding-left:18px;}
footer{margin-top:26px;color:var(--mut);font-size:12px;}
.qrow{cursor:pointer;}
.qrow:hover td{background:#f0f4f9;}
.pace{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:700;white-space:nowrap;}
.pace.crowded{background:var(--crit-bg);color:var(--crit);}
.pace.open{background:var(--ok-bg);color:var(--ok);}
.pace.steady{background:#eef1f5;color:var(--mut);}
.pace.none{color:var(--mut);}
.detail{display:none;}
.detail.show{display:table-row;}
.detail td{background:#f7f9fb;padding:0;border-bottom:1px solid var(--line);}
.dgrid{display:flex;flex-wrap:wrap;gap:18px;padding:12px 16px 14px;font-size:12.5px;}
.dgrid .k{color:var(--mut);text-transform:uppercase;letter-spacing:.3px;font-size:10.5px;}
.dgrid .v{font-weight:700;font-size:14px;}
.legend{font-size:12px;color:var(--mut);margin:2px 0 12px;}
.legend b{color:var(--ink);}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin:0 3px 0 10px;vertical-align:middle;}
.dot.crowded{background:var(--crit);}.dot.open{background:var(--ok);}.dot.steady{background:#9aa4b0;}
.navlink{display:inline-block;margin:14px 0 0;padding:9px 16px;background:var(--ink);color:#fff;border-radius:8px;text-decoration:none;font-size:13px;font-weight:600;}
.navlink:hover{opacity:.9;}
.chg-neg{color:var(--crit);font-weight:700;}
.chg-pos{color:var(--ok);font-weight:700;}
</style></head>
<body><div class="wrap">
<header>
  <div><h1>UK Steel Safeguard &mdash; Quota Dashboard</h1>
  <div class="sub">Live quota remaining per order number &middot; %%QLABEL%%</div></div>
  <div class="sub">Refreshed<br><strong>%%TS%%</strong></div>
</header>
<div class="summary">
  <div class="stat"><div class="n">%%NROWS%%</div><div class="l">Quotas tracked</div></div>
  <div class="stat ok"><div class="n">%%OK%%</div><div class="l">Healthy (20%+)</div></div>
  <div class="stat warn"><div class="n">%%WARN%%</div><div class="l">Watch (10-20%)</div></div>
  <div class="stat crit"><div class="n">%%CRIT%%</div><div class="l">Critical (under 10%)</div></div>
</div>
<a class="navlink" href="movers.html">&#128200; Daily movers &rarr;</a>
<a class="navlink" href="trends.html">&#128201; Historical trends &rarr;</a>
<h2>Low-quota alerts</h2>
%%ALERTS%%
<h2>All quotas by category</h2>
<div class="legend">Click any row for drawdown detail. <b>Pace</b> = how fast it is being used vs how far through the quarter we are:
<span class="dot crowded"></span><b>Crowded</b> (drawn faster than time &mdash; filling up)
<span class="dot steady"></span><b>Steady</b>
<span class="dot open"></span><b>Open</b> (underused &mdash; headroom / opportunity)</div>
<table>
<thead><tr>
  <th>Origin</th><th>Order</th>
  <th class="num">Q base (MT)</th><th class="num">Quota remaining (MT)</th>
  <th>% of base remaining</th><th>Pace</th>
</tr></thead>
<tbody>
%%TABLE%%
</tbody></table>
%%ERR%%
<footer>Source: HMRC trade-tariff quota API &middot; refreshed automatically<br>
Pace &amp; projections assume drawdown continues at the average rate since the quarter opened. Figures are the live quota remaining from HMRC only &mdash; they do not include our own open orders or material in transit.</footer>
</div>
<script>
document.addEventListener('click',function(e){
  var row=e.target.closest('.qrow'); if(!row) return;
  var d=document.getElementById(row.getAttribute('data-t'));
  if(d) d.classList.toggle('show');
});
</script>
</body></html>"""


def build_html(rows):
    now = datetime.now(UK_TZ) if UK_TZ else datetime.now()
    ts = now.strftime("%A %d %B %Y, %H:%M") + (" UK time" if UK_TZ else " UTC")
    qlabels = ["Q1 (1 Jul - 30 Sep)", "Q2 (1 Oct - 31 Dec)",
               "Q3 (1 Jan - 31 Mar)", "Q4 (1 Apr - 30 Jun)"]
    qlabel = "current quarter: " + qlabels[current_quarter_index()]

    groups = {}
    for r in rows:
        groups.setdefault((r["cat"], r["category"]), []).append(r)

    def bar(r):
        b = band(r["pct"]); pct = r["pct"]
        width = 0 if pct is None else min(100, pct * 100)
        label = "-" if pct is None else "{:.0f}%".format(pct * 100)
        return ('<div class="bar ' + b + '"><div class="fill" style="width:'
                + "{:.1f}".format(width) + '%"></div>'
                + '<span class="pctlabel">' + label + '</span></div>')

    def pace_chip(r):
        pb = pace_band(r)
        if r.get("pace") is None:
            return '<span class="pace none">-</span>'
        if r["balance"] is not None and r["balance"] <= 0:
            return '<span class="pace crowded">used up</span>'
        return '<span class="pace ' + pb + '">' + "{:.1f}x".format(r["pace"]) + '</span>'

    def detail_row(r, rid):
        def cell(k, v):
            return '<div><div class="k">' + k + '</div><div class="v">' + v + '</div></div>'
        consumed = fmt(r.get("consumed"), 0) + " t" if r.get("consumed") is not None else "-"
        rate = (fmt(r.get("daily_rate"), 0) + " MT/day") if r.get("daily_rate") is not None else "-"
        qprog = (str(r.get("elapsed", "-")) + " / " + str(r.get("qdays", "-")) + " days") if r.get("elapsed") else "-"
        chg = ""
        if r.get("change") is not None:
            c = r["change"]
            sign = "+" if c > 0 else ""
            label = "Change since " + (r.get("change_ref") or "prev")
            chg = cell(label, sign + fmt(c, 0) + " MT")
        grid = (cell("Consumed so far", consumed + " of " + fmt(r["base"]) + " MT")
                + cell("Avg drawdown", rate)
                + cell("At this rate", htmllib.escape(str(r.get("proj", "-"))))
                + chg
                + cell("Days left in quarter", str(r.get("days_left", "-")))
                + cell("Quarter elapsed", qprog))
        return ('<tr class="detail" id="' + rid + '"><td colspan="6">'
                + '<div class="dgrid">' + grid + '</div></td></tr>')

    rows_html = []
    def catkey(item):
        code = item[0][0]
        mm = re.match(r"(\d+)", code)
        return (int(mm.group(1)) if mm else 99, code)
    for (cat, catname), grp in sorted(groups.items(), key=catkey):
        head = htmllib.escape(cat + " - " + catname)
        rows_html.append('<tr class="grouphead"><td colspan="6">' + head + '</td></tr>')
        for r in grp:
            b = band(r["pct"])
            rid = "d_" + r["order"].replace(".", "_")
            rows_html.append(
                '<tr class="qrow r-' + b + '" data-t="' + rid + '">'
                + '<td class="ctry">' + htmllib.escape(r["origin"] or "-") + '</td>'
                + '<td class="mono">' + r["order"] + '</td>'
                + '<td class="num">' + fmt(r["base"]) + '</td>'
                + '<td class="num strong">' + fmt(r["balance"], 0) + '</td>'
                + '<td class="barcell">' + bar(r) + '</td>'
                + '<td>' + pace_chip(r) + '</td>'
                + '</tr>')
            rows_html.append(detail_row(r, rid))
    table = "\n".join(rows_html)

    alerts = sorted([r for r in rows if band(r["pct"]) in ("crit", "warn")], key=lambda r: r["pct"])
    if alerts:
        chips = "".join(
            '<div class="chip ' + band(r["pct"]) + '">'
            + '<span class="chip-ctry">' + htmllib.escape(r["origin"] or r["order"]) + '</span>'
            + '<span class="chip-cat">Cat ' + htmllib.escape(r["cat"]) + '</span>'
            + '<span class="chip-pct">' + "{:.0f}%".format(r["pct"] * 100) + '</span>'
            + '<span class="chip-mt">' + fmt(r["balance"], 0) + ' MT left</span>'
            + '</div>' for r in alerts)
        alerts_html = '<div class="chips">' + chips + '</div>'
    else:
        alerts_html = '<p class="none">No quotas below 20% - all healthy.</p>'

    errs = [r for r in rows if r["error"]]
    if errs:
        items = "".join('<li>' + r["order"] + ' (cat ' + htmllib.escape(r["cat"]) + '): '
                        + htmllib.escape(str(r["error"])) + '</li>' for r in errs[:40])
        extra = "" if len(errs) <= 40 else "<li>...and " + str(len(errs) - 40) + " more</li>"
        err_html = ('<div class="errbox"><strong>Could not fetch ' + str(len(errs))
                    + ':</strong><ul>' + items + extra + '</ul></div>')
    else:
        err_html = ""

    ok = sum(1 for r in rows if band(r["pct"]) == "ok")
    warn = sum(1 for r in rows if band(r["pct"]) == "warn")
    crit = sum(1 for r in rows if band(r["pct"]) == "crit")

    out = PAGE
    for tok, val in (("%%TS%%", ts), ("%%QLABEL%%", qlabel), ("%%NROWS%%", str(len(rows))),
                     ("%%OK%%", str(ok)), ("%%WARN%%", str(warn)), ("%%CRIT%%", str(crit)),
                     ("%%ALERTS%%", alerts_html), ("%%TABLE%%", table), ("%%ERR%%", err_html)):
        out = out.replace(tok, val)
    return out


def load_prev_history():
    """Read the previously published snapshot log from the live page."""
    try:
        r = requests.get(HISTORY_URL + "?t=" + str(int(time.time())), timeout=30)
        if r.status_code == 200 and r.text.strip():
            return r.json()
    except Exception as e:
        print("history load skipped:", e)
    return {}


def snapshot_ref(history, days_back):
    """Pick the reference snapshot ~days_back days ago (latest on/before that;
    else the earliest logged, so early-on comparisons still work)."""
    todaystr = date.today().isoformat()
    target = (date.today() - timedelta(days=days_back)).isoformat()
    cands = sorted(d for d in history.keys() if d < todaystr)
    if not cands:
        return None
    onbefore = [d for d in cands if d <= target]
    return onbefore[-1] if onbefore else cands[0]


def attach_changes(rows, history):
    """Attach daily and weekly change vs earlier snapshots to each row."""
    ref_d = snapshot_ref(history, 1)
    ref_w = snapshot_ref(history, 7)
    prev_d = history.get(ref_d) if ref_d else None
    prev_w = history.get(ref_w) if ref_w else None
    for r in rows:
        r["change"] = None;      r["change_ref"] = ref_d
        r["change_week"] = None;  r["change_week_ref"] = ref_w
        if r["balance"] is None:
            continue
        if prev_d and prev_d.get(r["order"]) is not None:
            r["change"] = round(r["balance"] - float(prev_d[r["order"]]), 1)
        if prev_w and prev_w.get(r["order"]) is not None:
            r["change_week"] = round(r["balance"] - float(prev_w[r["order"]]), 1)
    return ref_d, ref_w


def save_history(rows, history):
    today = date.today().isoformat()
    history[today] = {r["order"]: r["balance"] for r in rows if r["balance"] is not None}
    for d in sorted(history.keys())[:-HISTORY_KEEP_DAYS]:
        history.pop(d, None)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f)


MOVERS_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<script>
(function(){var p=location.pathname;if(location.search.indexOf('fresh')===-1){location.replace(p+'?fresh='+Date.now());return;}setTimeout(function(){location.replace(p+'?fresh='+Date.now());},1800000);})();
</script>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UK Quota Movers</title>
<style>
:root{--ok:#1a7f37;--crit:#c62828;--ink:#1a1f26;--mut:#5b6572;--line:#e3e7ec;--card:#fff;--bg:#f4f6f8;}
*{box-sizing:border-box;}body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);}
.wrap{max-width:1000px;margin:0 auto;padding:28px 20px 60px;}
header{display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:8px;border-bottom:2px solid var(--ink);padding-bottom:14px;}
h1{font-size:22px;margin:0;}.sub{color:var(--mut);font-size:13px;}
.navlink{display:inline-block;margin:14px 8px 4px 0;padding:9px 16px;background:var(--ink);color:#fff;border-radius:8px;text-decoration:none;font-size:13px;font-weight:600;}
.toggle{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;margin:16px 0 4px;}
.toggle button{border:0;background:#fff;padding:9px 18px;font-size:13px;font-weight:600;cursor:pointer;color:var(--mut);}
.toggle button.on{background:var(--ink);color:#fff;}
.rng{color:var(--mut);font-size:13px;margin:12px 0 0;}
.summary{display:flex;gap:12px;margin:12px 0 8px;flex-wrap:wrap;}
.stat{flex:1;min-width:150px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;}
.stat .n{font-size:24px;font-weight:700;}.stat .l{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px;}
.stat.crit .n{color:var(--crit);}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.5px;color:var(--mut);margin:26px 0 10px;}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13px;}
th{text-align:left;padding:9px 12px;font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--mut);border-bottom:1px solid var(--line);}
td{padding:8px 12px;border-bottom:1px solid var(--line);}tr:last-child td{border-bottom:none;}
.num{text-align:right;font-variant-numeric:tabular-nums;}
.cat{color:var(--mut);font-size:11px;}.ctry{font-weight:600;}.mono{font-family:ui-monospace,Menlo,monospace;color:var(--mut);}
.drop{color:var(--crit);font-weight:700;}.gain{color:var(--ok);font-weight:700;}
.none{color:var(--mut);}footer{margin-top:26px;color:var(--mut);font-size:12px;}
</style></head>
<body><div class="wrap">
<header><div><h1>UK Steel Quota &mdash; Movers</h1>
<div class="sub">Which quotas are being drawn down</div></div>
<div class="sub">Refreshed<br><strong>%%TS%%</strong></div></header>
<a class="navlink" href="index.html">&larr; Dashboard</a>
<a class="navlink" href="trends.html">Historical trends &rarr;</a>
<div class="toggle"><button id="btn-d" class="on" onclick="showv('d')">Since yesterday</button><button id="btn-w" onclick="showv('w')">Last 7 days</button></div>
<div id="view-d">%%DAILY%%</div>
<div id="view-w" style="display:none">%%WEEKLY%%</div>
<footer>Drops = quota consumed (imports cleared); gains = returns/adjustments. Weekly compares against the snapshot around 7 days ago.<br>
Tracking begins from the first snapshot, so figures build up over time. Source: HMRC trade-tariff quota API.</footer>
</div>
<script>function showv(v){document.getElementById('view-d').style.display=(v==='d')?'block':'none';document.getElementById('view-w').style.display=(v==='w')?'block':'none';document.getElementById('btn-d').classList.toggle('on',v==='d');document.getElementById('btn-w').classList.toggle('on',v==='w');}</script>
</body></html>"""


def _movers_view(rows, changekey, refdate, span_label):
    def rowline(r):
        c = r[changekey]
        cls = "drop" if c < 0 else "gain"
        sign = "+" if c > 0 else ""
        return ("<tr><td class='ctry'>" + htmllib.escape(r["origin"] or "-")
                + "<div class='cat'>Cat " + htmllib.escape(r["cat"]) + " &middot; "
                + htmllib.escape(r["category"]) + "</div></td>"
                + "<td class='mono'>" + r["order"] + "</td>"
                + "<td class='num " + cls + "'>" + sign + fmt(c, 0) + "</td>"
                + "<td class='num'>" + fmt(r["balance"], 0) + "</td>"
                + "<td class='num'>" + ("-" if r["pct"] is None else "{:.0f}%".format(r["pct"] * 100)) + "</td></tr>")

    if refdate is None:
        return ("<p class='none'>Not enough history yet for the " + span_label
                + " view &mdash; it appears once an earlier snapshot exists to compare against.</p>")
    moved = [r for r in rows if r.get(changekey) not in (None, 0)]
    drops = sorted([r for r in moved if r[changekey] < 0], key=lambda r: r[changekey])
    gains = sorted([r for r in moved if r[changekey] > 0], key=lambda r: -r[changekey])
    total = -sum(r[changekey] for r in drops) if drops else 0
    big = fmt(-drops[0][changekey], 0) if drops else "0"
    head = ("<p class='rng'>Changes since " + refdate + "</p>"
            "<div class='summary'>"
            "<div class='stat'><div class='n'>" + str(len(moved)) + "</div><div class='l'>Quotas that moved</div></div>"
            "<div class='stat crit'><div class='n'>" + fmt(total, 0) + "</div><div class='l'>Total drawn (MT)</div></div>"
            "<div class='stat crit'><div class='n'>" + big + "</div><div class='l'>Biggest single drop (MT)</div></div>"
            "</div>")
    parts = [head]
    if drops:
        parts.append("<h2>Biggest drawdowns (imports cleared)</h2><table><thead><tr>"
                     "<th>Origin / Category</th><th>Order</th><th class='num'>Change (MT)</th>"
                     "<th class='num'>Now (MT)</th><th class='num'>% left</th></tr></thead><tbody>"
                     + "".join(rowline(r) for r in drops[:60]) + "</tbody></table>")
    if gains:
        parts.append("<h2>Increases (returns / adjustments)</h2><table><thead><tr>"
                     "<th>Origin / Category</th><th>Order</th><th class='num'>Change (MT)</th>"
                     "<th class='num'>Now (MT)</th><th class='num'>% left</th></tr></thead><tbody>"
                     + "".join(rowline(r) for r in gains[:30]) + "</tbody></table>")
    if not drops and not gains:
        parts.append("<p class='none'>No quota movements over this period.</p>")
    return "".join(parts)


def build_movers(rows, ref, ref_week):
    now = datetime.now(UK_TZ) if UK_TZ else datetime.now()
    ts = now.strftime("%A %d %B %Y, %H:%M") + (" UK time" if UK_TZ else " UTC")
    out = MOVERS_PAGE
    out = out.replace("%%TS%%", ts)
    out = out.replace("%%DAILY%%", _movers_view(rows, "change", ref, "daily"))
    out = out.replace("%%WEEKLY%%", _movers_view(rows, "change_week", ref_week, "weekly"))
    return out


def build_trends():
    """Analytics page: reads the daily history log and charts drawdown over time
    per category. All computation is client-side so it enriches as the log grows."""
    meta = {order: {"cat": cat, "name": CATEGORY_NAMES.get(cat, cat), "origin": origin}
            for order, cat, origin, base in QUOTAS}
    meta_json = json.dumps(meta, ensure_ascii=True)
    tmpl = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<script>
(function(){var p=location.pathname;if(location.search.indexOf('fresh')===-1){location.replace(p+'?fresh='+Date.now());return;}})();
</script>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UK Quota Trends</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--ok:#1a7f37;--crit:#c62828;--ink:#1a1f26;--mut:#5b6572;--line:#e3e7ec;--card:#fff;--bg:#f4f6f8;}
*{box-sizing:border-box;}body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 60px;}
header{border-bottom:2px solid var(--ink);padding-bottom:14px;}
h1{font-size:22px;margin:0;}.sub{color:var(--mut);font-size:13px;}
.navlink{display:inline-block;margin:14px 8px 4px 0;padding:9px 16px;background:var(--ink);color:#fff;border-radius:8px;text-decoration:none;font-size:13px;font-weight:600;}
.controls{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin:18px 0;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;}
.controls label{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.3px;margin-right:6px;}
select{font-size:13px;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:#fff;}
.cats{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 18px;}
.catbtn{font-size:12px;padding:5px 10px;border:1px solid var(--line);border-radius:20px;background:#fff;cursor:pointer;user-select:none;}
.catbtn.on{background:var(--ink);color:#fff;border-color:var(--ink);}
.expbtn{padding:9px 16px;border:1px solid var(--ink);border-radius:8px;background:var(--ink);color:#fff;font-size:13px;font-weight:600;cursor:pointer;}
.chartbox{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.5px;color:var(--mut);margin:26px 0 10px;}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13px;}
th{text-align:left;padding:9px 12px;font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--mut);border-bottom:1px solid var(--line);cursor:pointer;}
td{padding:8px 12px;border-bottom:1px solid var(--line);}tr:last-child td{border-bottom:none;}
.num{text-align:right;font-variant-numeric:tabular-nums;}
.up{color:var(--crit);font-weight:700;}.down{color:var(--ok);font-weight:700;}.mut{color:var(--mut);}
.note{color:var(--mut);font-size:13px;margin:8px 0 0;}
footer{margin-top:26px;color:var(--mut);font-size:12px;}
</style></head>
<body><div class="wrap">
<header><h1>UK Steel Quota &mdash; Historical Trends</h1>
<div class="sub">Drawdown over time, built from the daily snapshot log</div></header>
<a class="navlink" href="index.html">&larr; Dashboard</a>
<a class="navlink" href="movers.html">Daily movers</a>
<div id="status" class="note">Loading history&hellip;</div>
<div id="app" style="display:none">
<div class="controls">
  <div><label>Metric</label>
    <select id="metric">
      <option value="cum">Cumulative consumed (MT)</option>
      <option value="rate">Daily drawdown, 7-day avg (MT/day)</option>
      <option value="remain">Remaining balance (MT)</option>
    </select></div>
  <div><label>Range</label>
    <select id="range">
      <option value="0">All time</option>
      <option value="30">Last 30 days</option>
      <option value="90">Last 90 days</option>
    </select></div>
</div>
<div id="cats" class="cats"></div>
<div class="chartbox"><canvas id="chart" height="120"></canvas></div>
<div style="margin:16px 0 4px;"><button class="expbtn" onclick="csvFull()">&#11015; Download full history (CSV)</button>
<span style="font-size:12px;color:var(--mut);margin-left:8px;">every order number's balance and daily drawdown, per day &mdash; opens in Excel / Power BI</span></div>
<h2>By category</h2>
<p class="note">Consumption is the day-over-day fall in remaining balance (imports cleared). Rates build up as more days are logged.</p>
<table id="tbl"><thead><tr>
  <th data-k="cat">Category</th>
  <th class="num" data-k="tot">Total consumed (MT)</th>
  <th class="num" data-k="r7">Rate last 7d (MT/day)</th>
  <th class="num" data-k="rprev">Prev 7d (MT/day)</th>
  <th class="num" data-k="trend">Trend</th>
</tr></thead><tbody></tbody></table>
</div>
<footer>Source: HMRC trade-tariff quota API.<br>
The log starts from the first run, so early history is sparse and fills in day by day.</footer>
</div>
<script>
const META = %%META%%;
const PALETTE = ['#c62828','#1a7f37','#1565c0','#b26a00','#6a1b9a','#00838f','#ad1457','#4e342e','#2e7d32','#283593'];
let HIST=null, DATES=[], DAILY={}, CATS=[], selected=new Set(), chart=null;

fetch('history.json?t='+Date.now()).then(r=>r.ok?r.json():{}).then(h=>{
  HIST=h||{}; DATES=Object.keys(HIST).sort();
  if(DATES.length<2){ document.getElementById('status').innerHTML =
    'Only '+DATES.length+' day(s) logged so far. Trends appear once at least two days are recorded &mdash; check back tomorrow.'; return; }
  document.getElementById('status').style.display='none';
  document.getElementById('app').style.display='block';
  compute(); buildCats(); wire(); render();
});

function compute(){
  // daily consumption per order, then aggregate per category
  DAILY={}; const catset=new Set();
  for(const o in META){ catset.add(META[o].cat); }
  CATS=[...catset].sort((a,b)=>(parseInt(a)||99)-(parseInt(b)||99)||a.localeCompare(b));
  for(const cat of CATS) DAILY[cat]=DATES.map(()=>0);
  for(let i=1;i<DATES.length;i++){
    const prev=HIST[DATES[i-1]], cur=HIST[DATES[i]];
    for(const o in cur){
      if(!(o in META)) continue;
      const pb=prev[o]; if(pb==null) continue;
      const drop=pb-cur[o];
      if(drop>0) DAILY[META[o].cat][i]+=drop;
    }
  }
}
function buildCats(){
  // default: top 6 categories by total consumed
  const totals=CATS.map(c=>[c,DAILY[c].reduce((a,b)=>a+b,0)]).sort((a,b)=>b[1]-a[1]);
  selected=new Set(totals.slice(0,6).filter(t=>t[1]>0).map(t=>t[0]));
  if(selected.size===0) selected=new Set(totals.slice(0,4).map(t=>t[0]));
  const box=document.getElementById('cats'); box.innerHTML='';
  for(const c of CATS){
    const b=document.createElement('span'); b.className='catbtn'+(selected.has(c)?' on':'');
    b.textContent='Cat '+c; b.onclick=()=>{selected.has(c)?selected.delete(c):selected.add(c); b.classList.toggle('on'); render();};
    box.appendChild(b);
  }
}
function wire(){ document.getElementById('metric').onchange=render; document.getElementById('range').onchange=render;
  document.querySelectorAll('#tbl th').forEach(th=>th.onclick=()=>sortTable(th.dataset.k)); }

function slice(){ const n=parseInt(document.getElementById('range').value)||0;
  if(!n||n>=DATES.length) return [0,DATES.length]; return [DATES.length-n,DATES.length]; }
function seriesFor(cat,metric,a,b){
  const d=DAILY[cat];
  if(metric==='cum'){ let s=0; return DATES.slice(a,b).map((_,i)=>{ s+=d[a+i]; return +s.toFixed(1);}); }
  if(metric==='rate'){ return DATES.slice(a,b).map((_,i)=>{ let s=0,n=0; for(let k=Math.max(1,a+i-6);k<=a+i;k++){s+=d[k];n++;} return +(n?s/n:0).toFixed(1);}); }
  // remaining: sum balances of orders in cat
  return DATES.slice(a,b).map((_,i)=>{ const dt=DATES[a+i]; let s=0; for(const o in HIST[dt]){ if(META[o]&&META[o].cat===cat) s+=HIST[dt][o]; } return +s.toFixed(0); });
}
function render(){
  const metric=document.getElementById('metric').value; const [a,b]=slice();
  const labels=DATES.slice(a,b);
  const ds=[...selected].map((c,i)=>({label:'Cat '+c,data:seriesFor(c,metric,a,b),borderColor:PALETTE[i%PALETTE.length],backgroundColor:PALETTE[i%PALETTE.length],tension:.2,pointRadius:0,borderWidth:2}));
  if(chart) chart.destroy();
  chart=new Chart(document.getElementById('chart'),{type:'line',data:{labels,datasets:ds},
    options:{responsive:true,interaction:{mode:'index',intersect:false},plugins:{legend:{position:'bottom'}},scales:{y:{beginAtZero:true}}}});
  buildTable();
}
function rate(cat,fromEnd0,fromEnd1){ // avg daily consumption over window [len-fromEnd1, len-fromEnd0)
  const d=DAILY[cat],L=DATES.length; let s=0,n=0;
  for(let i=Math.max(1,L-fromEnd1);i<L-fromEnd0;i++){s+=d[i];n++;} return n?s/n:0;
}
let sortK='tot',sortDir=-1;
function buildTable(){
  const rows=CATS.map(c=>{ const tot=DAILY[c].reduce((a,b)=>a+b,0); const r7=rate(c,0,7),rp=rate(c,7,14);
    return {cat:c,tot,r7,rprev:rp,trend:r7-rp}; });
  rows.sort((x,y)=>{ let v=(x[sortK]>y[sortK]?1:x[sortK]<y[sortK]?-1:0); return sortK==='cat'?v:v*sortDir; });
  const tb=document.querySelector('#tbl tbody'); tb.innerHTML='';
  for(const r of rows){ const tr=document.createElement('tr');
    const arrow=r.trend>0.5?'<span class="up">&#9650; faster</span>':(r.trend<-0.5?'<span class="down">&#9660; slower</span>':'<span class="mut">&mdash;</span>');
    tr.innerHTML='<td>Cat '+r.cat+' &middot; <span class="mut">'+(META[Object.keys(META).find(o=>META[o].cat===r.cat)]?.name||'')+'</span></td>'
      +'<td class="num">'+Math.round(r.tot).toLocaleString()+'</td>'
      +'<td class="num">'+r.r7.toFixed(0)+'</td><td class="num">'+r.rprev.toFixed(0)+'</td><td class="num">'+arrow+'</td>';
    tb.appendChild(tr); }
}
function csvFull(){
  const rows=[['Date','Order number','Category','Category name','Origin','Remaining (MT)','Consumed that day (MT)']];
  for(let i=0;i<DATES.length;i++){ const dt=DATES[i]; const prev=(i>0)?HIST[DATES[i-1]]:{};
    for(const o in HIST[dt]){ if(!META[o]) continue;
      const cons=(prev[o]!=null)?Math.max(0,prev[o]-HIST[dt][o]):'';
      rows.push([dt,o,META[o].cat,META[o].name,META[o].origin,Math.round(HIST[dt][o]),(cons==='')?'':Math.round(cons)]);
    }
  }
  const csv=rows.map(r=>r.map(c=>{const s=''+c; return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}).join(',')).join('\n');
  const b=new Blob([csv],{type:'text/csv;charset=utf-8;'}); const a=document.createElement('a');
  a.href=URL.createObjectURL(b); a.download='quota_history.csv'; document.body.appendChild(a); a.click(); a.remove();
}
function sortTable(k){ if(sortK===k) sortDir*=-1; else {sortK=k; sortDir=-1;} buildTable(); }
</script>
</body></html>"""
    return tmpl.replace("%%META%%", meta_json)


def main():
    print("Fetching live EU balances for {} quotas...".format(len(QUOTAS)))
    rows = build_rows()
    history = load_prev_history()
    ref, ref_week = attach_changes(rows, history)
    os.makedirs(os.path.dirname(OUTPUT_HTML), exist_ok=True)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(build_html(rows))
    with open(MOVERS_FILE, "w", encoding="utf-8") as f:
        f.write(build_movers(rows, ref, ref_week))
    with open(TRENDS_FILE, "w", encoding="utf-8") as f:
        f.write(build_trends())
    save_history(rows, history)
    ok = sum(1 for r in rows if r["error"] is None)
    print("Done. {}/{} live. ref={}. Wrote index + movers + trends + history.".format(ok, len(rows), ref))


if __name__ == "__main__":
    main()
