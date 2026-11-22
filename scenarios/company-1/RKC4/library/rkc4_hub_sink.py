#!/usr/bin/python
"""rkc4_hub_sink -- RKC4's hub/sink setup: define a pull-replication hub task and attach sinks.

A copy of define_hub / attach_sinks from library/ravendb_tasks.py, which the repo removed;
only RKC4 (the hub/sink bug reproduction) needs them.  Ansible finds it in the library/
folder next to rkc4.yml."""

import base64
import json
import os
from urllib.parse import quote

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.ravendb_client import request


def read_pfx_b64(certs_dir, sink_cluster_id):
    path = os.path.join(certs_dir, "cluster%s.replication.pfx.b64" % sink_cluster_id)
    with open(path, "r") as f:
        return f.read().strip()


def assert_target_hosts_db(p, target, db):
    s, b = request("GET", target, p["ravendb_domain"],
                   "/admin/databases?name=%s" % db,
                   p["client_cert"], p["ca_cert"])
    if s != 200:
        raise RuntimeError("can't check db membership: GET /admin/databases?name=%s "
                           "on %s returned HTTP %d" % (db, target, s))
    rec = json.loads(b)
    if (rec.get("Sharding") or {}).get("Shards"):
        return  # sharded -- any node is fine, orchestrator routes
    members = ((rec.get("Topology") or {}).get("Members") or [])
    tag = target[-1].upper()
    if tag not in members:
        raise RuntimeError(
            "target node %s (tag %s) is NOT a member of db '%s' (members=%s). "
            "Either pick a node from the Members list, or recreate the db with "
            "replication_factor large enough to cover %s "
            "(create_database -e replication_factor=N)." % (target, tag, db, members, target))


def sink_leader_url(domain, sink_cluster_id):
    return "https://%sa.%s:443" % (sink_cluster_id, domain)


def hub_task_exists(p, hub_url, db, task_name):
    path = "/databases/%s/tasks?type=PullReplicationAsHub" % db
    status, body = request("GET", p["hub_leader"], p["ravendb_domain"], path,
                           p["client_cert"], p["ca_cert"])
    if status != 200:
        raise RuntimeError("GET %s failed: HTTP %d" % (path, status))
    definitions = json.loads(body).get("PullReplications") or []
    for entry in definitions:
        if entry.get("Name") == task_name:
            return True
    return False


def sink_task_lookup(p, sink_leader, db, sink_id):
    """Return the PullReplicationAsSink task entry matching this sink_id, or
    None.  Raises on unreachable leader or unexpected HTTP status."""
    path = "/databases/%s/tasks" % db
    status, body = request("GET", sink_leader, p["ravendb_domain"], path,
                           p["client_cert"], p["ca_cert"])
    if status != 200:
        raise RuntimeError("GET %s failed: HTTP %d" % (path, status))
    expected_name = "cluster%s-to-hub" % sink_id
    ongoing = json.loads(body).get("OngoingTasks") or []
    for entry in ongoing:
        if (entry.get("TaskType") == "PullReplicationAsSink"
                and entry.get("TaskName") == expected_name):
            return entry
    return None


def sink_task_exists(p, sink_leader, db, sink_id):
    return sink_task_lookup(p, sink_leader, db, sink_id) is not None


