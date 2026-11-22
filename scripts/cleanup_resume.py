#!/usr/bin/env python3
"""
cleanup_resume.py -- did the revisions key cleanup CONTINUE after a restart, or start over?

Reads every "revisions key cleanup" log line of ONE database on stdin (grep -i: the error line
says "...in the revisions key cleanup", lower case), sorted by timestamp (LC_ALL=C sort), and
the restart points as line counts (--restart C_RESTART:C_UP, once per
restart, in order):
  C_RESTART  number of cleanup lines in the log just before the restart was issued
  C_UP       number of cleanup lines once the server answered again, before the db was loaded
             (lines C_RESTART..C_UP-1 were logged by the old process while it shut down)
Prints one JSON report and exits 0 when it continued every time, 1 when it did not, 2 on bad input.

The cleanup persists step, cursor, end, document and LastRawEtag with every batch; LastRawEtag
is set once, by the first batch, and the scenario writes a marker document before every restart.
So a resumed cleanup has the same LastRawEtag on every line, and the position never goes back
(lines are logged only after their batch commits, so this holds for SIGKILL too).
Position order: documents by lower-cased id byte order; per document Revisions in progress (by
cursor) < RevisionAttachments < RevisionAttachmentTombstones < Revisions not in progress; then
RevisionsTombstones (by cursor), then Done.  In the attachment steps "document" is an attachment
key <lower id><RS>r<RS>...; the part before the first control character is the document id.
"""

import argparse
import json
import re
import sys

PROGRESS = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?)\|.*?Revisions key cleanup progress: "
    r"step (?P<step>\w+), cursor (?P<cursor>\d+), end (?P<end>\d+), "
    r"document in progress (?P<in_progress>\w+), last raw etag (?P<last_raw_etag>\d+), "
    r"document '(?P<doc>.*)', rows \d+")
DONE = "Revisions key cleanup is done"
ERROR = re.compile(r"(?i)error|exception|fail")

DOC_SUB = {"RevisionAttachments": 1, "RevisionAttachmentTombstones": 2}


def parse(line):
    m = PROGRESS.match(line)
    if not m:
        return None
    r = m.groupdict()
    for k in ("cursor", "end", "last_raw_etag"):
        r[k] = int(r[k])
    r["in_progress"] = r["in_progress"].lower() == "true"
    return r


def position(r):
    step = r["step"]
    if step == "RevisionsTombstones":
        return (1, "", 0, "", r["cursor"])
    if step == "Done":
        return (2, "", 0, "", 0)
    parts = re.split(r"([\x00-\x1f])", r["doc"], maxsplit=1)
    head, rest = parts[0], "".join(parts[1:])
    if step == "Revisions":
        return (0, head, 0, "", r["cursor"]) if r["in_progress"] else (0, head, 3, "", 0)
    if step in DOC_SUB:
        return (0, head, DOC_SUB[step], rest, 0)
    raise ValueError("unknown cleanup step %r" % step)


def brief(r):
    if r is None:
        return None
    doc = re.sub(r"[\x00-\x1f]", "~", r["doc"])
    return "%s step %s, cursor %d, end %d, document '%s'" % (r["ts"], r["step"], r["cursor"], r["end"], doc)


