#!/usr/bin/env python3
"""
scripts/revisions_loader.py -- fast revisions seeder (stdlib only).

Writes `--docs` documents with `--revs` revisions each, `--batch` PUTs per /bulk_docs request
(each PUT in a batch becomes its own revision; docs with few revisions share a request).
Optionally adds a distinct attachment every `--att-every` revisions to the first `--att-docs`
docs.  A collection with a low MinimumRevisionsToKeep yields revision tombstones.

Data is deterministic (module_utils/revision_data.py), so ravendb_revisions_fingerprint can
verify it later.

USAGE
  python3 scripts/revisions_loader.py --target 1a --db db1 \\
      --collection Heavy --id-prefix heavy --docs 100 --revs 3000000 \\
      --att-docs 10 --att-every 1000 --att-size 4096 --threads 16

  --cert / --ca default to $CERT_DIR (default ~/selfsignedmaterials)/client.pem, ca.crt.

Exit 0 on success, 1 on any failed write (all threads stop at the first failure).
"""

import argparse
import os
import sys
import threading
import time
from urllib.parse import quote

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_REPO, "module_utils"))

import ravendb_client as rc  # noqa: E402
import revision_data as rd  # noqa: E402

# 503 = server too busy to start the transaction (nothing committed), safe to resend.
_BUSY_RETRIES = (0.5, 1, 2, 4, 8)


class Loader:
    def __init__(self, a):
        self.a = a
        self.lock = threading.Lock()
        self.revisions = 0
        self.attachments = 0
        self.error = None
        self.pad = "x" * a.pad_bytes
        self.groups = rd.doc_groups(a.docs, a.att_docs, rd.group_size(a.revs, a.batch))

    def _req(self, method, path, body, what):
        for delay in _BUSY_RETRIES + (None,):
            status, resp = rc.request(method, self.a.target, self.a.domain, path,
                                      self.a.cert, self.a.ca, body=body, timeout=300)
            if status in (200, 201, 204):
                return
            if status == 503 and delay is not None:
                time.sleep(delay)
                continue
            raise RuntimeError("%s: HTTP %d %s" % (what, status, resp[:300]))

    def _body(self, v):
        body = {"v": v, "@metadata": {"@collection": self.a.collection}}
        if self.a.pad_bytes:
            body["pad"] = self.pad
        return body

    def _puts(self, doc_ids, first, last):
        """PUT revisions first..last of every doc in doc_ids, up to --batch PUTs per request.
        Each doc's revisions stay in ascending order (a request never reorders them)."""
        a = self.a
        cmds = []
        for doc_id in doc_ids:
            for v in range(first, last + 1):
                cmds.append({"Type": "PUT", "Id": doc_id, "Document": self._body(v)})
                if len(cmds) == a.batch:
                    self._send(cmds, doc_ids, first, last)
                    cmds = []
                    if self.error:
                        return
        if cmds:
            self._send(cmds, doc_ids, first, last)

    def _send(self, cmds, doc_ids, first, last):
        what = "%s..%s revs %d-%d" % (doc_ids[0], doc_ids[-1], first, last) if len(doc_ids) > 1 \
            else "%s revs %d-%d" % (doc_ids[0], first, last)
        self._req("POST", "/databases/%s/bulk_docs" % self.a.db, {"Commands": cmds}, what)
        with self.lock:
            self.revisions += len(cmds)

    def _attachment(self, doc_id, v):
        name = rd.attachment_name(v)
        path = "/databases/%s/attachments?id=%s&name=%s" % (self.a.db, quote(doc_id), quote(name))
        self._req("PUT", path, rd.attachment_bytes(doc_id, name, self.a.att_size),
                  "%s attachment %s" % (doc_id, name))
        with self.lock:
            self.attachments += 1

    def _group(self, lo, hi):
        doc_ids = ["%s/%d" % (self.a.id_prefix, n) for n in range(lo, hi)]
        plan = rd.write_plan(self.a.revs, self.a.att_every, lo < self.a.att_docs)
        for step in plan:
            if self.error:
                return
            if step[0] == "puts":
                self._puts(doc_ids, step[1], step[2])
            else:
                for doc_id in doc_ids:
                    if self.error:
                        return
                    self._attachment(doc_id, step[1])

    def _worker(self, t):
        try:
            for i in range(t, len(self.groups), self.a.threads):
                if self.error:
                    return
                self._group(*self.groups[i])
        except Exception as e:  # first failure stops everyone
            with self.lock:
                self.error = self.error or e

    def run(self):
        a = self.a
        total = a.docs * a.revs
        start = time.time()
        threads = [threading.Thread(target=self._worker, args=(t,), daemon=True)
                   for t in range(min(a.threads, len(self.groups)))]
        for th in threads:
            th.start()
        while True:
            alive = [th for th in threads if th.is_alive()]
            if not alive:
                break
            alive[0].join(timeout=a.progress_secs)
            el = time.time() - start
            print("[%6.0fs] %s: %d/%d revisions (%.0f/s), %d attachments"
                  % (el, a.id_prefix, self.revisions, total, self.revisions / max(el, 1e-9),
                     self.attachments), flush=True)
        el = time.time() - start
        if self.error:
            print("FAILED %s: %s" % (a.id_prefix, self.error), file=sys.stderr, flush=True)
            return 1
        print("DONE %s: %d docs x %d revisions = %d PUTs, %d attachments, %.0fs (%.0f revisions/s)"
              % (a.id_prefix, a.docs, a.revs, self.revisions, self.attachments, el,
                 self.revisions / max(el, 1e-9)), flush=True)
        return 0


def parse_args(argv=None):
    cert_dir = os.environ.get("CERT_DIR", os.path.expanduser("~/selfsignedmaterials"))
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--target", required=True, help="node name, e.g. 1a")
    p.add_argument("--db", required=True)
    p.add_argument("--collection", required=True)
    p.add_argument("--id-prefix", required=True)
    p.add_argument("--docs", type=int, required=True)
    p.add_argument("--revs", type=int, required=True, help="document PUTs per doc")
    p.add_argument("--att-docs", type=int, default=0, help="first N docs get attachments")
    p.add_argument("--att-every", type=int, default=0, help="attachment every N revisions")
    p.add_argument("--att-size", type=int, default=4096, help="attachment size in bytes")
    p.add_argument("--batch", type=int, default=1000,
                   help="PUTs per /bulk_docs request (docs with few revisions share a request)")
    p.add_argument("--pad-bytes", type=int, default=0,
                   help="add a 'pad' string of this many bytes to every revision body")
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--progress-secs", type=float, default=10)
    p.add_argument("--domain", default="hubsink.test")
    p.add_argument("--cert", default=os.path.join(cert_dir, "client.pem"))
    p.add_argument("--ca", default=os.path.join(cert_dir, "ca.crt"))
    a = p.parse_args(argv)
    if a.docs < 1 or a.revs < 1 or a.batch < 1 or a.threads < 1:
        p.error("--docs, --revs, --batch and --threads must be >= 1")
    if a.att_docs and a.att_every < 1:
        p.error("--att-docs needs --att-every >= 1")
    return a


if __name__ == "__main__":
    sys.exit(Loader(parse_args()).run())
