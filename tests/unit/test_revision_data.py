"""
Tests for module_utils/revision_data.py and library/ravendb_revisions_fingerprint.py.

The loader and the verifier both derive data from revision_data, so these pin down:
  * the write plan (where attachments land, how many revisions a doc ends up with),
  * attachment content/hash (hash must match RavenDB's base64 BLAKE2b-256),
  * the fingerprint (sensitive to every field we promise to protect, blind to storage details),
  * the verifier's attachment read-back (catches wrong content and wrong hash).
Unit tests: ravendb_revisions_fingerprint.request is monkeypatched, no RavenDB needed.
"""

import json

import revision_data as rd
import ravendb_revisions_fingerprint as fpmod


# ----- write plan / expected counts ------------------------------------------------------------

def test_plan_without_attachments_is_one_run():
    assert rd.write_plan(10) == [("puts", 1, 10)]
    assert rd.write_plan(10, att_every=3, with_attachments=False) == [("puts", 1, 10)]


def test_plan_places_attachment_after_every_nth_revision():
    assert rd.write_plan(7, att_every=3, with_attachments=True) == [
        ("puts", 1, 3), ("att", 3), ("puts", 4, 6), ("att", 6), ("puts", 7, 7)]


def test_plan_ending_on_an_attachment_has_no_empty_tail():
    assert rd.write_plan(6, att_every=3, with_attachments=True) == [
        ("puts", 1, 3), ("att", 3), ("puts", 4, 6), ("att", 6)]


def test_plan_covers_every_revision_exactly_once():
    plan = rd.write_plan(1000, att_every=7, with_attachments=True)
    written = [v for s in plan if s[0] == "puts" for v in range(s[1], s[2] + 1)]
    assert written == list(range(1, 1001))


def test_expected_revisions_counts_attachment_revisions_and_keep():
    assert rd.expected_revisions(200) == 200
    assert rd.expected_revisions(200, 20, True) == 210
    assert rd.expected_revisions(200, 20, False) == 200
    assert rd.expected_revisions(5000, 500, True, keep=3) == 3


# ----- grouping docs into requests ---------------------------------------------------------------

def test_group_size_shares_requests_only_for_small_docs():
    assert rd.group_size(4, 1000) == 250
    assert rd.group_size(200, 1000) == 5
    assert rd.group_size(3_000_000, 1000) == 1
    assert rd.group_size(1000, 1000) == 1


def test_doc_groups_cover_every_doc_once_and_never_mix_attachment_docs():
    groups = rd.doc_groups(docs=1003, att_docs=7, size=250)
    covered = [n for lo, hi in groups for n in range(lo, hi)]
    assert covered == list(range(1003))
    assert all(hi - lo <= 250 for lo, hi in groups)
    assert all(hi <= 7 or lo >= 7 for lo, hi in groups)  # no group straddles the split
    assert groups[0] == (0, 7)


def test_doc_groups_edge_cases():
    assert rd.doc_groups(5, 0, 2) == [(0, 2), (2, 4), (4, 5)]
    assert rd.doc_groups(5, 5, 10) == [(0, 5)]
    assert rd.doc_groups(5, 99, 10) == [(0, 5)]


# ----- attachment content ------------------------------------------------------------------------

def test_attachment_bytes_exact_size_and_distinct():
    a = rd.attachment_bytes("att/1", "att-20", 4096)
    assert len(a) == 4096
    assert a != rd.attachment_bytes("att/1", "att-40", 4096)
    assert a != rd.attachment_bytes("att/2", "att-20", 4096)
    assert a == rd.attachment_bytes("att/1", "att-20", 4096)


def test_attachment_hash_matches_ravendb():
    # Hash RavenDB reported for an 8-byte attachment "blob-one" (probed on a live server).
    assert rd.attachment_hash(b"blob-one") == "7R7N+eW6uNW1B/YGEcnKqm5flq4RszJgPGc9R788iNk="


# ----- fingerprint -------------------------------------------------------------------------------

def rev(v, atts=(), cv="A:1-x", etag=1):
    md = {"@change-vector": cv, "@etag": etag}
    if atts:
        md["@attachments"] = [{"Name": n, "Hash": h, "Size": s, "ContentType": ""} for n, h, s in atts]
    return {"v": v, "@metadata": md}


def test_fingerprint_ignores_change_vector_and_etag():
    a = rd.fingerprint([rev(2, cv="A:2-x", etag=2), rev(1)])
    b = rd.fingerprint([rev(2, cv="B:9-y", etag=77), rev(1, cv="B:8-y", etag=76)])
    assert a == b


def test_fingerprint_ignores_attachment_list_order():
    a = rd.fingerprint([rev(1, [("x", "h1", 1), ("y", "h2", 2)])])
    b = rd.fingerprint([rev(1, [("y", "h2", 2), ("x", "h1", 1)])])
    assert a == b


