import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

import build_dashboard  # noqa: E402
import enrich  # noqa: E402
import store  # noqa: E402
import sync  # noqa: E402
from allocate import allocate  # noqa: E402
from parsers import instamart, rbl_card, swiggy  # noqa: E402
from gog_client import GogError  # noqa: E402
from parsers.common import ParseError, parse_amount  # noqa: E402

FIX = TESTS / "fixtures"


def fixture(path):
    return (FIX / path).read_text(encoding="utf-8")


class AllocateTest(unittest.TestCase):
    def test_spec_example(self):
        self.assertEqual(allocate([10000, 20000], 36000), [12000, 24000])

    def test_sample_a_discount_exceeds_fees(self):
        self.assertEqual(allocate([14800, 11000], 23900), [13710, 10190])

    def test_sample_b_free_item_gets_nothing(self):
        self.assertEqual(allocate([29800, 23900, 0, 25900], 66000), [24708, 19817, 0, 21475])

    def test_single_item_takes_whole_bill(self):
        self.assertEqual(allocate([24000], 26900), [26900])

    def test_indivisible_remainder_sums_exactly(self):
        out = allocate([100, 100, 100], 400)
        self.assertEqual(sum(out), 400)
        self.assertEqual(sorted(out), [133, 133, 134])

    def test_all_free_items_split_equally(self):
        self.assertEqual(allocate([0, 0], 1001), [501, 500])


class AmountTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_amount("₹148"), 14800)
        self.assertEqual(parse_amount("- ₹74.99"), -7499)
        self.assertEqual(parse_amount("FREE"), 0)
        self.assertEqual(parse_amount("₹1,240.50"), 124050)
        self.assertEqual(parse_amount("₹300\n₹250"), 25000)
        self.assertEqual(parse_amount("₹45 FREE"), 0)
        self.assertEqual(parse_amount("₹ 289 "), 28900)
        self.assertEqual(parse_amount("- ₹ 199.99"), -19999)
        with self.assertRaises(ParseError):
            parse_amount("₹45 extra")


class SwiggyParserTest(unittest.TestCase):
    def test_sample_a(self):
        bill = swiggy.parse(fixture("swiggy/sample_a.html"))
        self.assertEqual(bill["order_id"], "249377834139184")
        self.assertEqual(bill["merchant"], "Amul Ice cream parlour")
        self.assertEqual(bill["items"], [
            {"name": "Crunchy Butterscotch Milkshake [350ml]", "quantity": 1, "line_price": 14800},
            {"name": "Butterscotch Ice Cream", "quantity": 1, "line_price": 11000},
        ])
        self.assertEqual([c["amount"] for c in bill["charges"]], [1000, 1798, -7499, 0, 1900, 917])
        self.assertEqual(bill["bill_total"], 23900)

    def test_sample_b_quantity_and_free_item(self):
        bill = swiggy.parse(fixture("swiggy/sample_b.html"))
        self.assertEqual(bill["merchant"], "1947 Restaurant")
        self.assertEqual(bill["items"][0], {"name": "Garlic Naan", "quantity": 2, "line_price": 29800})
        self.assertEqual(bill["items"][2], {"name": "Brownie Cake", "quantity": 1, "line_price": 0})
        self.assertIn({"label": "Extra 13% off (with Swiggy One)", "amount": -10439}, bill["charges"])
        self.assertEqual(bill["bill_total"], 66000)

    def test_mro_items_rejected(self):
        html = fixture("swiggy/sample_b.html").replace(
            "<!-- MRO: items grouped by restaurant -->",
            "<!-- MRO: items grouped by restaurant --><tr><td>Other Dish x1</td><td></td><td>₹50</td></tr>",
        )
        with self.assertRaises(ParseError) as ctx:
            swiggy.parse(html)
        self.assertEqual(ctx.exception.code, "UNKNOWN_TEMPLATE")

    def test_total_mismatch(self):
        html = fixture("swiggy/sample_a.html").replace("<span>₹239</span>", "<span>₹500</span>")
        with self.assertRaises(ParseError) as ctx:
            swiggy.parse(html)
        self.assertEqual(ctx.exception.code, "TOTAL_MISMATCH")

    def test_other_template_rejected(self):
        with self.assertRaises(ParseError) as ctx:
            swiggy.parse(fixture("instamart/sample_c.html"))
        self.assertEqual(ctx.exception.code, "UNKNOWN_TEMPLATE")


class SwiggyHistoricalLayoutsTest(unittest.TestCase):
    """Real emails from asgard covering every Swiggy layout seen since 2024."""

    def test_2026_without_mro_comments(self):
        bill = swiggy.parse(fixture("swiggy/2026_07_no_markers.html"))
        self.assertEqual((bill["order_id"], bill["merchant"], bill["template_id"]),
                         ("241896192746774", "India Sweet House", "swiggy-food-2026"))
        self.assertEqual(bill["items"], [{"name": "Badam Milk", "quantity": 1, "line_price": 10476},
                                         {"name": "Ras Kadam 2 Pcs", "quantity": 1, "line_price": 10476}])
        self.assertEqual([c["amount"] for c in bill["charges"]], [1400, 1758, 0, 1048], "'₹45 FREE' is 0")
        self.assertEqual(bill["bill_total"], 25200)
        self.assertEqual(allocate([i["line_price"] for i in bill["items"]], bill["bill_total"]), [12600, 12600])

    def test_2026_gourmet_with_addons(self):
        bill = swiggy.parse(fixture("swiggy/2026_07_gourmet_addons.html"))
        self.assertEqual((bill["order_id"], bill["merchant"]), ("241882035947992", "Louis Burger"))
        self.assertEqual(bill["items"], [{"name": "Truffle Shroom Burger", "quantity": 1, "line_price": 47400}],
                         "add-on lines are part of the item price, not separate items")
        self.assertEqual([c["amount"] for c in bill["charges"]], [3000, 1758, -15000, 0, 1770])
        self.assertEqual(bill["bill_total"], 38900)

    def test_jul_2026_item_total_and_line_separator(self):
        bill = swiggy.parse(fixture("swiggy/2026_07_item_total_separator.html"))
        self.assertEqual(bill["items"], [{"name": "Garlic Khichdi", "quantity": 1, "line_price": 23500}])
        self.assertEqual([c["label"] for c in bill["charges"]],
                         ["Restaurant Packaging", "Platform fee with GST", "Delivery Fee | 9.3 kms", "Taxes"],
                         "Item Total is a subtotal and Line Separator a divider; neither is a charge")
        self.assertEqual(bill["bill_total"], 38200)

    def test_jul_2026_item_total_with_discount(self):
        bill = swiggy.parse(fixture("swiggy/2026_07_item_total_discount.html"))
        self.assertEqual([i["line_price"] for i in bill["items"]], [9500, 8900])
        self.assertEqual(sum(c["amount"] for c in bill["charges"]), 1000 + 1758 - 7499 + 0 + 1900 + 1068)
        self.assertEqual(allocate([9500, 8900], bill["bill_total"]), [8571, 8029])

    def test_jul_2026_item_total_mismatch(self):
        html = fixture("swiggy/2026_07_item_total_discount.html").replace("<span>₹184</span>", "<span>₹194</span>")
        with self.assertRaises(ParseError) as ctx:
            swiggy.parse(html)
        self.assertEqual(ctx.exception.code, "TOTAL_MISMATCH")

    def test_2025_legacy_with_discounts_and_quantity(self):
        bill = swiggy.parse(fixture("swiggy/2025_09_legacy.html"))
        self.assertEqual((bill["order_id"], bill["merchant"], bill["template_id"]),
                         ("217843577515937", "Helloo Delhi", "swiggy-food-legacy"))
        self.assertEqual(bill["items"], [
            {"name": "Paneer Lababdar", "quantity": 1, "line_price": 34000},
            {"name": "Garlic Butter Naan (Flour)", "quantity": 2, "line_price": 13000},
            {"name": "Hello Delhi Dal Makhani", "quantity": 1, "line_price": 34000}])
        self.assertEqual(bill["charges"], [
            {"label": "Order Packing Charges", "amount": 4400},
            {"label": "Platform Fee", "amount": 1499},
            {"label": "Delivery partner fee", "amount": 5000},
            {"label": "Discount Applied (CELEBRATIONS)", "amount": -19999},
            {"label": "Extra 10% off (with Swiggy One)", "amount": -6099},
            {"label": "Taxes", "amount": 3865}])
        self.assertEqual(bill["bill_total"], 69700)
        finals = allocate([i["line_price"] for i in bill["items"]], bill["bill_total"])
        self.assertEqual((sum(finals), finals), (69700, [29257, 11186, 29257]))

    def test_2024_legacy_single_item(self):
        bill = swiggy.parse(fixture("swiggy/2024_09_legacy.html"))
        self.assertEqual((bill["order_id"], bill["merchant"]), ("186430709428521", "Punjabi Swag"))
        self.assertEqual(bill["items"], [{"name": "Stuffed Mushroom Tikka (Sidhu Style )", "quantity": 1, "line_price": 28900}])
        self.assertEqual([c["label"] for c in bill["charges"]],
                         ["Order Packing Charges", "Platform fee", "Delivery partner fee", "Taxes"])
        self.assertEqual(bill["bill_total"], 35000)

    def test_legacy_split_payment_uses_order_total(self):
        html = fixture("swiggy/2024_09_legacy.html").replace(
            '<td class="small" width="80%" scope="row" colspan="2">Paid Via Cred:</td> <td width="20%"> ₹ 350.00</td>',
            '<td class="small" colspan="2">Paid Via Swiggy Money:</td> <td> ₹ 100.00</td> </tr> <tr> '
            '<td class="small" colspan="2">Paid Via Cred:</td> <td> ₹ 250.00</td>')
        self.assertEqual(swiggy.parse(html)["bill_total"], 35000)

    def test_legacy_item_total_mismatch(self):
        html = fixture("swiggy/2024_09_legacy.html").replace("₹ 289.00", "₹ 299.00")
        with self.assertRaises(ParseError) as ctx:
            swiggy.parse(html)
        self.assertEqual(ctx.exception.code, "TOTAL_MISMATCH")


