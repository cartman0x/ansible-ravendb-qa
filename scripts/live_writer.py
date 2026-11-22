#!/usr/bin/env python3
"""
live_writer.py -- user writes to EXISTING documents on every node of a cluster while it is being
upgraded, recording exactly which writes the server accepted, so a test can check afterwards that
every node holds exactly those revisions.

  run        Updates documents <prefix>/<n> for n = 0, every, 2*every, ... < docs.  Each document
             is only ever written on ONE home node (round-robin): writes from two nodes would
             conflict and make the expected revision count unknowable.  --threads threads share
             the documents, each writing its documents in turn, --interval seconds apart.
             One CSV row per write: ts, ms, node, doc, v, status, outcome.
               ok         HTTP 201 -- the write was applied (one new revision)
               refused    no connection (the node is down, e.g. being upgraded) -- not applied
               http_err   the server answered with an error status -- not applied
               uncertain  the request may have reached the server but no answer came back (timeout,
                          connection reset): applied or not is unknown.  That document gets no more
                          writes, so it holds 0 or 1 extra revision -- the check allows both.
             No retries (a retried PUT could apply twice).  Runs until --stop-file exists.
  summarize  JSON: per doc {node, ok, uncertain}, totals per outcome and per HTTP status, and per
             named window (--window NAME:START:END, epoch seconds) the outcomes per node.

The body is {"v": <number>, "@metadata": {"@collection": ...}}; v starts at --v-start and goes up
by one per write to that document, so every written revision is distinct.

Usage:
  live_writer.py run --nodes 1a,1b,1c --db db_rolling --prefix rkc8/plain --collection Plain \\
      --docs 200000 --every 100 --out /tmp/w.csv --stop-file /tmp/w.stop
  live_writer.py summarize --csv /tmp/w.csv --window cleanup-1a:1700000000:1700000600
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_REPO, "module_utils"))

FIELDS = ["ts", "ms", "node", "doc", "v", "status", "outcome"]


def doc_plan(prefix, docs, every, nodes):
    """[(doc id, home node)] -- the written documents and where each one is written."""
    ids = range(0, docs, every)
    return [("%s/%d" % (prefix, n), nodes[k % len(nodes)]) for k, n in enumerate(ids)]


def classify(exc):
    """(status, outcome) for an exception raised by one PUT."""
    if isinstance(exc, HTTPError):
        return exc.code, "http_err"
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
        return 0, "refused"                    # never connected: nothing was sent
    if isinstance(reason, OSError) and getattr(reason, "errno", None) in (111, 113):
        return 0, "refused"                    # ECONNREFUSED / EHOSTUNREACH
    return 0, "uncertain"


def put(url, body, ctx, timeout):
    data = json.dumps(body).encode()
    req = Request(url, data=data, method="PUT",
                  headers={"Content-Type": "application/json", "Connection": "close"})
    with urlopen(req, context=ctx, timeout=timeout) as r:
        r.read()
        return r.status


def run(a):
    import ravendb_client as rc
    ctx = rc._ssl_context_for(a.cert, a.ca)
    plan = doc_plan(a.prefix, a.docs, a.every, a.nodes.split(","))
    lock = threading.Lock()
    new = not os.path.exists(a.out) or os.path.getsize(a.out) == 0
    out = open(a.out, "a")
    if new:
        out.write(",".join(FIELDS) + "\n")
        out.flush()

    def worker(mine):
        v = {doc: a.v_start for doc, _ in mine}
        dead = set()                           # docs with an uncertain write: no more writes
        while not os.path.exists(a.stop_file):
            for doc, node in mine:
                if os.path.exists(a.stop_file):
                    return
                if doc in dead:
                    continue
                url = "https://%s.%s:443/databases/%s/docs?id=%s" % (node, a.domain, a.db, quote(doc))
                body = {"v": v[doc], "@metadata": {"@collection": a.collection}}
                t0 = time.time()
                try:
                    status, outcome = put(url, body, ctx, a.timeout), "ok"
                    if status != 201:
                        outcome = "http_err"
                except Exception as e:      # noqa: BLE001 -- every failure is recorded
                    status, outcome = classify(e)
                ms = (time.time() - t0) * 1000
                if outcome == "ok":
                    v[doc] += 1
                elif outcome == "uncertain":
                    dead.add(doc)
                with lock:
                    out.write("%.3f,%.1f,%s,%s,%d,%d,%s\n" % (t0, ms, node, doc, body["v"], status, outcome))
                    out.flush()
                time.sleep(a.interval if outcome == "ok" else max(a.interval, 0.5))
            if len(dead) == len(mine):
                return

    threads = [threading.Thread(target=worker, args=(plan[i::a.threads],), daemon=True)
               for i in range(min(a.threads, len(plan)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    out.close()
    return 0


def load_csv(path):
    rows = []
    with open(path) as f:
        header = f.readline().strip().split(",")
        for line in f:
            vals = line.rstrip("\n").split(",")
            if len(vals) != len(header):
                continue                       # a row cut off mid-write
            r = dict(zip(header, vals))
            r["ts"] = float(r["ts"])
            r["status"] = int(r["status"])
            rows.append(r)
    return rows


def summarize_rows(rows, windows):
    docs, totals, statuses = {}, {}, {}
    for r in rows:
        d = docs.setdefault(r["doc"], {"node": r["node"], "ok": 0, "uncertain": False})
        if r["outcome"] == "ok":
            d["ok"] += 1
        elif r["outcome"] == "uncertain":
            d["uncertain"] = True
        totals[r["outcome"]] = totals.get(r["outcome"], 0) + 1
        if r["outcome"] == "http_err":
            statuses[str(r["status"])] = statuses.get(str(r["status"]), 0) + 1
    out_windows = {}
    for name, start, end in windows:
        w = {}
        for r in rows:
            if start <= r["ts"] <= end:
                per = w.setdefault(r["node"], {})
                per[r["outcome"]] = per.get(r["outcome"], 0) + 1
        out_windows[name] = w
    return {"docs": docs, "totals": totals, "http_statuses": statuses,
            "first_ts": rows[0]["ts"] if rows else None, "last_ts": rows[-1]["ts"] if rows else None,
            "windows": out_windows}


def summarize(a):
    windows = []
    for spec in a.window:
        name, s, e = spec.rsplit(":", 2)
        windows.append((name, float(s), float(e)))
    print(json.dumps(summarize_rows(load_csv(a.csv), windows)))
    return 0


def main(argv=None):
    cert_dir = os.environ.get("CERT_DIR", os.path.expanduser("~/selfsignedmaterials"))
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--nodes", required=True, help="comma-separated, e.g. 1a,1b,1c")
    r.add_argument("--db", required=True)
    r.add_argument("--prefix", required=True)
    r.add_argument("--collection", required=True)
    r.add_argument("--docs", type=int, required=True, help="ids <prefix>/0 .. <prefix>/<docs-1> exist")
    r.add_argument("--every", type=int, default=100, help="write every N-th of them")
    r.add_argument("--threads", type=int, default=6)
    r.add_argument("--interval", type=float, default=0.05, help="seconds between a thread's writes")
    r.add_argument("--timeout", type=float, default=60)
    r.add_argument("--v-start", type=int, default=1_000_000)
    r.add_argument("--out", required=True)
    r.add_argument("--stop-file", required=True)
    r.add_argument("--domain", default="hubsink.test")
    r.add_argument("--cert", default=os.path.join(cert_dir, "client.pem"))
    r.add_argument("--ca", default=os.path.join(cert_dir, "ca.crt"))
    m = sub.add_parser("summarize")
    m.add_argument("--csv", required=True)
    m.add_argument("--window", action="append", default=[], metavar="NAME:START:END")
    a = p.parse_args(argv)
    return run(a) if a.cmd == "run" else summarize(a)


if __name__ == "__main__":
    sys.exit(main())
