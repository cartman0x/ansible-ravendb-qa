"""
Tests for scripts/container_metrics.py and scripts/write_probe.py -- the RKC5 measurements.

Hardening focus:  cgroup files parse (several io.stat devices summed), rates come from the
                  cumulative counters (a missed sample loses no data), windows only use their own
                  samples, a cut-off CSV row is skipped, percentiles are nearest-rank and errors
                  never enter the latency numbers.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import container_metrics as cm  # noqa: E402
import write_probe as wp  # noqa: E402


def test_parse_cpu_and_io_stat():
    assert cm.parse_cpu_stat("usage_usec 112960866\nuser_usec 94540117\n") == 112960866
    io = cm.parse_io_stat("8:48 rbytes=100 wbytes=200 rios=1 wios=2 dbytes=0 dios=0\n"
                          "8:32 rbytes=10 wbytes=20 rios=3 wios=4\n")
    assert io == {"rbytes": 110, "wbytes": 220, "rios": 4, "wios": 6}
    assert cm.parse_io_stat("") == {"rbytes": 0, "wbytes": 0, "rios": 0, "wios": 0}
    with pytest.raises(ValueError):
        cm.parse_cpu_stat("user_usec 1\n")


def rows(spec):
    """spec: [(ts, cpu_seconds, mem_mb, read_mb, write_mb)] cumulative."""
    return [{"ts": t, "cpu_usec": c * 1e6, "mem_bytes": m * 2 ** 20, "rbytes": r * 1e6,
             "wbytes": w * 1e6, "rios": r, "wios": w} for t, c, m, r, w in spec]


def test_summarize_window_rates_and_peaks():
    data = rows([(0, 0, 100, 0, 0), (2, 1, 200, 10, 0), (4, 5, 150, 10, 40), (6, 6, 120, 10, 40)])
    s = cm.summarize_window(data, 0, 6)
    assert s["cpu_pct_avg"] == 100.0           # 6 cpu-seconds in 6 s
    assert s["cpu_pct_peak"] == 200.0          # 4 cpu-seconds in 2 s
    assert s["mem_mb_peak"] == 200.0 and s["mem_mb_avg"] == 142.5
    assert s["read_mb_total"] == 10.0 and s["write_mb_total"] == 40.0
    assert s["write_mb_s_peak"] == 20.0 and s["read_mb_s_peak"] == 5.0


def test_window_uses_only_its_samples():
    data = rows([(0, 0, 100, 0, 0), (2, 10, 100, 0, 0), (4, 11, 100, 0, 0), (6, 12, 100, 0, 0)])
    assert cm.summarize_window(data, 2, 6)["cpu_pct_avg"] == 50.0
    assert "note" in cm.summarize_window(data, 5, 5.5)


def test_memory_without_page_cache():
    assert cm.parse_memory_anon("anon 5368709120\nfile 107374182400\n") == 5 * 2 ** 30
    with pytest.raises(ValueError):
        cm.parse_memory_anon("file 1\n")
    data = rows([(0, 0, 100_000, 0, 0), (2, 1, 110_000, 0, 0)])
    assert "mem_anon_mb_peak" not in cm.summarize_window(data, 0, 2)   # a CSV from before the column
    data[0]["anon_bytes"], data[1]["anon_bytes"] = 4 * 2 ** 30, 6 * 2 ** 30
    s = cm.summarize_window(data, 0, 2)
    assert s["mem_anon_mb_peak"] == 6144.0 and s["mem_anon_mb_avg"] == 5120.0 and s["mem_mb_peak"] == 110_000


def test_load_csv_skips_a_cut_off_row(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text(",".join(cm.FIELDS) + "\n1,2,3,4,5,6,7,8\n8,9,10\n")
    assert len(cm.load_csv(str(p))) == 1


def test_summarize_cli(tmp_path, capsys):
    p = tmp_path / "m.csv"
    p.write_text(",".join(cm.FIELDS) + "\n0,0,0,0,0,0,0,0\n10,5000000,0,0,0,0,0,0\n")
    cm.main(["summarize", "--csv", str(p), "--window", "w:0:10"])
    assert json.loads(capsys.readouterr().out)["w"]["cpu_pct_avg"] == 50.0


def test_percentile_nearest_rank():
    vals = list(range(1, 101))
    assert wp.percentile(vals, 50) == 50 and wp.percentile(vals, 99) == 99
    assert wp.percentile(vals, 100) == 100 and wp.percentile([7], 99) == 7
    assert wp.percentile([], 50) is None


def test_probe_window_excludes_errors_from_latency(tmp_path, capsys):
    p = tmp_path / "p.csv"
    p.write_text("ts,ms,status\n1,10,201\n2,20,201\n3,5000,0\n4,30,500\n20,999,201\n")
    wp.main(["summarize", "--csv", str(p), "--window", "w:0:10", "--window", "empty:100:200"])
    out = json.loads(capsys.readouterr().out)
    assert out["w"] == {"writes": 4, "errors": 2, "p50_ms": 10.0, "p95_ms": 20.0, "p99_ms": 20.0, "max_ms": 20.0}
    assert out["empty"]["writes"] == 0 and out["empty"]["p50_ms"] is None