class InstamartParserTest(unittest.TestCase):
    def test_sample_c(self):
        bill = instamart.parse(fixture("instamart/sample_c.html"))
        self.assertEqual(bill["order_id"], "237313777892504")
        self.assertEqual(bill["merchant"], "Instamart")
        self.assertEqual(bill["items"], [{"name": "Gold Flake Kings Blue Cigarette", "quantity": 1, "line_price": 24000}])
        self.assertEqual([c["label"] for c in bill["charges"]], ["Handling Fee", "Convenience Fee", "Delivery Partner Fee"])
        self.assertEqual(bill["bill_total"], 26900)

    def test_quantity_prefix_is_stripped_from_name(self):
        html = fixture("instamart/sample_c.html").replace(
            "1 x Gold Flake Kings Blue Cigarette", "6 x Coca-Cola Zero Sugar Diet Drink")
        item = instamart.parse(html)["items"][0]
        self.assertEqual(item["name"], "Coca-Cola Zero Sugar Diet Drink")
        self.assertEqual(item["quantity"], 6)

    def test_item_bill_mismatch(self):
        html = fixture("instamart/sample_c.html").replace(
            "Item Bill </td> <td width='50%' style='text-align: right;'><span style='font-size: 15px;'>₹240.00",
            "Item Bill </td> <td width='50%' style='text-align: right;'><span style='font-size: 15px;'>₹250.00")
        with self.assertRaises(ParseError) as ctx:
            instamart.parse(html)
        self.assertEqual(ctx.exception.code, "TOTAL_MISMATCH")




class RblCardParserTest(unittest.TestCase):
    def test_fuel_swipe(self):
        bill = rbl_card.parse(fixture("rbl/fuel_swipe.html"))
        self.assertEqual((bill["merchant"], bill["bill_total"], bill["order_id"]), ("SRI EKADANTHA FUEL STA", 350000, ""))
        self.assertEqual(bill["items"], [{"name": "Diesel", "quantity": 1, "line_price": 350000, "category": "fuel"}])

    def test_plain_text_and_spacing_variants(self):
        bill = rbl_card.parse("INR 1,234.5 spent at HPCL ABC SERVICE STN on RBL Bank credit card (XX0533) on 01-11-2026.")
        self.assertEqual((bill["merchant"], bill["bill_total"]), ("HPCL ABC SERVICE STN", 123450))

    def test_other_alert_rejected(self):
        with self.assertRaises(ParseError):
            rbl_card.parse("Your RBL Bank credit card statement is ready.")


class FakeGog:
    """In-memory stand-in for gog: one inbox, one spreadsheet. Search honours `after:<epoch>`
    the way Gmail does (strictly after), and messages can share a thread."""

    def __init__(self, messages):
        self.messages = messages
        self.tabs = {}
        self.queries = []
        self.fail_get = set()
        self.fail_append_after = None
        self.appends = 0

    def tab_titles(self, sid):
        return set(self.tabs)

    def read_values(self, sid, rng):
        rows = []
        for r in self.tabs.get(rng.split("!")[0], []):
            if r[1] == "order_id":
                rows.append(r[:4])
                continue
            ts = datetime.strptime(r[0], sync.DT_FMT)
            serial = (ts - sync.SHEETS_EPOCH).total_seconds() / 86400
            rows.append([serial, r[1].lstrip("'"), r[2], r[3]])
        return rows

    def add_tab(self, sid, tab):
        self.tabs[tab] = []

    def append_rows(self, sid, rng, rows):
        if self.fail_append_after is not None and self.appends >= self.fail_append_after:
            raise GogError("append failed")
        self.appends += 1
        self.tabs[rng.split("!")[0]].extend(rows)

    def search_threads(self, query):
        self.queries.append(query)
        after = int(query.rsplit("after:", 1)[1]) * 1000
        threads = {}
        for mid, m in self.messages.items():
            if int(m["message"]["internalDate"]) > after:
                tid = m["message"].get("threadId", mid)
                threads.setdefault(tid, []).append(mid)
        return [{"id": tid, "messageCount": len(ids)} for tid, ids in threads.items()]

    def thread_message_ids(self, tid):
        return [mid for mid, m in self.messages.items() if m["message"].get("threadId", mid) == tid]

    def get_message(self, mid):
        if mid in self.fail_get:
            raise GogError(f"get {mid} failed")
        return self.messages[mid]


def message(mid, sender, subject, body, epoch_ms):
    return {"headers": {"from": sender, "subject": subject, "date": ""},
            "body": body, "message": {"id": mid, "internalDate": str(epoch_ms)}}


CATEGORIES = ["meals", "snacks", "desserts", "beverages", "groceries", "tobacco", "other"]
CFG = {"spreadsheet_id": "sid", "initial_backfill_days": 90, "categories": CATEGORIES,
       "sources": [{"name": "swiggy", "sheet_tab": "swiggy", "parser": "swiggy",
                    "gmail_filters": {"from": "noreply@swiggy.in", "subject_contains": ["delivered"]}}]}
TODAY = date(2026, 10, 2)


def epoch_ist(text):
    return int(datetime.strptime(text, sync.DT_FMT).replace(tzinfo=sync.IST).timestamp())

ENRICH_A = [
    {"raw": "Crunchy Butterscotch Milkshake [350ml]", "name": "Butterscotch Milkshake", "category": "beverages"},
    {"raw": "Butterscotch Ice Cream", "name": "Butterscotch Ice Cream", "category": "desserts"},
]
ENRICH_B = [
    {"raw": "Garlic Naan", "name": "Garlic Naan", "category": "meals"},
    {"raw": "Dal Makhni", "name": "Dal Makhni", "category": "meals"},
    {"raw": "Brownie Cake", "name": "Brownie Cake", "category": "desserts"},
    {"raw": "Paneer Peshawari Masala", "name": "Paneer Peshawari Masala", "category": "meals"},
]


DIESEL = {"name": "fuel", "sheet_tab": "Fuel", "parser": "rbl_card",
          "gmail_filters": {"from": "RBLAlerts@rbl.bank.in", "subject_contains": ["has just been swiped"]},
          "merchant_include": ["FUEL", "PETROL", "HPCL"]}


