import json
import subprocess
import time

GMAIL_COMMANDS = "gmail.search,gmail.get,gmail.thread.get"
SEARCH_MAX = 500
SHEETS_COMMANDS = "sheets.metadata,sheets.get,sheets.append,sheets.add-tab"
# Only `apply-catalog` edits existing cells (item_name / category); the sync itself never can.
SHEETS_EDIT_COMMANDS = "sheets.metadata,sheets.batch-request"

# Google quotas are per minute. A rate-limited request was rejected outright (nothing was
# read or written), so waiting and retrying it is always safe, including for sheet appends.
RATE_LIMIT_MARKERS = ("ratelimitexceeded", "userratelimitexceeded", "quota exceeded", "429",
                      "resource_exhausted", "too many requests")
RETRY_WAITS = (15, 30, 60, 60, 60)
MIN_INTERVAL = 0.3  # seconds between gog calls, to stay under the per-minute quota during backfills


class GogError(Exception):
    pass


def is_rate_limited(stderr):
    text = stderr.lower()
    return any(marker in text for marker in RATE_LIMIT_MARKERS)


class Gog:
    def __init__(self, binary, account, timeout=180, sleep=time.sleep, clock=time.monotonic, log=None):
        self.binary = binary
        self.account = account
        self.timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._log = log
        self._last_call = None

    def _pace(self):
        if self._last_call is not None:
            wait = MIN_INTERVAL - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def _run(self, args, allowed, gmail=False):
        cmd = [self.binary, *args, "-a", self.account, "--json", "--no-input",
               "--enable-commands-exact", allowed]
        if gmail:
            cmd += ["--readonly", "--gmail-no-send"]
        name = " ".join(args[:2])
        for attempt in range(len(RETRY_WAITS) + 1):
            self._pace()
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
            except subprocess.TimeoutExpired as e:
                raise GogError(f"gog {name} timed out") from e
            if proc.returncode == 0:
                out = proc.stdout.strip()
                return json.loads(out) if out else None
            err = proc.stderr.strip()
            if not is_rate_limited(err) or attempt == len(RETRY_WAITS):
                raise GogError(f"gog {name} exited {proc.returncode}: {err[:500]}")
            wait = RETRY_WAITS[attempt]
            if self._log:
                self._log.warning("gog %s rate-limited; retry %d in %ss", name, attempt + 1, wait)
            self._sleep(wait)
        raise AssertionError("unreachable")

    def search_threads(self, query):
        res = self._run(["gmail", "search", query, "--all", "--max", str(SEARCH_MAX), "-z", "Asia/Kolkata"],
                        GMAIL_COMMANDS, gmail=True)
        threads = (res.get("threads") or []) if isinstance(res, dict) else (res or [])
        if len(threads) >= SEARCH_MAX:
            # If --max caps the total rather than the page size, results may be cut off and the
            # cursor would skip the rest. Fail loudly instead.
            raise GogError(f"search returned {len(threads)} threads (the --max limit); results may be "
                           f"truncated. Backfill in windows, oldest first: `prepare --since A --until B`, then commit.")
        return threads

    def thread_message_ids(self, thread_id):
        """IDs of every message in a thread. Shape: {"thread": {"id", "messages": [{"id", ...}]}}."""
        res = self._run(["gmail", "thread", "get", thread_id], GMAIL_COMMANDS, gmail=True)
        thread = res.get("thread") if isinstance(res, dict) else None
        messages = thread.get("messages") if isinstance(thread, dict) else None
        ids = [m["id"] for m in messages or [] if isinstance(m, dict) and isinstance(m.get("id"), str)]
        if not ids:
            raise GogError(f"could not read message ids from thread {thread_id}")
        return list(dict.fromkeys(ids))

    def get_message(self, message_id):
        return self._run(["gmail", "get", message_id], GMAIL_COMMANDS, gmail=True)

    def tab_titles(self, spreadsheet_id):
        """Tab names. Shape: {"sheets": [{"properties": {"title": "swiggy", ...}}], "title": <spreadsheet>}."""
        meta = self._run(["sheets", "metadata", spreadsheet_id], SHEETS_COMMANDS)
        sheets = meta.get("sheets") if isinstance(meta, dict) else None
        if not isinstance(sheets, list):
            raise GogError("sheets metadata: no 'sheets' list in the response")
        return {s["properties"]["title"] for s in sheets
                if isinstance(s, dict) and isinstance(s.get("properties"), dict) and "title" in s["properties"]}

    def tab_ids(self, spreadsheet_id):
        """{tab title: numeric sheetId}, needed to address cells in a batch request."""
        meta = self._run(["sheets", "metadata", spreadsheet_id], SHEETS_EDIT_COMMANDS)
        return {s["properties"]["title"]: s["properties"]["sheetId"]
                for s in (meta.get("sheets") or []) if "sheetId" in s.get("properties", {})}

    def batch_request(self, spreadsheet_id, requests):
        self._run(["sheets", "batch-request", spreadsheet_id,
                   "--requests-json", json.dumps(requests, ensure_ascii=False), "--force"],
                  SHEETS_EDIT_COMMANDS)

    def read_values(self, spreadsheet_id, a1_range):
        res = self._run(["sheets", "get", spreadsheet_id, a1_range, "--render", "UNFORMATTED_VALUE",
                         "--dimension", "ROWS"],
                        SHEETS_COMMANDS)
        if isinstance(res, dict):
            return res.get("values") or []
        return res or []

    def add_tab(self, spreadsheet_id, tab):
        self._run(["sheets", "add-tab", spreadsheet_id, tab], SHEETS_COMMANDS)

    def append_rows(self, spreadsheet_id, a1_range, rows):
        self._run(["sheets", "append", spreadsheet_id, a1_range,
                   "--values-json", json.dumps(rows, ensure_ascii=False),
                   "--input", "USER_ENTERED", "--insert", "INSERT_ROWS"], SHEETS_COMMANDS)
