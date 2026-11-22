"""scripts/plot_metrics.py: IOPS from the counters, RKC8's per-node cleanup window, and the
write-log-only mode (RKC8 has no container metrics)."""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import plot_metrics  # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts", "plot_metrics.py")


def page_data(path):
    html = open(path).read()
    return json.loads(re.search(r"const D = (\{.*?\});\n", html, re.S).group(1))


def test_load_metrics_has_iops(tmp_path):
    p = tmp_path / "metrics.csv"
    p.write_text("ts,cpu_usec,mem_bytes,rbytes,wbytes,rios,wios,anon_bytes\n"
                 "100,0,1073741824,0,0,0,0,0\n"
                 "110,1000000,1073741824,0,0,500,15000,0\n")
    (r,) = plot_metrics.load_metrics(str(p))
    assert (r["riops"], r["wiops"]) == (50, 1500)


def test_cleanup_window_rkc8_per_node():
    report = {"cleanup": {"1a": {"first": 100, "done": 300, "errors": 0},
                          "1b": {"first": 150, "done": 400, "errors": 0}}}
    assert plot_metrics.cleanup_window(report) == (100.0, 400.0)


def test_cleanup_window_rkc7_unchanged():
    report = {"cleanup": {"first_progress": "2026-11-17 10:00:00", "done": "2026-11-17 11:00:00"}}
    s, e = plot_metrics.cleanup_window(report)
    assert e - s == 3600


def test_probe_only_page(tmp_path):
    w = tmp_path / "rkc8-writer.csv"
    w.write_text("ts,ms,node,doc,v,status,outcome\n"
                 "1000.0,10,1a,d/1,1,201,ok\n"
                 "1001.0,20,1b,d/2,1,503,refused\n"
                 "1015.0,30,1c,d/3,1,201,ok\n")
    rep = tmp_path / "rkc8-report.json"
    rep.write_text(json.dumps({"cleanup": {"1a": {"first": 1000, "done": 1010}}}))
    out = tmp_path / "g.html"
    subprocess.run([sys.executable, SCRIPT, "--probe", str(w), "--report", str(rep), "--out", str(out),
                    "--title", "RKC8"], check=True, capture_output=True)
    d = page_data(out)
    assert d["metrics"] == [] and d["cleanup"] == [1000.0, 1010.0]
    assert sum(p["errors"] for p in d["probe"]) == 1


def test_requires_a_source():
    r = subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True)
    assert r.returncode != 0 and "--probe" in r.stderr