class IsolatedStateTest(unittest.TestCase):
    def setUp(self):
        sync.log.disabled = True
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for mod, attr, value in ((store, "PENDING_DIR", root / "pending"),
                                 (sync, "STAGED_PATH", root / "staged.json"),
                                 (enrich, "CATALOG_PATH", root / "catalog.json"),
                                 (enrich, "LEGACY_CATALOG_PATH", root / "item_catalog.json")):
            self.addCleanup(setattr, mod, attr, getattr(mod, attr))
            setattr(mod, attr, value)


class SyncTest(IsolatedStateTest):
    def setUp(self):
        super().setUp()
        self.gog = FakeGog({
            "1a0dcc24632ebfef": message("1a0dcc24632ebfef", "Swiggy <noreply@swiggy.in>",
                                        "Your Swiggy order was delivered before time",
                                        fixture("swiggy/sample_a.html"), 1790410114000),
            "1a0e22ddc9778e18": message("1a0e22ddc9778e18", "Swiggy <noreply@swiggy.in>",
                                        "Your Swiggy order was delivered superfast",
                                        fixture("swiggy/sample_b.html"), 1790501051000),
            "spoofed": message("spoofed", "noreply@swiggy.in.evil.com", "Order delivered",
                               fixture("swiggy/sample_a.html"), 1790500051000),
        })

    def run_sync(self, enrichment):
        out, failed = sync.prepare(self.gog, CFG, None, None, TODAY)
        self.assertFalse(failed)
        if enrichment:
            self.enrich(enrichment)
        result, code = sync.commit(self.gog, CFG, False, False)
        return out, result, code

    def enrich(self, entries):
        return sync.enrich_step(CFG, json.dumps(entries))

    def test_first_run_backfills_enriches_and_writes(self):
        out, result, code = self.run_sync(ENRICH_A + ENRICH_B)
        self.assertEqual(out["status"], "needs_enrichment")
        self.assertEqual(out["sources"]["swiggy"]["since"], "2026-07-04 00:00:00")
        self.assertEqual(out["sources"]["swiggy"]["cursor"], "empty_sheet_90_days")
        self.assertIn(f"after:{epoch_ist('2026-07-04 00:00:00') - 1}", self.gog.queries[0])
        self.assertEqual(out["sources"]["swiggy"]["errors"], {"FILTER_REJECTED": 1})
        self.assertEqual(len(out["to_enrich"]), 6)
        self.assertEqual(out["categories"], CATEGORIES)
        self.assertEqual((code, result["sources"]["swiggy"]["rows_written"]), (0, 6))

        rows = self.gog.tabs["swiggy"]
        self.assertEqual(rows[0], sync.HEADER)
        first = dict(zip(sync.HEADER, rows[1]))
        self.assertEqual(first["order_datetime"], "2026-09-26 13:38:34")
        self.assertEqual(first["order_id"], "'249377834139184")
        self.assertEqual(first["item_name"], "Butterscotch Milkshake")
        self.assertEqual(first["raw_item_name"], "Crunchy Butterscotch Milkshake [350ml]")
        self.assertEqual(first["category"], "beverages")
        self.assertEqual(first["final_price"], 137.10)
        naan = dict(zip(sync.HEADER, rows[3]))
        self.assertEqual((naan["item_name"], naan["quantity"], naan["final_price"]), ("Garlic Naan", 2, 247.08))

    def test_second_run_writes_nothing_and_needs_no_model(self):
        self.run_sync(ENRICH_A + ENRICH_B)
        before = len(self.gog.tabs["swiggy"])
        out, _ = sync.prepare(self.gog, CFG, None, None, TODAY)
        self.assertEqual(out["status"], "nothing_new")
        self.assertEqual(out["sources"]["swiggy"]["emails_found"], 1, "only the email at the cursor is re-read")
        self.assertEqual(out["sources"]["swiggy"]["duplicates_skipped"], 1)
        self.assertEqual(out["sources"]["swiggy"]["cursor"], "latest_in_sheet")
        self.assertEqual(out["sources"]["swiggy"]["since"], "2026-09-27 14:54:11")
        self.assertIn(f"after:{epoch_ist('2026-09-27 14:54:11') - 1}", self.gog.queries[-1])
        self.assertEqual(sync.commit(self.gog, CFG, False, False)[0]["status"], "nothing_to_commit")
        self.assertEqual(len(self.gog.tabs["swiggy"]), before)

    def test_known_items_come_from_catalog(self):
        enrich.save_catalog({enrich.key(e["raw"]): {"name": e["name"], "category": e["category"]}
                             for e in ENRICH_A + ENRICH_B})
        out, result, code = self.run_sync(None)
        self.assertEqual(out["status"], "ready_to_commit")
        self.assertNotIn("to_enrich", out)
        self.assertEqual(result["sources"]["swiggy"]["rows_written"], 6)

    def test_commit_refuses_until_items_are_enriched_unless_skipped(self):
        sync.prepare(self.gog, CFG, None, None, TODAY)
        out, _ = self.enrich(ENRICH_A)
        self.assertEqual((out["status"], out["saved"]), ("needs_enrichment", 2))
        self.assertEqual(len(out["to_enrich"]), 4)
        result, code = sync.commit(self.gog, CFG, False, False)
        self.assertEqual((code, result["status"]), (2, "needs_enrichment"))
        self.assertEqual(self.gog.tabs, {})
        result, code = sync.commit(self.gog, CFG, True, False)
        self.assertEqual((code, result["items_uncategorized"]), (0, 4))
        naan = dict(zip(sync.HEADER, self.gog.tabs["swiggy"][3]))
        self.assertEqual((naan["item_name"], naan["category"]), ("Garlic Naan", "uncategorized"))

    def test_bad_entry_does_not_discard_good_ones_and_gives_up_after_two_rounds(self):
        sync.prepare(self.gog, CFG, None, None, TODAY)
        bad = ENRICH_A + ENRICH_B
        bad[0] = {**bad[0], "category": "drinks"}
        out, code = self.enrich(bad)
        self.assertEqual((code, out["saved"], out["status"]), (0, 5, "needs_enrichment"))
        self.assertIn("drinks", out["problems"][0])
        self.assertEqual([i["raw"] for i in out["to_enrich"]], [ENRICH_A[0]["raw"]], "only the rejected item comes back")
        out, _ = self.enrich([bad[0]])
        self.assertEqual((out["status"], out["given_up"]), ("ready_to_commit", 1))
        result, code = sync.commit(self.gog, CFG, False, False)
        self.assertEqual((code, result["items_uncategorized"]), (0, 1))
        shake = dict(zip(sync.HEADER, self.gog.tabs["swiggy"][1]))
        self.assertEqual((shake["item_name"], shake["category"]),
                         ("Crunchy Butterscotch Milkshake [350ml]", "uncategorized"))

    def test_items_are_handed_out_in_batches(self):
        self.addCleanup(setattr, sync, "ENRICH_BATCH", sync.ENRICH_BATCH)
        sync.ENRICH_BATCH = 4
        out, _ = sync.prepare(self.gog, CFG, None, None, TODAY)
        self.assertEqual((len(out["to_enrich"]), out["to_enrich_remaining"]), (4, 2))
        batch1 = {i["raw"] for i in out["to_enrich"]}
        out, _ = self.enrich([e for e in ENRICH_A + ENRICH_B if e["raw"] in batch1])
        self.assertEqual((len(out["to_enrich"]), out["to_enrich_remaining"]), (2, 0))
        out, _ = self.enrich([e for e in ENRICH_A + ENRICH_B if e["raw"] not in batch1])
        self.assertEqual(out["status"], "ready_to_commit")
        self.assertEqual(sync.commit(self.gog, CFG, False, False)[0]["sources"]["swiggy"]["rows_written"], 6)

    def test_dry_run_writes_nothing_to_the_sheet(self):
        sync.prepare(self.gog, CFG, None, None, TODAY)
        self.enrich(ENRICH_A + ENRICH_B)
        result, _ = sync.commit(self.gog, CFG, False, True)
        self.assertEqual(result["sources"]["swiggy"]["rows_pending"], 6)
        self.assertEqual(self.gog.tabs, {})
        self.assertTrue(sync.STAGED_PATH.exists(), "a real commit can follow the dry run")

    def test_unparseable_email_is_queued_then_recovered(self):
        self.gog.messages["1a0dcc24632ebfef"]["body"] = fixture("swiggy/sample_a.html").replace("BILL DETAILS", "YOUR BILL")
        out, _, _ = self.run_sync(ENRICH_B)
        self.assertEqual(out["sources"]["swiggy"]["errors"]["UNKNOWN_TEMPLATE"], 1)
        self.assertEqual(out["pending_review"], 1)

        out, _ = sync.prepare(self.gog, CFG, None, None, TODAY)
        self.assertEqual(out["pending_review"], 1, "queued email is not re-fetched or re-queued")

        rec = store.load("1a0dcc24632ebfef")
        rec["html"] = fixture("swiggy/sample_a.html")  # simulate an improved parser
        store.save(rec)
        out, result, _ = self.run_sync(ENRICH_A)
        self.assertEqual(out["new_bills"], 1)
        self.assertEqual(out["pending_review"], 0, "a recovered email is no longer pending")
        self.assertEqual(result["sources"]["swiggy"]["rows_written"], 2)
        self.assertFalse(store.exists("1a0dcc24632ebfef"))

    def test_new_order_after_cursor_is_added_once(self):
        self.run_sync(ENRICH_A + ENRICH_B)
        self.gog.messages["later"] = message("later", "noreply@swiggy.in", "Order delivered",
                                             fixture("swiggy/sample_a.html").replace("249377834139184", "250000000000001"),
                                             1790601051000)
        out, result, _ = self.run_sync(None)
        self.assertEqual((out["new_bills"], out["sources"]["swiggy"]["duplicates_skipped"]), (1, 1))
        self.assertEqual(result["sources"]["swiggy"]["rows_written"], 2)
        ids = [r[1] for r in self.gog.tabs["swiggy"][1:]]
        self.assertEqual(len(ids), 8)
        self.assertEqual(len(set((r[1], r[3]) for r in self.gog.tabs["swiggy"][1:])), 8, "no duplicate rows")

    def test_every_message_in_a_shared_thread_is_read(self):
        self.gog.messages["1a0e22ddc9778e18"]["message"]["threadId"] = "1a0dcc24632ebfef"
        out, result, _ = self.run_sync(ENRICH_A + ENRICH_B)
        self.assertEqual(out["new_bills"], 2)
        self.assertEqual(result["sources"]["swiggy"]["rows_written"], 6)

    def test_fetch_error_fails_source_without_moving_cursor(self):
        self.gog.fail_get.add("1a0dcc24632ebfef")
        out, all_failed = sync.prepare(self.gog, CFG, None, None, TODAY)
        self.assertTrue(all_failed)
        self.assertEqual(out["failed_sources"], ["swiggy"])
        self.assertEqual(out["sources"]["swiggy"]["status"], "error")
        self.assertEqual(out["status"], "nothing_new")
        self.assertFalse(sync.STAGED_PATH.exists(), "the newer order B must not be written ahead of A")
        self.gog.fail_get.clear()
        _, result, _ = self.run_sync(ENRICH_A + ENRICH_B)
        self.assertEqual(result["sources"]["swiggy"]["rows_written"], 6)

    def test_failed_write_never_leaves_half_an_order(self):
        sync.prepare(self.gog, CFG, None, None, TODAY)
        self.addCleanup(setattr, sync, "APPEND_CHUNK", sync.APPEND_CHUNK)
        sync.APPEND_CHUNK = 3
        self.gog.fail_append_after = 2  # header + order A succeed, order B's append fails
        self.enrich(ENRICH_A + ENRICH_B)
        with self.assertRaises(GogError):
            sync.commit(self.gog, CFG, False, False)
        self.assertEqual(len(self.gog.tabs["swiggy"]), 1 + 2, "order A complete, order B absent")
        self.gog.fail_append_after = None
        result, _ = sync.commit(self.gog, CFG, False, False)
        self.assertEqual((result["sources"]["swiggy"]["rows_written"], result["sources"]["swiggy"]["duplicates_skipped"]), (4, 1))
        rows = self.gog.tabs["swiggy"]
        self.assertEqual([r[1] for r in rows].count("order_id"), 1, "header written once")
        self.assertEqual(len(rows), 1 + 6)

    def test_header_added_to_existing_empty_tab(self):
        self.gog.tabs["swiggy"] = []
        self.run_sync(ENRICH_A + ENRICH_B)
        self.assertEqual(self.gog.tabs["swiggy"][0], sync.HEADER)
        self.assertEqual(len(self.gog.tabs["swiggy"]), 7)

    def test_until_bounds_the_window(self):
        q = sync.gmail_query("noreply@swiggy.in", ["delivered"], datetime(2026, 7, 1), datetime(2026, 8, 1))
        self.assertTrue(q.endswith(f"after:{epoch_ist('2026-07-01 00:00:00') - 1} before:{epoch_ist('2026-08-01 00:00:00')}"))

    def test_since_override(self):
        since, cursor = sync.sync_cursor(datetime(2026, 9, 27, 14, 37, 31), date(2026, 9, 1), TODAY, 90)
        self.assertEqual((since, cursor), (datetime(2026, 9, 1), "--since"))

    def test_tab_name_case_does_not_matter(self):
        self.run_sync(ENRICH_A + ENRICH_B)
        self.gog.tabs = {"Swiggy": self.gog.tabs.pop("swiggy")}  # user capitalised the tab
        before = len(self.gog.tabs["Swiggy"])
        self.gog.messages["later"] = message("later", "noreply@swiggy.in", "Order delivered",
                                             fixture("swiggy/sample_a.html").replace("249377834139184", "250000000000001"),
                                             1790601051000)
        out, result, _ = self.run_sync(None)
        self.assertEqual(out["sources"]["swiggy"]["cursor"], "latest_in_sheet", "existing rows still found")
        self.assertEqual(out["new_bills"], 1, "no 90-day re-backfill")
        self.assertEqual(set(self.gog.tabs), {"Swiggy"}, "no second tab created")
        self.assertEqual(len(self.gog.tabs["Swiggy"]), before + 2)

    def test_a1_quoting(self):
        self.assertEqual(sync.a1("Swiggy", "A:N"), "Swiggy!A:N")
        self.assertEqual(sync.a1("Food Orders", "A:N"), "'Food Orders'!A:N")
        self.assertEqual(sync.a1("Daman's", "A1:D"), "'Daman''s'!A1:D")

    def test_text_cells_cannot_become_formulas(self):
        self.assertEqual(sync.as_text("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertEqual(sync.as_text("Garlic Naan"), "Garlic Naan")


class SearchGuardTest(unittest.TestCase):
    def test_possible_truncation_fails_loudly(self):
        from gog_client import SEARCH_MAX, Gog
        gog = Gog("gog", "me")
        gog._run = lambda *a, **k: {"threads": [{"id": str(i)} for i in range(SEARCH_MAX)]}
        with self.assertRaises(GogError):
            gog.search_threads("q")
        gog._run = lambda *a, **k: {"threads": [{"id": "1"}]}
        self.assertEqual(len(gog.search_threads("q")), 1)

    def gog_returning(self, response):
        from gog_client import Gog
        gog = Gog("gog", "me")
        gog._run = lambda *a, **k: response
        return gog

    def test_thread_message_ids_real_shape(self):
        # Shape of `gog gmail thread get 19e0da013f37d0a2 --json` on asgard (trimmed).
        res = {"downloaded": None, "thread": {"historyId": "24972041", "id": "19e0da013f37d0a2", "messages": [
            {"historyId": "24972041", "id": "19e0da013f37d0a2", "internalDate": "1778345054000",
             "labelIds": ["UNREAD"], "payload": {"body": {}, "headers": []}},
            {"id": "19e0da013f37d0b9", "internalDate": "1778345999000", "payload": {}}]}}
        self.assertEqual(self.gog_returning(res).thread_message_ids("19e0da013f37d0a2"),
                         ["19e0da013f37d0a2", "19e0da013f37d0b9"])
        with self.assertRaises(GogError):
            self.gog_returning({"thread": {"messages": []}}).thread_message_ids("x")

    def test_tab_titles_real_shape(self):
        # Shape of `gog sheets metadata <id> --json` on asgard.
        res = {"dataSourceSchedules": None, "dataSources": None, "locale": "en_GB", "sheets": [
            {"properties": {"gridProperties": {"columnCount": 26, "rowCount": 1000}, "sheetId": 1807956049,
                            "sheetType": "GRID", "title": "dashboard"}},
            {"properties": {"gridProperties": {"columnCount": 26, "rowCount": 1000}, "index": 1,
                            "sheetType": "GRID", "title": "swiggy"}},
            {"properties": {"gridProperties": {"columnCount": 26, "rowCount": 1000}, "index": 2,
                            "sheetId": 304195808, "sheetType": "GRID", "title": "instamart"}}],
            "spreadsheetId": "1SKlTsjmB_XXH4swFjaN9KpqxoBU_ZetjMt4wsaLz8Lc", "timeZone": "Etc/GMT",
            "title": "Expense Tracker"}
        self.assertEqual(self.gog_returning(res).tab_titles("sid"), {"dashboard", "swiggy", "instamart"})

    def test_read_values_empty_tab_real_shape(self):
        # `gog sheets get <id> 'swiggy!A1:D' --render UNFORMATTED_VALUE --dimension ROWS --json` on an empty tab.
        gog = self.gog_returning({"range": "swiggy!A1:D1000", "values": []})
        self.assertEqual(gog.read_values("sid", "swiggy!A1:D"), [])

    def test_empty_existing_tab_gets_header_and_90_day_backfill(self):
        class Gog(FakeGog):
            pass
        fake = Gog({})
        fake.tabs = {"dashboard": [["x"]], "swiggy": [], "instamart": []}
        state = sync.read_sheet_state(fake, CFG, "swiggy")
        self.assertEqual((state.exists, state.has_header, state.last_ts), (True, False, None))
        self.assertEqual(sync.sync_cursor(state.last_ts, None, TODAY, 90)[1], "empty_sheet_90_days")


class RateLimitTest(unittest.TestCase):
    QUOTA = ("Google API error (403 rateLimitExceeded): Quota exceeded for quota metric 'Total Query Cost' "
             "and limit 'Units per minute per user' of service 'gmail.googleapis.com'")

    def run_with(self, results):
        import subprocess
        import gog_client
        calls, sleeps = [], []
        responses = iter(results)

        def fake_run(cmd, **kw):
            calls.append(cmd)
            code, out, err = next(responses)
            return subprocess.CompletedProcess(cmd, code, out, err)

        self.addCleanup(setattr, gog_client.subprocess, "run", gog_client.subprocess.run)
        gog_client.subprocess.run = fake_run
        gog = gog_client.Gog("gog", "me", sleep=sleeps.append, clock=lambda: 1000.0)
        return gog, calls, sleeps

    def test_rate_limit_is_retried_with_backoff(self):
        gog, calls, sleeps = self.run_with([(7, "", self.QUOTA), (7, "", self.QUOTA), (0, '{"threads": []}', "")])
        self.assertEqual(gog.search_threads("q"), [])
        self.assertEqual(len(calls), 3)
        self.assertEqual([s for s in sleeps if s >= 1], [15, 30])

    def test_gives_up_after_all_retries(self):
        gog, calls, _ = self.run_with([(7, "", self.QUOTA)] * 6)
        with self.assertRaises(GogError) as ctx:
            gog.search_threads("q")
        self.assertEqual(len(calls), 6)
        self.assertIn("rateLimitExceeded", str(ctx.exception))

    def test_other_errors_are_not_retried(self):
        gog, calls, _ = self.run_with([(1, "", "invalid_grant: token expired")])
        with self.assertRaises(GogError):
            gog.search_threads("q")
        self.assertEqual(len(calls), 1)

    def test_calls_are_paced(self):
        gog, _, sleeps = self.run_with([(0, '{"threads": []}', "")] * 2)
        gog.search_threads("q")
        gog.search_threads("q")
        self.assertEqual(sleeps, [0.3])


class EnrichLoopTerminationTest(IsolatedStateTest):
    """Whatever the model sends, the enrich loop must reach ready_to_commit."""

    def setUp(self):
        super().setUp()
        self.gog = FakeGog({"m1": message("m1", "noreply@swiggy.in", "Order delivered",
                                          fixture("swiggy/sample_b.html"), 1790410114000)})
        out, _ = sync.prepare(self.gog, CFG, None, None, TODAY)
        self.assertEqual(len(out["to_enrich"]), 4)

    def test_unreadable_answers_still_terminate(self):
        out, _ = sync.enrich_step(CFG, "Sure! Here are the items: [{oops")
        self.assertEqual(out["status"], "needs_enrichment")
        self.assertIn("invalid JSON", out["problems"][0])
        out, _ = sync.enrich_step(CFG, "")
        self.assertEqual((out["status"], out["given_up"]), ("ready_to_commit", 4))
        result, code = sync.commit(self.gog, CFG, False, False)
        self.assertEqual((code, result["items_uncategorized"]), (0, 4))

    def test_lenient_parsing(self):
        for text in ['```json\n[{"raw": "Garlic Naan", "name": "Garlic Naan", "category": "meals"},]\n```',
                     'Here you go: [{"raw": "Garlic Naan", "name": "Garlic Naan", "category": "meals"}] done',
                     '{"raw": "Garlic Naan", "name": "Garlic Naan", "category": "meals"}']:
            self.assertEqual(sync.parse_enrichment(text)[0]["name"], "Garlic Naan")
        with self.assertRaises(ValueError):
            sync.parse_enrichment("no json here")

    def test_rerunning_prepare_keeps_attempt_counts(self):
        sync.enrich_step(CFG, "garbage")
        sync.prepare(self.gog, CFG, None, None, TODAY)
        out, _ = sync.enrich_step(CFG, "garbage")
        self.assertEqual(out["status"], "ready_to_commit", "second failed round counts even across prepare runs")

    def test_round_cap(self):
        self.addCleanup(setattr, sync, "ENRICH_MAX_ROUNDS", sync.ENRICH_MAX_ROUNDS)
        self.addCleanup(setattr, sync, "ENRICH_ATTEMPTS", sync.ENRICH_ATTEMPTS)
        sync.ENRICH_MAX_ROUNDS, sync.ENRICH_ATTEMPTS = 1, 99
        out, _ = sync.enrich_step(CFG, "[]")
        self.assertEqual(out["status"], "ready_to_commit")


class EnrichCliTest(unittest.TestCase):
    """The real CLI, as OpenClaw runs it: answers on stdin via a heredoc, names with apostrophes."""

    def test_heredoc_with_apostrophes(self):
        import subprocess
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        state = home / ".expense-tracker"
        state.mkdir()
        staged = {"sources": {"swiggy": {"tab": "swiggy", "recovered": [], "bills": [{
            "order_id": "1", "merchant": "McDonald's", "message_id": "m", "ordered_at": "2026-10-01 10:00:00",
            "bill_total": 10000, "charges": [], "template_id": "t",
            "items": [{"name": "McDonald's Big Spicy Paneer Wrap", "quantity": 1, "line_price": 10000}]}]}},
            "enrich_attempts": {}, "enrich_batch": ["mcdonald's big spicy paneer wrap"]}
        (state / "staged.json").write_text(json.dumps(staged))
        script = (
            f"python3 {TESTS.parent / 'sync.py'} enrich <<'JSON'\n"
            '[{"raw": "McDonald\'s Big Spicy Paneer Wrap", "name": "McDonald\'s Big Spicy Paneer Wrap", "category": "meals"}]\n'
            "JSON\n")
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={**os.environ, "HOME": str(home)})
        out = json.loads(proc.stdout)
        self.assertEqual((out["status"], out["saved"]), ("ready_to_commit", 1), proc.stderr)


class LockAndStatusTest(IsolatedStateTest):
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, sync, "LOCK_PATH", sync.LOCK_PATH)
        sync.LOCK_PATH = sync.STAGED_PATH.parent / "sync.lock"

    def test_second_run_is_skipped_and_told_who_holds_the_lock(self):
        held = sync.acquire_lock("prepare", ["prepare"])
        self.assertIsNotNone(held)
        self.assertIsNone(sync.acquire_lock("prepare", ["prepare"]))
        holder = sync.lock_holder()
        self.assertEqual((holder["pid"], holder["command"]), (os.getpid(), "prepare"),
                         "a failed attempt must not wipe the holder's details")
        self.assertGreaterEqual(holder["running_seconds"], 0)
        self.assertEqual(sync.status(CATEGORIES)[0]["status"], "running")
        held.close()
        self.assertIsNone(sync.lock_holder(), "lock is released when its holder closes or exits")
        nxt = sync.acquire_lock("enrich", ["enrich"])
        self.assertIsNotNone(nxt)
        nxt.close()

    def _hold(self, command, argv, out=None):
        held = sync.acquire_lock(command, argv)
        if out is not None:
            sync.save_result(held.run, out, 0)
        return held

    def test_rerunning_the_same_command_joins_the_running_one(self):
        self.addCleanup(setattr, sync.store, "STATE_DIR", sync.store.STATE_DIR)
        sync.store.STATE_DIR = sync.LOCK_PATH.parent
        held = self._hold("prepare", ["prepare"], {"status": "needs_enrichment", "new_bills": 150})
        lock, joined = sync.lock_or_join("prepare", ["prepare"], sleep=lambda s: held.close(), clock=lambda: 0)
        self.assertIsNone(lock)
        self.assertEqual(joined["output"], {"status": "needs_enrichment", "new_bills": 150})
        self.assertIsNone(sync.lock_holder(), "joining leaves the lock free")

    def test_a_different_command_waits_then_runs(self):
        self.addCleanup(setattr, sync.store, "STATE_DIR", sync.store.STATE_DIR)
        sync.store.STATE_DIR = sync.LOCK_PATH.parent
        held = self._hold("prepare", ["prepare"], {"status": "needs_enrichment"})
        lock, joined = sync.lock_or_join("commit", ["commit"], sleep=lambda s: held.close(), clock=lambda: 0)
        self.assertIsNone(joined)
        self.assertEqual(lock.run["command"], "commit")
        lock.close()

    def test_enrich_is_never_joined(self):
        self.addCleanup(setattr, sync.store, "STATE_DIR", sync.store.STATE_DIR)
        sync.store.STATE_DIR = sync.LOCK_PATH.parent
        held = self._hold("enrich", ["enrich"], {"status": "needs_enrichment"})
        lock, joined = sync.lock_or_join("enrich", ["enrich"], sleep=lambda s: held.close(), clock=lambda: 0)
        self.assertIsNone(joined, "same argv, but the answers on stdin may differ")
        lock.close()

    def test_holder_that_crashed_without_a_result_is_not_joined(self):
        self.addCleanup(setattr, sync.store, "STATE_DIR", sync.store.STATE_DIR)
        sync.store.STATE_DIR = sync.LOCK_PATH.parent
        held = self._hold("prepare", ["prepare"])
        lock, joined = sync.lock_or_join("prepare", ["prepare"], sleep=lambda s: held.close(), clock=lambda: 0)
        self.assertIsNone(joined)
        self.assertIsNotNone(lock, "no saved result from that run, so this call does the work itself")
        lock.close()

    def test_gives_up_after_the_timeout(self):
        held = self._hold("prepare", ["prepare"])
        ticks = iter(range(0, 10_000, 600))
        lock, joined = sync.lock_or_join("prepare", ["prepare"], timeout=1200, sleep=lambda s: None,
                                         clock=lambda: next(ticks))
        self.assertEqual((lock, joined), (None, None))
        held.close()

    def test_status_resumes_staged_work(self):
        idle = sync.status(CATEGORIES)[0]
        self.assertEqual((idle["status"], idle["running"], idle["pending_review"]), ("idle", None, 0))
        gog = FakeGog({"m1": message("m1", "noreply@swiggy.in", "Order delivered",
                                     fixture("swiggy/sample_a.html"), 1790410114000)})
        sync.prepare(gog, CFG, None, None, TODAY)
        out = sync.status(CATEGORIES)[0]
        self.assertEqual((out["status"], out["new_bills"], len(out["to_enrich"])), ("needs_enrichment", 1, 2))
        sync.enrich_step(CFG, json.dumps(ENRICH_A))
        self.assertEqual(sync.status(CATEGORIES)[0]["status"], "ready_to_commit")