def k_define_hub(p):
    hub_leader = p["hub_leader"]
    db = p["db_name"]
    task_name = p["hub_task_name"]
    sink_ids = p["sink_cluster_ids"]
    sink_paths = p["sink_allowed_paths"]
    sink_to_hub = p["sink_to_hub_paths"] or {}
    mode = p["replication_mode"] or "HubToSink, SinkToHub"
    certs_dir = p["replication_certs_dir"]

    os.makedirs(certs_dir, exist_ok=True)

    # 1. Create the hub task if it doesn't exist.
    if not hub_task_exists(p, hub_leader, db, task_name):
        body = {
            "Name": task_name,
            "TaskId": None,
            "DelayReplicationFor": None,
            "Mode": mode,
            "Disabled": False,
            "PreventDeletionsMode": "None",
            "WithFiltering": True,
        }
        path = "/databases/%s/admin/tasks/pull-replication/hub" % db
        status, _ = request("PUT", hub_leader, p["ravendb_domain"], path,
                            p["client_cert"], p["ca_cert"], body=body)
        if status not in (200, 201):
            raise RuntimeError("create hub task '%s' failed: HTTP %d" % (task_name, status))

    # 2. For each sink: mint cert, save PFX, register hub access entry.
    minted = []
    for sink_id in sink_ids:
        sink_id_str = str(sink_id)

        mint_path = "/databases/%s/admin/pull-replication/generate-certificate?validMonths=12" % db
        status, body_bytes = request("POST", hub_leader, p["ravendb_domain"], mint_path,
                                     p["client_cert"], p["ca_cert"])
        if status not in (200, 201):
            raise RuntimeError("mint cert for sink %s failed: HTTP %d" % (sink_id_str, status))
        cert_data = json.loads(body_bytes)
        pfx_b64 = cert_data["Certificate"]
        public_key = cert_data["PublicKey"]

        b64_path = os.path.join(certs_dir, "cluster%s.replication.pfx.b64" % sink_id_str)
        with open(b64_path, "w") as f:
            f.write(pfx_b64)
        os.chmod(b64_path, 0o600)

        bin_path = os.path.join(certs_dir, "cluster%s.replication.pfx" % sink_id_str)
        with open(bin_path, "wb") as f:
            f.write(base64.b64decode(pfx_b64))
        os.chmod(bin_path, 0o600)

        allowed_hub_to_sink = sink_paths.get(sink_id_str) or sink_paths.get(sink_id) or ["*"]
        allowed_sink_to_hub = sink_to_hub.get(sink_id_str) or sink_to_hub.get(sink_id) or ["*"]

        access_body = {
            "Name": "access-cluster%s" % sink_id_str,
            "CertificateBase64": public_key,
            "AllowedHubToSinkPaths": allowed_hub_to_sink,
            "AllowedSinkToHubPaths": allowed_sink_to_hub,
        }
        access_path = "/databases/%s/admin/tasks/pull-replication/hub/access?name=%s" % (
            db, quote(task_name))
        status, _ = request("PUT", hub_leader, p["ravendb_domain"], access_path,
                            p["client_cert"], p["ca_cert"], body=access_body)
        if status not in (200, 201):
            raise RuntimeError("register access for sink %s failed: HTTP %d" % (sink_id_str, status))

        minted.append(sink_id_str)

    return ("DEFINED hub task '%s' on %s + access entries for sinks %s "
            "(PFXs in %s)" % (task_name, hub_leader, minted, certs_dir))


