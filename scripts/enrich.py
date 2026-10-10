"""Item catalog: raw item name -> canonical display name + category.

The model fills it for raw names it hasn't seen; every later order of the same raw name
reuses the stored answer with no model call. Different raw names for the same product
("Butterscotch Thickshake", "Butterscotch Crunch Signature Ice Cream Shake") can share one
canonical name: the model is shown similar existing names and may reuse one exactly.
"""
import json
import re
from pathlib import Path

import store

# In the skill folder so you can read and edit it. Older versions kept it in the state folder;
# anything found there is merged in once (entries already in the new file win).
CATALOG_PATH = Path(__file__).resolve().parent.parent / "assets" / "catalog.json"
LEGACY_CATALOG_PATH = store.STATE_DIR / "item_catalog.json"
UNCATEGORIZED = "uncategorized"
MAX_NAME_LEN = 60

_TOKEN = re.compile(r"[a-z0-9]+(?:['&.-][a-z0-9]+)*")
_QTY = re.compile(r"^\s*\d+\s*[x×]\s+|\s[x×]\s*\d+\s*$", re.I)
_SIZE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ml|l|ltr|litre|liter|g|gm|gms|gram|grams|kg|pc|pcs|piece|pieces|units?)\b|\bpack of\b",
    re.I)


# Words too common to suggest a match on their own.
_GENERIC = {"with", "and", "the", "of", "a", "in", "on", "for", "special", "classic", "signature",
            "fresh", "crunchy", "served", "serves", "veg", "half", "full", "pc", "pcs", "piece",
            "pieces", "ml", "new", "combo", "regular", "large", "small", "medium"}


def key(raw_name):
    return re.sub(r"\s+", " ", raw_name).strip().lower()


class CatalogError(ValueError):
    pass


def _from_file(data):
    """File format -> {lowercased raw name: {"name", "category", "raw"}}.

    The file is a list of products, one per canonical name, made to be edited by hand:
      [{"name": "Butterscotch Milkshake", "category": "beverages",
        "raw": ["Butterscotch Shake", "Crunchy Butterscotch Milkshake [350ml]"]}, ...]
    The old flat format {"<raw>": {"name", "category"}} is still read."""
    flat = {}
    if isinstance(data, dict):
        for raw, e in data.items():
            flat[key(raw)] = {"name": e["name"], "category": e["category"], "raw": e.get("raw", raw)}
        return flat
    if not isinstance(data, list):
        raise CatalogError("catalog must be a JSON list of {name, category, raw: [...]}")
    for n, product in enumerate(data, start=1):
        if not isinstance(product, dict) or not all(k in product for k in ("name", "category", "raw")) \
                or not isinstance(product["raw"], list):
            raise CatalogError(f"catalog entry {n}: needs \"name\", \"category\" and a \"raw\" list")
        name, category = str(product["name"]).strip(), str(product["category"]).strip()
        if not name or not category:
            raise CatalogError(f"catalog entry {n}: empty name or category")
        for raw in product["raw"]:
            flat[key(str(raw))] = {"name": name, "category": category, "raw": str(raw)}
    return flat


def _to_file(catalog):
    groups = {}
    for k, e in catalog.items():
        g = groups.setdefault((e["name"], e["category"]), {"name": e["name"], "category": e["category"], "raw": []})
        g["raw"].append(e.get("raw", k))
    out = sorted(groups.values(), key=lambda g: (g["category"], g["name"].lower()))
    for g in out:
        g["raw"].sort(key=str.lower)
    return out


