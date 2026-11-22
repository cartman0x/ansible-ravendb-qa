#!/usr/bin/env python3
"""
plot_metrics.py -- turn the CPU / memory / disk I/O samples of an RKC run into one self-contained
HTML page of charts.  Works for every scenario that runs scripts/container_metrics.py:

  RKC5, RKC7   --dir <the scenario's output dir>: reads metrics.csv, probe.csv, rkc*-report-*.json
               and, if present, probe-cleaning.csv (writes to a database being cleaned)
  RKC3         --csv /tmp/rkc-metrics-<db>.csv  (picks up /tmp/rkc-metrics-<db>.json next to it)
  anything     --csv FILE [--probe FILE] [--report FILE] [--cleanup START,END]

Draws, on one shared time axis (minutes since the first sample), with the cleanup shaded:
  CPU %  (100 = one core)  |  memory GB (with / without page cache)  |  disk read / write MB/s
  |  write latency p50 / p99 ms (when there is a probe)
plus the report's per-window averages as a table.  Hover shows every value at that time.

The cleanup is shaded from the report: RKC7 'cleanup', RKC5 'databases' (first start -> last
done), RKC3 'cleanup_epoch'.  --cleanup overrides it: epoch seconds or UTC 'YYYY-MM-DD HH:MM:SS'.

Usage:
  scripts/plot_metrics.py --dir /tmp/rkc7                         # -> /tmp/rkc7/metrics.html
  scripts/plot_metrics.py --dir /tmp/rkc5 --out ~/rkc5.html --bucket 5
  scripts/plot_metrics.py --csv /tmp/rkc-metrics-db_large.csv     # -> /tmp/rkc-metrics-db_large.html
"""

import argparse
import csv
import datetime as dt
import glob
import json
import math
import os
import sys


def load_metrics(path):
    """Cumulative counters -> per-interval rates: [{t, cpu, mem_gb, read, write}]."""
    with open(path, newline="") as f:
        rows = [{k: float(v) for k, v in r.items()} for r in csv.DictReader(f)]
    out = []
    for a, b in zip(rows, rows[1:]):
        dt_s = b["ts"] - a["ts"]
        if dt_s <= 0:
            continue
        r = {
            "t": b["ts"],
            "cpu": round((b["cpu_usec"] - a["cpu_usec"]) / 1e6 / dt_s * 100, 1),
            "mem_gb": round(b["mem_bytes"] / 2**30, 2),
            "read": round(max(b["rbytes"] - a["rbytes"], 0) / 2**20 / dt_s, 1),
            "write": round(max(b["wbytes"] - a["wbytes"], 0) / 2**20 / dt_s, 1),
            "riops": round(max(b["rios"] - a["rios"], 0) / dt_s),
            "wiops": round(max(b["wios"] - a["wios"], 0) / dt_s),
        }
        if "anon_bytes" in b:       # CSVs from before container_metrics.py sampled it have no column
            r["anon_gb"] = round(b["anon_bytes"] / 2**30, 2)
        out.append(r)
    return out


def pct(sorted_vals, p):
    k = max(1, math.ceil(p / 100 * len(sorted_vals)))
    return sorted_vals[k - 1]


