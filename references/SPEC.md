# Expense Tracker — Specification

## 1. Overview

Expense Tracker reads delivered-order emails from Gmail and writes one row per ordered item to the "Expense Tracker" Google Sheet. Sources so far: **Swiggy** (food, including Swiggy Gourmet), **Instamart**, and **fuel** (RBL Bank credit-card swipe alerts at fuel stations, tab `Fuel`).

It ships as an **OpenClaw skill** (`expense-tracker`) that OpenClaw runs **once a day** on a **local open model** (e.g. gemma 12B on a 16 GB GPU). All Gmail and Sheets access goes through the **gog** CLI.

**Design principle: the script does the exact work, and the model does the judgment.**
- **Script:** search Gmail, parse bills, spread charges across items, dedup, write the sheet. Deterministic and covered by tests.
- **Model:** for item names it hasn't seen before, a clean display name and a category (§7). Every answer is checked before it is used.

The model never reads email HTML, does arithmetic, calls gog, or decides which rows are written.

### Goals
- One row per bill line, in the source's tab.
- Taxes, fees, packaging, tips and discounts are spread across the items in proportion to price, so each order's rows add up to the amount paid.
- Each run starts from the latest order already in the sheet, and nothing is ever written twice.

### Non-goals
- Sources other than Gmail.
- Cancelled, refunded or undelivered orders. Only "delivered" emails count, and later refunds are ignored.
- Deleting rows, or changing the numbers of rows already in the sheet. The sync only appends. The one exception is `sync.py apply-catalog`, run by hand, which rewrites the name and category cells (E–F) from the item catalog (§7).
- Dashboards inside the sync. `scripts/build_dashboard.py` builds a separate `Dashboard` tab once, by hand (§8.4).

---

## 2. Environment

| Item | Value |
|---|---|
| Host | `asgard` (`ssh -i /home/daman/.ssh/asgard daman@asgard`) |
| gog | `/usr/local/bin/gog` v0.42.0, account `aroradaman@gmail.com` |
| Spreadsheet | "Expense Tracker", ID `1SKlTsjmB_XXH4swFjaN9KpqxoBU_ZetjMt4wsaLz8Lc` (locale `en_GB`, time zone `Etc/GMT`) |
| Runtime | Python 3, standard library only |
| Orchestrator | OpenClaw scheduled task, daily, local model |
| Times | All stored times are IST (`Asia/Kolkata`) wall-clock |
| State | `~/.expense-tracker/`: staged run, review queue, logs, lock. Outside the skill folder, so it survives reinstalls. The item catalog is the exception: it lives in the skill folder (`assets/catalog.json`) so it can be edited by hand, and must be copied out before a reinstall. |

---

## 3. Configuration (`assets/config.json`)

```json
{
  "spreadsheet_id": "1SKlTsjmB_XXH4swFjaN9KpqxoBU_ZetjMt4wsaLz8Lc",
  "initial_backfill_days": 90,
  "categories": ["meals", "snacks", "desserts", "beverages", "groceries", "fruits_vegetables",
                 "dairy_bakery", "meat_fish_eggs", "personal_care", "household", "health",
                 "tobacco", "alcohol", "fuel", "other"],
  "gog": {"binary": "/usr/local/bin/gog", "account": "aroradaman@gmail.com"},
  "sources": [
    {"name": "swiggy", "sheet_tab": "Swiggy", "parser": "swiggy",
     "gmail_filters": {"from": "noreply@swiggy.in", "subject_contains": ["delivered"]}},
    {"name": "instamart", "sheet_tab": "Instamart", "parser": "instamart",
     "gmail_filters": {"from": "noreply@instamart.in", "subject_contains": ["delivered"]}},
    {"name": "fuel", "sheet_tab": "Fuel", "parser": "rbl_card",
     "gmail_filters": {"from": "RBLAlerts@rbl.bank.in", "subject_contains": ["has just been swiped"]},
     "merchant_include": ["FUEL", "PETROL", "DIESEL", "FILLING", "SERVICE STATION", "…"]}
  ]
}
```

An email belongs to a source only if **both** of these hold:
- The parsed `From` address is exactly the source's `from`. This is checked in code as well as in the Gmail query, so spoofed or forwarded mail is rejected (`FILTER_REJECTED`).
- The subject contains every `subject_contains` term (case-insensitive).

