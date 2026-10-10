"""RBL Bank credit-card swipe alerts (used for the fuel source, tab Fuel).

  Subject: "Alert: Your RBL Bank Credit Card has just been swiped"
  Body:    "INR3,500.00 spent at SRI EKADANTHA FUEL STA on RBL Bank credit card (0533)
            on 09-10-2026.AVL limit- INR40,500.00. Not you? Call 022-62327777"

An alert has no order number, so the bill's order_id is left empty and sync.py uses the
Gmail message id instead (one alert = one swipe). Every swipe becomes one "Diesel" row in
category "fuel"; the source's merchant_include list in config.json decides which swipes count.
"""
import html
import re

from .common import ParseError, validate

TEMPLATE = "rbl-card-alert"
ITEM_NAME, CATEGORY = "Diesel", "fuel"
_SWIPE = re.compile(
    r"INR\s*([\d,]+(?:\.\d{1,2})?)\s+spent at\s+(.+?)\s+on RBL Bank credit card\s*\(\w+\)\s+on\s+(\d{2}-\d{2}-\d{4})",
    re.I)


def _text(body):
    body = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body))).strip()


def parse(body):
    m = _SWIPE.search(_text(body))
    if not m:
        raise ParseError("UNKNOWN_TEMPLATE", "no 'INR… spent at … on RBL Bank credit card' sentence found")
    paise = round(float(m.group(1).replace(",", "")) * 100)
    if paise <= 0:
        raise ParseError("UNKNOWN_LINE", f"non-positive amount {m.group(1)!r}")
    return validate({
        "order_id": "",
        "merchant": m.group(2).strip(),
        "items": [{"name": ITEM_NAME, "quantity": 1, "line_price": paise, "category": CATEGORY}],
        "charges": [],
        "bill_total": paise,
        "template_id": TEMPLATE,
        "swipe_date": m.group(3),
    })
