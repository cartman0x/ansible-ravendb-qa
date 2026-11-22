"""
Tests for scripts/cleanup_eta.py -- progress / ETA / early stop of the revisions key cleanup.

Hardening focus:  a document's position is exact (checked against brute-force sorting of the
                  ids, the order the cleanup walks them), the per-document cursor/end is NOT
                  read as overall progress, and the verdict is TOO_SLOW only on real evidence.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import cleanup_eta as eta  # noqa: E402


def brute_rank(s, n):
    return sorted(str(i) for i in range(n)).index(s) if s in {str(i) for i in range(n)} else \
        sum(1 for i in range(n) if str(i) < s)


@pytest.mark.parametrize("n", [1, 2, 9, 10, 11, 99, 100, 101, 1000, 1234, 5000])
def test_numeric_rank_matches_sorting(n):
    for i in list(range(min(n, 300))) + [n - 1, n // 2, n // 3]:
        assert eta.numeric_rank(str(i), n) == brute_rank(str(i), n), (i, n)


def test_numeric_rank_for_ids_outside_the_range():
    assert eta.numeric_rank("99999", 1234) == brute_rank("99999", 1234)
    assert eta.numeric_rank("5", 3) == 3
    assert eta.numeric_rank("", 10) == 0


def test_doc_rank_walks_shapes_in_byte_order_of_their_prefix():
    universe = [{"prefix": "lg/plain", "docs": 100}, {"prefix": "lg/att", "docs": 50},
                {"prefix": "lg/pruned", "docs": 20}]
    ids = [f"{s['prefix']}/{i}" for s in universe for i in range(s["docs"])]
    order = sorted(ids)
    for k in (0, 1, 49, 50, 77, 149, 150, 169):
        assert eta.doc_rank(order[k], universe) == k, order[k]
    assert eta.doc_rank("LG/ATT/7", universe) == order.index("lg/att/7")   # ids are lower-cased


def test_the_real_laptop_position():
    """2026-11-14 run: 'lg/att/2056205' looked like 41% from cursor/end -- it was ~2%."""
    universe = [{"prefix": "lg/plain", "docs": 50_000_000}, {"prefix": "lg/pruned", "docs": 5_000_000},
                {"prefix": "lg/att", "docs": 5_000_000}]
    assert eta.doc_rank("lg/att/2056205", universe) == 1_173_566


def line(t, doc, step="Revisions", cursor=499_000_000, end=535_000_000):
    return (f"{t:%Y-%m-%d %H:%M:%S}.0486|A|INFO|33|db_large||Raven.Server.Documents.Revisions."
            f"RevisionsKeyCleaner|Revisions key cleanup progress: step {step}, cursor {cursor}, "
            f"end {end}, document in progress True, last raw etag {end}, document '{doc}', rows 149, "
            f"transaction size 1.82 MBytes|")


T0 = datetime(2026, 11, 14, 6, 40)
UNIVERSE = [{"prefix": "d", "docs": 1000}]   # ids d/0..d/999; byte order: d/0, d/1, d/10, d/100, ...
ORDER = sorted(f"d/{i}" for i in range(1000))


def run(points, max_hours=10, **kw):
    return eta.estimate(eta.parse(points), UNIVERSE, max_hours, **kw)


def test_no_lines_waits():
    assert run([])[0] == "WAIT"


def test_too_early_for_a_verdict():
    pts = [line(T0 + timedelta(minutes=m), ORDER[m]) for m in range(10)]
    verdict, msg = run(pts)
    assert verdict == "WAIT" and "too early" in msg


def test_fast_enough_is_ok():
    # 10 docs per minute -> 1000 docs in 100 min, budget 10 h
    pts = [line(T0 + timedelta(minutes=m), ORDER[m * 10]) for m in range(61)]
    verdict, msg = run(pts)
    assert verdict == "OK", msg
    assert "within the 10 h budget" in msg


def test_too_slow_is_reported_with_the_numbers():
    # 1 doc per minute -> 1000 docs take ~16.7 h, budget 10 h
    pts = [line(T0 + timedelta(minutes=m), ORDER[m]) for m in range(61)]
    verdict, msg = run(pts)
    assert verdict == "TOO_SLOW", msg
    assert "60 / 1,000" in msg and "OVER the 10 h budget" in msg


def test_rate_uses_the_recent_window_not_the_whole_run():
    # fast first hour (10 docs/min), then 1 doc/min: the slowdown must count
    pts = [line(T0 + timedelta(minutes=m), ORDER[m * 10]) for m in range(60)]
    pts += [line(T0 + timedelta(minutes=60 + m), ORDER[600 + m]) for m in range(61)]
    # last 30 min: 60 docs/h -> 340 left = 5.7 h more, 7.7 h in total; whole run: ~3 h in total
    assert run(pts, max_hours=5)[0] == "TOO_SLOW"
    assert run(pts, max_hours=5, window_min=120)[0] == "OK"


def test_no_budget_only_reports():
    pts = [line(T0 + timedelta(minutes=m), ORDER[m]) for m in range(61)]
    assert run(pts, max_hours=0)[0] == "OK"


def test_cursor_and_end_are_not_overall_progress_in_the_document_steps():
    # cursor/end says 99%, but the document is only the 5th of 1000
    pts = [line(T0 + timedelta(minutes=m), ORDER[m // 15], cursor=990, end=1000) for m in range(61)]
    verdict, msg = run(pts)
    assert "(0.4%)" in msg and verdict == "TOO_SLOW"


def test_tombstones_step_uses_cursor_over_end():
    pts = [line(T0 + timedelta(minutes=m), ORDER[999], step="RevisionsTombstones",
                cursor=1000 + m * 100, end=11_000) for m in range(61)]
    verdict, msg = run(pts)
    assert "tombstones step 60.0%" in msg and verdict == "OK"


def test_other_lines_and_other_steps_are_ignored():
    pts = ["2026-11-14 06:40:00.0000|A|INFO|Revisions key cleanup is done", "junk"]
    assert eta.parse(pts) == []