Swiggy and Instamart subjects seen so far all contain "delivered":
- "…delivered superfast" / "on time" / "before time";
- "…successfully delivered";
- "Your Swiggy Gourmet order was delivered…".

Instamart marketing emails from `noreply@swiggy.in` ("Onion price alert") don't match.

Each source is synced on its own, with its own cursor, dedup set and tab. If one source fails, the others still sync.

---

## 4. Parsing

`scripts/parsers/<source>.py` turns an email's HTML into a bill:

| Field | Notes |
|---|---|
| `order_id` | Swiggy / Instamart order number; for card alerts, which have none, the Gmail message id. The dedup key. |
| `merchant` | Restaurant name; always `Instamart` for Instamart (the email names no store). |
| `items[]` | `name` (without the quantity marker), `quantity`, and `line_price` (the line total, not the unit price). |
| `charges[]` | `label` and `amount`. Fees and taxes are positive, discounts negative, `FREE` is 0. |
| `bill_total` | The amount actually paid. |
| `template_id` | Which layout matched. |

The order time is not parsed from the body, because times there have no year. It is Gmail's `internalDate` from `gog gmail get`, converted to IST, with the `Date` header as a fallback.

### 4.1 Rules for all layouts
- **Money:** amounts are integer **paise**. `₹`, `Rs.`, commas and spaces are stripped, and `- ₹N` is negative. When a cell shows a struck-out price before the charged one (`₹45 FREE`, `₹300 ₹250`), the last value counts.
- **Lines are classified by their position in the bill, not their label.** Items and charges sit in separate blocks, so a new or renamed fee needs no change.
- **Subtotals are checks, never charges.** `Item Total` (Swiggy) and `Item Bill` (Instamart) must equal the sum of the item prices exactly.
- **Total check:** `items + charges` must equal `bill_total` within ±₹1. Swiggy rounds the amount paid; e.g. ₹239.16 computed vs ₹239 paid.
- **Allocation** always uses `bill_total`.
- **Failures:** any failure raises a coded error: `UNKNOWN_TEMPLATE`, `UNKNOWN_LINE` or `TOTAL_MISMATCH`. The email goes to the review queue (§8.3) and is never guessed at.

### 4.2 Swiggy food (`parsers/swiggy.py`), checked against real emails 2024–2026

| Layout | Seen | Detected by | Order ID | Restaurant | Items | Amount paid |
|---|---|---|---|---|---|---|
| `swiggy-food-2026` | Mar 2026 → | `BILL DETAILS` | `Order ID: <span>N</span>` | first `<p>` after `ORDER JOURNEY` | `<name> x<qty>` + line total | `Paid Via …` row |
| `swiggy-food-legacy` | 2024 → 2025 | `Item Name` + `Order Total` | `Order No:` `<h5>`/`<strong>` | `<p>Restaurant</p><h5>…</h5>`, else `Ordered from:` | 3 columns: name, quantity, line total | `Order Total:` row |

**`swiggy-food-2026`** reads the bill table after the `BILL DETAILS` heading in this order: items, a blank divider row, charges, a blank divider row, `Paid Via`. Within it:
- `<!-- Non-MRO / MRO -->` comments appear only from about Sep 2026 and are used only to spot multi-restaurant orders, which are rejected until a sample has been seen.
- One-cell add-on lines under an item (Gourmet, e.g. `With Cheese Slice (₹30)`) are skipped; their cost is already in the item price.
- Emails from about 13–26 Jul 2026 add `Item Total`, a subtotal check, and `Line Separator`, a divider whose amount is a bare `₹` and which is skipped.
- Charge labels seen: `Restaurant Packaging`, `Platform fee with GST`, `Delivery Fee (FREE with Swiggy One) | 5.3 kms`, `Express Fee`, `Discount Applied (<COUPON>)`, `Extra 13% off (with Swiggy One)`, `Taxes`.
- The "₹N saved on this order" block at the top is ignored, because it repeats the discount lines.

**`swiggy-food-legacy`:**
- Trailing colons are removed from labels.
- `Paid Via …:` rows are skipped. A payment split across two methods has several of them, and `Order Total` is the amount paid.
- Charge labels seen: `Order Packing Charges`, `Platform fee`, `Delivery partner fee`, `Discount Applied (…)`, `Extra 10% off (with Swiggy One)`, `Taxes`.

