"""
quota_dashboard.py  (self-contained / cloud version)
-----------------------------------------------------
Builds the live UK steel quota dashboard with NO Excel and NO OneDrive needed.
The quota list (category, product, country, annual quota) is baked in below, so
this runs anywhere - including a free GitHub Actions scheduler.

For each order number it pulls the live remaining balance from the HMRC
trade-tariff API and writes public/index.html. Figures are the pure HMRC quota
remaining - they do NOT subtract our open orders or material in transit.

To add/remove a quota or fix a figure at the yearly reset, edit the QUOTAS list.

Requires: requests   ->   pip install requests
"""

import os
import time
import html
from datetime import datetime, date

try:
    from zoneinfo import ZoneInfo
    UK_TZ = ZoneInfo("Europe/London")
except Exception:
    UK_TZ = None

import requests

# --------------------------------------------------------------- settings
OUTPUT_HTML = os.path.join("public", "index.html")
WARN_PCT    = 0.20     # amber below 20% of quarterly base
CRIT_PCT    = 0.10     # red below 10%
API  = "https://www.trade-tariff.service.gov.uk/uk/api/quotas/search"
HEAD = {"Accept": "application/vnd.hmrc.2.0+json"}

# --------------------------------------------------- the quota list (baked in)
# order, category, product, country, annual quota (MT). Quarter base = annual / 4.
QUOTAS = [
    ("058600", "1A",  "Non Alloy and Other Alloy Hot Rolled Sheets and Strips", "EU", 375000),
    ("058601", "1A",  "Non Alloy and Other Alloy Hot Rolled Sheets and Strips", "India", 33456),
    ("058602", "1A",  "Non Alloy and Other Alloy Hot Rolled Sheets and Strips", "Korea (the Republic of)", 8785),
    ("058603", "1A",  "Non Alloy and Other Alloy Hot Rolled Sheets and Strips", "Residual", 49763),
    ("058604", "4",   "Metallic Coated Sheets", "EU", 510273),
    ("058605", "4",   "Metallic Coated Sheets", "India", 125796),
    ("058606", "4",   "Metallic Coated Sheets", "Korea (the Republic of)", 100753),
    ("058607", "4",   "Metallic Coated Sheets", "Vietnam", 174367),
    ("058608", "4",   "Metallic Coated Sheets", "Residual", 100116),
    ("058609", "5",   "Organic Coated Sheets", "EU", 49836),
    ("058610", "5",   "Organic Coated Sheets", "Korea (the Republic of)", 19694),
    ("058611", "5",   "Organic Coated Sheets", "Residual", 5993),
    ("058612", "6",   "Tin Mill Products", "EU", 39795),
    ("058613", "6",   "Tin Mill Products", "Japan", 315),
    ("058614", "6",   "Tin Mill Products", "Korea (the Republic of)", 2534),
    ("058615", "6",   "Tin Mill Products", "Residual", 27151),
    ("058616", "7",   "Non-Alloy and Other Alloy Quarto Plates", "EU", 200868),
    ("058617", "7",   "Non-Alloy and Other Alloy Quarto Plates", "Korea (the Republic of)", 33795),
    ("058618", "7",   "Non-Alloy and Other Alloy Quarto Plates", "United States of America (the)", 766),
    ("058619", "7",   "Non-Alloy and Other Alloy Quarto Plates", "Residual", 14415),
    ("058620", "12A", "Alloy Merchant Bars and Light Sections", "EU", 83558),
    ("058621", "12A", "Alloy Merchant Bars and Light Sections", "Residual", 22342),
    ("058622", "12B", "Non Alloy Merchant Bars and Light Sections", "EU", 47618),
    ("058623", "12B", "Non Alloy Merchant Bars and Light Sections", "Turkey", 18654),
    ("058624", "12B", "Non Alloy Merchant Bars and Light Sections", "Residual", 4540),
    ("058625", "13",  "Rebars", "EU", 149024),
    ("058626", "13",  "Rebars", "Turkey", 50582),
    ("058627", "13",  "Rebars", "Residual", 68374),
    ("058628", "14",  "Stainless Bars and Light Sections", "EU", 16543),
    ("058629", "14",  "Stainless Bars and Light Sections", "United States of America (the)", 1782),
    ("058630", "14",  "Stainless Bars and Light Sections", "Residual", 2360),
    ("058634", "16",  "Non-alloy and other alloy wire rod", "EU", 168471),
    ("058635", "16",  "Non-alloy and other alloy wire rod", "Residual", 10504),
    ("058636", "17",  "Angles, shapes, and sections of iron or non-alloy steel", "EU", 253678),
    ("058637", "17",  "Angles, shapes, and sections of iron or non-alloy steel", "Korea (the Republic of)", 3002),
    ("058638", "17",  "Angles, shapes, and sections of iron or non-alloy steel", "United States of America (the)", 852),
    ("058639", "17",  "Angles, shapes, and sections of iron or non-alloy steel", "Residual", 13230),
    ("058642", "20",  "Gas Pipes", "EU", 17896),
    ("058643", "20",  "Gas Pipes", "India", 8777),
    ("058644", "20",  "Gas Pipes", "Turkey", 29917),
    ("058645", "20",  "Gas Pipes", "Residual", 5008),
    ("058646", "21",  "Hollow Sections", "EU", 35236),
    ("058647", "21",  "Hollow Sections", "Turkey", 99399),
    ("058648", "21",  "Hollow Sections", "Residual", 11498),
]
# ------------------------------------------------------------------------


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
    rows = []
    for order, cat, product, country, annual in QUOTAS:
        row = {"order": order, "cat": cat, "product": product,
               "country": country, "annual": annual, "base": int(annual // 4)}
        try:
            period = pick_period(fetch_periods(order))
            bal = to_tonnes(period) if period else None
            row["balance"] = bal
            row["error"] = None if bal is not None else "no balance returned"
        except Exception as e:
            row["balance"] = None
            row["error"] = str(e)
        if row["balance"] is not None and row["base"]:
            row["pct"] = max(0.0, row["balance"] / row["base"])
        elif row["balance"] is not None:
            row["pct"] = 0.0
        else:
            row["pct"] = None
        rows.append(row)
        time.sleep(0.2)
    return rows


def band(pct):
    if pct is None:
        return "err"
    if pct < CRIT_PCT:
        return "crit"
    if pct < WARN_PCT:
        return "warn"
    return "ok"


def fmt(v, dp=0):
    if v is None:
        return "-"
    return "{:,.{}f}".format(v, dp)


PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>UK Steel Quota Dashboard</title>
<style>
:root {
  --ok:#1a7f37; --ok-bg:#e6f4ea; --warn:#b26a00; --warn-bg:#fff4e0;
  --crit:#c62828; --crit-bg:#fdecea; --ink:#1a1f26; --mut:#5b6572;
  --line:#e3e7ec; --card:#ffffff; --bg:#f4f6f8;
}
* { box-sizing:border-box; }
body { margin:0; font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
  background:var(--bg); color:var(--ink); }
.wrap { max-width:1080px; margin:0 auto; padding:28px 20px 60px; }
header { display:flex; justify-content:space-between; align-items:flex-end;
  flex-wrap:wrap; gap:8px; border-bottom:2px solid var(--ink); padding-bottom:14px; }
h1 { font-size:22px; margin:0; letter-spacing:-.2px; }
.sub { color:var(--mut); font-size:13px; }
.summary { display:flex; gap:12px; margin:20px 0 8px; flex-wrap:wrap; }
.stat { flex:1; min-width:120px; background:var(--card); border:1px solid var(--line);
  border-radius:10px; padding:14px 16px; }
.stat .n { font-size:26px; font-weight:700; }
.stat .l { font-size:12px; color:var(--mut); text-transform:uppercase; letter-spacing:.4px; }
.stat.ok .n { color:var(--ok); } .stat.warn .n { color:var(--warn); } .stat.crit .n { color:var(--crit); }
h2 { font-size:14px; text-transform:uppercase; letter-spacing:.5px; color:var(--mut);
  margin:26px 0 10px; }
.chips { display:flex; flex-wrap:wrap; gap:8px; }
.chip { display:flex; flex-direction:column; gap:2px; border-radius:9px; padding:9px 12px;
  border:1px solid var(--line); min-width:150px; }
.chip.warn { background:var(--warn-bg); border-color:#f0d9ac; }
.chip.crit { background:var(--crit-bg); border-color:#f3c0bb; }
.chip-ctry { font-weight:700; font-size:13px; }
.chip-cat { font-size:11px; color:var(--mut); }
.chip-pct { font-size:20px; font-weight:800; }
.chip.warn .chip-pct { color:var(--warn); } .chip.crit .chip-pct { color:var(--crit); }
.chip-mt { font-size:11px; color:var(--mut); }
.none { color:var(--ok); font-weight:600; }
table { width:100%; border-collapse:collapse; background:var(--card);
  border:1px solid var(--line); border-radius:10px; overflow:hidden; font-size:13px; }
th { text-align:left; padding:9px 12px; font-size:11px; text-transform:uppercase;
  letter-spacing:.4px; color:var(--mut); border-bottom:1px solid var(--line); }
td { padding:8px 12px; border-bottom:1px solid var(--line); }
tr:last-child td { border-bottom:none; }
.grouphead td { background:#eef1f5; font-weight:700; font-size:12px;
  text-transform:uppercase; letter-spacing:.3px; color:var(--ink); }
.num { text-align:right; font-variant-numeric:tabular-nums; }
.strong { font-weight:700; }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--mut); }
.ctry { font-weight:600; }
.barcell { width:150px; }
.bar { position:relative; height:18px; background:#eef1f5; border-radius:5px; overflow:hidden; }
.bar .fill { position:absolute; left:0; top:0; bottom:0; }
.bar.ok .fill { background:var(--ok); } .bar.warn .fill { background:var(--warn); }
.bar.crit .fill { background:var(--crit); } .bar.err .fill { background:#bbb; }
.pctlabel { position:absolute; right:6px; top:1px; font-size:11px; font-weight:700; color:var(--ink); }
.r-crit td { background:#fef7f6; } .r-warn td { background:#fffaf0; }
.errbox { margin-top:20px; background:var(--crit-bg); border:1px solid #f3c0bb;
  border-radius:10px; padding:12px 16px; font-size:13px; }
.errbox ul { margin:6px 0 0; padding-left:18px; }
footer { margin-top:26px; color:var(--mut); font-size:12px; }
</style></head>
<body><div class="wrap">
<header>
  <div><h1>UK Steel Safeguard &mdash; Quota Dashboard</h1>
  <div class="sub">Live quota remaining per order number &middot; quarterly base = annual &divide; 4</div></div>
  <div class="sub">Refreshed<br><strong>%%TS%%</strong></div>
</header>

<div class="summary">
  <div class="stat"><div class="n">%%NROWS%%</div><div class="l">Quotas tracked</div></div>
  <div class="stat ok"><div class="n">%%OK%%</div><div class="l">Healthy (20%+)</div></div>
  <div class="stat warn"><div class="n">%%WARN%%</div><div class="l">Watch (10-20%)</div></div>
  <div class="stat crit"><div class="n">%%CRIT%%</div><div class="l">Critical (under 10%)</div></div>
</div>

<h2>Low-quota alerts</h2>
%%ALERTS%%

<h2>All quotas by category</h2>
<table>
<thead><tr>
  <th>Country / Allocation</th><th>Order</th>
  <th class="num">Q base (MT)</th><th class="num">Quota remaining (MT)</th>
  <th>% of base remaining</th>
</tr></thead>
<tbody>
%%TABLE%%
</tbody></table>
%%ERR%%
<footer>Source: HMRC trade-tariff quota API &middot; refreshed automatically each weekday morning<br>
Figures are the live quota remaining from HMRC only &mdash; they do not include our own open orders or material in transit.</footer>
</div></body></html>"""


def build_html(rows):
    now = datetime.now(UK_TZ) if UK_TZ else datetime.now()
    ts = now.strftime("%A %d %B %Y, %H:%M") + (" UK time" if UK_TZ else " UTC")

    groups = {}
    for r in rows:
        groups.setdefault((r["cat"], r["product"]), []).append(r)

    def bar(r):
        b = band(r["pct"])
        pct = r["pct"]
        width = 0 if pct is None else min(100, pct * 100)
        label = "-" if pct is None else "{:.0f}%".format(pct * 100)
        return ('<div class="bar ' + b + '"><div class="fill" style="width:'
                + "{:.1f}".format(width) + '%"></div>'
                + '<span class="pctlabel">' + label + '</span></div>')

    rows_html = []
    for (cat, prod), grp in groups.items():
        head = html.escape((cat + " - " + prod) if prod else cat)
        rows_html.append('<tr class="grouphead"><td colspan="5">' + head + '</td></tr>')
        for r in grp:
            b = band(r["pct"])
            rows_html.append(
                '<tr class="r-' + b + '">'
                + '<td class="ctry">' + html.escape(r["country"]) + '</td>'
                + '<td class="mono">' + r["order"] + '</td>'
                + '<td class="num">' + fmt(r["base"]) + '</td>'
                + '<td class="num strong">' + fmt(r["balance"], 0) + '</td>'
                + '<td class="barcell">' + bar(r) + '</td>'
                + '</tr>'
            )
    table = "\n".join(rows_html)

    alerts = sorted([r for r in rows if band(r["pct"]) in ("crit", "warn")],
                    key=lambda r: r["pct"])
    if alerts:
        chips = "".join(
            '<div class="chip ' + band(r["pct"]) + '">'
            + '<span class="chip-ctry">' + html.escape(r["country"]) + '</span>'
            + '<span class="chip-cat">' + html.escape(r["cat"]) + '</span>'
            + '<span class="chip-pct">' + "{:.0f}%".format(r["pct"] * 100) + '</span>'
            + '<span class="chip-mt">' + fmt(r["balance"], 0) + ' MT left</span>'
            + '</div>'
            for r in alerts
        )
        alerts_html = '<div class="chips">' + chips + '</div>'
    else:
        alerts_html = '<p class="none">No quotas below 20% - all healthy.</p>'

    errs = [r for r in rows if r["error"]]
    if errs:
        items = "".join('<li>' + r["order"] + ' (' + html.escape(r["country"]) + '): '
                        + html.escape(str(r["error"])) + '</li>' for r in errs)
        err_html = '<div class="errbox"><strong>Could not fetch ' + str(len(errs)) \
                   + ':</strong><ul>' + items + '</ul></div>'
    else:
        err_html = ""

    ok = sum(1 for r in rows if band(r["pct"]) == "ok")
    warn = sum(1 for r in rows if band(r["pct"]) == "warn")
    crit = sum(1 for r in rows if band(r["pct"]) == "crit")

    out = PAGE
    for token, value in (("%%TS%%", ts), ("%%NROWS%%", str(len(rows))),
                         ("%%OK%%", str(ok)), ("%%WARN%%", str(warn)),
                         ("%%CRIT%%", str(crit)), ("%%ALERTS%%", alerts_html),
                         ("%%TABLE%%", table), ("%%ERR%%", err_html)):
        out = out.replace(token, value)
    return out


def main():
    print("Fetching live balances for {} quotas...".format(len(QUOTAS)))
    rows = build_rows()
    os.makedirs(os.path.dirname(OUTPUT_HTML), exist_ok=True)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(build_html(rows))
    ok = sum(1 for r in rows if r["error"] is None)
    print("Done. {}/{} live. Wrote {}".format(ok, len(rows), OUTPUT_HTML))


if __name__ == "__main__":
    main()
