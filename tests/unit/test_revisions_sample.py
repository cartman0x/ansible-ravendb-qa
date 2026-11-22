"""
Tests for library/ravendb_revisions_sample.py -- sampled fingerprints for docs with millions of
revisions (RKC7, dataset 1).

Hardening focus:  the newest and the oldest page are always read, offsets never run past the end,
                  the count comes from TotalResults (not from the pages read), a change in a
                  sampled page changes the result, and attachments are read back through the
                  OLDEST sampled revision that references them.
"""

import json

import revision_data as rd
import ravendb_revisions_sample as smod

P = {"db_name": "db", "target": "1a", "ravendb_domain": "d", "client_cert": "c", "ca_cert": "a",
     "page_size": 2, "pages": 3, "timeout": 30, "verify_attachment_content": True}


def rev(v, atts=(), cv=None):
    md = {"@change-vector": cv or "cv%d" % v}
    if atts:
        md["@attachments"] = [{"Name": n, "Hash": h, "Size": s} for n, h, s in atts]
    return {"v": v, "@metadata": md}


def fake_server(monkeypatch, revisions, content=None):
    calls = []

    def request(method, target, domain, path, cert, ca, body=None, timeout=30, **kw):
        calls.append((method, path, body))
        if method == "GET":
            start = int(path.split("start=")[1].split("&")[0])
            return 200, json.dumps({"Results": revisions[start:start + P["page_size"]],
                                    "TotalResults": len(revisions)}).encode()
        doc_id = path.split("id=")[1].split("&")[0].replace("%2F", "/")
        name = path.split("name=")[1]
        data = (content or {}).get((doc_id, name, body["ChangeVector"]))
        return (200, data) if data is not None else (404, b"")

    monkeypatch.setattr(smod, "request", request)
    return calls


def test_page_offsets_cover_newest_and_oldest():
    assert smod.page_offsets(1000, 10, 5) == [0, 248, 495, 742, 990]
    assert smod.page_offsets(1000, 10, 1) == [0]
    assert smod.page_offsets(25, 10, 5) == [0, 10, 20]       # small doc: every page
    assert smod.page_offsets(0, 10, 5) == [0]
    for count in (11, 999, 2_500_002):
        offs = smod.page_offsets(count, 1024, 7)
        assert offs[0] == 0 and offs[-1] == max(count - 1024, 0) and offs == sorted(set(offs))


def test_count_is_exact_and_only_sampled_pages_are_read(monkeypatch):
    revisions = [rev(v) for v in range(100, 0, -1)]
    calls = fake_server(monkeypatch, revisions)
    _, s, checked, failures = smod.one_doc(dict(P, verify_attachment_content=False), "big/0")
    assert s["count"] == 100
    assert list(s["pages"]) == ["0", "49", "98"]
    assert len([c for c in calls if c[0] == "GET"]) == 3      # first page reused for offset 0
    assert s["pages"]["98"] == rd.fingerprint(revisions[98:100])
    assert (checked, failures) == (0, [])


def test_change_in_a_sampled_page_changes_the_sample(monkeypatch):
    revisions = [rev(v) for v in range(100, 0, -1)]
    fake_server(monkeypatch, revisions)
    _, before, _, _ = smod.one_doc(P, "big/0")
    revisions[99] = rev(999)                                   # the oldest revision changed
    _, after, _, _ = smod.one_doc(P, "big/0")
    assert before["count"] == after["count"] and before["pages"] != after["pages"]


def test_attachment_read_back_through_the_oldest_sampled_revision(monkeypatch):
    data = rd.attachment_bytes("big/0", "att-5", 10)
    h = rd.attachment_hash(data)
    a = [("att-5", h, 10)]
    revisions = [rev(v, a, cv="cv%d" % v) for v in range(10, 4, -1)] + [rev(v) for v in range(4, 0, -1)]
    # 10 revisions, pages of 2 at offsets 0, 4, 8: the oldest sampled one with att-5 is v=6 (offset 4..5)
    calls = fake_server(monkeypatch, revisions, {("big/0", "att-5", "cv5"): data, ("big/0", "att-5", "cv6"): data})
    _, s, checked, failures = smod.one_doc(P, "big/0")
    assert (checked, failures) == (1, [])
    assert [c[2] for c in calls if c[0] == "POST"] == [{"Type": "Revision", "ChangeVector": "cv5"}]


def test_wrong_attachment_content_is_reported(monkeypatch):
    data = rd.attachment_bytes("big/0", "att-1", 10)
    revisions = [rev(1, [("att-1", rd.attachment_hash(data), 10)], cv="cv1")]
    fake_server(monkeypatch, revisions, {("big/0", "att-1", "cv1"): b"y" * 10})
    _, _, checked, failures = smod.one_doc(P, "big/0")
    assert checked == 1 and "metadata hash" in failures[0]