### 4.3 Instamart (`parsers/instamart.py`, template `instamart-2026`)

The HTML is malformed (`<table>` directly inside `<tr>`), so it is read with a lenient row collector built on `html.parser`, which keeps rows in document order.
- **Order ID:** `Your Instamart order id: <span>N</span>`.
- **Items:** rows between the `Order Items` and `Order Summary` headings, written `<qty> x <name>` (the quantity comes first) with the line total.
- **Summary:**
  - `Item Bill` is a subtotal check;
  - `Grand Total` is `bill_total`;
  - every other row is a charge: `Handling Fee`, `Convenience Fee` and `Delivery Partner Fee` have been seen.

### 4.3a Fuel (`parsers/rbl_card.py`, template `rbl-card-alert`)

**Emails:** RBL Bank credit-card alerts from `RBLAlerts@rbl.bank.in`, subject "Alert: Your RBL Bank Credit Card has just been swiped". The config term `has just been swiped` is sent to Gmail as a quoted phrase.

**Body,** read from the visible text:
`INR3,500.00 spent at SRI EKADANTHA FUEL STA on RBL Bank credit card (0533) on 09-10-2026.AVL limit- INR40,500.00…`

**Each alert becomes one row:**
- the merchant is the station (`SRI EKADANTHA FUEL STA`);
- one item `Diesel`, quantity 1, category `fuel`, set by the parser, so the model never sees it;
- the bill total is the amount.

**Ignored:** the card digits and available limit are never stored.

**Order id:** an alert has no order number, so the Gmail message id is used, one alert per swipe.

**Order time:** as for the other sources, the email's `internalDate`. The alert's own date (`09-10-2026`) is a day-level cross-check.

**Fuel only:** the card can be used anywhere, so the source's `merchant_include` list keeps only merchants containing words like `FUEL`, `PETROL`, `HPCL`, `IOCL`, `BPCL` or `SHELL`. Other swipes are counted as `not_tracked` in the prepare summary and logged. They're not queued for review, because they aren't parse failures. To track every swipe on the card, set `merchant_include` to `[""]`. A station whose name contains none of the words is skipped: add a word to the list, then run `prepare --since` to pick up the missed fill-ups.

### 4.4 Test fixtures (`scripts/tests/fixtures/`), from real emails

Customer details in the fixtures are replaced with placeholders.

| File | Gmail message | Covers |
|---|---|---|
| `swiggy/sample_a.html` | `1a0dcc24632ebfef` | 2026 layout, discount, `FREE` delivery |
| `swiggy/sample_b.html` | `1a0e22ddc9778e18` | free item, qty 2, two discounts |
| `swiggy/2026_07_no_markers.html` | `19f1eca8d2ea8d99` | 2026 layout without comments, `₹45 FREE` |
| `swiggy/2026_07_gourmet_addons.html` | `19f1e0305486e050` | Gourmet add-on lines |
| `swiggy/2026_07_item_total_separator.html` | `19f5bd5d327b7403` | `Item Total` + `Line Separator` (bill rows as dumped on asgard) |
| `swiggy/2026_07_item_total_discount.html` | `19f9e7d0a6746d87` | same, with discount |
| `swiggy/2025_09_legacy.html` | `199853d0aa11179f` | legacy, 3 items, two discounts |
| `swiggy/2024_09_legacy.html` | `19234e4b13bf83f4` | legacy, single item |
| `rbl/fuel_swipe.html` | alert text from the user (HTML wrapper is a stand-in) | diesel swipe, amount with comma |
| `instamart/sample_c.html` | `19e0da013f37d0a2` | Instamart, fees, `Item Bill` |

---

## 5. Charge allocation (`scripts/allocate.py`)

Everything that isn't an item (taxes, fees, packaging, tips and discounts) is spread across the items **in proportion to each item's `line_price`**:

```
extra         = bill_total − Σ line_price          (negative when discounts exceed fees)
final_price_i = line_price_i + extra × line_price_i / Σ line_price
```

The split is done in paise with **largest-remainder rounding**, so `Σ final_price == bill_total` exactly.

| Example | Items | Paid | Final prices |
|---|---|---|---|
| Spec | 100, 200 | 360 | 120, 240 |
| Sample A | 148, 110 | 239 | 137.10, 101.90 |
| Sample B | 298, 239, 0 (free), 259 | 660 | 247.08, 198.17, 0.00, 214.75 |