class CanonicalNamesTest(unittest.TestCase):
    CATALOG = {
        "crunchy butterscotch milkshake [350ml]": {"name": "Butterscotch Milkshake", "category": "beverages"},
        "butterscotch ice cream": {"name": "Butterscotch Ice Cream", "category": "desserts"},
        "garlic naan": {"name": "Garlic Naan", "category": "meals"},
    }

    def test_similar_suggests_existing_names_sharing_a_word(self):
        names = [s["name"] for s in enrich.similar(self.CATALOG, "Butterscotch Thickshake")]
        self.assertEqual(set(names), {"Butterscotch Milkshake", "Butterscotch Ice Cream"})
        self.assertEqual(enrich.similar(self.CATALOG, "Veg Momos"), [], "generic words alone don't match")

    def test_reusing_an_existing_name_is_allowed_and_keeps_its_category(self):
        raws = ["Butterscotch Thickshake", "Butterscotch Crunch Signature Ice Cream Shake"]
        problems, accepted = enrich.validate(
            [{"raw": raws[0], "name": "Butterscotch Milkshake", "category": "beverages"},
             {"raw": raws[1], "name": "butterscotch milkshake", "category": "desserts"}],
            raws, CATEGORIES, self.CATALOG)
        self.assertEqual(problems, [])
        self.assertEqual({v["name"] for v in accepted.values()}, {"Butterscotch Milkshake"})
        self.assertEqual({v["category"] for v in accepted.values()}, {"beverages"}, "existing category wins")

    def test_new_name_with_invented_words_still_rejected(self):
        problems, accepted = enrich.validate(
            [{"raw": "Butterscotch Thickshake", "name": "Butterscotch Shake Drink", "category": "beverages"}],
            ["Butterscotch Thickshake"], CATEGORIES, self.CATALOG)
        self.assertEqual(accepted, {})
        self.assertIn("reuse one of the 'similar' names", problems[0])

    def test_two_new_raws_in_one_batch_can_share_a_new_name(self):
        raws = ["Mango Lassi (Large)", "Sweet Mango Lassi Glass"]
        problems, accepted = enrich.validate(
            [{"raw": raws[1], "name": "Mango Lassi", "category": "beverages"},
             {"raw": raws[0], "name": "Mango Lassi", "category": "beverages"}],
            raws, CATEGORIES, {})
        self.assertEqual((problems, len(accepted)), ([], 2))

    def test_merge_points_raw_names_at_one_canonical(self):
        catalog = {**self.CATALOG, "butterscotch thickshake": {"name": "Butterscotch Thickshake", "category": "desserts"}}
        self.assertEqual(enrich.merge(catalog, ["Butterscotch Thickshake"], into="butterscotch milkshake"), 1)
        self.assertEqual(catalog["butterscotch thickshake"], {"name": "Butterscotch Milkshake", "category": "beverages"})
        with self.assertRaises(KeyError):
            enrich.merge(catalog, ["Garlic Naan"], into="Nonexistent")


