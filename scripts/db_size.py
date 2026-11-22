#!/usr/bin/env python3
"""
db_size.py -- data file and journal sizes of RavenDB databases in a Docker container.

Per database folder (<root>/<db>, default root /var/lib/ravendb/data/Databases):
  voron_bytes        Raven.voron, nominal size (Voron pre-allocates: grows in steps, never shrinks)
  voron_alloc_bytes  Raven.voron, disk blocks actually allocated (the file can be sparse)
  journal_bytes      Journals/*, nominal size of every journal file (256 MB each by default)
  journal_files      journal files in use (*.journal)
  recyclable_files   journal files kept for reuse (recyclable-journal.*)
  temp_bytes         Temp/* (scratch buffers)
  total_alloc_bytes  the whole db folder, disk blocks allocated (du)
One `docker exec` per reading, file metadata only (stat / du): it never touches RavenDB itself.

  snapshot   one reading now -> JSON {db: {field: value}} ({db: null} if the folder is missing)
  sample     a reading every --interval seconds -> CSV (ts, db, fields...) until --stop-file;
             a reading that fails (server restarting, db not created yet) is skipped
  summarize  per window (--window NAME:START:END, epoch) and db: first / last / peak of each field

Usage:
  db_size.py snapshot  --container 1a --dbs db_a,db_b
  db_size.py sample    --container 1a --dbs db_a,db_b --interval 15 --out s.csv --stop-file s.stop
  db_size.py summarize --csv s.csv --window cleanup:1700000000:1700003600
"""

import argparse
import json
import os
import subprocess
import sys
import time

ROOT = "/var/lib/ravendb/data/Databases"
FIELDS = ["voron_bytes", "voron_alloc_bytes", "journal_bytes", "journal_files",
          "recyclable_files", "temp_bytes", "total_alloc_bytes"]

# For each db: "DB <name>", then "F <path>|<size>|<blocks of 512>" per file, "DU <kbytes>".
_SCRIPT = r'''
for db in "$@"; do
  d="$ROOT/$db"
  echo "DB $db"
  [ -d "$d" ] || { echo "MISSING"; continue; }
  stat -c 'F %n|%s|%b' "$d/Raven.voron" "$d"/Journals/* "$d"/Temp/* 2>/dev/null
  echo "DU $(du -sk "$d" 2>/dev/null | cut -f1)"
done
'''


def parse(text, root=ROOT):
    """Output of _SCRIPT -> {db: {field: int} or None}."""
    out, cur, db = {}, None, None
    for line in text.split("\n"):
        if line.startswith("DB "):
            db = line[3:].strip()
            cur = out[db] = {f: 0 for f in FIELDS}
        elif cur is None:
            continue
        elif line == "MISSING":
            out[db] = cur = None
        elif line.startswith("F "):
            path, size, blocks = line[2:].rsplit("|", 2)
            size, alloc = int(size), int(blocks) * 512
            rel = path[len(root) + 1:].split("/", 1)[1] if path.startswith(root + "/") else path
            name = rel.rsplit("/", 1)[-1]
            if rel == "Raven.voron":
                cur["voron_bytes"], cur["voron_alloc_bytes"] = size, alloc
            elif rel.startswith("Journals/"):
                cur["journal_bytes"] += size
                if name.startswith("recyclable-journal"):
                    cur["recyclable_files"] += 1
                elif name.endswith(".journal"):
                    cur["journal_files"] += 1
            elif rel.startswith("Temp/"):
                cur["temp_bytes"] += size
        elif line.startswith("DU "):
            v = line[3:].strip()
            cur["total_alloc_bytes"] = int(v) * 1024 if v.isdigit() else 0
    return out


def read(container, dbs, root=ROOT):
    text = subprocess.check_output(
        ["docker", "exec", "-e", "ROOT=" + root, container, "sh", "-c", _SCRIPT, "sh"] + list(dbs),
        text=True, timeout=120)
    return parse(text, root)


def snapshot(a):
    print(json.dumps(read(a.container, a.dbs.split(","), a.root)))
    return 0


def sample(a):
    dbs = a.dbs.split(",")
    end = time.time() + a.max_secs if a.max_secs else None
    new = not os.path.exists(a.out) or os.path.getsize(a.out) == 0
    with open(a.out, "a") as f:
        if new:
            f.write(",".join(["ts", "db"] + FIELDS) + "\n")
        while not os.path.exists(a.stop_file) and (end is None or time.time() < end):
            ts = time.time()
            try:
                for db, v in read(a.container, dbs, a.root).items():
                    if v:
                        f.write(",".join(["%.3f" % ts, db] + [str(v[k]) for k in FIELDS]) + "\n")
                f.flush()
            except (subprocess.SubprocessError, OSError, ValueError):
                pass                      # container busy / restarting: skip this reading
            time.sleep(max(0.0, a.interval - (time.time() - ts)))
    return 0


def load_csv(path):
    rows = []
    with open(path) as f:
        f.readline()
        for line in f:
            vals = line.strip().split(",")
            if len(vals) == len(FIELDS) + 2:
                rows.append({"ts": float(vals[0]), "db": vals[1],
                             **{k: int(v) for k, v in zip(FIELDS, vals[2:])}})
    return rows


def summarize_window(rows, start, end):
    out = {}
    for r in rows:
        if start <= r["ts"] <= end:
            d = out.setdefault(r["db"], {"samples": 0})
            d["samples"] += 1
            for k in FIELDS:
                v = d.setdefault(k, {"first": r[k], "last": r[k], "peak": r[k]})
                v["last"] = r[k]
                v["peak"] = max(v["peak"], r[k])
    return out


def summarize(a):
    rows = load_csv(a.csv)
    out = {}
    for spec in a.window:
        name, s, e = spec.rsplit(":", 2)
        out[name] = summarize_window(rows, float(s), float(e))
    print(json.dumps(out))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("snapshot", "sample"):
        s = sub.add_parser(name)
        s.add_argument("--container", required=True)
        s.add_argument("--dbs", required=True, help="comma-separated database names")
        s.add_argument("--root", default=ROOT)
    s = sub.choices["sample"]
    s.add_argument("--interval", type=float, default=15)
    s.add_argument("--out", required=True)
    s.add_argument("--stop-file", required=True)
    s.add_argument("--max-secs", type=float, default=0)
    m = sub.add_parser("summarize")
    m.add_argument("--csv", required=True)
    m.add_argument("--window", action="append", default=[], metavar="NAME:START:END")
    a = p.parse_args(argv)
    return {"snapshot": snapshot, "sample": sample, "summarize": summarize}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