def k_attach_sinks(p):
    db = p["db_name"]
    task_name = p["hub_task_name"]
    sink_ids = p["sink_cluster_ids"]
    sink_paths = p["sink_allowed_paths"]
    sink_to_hub = p["sink_to_hub_paths"] or {}
    mode = p["replication_mode"] or "HubToSink, SinkToHub"
    conn_name = p["connection_string_name"] or "hub-connection"
    hub_urls = p["hub_topology_urls"]
    certs_dir = p["replication_certs_dir"]

    attached = []
    for sink_id in sink_ids:
        sink_id_str = str(sink_id)
        sink_leader = "%sa" % sink_id_str
        leader_url = sink_leader_url(p["ravendb_domain"], sink_id_str)

        cs_body = {
            "Name": conn_name,
            "Database": db,
            "TopologyDiscoveryUrls": hub_urls,
            "Type": "Raven",
        }
        cs_path = "/databases/%s/admin/connection-strings" % db
        status, _ = request("PUT", sink_leader, p["ravendb_domain"], cs_path,
                            p["client_cert"], p["ca_cert"], body=cs_body)
        if status not in (200, 201):
            raise RuntimeError("create connection string on %s failed: HTTP %d" %
                               (sink_leader, status))

        existing = sink_task_lookup(p, sink_leader, db, sink_id_str)
        if existing is not None:
            # Don't silently re-use a half-broken task.  Verify state +
            # connection string match expectations BEFORE claiming "attached".
            state = existing.get("TaskState")
            existing_cs = existing.get("ConnectionStringName")
            problems = []
            if state not in (None, "Enabled"):   # None = older Raven server, treat as ok
                problems.append("TaskState=%r" % state)
            if existing_cs and existing_cs != conn_name:
                problems.append("ConnectionStringName=%r (expected %r)"
                                % (existing_cs, conn_name))
            if problems:
                raise RuntimeError(
                    "k_attach_sinks: sink-pull task on cluster %s is already "
                    "present but unhealthy: %s -- refusing to claim 'attached' "
                    "on a broken task.  Delete the task and re-run, or fix "
                    "manually." % (sink_id_str, problems))
            attached.append("%s (already present, verified healthy)" % sink_id_str)
            continue

        pfx_b64 = read_pfx_b64(certs_dir, sink_id_str)

        allowed_hub_to_sink = sink_paths.get(sink_id_str) or sink_paths.get(sink_id) or ["*"]
        allowed_sink_to_hub = sink_to_hub.get(sink_id_str) or sink_to_hub.get(sink_id) or ["*"]

        task_body = {
            "PullReplicationAsSink": {
                "TaskId": None,
                "Name": "cluster%s-to-hub" % sink_id_str,
                "ConnectionStringName": conn_name,
                "HubName": task_name,
                "Mode": mode,
                "Disabled": False,
                "AccessName": "access-cluster%s" % sink_id_str,
                "CertificateWithPrivateKey": pfx_b64,
                "AllowedHubToSinkPaths": allowed_hub_to_sink,
                "AllowedSinkToHubPaths": allowed_sink_to_hub,
            }
        }
        task_path = "/databases/%s/admin/tasks/sink-pull-replication" % db
        status, _ = request("POST", sink_leader, p["ravendb_domain"], task_path,
                            p["client_cert"], p["ca_cert"], body=task_body)
        if status not in (200, 201):
            raise RuntimeError("create sink-pull on cluster %s failed: HTTP %d" %
                               (sink_id_str, status))
        attached.append(sink_id_str)

    return ("ATTACHED sinks %s to hub task '%s' on database '%s' "
            "(leader URLs %s)" %
            (attached, task_name, db,
             [sink_leader_url(p["ravendb_domain"], str(sid)) for sid in sink_ids]))


KINDS = {
    "define_hub":   k_define_hub,
    "attach_sinks": k_attach_sinks,
}


def main():
    module = AnsibleModule(argument_spec=dict(
        kind=dict(required=True, choices=list(KINDS)),
        ravendb_domain=dict(required=True),
        client_cert=dict(required=True, type="path"),
        ca_cert=dict(required=True, type="path"),
        hub_leader=dict(default=None),
        db_name=dict(required=True),
        hub_task_name=dict(default=None),
        sink_cluster_ids=dict(type="list", elements="raw", default=None),
        sink_allowed_paths=dict(type="dict", default=None),
        sink_to_hub_paths=dict(type="dict", default=None),
        replication_mode=dict(default=None),
        replication_certs_dir=dict(type="path", default=None),
        hub_topology_urls=dict(type="list", elements="str", default=None),
        connection_string_name=dict(default=None),
    ))

    handler = KINDS[module.params["kind"]]
    try:
        message = handler(module.params)
    except Exception as e:
        module.fail_json(msg=str(e))

    module.exit_json(changed=True, msg=message)


if __name__ == "__main__":
    main()
