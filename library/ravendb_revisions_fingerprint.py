#!/usr/bin/python
"""
ravendb_revisions_fingerprint -- per-doc revision fingerprints, plus (optionally) a
read-back of every distinct attachment through the revision that first references it.

For each id: pages through /revisions, returns {count, digest}.  The digest covers each
revision's body version `v` and its attachments' (name, hash, size), in server order --
see module_utils/revision_data.py.  Compare two runs' `fingerprints` to prove nothing
changed.

verify_attachment_content=true: for every distinct (doc, attachment, hash), fetch the
content from the OLDEST revision that references it and check it against the hash in the
metadata and against the deterministic content the loader wrote.  Fails the task on any
mismatch.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.ravendb_client import request
from ansible.module_utils.revision_data import check_revision_attachment, fingerprint


def fetch_revisions(p, doc_id):
    out = []
    start = 0
    while True:
        path = "/databases/%s/revisions?id=%s&start=%d&pageSize=%d" % (
            p["db_name"], quote(doc_id), start, p["page_size"])
        status, body = request("GET", p["target"], p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"], timeout=120)
        if status != 200:
            raise RuntimeError("%s: GET revisions start=%d -> HTTP %d" % (doc_id, start, status))
        page = json.loads(body).get("Results") or []
        out.extend(page)
        if len(page) < p["page_size"]:
            return out
        start += p["page_size"]


def oldest_reference_per_attachment(revisions):
    """{(name, hash, size): change vector of the oldest revision referencing it}."""
    refs = {}
    for rev in revisions:  # newest first -> the last one seen is the oldest
        md = rev.get("@metadata") or {}
        for a in md.get("@attachments") or []:
            refs[(a.get("Name"), a.get("Hash"), a.get("Size"))] = md.get("@change-vector")
    return refs



def one_doc(p, doc_id):
    revisions = fetch_revisions(p, doc_id)
    fp = fingerprint(revisions)
    failures = []
    checked = 0
    if p["verify_attachment_content"]:
        for (name, h, size), cv in sorted(oldest_reference_per_attachment(revisions).items()):
            checked += 1
            err = check_revision_attachment(request, p, doc_id, name, h, size, cv, timeout=120)
            if err:
                failures.append(err)
    return doc_id, fp, checked, failures


def main():
    module = AnsibleModule(argument_spec=dict(
        target=dict(type="str", required=True),
        db_name=dict(type="str", required=True),
        ids=dict(type="list", elements="str", required=True),
        verify_attachment_content=dict(type="bool", default=False),
        page_size=dict(type="int", default=1024),
        parallel=dict(type="int", default=8),
        ravendb_domain=dict(type="str", required=True),
        client_cert=dict(type="str", required=True),
        ca_cert=dict(type="str", required=True),
    ), supports_check_mode=True)
    p = module.params
    if not p["ids"]:
        module.fail_json(msg="ids is empty -- nothing to fingerprint")

    fingerprints, failures, checked = {}, [], 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, p["parallel"])) as pool:
            for doc_id, fp, n, errs in pool.map(lambda i: one_doc(p, i), p["ids"]):
                fingerprints[doc_id] = fp
                checked += n
                failures.extend(errs)
    except Exception as e:
        module.fail_json(msg=str(e))

    total = sum(fp["count"] for fp in fingerprints.values())
    summary = "%d docs, %d revisions" % (len(fingerprints), total)
    if p["verify_attachment_content"]:
        summary += ", %d attachments read back from revisions" % checked
    if failures:
        module.fail_json(msg="%d attachment check(s) failed (first 20): %s" % (
            len(failures), failures[:20]), fingerprints=fingerprints)
    module.exit_json(changed=False, msg=summary, fingerprints=fingerprints,
                     total_revisions=total, attachments_checked=checked)


if __name__ == "__main__":
    main()
