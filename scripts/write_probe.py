#!/usr/bin/env python3
"""
write_probe.py -- a steady trickle of small user writes to one database, timing each one.

  run        one PUT every --interval seconds (single thread; a slow write delays the next, it
             never piles up), cycling over --docs document ids.  Appends a CSV row per write:
             ts (epoch, when the write started), ms, status.  Until --stop-file exists.
  summarize  per named window (--window NAME:START:END, epoch seconds), JSON: writes, errors
             (status not 201, or no response), p50 / p95 / p99 / max ms.

Each request opens a new TLS connection (module_utils/ravendb_client: Connection: close), so the
absolute numbers include the handshake -- compare windows with each other, not with a client
that pools connections.

Usage:
  write_probe.py run --target 1a --db db_bystander --out /tmp/p.csv --stop-file /tmp/p.stop
  write_probe.py summarize --csv /tmp/p.csv --window idle:1700000000:1700000120 ...
"""

import argparse
import json
import math
import os
import sys
import time

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_REPO, "module_utils"))


def percentile(sorted_vals, p):
    """Nearest-rank percentile of an ascending list."""
    if not sorted_vals:
        return None
    k = max(1, math.ceil(p / 100 * len(sorted_vals)))
    return sorted_vals[k - 1]


def run(a):
    import ravendb_client as rc
    new = not os.path.exists(a.out) or os.path.getsize(a.out) == 0
    n = 0
    with open(a.out, "a") as f:
        if new:
            f.write("ts,ms,status\n")
        while not os.path.exists(a.stop_file):
            doc = "probe/%d" % (n % a.docs)
            body = {"n": n, "pad": "x" * a.pad_bytes, "@metadata": {"@collection": "Probe"}}
            t0 = time.time()
            try:
                status, _ = rc.request("PUT", a.target, a.domain,
                                       "/databases/%s/docs?id=%s" % (a.db, doc),
                                       a.cert, a.ca, body=body, timeout=60)
            except Exception:
                status = 0
            ms = (time.time() - t0) * 1000
            f.write("%.3f,%.1f,%d\n" % (t0, ms, status))
            f.flush()
            n += 1
            time.sleep(max(0.0, a.interval - (time.time() - t0)))
    return 0


def load_csv(path):
    rows = []
    with open(path) as f:
        f.readline()
        for line in f:
            vals = line.strip().split(",")
            if len(vals) == 3:
                rows.append((float(vals[0]), float(vals[1]), int(vals[2])))
    return rows


def summarize_window(rows, start, end):
    inside = [r for r in rows if start <= r[0] <= end]
    ok = sorted(ms for _, ms, st in inside if st == 201)
    r1 = lambda x: None if x is None else round(x, 1)
    return {
        "writes": len(inside),
        "errors": sum(1 for _, _, st in inside if st != 201),
        "p50_ms": r1(percentile(ok, 50)),
        "p95_ms": r1(percentile(ok, 95)),
        "p99_ms": r1(percentile(ok, 99)),
        "max_ms": r1(ok[-1] if ok else None),
    }


def summarize(a):
    rows = load_csv(a.csv)
    out = {}
    for spec in a.window:
        name, s, e = spec.rsplit(":", 2)
        out[name] = summarize_window(rows, float(s), float(e))
    print(json.dumps(out))
    return 0


def main(argv=None):
    cert_dir = os.environ.get("CERT_DIR", os.path.expanduser("~/selfsignedmaterials"))
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--target", required=True)
    r.add_argument("--db", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--stop-file", required=True)
    r.add_argument("--interval", type=float, default=0.2)
    r.add_argument("--docs", type=int, default=100)
    r.add_argument("--pad-bytes", type=int, default=200)
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
