---
name: expense-tracker
description: "Daily sync of delivered Swiggy and Instamart orders, and diesel fill-ups paid with the RBL credit card, from Gmail into the Expense Tracker Google Sheet, one row per ordered item, with a clean item name and a category. Use when asked to sync or update expenses, on the daily schedule, or when asked for the expense tracker's status or progress."
metadata:
  {
    "openclaw":
      {
        "emoji": "💰",
        "requires": { "bins": ["gog", "python3"] },
        "install":
          [
            {
              "id": "brew",
              "kind": "brew",
              "formula": "gogcli",
              "bins": ["gog"],
              "label": "Install gog (brew)",
            },
          ],
      },
  }
---

# Expense Tracker

A script does the exact work: reading Gmail, parsing bills, splitting taxes/fees/discounts across items, skipping duplicates, writing the sheet. **Your job is only to give new items a clean name and a category.** Never calculate prices and never call `gog` yourself.

## Only asked for status?

If the user asks for the **status** or **progress** (not to sync), run only this, then answer and stop. Do **not** run `prepare`, `enrich` or `commit`.

```bash
python3 {baseDir}/scripts/sync.py status
```

It never waits and never changes anything. Report its `status`:

| status | tell the user |
|---|---|
| `idle` | Nothing in progress, nothing waiting. The next sync starts from the latest order in the sheet. |
| `running` | A sync is running: give `running.command`, `running.started_at` and `running.running_seconds`. |
| `needs_enrichment` | `new_bills` orders are fetched and waiting; items still need names (`to_enrich_remaining` + the current batch). Say a sync will continue from here. |
| `ready_to_commit` | `new_bills` orders are fetched and named, and only the sheet write is left. Say a sync will finish it. |
| `catalog_error` | The user's item catalog (`assets/catalog.json`) has a mistake: list `catalog_problems`. A sync won't run until they fix it. |

Also mention `pending_review` if it's above 0 (emails that couldn't be parsed and are waiting in the review queue). Then ask whether they want you to continue the sync.

## Steps

### 1. Prepare

```bash
python3 {baseDir}/scripts/sync.py prepare
```

It searches Gmail from the latest order already in each sheet tab up to now. If a tab is empty, it searches the last 90 days. Orders already in the sheet are skipped automatically.

**This can take several minutes** (the first run, or after a long gap). If your tool moves it to the background, wait for that process and read its output. Never kill it.
- If you run the same command again while it's still running, the new call waits and returns the same result, so that's safe.
- If you lose track, run `python3 {baseDir}/scripts/sync.py status`. It never waits, never changes anything, and tells you whether a run is still going (`running`), or gives the current `to_enrich` batch, or says `ready_to_commit`.

It prints one JSON line. Look at `status`:

| status | do this |
|---|---|
| `nothing_new` | Go to step 4. |
| `ready_to_commit` | Run `python3 {baseDir}/scripts/sync.py commit`, then go to step 4. |
| `needs_enrichment` | Go to step 2. |
| `skipped` | Another run held the lock for over 20 minutes. Report the `running` details and stop. |
| `error` | Report the `error` text exactly. Stop. |

### 2. Name and categorise each item in `to_enrich`

`to_enrich` lists up to 10 items never seen before. Each entry has `raw` (the name from the bill), `merchant`, `source` and `similar` (existing item names that look related, each with its category). For **every** entry produce:

- `raw`: copied **exactly**, character for character.
- `name`: the **minimal standard name**: the kind of dish or product, usually one or two words (`Sandwich`, `Burger`, `Pizza`, `Maggi`, `Momos`, `Dosa`, `Rice`, `Milkshake`, `Ice Cream`, `Milk`, `Chips`).
  - **First check `similar`.** If one of them is the standard name for this **same kind of dish or product** (not just a shared word or ingredient), use it **exactly**: `Butterscotch Thickshake` → `Milkshake`, `Veg Momo` → `Momos`. A reused name keeps its existing category automatically.
  - Otherwise make a new minimal name by **removing** words from `raw`: drop flavours, fillings, brands, sizes, quantities and marketing words. Never add, translate or correct words.
  - Never swap the dish type for a different one: a vada pav is not a `Burger`, a shake is not `Ice Cream`.
  - Standard names already in use, which you may always reuse exactly even if `similar` doesn't list them (`Veg Thickshake` → `Milkshake`, `Masala Maggie` → `Maggi`): `Milkshake`, `Chai`, `Lassi`, `Coca-Cola`, `Ice Cream` (also sundaes, gudbud), `Sandwich`, `Burger`, `Pizza`, `Pasta`, `Maggi`, `Noodles`, `Rice`, `Biryani`, `Dosa`, `Bread` (naan, roti, garlic bread), `Kulcha`, `Wrap`, `Roll`, `Soup`, `Momos`, `Samosa`, `Kachori`, `Kebab`, `Tikka`. More are listed in `{baseDir}/references/swiggy.md`.