def load_probe(path, bucket):
    """Write latency per `bucket`-second slot: [{t, p50, p99, errors}]."""
    if not os.path.isfile(path):
        return []
    slots = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            ts = float(r["ts"])
            s = slots.setdefault(int(ts // bucket), {"ms": [], "err": 0})
            if r.get("status") == "201" and r.get("ms"):
                s["ms"].append(float(r["ms"]))
            else:
                s["err"] += 1
    out = []
    for k in sorted(slots):
        ms = sorted(slots[k]["ms"])
        out.append({"t": (k + 0.5) * bucket,
                    "p50": pct(ms, 50) if ms else None,
                    "p99": pct(ms, 99) if ms else None,
                    "errors": slots[k]["err"]})
    return out


def utc_epoch(s):
    """'2026-11-16 09:54:08.8504' (server log time, UTC) -> epoch seconds."""
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc).timestamp()
        except ValueError:
            pass
    return None


def cleanup_window(report):
    """(start, done) epoch from an RKC3 ('cleanup_epoch'), RKC7 ('cleanup') or RKC5 ('databases') report."""
    if report.get("cleanup_epoch"):
        s, e = report["cleanup_epoch"]
        return float(s), float(e)
    starts, dones = [], []
    cl = report.get("cleanup")
    if isinstance(cl, dict) and cl and all(isinstance(v, dict) and "first" in v for v in cl.values()):
        # RKC8: per node {first, done} in epoch seconds
        f = [float(v["first"]) for v in cl.values() if v.get("first")]
        d = [float(v["done"]) for v in cl.values() if v.get("done")]
        return (min(f), max(d)) if f and d else None
    if isinstance(cl, dict):
        starts.append(cl.get("first_progress"))
        dones.append(cl.get("done"))
    for db in (report.get("databases") or {}).values():
        starts.append(db.get("first_progress"))
        dones.append(db.get("done"))
    starts = [utc_epoch(s) for s in starts if s]
    dones = [utc_epoch(s) for s in dones if s]
    starts, dones = [s for s in starts if s], [d for d in dones if d]
    return (min(starts), max(dones)) if starts and dones else None


def disk_cap_text(cap):
    """'259:0 rbps=100000000 wbps=100000000 riops=1500 wiops=1500' -> 'disk capped at 1500 IOPS, 100 MB/s'."""
    if not cap:
        return ""
    kv = dict(x.split("=", 1) for x in str(cap.get("limits", "")).split() if "=" in x)
    parts = []
    if kv.get("riops", "max") != "max":
        parts.append("%s IOPS" % kv["riops"])
    if kv.get("rbps", "max") != "max":
        parts.append("%d MB/s" % round(int(kv["rbps"]) / 1e6))
    return ("disk capped at " + ", ".join(parts) + " (reads and writes each)") if parts else ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--dir", help="RKC5 / RKC7 out dir with metrics.csv (+ probe.csv, report)")
    src.add_argument("--csv", help="a container_metrics.py CSV, e.g. RKC3's /tmp/rkc-metrics-<db>.csv")
    ap.add_argument("--probe", help="write_probe.py CSV (with --csv), or alone: a write log with no "
                                    "container metrics, e.g. RKC8's rkc8-writer.csv (latency + rejected writes)")
    ap.add_argument("--report", help="report / summary JSON (with --csv; default <csv>.json if it exists)")
    ap.add_argument("--cleanup", help="shade START,END instead of the report's (epoch or UTC 'YYYY-MM-DD HH:MM:SS')")
    ap.add_argument("--out", help="output HTML (default <dir>/metrics.html, or <csv> with .html)")
    ap.add_argument("--bucket", type=float, default=10, help="seconds per latency point (default 10)")
    ap.add_argument("--title", help="page title (default from the report)")
    a = ap.parse_args()
    if not (a.dir or a.csv or a.probe):
        ap.error("one of --dir, --csv or --probe is required")

    if a.dir:
        mpath, ppath = os.path.join(a.dir, "metrics.csv"), os.path.join(a.dir, "probe.csv")
        p2path = os.path.join(a.dir, "probe-cleaning.csv")
        reports = sorted(glob.glob(os.path.join(a.dir, "rkc*-report-*.json")), key=os.path.getmtime)
        rpath = reports[-1] if reports else None
        out = a.out or os.path.join(a.dir, "metrics.html")
    else:
        mpath, ppath, p2path = a.csv, a.probe, None
        base = os.path.splitext(a.csv or a.probe)[0]
        rpath = a.report or (base + ".json" if os.path.isfile(base + ".json") else None)
        out = a.out or base + ".html"
    if mpath:
        if not os.path.isfile(mpath):
            sys.exit("no metrics CSV at %s" % mpath)
        metrics = load_metrics(mpath)
        if not metrics:
            sys.exit("%s has fewer than 2 samples" % mpath)
    else:
        metrics = []
    probe = load_probe(ppath, a.bucket) if ppath else []
    probe2 = load_probe(p2path, a.bucket) if p2path else []
    if not metrics and not probe:
        sys.exit("nothing to draw: %s has no writes" % ppath)

    report, rname = {}, None
    if rpath:
        rname = os.path.basename(rpath)
        with open(rpath) as f:
            report = json.load(f)
    if a.cleanup:
        parts = [p.strip() for p in a.cleanup.split(",")]
        win = tuple(float(p) if p.replace(".", "", 1).isdigit() else utc_epoch(p) for p in parts)
        if len(win) != 2 or None in win:
            sys.exit("--cleanup wants START,END (epoch or UTC 'YYYY-MM-DD HH:MM:SS')")
    else:
        win = cleanup_window(report) if report else None

    t0 = metrics[0]["t"] if metrics else probe[0]["t"]
    if a.title:
        title = a.title
    elif rname and "-report-" in rname:
        title = "%s  %s -> %s" % (rname.split("-report-")[0].upper(), report.get("from", "?"), report.get("to", "?"))
    elif report.get("db"):
        title = "RKC3  %s on %s" % (report["db"], report.get("node", "?"))
    else:
        title = os.path.basename(a.dir.rstrip("/") if a.dir else base)
    data = {
        "title": title,
        "t0": t0,
        "metrics": metrics,
        "probe": [p for p in probe if p["t"] >= t0 - a.bucket],
        "probe2": [p for p in probe2 if p["t"] >= t0 - a.bucket],
        "probe_db": report.get("bystander_db") or "the bystander database",
        "probe2_db": ((report.get("cleaning_db_writes") or {}).get("db")) or "a database being cleaned",
        "limits": " · ".join(x for x in (
            ("memory capped at " + report["container_memory"]) if report.get("container_memory") else "",
            disk_cap_text(report.get("disk_cap"))) if x),
        "cleanup": list(win) if win else None,
        "windows": report.get("container") or report.get("windows") or {},
        "bucket": a.bucket,
        "source": ", ".join(os.path.basename(p) for p in (rpath, mpath, ppath if probe else None,
                                                          p2path if probe2 else None) if p),
    }
    with open(os.path.expanduser(out), "w") as f:
        f.write(HTML.replace("__TITLE__", title).replace("__DATA__", json.dumps(data, separators=(",", ":"))))
    print(out)


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{color-scheme:light;--surface:#fcfcfb;--text:#0b0b0b;--text2:#52514e;--muted:#8a8984;
  --grid:#e7e6e2;--band:rgba(42,120,214,.08);--s1:#2a78d6;--s2:#eb6834;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--surface:#1a1a19;
  --text:#f0efec;--text2:#c3c2b7;--muted:#8f8e86;--grid:#2e2e2c;--band:rgba(57,135,229,.14);--s1:#3987e5;--s2:#d95926;}}
:root[data-theme="dark"]{color-scheme:dark;--surface:#1a1a19;--text:#f0efec;--text2:#c3c2b7;--muted:#8f8e86;
  --grid:#2e2e2c;--band:rgba(57,135,229,.14);--s1:#3987e5;--s2:#d95926;}
body{margin:0;background:var(--surface);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--text2);margin:0 0 18px}
.chart{margin:0 0 18px;position:relative}
.chart h2{font-size:13px;font-weight:600;margin:0 0 2px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.key{display:inline-flex;align-items:center;gap:5px;font-weight:400;color:var(--text2)}
.key i{width:14px;height:2px;border-radius:1px;display:inline-block}
svg{display:block;width:100%;height:auto;overflow:visible}
svg text{fill:var(--muted);font-size:11px}
.tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--grid);
  border-radius:6px;padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);display:none;white-space:nowrap;z-index:2}