class CatalogCommandsTest(IsolatedStateTest):
    def test_batch_carries_similar_names_and_reuse_reaches_the_sheet(self):
        enrich.save_catalog({"crunchy butterscotch milkshake [350ml]": {"name": "Butterscotch Milkshake", "category": "beverages"}})
        gog = FakeGog({"m1": message("m1", "noreply@swiggy.in", "Order delivered",
                                     fixture("swiggy/sample_a.html").replace("Butterscotch Ice Cream x1", "Butterscotch Thickshake x1"),
                                     1790410114000)})
        out, _ = sync.prepare(gog, CFG, None, None, TODAY)
        item = next(i for i in out["to_enrich"] if i["raw"] == "Butterscotch Thickshake")
        self.assertEqual(item["similar"], [{"name": "Butterscotch Milkshake", "category": "beverages"}])
        res, _ = sync.enrich_step(CFG, json.dumps([{"raw": "Butterscotch Thickshake", "name": "Butterscotch Milkshake", "category": "beverages"}]))
        self.assertEqual(res["status"], "ready_to_commit")
        sync.commit(gog, CFG, False, False)
        self.assertEqual([r[4] for r in gog.tabs["swiggy"][1:]], ["Butterscotch Milkshake", "Butterscotch Milkshake"])

    def test_items_and_merge_items(self):
        enrich.save_catalog({"a": {"name": "Butterscotch Milkshake", "category": "beverages"},
                             "b": {"name": "Butterscotch Thickshake", "category": "beverages"}})
        self.assertEqual(sync.list_items()[0]["count"], 2)
        out, code = sync.merge_items(["Butterscotch Thickshake"], "Butterscotch Milkshake")
        self.assertEqual((code, out["catalog_entries_changed"]), (0, 1))
        items = sync.list_items()[0]["items"]
        self.assertEqual([(i["name"], sorted(i["raw"])) for i in items], [("Butterscotch Milkshake", ["a", "b"])])
        self.assertEqual(sync.merge_items(["x"], "Nope")[1], 2)


