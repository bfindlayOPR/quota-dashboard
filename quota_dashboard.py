"""
Carry-over fix for quota_dashboard.py (UK)

Unused tonnage rolls into the next quarter, so a quota can open a period holding
more than that quarter's allocation. The old pace maths assumed the period
started at `base`, which made consumed come out as zero for any line with
carry-over - no drawdown rate and no projection until enough had been drawn to
fall back below base.

These measure consumption from the period's ACTUAL opening balance, read from
history.json, falling back to `base` when there is no history to read.

THREE EDITS - all at the top level of the file, nothing nested.
"""

# ===========================================================================
# EDIT 1 of 3  -  REPLACE the existing add_pace() with this one
# ===========================================================================

def add_pace(row, qstart, qdays, today):
    """Compute drawdown metrics for a row.

    Uses row['opening'] (the balance this quota period actually started with,
    allocation + any carry-over) when it is present, otherwise falls back to
    row['base'] so behaviour is unchanged before any history exists.
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


# ===========================================================================
# EDIT 2 of 3  -  ADD these two functions (put them just after add_pace)
# ===========================================================================

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

    Call this after the history has loaded. Opening is never taken as less than
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


# ===========================================================================
# EDIT 3 of 3  -  in main(), add ONE line after the history loads
# ===========================================================================
#
#   def main():
#       print("Fetching live EU balances for {} quotas...".format(len(QUOTAS)))
#       rows = build_rows()
#       history = load_prev_history()
#       apply_openings(rows, history)          # <-- NEW
#       ref, ref_week = attach_changes(rows, history)
#       ...rest unchanged...
#
# ===========================================================================
# OPTIONAL - makes a carried-over line obvious on the dashboard
# ===========================================================================
#
# The "% of base remaining" label already shows the true figure (135% etc); only
# the bar graphic stops at full width. To mark those bars with a stripe:
#
# 1. In the <style> block of PAGE, after the .bar rules, add:
#
#      .bar.over .fill{background:repeating-linear-gradient(45deg,
#        var(--ok),var(--ok) 6px,#13632b 6px,#13632b 12px);}
#
# 2. Inside build_html(), in the nested bar() function, change:
#
#      return ('<div class="bar ' + b + '"><div class="fill" style="width:'
#
#    to:
#
#      over = ' over' if (pct is not None and pct > 1.001) else ''
#      return ('<div class="bar ' + b + over + '"><div class="fill" style="width:'
