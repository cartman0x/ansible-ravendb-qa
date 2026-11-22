#!/usr/bin/env python3
"""
cleanup_eta.py -- how far the revisions key cleanup is, and whether it will finish in time.

Reads the cleanup's progress lines (stdin) and prints one line: progress, rate, ETA, verdict.
Exit code 0 = keep waiting, 3 = TOO SLOW (projected total time is over --max-hours).

The document steps walk every document with revisions in byte order of its lower-cased id; their
"cursor ... end ..." is only the position inside the current document, so overall progress is the
document's rank in --universe ([{"prefix": "lg/att", "docs": N}, ...], ids <prefix>/0 .. <prefix>/<N-1>),
computed arithmetically.  In the RevisionsTombstones step cursor/end IS the progress.
Times come from the log lines (server clock).

Usage:
  docker exec 1a sh -c 'grep -rhF "Revisions key cleanup progress" /var/log/ravendb/logs' \
    | grep -F '|db_large|' \
    | cleanup_eta.py --universe '[{"prefix":"lg/att","docs":5000000}, ...]' --max-hours 20
"""

import argparse
import json
import re
import sys
from datetime import datetime

LINE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?)\|.*?Revisions key cleanup progress: "
    r"step (?P<step>\w+), cursor (?P<cursor>\d+), end (?P<end>\d+),.*?document '(?P<doc>[^']*)'")

DOC_STEPS = ("Revisions", "RevisionAttachments", "RevisionAttachmentTombstones")
TOMBSTONE_STEP = "RevisionsTombstones"


def numeric_rank(s, n):
    """How many of the strings str(0) .. str(n-1) sort before s (byte order)."""
    if n <= 0:
        return 0
    if not s.isdigit():
        # not <prefix>/<number>: place it right after its leading digits
        digits = re.match(r"\d*", s).group()
        if not digits:
            return 0 if s < "0" else n
        valid = (len(digits) == 1 or digits[0] != "0") and int(digits) < n
        return numeric_rank(digits, n) + (1 if valid else 0)
    count = 0
    for length in range(1, len(str(n - 1)) + 1):
        lo = 0 if length == 1 else 10 ** (length - 1)
        hi = min(10 ** length - 1, n - 1)
        if lo > hi:
            continue
        k = min(length, len(s))
        head = s[:k]
        # numbers whose first k digits are smaller than head
        bound = int(head) * 10 ** (length - k)
        count += max(0, min(hi, bound - 1) - lo + 1)
        # a number equal to a proper prefix of s sorts before s
        if length < len(s):
            t = int(s[:length])
            if (length == 1 or s[0] != "0") and lo <= t <= hi:
                count += 1
    return count


def doc_rank(doc_id, universe):
    """Position of doc_id among all universe ids (0-based), in byte order of the lower-cased id.
    In the attachment steps the line shows a longer key: '<id>\\x1er\\x1e...' -- keep the id."""
    doc = re.split(r"[\x00-\x1f]", doc_id, maxsplit=1)[0].lower()
    rank = 0
    for shape in universe:
        p = shape["prefix"].lower().rstrip("/") + "/"
        n = int(shape["docs"])
        if doc.startswith(p):
            rank += numeric_rank(doc[len(p):], n)
        elif doc > p:
            rank += n
    return rank


def parse(lines):
    out = []
    for line in lines:
        m = LINE.search(line)
        if not m:
            continue
        ts = m["ts"]
        if "." in ts:
            base, frac = ts.split(".")
            ts = base + "." + (frac + "000000")[:6]
        else:
            ts += ".000000"
        out.append({"t": datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f"), "step": m["step"],
                    "cursor": int(m["cursor"]), "end": int(m["end"]), "doc": m["doc"]})
    return out


def hours(td):
    return td.total_seconds() / 3600


def fmt_h(h):
    return f"{h:.1f} h" if h < 100 else f"{h:.0f} h ({h / 24:.1f} days)"


def estimate(points, universe, max_hours, window_min=30, min_observe_min=30):
    """Returns (verdict, message).  verdict: WAIT (not enough data) | OK | TOO_SLOW."""
    if not points:
        return "WAIT", "key cleanup: no progress line yet"
    first, last = points[0], points[-1]
    elapsed_h = hours(last["t"] - first["t"])
    total = sum(int(s["docs"]) for s in universe)

    if last["step"] == TOMBSTONE_STEP:
        steps = [p for p in points if p["step"] == TOMBSTONE_STEP]
        c0 = steps[0]["cursor"]
        span = max(last["end"] - c0, 1)
        done = (last["cursor"] - c0) / span
        what = f"documents done; tombstones step {done:.1%} (etag {last['cursor']} of {last['end']})"
        series = [(p["t"], (p["cursor"] - c0) / span) for p in steps]
        remaining = 1 - done
    else:
        pos = doc_rank(last["doc"], universe)
        done = pos / total if total else 0
        what = f"documents {pos:,} / {total:,} ({done:.1%}), at '{last['doc']}'"
        series = [(p["t"], doc_rank(p["doc"], universe) / total) for p in points
                  if p["step"] in DOC_STEPS] if total else []
        remaining = 1 - done

    window_start = last["t"].timestamp() - window_min * 60
    base = next(((t, f) for t, f in series if t.timestamp() >= window_start), None)
    if base is None or elapsed_h * 60 < min_observe_min or last["t"] == base[0]:
        return "WAIT", f"key cleanup: {what}; running {fmt_h(elapsed_h)} -- too early for an ETA"
    rate = (series[-1][1] - base[1]) / hours(last["t"] - base[0])   # fraction per hour
    if rate <= 0:
        eta_h = float("inf")
        rate_txt = "no progress"
    else:
        eta_h = remaining / rate
        rate_txt = (f"{rate * total:,.0f} docs/h" if last["step"] != TOMBSTONE_STEP
                    else f"{rate:.1%}/h") + f" over the last {window_min} min"
    projected = elapsed_h + eta_h
    msg = (f"key cleanup: {what}; {rate_txt}; running {fmt_h(elapsed_h)}, "
           f"ETA {fmt_h(eta_h) if eta_h != float('inf') else 'never'} more"
           f" -> {fmt_h(projected) if projected != float('inf') else 'never'} in total")
    if last["step"] != TOMBSTONE_STEP:
        msg += " (+ the tombstones step after it)"
    if max_hours and projected > max_hours:
        return "TOO_SLOW", msg + f" -- OVER the {max_hours:g} h budget"
    return "OK", msg + (f", within the {max_hours:g} h budget" if max_hours else "")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--universe", required=True, help='JSON list: [{"prefix": "lg/att", "docs": 5000000}, ...]')
    ap.add_argument("--max-hours", type=float, default=0, help="budget for the whole cleanup; 0 = report only")
    ap.add_argument("--window-min", type=float, default=30, help="rate is measured over this many minutes")
    ap.add_argument("--min-observe-min", type=float, default=30, help="no verdict before this much progress")
    a = ap.parse_args(argv)
    verdict, msg = estimate(parse(sys.stdin), json.loads(a.universe), a.max_hours, a.window_min, a.min_observe_min)
    print(f"{verdict}: {msg}")
    return 3 if verdict == "TOO_SLOW" else 0


if __name__ == "__main__":
    sys.exit(main())