**Edge cases:**
- A free item gets no share of the charges.
- If every item is free, the total is split equally.
- A single item takes the whole bill.
- A repeated item name stays as separate rows, distinguished by `line_no`.

---

## 6. Cursor and deduplication

### 6.1 The cursor comes from the sheet
For each source, on each `prepare`, the script reads columns A–D of the tab.
- **Cursor:** the latest `order_datetime` in the tab (`"cursor": "latest_in_sheet"`).
- **Empty tab** (or none at all): `today 00:00 IST − initial_backfill_days`, which is 90 days (`"cursor": "empty_sheet_90_days"`).
- **Manual backfill:** `prepare --since YYYY-MM-DD [--until YYYY-MM-DD]` overrides the cursor (`"cursor": "--since"`).

The sheet is the only record of progress; the script keeps no other cursor. Column A is read unformatted, as a serial date rounded to the second, so changing how the column is displayed doesn't move the cursor.

### 6.2 Fetch window: cursor → now
- **Query:** `after:<cursor epoch − 1>` with no upper bound.
- **Epoch seconds, not dates:** Gmail reads `after:YYYY/MM/DD` in US Pacific time, which is up to 13.5 hours off IST.
- **The `− 1`** makes the bound inclusive. The order sitting exactly at the cursor is fetched again and then dropped by dedup; normally that is the only email read twice.
- **Same clock:** the order time is Gmail's `internalDate`, the clock `after:` filters on, so the cursor and the search agree.

### 6.3 No duplicates
- **Key:** `order_id`, per tab. Each order is all-or-nothing: either all of its rows are in the sheet, or none are.
- **Checked three times:**
  - against the sheet in `prepare`;
  - within the run, since Swiggy can send two "delivered" emails for one order;
  - against the sheet again in `commit`, right before appending.
- **Append only.**
  - During a sync, gog may only run `sheets.metadata`, `sheets.get`, `sheets.append` and `sheets.add-tab`. Appends use `--insert INSERT_ROWS`, so nothing in the sheet is overwritten.
  - Rows go out in batches of up to 500 that never split an order. A failed append leaves only whole orders, and the next `commit` writes the rest.
- **Failure handling:**
  - If a source fails in `prepare` (fetch error, rate limit after all retries, or a possibly truncated search), nothing is staged for it. Its cursor can't move past an email that was never read.
  - If `commit` fails, the staged file is kept, so running `commit` again carries on from what is actually in the sheet.

### 6.4 Don't type into the data tabs
The cursor and dedup read every row in A–D. A stray date there could move the cursor and cause orders to be skipped. Keep notes, formulas and pivots in other tabs.

---

## 7. Sheet schema and item enrichment

Tabs `Swiggy`, `Instamart` and `Fuel` share one schema. A missing tab is created, and a missing header row is added on the next write.

- **Tab names are matched ignoring case**, as Google Sheets itself does. Renaming `Swiggy` to `swiggy` in the sheet changes nothing: the script finds the existing tab and its cursor, and never creates a second one.
- **Quoting:** names with spaces or punctuation are quoted in ranges (`'Food Orders'!A:N`).

| Col | Header | Example |
|---|---|---|
| A | `order_datetime` (IST) | `2026-09-26 13:38:34` |
| B | `order_id` (text) | `249377834139184` |
| C | `merchant` | `Amul Ice cream parlour` |
| D | `line_no` | `1` |
| E | `item_name` (minimal standard name from the catalog) | `Milkshake` |
| F | `category` | `beverages` |
| G | `quantity` | `1` |
| H | `item_price` (line total before charges) | `148.00` |
| I | `final_price` (after allocation) | `137.10` |
| J | `bill_total` | `239.00` |
| K | `source` | `swiggy` |
| L | `raw_item_name` (as on the bill, without the quantity) | `Crunchy Butterscotch Milkshake [350ml]` |
| M | `gmail_message_id` (text) | `1a0dcc24632ebfef` |
| N | `ingested_at` (IST) | `2026-10-02 16:03:33` |

