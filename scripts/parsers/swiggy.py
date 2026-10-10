"""Swiggy food (incl. Swiggy Gourmet) delivery emails.

Two layouts seen so far:
  swiggy-food-2026    "BILL DETAILS" card (Mar 2026 onwards). Items, a divider row, charges,
                      a divider row, "Paid Via ...". Newer mails also carry <!-- Non-MRO / MRO -->
                      comments; older 2026 mails don't, so sections are found by the divider rows.
                      Mid-Jul 2026 mails add an "Item Total" row and a "Line Separator" row
                      (amount: bare '₹') to the charges block.
  swiggy-food-legacy  "Item Name | Quantity | Price" table (2024-2025). Then "Item Total:",
                      charges, "Paid Via ...:" and "Order Total:".
"""
import re

from .common import ParseError, events, parse_amount, strip_tags, validate

_ITEM_QTY_SUFFIX = re.compile(r"^(.*\S)\s+x(\d+)$")


def parse(body):
    if "BILL DETAILS" in body:
        return _parse_2026(body)
    if "Item Name" in body and "Order Total" in body:
        return _parse_legacy(body)
    raise ParseError("UNKNOWN_TEMPLATE", "no known Swiggy layout (BILL DETAILS / Item Name + Order Total)")


def _first(patterns, body, what):
    for pattern in patterns:
        m = re.search(pattern, body, re.S | re.I)
        if m and strip_tags(m.group(1)):
            return strip_tags(m.group(1))
    raise ParseError("UNKNOWN_TEMPLATE", f"{what} not found")


def _label(text):
    return text.strip().rstrip(":").strip()


def _parse_2026(body):
    order_id = _first([r"Order ID:\s*<span[^>]*>\s*(\d+)\s*</span>"], body, "order id")
    merchant = _first([r"ORDER JOURNEY\s*</p>.*?<p[^>]*>(.*?)</p>"], body, f"order {order_id}: restaurant name")

    items, charges, item_total, total = [], [], None, None
    state, mro_seen = "pre", False
    for kind, value in events(body):
        if kind == "comment":
            if value.startswith("MRO") and state == "items":
                mro_seen = True
            continue
        cells = [c for c in value if c]
        if state == "pre":
            if cells == ["BILL DETAILS"]:
                state = "items"
            continue
        if state == "done":
            continue
        if not cells:  # divider row
            if state == "items" and items:
                state = "charges"
            continue
        if state == "items" and mro_seen:
            raise ParseError("UNKNOWN_TEMPLATE", f"order {order_id}: multi-restaurant (MRO) items not supported yet")
        if state == "items" and len(cells) == 1:
            continue  # add-on line such as "With Cheese Slice (₹30)"; already in the item's price
        if len(cells) != 2:
            raise ParseError("UNKNOWN_LINE", f"order {order_id}: unexpected bill row {cells}")
        label, amount = cells
        low = label.lower()
        if state == "charges" and low == "line separator":
            continue  # visual divider (Jul 2026 emails); its amount cell is a bare '₹'
        if state == "items":
            m = _ITEM_QTY_SUFFIX.match(label)
            if not m:
                raise ParseError("UNKNOWN_LINE", f"order {order_id}: item without quantity: {label!r}")
            items.append({"name": m.group(1), "quantity": int(m.group(2)), "line_price": parse_amount(amount)})
        elif low.startswith("paid via"):
            total = parse_amount(amount)
            state = "done"
        elif low == "item total":
            item_total = parse_amount(amount)  # a subtotal, never a charge
        else:
            charges.append({"label": label, "amount": parse_amount(amount)})

    if not items or total is None:
        raise ParseError("UNKNOWN_TEMPLATE", f"order {order_id}: items or 'Paid Via' total not found")
    items_sum = sum(i["line_price"] for i in items)
    if item_total is not None and item_total != items_sum:
        raise ParseError("TOTAL_MISMATCH",
                         f"order {order_id}: Item Total {item_total / 100:.2f} != sum of items {items_sum / 100:.2f}")
    return validate({"order_id": order_id, "merchant": merchant, "items": items, "charges": charges,
                     "bill_total": total, "template_id": "swiggy-food-2026"})


def _parse_legacy(body):
    order_id = _first([r"Order No:\s*</p>\s*<h5[^>]*>\s*(\d+)\s*</h5>",
                       r"Order No:\s*<strong>\s*(\d+)\s*</strong>"], body, "order id")
    merchant = _first([r"<p[^>]*>\s*Restaurant\s*</p>\s*<h5[^>]*>(.*?)</h5>",
                       r"Ordered from:\s*</p>\s*<h5[^>]*>(.*?)</h5>"], body, f"order {order_id}: restaurant name")

    items, charges, item_total, total = [], [], None, None
    state = "pre"
    for kind, value in events(body):
        if kind != "row":
            continue
        cells = [c for c in value if c]
        if not cells or state == "done":
            continue
        if state == "pre":
            if cells == ["Item Name", "Quantity", "Price"]:
                state = "items"
            continue
        label = _label(cells[0])
        if state == "items" and len(cells) == 3:
            name, qty, amount = cells
            if not qty.isdigit():
                raise ParseError("UNKNOWN_LINE", f"order {order_id}: bad quantity in {cells}")
            items.append({"name": name, "quantity": int(qty), "line_price": parse_amount(amount)})
            continue
        if len(cells) != 2:
            raise ParseError("UNKNOWN_LINE", f"order {order_id}: unexpected bill row {cells}")
        amount = parse_amount(cells[1])
        state = "summary"
        low = label.lower()
        if low == "item total":
            item_total = amount
        elif low.startswith("paid via"):
            continue  # payment method(s); "Order Total" is the amount paid, even when split
        elif low == "order total":
            total = amount
            state = "done"
        else:
            charges.append({"label": label, "amount": amount})

    if not items or total is None:
        raise ParseError("UNKNOWN_TEMPLATE", f"order {order_id}: items or Order Total not found")
    items_sum = sum(i["line_price"] for i in items)
    if item_total is not None and item_total != items_sum:
        raise ParseError("TOTAL_MISMATCH",
                         f"order {order_id}: Item Total {item_total / 100:.2f} != sum of items {items_sum / 100:.2f}")
    return validate({"order_id": order_id, "merchant": merchant, "items": items, "charges": charges,
                     "bill_total": total, "template_id": "swiggy-food-legacy"})