- `category`: exactly one value from the `categories` list in the prepare output.

Examples (more in `{baseDir}/references/swiggy.md` and `{baseDir}/references/instamart.md`):

| raw | name | category |
|---|---|---|
| `Crunchy Butterscotch Milkshake [350ml]` | `Milkshake` | `beverages` |
| `Coca-Cola Zero Sugar Diet Drink` | `Coca-Cola` | `beverages` |
| `Butterscotch Thickshake` (similar: `Milkshake`) | `Milkshake` | `beverages` |
| `Bombay Masala Cheese Grill Sandwich` | `Sandwich` | `meals` |
| `Gold Flake Kings Blue Cigarette` | `Cigarette` | `tobacco` |

### 3. Save the batch, repeat, then commit

Send this batch's answers as one JSON list, **exactly in this heredoc form**. Names with apostrophes (`McDonald's`) are safe inside it; don't escape anything:

```bash
python3 {baseDir}/scripts/sync.py enrich <<'JSON'
[
  {"raw": "Crunchy Butterscotch Milkshake [350ml]", "name": "Milkshake", "category": "beverages"},
  {"raw": "McDonald's Big Spicy Paneer Wrap", "name": "Wrap", "category": "meals"}
]
JSON
```

Send one `enrich` per batch, then read its output. **Every `enrich` call moves forward**, even if your JSON was broken. The output has a `status`:

| status | do this |
|---|---|
| `needs_enrichment` | A new `to_enrich` batch is ready. If `problems` is present, the items it names are back in the batch: fix exactly what each problem says. Go back to step 2 for this batch. |
| `ready_to_commit` | Run `python3 {baseDir}/scripts/sync.py commit`, then go to step 4. |

`commit` prints `status: ok` when the rows are written. If it prints `needs_enrichment` instead, some items still need answers: go back to step 2 with its `to_enrich`. If it prints `nothing_to_commit`, there was nothing staged (no new orders); go to step 4.

Good answers are saved even when others in the same batch are rejected. An item that isn't accepted in 2 rounds is written with its original name and category `uncategorized`, and a run never takes more than 25 rounds. So the loop always ends in `ready_to_commit`: keep going until you see it. Don't run `prepare` again in the middle; it would only repeat the Gmail work. If you lose your place, `sync.py status` gives you the current batch.

### 4. Report

Keep it short:
- For each source: orders added (`new_bills`) and rows written (`rows_written`).
- How many new items you categorised (sum of `saved` across the enrich steps), and `items_uncategorized` from commit.
- If `pending_review` > 0: "N emails could not be parsed; see ~/.expense-tracker/pending/".
- Any `errors` codes with their counts.
- If `failed_sources` is present, name each failed source and quote its `error` from `sources`. That source will be retried on the next run; the other sources were still synced.

## Rules

- Run only the commands above. Don't edit files, don't call `gog`, and don't change prices.
- Never run `scripts/build_dashboard.py`, `sync.py set-item`, `sync.py merge-items` or `sync.py apply-catalog`, and never edit `assets/catalog.json`. They are the user's tools for layout and corrections; the dashboard updates itself from the sheet.
- If any command prints `"error": "assets/catalog.json needs fixing…"`, report its `problems` to the user and stop.
- Items in the sheet are **one row per bill line**. `6 x Coca-Cola Zero Sugar Diet Drink` is ONE row with `quantity` 6 and name `Coca-Cola Zero Sugar`. The script handles this; never put the quantity in `name`.
- Taxes, fees, packaging, tips and discounts are never rows. The script spreads them across items in proportion to price.
- Re-running is always safe. Orders already in the sheet are skipped.

## Reference

- `{baseDir}/references/swiggy.md`: what Swiggy food bills contain, plus naming and category examples for restaurant items.
- `{baseDir}/references/instamart.md`: the same for Instamart grocery bills.