def test_fingerprint_detects_every_protected_change():
    base = [rev(3, [("att-2", "h", 10)]), rev(2, [("att-2", "h", 10)]), rev(1)]
    fp = rd.fingerprint(base)
    assert rd.fingerprint(base[:-1]) != fp                                          # lost revision
    assert rd.fingerprint(base + [rev(1)]) != fp                                    # duplicated
    assert rd.fingerprint([base[1], base[0], base[2]]) != fp                        # reordered
    assert rd.fingerprint([rev(9, [("att-2", "h", 10)])] + base[1:]) != fp          # body changed
    assert rd.fingerprint([rev(3)] + base[1:]) != fp                                # attachment dropped
    assert rd.fingerprint([rev(3, [("att-2", "OTHER", 10)])] + base[1:]) != fp      # hash changed
    assert rd.fingerprint([rev(3, [("att-2", "h", 11)])] + base[1:]) != fp          # size changed


# ----- verifier: attachment read-back --------------------------------------------------------------

P = {"db_name": "db", "target": "1a", "ravendb_domain": "d", "client_cert": "c", "ca_cert": "a",
     "page_size": 2, "verify_attachment_content": True}


def fake_server(monkeypatch, revisions, content):
    """revisions: newest first.  content: {(doc_id, name, cv): bytes}."""
    calls = []

    def request(method, target, domain, path, cert, ca, body=None, timeout=30, **kw):
        calls.append((method, path, body))
        if method == "GET":
            start = int(path.split("start=")[1].split("&")[0])
            return 200, json.dumps({"Results": revisions[start:start + P["page_size"]]}).encode()
        doc_id = path.split("id=")[1].split("&")[0].replace("%2F", "/")
        name = path.split("name=")[1]
        data = content.get((doc_id, name, body["ChangeVector"]))
        return (200, data) if data is not None else (404, b"")

    monkeypatch.setattr(fpmod, "request", request)
    return calls


def good_doc(doc_id="att/0"):
    data = rd.attachment_bytes(doc_id, "att-2", 10)
    h = rd.attachment_hash(data)
    revisions = [rev(3, [("att-2", h, 10)], cv="cv3"), rev(2, [("att-2", h, 10)], cv="cv2"),
                 rev(2, cv="cv2a"), rev(1, cv="cv1")]
    return revisions, {(doc_id, "att-2", "cv2"): data}


def test_verifier_pages_and_reads_attachment_from_oldest_revision(monkeypatch):
    revisions, content = good_doc()
    calls = fake_server(monkeypatch, revisions, content)
    doc_id, fp, checked, failures = fpmod.one_doc(P, "att/0")
    assert fp == rd.fingerprint(revisions)
    assert fp["count"] == 4
    assert (checked, failures) == (1, [])
    gets = [c for c in calls if c[0] == "GET"]
    assert len(gets) == 3  # pages of 2: [2, 2, 0]
    posts = [c for c in calls if c[0] == "POST"]
    assert posts[0][2] == {"Type": "Revision", "ChangeVector": "cv2"}


def test_verifier_reports_content_that_does_not_match_the_hash(monkeypatch):
    revisions, content = good_doc()
    content[("att/0", "att-2", "cv2")] = b"x" * 10
    fake_server(monkeypatch, revisions, content)
    _, _, checked, failures = fpmod.one_doc(P, "att/0")
    assert checked == 1 and len(failures) == 1 and "metadata hash" in failures[0]


# --- check_revision_attachment: the one attachment read-back check both verifier modules use ---

_P = {"db_name": "db 1", "target": "1a", "ravendb_domain": "d", "client_cert": "c", "ca_cert": "a"}


def _reader(status, data, calls):
    def request(method, target, domain, path, cert, ca, body=None, timeout=None):
        calls.append((method, path, body, timeout))
        return status, data
    return request


def test_check_revision_attachment_ok_and_request_shape():
    data = rd.attachment_bytes("big/0", "att-1", 40)
    calls = []
    assert rd.check_revision_attachment(_reader(200, data, calls), _P, "big/0", "att-1",
                                        rd.attachment_hash(data), 40, "cv1", timeout=77) is None
    method, path, body, timeout = calls[0]
    assert method == "POST" and timeout == 77
    assert path == "/databases/db%201/attachments?id=big/0&name=att-1"
    assert body == {"Type": "Revision", "ChangeVector": "cv1"}


def test_check_revision_attachment_reports_each_kind_of_failure():
    data = rd.attachment_bytes("big/0", "att-1", 40)
    good = rd.attachment_hash(data)
    err = rd.check_revision_attachment(_reader(404, b"", []), _P, "big/0", "att-1", good, 40, "cv1", 5)
    assert "HTTP 404" in err and "cv1" in err
    err = rd.check_revision_attachment(_reader(200, data, []), _P, "big/0", "att-1", "other", 40, "cv1", 5)
    assert "content hash" in err
    wrong = b"x" * 40
    err = rd.check_revision_attachment(_reader(200, wrong, []), _P, "big/0", "att-1",
                                       rd.attachment_hash(wrong), 40, "cv1", 5)
    assert "differs from what the loader wrote" in err
