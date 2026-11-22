#!/usr/bin/python
"""
ravendb_revisions_sample -- per-doc revision fingerprints for docs with MILLIONS of revisions.

library/ravendb_revisions_fingerprint.py reads every revision page by page, but
GET /revisions?start=N skips N entries one by one on the server (SeekBackwardFrom(.., skip)), so
paging through a doc with millions of revisions is quadratic.  This module instead reads, per doc:
  * the exact count (TotalResults of the first page);
  * `pages` pages of page_size revisions spread over the doc: the newest page, the oldest page,
    and evenly spaced pages between (offsets counted from the newest, server order);
and returns {count, pages: {offset: digest}} -- digest as in module_utils/revision_data.py (body
version `v` and every attachment's (name, hash, size), in server order).  Compare two runs.

verify_attachment_content=true: every distinct (doc, attachment, hash) seen in the sampled pages
is read back through the OLDEST sampled revision that references it, and checked against the
hash in its metadata and the deterministic content scripts/revisions_loader.py wrote.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.ravendb_client import request
from ansible.module_utils.revision_data import check_revision_attachment, fingerprint


def get_page(p, doc_id, start):
    path = "/databases/%s/revisions?id=%s&start=%d&pageSize=%d" % (
        p["db_name"], quote(doc_id), start, p["page_size"])
    status, body = request("GET", p["target"], p["ravendb_domain"], path,
                           p["client_cert"], p["ca_cert"], timeout=p["timeout"])
    if status != 200:
        raise RuntimeError("%s: GET revisions start=%d -> HTTP %d" % (doc_id, start, status))
    data = json.loads(body)
    return data.get("Results") or [], int(data.get("TotalResults") or 0)


def page_offsets(count, page_size, pages):
    """Newest, oldest and evenly spaced pages between; offsets from the newest revision."""
    if count <= page_size * max(pages, 1):
        return list(range(0, count, page_size)) or [0]
    last = count - page_size
    if pages <= 1:
        return [0]
    return sorted({round(i * last / (pages - 1)) for i in range(pages)})



def one_doc(p, doc_id):
    first, count = get_page(p, doc_id, 0)
    pages = {}
    refs = {}   # (name, hash, size) -> change vector of the oldest sampled revision referencing it
    for off in page_offsets(count, p["page_size"], p["pages"]):
        revs = first if off == 0 else get_page(p, doc_id, off)[0]
        pages[str(off)] = fingerprint(revs)
        for rev in revs:   # newest first: the last one seen is the oldest
            md = rev.get("@metadata") or {}
            for a in md.get("@attachments") or []:
                key = (a.get("Name"), a.get("Hash"), a.get("Size"))
                if key not in refs or off >= refs[key][0]:
                    refs[key] = (off, md.get("@change-vector"))
    failures, checked = [], 0
    if p["verify_attachment_content"]:
        for (name, h, size), (_, cv) in sorted(refs.items()):
            checked += 1
            err = check_revision_attachment(request, p, doc_id, name, h, size, cv, timeout=p["timeout"])
            if err:
                failures.append(err)
    return doc_id, {"count": count, "pages": pages}, checked, failures


def main():
    module = AnsibleModule(argument_spec=dict(
        target=dict(type="str", required=True),
        db_name=dict(type="str", required=True),
        ids=dict(type="list", elements="str", required=True),
        pages=dict(type="int", default=5),
        page_size=dict(type="int", default=1024),
        verify_attachment_content=dict(type="bool", default=False),
        parallel=dict(type="int", default=4),
        timeout=dict(type="int", default=600),
        ravendb_domain=dict(type="str", required=True),
        client_cert=dict(type="str", required=True),
        ca_cert=dict(type="str", required=True),
    ), supports_check_mode=True)
    p = module.params
    if not p["ids"]:
        module.fail_json(msg="ids is empty -- nothing to sample")

    samples, failures, checked = {}, [], 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, p["parallel"])) as pool:
            for doc_id, s, n, errs in pool.map(lambda i: one_doc(p, i), p["ids"]):
                samples[doc_id] = s
                checked += n
                failures.extend(errs)
    except Exception as e:
        module.fail_json(msg=str(e))

    summary = "%d docs, %d revisions, %d pages read" % (
        len(samples), sum(s["count"] for s in samples.values()),
        sum(len(s["pages"]) for s in samples.values()))
    if p["verify_attachment_content"]:
        summary += ", %d attachments read back" % checked
    if failures:
        module.fail_json(msg="%d attachment check(s) failed (first 20): %s" % (len(failures), failures[:20]),
                         samples=samples)
    module.exit_json(changed=False, msg=summary, samples=samples, attachments_checked=checked)


if __name__ == "__main__":
    main()
