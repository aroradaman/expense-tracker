#!/usr/bin/env python3
"""Sync delivered Swiggy / Instamart orders from Gmail into the Expense Tracker sheet.

  sync.py prepare
      Fetch new delivery emails, parse bills, stage them locally. Prints `to_enrich`: up to 10
      item names never seen before, which need a clean name + category from the model.

  sync.py enrich < answers.json   (or --enrichment-json '<json list>')
      Check the model's answers, save the good ones to the item catalog, print the next batch
      (including any rejected items, with the problems). Repeat until status is ready_to_commit.

  sync.py commit [--skip-enrichment] [--dry-run]
      Append the staged rows to the sheet (one row per line item, no duplicates).

  sync.py items | sync.py merge-items NAME... --into NAME
      Review the item catalog; map several item names onto one (applies to future rows).

  sync.py set-item RAW --name NAME --category CAT | sync.py set-item --from-file FILE
      A person's correction to the catalog (applies to future rows). You can also edit
      assets/catalog.json directly; every command checks it first.

  sync.py apply-catalog [--dry-run] [--source S]
      Rewrite item_name / category (columns E-F) of rows already in the sheet from the
      catalog. The only command that edits existing cells; it never touches other columns.

Each command prints exactly one JSON line on stdout. Details go to ~/.expense-tracker/logs/.
"""
import argparse
import fcntl
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import enrich  # noqa: E402
import store  # noqa: E402
from allocate import allocate  # noqa: E402
from gog_client import Gog  # noqa: E402
from parsers import PARSERS  # noqa: E402
from parsers.common import ParseError  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
SHEETS_EPOCH = datetime(1899, 12, 30)
DT_FMT = "%Y-%m-%d %H:%M:%S"
APPEND_CHUNK = 500
HEADER = ["order_datetime", "order_id", "merchant", "line_no", "item_name", "category", "quantity",
          "item_price", "final_price", "bill_total", "source", "raw_item_name", "gmail_message_id",
          "ingested_at"]
SHEET_RANGE = "A:N"
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "assets" / "config.json"
STAGED_PATH = store.STATE_DIR / "staged.json"
ENRICH_BATCH = 10       # items per model round; small enough for a 12B model to answer reliably
ENRICH_ATTEMPTS = 2     # rounds an item may go unanswered before it is written as 'uncategorized'
ENRICH_MAX_ROUNDS = 25  # hard cap per run: after this, everything left is written as 'uncategorized'

log = logging.getLogger("expense-tracker")


# ---------- sheet helpers ----------

def as_text(value):
    """Keep Sheets from reinterpreting text: '=...' never becomes a formula."""
    value = str(value)
    if value[:1] in ("=", "+", "-", "@", "'") or value.isdigit():
        return "'" + value
    return value


def as_id(value):
    # Always text: '1a0e22...' is safe but '19e0123' would be read as scientific notation.
    return "'" + str(value)


def parse_sheet_datetime(value):
    if isinstance(value, (int, float)):
        # Round to whole seconds: serial day fractions don't round-trip exactly.
        return SHEETS_EPOCH + timedelta(seconds=round(float(value) * 86400))
    if isinstance(value, str) and value.strip():
        try:
            return datetime.strptime(value.strip().lstrip("'"), DT_FMT)
        except ValueError:
            return None
    return None


