#!/usr/bin/env python3
"""Build (or rebuild) the charts on the Expense Tracker's Dashboard tab.

Run it once. Every number is a live formula over the source tabs (Swiggy, Instamart, Fuel, …
from assets/config.json), so the dashboard follows new rows on its own; this is not a sync step.
Re-run only to change the layout or after adding a source to config.json. A re-run replaces
the charts it made before and keeps any chart you added yourself.

  python3 scripts/build_dashboard.py            # build
  python3 scripts/build_dashboard.py --dry-run  # print the Sheets API requests, change nothing

What it writes:
  _data      hidden helper tab: one normalised row per item from all source tabs + summary tables
  Dashboard  title, KPI tiles (rows 1-7) and charts below them
The source tabs are only read by formulas, never written (a missing one is created empty).
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "assets" / "config.json"
DASHBOARD, DATA_TAB = "Dashboard", "_data"
NEW_IDS = iter(range(770001, 770100))
ROWS = 20000  # ~20 years of orders at the current pace; source tabs are read open-ended (A2:N)

# Colours follow the entity in every chart (validated: adjacent CVD dE >= 9.1, normal-vision >= 22.9).
SOURCE_COLORS = {"swiggy": "#eb6834", "instamart": "#2a78d6", "fuel": "#eda100"}
TOTAL_COLOR = "#1baf7a"
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
INK, INK_MUTED, SURFACE = "#0b0b0b", "#52514e", "#ffffff"
RUPEE = "₹#,##0"


# ---------- A1 helpers ----------

def col(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def letter(i):
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def cell(a1):
    letters = "".join(c for c in a1 if c.isalpha())
    return int(a1[len(letters):]) - 1, col(letters)


def q(tab):
    return tab if tab.isalnum() else "'" + tab.replace("'", "''") + "'"


class Layout:
    """Hands out _data columns left to right, one gap column between tables."""

    def __init__(self, first="O"):
        self.next = col(first)

    def take(self, width):
        start = self.next
        self.next += width + 1
        return [letter(start + i) for i in range(width)]


# ---------- formulas ----------
# _data A:L, one row per item from every source tab.
# A date-time (order_datetime may be a real date or "yyyy-mm-dd hh:mm:ss" text; both handled)
# B month  C source  D merchant  E item  F category  G amount  H order_id  I week start
# J hour  K weekday  L day          M category folded to top 7 + "other"
MAIN_HEADERS = ["date", "month", "source", "merchant", "item", "category", "amount", "order_id",
                "week", "hour", "weekday", "day"]


def main_formula(tabs):
    return (
        "=LET(d, VSTACK(" + ", ".join(f"{q(t)}!A2:N" for t in tabs) + "),"
        " k, FILTER(d, CHOOSECOLS(d, 2) <> \"\"),"
        " dt, MAP(CHOOSECOLS(k, 1), LAMBDA(x, IF(ISNUMBER(x), x, DATEVALUE(LEFT(x, 10)) + TIMEVALUE(MID(x, 12, 8))))),"
        " HSTACK(dt,"
        " MAP(dt, LAMBDA(x, TEXT(x, \"yyyy-mm\"))),"
        " CHOOSECOLS(k, 11), CHOOSECOLS(k, 3), CHOOSECOLS(k, 5), CHOOSECOLS(k, 6), CHOOSECOLS(k, 9), CHOOSECOLS(k, 2),"
        " MAP(dt, LAMBDA(x, INT(x) - WEEKDAY(x, 3))),"
        " MAP(dt, LAMBDA(x, HOUR(x))),"
        " MAP(dt, LAMBDA(x, TEXT(x, \"ddd\"))),"
        " MAP(dt, LAMBDA(x, INT(x)))))"
    )


def per_source(keys_expr, sources, fn):
    """HSTACK(keys, fn(source) for each source) as one spilling formula."""
    parts = ", ".join(f"MAP(k, LAMBDA(x, {fn(s)}))" for s in sources)
    return f"=LET(k, {keys_expr}, HSTACK(k, {parts}))"


MONTHS = "SORT(UNIQUE(FILTER(B2:B, G2:G <> \"\")))"


def daily_formula(sources):
    """Day | daily spend per source + Total | 7-day average (same) | 30-day average (same).

    Each average is the sum over the last N calendar days divided by N (days without orders
    count as 0), shown only once N days of history exist. Written with plain SUMIFS: no
    LET-bound LAMBDA calls, and no LET names that shadow functions (an earlier version
    named a variable `days` and Sheets rejected the whole formula with #ERROR!)."""
    def per(src_filter, window=None):
        crit = f', C2:C, "{src_filter}"' if src_filter else ""
        if window is None:
            return f"MAP(day_list, LAMBDA(dd, SUMIFS(G2:G, L2:L, dd{crit})))"
        return (f"MAP(day_list, LAMBDA(dd, IF(dd - {window - 1} < first_day, NA(),"
                f" SUMIFS(G2:G, L2:L, \">=\" & (dd - {window - 1}), L2:L, \"<=\" & dd{crit}) / {window})))")
    targets = list(sources) + [None]  # None = all sources (Total)
    blocks = [per(t) for t in targets] + [per(t, 7) for t in targets] + [per(t, 30) for t in targets]
    return ("=LET(first_day, INT(MIN(A2:A)), day_list, SEQUENCE(TODAY() - first_day + 1, 1, first_day),"
            f" HSTACK(day_list, {', '.join(blocks)}))")


def swiggy_orders_by_restaurant(limit=None):
    inner = "QUERY(A2:L, \"select D, H, count(G) where G is not null and C = 'swiggy' group by D, H\", 0)"
    lim = f" limit {limit}" if limit else ""
    return (f"QUERY({inner}, \"select Col1, count(Col2) group by Col1 order by count(Col2) desc{lim} "
            "label Col1 'Restaurant', count(Col2) 'Orders'\", 1)")


# ---------- Sheets API helpers ----------

def rgb(hex_):
    h = hex_.lstrip("#")
    return {"rgbColor": {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}}


def value(v):
    if isinstance(v, str) and v.startswith("="):
        return {"userEnteredValue": {"formulaValue": v}}
    if isinstance(v, (int, float)):
        return {"userEnteredValue": {"numberValue": v}}
    return {"userEnteredValue": {"stringValue": v}}


def write(sheet_id, a1, rows, fmt=None):
    r, c = cell(a1)
    cells = [{"values": [{**value(v), **({"userEnteredFormat": fmt} if fmt else {})} for v in row]} for row in rows]
    return {"updateCells": {"start": {"sheetId": sheet_id, "rowIndex": r, "columnIndex": c},
                            "rows": cells, "fields": "userEnteredValue" + (",userEnteredFormat" if fmt else "")}}


def number_format(sheet_id, first_col, last_col, pattern, kind="NUMBER", start_row=1):
    return {"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": start_row,
                  "startColumnIndex": col(first_col), "endColumnIndex": col(last_col) + 1},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": kind, "pattern": pattern}}},
        "fields": "userEnteredFormat.numberFormat"}}


