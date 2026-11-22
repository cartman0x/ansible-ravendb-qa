"""Deterministic revision/attachment data shared by the revisions loader
(scripts/revisions_loader.py) and the verifiers
(library/ravendb_revisions_fingerprint.py, library/ravendb_revisions_sample.py).

Both sides derive everything from (doc id, revision number, attachment name, size), so the
verifier can check content read back from the server without storing what was written.
"""

import base64
import hashlib
from urllib.parse import quote


def attachment_name(v):
    return "att-%d" % v


def attachment_bytes(doc_id, name, size):
    """Distinct per (doc, name), so RavenDB's content-hash dedup never merges two of them."""
    seed = ("%s|%s|" % (doc_id, name)).encode()
    reps = size // len(seed) + 1
    return (seed * reps)[:size]


def attachment_hash(data):
    """RavenDB's attachment hash: base64(BLAKE2b-256(content))."""
    return base64.b64encode(hashlib.blake2b(data, digest_size=32).digest()).decode()


def write_plan(revs, att_every=0, with_attachments=False):
    """Ordered steps for one doc: ("puts", first_v, last_v) runs of document PUTs, and
    ("att", v) = add attachment_name(v) right after revision v was written.

    Adding an attachment creates one extra revision, so a doc ends up with
    revs + revs // att_every revisions (before any pruning)."""
    steps = []
    start = 1
    if with_attachments and att_every > 0:
        for v in range(att_every, revs + 1, att_every):
            steps.append(("puts", start, v))
            steps.append(("att", v))
            start = v + 1
    if start <= revs:
        steps.append(("puts", start, revs))
    return steps


def group_size(revs, batch):
    """How many docs share one /bulk_docs request: a doc with 3M revisions gets requests of its
    own, a doc with 4 revisions shares a 1000-PUT request with 249 others."""
    return max(1, batch // max(1, revs))


def doc_groups(docs, att_docs, size):
    """Contiguous [lo, hi) doc-number ranges of at most `size` docs.  A range never mixes docs
    with and without attachments (they follow different write plans)."""
    split = min(max(att_docs, 0), docs)
    out = []
    for lo, hi in ((0, split), (split, docs)):
        out.extend((s, min(hi, s + size)) for s in range(lo, hi, size))
    return out


def expected_revisions(revs, att_every=0, with_attachments=False, keep=None):
    total = revs + (revs // att_every if with_attachments and att_every > 0 else 0)
    return total if keep is None else min(total, keep)


def revision_signature(rev):
    """What must survive the cleanup for one revision: its body version and the
    (name, hash, size) of every attachment it references.  Change vectors and etags are
    deliberately left out -- they are storage details, not data."""
    md = rev.get("@metadata") or {}
    atts = sorted((a.get("Name"), a.get("Hash"), a.get("Size"))
                  for a in (md.get("@attachments") or []))
    return [rev.get("v"), atts]


def fingerprint(revisions):
    """Order-sensitive digest of a doc's revisions (server order, newest first)."""
    h = hashlib.sha256()
    for rev in revisions:
        h.update(repr(revision_signature(rev)).encode())
        h.update(b"\n")
    return {"count": len(revisions), "digest": h.hexdigest()}


def check_revision_attachment(request, p, doc_id, name, expected_hash, size, cv, timeout):
    """Read one attachment back through the revision with change vector `cv` and check it
    against the hash in that revision's metadata and against the content the loader wrote.

    `request` is ravendb_client.request, passed in so this file stays free of HTTP (the loader
    imports it outside Ansible) and tests can stub it.  `p` holds target, ravendb_domain,
    client_cert, ca_cert, db_name.  Returns None when it matches, else one line saying what differs."""
    path = "/databases/%s/attachments?id=%s&name=%s" % (quote(p["db_name"]), quote(doc_id), quote(name))
    status, data = request("POST", p["target"], p["ravendb_domain"], path,
                           p["client_cert"], p["ca_cert"],
                           body={"Type": "Revision", "ChangeVector": cv}, timeout=timeout)
    if status != 200:
        return "%s/%s: HTTP %d reading from revision %s" % (doc_id, name, status, cv)
    got = attachment_hash(data)
    if got != expected_hash:
        return "%s/%s: content hash %s != metadata hash %s" % (doc_id, name, got, expected_hash)
    if data != attachment_bytes(doc_id, name, size):
        return "%s/%s: content differs from what the loader wrote" % (doc_id, name)
    return None