.tip b{font-weight:600}
table{border-collapse:collapse;margin-top:8px;font-variant-numeric:tabular-nums;width:100%;max-width:760px}
th,td{padding:5px 10px;border-bottom:1px solid var(--grid);text-align:right}
th:first-child,td:first-child{text-align:left}
th{color:var(--text2);font-weight:600}
.wrap{overflow-x:auto}
.note{color:var(--muted);font-size:12px}
</style></head>
<body><main>
<h1 id="title"></h1>
<p class="sub" id="sub"></p>
<div id="charts"></div>
<h2 style="font-size:15px;margin:26px 0 0">Window averages from the report</h2>
<div class="wrap"><table id="tbl"></table></div>
<p class="note" id="note"></p>
</main>
<script>
const D = __DATA__;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const t0 = D.t0, min = t => (t - t0) / 60;
const clock = t => new Date(t * 1000).toISOString().slice(11, 19) + " UTC";
document.getElementById("title").textContent = D.title;
const cw = D.cleanup;
document.getElementById("sub").textContent = cw
  ? `Cleanup ${clock(cw[0])} – ${clock(cw[1])} (${((cw[1]-cw[0])/60).toFixed(1)} min, shaded). Time axis: minutes since ${clock(t0)}.`
  : `Time axis: minutes since ${clock(t0)}. No report found, so the cleanup is not shaded.`;
