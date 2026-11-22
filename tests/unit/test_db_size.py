"""
Tests for scripts/db_size.py -- data file and journal sizes (RKC3, RKC5, RKC7).

Hardening focus:  every file lands in the right field (data file vs journals vs recyclable vs
                  temp), nominal and allocated sizes are kept apart, a missing db is None (not
                  zeros), two dbs never mix, and windows report first / last / peak.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import db_size as ds  # noqa: E402

R = ds.ROOT
OUT = "\n".join([
    "DB db_a",
    f"F {R}/db_a/Raven.voron|5368709120|10485768",
    f"F {R}/db_a/Journals/0000000000000000022.journal|268435456|524288",
    f"F {R}/db_a/Journals/recyclable-journal.0000000000000000021|268435456|8",
    f"F {R}/db_a/Temp/scratch.0000000000.buffers|335544320|0",
    "DU 6095188",
    "DB db_missing",
    "MISSING",
    "DB db_b",
    f"F {R}/db_b/Raven.voron|268435456|1024",
    "DU 600",
    "",
])


def test_parse_classifies_every_file():
    out = ds.parse(OUT)
    a = out["db_a"]
    assert a["voron_bytes"] == 5368709120 and a["voron_alloc_bytes"] == 10485768 * 512
    assert a["journal_bytes"] == 2 * 268435456
    assert (a["journal_files"], a["recyclable_files"]) == (1, 1)
    assert a["temp_bytes"] == 335544320 and a["total_alloc_bytes"] == 6095188 * 1024
    assert out["db_missing"] is None
    b = out["db_b"]
    assert b["voron_alloc_bytes"] == 1024 * 512 and b["journal_bytes"] == 0 and b["journal_files"] == 0


def test_summarize_window_first_last_peak(tmp_path, capsys):
    p = tmp_path / "s.csv"
    rows = ["ts,db," + ",".join(ds.FIELDS)]
    for ts, j in [(10, 100), (20, 900), (30, 300), (40, 50)]:
        rows.append(f"{ts},db_a,1,1,{j},1,0,0,1")
    rows.append("25,db_b,7,7,7,1,0,0,7")
    rows.append("26,db_b,7,7")                           # cut-off row
    p.write_text("\n".join(rows) + "\n")
    ds.main(["summarize", "--csv", str(p), "--window", "w:15:35"])
    out = json.loads(capsys.readouterr().out)["w"]
    assert out["db_a"]["journal_bytes"] == {"first": 900, "last": 300, "peak": 900}
    assert out["db_a"]["samples"] == 2 and out["db_b"]["samples"] == 1