class SheetEditGog:
    """Fake gog for apply-catalog: full rows in, batch requests recorded."""

    def __init__(self, rows):
        self.rows, self.requests = rows, []

    def tab_titles(self, sid):
        return {"Swiggy"}

    def tab_ids(self, sid):
        return {"Swiggy": 42}

    def read_values(self, sid, rng):
        assert rng == "Swiggy!A1:L", rng
        return self.rows

    def batch_request(self, sid, requests):
        self.requests.extend(requests)


def sheet_row(order_id, line, name, category, raw):
    return [46000.5, order_id, "Shop", line, name, category, 1, 100, 100, 100, "swiggy", raw]


class CatalogFixesTest(IsolatedStateTest):
    def test_set_item_moves_every_raw_name_of_that_product(self):
        catalog = {"panner dhudiya kebab": {"name": "Paneer Kebab", "category": "meals"},
                   "paneer subz kebab": {"name": "Paneer Kebab", "category": "meals"}}
        self.assertEqual(enrich.set_item(catalog, "Panner Dhudiya Kebab", "Paneer Kebab", "snacks"), 2)
        self.assertEqual({e["category"] for e in catalog.values()}, {"snacks"})
        self.assertEqual(enrich.set_item(catalog, "Panner Dhudiya Kebab", "Paneer Kebab", "snacks"), 0)

    def test_set_items_is_all_or_nothing(self):
        out, code = sync.set_items([{"raw": "A", "name": "A", "category": "meals"},
                                    {"raw": "B", "name": "B", "category": "food"}], CATEGORIES)
        self.assertEqual(code, 2)
        self.assertIn("food", out["problems"][0])
        self.assertEqual(enrich.load_catalog(), {})

    def test_shipped_catalog_is_valid(self):
        shipped = Path(sync.DEFAULT_CONFIG).parent / "catalog.json"
        cfg = json.loads(sync.DEFAULT_CONFIG.read_text())
        catalog = enrich.load_catalog(shipped)
        self.assertEqual(enrich.check(catalog, cfg["categories"]), [])
        self.assertEqual(catalog[enrich.key("Butterscotch Thickshake")]["name"], "Milkshake")
        self.assertEqual(catalog[enrich.key("nectr Avocado")]["category"], "fruits_vegetables")
        # New variants are offered the minimal standard name to reuse.
        self.assertEqual(enrich.similar(catalog, "Paneer Tikka Grilled Sandwich")[0]["name"], "Sandwich")
        self.assertIn("Momos", [s["name"] for s in enrich.similar(catalog, "Chicken Fried Momo")])

    def test_file_is_grouped_by_product_and_round_trips(self):
        enrich.save_catalog({"butterscotch shake": {"name": "Butterscotch Milkshake", "category": "beverages", "raw": "Butterscotch Shake"},
                             "butterscotch thickshake": {"name": "Butterscotch Milkshake", "category": "beverages"}})
        data = json.loads(enrich.CATALOG_PATH.read_text())
        self.assertEqual(data, [{"name": "Butterscotch Milkshake", "category": "beverages",
                                 "raw": ["Butterscotch Shake", "butterscotch thickshake"]}])
        self.assertEqual(enrich.lookup(enrich.load_catalog(), "BUTTERSCOTCH SHAKE"), ("Butterscotch Milkshake", "beverages"))

    def test_hand_edit_renames_every_raw_name(self):
        enrich.CATALOG_PATH.write_text(json.dumps([{"name": "Gudbud", "category": "desserts",
                                                    "raw": ["Special Gudbud", "Gudbud Ice Cream"]}]))
        catalog = enrich.load_catalog()
        self.assertEqual({e["name"] for e in catalog.values()}, {"Gudbud"})
        self.assertEqual(sync.catalog_problems(CATEGORIES), [])

    def test_bad_hand_edit_is_reported_and_blocks_commands(self):
        enrich.CATALOG_PATH.write_text('[{"name": "A", "category": "meal", "raw": ["a"]},')
        self.assertIn("not valid JSON", sync.catalog_problems(CATEGORIES)[0])
        enrich.CATALOG_PATH.write_text(json.dumps([{"name": "A", "category": "meal", "raw": ["a"]},
                                                   {"name": "A", "category": "snacks", "raw": ["b"]}]))
        problems = sync.catalog_problems(CATEGORIES)
        self.assertTrue(any("'meal'" in p for p in problems) and any("two categories" in p for p in problems))
        out, _ = sync.status(CATEGORIES)
        self.assertEqual(out["status"], "catalog_error")

    def test_legacy_catalog_is_merged_once(self):
        enrich.LEGACY_CATALOG_PATH.write_text(json.dumps({"garlic naan": {"name": "Garlic Naan", "category": "meals"},
                                                          "gudbud": {"name": "Special Gudbud", "category": "desserts"}}))
        enrich.save_catalog({"gudbud": {"name": "Gudbud", "category": "desserts", "raw": "Gudbud"}})
        self.assertFalse(enrich.LEGACY_CATALOG_PATH.exists())
        catalog = enrich.load_catalog()
        self.assertEqual(catalog["gudbud"]["name"], "Gudbud")  # new file wins

    def test_legacy_entries_missing_from_new_file_are_kept(self):
        enrich.LEGACY_CATALOG_PATH.write_text(json.dumps({"garlic naan": {"name": "Garlic Naan", "category": "meals"}}))
        enrich.CATALOG_PATH.write_text("[]")
        self.assertIn("garlic naan", enrich.load_catalog())

    def test_apply_catalog_rewrites_only_item_name_and_category(self):
        enrich.save_catalog({"butterscotch thickshake": {"name": "Butterscotch Milkshake", "category": "beverages"},
                             "garlic naan": {"name": "Garlic Naan", "category": "meals"}})
        gog = SheetEditGog([sync.HEADER[:12],
                            sheet_row("o1", 1, "Butterscotch Thickshake", "beverages", "Butterscotch Thickshake"),
                            sheet_row("o1", 2, "Garlic Naan", "meals", "Garlic Naan"),
                            sheet_row("o2", 1, "Diesel", "fuel", "Diesel")])
        cfg = {**CFG, "sources": [{**CFG["sources"][0], "sheet_tab": "Swiggy"}]}
        out, _ = sync.apply_catalog(gog, cfg, None, dry_run=True)
        self.assertEqual((out["sources"]["swiggy"]["rows_to_change"], gog.requests), (1, []))
        out, _ = sync.apply_catalog(gog, cfg, None, dry_run=False)
        self.assertEqual(out["sources"]["swiggy"]["rows_changed"], 1)
        (req,) = gog.requests
        self.assertEqual(req["updateCells"]["range"], {"sheetId": 42, "startRowIndex": 1, "endRowIndex": 2,
                                                       "startColumnIndex": 4, "endColumnIndex": 6})
        self.assertEqual([v["userEnteredValue"]["stringValue"] for v in req["updateCells"]["rows"][0]["values"]],
                         ["Butterscotch Milkshake", "beverages"])

    def test_apply_catalog_refuses_an_unexpected_header(self):
        enrich.save_catalog({"x": {"name": "X", "category": "meals"}})
        header = sync.HEADER[:12]
        header[4], header[5] = header[5], header[4]
        gog = SheetEditGog([header, sheet_row("o1", 1, "y", "meals", "x")])
        cfg = {**CFG, "sources": [{**CFG["sources"][0], "sheet_tab": "Swiggy"}]}
        with self.assertRaises(RuntimeError):
            sync.apply_catalog(gog, cfg, None, dry_run=False)
        self.assertEqual(gog.requests, [])


