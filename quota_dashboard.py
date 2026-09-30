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

QUOTAS = [
    ("058600", "1A", "EU", 93750),("058601", "1A", "India", 8364),("058602", "1A", "Korea (the Republic of)", 2196),("058603", "1A", "Residual", 12440),
    ("058604", "4", "EU", 127568),("058605", "4", "India", 31449),("058606", "4", "Korea (the Republic of)", 25188),("058607", "4", "Vietnam", 43591),("058608", "4", "Residual", 25029),
    ("058609", "5", "EU", 12459),("058610", "5", "Korea (the Republic of)", 4923),("058611", "5", "Residual", 1498),
    ("058612", "6", "EU", 9948),("058613", "6", "Japan", 78),("058614", "6", "Korea (the Republic of)", 633),("058615", "6", "Residual", 6787),
    ("058616", "7", "EU", 50217),("058617", "7", "Korea (the Republic of)", 8448),("058618", "7", "United States of America (the)", 191),("058619", "7", "Residual", 3603),
    ("058620", "12A", "EU", 20889),("058621", "12A", "Residual", 5585),
    ("058622", "12B", "EU", 11904),("058623", "12B", "Turkey", 4663),("058624", "12B", "Residual", 1135),
    ("058625", "13", "EU", 37256),("058626", "13", "Turkey", 12645),("058627", "13", "Residual", 17093),
    ("058628", "14", "EU", 4135),("058629", "14", "United States of America (the)", 445),("058630", "14", "Residual", 590),
    ("058634", "16", "EU", 42117),("058635", "16", "Residual", 2626),
    ("058636", "17", "EU", 63419),("058637", "17", "Korea (the Republic of)", 750),("058638", "17", "United States of America (the)", 213),("058639", "17", "Residual", 3307),
    ("058642", "20", "EU", 4474),("058643", "20", "India", 2194),("058644", "20", "Turkey", 7479),("058645", "20", "Residual", 1252),
    ("058646", "21", "EU", 8809),("058647", "21", "Turkey", 24849),("058648", "21", "Residual", 2874),
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
    """Compute drawdown metrics for a row.

    Uses row['opening'] - the balance this quota period actually started with,
    i.e. the allocation plus any tonnage carried over from the previous quarter -
    when it is present, otherwise falls back to row['base'] so behaviour is
    unchanged before any history exists.
    """
    row["pace"] = None
    row["consumed"] = None
    row["daily_rate"] = None
    row["days_left"] = max(0, qdays - ((today - qstart).days + 1))
    row["proj"] = "-"
    opening = row.get("opening") or row["base"]
    if row["balance"] is None or not opening:
        return
    consumed = max(0.0, opening - row["balance"])
    elapsed = min(max((today - qstart).days + 1, 1), qdays)
    row["consumed"] = consumed
    row["elapsed"] = elapsed
    row["qdays"] = qdays
    row["daily_rate"] = consumed / elapsed
    consumed_frac = consumed / opening
    elapsed_frac = elapsed / qdays
    row["pace"] = (consumed_frac / elapsed_frac) if elapsed_frac > 0 else None
    if row["balance"] <= 0:
        row["proj"] = "exhausted"
    elif row["daily_rate"] > 0:
        days_to_go = row["balance"] / row["daily_rate"]
        if days_to_go >= row["days_left"]:
            row["proj"] = "lasts the quarter"
        else:
            d = today + timedelta(days=round(days_to_go))
            row["proj"] = "runs out ~" + d.strftime("%d %b")
    else:
        row["proj"] = "no drawdown yet"


# ------------------------------------------------- carry-over between quarters
# Unused tonnage rolls into the following quarter, so a quota can open a period
# holding more than that quarter's allocation. Measuring consumption from `base`
# would report zero drawdown for those lines until they fell back below base.

OPENING_WINDOW_DAYS = 7      # how far into the quarter to look for the opening


def period_openings(history, qstart, today):
    """Opening balance per order for the current quota period.

    Reads the highest balance logged in the first few days of the quarter. Taking
    the max rather than the first snapshot means a missed run on day one doesn't
    understate the opening, and carry-over only ever pushes the opening up.
    Orders with nothing logged in that window are left out, so the caller falls
    back to `base`.
    """
    if not history:
        return {}
    window_end = min(qstart + timedelta(days=OPENING_WINDOW_DAYS), today)
    dates = [d for d in sorted(history)
             if qstart.isoformat() <= d <= window_end.isoformat()]
    out = {}
    for d in dates:
        for order, bal in (history[d] or {}).items():
            try:
                v = float(bal)
            except (TypeError, ValueError):
                continue
            if order not in out or v > out[order]:
                out[order] = v
    return out


def apply_openings(rows, history):
    """Attach the period opening balance to each row and recompute pace.

    Called after the history has loaded. Opening is never taken as less than
    `base`, so a quarter with no carry-over behaves exactly as it did before.
    """
    qstart, qend, qdays = quarter_bounds()
    today = date.today()
    openings = period_openings(history, qstart, today)
    carried = 0
    for r in rows:
        logged = openings.get(r["order"])
        base = r.get("base") or 0
        opening = max(base, logged) if logged is not None else base
        r["opening"] = opening
        r["carried"] = round(opening - base, 1) if opening > base else 0
        if r["carried"] > 0:
            carried += 1
        add_pace(r, qstart, qdays, today)
    if carried:
        print("carry-over detected on {} quota line(s); pace measured from the "
              "period opening balance".format(carried))
    return rows


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
:root{--ok:#1a7f37;--ok-bg:#e6f4ea;--warn:#b26a00;--warn-bg:#fff4e0;--crit:#c62828;--crit-bg:#fdecea;--ink:#16294f;--mut:#5b6572;--line:#e3e7ec;--card:#fff;--bg:#f4f6f8;}
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
.bar.over .fill{background:repeating-linear-gradient(45deg,var(--ok),var(--ok) 6px,#13632b 6px,#13632b 12px);}
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
.brandbar{display:flex;align-items:center;justify-content:space-between;gap:12px;background:var(--ink);border-radius:12px;padding:13px 18px;margin-bottom:14px;}
.brandbar img{height:32px;width:auto;}
.brandsub{color:#fff;font-size:13px;font-weight:600;opacity:.9;}
@media (max-width:680px){
  .brandbar{padding:11px 14px;}.brandbar img{height:26px;}.brandsub{font-size:11px;}
  .wrap{padding:16px 12px 50px;}
  h1{font-size:19px;}
  header{align-items:flex-start;}
  .summary{gap:8px;}
  .stat{min-width:calc(50% - 6px);padding:12px;}
  .stat .n{font-size:22px;}
  table{font-size:12px;}
  th,td{padding:7px 8px;}
  th:nth-child(3),td:nth-child(3){display:none;}
  .barcell{width:86px;}
  .chip{min-width:calc(50% - 8px);}
  .navlink{display:block;margin:8px 0;text-align:center;}
  .dgrid{gap:12px;}
}
</style></head>
<body><div class="wrap">
<div class="brandbar"><img src="https://oprgroup.co.uk/wp-content/uploads/2024/01/OPR-LOGO-WHITE.svg" alt="OPR Group"><span class="brandsub">Steel Quota Dashboard</span></div>
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
<span class="dot open"></span><b>Open</b> (underused &mdash; headroom / opportunity)<br>
Unused tonnage carries into the next quarter, so a line can hold more than one quarter's allocation &mdash; those show above 100% with a striped bar.</div>
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
Pace &amp; projections assume drawdown continues at the average rate since the quarter opened, measured from the balance the quarter actually started with. Figures are the live quota remaining from HMRC only &mdash; they do not include our own open orders or material in transit.</footer>
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
        over = " over" if (pct is not None and pct > 1.001) else ""
        return ('<div class="bar ' + b + over + '"><div class="fill" style="width:'
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
        carry = ""
        if r.get("carried"):
            carry = cell("Carried over from last quarter", "+" + fmt(r["carried"], 0) + " MT")
        opened = fmt(r.get("opening") or r["base"], 0) + " MT"
        grid = (cell("Consumed so far", consumed + " of " + opened)
                + cell("Quarter opened with", opened)
                + carry
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
    """Read the snapshot log: committed seed (durable) merged with the live page."""
    hist = {}
    try:
        with open("history_seed.json", encoding="utf-8") as f:
            hist.update(json.load(f))
    except Exception:
        pass
    try:
        r = requests.get(HISTORY_URL + "?t=" + str(int(time.time())), timeout=30)
        if r.status_code == 200 and r.text.strip():
            hist.update(r.json())
    except Exception as e:
        print("history load skipped:", e)
    return hist


def write_backups(history):
    """Durable daily backup: commit history_seed.json (repo) + an Excel-openable
    history.csv (repo root AND public/ so it's also downloadable from the site)."""
    with open("history_seed.json", "w", encoding="utf-8") as f:
        json.dump(history, f)
    meta = {order: (cat, CATEGORY_NAMES.get(cat, cat), origin)
            for order, cat, origin, base in QUOTAS}
    def esc(s):
        s = str(s)
        return '"' + s.replace('"', '""') + '"' if ("," in s or '"' in s) else s
    lines = ["Date,Order number,Category,Category name,Origin,Remaining (MT)"]
    for d in sorted(history):
        for o, bal in history[d].items():
            c, name, og = meta.get(o, ("", "", ""))
            lines.append(",".join([d, o, c, esc(name), esc(og), str(round(bal))]))
    csv_text = "\n".join(lines) + "\n"
    with open("history.csv", "w", encoding="utf-8") as f:
        f.write(csv_text)
    with open(os.path.join("public", "history.csv"), "w", encoding="utf-8") as f:
        f.write(csv_text)
    # 3) Small single-day snapshot (~15KB) for the daily download task.
    header = "Date,Order number,Category,Category name,Origin,Remaining (MT)"
    latest = ""
    if history:
        d = sorted(history)[-1]
        day_lines = [header]
        for o, bal in history[d].items():
            c, name, og = meta.get(o, ("", "", ""))
            day_lines.append(",".join([d, o, c, esc(name), esc(og), str(round(bal))]))
        latest = "\n".join(day_lines) + "\n"
    for path in ("latest.csv", os.path.join("public", "latest.csv")):
        with open(path, "w", encoding="utf-8") as f:
            f.write(latest)


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
    """Attach latest-movement and weekly change vs earlier snapshots to each row."""
    current = {r["order"]: r["balance"] for r in rows if r["balance"] is not None}
    todaystr = date.today().isoformat()
    priors = sorted(d for d in history if d < todaystr)
    ref_d = None
    for d in reversed(priors):
        snap = history[d]
        if any((o in current) and abs(current[o] - float(snap[o])) > 0.001 for o in snap):
            ref_d = d
            break
    if ref_d is None and priors:
        ref_d = priors[-1]
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
:root{--ok:#1a7f37;--crit:#c62828;--ink:#16294f;--mut:#5b6572;--line:#e3e7ec;--card:#fff;--bg:#f4f6f8;}
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
.brandbar{display:flex;align-items:center;justify-content:space-between;gap:12px;background:var(--ink);border-radius:12px;padding:13px 18px;margin-bottom:14px;}
.brandbar img{height:32px;width:auto;}.brandsub{color:#fff;font-size:13px;font-weight:600;opacity:.9;}
.none{color:var(--mut);}footer{margin-top:26px;color:var(--mut);font-size:12px;}
@media (max-width:680px){
  .wrap{padding:16px 12px 50px;}
  .stat{min-width:calc(50% - 6px);}
  table{font-size:12px;}
  th,td{padding:7px 8px;}
  th:nth-child(4),td:nth-child(4){display:none;}
  .navlink{display:block;margin:8px 0;text-align:center;}
  .toggle{display:flex;width:100%;}.toggle button{flex:1;}
}
</style></head>
<body><div class="wrap">
<div class="brandbar"><img src="https://oprgroup.co.uk/wp-content/uploads/2024/01/OPR-LOGO-WHITE.svg" alt="OPR Group"><span class="brandsub">Steel Quota Dashboard</span></div>
<header><div><h1>UK Steel Quota &mdash; Movers</h1>
<div class="sub">Which quotas are being drawn down</div></div>
<div class="sub">Refreshed<br><strong>%%TS%%</strong></div></header>
<a class="navlink" href="index.html">&larr; Dashboard</a>
<a class="navlink" href="trends.html">Historical trends &rarr;</a>
<div class="toggle"><button id="btn-d" class="on" onclick="showv('d')">Latest changes</button><button id="btn-w" onclick="showv('w')">Last 7 days</button></div>
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
    """Analytics page: reads the daily history log and charts drawdown over time.

    Two levels of detail:
      * 'All categories' - one line per category (as before)
      * a single category - one line per quota line inside it, so you can see
        exactly when each origin was exhausted rather than a category total.

    Everything is computed client-side, so the page gets richer as the log grows.
    """
    meta = {order: {"cat": cat, "name": CATEGORY_NAMES.get(cat, cat),
                    "origin": origin, "base": base}
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
:root{--ok:#1a7f37;--crit:#c62828;--ink:#16294f;--mut:#5b6572;--line:#e3e7ec;--card:#fff;--bg:#f4f6f8;}
*{box-sizing:border-box;}body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 60px;}
header{border-bottom:2px solid var(--ink);padding-bottom:14px;}
h1{font-size:22px;margin:0;}.sub{color:var(--mut);font-size:13px;}
.navlink{display:inline-block;margin:14px 8px 4px 0;padding:9px 16px;background:var(--ink);color:#fff;border-radius:8px;text-decoration:none;font-size:13px;font-weight:600;}
.controls{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin:18px 0;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;}
.controls label{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.3px;margin-right:6px;}
select{font-size:13px;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:#fff;}
.cats{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 6px;align-items:center;}
.catbtn{font-size:12px;padding:5px 10px;border:1px solid var(--line);border-radius:20px;background:#fff;cursor:pointer;user-select:none;}
.catbtn.on{background:var(--ink);color:#fff;border-color:var(--ink);}
.catbtn.dead{text-decoration:line-through;opacity:.75;}
.pickall{font-size:12px;color:var(--mut);margin-left:4px;}
.pickall a{color:var(--ink);cursor:pointer;text-decoration:underline;margin:0 4px;}
.expbtn{padding:9px 16px;border:1px solid var(--ink);border-radius:8px;background:var(--ink);color:#fff;font-size:13px;font-weight:600;cursor:pointer;}
.chartbox{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.5px;color:var(--mut);margin:26px 0 10px;}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13px;}
th{text-align:left;padding:9px 12px;font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--mut);border-bottom:1px solid var(--line);cursor:pointer;}
td{padding:8px 12px;border-bottom:1px solid var(--line);}tr:last-child td{border-bottom:none;}
.num{text-align:right;font-variant-numeric:tabular-nums;}
.up{color:var(--crit);font-weight:700;}.down{color:var(--ok);font-weight:700;}.mut{color:var(--mut);}
.exh{color:var(--crit);font-weight:700;}
.note{color:var(--mut);font-size:13px;margin:8px 0 0;}
.swatch{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:7px;vertical-align:middle;}
.brandbar{display:flex;align-items:center;justify-content:space-between;gap:12px;background:var(--ink);border-radius:12px;padding:13px 18px;margin-bottom:14px;}
.brandbar img{height:32px;width:auto;}.brandsub{color:#fff;font-size:13px;font-weight:600;opacity:.9;}
@media (max-width:680px){
  .wrap{padding:16px 12px 50px;}
  .controls{flex-direction:column;align-items:stretch;}
  select{width:100%;}
  table{font-size:12px;}
  th,td{padding:7px 8px;}
  .navlink{display:block;margin:8px 0;text-align:center;}
  .expbtn{width:100%;}
}
footer{margin-top:26px;color:var(--mut);font-size:12px;}
</style></head>
<body><div class="wrap">
<div class="brandbar"><img src="https://oprgroup.co.uk/wp-content/uploads/2024/01/OPR-LOGO-WHITE.svg" alt="OPR Group"><span class="brandsub">Steel Quota Dashboard</span></div>
<header><h1>UK Steel Quota &mdash; Historical Trends</h1>
<div class="sub">Drawdown over time, built from the daily snapshot log</div></header>
<a class="navlink" href="index.html">&larr; Dashboard</a>
<a class="navlink" href="movers.html">Daily movers</a>
<div style="margin:16px 0 4px;"><button class="expbtn" onclick="csvFull()">&#11015; Download full history (CSV)</button>
<span style="font-size:12px;color:var(--mut);margin-left:8px;">every order number's balance and daily drawdown, per day &mdash; opens in Excel / Power BI</span></div>
<div id="status" class="note">Loading history&hellip;</div>
<div id="app" style="display:none">
<div class="controls">
  <div><label>Detail</label>
    <select id="catsel"></select></div>
  <div><label>Metric</label>
    <select id="metric">
      <option value="remain">Remaining balance (MT)</option>
      <option value="pct">Remaining (% of base)</option>
      <option value="cum">Cumulative consumed (MT)</option>
      <option value="rate">Daily drawdown, 7-day avg (MT/day)</option>
    </select></div>
  <div><label>Range</label>
    <select id="range">
      <option value="0">All time</option>
      <option value="30">Last 30 days</option>
      <option value="90">Last 90 days</option>
      <option value="q1">Q1 &middot; Jul&ndash;Sep</option>
      <option value="q2">Q2 &middot; Oct&ndash;Dec</option>
      <option value="q3">Q3 &middot; Jan&ndash;Mar</option>
      <option value="q4">Q4 &middot; Apr&ndash;Jun</option>
    </select></div>
</div>
<div id="cats" class="cats"></div>
<div id="pickall" class="pickall"></div>
<div class="chartbox" style="margin-top:10px;"><canvas id="chart" height="120"></canvas></div>
<div id="exhbox"></div>
<h2 id="tblhead">By category</h2>
<p class="note">Consumption is the day-over-day fall in remaining balance (imports cleared). Rates build up as more days are logged.
Unused tonnage carries into the following quarter, so a line can open above 100% of that quarter's allocation.</p>
<table id="tbl"><thead><tr>
  <th data-k="cat">Category</th>
  <th class="num" data-k="tot">Total consumed (MT)</th>
  <th class="num" data-k="r7">Rate last 7d (MT/day)</th>
  <th class="num" data-k="rprev">Prev 7d (MT/day)</th>
  <th class="num" data-k="trend">Trend</th>
</tr></thead><tbody></tbody></table>
</div>
<footer>Source: HMRC trade-tariff quota API.<br>
The log starts from the first run, so early history is sparse and fills in day by day.
Lines break at quarter boundaries, where quotas reset to a fresh allocation.</footer>
</div>
<script>
const META = %%META%%;
const PALETTE = ['#c62828','#1a7f37','#1565c0','#b26a00','#6a1b9a','#00838f','#ad1457','#4e342e','#2e7d32','#283593','#795548','#0277bd'];
let HIST=null, DATES=[], DAILY={}, DAILY_O={}, CATS=[], ORDERS_BY_CAT={},
    selected=new Set(), mode='', chart=null, colourOf={};

fetch('history.json?t='+Date.now()).then(r=>r.ok?r.json():{}).then(h=>{
  HIST=h||{}; DATES=Object.keys(HIST).sort();
  if(DATES.length<2){ document.getElementById('status').innerHTML =
    'Only '+DATES.length+' day(s) logged so far. Trends appear once at least two days are recorded &mdash; check back tomorrow.'; return; }
  document.getElementById('status').style.display='none';
  document.getElementById('app').style.display='block';
  compute(); buildCatSelect(); buildPicker(); wire(); render();
});

/* ---------- data prep ---------- */
function compute(){
  DAILY={}; DAILY_O={}; const catset=new Set(); ORDERS_BY_CAT={};
  for(const o in META){
    catset.add(META[o].cat);
    (ORDERS_BY_CAT[META[o].cat] = ORDERS_BY_CAT[META[o].cat] || []).push(o);
    DAILY_O[o]=DATES.map(()=>0);
  }
  CATS=[...catset].sort((a,b)=>(parseInt(a)||99)-(parseInt(b)||99)||a.localeCompare(b));
  for(const c of CATS){ DAILY[c]=DATES.map(()=>0); ORDERS_BY_CAT[c].sort((x,y)=>
      (META[x].origin||'').localeCompare(META[y].origin||'')); }
  for(let i=1;i<DATES.length;i++){
    const prev=HIST[DATES[i-1]], cur=HIST[DATES[i]];
    for(const o in cur){
      if(!(o in META)) continue;
      const pb=prev[o]; if(pb==null) continue;
      const drop=pb-cur[o];
      if(drop>0){ DAILY[META[o].cat][i]+=drop; DAILY_O[o][i]+=drop; }
    }
  }
}
function keyList(){ return mode==='' ? CATS : (ORDERS_BY_CAT[mode]||[]); }
function keyLabel(k){ return mode==='' ? ('Cat '+k) : (META[k].origin||k); }
function dailyArr(k){ return mode==='' ? DAILY[k] : DAILY_O[k]; }
function baseFor(k){
  if(mode!=='') return META[k].base||0;
  let s=0; for(const o of (ORDERS_BY_CAT[k]||[])) s+=(META[o].base||0); return s;
}
function remainAt(k,dt){
  const snap=HIST[dt]||{};
  if(mode!==''){ return (snap[k]==null)?null:snap[k]; }
  let s=null;
  for(const o of (ORDERS_BY_CAT[k]||[])){ if(snap[o]!=null){ s=(s||0)+snap[o]; } }
  return s;
}
/* A balance that jumps UP is a new quota period, not a return - break the line
   there so an exhausted quarter doesn't appear to recover. */
function isReset(k,i){
  if(i<1) return false;
  const a=remainAt(k,DATES[i-1]), b=remainAt(k,DATES[i]);
  if(a==null||b==null) return false;
  return b > a*1.05 && b-a > 1;
}

/* ---------- controls ---------- */
function buildCatSelect(){
  const sel=document.getElementById('catsel');
  sel.innerHTML='<option value="">All categories (totals)</option>'
    + CATS.map(c=>'<option value="'+c+'">Cat '+c+' &middot; '+(META[ORDERS_BY_CAT[c][0]].name||'')+'</option>').join('');
  sel.value=mode;
}
function buildPicker(){
  const keys=keyList();
  if(mode===''){
    const totals=keys.map(c=>[c,DAILY[c].reduce((a,b)=>a+b,0)]).sort((a,b)=>b[1]-a[1]);
    selected=new Set(totals.slice(0,6).filter(t=>t[1]>0).map(t=>t[0]));
    if(selected.size===0) selected=new Set(totals.slice(0,4).map(t=>t[0]));
  } else {
    selected=new Set(keys);          // all lines in the category, on by default
  }
  colourOf={}; keys.forEach((k,i)=>colourOf[k]=PALETTE[i%PALETTE.length]);
  const box=document.getElementById('cats'); box.innerHTML='';
  for(const k of keys){
    const b=document.createElement('span');
    const dead = isDead(k);
    b.className='catbtn'+(selected.has(k)?' on':'')+(dead?' dead':'');
    b.innerHTML='<span class="swatch" style="background:'+colourOf[k]+'"></span>'+keyLabel(k);
    b.title = mode==='' ? '' : ('Order '+k);
    b.onclick=()=>{ selected.has(k)?selected.delete(k):selected.add(k);
                    b.classList.toggle('on'); counter(); render(); };
    box.appendChild(b);
  }
  counter();
}
function counter(){
  const keys=keyList();
  document.getElementById('pickall').innerHTML = keys.length>1
    ? 'Showing '+selected.size+' of '+keys.length+' &middot; <a onclick="pick(1)">all</a>|<a onclick="pick(0)">none</a>'
    : '';
}
function pick(on){ const keys=keyList();
  selected = on ? new Set(keys) : new Set();
  document.querySelectorAll('#cats .catbtn').forEach(b=>b.classList.toggle('on',on===1));
  counter(); render(); }
function wire(){
  document.getElementById('metric').onchange=render;
  document.getElementById('range').onchange=render;
  document.getElementById('catsel').onchange=function(){ mode=this.value; buildPicker(); render(); };
  document.querySelectorAll('#tbl th').forEach(th=>th.onclick=()=>sortTable(th.dataset.k));
}

/* ---------- range ---------- */
function qbounds(q){ const t=new Date(); const y=(t.getMonth()>=6)?t.getFullYear():t.getFullYear()-1;
  const R={q1:[y+'-07-01',y+'-09-30'],q2:[y+'-10-01',y+'-12-31'],
           q3:[(y+1)+'-01-01',(y+1)+'-03-31'],q4:[(y+1)+'-04-01',(y+1)+'-06-30']};
  return R[q]; }
function slice(){ const v=document.getElementById('range').value;
  if(v[0]==='q'){ const qb=qbounds(v); let a=DATES.findIndex(d=>d>=qb[0]);
    if(a<0) return [DATES.length,DATES.length];
    let b=a; while(b<DATES.length && DATES[b]<=qb[1]) b++; return [a,b]; }
  const n=parseInt(v)||0; if(!n||n>=DATES.length) return [0,DATES.length]; return [DATES.length-n,DATES.length]; }

/* ---------- series ---------- */
/* Split the window into quota periods. A reset (balance jumping up) starts a new
   one, so each quarter is drawn as its own segment - no day is lost at the join
   and an exhausted quarter never appears to recover. */
function segmentsFor(k,a,b){
  const segs=[]; let start=a;
  for(let i=a+1;i<b;i++){ if(isReset(k,i)){ segs.push([start,i]); start=i; } }
  if(start<b) segs.push([start,b]);
  return segs;
}
function valueAt(k,metric,i,segStart){
  const d=dailyArr(k);
  if(metric==='cum'){ let s=0; for(let q=segStart;q<=i;q++) s+=d[q]; return +s.toFixed(1); }
  if(metric==='rate'){ let s=0,n=0; for(let q=Math.max(1,i-6);q<=i;q++){s+=d[q];n++;} return +(n?s/n:0).toFixed(1); }
  const v=remainAt(k,DATES[i]);
  if(v==null) return null;
  if(metric==='pct'){ const bs=baseFor(k)||1; return +Math.max(0,v/bs*100).toFixed(1); }
  /* no upper clamp on pct: unused tonnage carries into the next quarter, so an
     opening balance of allocation + carry-over reads above 100% */
  return +v.toFixed(0);
}

/* Every point where this key first hit zero, one per quota period. A quarter
   reset starts a new period, so a line exhausted in September still shows its
   September date even once October data exists. */
function exhaustIndices(k){
  const out=[]; let zeroed=false;
  for(let i=0;i<DATES.length;i++){
    if(isReset(k,i)){ zeroed=false; continue; }
    const v=remainAt(k,DATES[i]);
    if(v==null) continue;
    if(v<=0){ if(!zeroed){ out.push(i); zeroed=true; } }
    else zeroed=false;
  }
  return out;
}
/* is it sitting at zero right now? (for the struck-through chip) */
function isDead(k){
  for(let i=DATES.length-1;i>=0;i--){ const v=remainAt(k,DATES[i]); if(v!=null) return v<=0; }
  return false;
}

/* dashed line at 100% - one full quarterly allocation. Anything above it is
   carry-over from the previous quarter sitting on top of the new grant. */
const fullLine={
  id:'fullLine',
  beforeDatasetsDraw(c,args,opts){
    if(opts.metric!=='pct') return;
    const y=c.scales.y, x=c.scales.x;
    if(100>y.max || 100<y.min) return;
    const py=y.getPixelForValue(100), ctx=c.ctx;
    ctx.save();
    ctx.strokeStyle='#9aa4b0'; ctx.lineWidth=1; ctx.setLineDash([5,4]);
    ctx.beginPath(); ctx.moveTo(x.left,py); ctx.lineTo(x.right,py); ctx.stroke();
    ctx.setLineDash([]);
    ctx.font='600 10px -apple-system,Segoe UI,Roboto,sans-serif';
    ctx.fillStyle='#5b6572'; ctx.textAlign='left';
    ctx.fillText('100% = one quarter\u2019s allocation', x.left+6, py-4);
    ctx.restore();
  }
};

/* label the exhaustion point on the chart */
const exhLabels={
  id:'exhLabels',
  afterDatasetsDraw(c){
    const ctx=c.ctx;
    c.data.datasets.forEach((ds,di)=>{
      if(!ds._marks || !ds._marks.length) return;
      const meta=c.getDatasetMeta(di);
      ctx.save();
      ctx.font='600 11px -apple-system,Segoe UI,Roboto,sans-serif';
      ctx.fillStyle=ds.borderColor; ctx.textAlign='center';
      for(const m of ds._marks){
        const pt=meta.data[m.at]; if(!pt) continue;
        ctx.fillText('exhausted '+m.label, pt.x, pt.y-10);
      }
      ctx.restore();
    });
  }
};

function render(){
  const metric=document.getElementById('metric').value; const [a,b]=slice();
  const labels=DATES.slice(a,b);
  const ds=[];
  for(const k of selected){
    const col=colourOf[k]||PALETTE[0];
    const marks=(metric==='remain'||metric==='pct')
      ? exhaustIndices(k).filter(i=>i>=a&&i<b) : [];
    segmentsFor(k,a,b).forEach((seg,si)=>{
      const [s0,s1]=seg;
      const data=new Array(b-a).fill(null);
      for(let i=s0;i<s1;i++) data[i-a]=valueAt(k,metric,i,s0);
      const segMarks=marks.filter(i=>i>=s0&&i<s1)
                          .map(i=>({at:i-a,label:fmtDate(DATES[i])}));
      const markSet=new Set(segMarks.map(m=>m.at));
      ds.push({label:keyLabel(k),data,borderColor:col,backgroundColor:col,tension:.2,
               pointRadius:data.map((_,i)=>markSet.has(i)?5:0),pointHoverRadius:4,
               borderWidth:2,spanGaps:false,_marks:segMarks,_first:si===0});
    });
  }
  if(chart) chart.destroy();
  chart=new Chart(document.getElementById('chart'),{type:'line',data:{labels,datasets:ds},
    plugins:[exhLabels,fullLine],
    options:{responsive:true,interaction:{mode:'index',intersect:false},
      plugins:{legend:{position:'bottom',labels:{filter:(it,d)=>d.datasets[it.datasetIndex]._first}},
        fullLine:{metric:metric},
        tooltip:{filter:(c)=>c.parsed.y!=null,
          callbacks:{label:(c)=>c.dataset.label+': '
            +(c.parsed.y==null?'-':c.parsed.y.toLocaleString()+(metric==='pct'?'%':''))}}},
      scales:{y:{beginAtZero:true,
        ticks:{callback:(v)=>metric==='pct'?v+'%':v.toLocaleString()}}}}});
  buildExhBox();
  buildTable();
}
function fmtDate(iso){ const d=new Date(iso+'T00:00:00');
  return d.toLocaleDateString('en-GB',{day:'numeric',month:'short'}); }

/* summary of exhausted lines under the chart */
function buildExhBox(){
  const box=document.getElementById('exhbox');
  if(mode===''){ box.innerHTML=''; return; }
  const keys=keyList().filter(k=>selected.has(k));
  const done=[];
  for(const k of keys){ for(const i of exhaustIndices(k)) done.push({k,when:DATES[i]}); }
  if(!done.length){ box.innerHTML='<p class="note">None of the selected lines have been exhausted in the logged period.</p>'; return; }
  box.innerHTML='<h2>Exhausted</h2><table><thead><tr><th>Origin</th><th>Order</th>'
    +'<th class="num">Base (MT)</th><th>First recorded at zero</th></tr></thead><tbody>'
    + done.sort((x,y)=>x.when<y.when?-1:1).map(r=>
        '<tr><td><span class="swatch" style="background:'+colourOf[r.k]+'"></span>'
        + (META[r.k].origin||'') + '</td><td class="mut">'+r.k+'</td>'
        + '<td class="num">'+(META[r.k].base||0).toLocaleString()+'</td>'
        + '<td class="exh">'+r.when+'</td></tr>').join('')
    + '</tbody></table>'
    + '<p class="note">Date the snapshot first showed zero, so the true exhaustion is on or shortly before it.</p>';
}

/* ---------- table ---------- */
function rateFor(k,fromEnd0,fromEnd1){
  const d=dailyArr(k),L=DATES.length; let s=0,n=0;
  for(let i=Math.max(1,L-fromEnd1);i<L-fromEnd0;i++){s+=d[i];n++;} return n?s/n:0;
}
let sortK='tot',sortDir=-1;
function buildTable(){
  const [sa,sb]=slice();
  const keys=keyList();
  document.getElementById('tblhead').textContent = mode==='' ? 'By category' : ('Cat '+mode+' by line');
  document.querySelector('#tbl th[data-k="cat"]').textContent = mode==='' ? 'Category' : 'Origin';
  const rows=keys.map(k=>{ const tot=dailyArr(k).slice(sa,sb).reduce((x,y)=>x+y,0);
    const r7=rateFor(k,0,7),rp=rateFor(k,7,14);
    return {cat:k,tot,r7,rprev:rp,trend:r7-rp}; });
  rows.sort((x,y)=>{ let v=(x[sortK]>y[sortK]?1:x[sortK]<y[sortK]?-1:0); return sortK==='cat'?v:v*sortDir; });
  const tb=document.querySelector('#tbl tbody'); tb.innerHTML='';
  for(const r of rows){ const tr=document.createElement('tr');
    const arrow=r.trend>0.5?'<span class="up">&#9650; faster</span>':(r.trend<-0.5?'<span class="down">&#9660; slower</span>':'<span class="mut">&mdash;</span>');
    const name = mode==='' ? ('Cat '+r.cat+' &middot; <span class="mut">'+(META[ORDERS_BY_CAT[r.cat][0]].name||'')+'</span>')
                           : ((META[r.cat].origin||'')+' &middot; <span class="mut">'+r.cat+'</span>');
    tr.innerHTML='<td>'+name+'</td>'
      +'<td class="num">'+Math.round(r.tot).toLocaleString()+'</td>'
      +'<td class="num">'+r.r7.toFixed(0)+'</td><td class="num">'+r.rprev.toFixed(0)+'</td><td class="num">'+arrow+'</td>';
    tb.appendChild(tr); }
}
function sortTable(k){ if(sortK===k) sortDir*=-1; else {sortK=k; sortDir=-1;} buildTable(); }

/* ---------- export ---------- */
function csvFull(){
  if(!HIST || DATES.length===0){ alert('No history logged yet - it fills in from the next scheduled run.'); return; }
  const rows=[['Date','Order number','Category','Category name','Origin','Remaining (MT)','Consumed that day (MT)']];
  for(let i=0;i<DATES.length;i++){ const dt=DATES[i]; const prev=(i>0)?HIST[DATES[i-1]]:{};
    for(const o in HIST[dt]){ if(!META[o]) continue;
      const cons=(prev[o]!=null)?Math.max(0,prev[o]-HIST[dt][o]):'';
      rows.push([dt,o,META[o].cat,META[o].name,META[o].origin,Math.round(HIST[dt][o]),(cons==='')?'':Math.round(cons)]);
    }
  }
  const csv=rows.map(r=>r.map(c=>{const s=''+c; return /[",\\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}).join(',')).join('\\n');
  const b=new Blob([csv],{type:'text/csv;charset=utf-8;'}); const a=document.createElement('a');
  a.href=URL.createObjectURL(b); a.download='quota_history.csv'; document.body.appendChild(a); a.click(); a.remove();
}
</script>
</body></html>"""
    return tmpl.replace("%%META%%", meta_json)


def main():
    print("Fetching live UK balances for {} quotas...".format(len(QUOTAS)))
    rows = build_rows()
    history = load_prev_history()
    apply_openings(rows, history)
    ref, ref_week = attach_changes(rows, history)
    os.makedirs(os.path.dirname(OUTPUT_HTML), exist_ok=True)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(build_html(rows))
    with open(MOVERS_FILE, "w", encoding="utf-8") as f:
        f.write(build_movers(rows, ref, ref_week))
    with open(TRENDS_FILE, "w", encoding="utf-8") as f:
        f.write(build_trends())
    save_history(rows, history)
    write_backups(history)
    ok = sum(1 for r in rows if r["error"] is None)
    print("Done. {}/{} live. ref={}. Wrote index + movers + trends + history.".format(ok, len(rows), ref))


if __name__ == "__main__":
    main()