def check(lines, restarts, allow_shutdown_errors=False):
    """lines: sorted cleanup lines of one db.  restarts: [(c_restart, c_up), ...]."""
    problems = []
    lines = [l.rstrip("\n") for l in lines if l.strip()]
    for i, (cr, cu) in enumerate(restarts):
        if not 0 <= cr <= cu <= len(lines):
            raise ValueError("restart %d: bad counts %d:%d for %d lines" % (i + 1, cr, cu, len(lines)))
        if i and cr < restarts[i - 1][1]:
            raise ValueError("restart %d starts before restart %d ended" % (i + 1, i))

    recs = [(i, parse(l)) for i, l in enumerate(lines)]
    progress = [(i, r) for i, r in recs if r]
    unparsed = [l for l, (_, r) in zip(lines, recs) if r is None and "Revisions key cleanup progress:" in l]
    if unparsed:
        problems.append("progress line(s) in an unknown format (first): %s" % unparsed[0][:300])

    etags = sorted({r["last_raw_etag"] for _, r in progress})
    if len(etags) > 1:
        first_change = next(r for _, r in progress if r["last_raw_etag"] != progress[0][1]["last_raw_etag"])
        problems.append("LastRawEtag changed %s -> it started over (first line with %d: %s)"
                        % (etags, first_change["last_raw_etag"], brief(first_change)))

    back = []
    for (_, a), (_, b) in zip(progress, progress[1:]):
        if position(b) < position(a):
            back.append("%s  ->  %s" % (brief(a), brief(b)))
    if back:
        problems.append("position went back %d time(s), first: %s" % (len(back), back[0]))

    bounds = [0] + [cu for _, cu in restarts] + [len(lines)]
    report = []
    for n, (cr, cu) in enumerate(restarts, 1):
        seg_start, seg_next_end = bounds[n - 1], bounds[n + 1]
        before = [r for i, r in progress if seg_start <= i < cu]
        after = [r for i, r in progress if cu <= i < seg_next_end]
        done_before = [l for l in lines[:cu] if DONE in l]
        any_after = lines[cu:seg_next_end]
        if not before:
            problems.append("restart %d: no progress line before it -- it did not hit the cleanup" % n)
        if done_before:
            problems.append("restart %d: the cleanup was already done before it" % n)
        if not any_after:
            problems.append("restart %d: no cleanup line after it -- the cleanup did not continue" % n)
        last, first = (before[-1] if before else None), (after[0] if after else None)
        report.append({
            "restart": n,
            "last_before": brief(last),
            "first_after": brief(first) if first else
                           ("(none -- next line: %s)" % any_after[0][:200] if any_after else None),
            "same_position": bool(last and first and position(first) == position(last)),
            "last_raw_etag_before": last["last_raw_etag"] if last else None,
            "last_raw_etag_after": first["last_raw_etag"] if first else None,
        })

    # errors while the old process shut down are allowed only with allow_shutdown_errors
    shutdown = set()
    for cr, cu in restarts:
        shutdown.update(range(cr, cu))
    errors = [(i, l) for i, l in enumerate(lines) if ERROR.search(l)]
    at_shutdown = [l for i, l in errors if i in shutdown]
    other = [l for i, l in errors if i not in shutdown]
    if other:
        problems.append("%d cleanup error line(s), first: %s" % (len(other), other[0][:400]))
    if at_shutdown and not allow_shutdown_errors:
        problems.append("%d cleanup error line(s) while the server shut down, first: %s"
                        % (len(at_shutdown), at_shutdown[0][:400]))

    return {
        "ok": not problems,
        "problems": problems,
        "restarts": report,
        "progress_lines": len(progress),
        "last_raw_etags": etags,
        "done": any(DONE in l for l in lines),
        "shutdown_errors": [l[:400] for l in at_shutdown],
    }


def timeline(lines):
    """First/last progress timestamp per document and per step (accurate to about one batch)."""
    progress = [r for r in (parse(l) for l in lines if l.strip()) if r]
    done = next((l[:24].rstrip("|") for l in lines if DONE in l), None)
    docs, steps = {}, {}
    for r in progress:
        head = re.split(r"[\x00-\x1f]", r["doc"], maxsplit=1)[0]
        if r["step"] not in ("RevisionsTombstones", "Done") and head:
            d = docs.setdefault(head, {"first": r["ts"], "lines": 0})
            d["last"] = r["ts"]
            d["lines"] += 1
        s = steps.setdefault(r["step"], {"first": r["ts"], "lines": 0})
        s["last"] = r["ts"]
        s["lines"] += 1
    return {"first_progress": progress[0]["ts"] if progress else None, "done": done,
            "progress_lines": len(progress), "documents": docs, "steps": steps}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--restart", action="append", default=[], metavar="C_RESTART:C_UP")
    p.add_argument("--allow-shutdown-errors", action="store_true")
    p.add_argument("--timeline", action="store_true",
                   help="instead of the resume check: per-document and per-step times (JSON)")
    a = p.parse_args(argv)
    if a.timeline:
        print(json.dumps(timeline(sys.stdin.read().split("\n"))))
        return 0
    try:
        restarts = [tuple(int(x) for x in s.split(":")) for s in a.restart]
        if any(len(t) != 2 for t in restarts):
            raise ValueError("--restart takes C_RESTART:C_UP")
        # split("\n"), NOT splitlines(): splitlines also splits on \x1e, the record separator
        # inside the attachment keys that the attachment-step progress lines print.
        out = check(sys.stdin.read().split("\n"), restarts, a.allow_shutdown_errors)
    except ValueError as e:
        print(json.dumps({"ok": False, "problems": ["bad input: %s" % e]}))
        return 2
    print(json.dumps(out))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