class DieselSourceTest(IsolatedStateTest):
    SUBJECT = "Alert: Your RBL Bank Credit Card has just been swiped"

    def setUp(self):
        super().setUp()
        fuel = fixture("rbl/fuel_swipe.html")
        self.gog = FakeGog({
            "d1": message("d1", "RBL Bank <RBLAlerts@rbl.bank.in>", self.SUBJECT, fuel, 1791540000000),
            "d2": message("d2", "RBLAlerts@rbl.bank.in", self.SUBJECT, fuel.replace("3,500.00", "2,000.00"), 1791800000000),
            "shop": message("shop", "RBLAlerts@rbl.bank.in", self.SUBJECT,
                            fuel.replace("SRI EKADANTHA FUEL STA", "AMAZON PAY INDIA"), 1791700000000),
        })
        self.cfg = {**CFG, "sources": [DIESEL]}

    def test_fuel_swipes_become_diesel_rows_without_the_model(self):
        out, _ = sync.prepare(self.gog, self.cfg, None, None, TODAY)
        stats = out["sources"]["fuel"]
        self.assertEqual((stats["new_bills"], stats["not_tracked"]), (2, 1), "the Amazon swipe is not diesel")
        self.assertEqual(out["status"], "ready_to_commit", "no item needs a name from the model")
        self.assertIn('subject:"has just been swiped"', self.gog.queries[0])
        result, _ = sync.commit(self.gog, self.cfg, False, False)
        self.assertEqual(result["sources"]["fuel"]["rows_written"], 2)
        rows = [dict(zip(sync.HEADER, r)) for r in self.gog.tabs["Fuel"][1:]]
        self.assertEqual([(r["item_name"], r["category"], r["final_price"], r["merchant"]) for r in rows],
                         [("Diesel", "fuel", 3500.0, "SRI EKADANTHA FUEL STA"), ("Diesel", "fuel", 2000.0, "SRI EKADANTHA FUEL STA")])
        self.assertEqual({r["order_id"] for r in rows}, {"'d1", "'d2"}, "the Gmail message id is the order id")

    def test_existing_empty_fuel_tab_gets_header_then_rows(self):
        self.gog.tabs["Fuel"] = []  # the user created the tab by hand
        sync.prepare(self.gog, self.cfg, None, None, TODAY)
        sync.commit(self.gog, self.cfg, False, False)
        self.assertEqual(self.gog.tabs["Fuel"][0], sync.HEADER)
        self.assertEqual(len(self.gog.tabs["Fuel"]), 3)
        self.assertEqual({r[10] for r in self.gog.tabs["Fuel"][1:]}, {"fuel"}, "source column")

    def test_rerun_adds_nothing(self):
        sync.prepare(self.gog, self.cfg, None, None, TODAY)
        sync.commit(self.gog, self.cfg, False, False)
        out, _ = sync.prepare(self.gog, self.cfg, None, None, TODAY)
        self.assertEqual(out["status"], "nothing_new")
        self.assertEqual(len(self.gog.tabs["Fuel"]), 3)