def _read(path):
    try:
        return _from_file(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as e:
        raise CatalogError(f"{path} is not valid JSON (line {e.lineno}, column {e.colno}): {e.msg}") from e


def load_catalog(path=None):
    path = path or CATALOG_PATH
    catalog = _read(path) if path.exists() else {}
    if path == CATALOG_PATH and LEGACY_CATALOG_PATH.exists():
        for k, e in _read(LEGACY_CATALOG_PATH).items():
            catalog.setdefault(k, e)
    return catalog


def save_catalog(catalog, path=None):
    path = path or CATALOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_to_file(catalog), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    if path == CATALOG_PATH and LEGACY_CATALOG_PATH.exists():
        LEGACY_CATALOG_PATH.rename(LEGACY_CATALOG_PATH.with_suffix(".json.migrated"))


def check(catalog, categories):
    """Problems a hand edit can introduce: unknown categories, one name in two categories."""
    problems, seen = [], {}
    for e in catalog.values():
        if e["category"] not in categories:
            problems.append(f"{e['name']!r}: category {e['category']!r} is not one of {categories}")
        prev = seen.setdefault(e["name"].lower(), e["category"])
        if prev != e["category"]:
            problems.append(f"{e['name']!r} appears with two categories ({prev}, {e['category']})")
    return sorted(set(problems))


def lookup(catalog, raw_name):
    """(clean name, category); falls back to the raw name and 'uncategorized'."""
    entry = catalog.get(key(raw_name))
    if entry:
        return entry["name"], entry["category"]
    return raw_name, UNCATEGORIZED


def canonical_names(catalog):
    """{lowercased name: (name, category)} for every distinct name already in the catalog."""
    out = {}
    for entry in catalog.values():
        out.setdefault(entry["name"].lower(), (entry["name"], entry["category"]))
    return out


def _stem(word):
    """Singular form, so "Momo" finds "Momos" and "Kebabs" finds "Kebab"."""
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _words(text):
    return {_stem(t) for t in _TOKEN.findall(text.lower()) if t not in _GENERIC and not t.isdigit()}


def similar(catalog, raw_name, limit=5):
    """Existing canonical names that share a meaningful word with `raw_name`, best first."""
    raw_words = _words(raw_name)
    # The kind of dish is usually the last word ("Paneer Tikka Grilled Sandwich" is a sandwich),
    # so among equally good matches, a match on a later word ranks first.
    position = {_stem(w): i for i, w in enumerate(_TOKEN.findall(raw_name.lower()))}
    scored = []
    for name, category in canonical_names(catalog).values():
        shared = raw_words & _words(name)
        if shared:
            scored.append((-len(shared), -max(position.get(w, -1) for w in shared), len(name), name, category))
    return [{"name": n, "category": c} for *_, n, c in sorted(scored)[:limit]]


def _check_new_name(raw, name):
    if not name:
        return f"{raw!r}: name is empty"
    if len(name) > MAX_NAME_LEN:
        return f"{raw!r}: name is longer than {MAX_NAME_LEN} characters; shorten it"
    if _QTY.search(name) or _SIZE.search(name):
        return f"{raw!r}: name {name!r} must not contain a quantity, size or pack count"
    raw_tokens = set(_TOKEN.findall(raw.lower()))
    extra = [t for t in _TOKEN.findall(name.lower()) if t not in raw_tokens]
    if extra:
        return (f"{raw!r}: name uses words not in the original ({', '.join(extra)}); either reuse "
                f"one of the 'similar' names exactly, or only remove words from 'raw'")
    return None


def validate(entries, wanted_raw_names, categories, catalog=None):
    """Check the model's answers entry by entry. Returns (problems, accepted): `accepted` holds
    only the entries that passed, so one bad answer doesn't throw away the good ones.

    A name is valid if it reuses an existing canonical name exactly (its category then wins,
    so one product never ends up in two categories), or if it only removes words from `raw`.
    A name created earlier in the same batch also counts as existing."""
    if not isinstance(entries, list):
        return ['enrichment must be a JSON list like [{"raw": "...", "name": "...", "category": "..."}]'], {}
    wanted = {key(r): r for r in wanted_raw_names}
    known = canonical_names(catalog or {})
    problems, accepted, deferred = [], {}, []

    candidates = []
    for n, e in enumerate(entries, start=1):
        if not isinstance(e, dict):
            problems.append(f"entry {n}: must be an object with raw, name, category")
            continue
        raw, name, category = (str(e.get(k, "")).strip() for k in ("raw", "name", "category"))
        if key(raw) not in wanted:
            problems.append(f"{raw!r}: 'raw' must be copied exactly from to_enrich")
            continue
        candidates.append((raw, name, category))

    # Pass 1: reuse of an existing name, or a new name made by removing words.
    for raw, name, category in candidates:
        if name.lower() in known:
            canon, canon_category = known[name.lower()]
            accepted[key(raw)] = {"raw": raw, "name": canon, "category": canon_category}
            continue
        problem = _check_new_name(raw, name)
        if problem:
            deferred.append((raw, name, category, problem))
            continue
        if category not in categories:
            problems.append(f"{raw!r}: category {category!r} is not one of {categories}")
            continue
        accepted[key(raw)] = {"raw": raw, "name": name, "category": category}
        known.setdefault(name.lower(), (name, category))

    # Pass 2: names that reuse a name created by another entry in this batch.
    for raw, name, category, problem in deferred:
        if name.lower() in known:
            canon, canon_category = known[name.lower()]
            accepted[key(raw)] = {"raw": raw, "name": canon, "category": canon_category}
        else:
            problems.append(problem)
    return problems, accepted


def merge(catalog, names, into):
    """Point every raw name currently mapped to one of `names` at canonical `into`.
    Returns the number of catalog entries changed."""
    known = canonical_names(catalog)
    if into.lower() not in known:
        raise KeyError(into)
    target, category = known[into.lower()]
    wanted = {n.lower() for n in names}
    changed = 0
    for entry in catalog.values():
        if entry["name"].lower() in wanted and entry["name"] != target:
            entry["name"], entry["category"] = target, category
            changed += 1
    return changed


def set_item(catalog, raw_name, name, category):
    """A person's correction: map `raw_name` to `name` / `category`, no word rules applied.
    Every other raw name already mapped to `name` follows the new spelling and category,
    so one product never ends up in two categories. Returns the catalog entries changed."""
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        raise ValueError("name is empty")
    changed = 0
    for k, entry in catalog.items():
        if k != key(raw_name) and entry["name"].lower() == name.lower() \
                and (entry["name"], entry["category"]) != (name, category):
            entry["name"], entry["category"] = name, category
            changed += 1
    current = catalog.get(key(raw_name))
    if not current or (current["name"], current["category"]) != (name, category):
        catalog[key(raw_name)] = {"name": name, "category": category, "raw": raw_name}
        changed += 1
    return changed


def missing(catalog, raw_names):
    seen, out = set(), []
    for raw in raw_names:
        k = key(raw)
        if k not in catalog and k not in seen:
            seen.add(k)
            out.append(raw)
    return out
