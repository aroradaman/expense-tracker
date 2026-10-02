# Expense Tracker

Syncs delivered **Swiggy** and **Instamart** orders from Gmail into the **Expense Tracker** Google Sheet. Each ordered item gets its own row, with a clean name, a category, and its share of taxes, fees and discounts. It runs daily as an OpenClaw skill on a local model, using `gog` for Gmail and Sheets.

## How it works

```
 Gmail ──gog──▶ prepare ──▶ enrich (model, batches of 20) ──▶ commit ──gog──▶ Google Sheet
              │                  │                               │
              │ search from the  │ new item names get a clean    │ append only,
              │ latest order in  │ name + category; answers are  │ one row per item,
              │ the sheet → now  │ checked, cached in a catalog  │ no duplicates
              │ parse bills      │                               │
              │ spread charges   │                               │
              │ skip duplicates  │                               │
```

1. **`prepare`**
   - **Cursor:** reads the latest order time in each tab. An empty tab starts 90 days back.
   - **Search:** searches Gmail from the cursor up to now.
   - **Parse:** parses each "delivered" email and spreads taxes, fees and discounts across its items in proportion to price, so each order's rows add up to the amount paid.
   - **Stage:** drops orders already in the sheet and stages the rest locally.
2. **`enrich`** (the model's only job): gives each **unseen** item name a short display name and a category.
   - **Checking:** the script checks every answer and saves the good ones to `~/.expense-tracker/item_catalog.json`, so repeat orders never need the model.
   - **Fallback:** an item rejected twice is written as `uncategorized`.
3. **`commit`**: re-checks for duplicates and appends the rows. It never overwrites or deletes.

Emails the parser doesn't recognise go to a **review queue** (`~/.expense-tracker/pending/`) and are retried on every run, so a parser fix picks them up automatically.

## Run it

From the skill folder:

```bash
python3 scripts/sync.py prepare
python3 scripts/sync.py enrich --enrichment-json '[{"raw": "...", "name": "...", "category": "..."}]'   # repeat per batch
python3 scripts/sync.py commit                      # --dry-run to preview, --skip-enrichment to write as uncategorized
python3 scripts/sync.py items                       # review item names; merge with merge-items NAME --into NAME
python3 -m unittest discover -s scripts/tests       # offline tests, fake gog
```

In OpenClaw the model follows `SKILL.md`, which covers these same steps.

## Files

| File | Purpose |
|---|---|
| `SKILL.md` | Instructions for the model |
| `references/swiggy.md`, `references/instamart.md` | Bill layouts plus naming and category guidance for the model |
| `references/SPEC.md` | Full specification: parsing, allocation, cursor, dedup, operations |
| `assets/config.json` | Sources, spreadsheet ID, categories, backfill days |
| `scripts/` | `sync.py` (CLI), parsers, allocation, catalog, review queue, gog wrapper |
| `scripts/tests/` | Unit and end-to-end tests; fixtures come from real emails |

## Good to know

- **Leave the `swiggy` and `instamart` tabs alone.** The script reads them to find where to resume. Put dashboards, pivots and notes in other tabs.
- **One name per product.** New items are shown similar existing names and reuse one when it's the same product, so `Butterscotch Thickshake` becomes `Butterscotch Milkshake`.
- **Review and merge names:** `python3 scripts/sync.py items` lists every name with its raw variants. `python3 scripts/sync.py merge-items "Butterscotch Thickshake" --into "Butterscotch Milkshake"` folds names together. Merges and catalog edits apply to future rows; for rows already in the sheet, use Find and replace on column E (`item_name`), which the script never reads.
- **To add a source,** see `references/SPEC.md` §10.