class EnrichValidationTest(unittest.TestCase):
    RAW = ["Crunchy Butterscotch Milkshake [350ml]", "6 x Coca-Cola Zero Sugar Diet Drink",
           "Coca-Cola Zero Sugar Diet Drink", "Amul Taaza Toned Milk 500 ml"]

    def check(self, raw, name, category="beverages"):
        return enrich.validate([{"raw": raw, "name": name, "category": category}], self.RAW, CATEGORIES)[0]

    def test_good_cleanups_accepted(self):
        self.assertEqual(self.check(self.RAW[0], "Butterscotch Milkshake"), [])
        self.assertEqual(self.check(self.RAW[2], "Coca-Cola Zero Sugar"), [])
        self.assertEqual(self.check(self.RAW[3], "Amul Taaza Toned Milk", "groceries"), [])

    def test_quantity_or_size_in_name_rejected(self):
        self.assertTrue(self.check(self.RAW[2], "6 x Coca-Cola Zero Sugar"))
        self.assertTrue(self.check(self.RAW[3], "Amul Taaza Toned Milk 500 ml", "groceries"))

    def test_added_words_rejected(self):
        problems = self.check(self.RAW[0], "Butterscotch Shake Drink")
        self.assertTrue(any("drink" in p for p in problems), problems)

    def test_unknown_category_and_raw_rejected(self):
        self.assertTrue(self.check(self.RAW[0], "Butterscotch Milkshake", "drinks"))
        self.assertTrue(self.check("Something Else", "Something"))


class BuildDashboardTest(unittest.TestCase):
    CFG = json.loads((TESTS.parent.parent / "assets" / "config.json").read_text())
    META = {"sheets": [{"properties": {"sheetId": 5, "title": "Dashboard"},
                        "charts": [{"chartId": 1, "spec": {"title": "Spend by weekday"}},
                                   {"chartId": 2, "spec": {"title": "My own chart"}}]},
                       {"properties": {"sheetId": 0, "title": "Swiggy"}},
                       {"properties": {"sheetId": 1, "title": "Instamart"}},
                       {"properties": {"sheetId": 2, "title": "Fuel"}}]}

    def setUp(self):
        self.reqs = build_dashboard.requests_for(self.META, self.CFG)
        self.cells, _, self.charts, _, _ = build_dashboard.plan(self.CFG)
        json.dumps(self.reqs)

    def test_reads_every_source_tab(self):
        main = self.cells["A2"]
        for tab in ("Swiggy!A2:N", "Instamart!A2:N", "Fuel!A2:N"):
            self.assertIn(tab, main)

    def test_tables_do_not_overlap(self):
        used = {}
        for c in self.charts:
            for column in [c["domain"], *c["series"]]:
                used.setdefault(column, set()).add(c["title"])
        starts = sorted(build_dashboard.col("".join(ch for ch in a1 if ch.isalpha())) for a1 in self.cells)
        self.assertEqual(len(starts), len(set(starts)) + sum(1 for a1 in self.cells if a1.endswith("2")),
                         "each table owns its columns (header cell + formula cell share a column)")

    def test_source_colours_follow_the_source(self):
        by_title = {c["title"]: c for c in self.charts}
        expect = [build_dashboard.SOURCE_COLORS[s["name"]] for s in self.CFG["sources"]]
        self.assertEqual(by_title["Monthly spend by source"]["colors"], expect)
        self.assertEqual(by_title["Daily spend · 7-day moving average"]["colors"], expect + [build_dashboard.TOTAL_COLOR])
        self.assertEqual(len(by_title["Daily spend · 30-day moving average"]["series"]), len(expect) + 1)
        self.assertIn("Fuel spend per month", by_title)

    def test_formulas_avoid_names_sheets_rejects(self):
        """The moving-average formula once failed in Sheets with #ERROR!. It used `days` (also a
        function), cell-like names such as `s0`, and a LAMBDA bound in LET and called by name.
        None of these may come back. (`n`/`t` are fine: the pie table uses them and works.)"""
        import re
        shadowing = {"days", "date", "month", "year", "sum", "min", "max", "rows", "index", "today", "sequence"}
        for a1, v in self.cells.items():
            if isinstance(v, str) and v.startswith("=LET("):
                names = re.findall(r"(?:LET\(|, )([A-Za-z_][A-Za-z0-9_]*), ", v)
                bad = {n for n in names if n.lower() in shadowing or re.fullmatch(r"[A-Za-z]{1,3}\d+", n)}
                self.assertFalse(bad, f"{a1}: LET names {bad}")
                self.assertNotRegex(v, r", [A-Za-z_]+, LAMBDA\(", f"{a1}: LAMBDA bound to a LET name")

    def test_daily_table_has_one_column_per_header(self):
        cols = [k for k in self.cells if self.cells[k] and isinstance(self.cells[k], list) and self.cells[k][0] == "Day"][0]
        headers = self.cells[cols]
        formula = self.cells[cols[:-1] + "2"]
        self.assertEqual(formula.count("MAP(day_list"), len(headers) - 1)

    def test_only_own_charts_are_replaced(self):
        deleted = [r["deleteEmbeddedObject"]["objectId"] for r in self.reqs if "deleteEmbeddedObject" in r]
        self.assertEqual(deleted, [1])

    def test_missing_source_tab_is_created(self):
        meta = {"sheets": [s for s in self.META["sheets"] if s["properties"]["title"] != "Fuel"]}
        added = [r["addSheet"]["properties"]["title"] for r in build_dashboard.requests_for(meta, self.CFG) if "addSheet" in r]
        self.assertIn("Fuel", added)


if __name__ == "__main__":
    unittest.main()
