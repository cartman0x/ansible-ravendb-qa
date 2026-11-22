#!/usr/bin/env python3
"""
container_metrics.py -- CPU, memory and disk I/O of one Docker container, sampled from the HOST.

  sample     every --interval seconds, append one CSV row read from the container's cgroup (v2):
             ts (epoch, this machine's clock), cpu_usec (cpu.stat usage_usec), mem_bytes
             (memory.current), rbytes / wbytes / rios / wios (io.stat, summed over devices),
             anon_bytes (memory.stat anon: WITHOUT the page cache, which memory.current
             includes and which for a big database is most of the host's RAM).
             Runs until --stop-file exists (or --max-secs).  Nothing runs inside the container.
  summarize  per named time window (--window NAME:START:END, epoch seconds), JSON:
             cpu_pct avg / peak (100 = one full core), mem_mb avg / peak (with page cache),
             mem_anon_mb avg / peak (without; CSVs from before it have no anon column),
             read / write MB/s avg / peak, MB read / written, IOPS avg.
             "peak" is the highest single-interval value.

The cgroup is /sys/fs/cgroup/system.slice/docker-<id>.scope (systemd cgroup driver) or
/sys/fs/cgroup/docker/<id> (cgroupfs driver) -- found from `docker inspect`.  The counters are
cumulative, so a missed sample loses resolution, not data.

Usage:
  container_metrics.py sample --container 1a --interval 2 --out /tmp/m.csv --stop-file /tmp/m.stop
  container_metrics.py summarize --csv /tmp/m.csv --window idle:1700000000:1700000120 ...
"""

import argparse
import json
import os
import subprocess
import sys
import time

FIELDS = ["ts", "cpu_usec", "mem_bytes", "rbytes", "wbytes", "rios", "wios", "anon_bytes"]


def cgroup_dir(container, root="/sys/fs/cgroup"):
    cid = subprocess.check_output(["docker", "inspect", "-f", "{{.Id}}", container], text=True).strip()
    for d in ("%s/system.slice/docker-%s.scope" % (root, cid), "%s/docker/%s" % (root, cid)):
        if os.path.isfile(os.path.join(d, "cpu.stat")):
            return d
    raise RuntimeError("no cgroup v2 folder found for container %s (%s)" % (container, cid[:12]))


def parse_cpu_stat(text):
    for line in text.splitlines():
        k, _, v = line.partition(" ")
        if k == "usage_usec":
            return int(v)
    raise ValueError("cpu.stat has no usage_usec")


def parse_io_stat(text):
    """Sum rbytes/wbytes/rios/wios over every device line ('8:48 rbytes=.. wbytes=..')."""
    out = {"rbytes": 0, "wbytes": 0, "rios": 0, "wios": 0}
    for line in text.splitlines():
        for kv in line.split()[1:]:
            k, _, v = kv.partition("=")
            if k in out:
                out[k] += int(v)
    return out


def parse_memory_anon(text):
    for line in text.splitlines():
        k, _, v = line.partition(" ")
        if k == "anon":
            return int(v)
    raise ValueError("memory.stat has no anon")


def read_sample(d):
    def rd(name):
        with open(os.path.join(d, name)) as f:
            return f.read()
    row = {"ts": round(time.time(), 3), "cpu_usec": parse_cpu_stat(rd("cpu.stat")),
           "mem_bytes": int(rd("memory.current").strip()),
           "anon_bytes": parse_memory_anon(rd("memory.stat"))}
    row.update(parse_io_stat(rd("io.stat")))
    return row


def sample(a):
    d = cgroup_dir(a.container)
    end = time.time() + a.max_secs if a.max_secs else None
    new = not os.path.exists(a.out) or os.path.getsize(a.out) == 0
    with open(a.out, "a") as f:
        if new:
            f.write(",".join(FIELDS) + "\n")
        while not os.path.exists(a.stop_file) and (end is None or time.time() < end):
            try:
                row = read_sample(d)
            except FileNotFoundError:
                break                      # the container is gone
            f.write(",".join(str(row[k]) for k in FIELDS) + "\n")
            f.flush()
            time.sleep(a.interval)
    return 0


def load_csv(path):
    rows = []
    with open(path) as f:
        header = f.readline().strip().split(",")
        for line in f:
            vals = line.strip().split(",")
            if len(vals) != len(header):
                continue                   # a row cut off mid-write
            rows.append({k: float(v) for k, v in zip(header, vals)})
    return rows


def summarize_window(rows, start, end):
    """Rates come from consecutive samples both inside [start, end]."""
    inside = [r for r in rows if start <= r["ts"] <= end]
    if len(inside) < 2:
        return {"samples": len(inside), "note": "fewer than 2 samples in the window"}
    cpu, rd, wr = [], [], []
    for a, b in zip(inside, inside[1:]):
        dt = b["ts"] - a["ts"]
        if dt <= 0:
            continue
        cpu.append((b["cpu_usec"] - a["cpu_usec"]) / 1e6 / dt * 100)
        rd.append((b["rbytes"] - a["rbytes"]) / dt / 1e6)
        wr.append((b["wbytes"] - a["wbytes"]) / dt / 1e6)
    first, last = inside[0], inside[-1]
    span = last["ts"] - first["ts"]
    mem = [r["mem_bytes"] / 2 ** 20 for r in inside]
    r1 = lambda x: round(x, 1)
    out = {
        "samples": len(inside),
        "secs": r1(span),
        "cpu_pct_avg": r1((last["cpu_usec"] - first["cpu_usec"]) / 1e6 / span * 100),
        "cpu_pct_peak": r1(max(cpu)),
        "mem_mb_avg": r1(sum(mem) / len(mem)),
        "mem_mb_peak": r1(max(mem)),
        "read_mb_s_avg": r1((last["rbytes"] - first["rbytes"]) / span / 1e6),
        "read_mb_s_peak": r1(max(rd)),
        "write_mb_s_avg": r1((last["wbytes"] - first["wbytes"]) / span / 1e6),
        "write_mb_s_peak": r1(max(wr)),
        "read_mb_total": r1((last["rbytes"] - first["rbytes"]) / 1e6),
        "write_mb_total": r1((last["wbytes"] - first["wbytes"]) / 1e6),
        "iops_avg": r1((last["rios"] + last["wios"] - first["rios"] - first["wios"]) / span),
    }
    if all("anon_bytes" in r for r in inside):
        anon = [r["anon_bytes"] / 2 ** 20 for r in inside]
        out["mem_anon_mb_avg"] = r1(sum(anon) / len(anon))
        out["mem_anon_mb_peak"] = r1(max(anon))
    return out


def parse_windows(specs):
    out = []
    for s in specs:
        name, start, end = s.rsplit(":", 2)
        out.append((name, float(start), float(end)))
    return out


def summarize(a):
    rows = load_csv(a.csv)
    print(json.dumps({name: summarize_window(rows, s, e) for name, s, e in parse_windows(a.window)}))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--container", required=True)
    s.add_argument("--interval", type=float, default=2)
    s.add_argument("--out", required=True)
    s.add_argument("--stop-file", required=True)
    s.add_argument("--max-secs", type=float, default=0)
    m = sub.add_parser("summarize")
    m.add_argument("--csv", required=True)
    m.add_argument("--window", action="append", default=[], metavar="NAME:START:END")
    a = p.parse_args(argv)
    return sample(a) if a.cmd == "sample" else summarize(a)


if __name__ == "__main__":
    sys.exit(main())