if (D.limits) document.getElementById("sub").textContent += ` Limits: ${D.limits}.`;

const M = D.metrics, P = D.probe;
const charts = !M.length ? [] : [
  {title: "CPU (%, 100 = one core)", rows: M, series: [{k: "cpu", name: "CPU", c: "--s1"}], unit: "%"},
  M[0].anon_gb != null
    ? {title: "Memory (GB)", rows: M, series: [{k: "mem_gb", name: "With page cache", c: "--s1"},
                                              {k: "anon_gb", name: "Server process only", c: "--s2"}], unit: " GB"}
    : {title: "Memory (GB, includes page cache)", rows: M, series: [{k: "mem_gb", name: "Memory", c: "--s1"}], unit: " GB"},
  {title: "Disk I/O (MB/s)", rows: M, series: [{k: "write", name: "Write", c: "--s1"}, {k: "read", name: "Read", c: "--s2"}], unit: " MB/s"},
];
if (M.length && M[0].wiops != null) charts.push({title: "Disk operations (IOPS)", rows: M,
  series: [{k: "wiops", name: "Write", c: "--s1"}, {k: "riops", name: "Read", c: "--s2"}], unit: " IOPS"});
if (P.length) charts.push({title: (D.probe2 || []).length ? `Write latency to ${D.probe_db} -- not being cleaned (ms, per ${D.bucket} s)` : `Write latency (ms, per ${D.bucket} s)`, rows: P,
  series: [{k: "p99", name: "p99", c: "--s1"}, {k: "p50", name: "p50", c: "--s2"}], unit: " ms"});
if (P.length && !M.length) charts.push({title: `Writes not accepted (per ${D.bucket} s)`, rows: P,
  series: [{k: "errors", name: "Not accepted", c: "--s2"}], unit: ""});
const P2 = D.probe2 || [];
if (P2.length) charts.push({title: `Write latency to ${D.probe2_db} -- WHILE it is being cleaned (ms, per ${D.bucket} s)`, rows: P2,
  series: [{k: "p99", name: "p99", c: "--s1"}, {k: "p50", name: "p50", c: "--s2"}], unit: " ms"});

const xMax = Math.max(M.length ? min(M[M.length-1].t) : 0, P.length ? min(P[P.length-1].t) : 0, P2.length ? min(P2[P2.length-1].t) : 0);
const W = 1000, H = 190, L = 52, R = 14, T = 10, B = 24;
const x = v => L + (v / xMax) * (W - L - R);
function nice(m){ if (m <= 0) return 1; const p = 10 ** Math.floor(Math.log10(m)); const f = m / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p; }
const NS = "http://www.w3.org/2000/svg";
const el = (n, a, parent) => { const e = document.createElementNS(NS, n);
  for (const k in a) e.setAttribute(k, a[k]); parent && parent.appendChild(e); return e; };
const views = [];

