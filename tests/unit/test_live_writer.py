"""Tests for scripts/live_writer.py -- which documents are written where, how a failed write is
classified (applied / not applied / unknown), and the summary the RKC8 checks are built on."""

import errno
import socket
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import live_writer as lw  # noqa: E402


def test_doc_plan_spreads_documents_over_nodes():
    plan = lw.doc_plan("p", 1000, 100, ["1a", "1b", "1c"])
    assert [d for d, _ in plan] == ["p/%d" % n for n in range(0, 1000, 100)]
    assert [n for _, n in plan[:4]] == ["1a", "1b", "1c", "1a"]
    assert len({d for d, _ in plan}) == len(plan)          # one home node per doc


def test_classify_separates_not_applied_from_unknown():
    assert lw.classify(HTTPError("u", 503, "x", {}, None)) == (503, "http_err")
    assert lw.classify(URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))) == (0, "refused")
    assert lw.classify(URLError(socket.gaierror("no name"))) == (0, "refused")
    assert lw.classify(ConnectionRefusedError()) == (0, "refused")
    assert lw.classify(URLError(OSError(errno.EHOSTUNREACH, "unreachable"))) == (0, "refused")
    # the request may have been applied: no answer
    assert lw.classify(socket.timeout()) == (0, "uncertain")
    assert lw.classify(ConnectionResetError()) == (0, "uncertain")
    assert lw.classify(URLError(TimeoutError())) == (0, "uncertain")


def test_summarize_counts_applied_writes_per_doc_and_window(tmp_path):
    csv = tmp_path / "w.csv"
    csv.write_text(",".join(lw.FIELDS) + "\n"
                   "10.0,5,1a,p/0,1000000,201,ok\n"
                   "11.0,5,1a,p/0,1000001,201,ok\n"
                   "12.0,5,1b,p/100,1000000,0,refused\n"
                   "13.0,5,1b,p/100,1000000,503,http_err\n"
                   "14.0,5,1c,p/200,1000000,0,uncertain\n"
                   "15.0,5,1a,p/0,10000")                           # cut off mid-write
    s = lw.summarize_rows(lw.load_csv(str(csv)), [("w", 10.5, 13.5)])
    assert s["docs"]["p/0"] == {"node": "1a", "ok": 2, "uncertain": False}
    assert s["docs"]["p/100"]["ok"] == 0
    assert s["docs"]["p/200"]["uncertain"] is True
    assert s["totals"] == {"ok": 2, "refused": 1, "http_err": 1, "uncertain": 1}
    assert s["http_statuses"] == {"503": 1}
    assert s["windows"]["w"] == {"1a": {"ok": 1}, "1b": {"refused": 1, "http_err": 1}}