- **Text cells:** IDs are written with a leading `'` so they stay text instead of becoming `2.49E+14`. Any text starting with `=`, `+`, `-` or `@` gets one too, so it can never become a formula.
- **Quantity:** `6 x Coca-Cola Zero Sugar Diet Drink` is **one** row with `quantity` 6, never 6 rows.

**Item enrichment (the model's job):**
- **Catalog:** `assets/catalog.json` in the skill folder. Repeat orders reuse the stored answer with no model call, so the same item is always categorised the same way.
  - **Format,** made for hand edits: a list of products sorted by category, `[{"name", "category", "raw": [bill names…]}, …]`. Raw names are matched ignoring case and spacing. Renaming or recategorising a product is one edit; merging is moving raw names between lists.
  - **Updated by the sync** whenever `enrich` accepts answers (and by `set-item` / `merge-items`). Writes are atomic (temp file + rename). Hand edits should be made while no run is going, since a run that saves rewrites the file.
  - **Checked before every command** (`enrich.check`): bad JSON or shape, a category not in `config.json`, or one name in two categories stops the command with `status: error` and `problems`, and `status` reports `catalog_error`. Nothing is written until it's fixed.
  - **Migration:** earlier versions kept `~/.expense-tracker/item_catalog.json` (flat `{raw: {name, category}}`, still readable). Its entries are merged in where `assets/catalog.json` has none, and on the first save it's renamed to `item_catalog.json.migrated`.
  - **Shipped seed:** the Oct 2026 version was built from the sheet itself (column L → E/F), with the audit corrections applied and every name reduced to its minimal standard name (126 → 69 products). Groups chosen by the user: every ice-cream dessert (sundae, gudbud, McFlurry, gelato) is `Ice Cream`; naan, roti and garlic bread are `Bread`.
- **Canonical names** are minimal standard names, the kind of dish or product (`Sandwich`, `Burger`, `Pizza`, `Maggi`, `Momos`, `Rice`, `Milkshake`, `Milk`), so many raw names share one (`Butterscotch Thickshake`, `Ferrero Rocher Shake (400ml)` → `Milkshake`). Brands, flavours, fillings and sizes stay visible in `raw_item_name` (L).
  - Each `to_enrich` item carries `similar`: up to 5 existing names that share a meaningful word with it. Common words such as `veg`, `special`, `classic`, sizes and numbers don't count, and singular and plural count as the same word (`Momo` finds `Momos`).
  - **Ranking:** most shared words first; then a match on a later word in `raw` first, since the dish type is usually the last word (`Paneer Tikka Grilled Sandwich` is offered `Sandwich` before `Tikka`); then shorter names.
  - Some variants share no word with their standard name (`Thickshake` / `Milkshake`, `Maggie` / `Maggi`). SKILL.md therefore lists the common standard names, and `references/swiggy.md` lists all of them, because the model may reuse **any** existing name, not only those in `similar`.
  - The model reuses one exactly when it's the same product. A reused name always takes its existing category, so a product can't be split across categories.
- **Checks** (`scripts/enrich.py`), applied to each answer separately:
  - `raw` matches a staged item;
  - `category` is in `assets/config.json`;
  - `name` is non-empty and at most 60 characters;
  - `name` contains no quantity, size or pack count (`6 x`, `x2`, `350ml`, `1 kg`, `pack of`);
  - every word in `name` appears in `raw` (words may only be removed, which rules out invented or misspelled names), **unless** `name` is an existing name (case-insensitive), or one created by another answer in the same batch.
- **Fallback:** an item rejected in 2 rounds, or skipped with `commit --skip-enrichment`, is written with its raw name and `uncategorized`. Fix them with `set-item` + `apply-catalog` (below).
- **Guidance for the model:** naming and category examples live in `references/swiggy.md` and `references/instamart.md`, including counter-examples from the Oct 2026 audit: reuse a name only for the *same dish* (a vada pav is not a burger), shakes are `beverages`, starters are `snacks`, fresh produce is `fruits_vegetables` whatever the brand.
- **Maintenance** (for you, never the model):
  - `sync.py items` lists each canonical name with its category and raw names.
  - `sync.py merge-items NAME… --into NAME` maps all raw names of `NAME…` onto an existing name and its category.
  - `sync.py set-item RAW --name NAME --category CAT` sets one raw name directly, with no word rules (a person may add or correct words). Every other raw name already using `NAME` follows the new category, so a product stays in one category. `set-item --from-file FILE` applies a JSON list `[{"raw","name","category"}, …]` all-or-nothing.
  - Or edit `assets/catalog.json` by hand.
  - These commands change the catalog, which applies to rows written afterwards.
  - **`sync.py apply-catalog [--dry-run] [--source S]`** brings rows already in the sheet in line with the catalog. For every row whose `raw_item_name` (L) has a catalog entry and whose E/F differ, it rewrites exactly those two cells with one `updateCells` request per row (`sheets batch-request`). Other columns, row order and rows with no catalog entry (fuel) are untouched, and it refuses to run if row 1 isn't the expected header. The cursor and dedup only read A–D, so this never affects syncing.

---

## 8. Skill and operations

### 8.1 Layout
OpenClaw only accepts support files under `assets/`, `examples/`, `references/`, `scripts/` and `templates/`, so `SKILL.md` is the only file at the top level.

```
expense-tracker/
├── SKILL.md              # the model's workflow; frontmatter mirrors the gog skill (💰, requires gog + python3, brew install for gog)
├── assets/
│   ├── config.json       # sources, spreadsheet, categories, backfill days
│   └── catalog.json      # item catalog: name, category, bill names per product (sync updates it; hand-editable)
├── references/
│   ├── swiggy.md         # Swiggy bill anatomy + naming/category guidance (read by the model)
│   ├── instamart.md      # Instamart bill anatomy + naming/category guidance (read by the model)
│   ├── README.md         # high-level overview
│   └── SPEC.md           # this document
└── scripts/
    ├── sync.py           # CLI: prepare, enrich, commit, status, items, merge-items, set-item, apply-catalog
    ├── gog_client.py     # gog wrapper: allowlist, read-only Gmail, rate-limit retries
    ├── allocate.py       # proportional allocation
    ├── enrich.py         # item catalog, similar names, answer checks
    ├── store.py          # review queue
    ├── build_dashboard.py # one-off: Dashboard tab (charts + KPIs) from live formulas, §8.4
    ├── parsers/          # common.py, swiggy.py, instamart.py, rbl_card.py
    └── tests/            # unit + end-to-end tests with a fake gog; fixtures from real emails
```

### 8.2 Run flow
Each command prints exactly one JSON line on stdout. Details go to `~/.expense-tracker/logs/`.

1. **`sync.py prepare [--since D] [--until D] [--source S]`**
   - Searches Gmail, parses new bills, dedups them and stages them in `~/.expense-tracker/staged.json`. It writes nothing to the sheet.
   - **Status** is `nothing_new`, `ready_to_commit` or `needs_enrichment`. With `needs_enrichment`, it also prints `to_enrich` (up to **10** unseen item names, each with its `similar` existing names), `to_enrich_remaining` and `categories`.
2. **`sync.py enrich`**, with a JSON list `[{"raw","name","category"}, …]` on stdin (heredoc) or in `--enrichment-json`.
   - **Why stdin:** many names contain apostrophes (`McDonald's`, `Lay's`), which break a single-quoted shell argument. On asgard this sent gemma into an endless `enrich` loop.
   - **Lenient parsing:** code fences, prose around the list, trailing commas and a single bare object are all accepted.
   - Saves the answers that pass the checks and prints the next batch. Rejected items come back in that batch with their `problems`.
   - **Guaranteed to finish:**
     - every call is one round, and each item in the shown batch that is still unnamed afterwards (wrong, missing, or unreadable JSON) uses up one attempt;
     - after 2 attempts the item drops out and is written as `uncategorized`;
     - a run stops handing out batches after **25 rounds**;
     - attempt counts carry over if `prepare` is run again.
   - **Batches of 10:** the first 90-day run had about 160 new items, too many for a 12B model in one answer.
3. **`sync.py status`**: read-only, see §8.3. **`items`, `merge-items`, `set-item`, `apply-catalog`** (for you, not the model): review and correct names and categories, see §7.
4. **`sync.py commit [--skip-enrichment] [--dry-run]`**
   - Re-checks dedup and appends the rows.
   - Refuses with `needs_enrichment` (exit 2) while items still await answers, unless `--skip-enrichment` is given.
   - `--dry-run` prints the rows to stderr and writes nothing.

**Exit codes:**
- `0`: OK, including runs that queued some emails or where one source failed.
- `1`: a fatal error, or every source failed.
- `2`: a model answer or command needs fixing.

### 8.3 Operations
- **Schedule:** daily. A missed day is caught up automatically, since the cursor comes from the sheet.
- **Locking:** an `flock` on `~/.expense-tracker/sync.lock` stops runs from overlapping.
  - **Released by the OS when the holder exits,** even after a crash or kill, so a leftover file never blocks anything. While a run holds the lock, the file records its `pid`, `command`, `argv` and `started_at`.
  - **A second call waits** for the lock, for up to 20 minutes. OpenClaw moves long commands (the first `prepare`) to the background, and the model may then run the same command again.
  - **Joining** (`prepare` and `commit` only; `enrich` reads its answers from stdin, so equal argv doesn't mean an equal call): if the holder was the identical command (same argv) and saved its result to `~/.expense-tracker/last_<command>.json`, the waiting call prints that result with `"joined_running_run": true` instead of repeating the work.
  - **Other commands** (or a holder that died without saving a result) run normally once the lock is free.
  - **Timeout:** only after 20 minutes does a call print `status: skipped`, with the holder's details.
- **`sync.py status`:** read-only and lock-free. Reports `running` (the lock holder, or null) and any staged work: the current `to_enrich` batch, or `ready_to_commit`. Use it to resume after losing track of a backgrounded run. SKILL.md also routes a user's request for "status" or "progress" to this command alone, so asking never starts a sync.
- **Review queue** (`~/.expense-tracker/pending/<message_id>.json`):
  - An email that fails parsing is saved here with its HTML and error, then excluded from later fetches.
  - Every `prepare` retries the queue with the current parsers. A recovered email is staged and leaves the queue at `commit`, even if the cursor has already moved past it.
  - `pending_review` in the output counts what is still stuck.
- **gog usage:** every call passes `-a <account> --json --no-input --enable-commands-exact <allowlist>`.
  - **Gmail:** may only run `gmail.search`, `gmail.get` and `gmail.thread.get`, always with `--readonly --gmail-no-send`. The mailbox can't be changed.
  - **Sheets:** the sync may only run `metadata`, `get`, `append` and `add-tab`, so it can't clear, update or delete. Only `apply-catalog` gets `metadata` + `batch-request`, and builds nothing but `updateCells` on columns E–F.
  - **Search:** `--all --max 500`. Exactly 500 results fails the source rather than risking a truncated window. To backfill a longer period, use `--since/--until` windows, oldest first.
  - **Threads:** a thread with `messageCount > 1` is expanded with `gmail thread get`, because Gmail groups orders with the same subject.
- **Rate limits** (Gmail quota is per minute, per Cloud project):
  - Only `rateLimitExceeded` / quota errors are retried, after waiting 15, 30, 60, 60 and then 60 seconds. A rejected call did nothing, so a retried append is safe.
  - Timeouts are never retried, because an append that timed out may have been written.
  - There is at least 0.3 seconds between calls.
- **Common errors** (`error` / `failed_sources` in the output):

  | Error contains | Meaning | Action |
  |---|---|---|
  | `auth` / `token` / `401` / `invalid_grant` | gog login expired | re-authenticate gog on asgard |
  | `rateLimitExceeded` after retries | quota exhausted for minutes | run again later; raise the Gmail API quota for gog's Cloud project if frequent |
  | `results may be truncated` | ≥ 500 emails in the window | backfill with `prepare --since A --until B`, oldest first |
  | `command not enabled` | allowlist blocked a call | script bug |
  | `Unable to parse range` | tab name wrong | check `sheet_tab` in `assets/config.json` |

---

### 8.4 Dashboard (`scripts/build_dashboard.py`)

**When to run it:** once, by hand, and never by the model. Re-run it only after adding a source or to change the layout.

**How it stays current:** every number is a live formula over the source tabs, so the dashboard follows new rows on its own.

**Helper tab `_data` (hidden):**
- **Columns A–L:** one normalised row per item from all source tabs (`VSTACK` of `<tab>!A2:N`, open-ended). Text and real dates in `order_datetime` are both handled.
- **Column M:** category folded to the top 7 plus "other".
- **To the right:** small summary tables, laid out automatically from `assets/config.json`.
- **Per-source columns** are generated in config order with `SUMIFS` / `COUNTUNIQUEIFS`. Pivots are avoided here, so a source keeps its column and its colour even while it has no data.

**Charts and tiles:**
- **KPI tiles:** total spent, orders, average per order, this month, last month, last 7 days, top category.
- **Moving averages, 7- and 30-day:** daily spend on calendar days (no-order days count as 0), with one line per source plus Total. A value appears only once a full window of history exists.
- **Monthly charts:** spend by source, spend by category (top 7 + other), orders by source.
- **Swiggy restaurants:** top 10 by orders, a pie of order share (top 7 + Others), top 10 by spend.
- **Other charts:** top 12 items, spend by hour of day, spend by weekday, fuel per month.

**Colours** (validated for colour-blind readers): Swiggy orange, Instamart blue, Fuel yellow, Total aqua, the same in every chart. Single-series charts use blue.

**Safety:**
- the source tabs are never written; a missing one is created empty, so the formulas don't break;
- a re-run deletes only charts whose titles it owns;
- capacity is 20,000 rows.

## 9. Testing

`python3 -m unittest discover -s scripts/tests` runs offline with an in-memory fake gog. The tests cover:
- **Parsers:** every fixture in §4.4, plus the amount formats and the mismatch, unknown-layout and multi-restaurant (MRO) rejections.
- **Allocation:** the examples in §5, the edge cases and the rounding.
- **Cursor and dedup:**
  - an empty tab starts 90 days back;
  - a second run starts at the latest row, re-reads one email and writes nothing;
  - a new order is added exactly once;
  - a shared thread yields every order in it;
  - a fetch error stages nothing;
  - a failed append leaves whole orders only and the retry completes;
  - a missing header is re-added;
  - `--until` bounds the window.
- **Enrichment:** batching, partial acceptance, giving up after two rounds, termination on unreadable answers, lenient JSON parsing, attempt counts surviving a re-run of `prepare`, the round cap, a real heredoc call with apostrophes, `--skip-enrichment`, and the name and category checks. Also: `similar` suggestions (plural matching, the dish word ranked first), reusing an existing name (its category wins), sharing a new name within one batch, `items` and `merge-items`.
- **Catalog file:** the shipped `assets/catalog.json` passes the checks; the product-list format round-trips; a hand rename applies to every raw name; bad JSON, unknown categories and split categories are reported and block commands (`status` → `catalog_error`); a legacy catalog is merged once, with the new file winning.
- **Corrections:** `set-item` moves every raw name of a product to the new category; `--from-file` is all-or-nothing; `apply-catalog` changes only E–F of rows that differ, `--dry-run` writes nothing, and an unexpected header stops it.
- **gog:** the real JSON shapes captured on asgard, the truncation guard, rate-limit retries and pacing.
- **Fuel:** the real alert text, spacing and amount variants, non-fuel swipes skipped as `not_tracked`, rows written without the model, re-run adds nothing, quoted subject phrase.
- **Review queue:** queue, don't re-fetch, recover after a parser fix, and the `pending_review` count.
- **Locking and `status`:**
  - a second call is told who holds the lock;
  - re-running the identical command joins it and returns its result;
  - a different command waits and then runs;
  - a holder that died without a result isn't joined;
  - the 20-minute timeout;
  - `status` resumes staged work.

---

## 10. Adding a source

1. Add an entry to `sources` in `assets/config.json`. Optionally add `merchant_include` (substring list) to keep only some merchants, as the fuel source does.
2. Add `scripts/parsers/<name>.py` returning the bill structure from §4, and register it in `parsers/__init__.py`. An item may carry its own `category` (as `Diesel`/`fuel` does); it then skips the model step. An empty `order_id` makes the Gmail message id the dedup key.
3. Add fixtures from real emails and tests.
4. If the model names its items, add a `references/<name>.md` with naming and category guidance and list it in SKILL.md. (Fuel needs none: the parser sets the name and category.)

Allocation, cursor, dedup, enrichment and sheet writing are shared and need no changes.

---

## 11. Open questions

1. **Instamart discounts:** no sample yet shows how a discount line looks. If it isn't a `- ₹N` row, those emails will go to the review queue rather than be mis-parsed.
2. **Gemma and the heredoc:** confirm on asgard that OpenClaw's exec tool passes the heredoc through a shell. If it doesn't, the answers could be written to a file and passed with `< file` instead.