def normalize_id(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip().lstrip("'")


def now_ist():
    return datetime.now(IST).replace(tzinfo=None)


def load_config(path):
    return json.loads(Path(path).read_text())


class SheetState:
    def __init__(self, exists=False, has_header=False, last_ts=None, order_ids=None, title=None):
        self.exists = exists
        self.title = title  # the tab's real name in the sheet (may differ in case from config)
        self.has_header = has_header
        self.last_ts = last_ts
        self.order_ids = order_ids if order_ids is not None else set()


def a1(title, cells):
    """A1 range for a tab; quoted when the name has anything but letters, digits or _."""
    if re.fullmatch(r"\w+", title):
        return f"{title}!{cells}"
    return "'" + title.replace("'", "''") + f"'!{cells}"


def resolve_tab(titles, tab):
    """The sheet's own spelling of `tab`. Google Sheets tab names are unique ignoring case,
    so 'Swiggy' in the sheet and 'swiggy' in config are the same tab."""
    if tab in titles:
        return tab
    matches = [t for t in titles if t.casefold() == tab.casefold()]
    return matches[0] if matches else None


def read_sheet_state(gog, cfg, tab):
    """The tab's latest order_datetime (the sync cursor) and every order_id already written."""
    title = resolve_tab(gog.tab_titles(cfg["spreadsheet_id"]), tab)
    if title is None:
        return SheetState(title=tab)
    state = SheetState(exists=True, title=title)
    for row in gog.read_values(cfg["spreadsheet_id"], a1(title, "A1:D")):
        if len(row) < 2 or row[1] in ("", None):
            continue
        if row[1] == HEADER[1]:
            state.has_header = True
            continue
        state.order_ids.add(normalize_id(row[1]))
        ts = parse_sheet_datetime(row[0])
        if ts and (state.last_ts is None or ts > state.last_ts):
            state.last_ts = ts
    return state


def write_bills(gog, cfg, tab, state, bills_rows):
    """Append rows bill by bill. A chunk never splits a bill, so a failed write can't
    leave half an order in the sheet (which dedup would then treat as complete)."""
    if not bills_rows:
        return
    title = state.title or tab
    rng = a1(title, SHEET_RANGE)
    if not state.exists:
        gog.add_tab(cfg["spreadsheet_id"], title)
    if not state.has_header:
        gog.append_rows(cfg["spreadsheet_id"], rng, [HEADER])
    chunk = []
    for rows in bills_rows:
        if chunk and len(chunk) + len(rows) > APPEND_CHUNK:
            gog.append_rows(cfg["spreadsheet_id"], rng, chunk)
            chunk = []
        chunk.extend(rows)
    if chunk:
        gog.append_rows(cfg["spreadsheet_id"], rng, chunk)


def bill_rows(bill, source, catalog, ingested_at):
    finals = allocate([i["line_price"] for i in bill["items"]], bill["bill_total"])
    rows = []
    for n, (item, final) in enumerate(zip(bill["items"], finals), start=1):
        if item.get("category"):
            name, category = item["name"], item["category"]
        else:
            name, category = enrich.lookup(catalog, item["name"])
        rows.append([
            bill["ordered_at"],
            as_id(bill["order_id"]),
            as_text(bill["merchant"]),
            n,
            as_text(name),
            category,
            item["quantity"],
            item["line_price"] / 100,
            final / 100,
            bill["bill_total"] / 100,
            source,
            as_text(item["name"]),
            as_id(bill["message_id"]),
            ingested_at,
        ])
    return rows


# ---------- prepare ----------

def message_datetime(msg):
    internal = (msg.get("message") or {}).get("internalDate")
    if internal:
        return datetime.fromtimestamp(int(internal) / 1000, IST).replace(tzinfo=None)
    return parsedate_to_datetime(msg["headers"]["date"]).astimezone(IST).replace(tzinfo=None)


def queue_for_review(source, msg_id, subject, ordered_at, body, err):
    store.save({
        "message_id": msg_id,
        "source": source,
        "subject": subject,
        "ordered_at": ordered_at.strftime(DT_FMT),
        "error_code": err.code,
        "error": err.detail[:1000],
        "status": "pending",
        "queued_at": now_ist().strftime(DT_FMT),
        "html": body,
    })


def sync_cursor(last_ts, since_override, today, backfill_days):
    """Where this run starts searching (IST): the latest order already in the sheet;
    if the tab is empty, `backfill_days` before today. --since overrides both."""
    if since_override:
        return datetime.combine(since_override, datetime.min.time()), "--since"
    if last_ts:
        return last_ts, "latest_in_sheet"
    return datetime.combine(today - timedelta(days=backfill_days), datetime.min.time()), f"empty_sheet_{backfill_days}_days"


def merchant_tracked(source, merchant):
    """Sources with `merchant_include` (e.g. diesel on a credit card) keep only matching merchants."""
    words = source.get("merchant_include")
    return not words or any(w.lower() in merchant.lower() for w in words)


def gmail_query(sender, subject_terms, since, until=None):
    # Epoch seconds, not YYYY/MM/DD: Gmail reads dates in US Pacific time, which is up to
    # 13.5h off from IST. One second earlier makes the bound inclusive; the order already
    # in the sheet at exactly `since` is then skipped by order_id dedup.
    epoch = int(since.replace(tzinfo=IST).timestamp()) - 1
    terms = " ".join(f'subject:"{t}"' if " " in t else f"subject:{t}" for t in subject_terms)
    query = f"from:{sender} {terms} after:{epoch}"
    if until:
        query += f" before:{int(until.replace(tzinfo=IST).timestamp())}"
    return query


def collect_source(gog, cfg, source, since_override, today, until=None):
    """Find and parse new bills for one source. Reads only; never writes to the sheet."""
    name, tab = source["name"], source["sheet_tab"]
    parser = PARSERS[source["parser"]]
    want_from = source["gmail_filters"]["from"].lower()
    subject_terms = [t.lower() for t in source["gmail_filters"]["subject_contains"]]
    stats = {"emails_found": 0, "new_bills": 0, "duplicates_skipped": 0}
    errors = Counter()

    sheet = read_sheet_state(gog, cfg, tab)
    since, cursor = sync_cursor(sheet.last_ts, since_override, today, cfg.get("initial_backfill_days", 90))
    stats["since"] = since.strftime(DT_FMT)
    stats["cursor"] = cursor

    seen = set(sheet.order_ids)
    bills, recovered = [], []

    def accept(bill, msg_id, ordered_at):
        if not bill["order_id"]:
            bill = {**bill, "order_id": msg_id}  # e.g. card alerts: one email = one purchase
        if not merchant_tracked(source, bill["merchant"]):
            stats["not_tracked"] = stats.get("not_tracked", 0) + 1
            log.info("[%s] %s skipped: merchant %r not in merchant_include", name, msg_id, bill["merchant"])
            return
        if bill["order_id"] in seen:
            stats["duplicates_skipped"] += 1
            return
        seen.add(bill["order_id"])
        bills.append({**bill, "message_id": msg_id, "ordered_at": ordered_at.strftime(DT_FMT)})
        stats["new_bills"] += 1
        log.info("[%s] %s order %s: %d items, paid %.2f", name, msg_id, bill["order_id"],
                 len(bill["items"]), bill["bill_total"] / 100)

    # Emails that failed before get another try, in case the parsers have been improved since.
    for rec in store.all_records(status="pending", source=name):
        try:
            bill = parser(rec["html"])
        except ParseError:
            continue
        accept(bill, rec["message_id"], datetime.strptime(rec["ordered_at"], DT_FMT))
        recovered.append(rec["message_id"])

    query = gmail_query(want_from, subject_terms, since, until)
    threads = gog.search_threads(query)
    message_ids = []
    for thread in threads:
        if thread.get("messageCount", 1) > 1:
            message_ids.extend(gog.thread_message_ids(thread["id"]))
        else:
            message_ids.append(thread["id"])
    message_ids = list(dict.fromkeys(message_ids))
    stats["emails_found"] = len(message_ids)
    log.info("[%s] query=%r threads=%d messages=%d last_ts=%s existing_orders=%d",
             name, query, len(threads), len(message_ids), sheet.last_ts, len(sheet.order_ids))

    # A fetch error propagates and fails this source for the run: nothing is staged, so the
    # cursor can't move past an email we never read. The next run retries the same window.
    for msg_id in message_ids:
        if store.exists(msg_id):
            continue
        msg = gog.get_message(msg_id)
        headers = msg.get("headers") or {}
        sender = parseaddr(headers.get("from", ""))[1].lower()
        subject = headers.get("subject", "")
        if sender != want_from or not all(t in subject.lower() for t in subject_terms):
            errors["FILTER_REJECTED"] += 1
            log.warning("[%s] %s rejected: from=%r subject=%r", name, msg_id, sender, subject)
            continue
        ordered_at = message_datetime(msg)
        body = msg.get("body") or ""
        try:
            bill = parser(body)
        except ParseError as e:
            errors[e.code] += 1
            log.error("[%s] %s queued for review: %s", name, msg_id, e)
            queue_for_review(name, msg_id, subject, ordered_at, body, e)
            continue
        accept(bill, msg_id, ordered_at)

    stats["errors"] = dict(errors)
    # Recovered emails leave the queue at commit; they are new bills now, not pending.
    stats["pending_review"] = len(store.all_records(status="pending", source=name)) - len(recovered)
    return stats, {"tab": tab, "bills": bills, "recovered": recovered}


def prepare(gog, cfg, since_override, only_source, today, until=None):
    staged = {"created_at": now_ist().strftime(DT_FMT), "sources": {}}
    summary, failed = {}, []
    for source in cfg["sources"]:
        if only_source and source["name"] != only_source:
            continue
        try:
            stats, data = collect_source(gog, cfg, source, since_override, today, until)
        except Exception as e:  # one source failing must not block the others
            failed.append(source["name"])
            log.exception("[%s] failed", source["name"])
            summary[source["name"]] = {"status": "error", "error": str(e)[:300]}
            continue
        summary[source["name"]] = stats
        if data["bills"] or data["recovered"]:
            staged["sources"][source["name"]] = data

    # A re-run of prepare keeps the previous run's attempt counts, so items the model keeps
    # getting wrong still drop out instead of coming back forever. The round cap restarts.
    previous = json.loads(STAGED_PATH.read_text()) if STAGED_PATH.exists() else {}
    staged["enrich_attempts"] = previous.get("enrich_attempts", {})
    staged["enrich_rounds"] = 0
    batch, remaining = next_enrich_batch(staged, enrich.load_catalog())
    staged["enrich_batch"] = [enrich.key(b["raw"]) for b in batch]

    if staged["sources"]:
        STAGED_PATH.parent.mkdir(parents=True, exist_ok=True)
        STAGED_PATH.write_text(json.dumps(staged, ensure_ascii=False))
    else:
        STAGED_PATH.unlink(missing_ok=True)

    new_bills = sum(len(d["bills"]) for d in staged["sources"].values())
    if not staged["sources"]:
        status, nxt = "nothing_new", "Nothing to commit. Report the summary and stop."
    elif batch:
        status, nxt = "needs_enrichment", ENRICH_NEXT
    else:
        status, nxt = "ready_to_commit", "All items are already in the catalog. Run: sync.py commit"
    out = {"status": status, "new_bills": new_bills, "sources": summary,
           "pending_review": sum(s.get("pending_review", 0) for s in summary.values()),
           "next": nxt}
    if batch:
        out["categories"] = cfg["categories"]
        out["to_enrich"] = batch
        out["to_enrich_remaining"] = remaining
    if failed:
        # Reported, not fatal: the other sources still commit. A failed source stages nothing,
        # so its cursor stays put and the next run retries the same window.
        out["failed_sources"] = failed
    return out, bool(failed) and len(failed) == len(summary)


# ---------- enrich + commit ----------

ENRICH_NEXT = ("Give each to_enrich item a clean name and a category, then send them as a JSON list "
               "on stdin: sync.py enrich <<'JSON' ... JSON")


def staged_items(staged):
    """One entry per distinct raw item name in the staged bills, in order."""
    seen, out = set(), []
    for name, data in staged["sources"].items():
        for bill in data["bills"]:
            for item in bill["items"]:
                if item.get("category"):
                    continue  # the parser already named and categorised it (e.g. Diesel)
                k = enrich.key(item["name"])
                if k not in seen:
                    seen.add(k)
                    out.append({"raw": item["name"], "merchant": bill["merchant"], "source": name})
    return out


def next_enrich_batch(staged, catalog):
    """Items still needing a name/category, minus those the model already failed too often.
    Empty once the per-run round cap is hit, so the loop always ends."""
    if staged.get("enrich_rounds", 0) >= ENRICH_MAX_ROUNDS:
        return [], 0
    attempts = staged.get("enrich_attempts", {})
    todo = [i for i in staged_items(staged)
            if enrich.key(i["raw"]) not in catalog and attempts.get(enrich.key(i["raw"]), 0) < ENRICH_ATTEMPTS]
    batch = [{**i, "similar": enrich.similar(catalog, i["raw"])} for i in todo[:ENRICH_BATCH]]
    return batch, len(todo) - len(batch)


def parse_enrichment(text):
    """The model's answer as a list. Tolerates code fences, prose around the JSON, trailing
    commas and a single object. Raises ValueError if nothing usable is found."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    candidates = [text]
    if "[" in text and "]" in text:
        candidates.append(text[text.index("["):text.rindex("]") + 1])
    if "{" in text and "}" in text:
        candidates.append(text[text.index("{"):text.rindex("}") + 1])
    for c in candidates:
        for variant in (c, re.sub(r",\s*([\]}])", r"\1", c)):
            try:
                data = json.loads(variant)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                data = data.get("items") or data.get("to_enrich") or [data]
            if isinstance(data, list):
                return data
    raise ValueError("could not find a JSON list in the answer")


def enrich_step(cfg, enrichment_text):
    """Save the model's answers for the current batch and hand out the next one.

    Every call is one round. Items of the current batch that are still unanswered afterwards
    (wrong answer, missing answer, or unreadable JSON) count one attempt; after
    ENRICH_ATTEMPTS they drop out and are written as 'uncategorized'. So the loop always
    reaches ready_to_commit, whatever the model sends."""
    if not STAGED_PATH.exists():
        return {"status": "nothing_to_commit", "next": "Run sync.py prepare first."}, 0
    staged = json.loads(STAGED_PATH.read_text())
    catalog = enrich.load_catalog()
    try:
        entries = parse_enrichment(enrichment_text)
        problems, accepted = enrich.validate(entries, [i["raw"] for i in staged_items(staged)],
                                             cfg["categories"], catalog)
    except ValueError as e:
        problems, accepted = [f"invalid JSON ({e}). Send only a JSON list, using the heredoc form from SKILL.md"], {}
    catalog.update({k: {"name": v["name"], "category": v["category"], "raw": v["raw"]} for k, v in accepted.items()})
    enrich.save_catalog(catalog)

    staged["enrich_rounds"] = staged.get("enrich_rounds", 0) + 1
    attempts = staged.setdefault("enrich_attempts", {})
    for k in staged.get("enrich_batch", []):
        if k not in catalog:
            attempts[k] = attempts.get(k, 0) + 1
    batch, remaining = next_enrich_batch(staged, catalog)
    staged["enrich_batch"] = [enrich.key(b["raw"]) for b in batch]
    STAGED_PATH.write_text(json.dumps(staged, ensure_ascii=False))

    total = len(staged_items(staged))
    done = sum(1 for i in staged_items(staged) if enrich.key(i["raw"]) in catalog)
    out = {"saved": len(accepted), "progress": f"{done}/{total} items named",
           "given_up": total - done - len(batch) - remaining}
    if problems:
        out["problems"] = problems
    if batch:
        out.update({"status": "needs_enrichment", "categories": cfg["categories"], "to_enrich": batch,
                    "to_enrich_remaining": remaining,
                    "next": ("Fix the items listed in problems (they are back in to_enrich) and answer the rest. "
                             if problems else "") + ENRICH_NEXT})
    else:
        out.update({"status": "ready_to_commit", "next": "Run: sync.py commit"})
    return out, 0


def list_items():
    """Canonical names with their category and the raw names that map to them."""
    groups = {}
    for raw_key, entry in enrich.load_catalog().items():
        g = groups.setdefault(entry["name"].lower(), {"name": entry["name"], "category": entry["category"], "raw": []})
        g["raw"].append(entry.get("raw", raw_key))
    items = sorted(groups.values(), key=lambda g: (g["category"], g["name"].lower()))
    return {"status": "ok", "items": items, "count": len(items)}, 0


def merge_items(names, into):
    catalog = enrich.load_catalog()
    try:
        changed = enrich.merge(catalog, names, into)
    except KeyError:
        return {"status": "error", "error": f"{into!r} is not an existing item name; see `sync.py items`"}, 2
    enrich.save_catalog(catalog)
    return {"status": "ok", "catalog_entries_changed": changed, "into": into,
            "note": "Applies to rows written from now on. To fix existing rows: sync.py apply-catalog"}, 0


def set_items(entries, categories):
    """Apply a person's corrections: [{"raw", "name", "category"}, ...]. All-or-nothing."""
    if not isinstance(entries, list) or not entries:
        return {"status": "error", "error": 'expected a JSON list like [{"raw": "...", "name": "...", "category": "..."}]'}, 2
    problems = []
    for n, e in enumerate(entries, start=1):
        if not isinstance(e, dict) or not all(str(e.get(k, "")).strip() for k in ("raw", "name", "category")):
            problems.append(f"entry {n}: needs non-empty raw, name and category")
        elif e["category"] not in categories:
            problems.append(f"entry {n} ({e['raw']!r}): category {e['category']!r} is not one of {categories}")
    if problems:
        return {"status": "error", "problems": problems}, 2
    catalog = enrich.load_catalog()
    changed = sum(enrich.set_item(catalog, e["raw"], e["name"], e["category"]) for e in entries)
    enrich.save_catalog(catalog)
    return {"status": "ok", "items": len(entries), "catalog_entries_changed": changed,
            "note": "Applies to rows written from now on. To fix existing rows: sync.py apply-catalog"}, 0


CATALOG_COLUMNS = {4: "item_name", 5: "category", 11: "raw_item_name"}  # 0-based: E, F, L
UPDATE_CHUNK = 200


def catalog_changes(rows, catalog):
    """[(sheet row number, name, category)] for rows whose E/F differ from the catalog entry
    of their raw_item_name. Rows whose raw name has no catalog entry (e.g. fuel) are left alone."""
    changes = []
    for n, row in enumerate(rows, start=1):
        if len(row) <= 11 or row[1] in ("", None, HEADER[1]):
            continue
        entry = catalog.get(enrich.key(str(row[11])))
        if entry and (str(row[4]), str(row[5])) != (entry["name"], entry["category"]):
            changes.append((n, entry["name"], entry["category"]))
    return changes


def apply_catalog(gog, cfg, only_source, dry_run):
    """Rewrite item_name (E) and category (F) of existing rows from the item catalog.
    Only those two cells change; every other cell, and the row order, stay as they are."""
    catalog = enrich.load_catalog()
    titles = gog.tab_titles(cfg["spreadsheet_id"])
    ids = None
    result = {}
    for source in cfg["sources"]:
        name = source["name"]
        if only_source and name != only_source:
            continue
        title = resolve_tab(titles, source["sheet_tab"])
        if title is None:
            continue
        rows = gog.read_values(cfg["spreadsheet_id"], a1(title, "A1:L"))
        header = rows[0] if rows else []
        if any(len(header) <= i or header[i] != col for i, col in CATALOG_COLUMNS.items()):
            raise RuntimeError(f"tab {title!r}: row 1 is not the expected header "
                               f"(E=item_name, F=category, L=raw_item_name); nothing changed")
        changes = catalog_changes(rows, catalog)
        result[name] = {"rows_changed" if not dry_run else "rows_to_change": len(changes),
                        "examples": [f"row {n}: {nm} / {cat}" for n, nm, cat in changes[:5]]}
        if dry_run or not changes:
            continue
        if ids is None:
            ids = gog.tab_ids(cfg["spreadsheet_id"])
        requests = [{"updateCells": {
            "range": {"sheetId": ids[title], "startRowIndex": n - 1, "endRowIndex": n,
                      "startColumnIndex": 4, "endColumnIndex": 6},
            "rows": [{"values": [{"userEnteredValue": {"stringValue": nm}},
                                 {"userEnteredValue": {"stringValue": cat}}]}],
            "fields": "userEnteredValue"}} for n, nm, cat in changes]
        for i in range(0, len(requests), UPDATE_CHUNK):
            gog.batch_request(cfg["spreadsheet_id"], requests[i:i + UPDATE_CHUNK])
        log.info("apply-catalog: %s rows changed in %s", len(changes), title)
    return {"status": "ok", "dry_run": dry_run, "sources": result}, 0


def commit(gog, cfg, skip_enrichment, dry_run):
    if not STAGED_PATH.exists():
        return {"status": "nothing_to_commit", "next": "Run sync.py prepare first."}, 0
    staged = json.loads(STAGED_PATH.read_text())
    catalog = enrich.load_catalog()
    batch, remaining = next_enrich_batch(staged, catalog)
    if batch and not skip_enrichment:
        return {"status": "needs_enrichment", "categories": cfg["categories"], "to_enrich": batch,
                "to_enrich_remaining": remaining, "next": ENRICH_NEXT}, 2
    still_missing = enrich.missing(catalog, [i["raw"] for i in staged_items(staged)])

    ingested_at = now_ist().strftime(DT_FMT)
    result = {}
    for name, data in staged["sources"].items():
        # Re-read the sheet: anything written since `prepare` (e.g. by an overlapping run) is skipped.
        sheet = read_sheet_state(gog, cfg, data["tab"])
        bills_rows, dupes = [], 0
        for bill in sorted(data["bills"], key=lambda b: (b["ordered_at"], b["order_id"])):
            if bill["order_id"] in sheet.order_ids:
                dupes += 1
                continue
            sheet.order_ids.add(bill["order_id"])
            bills_rows.append(bill_rows(bill, name, catalog, ingested_at))
        n_rows = sum(len(r) for r in bills_rows)
        if dry_run:
            for rows in bills_rows:
                for row in rows:
                    print(json.dumps(row, ensure_ascii=False), file=sys.stderr)
        else:
            write_bills(gog, cfg, data["tab"], sheet, bills_rows)
            for msg_id in data["recovered"]:
                store.delete(msg_id)
        result[name] = {"rows_written": 0 if dry_run else n_rows, "duplicates_skipped": dupes}
        if dry_run:
            result[name]["rows_pending"] = n_rows

    if not dry_run:
        STAGED_PATH.unlink(missing_ok=True)
    return {"status": "ok", "dry_run": dry_run, "sources": result,
            "items_uncategorized": len(still_missing),
            "pending_review": len(store.all_records(status="pending"))}, 0


# ---------- CLI ----------

def setup_logging():
    log_dir = store.STATE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / f"sync-{date.today().isoformat()}.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


LOCK_PATH = store.STATE_DIR / "sync.lock"
LOCK_WAIT = 20 * 60  # seconds a call waits for a running sync.py before giving up
JOINABLE = {"prepare", "commit"}


def lock_holder():
    """Who holds the run lock: {"pid", "command", "started_at", "running_seconds"}, or None.
    The OS releases the lock when its process exits, so a holder is always a live process."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.seek(0)
            try:
                info = json.loads(f.read() or "{}")
            except json.JSONDecodeError:
                info = {}
            if info.get("started_at"):
                started = datetime.strptime(info["started_at"], DT_FMT)
                info["running_seconds"] = int((now_ist() - started).total_seconds())
            return info or {"pid": None}
        fcntl.flock(f, fcntl.LOCK_UN)
        return None


def acquire_lock(command, argv=()):
    """Returns an open, exclusively locked file, or None if another run holds it.
    Opened in append mode so a failed attempt never wipes the holder's details.
    `lock.run` identifies this run: {"pid", "command", "argv", "started_at"}."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    run = {"pid": os.getpid(), "command": command, "argv": list(argv), "started_at": now_ist().strftime(DT_FMT)}
    lock.seek(0)
    lock.truncate()
    lock.write(json.dumps(run))
    lock.flush()
    lock.run = run
    return lock


def _result_path(command):
    return store.STATE_DIR / f"last_{command}.json"


def save_result(run, out, code):
    path = _result_path(run["command"])
    path.write_text(json.dumps({"run": run, "code": code, "output": out}, ensure_ascii=False))


def lock_or_join(command, argv, timeout=None, poll=2.0, sleep=time.sleep, clock=time.monotonic):
    """Take the run lock, waiting for whoever holds it.

    OpenClaw moves long commands (the first `prepare` takes minutes) to the background,
    and the model may then run the same command again. Instead of failing, the second call
    waits; if the holder was the *identical* command, it returns that run's saved result
    rather than repeating the work. Returns (lock, None) to proceed, (None, result) when
    joined, or (None, None) on timeout."""
    timeout = LOCK_WAIT if timeout is None else timeout
    lock = acquire_lock(command, argv)
    if lock:
        return lock, None
    holder = lock_holder() or {}
    log.info("%s waiting for running %s (pid %s)", command, holder.get("command"), holder.get("pid"))
    deadline = clock() + timeout
    while True:
        sleep(poll)
        lock = acquire_lock(command, argv)
        if lock:
            break
        if clock() > deadline:
            return None, None
    # Only prepare and commit are joinable: their argv says everything about the run. enrich
    # reads its answers from stdin, so two enrich calls with equal argv can differ.
    if command in JOINABLE and holder.get("command") == command and holder.get("argv") == list(argv):
        path = _result_path(command)
        saved = json.loads(path.read_text()) if path.exists() else None
        if saved and saved.get("run") == {k: holder.get(k) for k in ("pid", "command", "argv", "started_at")}:
            lock.close()
            return None, saved
    return lock, None


def status(categories):
    """Read-only: is a run in progress, and is there staged work to continue?"""
    holder = lock_holder()
    out = {"running": holder, "pending_review": len(store.all_records(status="pending"))}
    problems = catalog_problems(categories)
    if problems:
        out["catalog_problems"] = problems[:20]
        return {**out, "status": "catalog_error",
                "next": "assets/catalog.json needs fixing by the user; a sync won't run until it's fixed."}, 0
    if not STAGED_PATH.exists():
        out.update({"status": "running" if holder else "idle", "staged": False,
                    "next": ("A run is in progress: wait for its output, do not start another." if holder
                             else "Nothing in progress. Only if the user asked to sync: sync.py prepare")})
        return out, 0
    staged = json.loads(STAGED_PATH.read_text())
    batch, remaining = next_enrich_batch(staged, enrich.load_catalog())
    out.update({"staged": True, "staged_at": staged.get("created_at"),
                "new_bills": sum(len(d["bills"]) for d in staged["sources"].values())})
    if holder:
        out.update({"status": "running", "next": "A run is in progress: wait for its output, do not start another."})
    elif batch:
        out.update({"status": "needs_enrichment", "categories": categories,
                    "to_enrich": batch, "to_enrich_remaining": remaining,
                    "next": "Only if the user asked to sync: " + ENRICH_NEXT})
    else:
        out.update({"status": "ready_to_commit", "next": "Only if the user asked to sync: sync.py commit"})
    return out, 0


def catalog_problems(categories):
    """Problems in assets/catalog.json (hand edits): bad JSON or format, unknown category,
    one name in two categories. Checked before every command except status."""
    try:
        return enrich.check(enrich.load_catalog(), categories)
    except enrich.CatalogError as e:
        return [str(e)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="fetch and parse new orders; list items needing enrichment")
    p.add_argument("--since", type=date.fromisoformat, help="YYYY-MM-DD; overrides the sheet watermark")
    p.add_argument("--until", type=date.fromisoformat,
                   help="YYYY-MM-DD (exclusive, IST); with --since, backfill one window at a time, oldest first")
    p.add_argument("--source", help="only this source")
    p = sub.add_parser("enrich", help="save names/categories for the current batch; prints the next batch")
    p.add_argument("--enrichment-json", help='[{"raw": "...", "name": "...", "category": "..."}, ...]; '
                                             'omit to read the list from stdin (safe for names with apostrophes)')
    sub.add_parser("status", help="read-only: is a run in progress, what is staged, the current to_enrich batch")
    sub.add_parser("items", help="list canonical item names, their category and raw names")
    p = sub.add_parser("merge-items", help="map items onto one existing canonical name (future rows)")
    p.add_argument("names", nargs="+", help="canonical names to merge away")
    p.add_argument("--into", required=True, help="existing canonical name to keep")
    p = sub.add_parser("set-item", help="correct one item's name/category (or many with --from-file)")
    p.add_argument("raw", nargs="?", help="raw item name as on the bill (column L)")
    p.add_argument("--name", help="display name to use")
    p.add_argument("--category", help="one of the categories in config.json")
    p.add_argument("--from-file", help='JSON list [{"raw", "name", "category"}, ...]')
    p = sub.add_parser("apply-catalog",
                       help="rewrite item_name/category (columns E-F) of existing rows from the catalog")
    p.add_argument("--source", help="only this source")
    p.add_argument("--dry-run", action="store_true", help="only count the rows that would change")
    p = sub.add_parser("commit", help="append staged rows to the sheet")
    p.add_argument("--skip-enrichment", action="store_true",
                   help="write items without a catalog entry as raw name + 'uncategorized'")
    p.add_argument("--dry-run", action="store_true", help="print rows to stderr; write nothing")
    args = ap.parse_args(argv)

    setup_logging()
    if args.cmd == "status":
        out, code = status(load_config(args.config)["categories"])
        print(json.dumps(out, ensure_ascii=False))
        return code
    lock, joined = lock_or_join(args.cmd, argv if argv is not None else sys.argv[1:])
    if joined:
        print(json.dumps({**joined["output"], "joined_running_run": True}, ensure_ascii=False))
        return joined["code"]
    if lock is None:
        print(json.dumps({"status": "skipped", "reason": f"another sync.py run held the lock for over {LOCK_WAIT // 60} minutes",
                          "running": lock_holder(),
                          "next": "Do not start another run. Report this and stop."}, ensure_ascii=False))
        return 0

    problems = catalog_problems(load_config(args.config)["categories"])
    if problems:
        print(json.dumps({"status": "error", "error": "assets/catalog.json needs fixing; nothing was run",
                          "problems": problems[:20],
                          "next": "Report the problems to the user and stop. Do not edit the catalog yourself."},
                         ensure_ascii=False))
        return 2

    if args.cmd == "items":
        out, code = list_items()
        print(json.dumps(out, ensure_ascii=False))
        return code
    if args.cmd == "merge-items":
        out, code = merge_items(args.names, args.into)
        print(json.dumps(out, ensure_ascii=False))
        return code

    cfg = load_config(args.config)
    if args.cmd == "set-item":
        if args.from_file:
            path = Path(args.from_file)
            if not path.is_absolute() and not path.exists():
                path = DEFAULT_CONFIG.parent.parent / path  # relative to the skill folder
            entries = json.loads(path.read_text())
        elif args.raw and args.name and args.category:
            entries = [{"raw": args.raw, "name": args.name, "category": args.category}]
        else:
            entries = None
        out, code = set_items(entries, cfg["categories"])
        print(json.dumps(out, ensure_ascii=False))
        return code
    gog = Gog(cfg["gog"]["binary"], cfg["gog"]["account"], log=log)
    try:
        if args.cmd == "prepare":
            until = datetime.combine(args.until, datetime.min.time()) if args.until else None
            out, failed = prepare(gog, cfg, args.since, args.source, now_ist().date(), until)
            code = 1 if failed else 0
        elif args.cmd == "enrich":
            text = args.enrichment_json if args.enrichment_json is not None else sys.stdin.read()
            out, code = enrich_step(cfg, text)
        elif args.cmd == "apply-catalog":
            out, code = apply_catalog(gog, cfg, args.source, args.dry_run)
        else:
            out, code = commit(gog, cfg, args.skip_enrichment, args.dry_run)
    except Exception as e:
        log.exception("%s failed", args.cmd)
        out, code = {"status": "error", "error": str(e)[:300]}, 1
    save_result(lock.run, out, code)
    print(json.dumps(out, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