function draw(){
  const host = document.getElementById("charts"); host.innerHTML = ""; views.length = 0;
  for (const c of charts){
    const box = document.createElement("div"); box.className = "chart";
    const h = document.createElement("h2"); h.textContent = c.title;
    if (c.series.length > 1) for (const s of c.series){
      const k = document.createElement("span"); k.className = "key";
      k.innerHTML = `<i style="background:${css(s.c)}"></i>${s.name}`; h.appendChild(k); }
    box.appendChild(h);
    const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": c.title}, box);
    const ymax = nice(Math.max(...c.rows.flatMap(r => c.series.map(s => r[s.k] ?? 0))) * 1.05);
    const y = v => T + (1 - v / ymax) * (H - T - B);
    if (cw) el("rect", {x: x(min(cw[0])), y: T, width: Math.max(1, x(min(cw[1])) - x(min(cw[0]))),
                        height: H - T - B, fill: css("--band")}, svg);
    for (let i = 0; i <= 4; i++){ const v = ymax * i / 4;
      el("line", {x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: css("--grid"), "stroke-width": 1}, svg);
      el("text", {x: L - 6, y: y(v) + 4, "text-anchor": "end"}, svg).textContent = +v.toFixed(2); }
    const step = nice(xMax / 8);
    for (let v = 0; v <= xMax + 1e-9; v += step)
      el("text", {x: x(v), y: H - 6, "text-anchor": "middle"}, svg).textContent = +v.toFixed(1) + (v ? "" : " min");
    if (cw) el("text", {x: x(min(cw[0])) + 4, y: T + 12, style: "fill:" + css("--text2")}, svg).textContent = "cleanup";
    for (const s of [...c.series].reverse()){
      let d = "", pen = false;
      for (const r of c.rows){ const v = r[s.k];
        if (v == null){ pen = false; continue; }
        d += (pen ? "L" : "M") + x(min(r.t)).toFixed(1) + "," + y(v).toFixed(1); pen = true; }
      el("path", {d, fill: "none", stroke: css(s.c), "stroke-width": 2, "stroke-linejoin": "round"}, svg);
    }
    const cross = el("line", {y1: T, y2: H - B, stroke: css("--muted"), "stroke-width": 1, visibility: "hidden"}, svg);
    const tip = document.createElement("div"); tip.className = "tip"; box.appendChild(tip);
    host.appendChild(box);
    views.push({c, svg, cross, tip});
    svg.addEventListener("mousemove", ev => hover(ev, svg));
    svg.addEventListener("mouseleave", () => views.forEach(v => { v.cross.setAttribute("visibility", "hidden"); v.tip.style.display = "none"; }));
  }
}
function nearest(rows, t){ let lo = 0, hi = rows.length - 1;
  while (lo < hi){ const m = (lo + hi) >> 1; if (rows[m].t < t) lo = m + 1; else hi = m; }
  return lo > 0 && Math.abs(rows[lo-1].t - t) < Math.abs(rows[lo].t - t) ? rows[lo-1] : rows[lo]; }
function hover(ev, svg){
  const b = svg.getBoundingClientRect(), sx = (ev.clientX - b.left) / b.width * W;
  const m = Math.min(Math.max((sx - L) / (W - L - R) * xMax, 0), xMax), t = t0 + m * 60;
  for (const v of views){
    const r = nearest(v.c.rows, t), px = x(min(r.t));
    v.cross.setAttribute("x1", px); v.cross.setAttribute("x2", px); v.cross.setAttribute("visibility", "visible");
    v.tip.innerHTML = `<b>${clock(r.t)}</b> · ${min(r.t).toFixed(1)} min<br>` +
      v.c.series.map(s => `${s.name}: <b>${r[s.k] ?? "–"}</b>${r[s.k] != null ? s.k === "cpu" ? "%" : v.c.unit : ""}`).join("<br>") +
      (r.errors ? `<br>errors: <b>${r.errors}</b>` : "");
    v.tip.style.display = "block";
    const bw = v.svg.getBoundingClientRect().width, left = px / W * bw;
    v.tip.style.left = (left > bw - 180 ? left - v.tip.offsetWidth - 10 : left + 10) + "px";
    v.tip.style.top = "26px";
  }
}
function table(){
  const w = D.windows, names = Object.keys(w);
  const tbl = document.getElementById("tbl");
  if (!names.length){ tbl.outerHTML = "<p class='note'>No window summary in the report.</p>"; return; }
  const cols = [["secs","Duration (s)"],["cpu_pct_avg","CPU avg %"],["cpu_pct_peak","CPU peak %"],["mem_mb_peak","Mem peak MB"],
    ...(names.some(n => w[n].mem_anon_mb_peak != null) ? [["mem_anon_mb_peak","Process mem peak MB"]] : []),
    ["read_mb_s_avg","Read MB/s"],["write_mb_s_avg","Write MB/s"],["write_mb_total","Written MB"],["iops_avg","IOPS avg"]];
  tbl.innerHTML = "<tr><th>Window</th>" + cols.map(c => `<th>${c[1]}</th>`).join("") + "</tr>" +
    names.map(n => `<tr><td>${n}</td>` + cols.map(c => `<td>${w[n][c[0]] ?? "–"}</td>`).join("") + "</tr>").join("");
}
document.getElementById("note").textContent = "Source: " + D.source + ".";
draw(); table();
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
</script></body></html>
"""

if __name__ == "__main__":
    main()