def src(sheet_id, column):
    c = col(column)
    return {"sourceRange": {"sources": [{"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": ROWS,
                                         "startColumnIndex": c, "endColumnIndex": c + 1}]}}


def position(dash_id, row, column):
    return {"overlayPosition": {"anchorCell": {"sheetId": dash_id, "rowIndex": row, "columnIndex": column},
                                "widthPixels": 640, "heightPixels": 340}}


def spec_base(title):
    return {"title": title, "fontName": "Roboto",
            "titleTextFormat": {"foregroundColorStyle": rgb(INK), "fontSize": 13, "bold": True},
            "backgroundColorStyle": rgb(SURFACE)}


def chart(data_id, dash_id, c, row, column):
    if c["kind"] == "PIE":
        spec = {**spec_base(c["title"]),
                "pieChart": {"legendPosition": "LABELED_LEGEND", "pieHole": 0.45,
                             "domain": src(data_id, c["domain"]), "series": src(data_id, c["series"][0])}}
        return {"addChart": {"chart": {"spec": spec, "position": position(dash_id, row, column)}}}
    horizontal = c["kind"] == "BAR"
    value_axis = "BOTTOM_AXIS" if horizontal else "LEFT_AXIS"
    colors = c.get("colors") or [PALETTE[i % len(PALETTE)] for i in range(len(c["series"]))]
    series = []
    for s, color in zip(c["series"], colors):
        entry = {"series": src(data_id, s), "targetAxis": value_axis, "colorStyle": rgb(color)}
        if c["kind"] == "LINE":
            entry["lineStyle"] = {"width": 2}
        if horizontal:
            entry["dataLabel"] = {"type": "DATA", "placement": "OUTSIDE_END",
                                  "textFormat": {"foregroundColorStyle": rgb(INK_MUTED), "fontSize": 9}}
        series.append(entry)
    basic = {"chartType": c["kind"],
             "legendPosition": "TOP_LEGEND" if len(series) > 1 else "NO_LEGEND",
             "axis": [{"position": value_axis, "title": c.get("value_title", "₹")},
                      {"position": "LEFT_AXIS" if horizontal else "BOTTOM_AXIS"}],
             "domains": [{"domain": src(data_id, c["domain"])}],
             "series": series, "headerCount": 1}
    if c.get("stacked"):
        basic["stackedType"] = "STACKED"
    return {"addChart": {"chart": {"spec": {**spec_base(c["title"]), "basicChart": basic},
                                   "position": position(dash_id, row, column)}}}


