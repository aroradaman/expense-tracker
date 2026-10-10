# Expense Tracker

Syncs delivered **Swiggy** and **Instamart** orders, and **fuel** fill-ups (RBL credit-card swipe alerts at fuel stations, tab `Fuel`), from Gmail into the **Expense Tracker** Google Sheet. Each ordered item gets its own row, with a clean name, a category, and its share of taxes, fees and discounts. It runs daily as an OpenClaw skill on a local model, using `gog` for Gmail and Sheets.

## How it works

```
 Gmail ──gog──▶ prepare ──▶ enrich (model, batches of 10) ──▶ commit ──gog──▶ Google Sheet
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
2. **`enrich`** (the model's only job): gives each **unseen** item name a short display name and a category, 10 at a time. Each call is one round; the loop always finishes, because unanswered items fall back to `uncategorized` and a run is capped at 25 rounds.
   - **Checking:** the script checks every answer and saves the good ones to the item catalog, `assets/catalog.json` in the skill folder, so repeat orders never need the model.
   - **Fallback:** an item rejected twice is written as `uncategorized`.
3. **`commit`**: re-checks for duplicates and appends the rows. It never overwrites or deletes. (Only `apply-catalog`, which you run by hand, edits existing cells, and only `item_name` / `category`.)

Emails the parser doesn't recognise go to a **review queue** (`~/.expense-tracker/pending/`) and are retried on every run, so a parser fix picks them up automatically.

## Run it

From the skill folder:

```bash
python3 scripts/sync.py prepare
python3 scripts/sync.py enrich < answers.json       # JSON list on stdin (or --enrichment-json '...'); repeat per batch
python3 scripts/sync.py commit                      # --dry-run to preview, --skip-enrichment to write as uncategorized
python3 scripts/sync.py status                      # is a run going? what's staged? (read-only)
python3 scripts/sync.py items                       # review item names; merge with merge-items NAME --into NAME
python3 scripts/sync.py set-item "RAW" --name N --category C   # correct one item (or --from-file FILE)
python3 scripts/sync.py apply-catalog --dry-run      # rows in the sheet whose name/category differ from the catalog; drop --dry-run to fix them
python3 -m unittest discover -s scripts/tests       # offline tests, fake gog
python3 scripts/build_dashboard.py                  # one-off: build the Dashboard tab (see below)
```

In OpenClaw the model follows `SKILL.md`, which covers these same steps. Asking OpenClaw for the expense tracker's **status** runs only `sync.py status`: read-only, no sync.

## Files

| File | Purpose |
|---|---|
| `SKILL.md` | Instructions for the model |
| `references/swiggy.md`, `references/instamart.md` | Bill layouts plus naming and category guidance for the model |
| `references/SPEC.md` | Full specification: parsing, allocation, cursor, dedup, operations |
| `assets/config.json` | Sources, spreadsheet ID, categories, backfill days |
| `assets/catalog.json` | Item catalog: every product's display name, category and the bill names that map to it. Updated by each sync; edit it by hand any time |
| `scripts/` | `sync.py` (CLI), parsers, allocation, catalog, review queue, gog wrapper |
| `scripts/tests/` | Unit and end-to-end tests; fixtures come from real emails |

## Good to know

- **Leave the `Swiggy`, `Instamart` and `Fuel` tabs alone.** The script reads them to find where to resume. Put dashboards, pivots and notes in other tabs.
- **One name per product.** New items are shown similar existing names and reuse one when it's the same product, and names are minimal (`Sandwich`, `Burger`, `Maggi`, `Momos`), so `Butterscotch Thickshake` becomes `Milkshake`.
- **Review and fix names:**
  - `sync.py items` lists every name with its raw variants.
  - `sync.py merge-items "Thickshake" --into "Milkshake"` folds names together.
  - `sync.py set-item "Spicy Cheese Vada Pav" --name "Vada Pav" --category snacks` corrects one item; `set-item --from-file FILE` applies a list.
  - Or edit `assets/catalog.json` directly (see below).
  - These change the catalog (future rows). Then `sync.py apply-catalog` rewrites `item_name` / `category` of rows already in the sheet to match; `--dry-run` first shows how many would change.
- **Editing `assets/catalog.json` by hand.** It's a list of products, sorted by category:
  ```json
  {
    "name": "Milkshake",
    "category": "beverages",
    "raw": ["Butterscotch Shake", "Butterscotch Thickshake", "Crunchy Butterscotch Milkshake [350ml]"]
  }
  ```
  - **Rename a product:** change `name`. **Recategorise:** change `category` (one of `categories` in `config.json`).
  - **Merge:** move bill names into another product's `raw` list, and delete the empty product.
  - **Split:** cut a bill name out of a `raw` list into a new product.
  - `raw` entries are the bill names as in column L, matched ignoring case. Don't edit them.
  - Edit only while no sync is running (`sync.py status` says `idle`); a running sync rewrites the file when it saves new items.
  - Every command checks the file first. A JSON typo, an unknown category, or one name in two categories stops the sync with a clear message (and `status` reports `catalog_error`) instead of writing bad rows.
  - Then run `sync.py apply-catalog` to update the rows already in the sheet.
- **Reinstalling the skill replaces the skill folder,** including `assets/catalog.json`. Copy it out first and put it back afterwards (or, if it was lost, a sync rebuilds it with gemma's help, losing your hand edits).
- **Dashboard:** `scripts/build_dashboard.py` builds the `Dashboard` tab once:
  - KPI tiles, plus 7- and 30-day moving averages with Swiggy, Instamart, Fuel and Total lines;
  - spend by source and by category;
  - Swiggy restaurants (top by orders, a pie of order share, top by spend);
  - top items, spend by hour and by weekday, and fuel per month.

  Every number is a live formula, so it follows new rows by itself. Re-run it only after adding a source or to change the layout; it replaces only its own charts.
- **To add a source,** see `references/SPEC.md` §10, then re-run `build_dashboard.py` once.
