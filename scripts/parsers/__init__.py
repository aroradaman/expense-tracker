from . import instamart, rbl_card, swiggy

PARSERS = {"swiggy": swiggy.parse, "instamart": instamart.parse, "rbl_card": rbl_card.parse}