# ---------- plan: tables in _data + charts ----------

def plan(cfg):
    """(cells to write in _data, number formats, charts, KPI references)."""
    sources = [s["name"] for s in cfg["sources"]]
    labels = [t["sheet_tab"] for t in cfg["sources"]]
    colors = [SOURCE_COLORS.get(s, PALETTE[(i + 4) % len(PALETTE)]) for i, s in enumerate(sources)]
    lay, cells, fmts, charts = Layout(), {}, [], []
    S = len(sources)

    cells["A1"] = MAIN_HEADERS
    cells["A2"] = main_formula(labels)
    for c_, p in (("A", "yyyy-mm-dd hh:mm"), ("I", "yyyy-mm-dd"), ("L", "yyyy-mm-dd")):
        fmts.append((c_, c_, p, "DATE_TIME" if c_ == "A" else "DATE"))
    fmts.append(("G", "G", "#,##0.00", "NUMBER"))

    def table(width, formula, headers=None, numeric_from=1):
        """A table in _data. With `headers`, they go in row 1 and the formula in row 2;
        without, the formula (a QUERY with labels) produces its own header row in row 1."""
        cols = lay.take(width)
        if headers:
            cells[f"{cols[0]}1"] = headers
            cells[f"{cols[0]}2"] = formula
        else:
            cells[f"{cols[0]}1"] = formula
        if width > numeric_from:
            fmts.append((cols[numeric_from], cols[-1], "#,##0", "NUMBER"))
        return cols

    cat = table(2, "=QUERY(A2:L, \"select F, sum(G) where G is not null group by F order by sum(G) desc "
                   "label F 'Category', sum(G) 'Spend'\", 0)")
    cells["M1"] = ["category_group"]
    cells["M2"] = (f"=MAP(FILTER(F2:F, G2:G <> \"\"), LAMBDA(c,"
                   f" IF(COUNTIF(ARRAY_CONSTRAIN({cat[0]}2:{cat[0]}, 7, 1), c), c, \"other\")))")

    month_src = table(1 + S, per_source(MONTHS, sources, lambda s: f"SUMIFS(G2:G, B2:B, x, C2:C, \"{s}\")"),
                      ["Month"] + labels)
    orders_src = table(1 + S, per_source(MONTHS, sources,
                                         lambda s: f"COUNTUNIQUEIFS(H2:H, B2:B, x, C2:C, \"{s}\")"),
                       ["Month"] + labels)
    daily = table(1 + 3 * (S + 1), daily_formula(sources), ["Day"] + (labels + ["Total"]) * 3)
    fmts.append((daily[0], daily[0], "d mmm", "DATE"))
    ma7, ma30 = daily[1 + (S + 1):1 + 2 * (S + 1)], daily[1 + 2 * (S + 1):]
    month_cat = table(9, "=QUERY({B2:B, M2:M, G2:G}, \"select Col1, sum(Col3) where Col3 is not null "
                         "group by Col1 pivot Col2 label Col1 'Month'\", 0)")
    items = table(2, "=QUERY(A2:L, \"select E, sum(G) where G is not null group by E order by sum(G) desc "
                     "limit 12 label E 'Item', sum(G) 'Spend'\", 0)")
    hour = table(2, "=LET(h, SEQUENCE(24, 1, 0), HSTACK(h, MAP(h, LAMBDA(x, SUMIF(J2:J, x, G2:G)))))",
                 ["Hour", "Spend"])
    weekday = table(2, "=LET(d, {\"Mon\"; \"Tue\"; \"Wed\"; \"Thu\"; \"Fri\"; \"Sat\"; \"Sun\"},"
                       " HSTACK(d, MAP(d, LAMBDA(x, SUMIF(K2:K, x, G2:G)))))", ["Weekday", "Spend"])

    charts += [
        {"title": "Daily spend · 7-day moving average", "kind": "LINE", "domain": daily[0], "series": ma7,
         "colors": colors + [TOTAL_COLOR]},
        {"title": "Daily spend · 30-day moving average", "kind": "LINE", "domain": daily[0], "series": ma30,
         "colors": colors + [TOTAL_COLOR]},
        {"title": "Monthly spend by source", "kind": "COLUMN", "domain": month_src[0], "series": month_src[1:],
         "colors": colors, "stacked": True},
        {"title": "Spend by category", "kind": "BAR", "domain": cat[0], "series": [cat[1]]},
        {"title": "Monthly spend by category (top 7 + other)", "kind": "COLUMN", "domain": month_cat[0],
         "series": month_cat[1:], "stacked": True},
        {"title": "Orders per month by source", "kind": "COLUMN", "domain": orders_src[0], "series": orders_src[1:],
         "colors": colors, "stacked": True, "value_title": "Orders"},
    ]

    if "swiggy" in sources:
        rest_orders = table(2, "=" + swiggy_orders_by_restaurant(10))
        pie = table(2, "=LET(t, " + swiggy_orders_by_restaurant() + ","
                       " n, ROWS(t) - 1, body, CHOOSEROWS(t, SEQUENCE(n, 1, 2)), top, ARRAY_CONSTRAIN(body, 7, 2),"
                       " IF(n > 7, VSTACK(top, {\"Others\", SUM(CHOOSECOLS(CHOOSEROWS(body, SEQUENCE(n - 7, 1, 8)), 2))}),"
                       " top))", ["Restaurant", "Orders"])
        rest_spend = table(2, "=QUERY(A2:L, \"select D, sum(G) where G is not null and C = 'swiggy' group by D "
                              "order by sum(G) desc limit 10 label D 'Restaurant', sum(G) 'Spend'\", 0)")
        charts += [
            {"title": "Top Swiggy restaurants by orders", "kind": "BAR", "domain": rest_orders[0],
             "series": [rest_orders[1]], "value_title": "Orders"},
            {"title": "Share of Swiggy orders by restaurant", "kind": "PIE", "domain": pie[0], "series": [pie[1]]},
            {"title": "Top Swiggy restaurants by spend", "kind": "BAR", "domain": rest_spend[0],
             "series": [rest_spend[1]]},
        ]
    charts += [
        {"title": "Top 12 items", "kind": "BAR", "domain": items[0], "series": [items[1]]},
        {"title": "Spend by hour of day", "kind": "COLUMN", "domain": hour[0], "series": [hour[1]]},
        {"title": "Spend by weekday", "kind": "COLUMN", "domain": weekday[0], "series": [weekday[1]]},
    ]
    if "fuel" in sources:
        fuel_m = table(2, per_source(MONTHS, ["fuel"], lambda s: f"SUMIFS(G2:G, B2:B, x, C2:C, \"{s}\")"),
                       ["Month", "Fuel"])
        charts.append({"title": "Fuel spend per month", "kind": "COLUMN", "domain": fuel_m[0],
                       "series": [fuel_m[1]], "colors": [SOURCE_COLORS["fuel"]]})
    return cells, fmts, charts, {"category_col": cat[0]}, lay.next


