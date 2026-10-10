import re

from .common import ParseError, events, parse_amount, validate

TEMPLATE = "instamart-2026"
_ORDER_ID_RE = re.compile(r"order id:\s*<span[^>]*>\s*(\d+)\s*</span>", re.I)
_ITEM_RE = re.compile(r"^(\d+)\s*x\s+(.*\S)$")


def parse(body):
    if "Order Items" not in body or "Order Summary" not in body:
        raise ParseError("UNKNOWN_TEMPLATE", "Instamart markers (Order Items / Order Summary) not found")

    m = _ORDER_ID_RE.search(body)
    if not m:
        raise ParseError("UNKNOWN_TEMPLATE", "order id not found")
    order_id = m.group(1)

    items, charges, item_bill, total = [], [], None, None
    state = "pre"
    for kind, value in events(body):
        if kind != "row":
            continue
        cells = [c for c in value if c]
        if not cells or state == "done":
            continue
        if cells == ["Order Items"]:
            state = "items"
            continue
        if cells == ["Order Summary"]:
            state = "summary"
            continue
        if state == "pre":
            continue
        if len(cells) != 2:
            raise ParseError("UNKNOWN_LINE", f"order {order_id}: unexpected row {cells}")
        label, amount = cells
        if state == "items":
            im = _ITEM_RE.match(label)
            if not im:
                raise ParseError("UNKNOWN_LINE", f"order {order_id}: item without quantity: {label!r}")
            items.append({"name": im.group(2), "quantity": int(im.group(1)), "line_price": parse_amount(amount)})
        elif label.lower() == "item bill":
            item_bill = parse_amount(amount)
        elif label.lower() == "grand total":
            total = parse_amount(amount)
            state = "done"
        else:
            charges.append({"label": label, "amount": parse_amount(amount)})

    if not items or total is None:
        raise ParseError("UNKNOWN_TEMPLATE", f"order {order_id}: items or Grand Total not found")
    items_sum = sum(i["line_price"] for i in items)
    if item_bill is not None and item_bill != items_sum:
        raise ParseError(
            "TOTAL_MISMATCH",
            f"order {order_id}: Item Bill {item_bill / 100:.2f} != sum of items {items_sum / 100:.2f}",
        )

    return validate({
        "order_id": order_id,
        "merchant": "Instamart",
        "items": items,
        "charges": charges,
        "bill_total": total,
        "template_id": TEMPLATE,
    })
