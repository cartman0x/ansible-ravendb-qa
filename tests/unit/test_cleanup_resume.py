"""
Tests for scripts/cleanup_resume.py -- did the revisions key cleanup continue after a restart?

Hardening focus:  a resumed cleanup passes (also when it resumes at exactly the same position),
                  a cleanup that starts over fails on BOTH signals (position back, LastRawEtag
                  changed), the attachment-step keys order correctly, and a restart that missed
                  the cleanup is never reported as a pass.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import cleanup_resume as cr  # noqa: E402

RS = "\x1e"
DB = "db_resume"


def prog(ts, step, cursor, end, doc, etag=500, in_progress=None):
    if in_progress is None:
        in_progress = end != 0
    return (f"2026-11-15 10:00:{ts:06.3f}0000|1|Information|{DB}|RevisionsKeyCleaner|"
            f"Revisions key cleanup progress: step {step}, cursor {cursor}, end {end}, "
            f"document in progress {in_progress}, last raw etag {etag}, document '{doc}', "
            f"rows 1234, transaction size 5678")


def done(ts):
    return f"2026-11-15 10:00:{ts:06.3f}0000|1|Information|{DB}|RevisionsKeyCleaner|Revisions key cleanup is done"


def err(ts):
    return (f"2026-11-15 10:00:{ts:06.3f}0000|1|Error|{DB}|RevisionsKeyCleaner|An error occurred in the "
            f"revisions key cleanup, last progress: 'x'; it continues on the next database load "
            f"System.ObjectDisposedException")


def test_parse_reads_every_field():
    r = cr.parse(prog(1, "Revisions", 42, 900, "rr/plain/7"))
    assert r["step"] == "Revisions" and r["cursor"] == 42 and r["end"] == 900
    assert r["in_progress"] is True and r["last_raw_etag"] == 500 and r["doc"] == "rr/plain/7"
    assert cr.parse(done(1)) is None


def test_position_orders_steps_within_a_document():
    seq = [
        prog(1, "Revisions", 10, 900, "rr/att/1"),
        prog(2, "Revisions", 20, 900, "rr/att/1"),
        prog(3, "RevisionAttachments", 0, 900, f"rr/att/1{RS}r{RS}a{RS}"),
        prog(4, "RevisionAttachments", 0, 900, f"rr/att/1{RS}r{RS}b{RS}"),
        prog(5, "RevisionAttachmentTombstones", 0, 900, "rr/att/1"),
        prog(6, "RevisionAttachmentTombstones", 0, 900, f"rr/att/1{RS}r{RS}a{RS}"),
        prog(7, "Revisions", 0, 0, "rr/att/1"),                       # finished
        prog(8, "Revisions", 5, 900, "rr/att/10"),                     # next in byte order
        prog(9, "Revisions", 5, 900, "rr/plain/0"),
        prog(10, "RevisionsTombstones", 100, 5000, ""),
        prog(11, "RevisionsTombstones", 4000, 5000, ""),
        prog(12, "Done", 0, 0, ""),
    ]
    pos = [cr.position(cr.parse(l)) for l in seq]
    assert pos == sorted(pos) and len(set(pos)) == len(pos)


def test_resumed_cleanup_passes():
    lines = [
        prog(1, "Revisions", 10, 900, "rr/att/1"),
        prog(2, "Revisions", 30, 900, "rr/plain/3"),
        # restart (graceful): new process resumes at the same document
        prog(20, "Revisions", 30, 900, "rr/plain/3"),
        prog(21, "Revisions", 0, 0, "rr/plain/3"),
        prog(22, "RevisionsTombstones", 100, 5000, ""),
        # restart (kill)
        prog(40, "RevisionsTombstones", 2200, 5000, ""),
        done(41),
    ]
    out = cr.check(lines, [(2, 2), (5, 5)])
    assert out["ok"], out["problems"]
    assert out["restarts"][0]["same_position"] is True
    assert out["restarts"][1]["same_position"] is False
    assert out["done"] is True


def test_started_over_fails_on_position_and_etag():
    lines = [
        prog(1, "Revisions", 10, 900, "rr/att/1"),
        prog(2, "Revisions", 30, 900, "rr/plain/3"),
        prog(20, "Revisions", 10, 950, "rr/att/1", etag=777),       # back to the start
        done(30),
    ]
    out = cr.check(lines, [(2, 2)])
    assert not out["ok"]
    joined = " ".join(out["problems"])
    assert "LastRawEtag changed" in joined and "position went back" in joined


def test_only_position_back_is_still_caught():
    lines = [
        prog(1, "RevisionsTombstones", 3000, 5000, ""),
        prog(20, "RevisionsTombstones", 100, 5000, ""),
        done(30),
    ]
    out = cr.check(lines, [(1, 1)])
    assert not out["ok"] and "position went back" in out["problems"][0]


def test_restart_before_any_progress_or_after_done_fails():
    lines = [prog(1, "Revisions", 10, 900, "rr/att/1"), done(2), prog(20, "Done", 0, 0, "")]
    out = cr.check(lines, [(2, 2)])
    assert any("already done" in p for p in out["problems"])
    out = cr.check([done(5)], [(0, 0)])
    assert any("no progress line before" in p for p in out["problems"])


def test_no_line_after_restart_fails():
    out = cr.check([prog(1, "Revisions", 10, 900, "rr/att/1")], [(1, 1)])
    assert any("did not continue" in p for p in out["problems"])


def test_error_lines_at_shutdown_are_allowed_only_with_the_flag():
    lines = [
        prog(1, "Revisions", 10, 900, "rr/att/1"),
        err(2),                                                      # old process shutting down
        prog(20, "Revisions", 11, 900, "rr/att/1"),
        done(21),
    ]
    assert cr.check(lines, [(1, 2)], allow_shutdown_errors=True)["ok"]
    out = cr.check(lines, [(1, 2)], allow_shutdown_errors=False)
    assert not out["ok"] and "shut down" in out["problems"][0]
    # the same error line outside a shutdown window always fails
    out = cr.check(lines, [(1, 1)], allow_shutdown_errors=True)
    assert any("error line" in p for p in out["problems"])


def test_unknown_progress_format_is_reported():
    bad = "2026-11-15 10:00:01.0000000|1|Information|db|X|Revisions key cleanup progress: something new"
    out = cr.check([bad, prog(2, "Revisions", 1, 9, "a"), prog(3, "Revisions", 2, 9, "a")], [(2, 2)])
    assert any("unknown format" in p for p in out["problems"])


def test_bad_counts_are_rejected():
    with pytest.raises(ValueError):
        cr.check([done(1)], [(0, 5)])
    with pytest.raises(ValueError):
        cr.check([done(1), done(2), done(3)], [(1, 2), (1, 3)])


def test_cli(capsys, monkeypatch):
    lines = [prog(1, "Revisions", 10, 900, "a"), prog(20, "Revisions", 11, 900, "a"), done(21)]
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("\n".join(lines) + "\n"))
    assert cr.main(["--restart", "1:1"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cr.main(["--restart", "1"]) == 2


# Real lines: db_resume (RKC6 dataset), 6.2.20 -> PR build, one uninterrupted cleanup,
# 2026-11-16.  The attachment-step lines carry \x1e inside the key -- str.splitlines() would cut
# them apart (it splits on \x1e too), which once made every one of them "unknown format".
REAL = Path(__file__).resolve().parent / "data" / "rkc6_cleanup_lines.log"


def run_cli(monkeypatch, capsys, text, *args):
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(text))
    rc = cr.main(list(args))
    return rc, json.loads(capsys.readouterr().out)


def test_real_uninterrupted_cleanup_parses_and_only_moves_forward(monkeypatch, capsys):
    text = REAL.read_text()
    assert RS in text
    rc, out = run_cli(monkeypatch, capsys, text)
    assert rc == 0, out["problems"]
    assert out["progress_lines"] == 34 and out["done"] and len(out["last_raw_etags"]) == 1


def test_timeline_of_the_real_cleanup():
    t = cr.timeline(REAL.read_text().split("\n"))
    assert t["first_progress"].startswith("2026-11-16 08:20:05") and t["done"].startswith("2026-11-16 08:20:22")
    assert t["progress_lines"] == 34
    assert t["steps"]["RevisionsTombstones"]["lines"] == 9
    assert t["documents"]["rr/plain/10"]["lines"] == 1
    assert all(RS not in d for d in t["documents"])       # attachment keys grouped under their doc
    assert sum(d["lines"] for d in t["documents"].values()) == 24
