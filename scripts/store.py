"""Review queue for emails the built-in parsers could not handle.

One JSON file per Gmail message in ~/.expense-tracker/pending/. A record stays
until it is written to the sheet (by a later parser run or `pending.py submit`)
or dismissed, so a failed email is never silently lost.
"""
import json
import os
import re
from pathlib import Path

STATE_DIR = Path(os.path.expanduser("~/.expense-tracker"))
PENDING_DIR = STATE_DIR / "pending"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class NotQueued(LookupError):
    pass


def _path(message_id):
    if not _SAFE_ID.match(message_id):
        raise NotQueued(message_id)
    return PENDING_DIR / f"{message_id}.json"


def exists(message_id):
    return _path(message_id).exists()


def save(record):
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(record["message_id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1))
    tmp.replace(path)


def load(message_id):
    path = _path(message_id)
    if not path.exists():
        raise NotQueued(message_id)
    return json.loads(path.read_text())


def delete(message_id):
    _path(message_id).unlink(missing_ok=True)


def all_records(status=None, source=None):
    if not PENDING_DIR.exists():
        return []
    out = []
    for p in sorted(PENDING_DIR.glob("*.json")):
        rec = json.loads(p.read_text())
        if (status is None or rec.get("status") == status) and (source is None or rec["source"] == source):
            out.append(rec)
    return out