def requests_for(meta, cfg):
    cells, fmts, charts, refs, width = plan(cfg)
    sheets = {s["properties"]["title"].casefold(): s for s in meta.get("sheets", [])}
    reqs = []

    for s in cfg["sources"]:  # formulas need every source tab to exist
        if s["sheet_tab"].casefold() not in sheets:
            reqs.append({"addSheet": {"properties": {"sheetId": next(NEW_IDS), "title": s["sheet_tab"]}}})

    ours = {c["title"] for c in charts}
    dash = sheets.get(DASHBOARD.casefold())
    if dash:
        dash_id = dash["properties"]["sheetId"]
        reqs += [{"deleteEmbeddedObject": {"objectId": c["chartId"]}} for c in dash.get("charts", [])
                 if c.get("spec", {}).get("title") in ours]  # charts you made yourself are kept
    else:
        dash_id = next(NEW_IDS)
        reqs.append({"addSheet": {"properties": {"sheetId": dash_id, "title": DASHBOARD, "index": 0}}})

    data = sheets.get(DATA_TAB.casefold())
    grid = {"rowCount": ROWS, "columnCount": width + 5}
    if data:
        data_id = data["properties"]["sheetId"]
        reqs.append({"updateSheetProperties": {"properties": {"sheetId": data_id, "hidden": True, "gridProperties": grid},
                                               "fields": "hidden,gridProperties.rowCount,gridProperties.columnCount"}})
    else:
        data_id = next(NEW_IDS)
        reqs.append({"addSheet": {"properties": {"sheetId": data_id, "title": DATA_TAB, "hidden": True,
                                                 "gridProperties": grid}}})

    reqs.append({"updateCells": {"range": {"sheetId": data_id}, "fields": "userEnteredValue"}})
    for a1, v in cells.items():
        reqs.append(write(data_id, a1, [v if isinstance(v, list) else [v]]))
    for first, last, pattern, kind in fmts:
        reqs.append(number_format(data_id, first, last, pattern, kind))

    # Dashboard: title + KPI tiles in rows 1-7
    reqs.append({"updateCells": {"range": {"sheetId": dash_id, "startRowIndex": 0, "endRowIndex": 7,
                                           "startColumnIndex": 0, "endColumnIndex": 16},
                                 "fields": "userEnteredValue,userEnteredFormat"}})
    reqs.append({"updateSheetProperties": {"properties": {"sheetId": dash_id, "gridProperties": {"hideGridlines": True}},
                                           "fields": "gridProperties.hideGridlines"}})
    d = q(DATA_TAB)
    names = " + ".join(s["sheet_tab"] for s in cfg["sources"])
    reqs.append(write(dash_id, "B2", [["Expense Tracker"]],
                      {"textFormat": {"fontSize": 20, "bold": True, "foregroundColorStyle": rgb(INK)}}))
    reqs.append(write(dash_id, "B3", [[f"=\"{names} · \" & TEXT(MIN({d}!A2:A), \"d mmm yyyy\")"
                                       f" & \" – \" & TEXT(MAX({d}!A2:A), \"d mmm yyyy\")"]],
                      {"textFormat": {"fontSize": 10, "foregroundColorStyle": rgb(INK_MUTED)}}))
    kpis = [
        ("B", "Total spent", f"=SUM({d}!G2:G)", RUPEE),
        ("D", "Orders", f"=COUNTUNIQUE({d}!H2:H)", "#,##0"),
        ("F", "Avg per order", "=IFERROR(B6 / D6, 0)", RUPEE),
        ("H", "This month", f"=SUMIF({d}!B2:B, TEXT(TODAY(), \"yyyy-mm\"), {d}!G2:G)", RUPEE),
        ("J", "Last month", f"=SUMIF({d}!B2:B, TEXT(EDATE(TODAY(), -1), \"yyyy-mm\"), {d}!G2:G)", RUPEE),
        ("L", "Last 7 days", f"=SUMIF({d}!A2:A, \">=\" & (TODAY() - 6), {d}!G2:G)", RUPEE),
        ("N", "Top category", f"=IFERROR(INDEX({d}!{refs['category_col']}2:{refs['category_col']}, 1), \"–\")", None),
    ]
    for column, label, formula, pattern in kpis:
        reqs.append(write(dash_id, f"{column}5", [[label]],
                          {"textFormat": {"fontSize": 9, "bold": True, "foregroundColorStyle": rgb(INK_MUTED)}}))
        fmt = {"textFormat": {"fontSize": 18, "bold": True, "foregroundColorStyle": rgb(INK)}, "horizontalAlignment": "LEFT"}
        if pattern:
            fmt["numberFormat"] = {"type": "NUMBER", "pattern": pattern}
        reqs.append(write(dash_id, f"{column}6", [[formula]], fmt))

    # Charts: two per row, at columns B and I, every 18 rows from row 9.
    for i, c in enumerate(charts):
        reqs.append(chart(data_id, dash_id, c, 8 + (i // 2) * 18, 1 if i % 2 == 0 else 8))
    return reqs


def gog(cfg, *args):
    cmd = [cfg["gog"]["binary"], *args, "-a", cfg["gog"]["account"], "--no-input"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"gog {' '.join(args[:2])} failed ({proc.returncode}): {proc.stderr.strip()[:800]}")
    return proc.stdout


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the requests; change nothing")
    ap.add_argument("--config", default=str(CONFIG))
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    meta = json.loads(gog(cfg, "sheets", "raw", cfg["spreadsheet_id"], "--json"))
    reqs = requests_for(meta.get("spreadsheet", meta), cfg)
    if args.dry_run:
        print(json.dumps(reqs, ensure_ascii=False, indent=1))
        return
    gog(cfg, "sheets", "batch-request", cfg["spreadsheet_id"], "--requests-json",
        json.dumps(reqs, ensure_ascii=False), "--force", "--json")
    print(f"Dashboard built: {sum('addChart' in r for r in reqs)} charts, KPI tiles in rows 5-6.")


if __name__ == "__main__":
    main()
