import html
import re
from decimal import Decimal
from html.parser import HTMLParser

TOTAL_TOLERANCE_PAISE = 100


class ParseError(Exception):
    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


_LINE_BREAK_TAGS = {"p", "div", "br", "li"}


class _RowCollector(HTMLParser):
    """Flattens HTML into an ordered stream of ("row", [cell_text, ...]) and
    ("comment", text) events. Tolerates the malformed nesting in Instamart mail:
    text always goes to the innermost open <tr>'s current cell."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events = []
        self._rows = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._rows.append({"cells": [], "open": False})
        elif tag in ("td", "th") and self._rows:
            row = self._rows[-1]
            row["cells"].append([])
            row["open"] = True
        elif tag in _LINE_BREAK_TAGS:
            self._add("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _LINE_BREAK_TAGS:
            self._add("\n")

    def handle_endtag(self, tag):
        if tag == "tr" and self._rows:
            row = self._rows.pop()
            self.events.append(("row", [_clean(c) for c in row["cells"]]))
        elif tag in ("td", "th") and self._rows:
            self._rows[-1]["open"] = False
        elif tag in ("p", "div"):
            self._add("\n")

    def handle_data(self, data):
        self._add(data)

    def handle_comment(self, data):
        self.events.append(("comment", data.strip()))

    def _add(self, text):
        if self._rows and self._rows[-1]["open"]:
            self._rows[-1]["cells"][-1].append(text)


def _clean(parts):
    lines = (re.sub(r"\s+", " ", ln).strip() for ln in "".join(parts).split("\n"))
    return "\n".join(ln for ln in lines if ln)


def events(body):
    collector = _RowCollector()
    collector.feed(body)
    collector.close()
    return collector.events


def strip_tags(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


_AMOUNT_RE = re.compile(r"(-)?\s*(?:₹|Rs\.?)\s*(\d+(?:\.\d{1,2})?)", re.IGNORECASE)


def parse_amount(text):
    """'₹148' -> 14800, '- ₹ 74.99' -> -7499, 'FREE' -> 0. When a cell shows a struck-through
    price before the charged one ('₹45 FREE', '₹300\\n₹250'), the last value is what was charged."""
    flat = " ".join(text.replace(",", "").split())
    if not flat:
        raise ParseError("UNKNOWN_LINE", f"empty amount: {text!r}")
    if flat.upper().endswith("FREE"):
        return 0
    matches = list(_AMOUNT_RE.finditer(flat))
    if not matches or flat[matches[-1].end():].strip():
        raise ParseError("UNKNOWN_LINE", f"unparseable amount: {text!r}")
    m = matches[-1]
    paise = int((Decimal(m.group(2)) * 100).to_integral_value())
    return -paise if m.group(1) else paise


def validate(bill):
    computed = sum(i["line_price"] for i in bill["items"]) + sum(c["amount"] for c in bill["charges"])
    if abs(computed - bill["bill_total"]) > TOTAL_TOLERANCE_PAISE:
        raise ParseError(
            "TOTAL_MISMATCH",
            f"order {bill['order_id']}: items+charges={computed / 100:.2f} "
            f"vs paid={bill['bill_total'] / 100:.2f}; bill={bill}",
        )
    return bill
