#!/usr/bin/python
"""ravendb_backup -- Ansible module that triggers and restores RavenDB backups."""

import json
import os
import random
import time
from urllib.parse import quote

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.ravendb_client import request
from ansible.module_utils.polling import poll_until


def log_line(progress_log, message):
    """Append a timestamped line to `progress_log` if one is configured."""
    if not progress_log:
        return
    with open(progress_log, "a") as f:
        f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), message))


def wait_for_operation(p, target, base_path, op_id, timeout, interval, progress_log):
    """Poll /operations/state for `op_id` until it reaches a terminal state; return (state, elapsed)."""
    state_path = "%s/operations/state?id=%d" % (base_path, op_id)

    log_line(progress_log,
             "op %d  STARTED waiting (target=%s, path=%s, timeout=%ds, interval=%ds)" %
             (op_id, target, state_path, timeout, interval))

    def predicate():
        log_line(progress_log, "op %d  polling..." % op_id)
        try:
            status, body = request("GET", target, p["ravendb_domain"], state_path,
                                   p["client_cert"], p["ca_cert"])
        except Exception as e:
            log_line(progress_log, "op %d  poll FAILED: %s" % (op_id, repr(e)))
            return False, {"error": repr(e)}

        if status != 200:
            log_line(progress_log, "op %d  poll HTTP %d  body=%s" %
                     (op_id, status, body[:200] if body else ""))
            return False, {"http_status": status}

        data = json.loads(body)
        op_status = data.get("Status", "?")
        progress = data.get("Progress") or {}
        log_line(progress_log, "op %d  status=%s  progress=%s" %
                 (op_id, op_status, json.dumps(progress)))

        if op_status in ("Completed", "Faulted", "Canceled"):
            return True, data
        return False, data

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=interval)
    log_line(progress_log, "op %d  FINISHED (done=%s, elapsed=%.1fs)" % (op_id, done, elapsed))
    if not done:
        raise RuntimeError("operation %d did not finish within %.1fs; last state: %s" %
                           (op_id, elapsed, value))
    return value, elapsed


def get_backup_status(p, target, db, task_id):
    """Return the backup Status dict, or None when the task has never produced a backup yet."""
    status_path = "/periodic-backup/status?name=%s&taskId=%d" % (quote(db), task_id)
    status, body = request("GET", target, p["ravendb_domain"], status_path,
                           p["client_cert"], p["ca_cert"])
    if status != 200:
        raise RuntimeError(
            "get_backup_status: %s/%s taskId=%d returned HTTP %d body=%s -- "
            "can't distinguish in-progress from real server failure"
            % (target, db, task_id, status, (body or b"")[:300]))
    data = json.loads(body)
    return data.get("Status") or None


def s3_settings(p, remote_folder=None):
    """RavenDB S3Settings from the module's s3_* params, or None when no bucket is set.
    Secrets go only into the request body -- never into messages or logs."""
    if not p.get("s3_bucket"):
        return None
    if not (p.get("s3_access_key") and p.get("s3_secret_key") and p.get("s3_region")):
        raise ValueError("S3 needs s3_access_key, s3_secret_key and s3_region "
                         "(AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION)")
    return {
        "Disabled": False,
        "AwsAccessKey": p["s3_access_key"],
        "AwsSecretKey": p["s3_secret_key"],
        "AwsSessionToken": p.get("s3_session_token") or None,
        "AwsRegionName": p["s3_region"],
        "BucketName": p["s3_bucket"],
        "RemoteFolderName": remote_folder if remote_folder is not None else (p.get("s3_folder") or ""),
        "CustomServerUrl": None,
        "ForcePathStyle": False,
    }


def s3_url(p, folder=None):
    return "s3://%s/%s" % (p["s3_bucket"], folder if folder is not None else (p.get("s3_folder") or ""))


def k_backup(p):
    """Create a periodic-backup task, trigger it, wait for completion, and clean up the task."""
    target = p["target"]
    db = p["db_name"]
    backup_type = p["backup_type"] or "Backup"
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = p["backup_path"] or "/backups/%s-%s" % (db, timestamp)
    timeout = p["timeout"]
    interval = p["poll_interval"]
    progress_log = p["progress_log"]

    task_name = "manual-%s-%d" % (timestamp, random.randint(0, 9999))
    s3 = s3_settings(p)

    # 1. Create a periodic-backup task scheduled far in the future.
    task_body = {
        "Name": task_name,
        "BackupType": backup_type,
        "Disabled": False,
        "FullBackupFrequency": "0 0 1 1 *",
        "IncrementalBackupFrequency": None,
        # S3 mode: upload only (RavenDB stages the file in its temp folder, then uploads).
        "LocalSettings": None if s3 else {
            "FolderPath": backup_path,
            "Disabled": False,
        },
        "S3Settings": s3,
        "AzureSettings": None,
        "GoogleCloudSettings": None,
        "FtpSettings": None,
        "GlacierSettings": None,
    }
    create_path = "/databases/%s/admin/periodic-backup" % db
    status, body = request("POST", target, p["ravendb_domain"], create_path,
                           p["client_cert"], p["ca_cert"], body=task_body)
    if status not in (200, 201):
        raise RuntimeError("create periodic-backup task failed: HTTP %d" % status)
    task_id = int(json.loads(body)["TaskId"])
    log_line(progress_log, "task created  name=%s  taskId=%d" % (task_name, task_id))

    # 2. Capture the baseline LastFullBackup (may be None for a fresh task).
    baseline = get_backup_status(p, target, db, task_id)
    baseline_lfb = (baseline or {}).get("LastFullBackup")
    log_line(progress_log, "baseline LastFullBackup=%s" % baseline_lfb)

    # 3. Trigger the backup.
    trigger_path = "/databases/%s/admin/backup/database?taskId=%d" % (db, task_id)
    status, _ = request("POST", target, p["ravendb_domain"], trigger_path,
                        p["client_cert"], p["ca_cert"])
    if status not in (200, 201):
        raise RuntimeError("trigger backup failed: HTTP %d" % status)
    log_line(progress_log, "backup TRIGGERED  taskId=%d" % task_id)

    # 4. Poll until LastFullBackup advances or the local backup records an Exception.
    def predicate():
        log_line(progress_log, "polling  taskId=%d" % task_id)
        s = get_backup_status(p, target, db, task_id)
        if s is None:
            log_line(progress_log, "status fetch returned no data")
            return False, None

        local = s.get("LocalBackup") or {}
        exception = local.get("Exception") or (s.get("UploadToS3") or {}).get("Exception")
        if exception:
            log_line(progress_log, "FAILED  exception=%s" % exception)
            return True, s

        current_lfb = s.get("LastFullBackup")
        log_line(progress_log, "LastFullBackup=%s  node=%s  dir=%s" % (
            current_lfb, s.get("NodeTag"), local.get("BackupDirectory")))
        if current_lfb and current_lfb != baseline_lfb:
            return True, s
        return False, s

    done, last_status, elapsed = poll_until(predicate, timeout=timeout, interval=interval)
    log_line(progress_log, "FINISHED  done=%s  elapsed=%.1fs" % (done, elapsed))

    # 5. Delete the periodic-backup task.  NOT best-effort: a leftover backup task pins every
    #    tombstone for its next incremental backup (MinAllDocsEtag 0), so the tombstone cleaner
    #    deletes nothing.  The ongoing-task type is "Backup" -- "PeriodicBackup" is HTTP 500,
    #    which this used to ignore (found 2026-11-11: a dataset build kept 499,500 tombstones).
    cleanup_path = ("/databases/%s/admin/tasks?id=%d&type=Backup&taskName=%s" %
                    (db, task_id, quote(task_name)))
    del_status, del_body = request("DELETE", target, p["ravendb_domain"], cleanup_path,
                                   p["client_cert"], p["ca_cert"])
    if del_status not in (200, 201, 204):
        raise RuntimeError(
            "backup finished but its temporary task %s (TaskId=%d) could not be deleted: HTTP %d %s"
            " -- it holds tombstones; delete it via Studio > Ongoing Tasks"
            % (task_name, task_id, del_status, (del_body or b"")[:200]))

    if not done:
        raise RuntimeError("backup did not complete within %.1fs; last status: %s" %
                           (elapsed, json.dumps(last_status)))

    local = (last_status or {}).get("LocalBackup") or {}
    upload = (last_status or {}).get("UploadToS3") or {}
    exception = local.get("Exception") or upload.get("Exception")
    if exception:
        raise RuntimeError("backup failed: %s" % exception)
    if s3 and upload.get("Skipped"):
        raise RuntimeError("backup finished but the S3 upload was skipped: %s" % json.dumps(upload))

    folder = s3_url(p) if s3 else (local.get("BackupDirectory") or backup_path)
    duration_ms = local.get("FullBackupDurationInMs")
    return ("BACKED UP %s (%s) on responsible node %s -> %s "
            "(TaskId=%d, server reports %dms, total wall-clock %.1fs)" %
            (db, backup_type, last_status.get("NodeTag"), folder,
             task_id, duration_ms or 0, elapsed))


def k_restore(p):
    """Trigger a local restore into a new database and wait for the operation to complete."""
    target = p["target"]
    backup_path = p["backup_path"]
    new_db = p["new_db_name"]
    timeout = p["timeout"]
    interval = p["poll_interval"]

    if not backup_path or not new_db:
        raise ValueError("kind=restore requires `backup_path` and `new_db_name` "
                         "(with s3_bucket set, backup_path is the restore point's S3 folder)")

    # 1. Trigger the restore.
    s3 = s3_settings(p, remote_folder=backup_path)
    if s3:
        body = {
            "Type": "S3",
            "Settings": s3,
            "DatabaseName": new_db,
            "DisableOngoingTasks": True,
            "EncryptionKey": None,
        }
        backup_path = s3_url(p, backup_path)
    else:
        body = {
            "Type": "Local",
            "BackupLocation": backup_path,
            "DatabaseName": new_db,
            "DisableOngoingTasks": True,
            "EncryptionKey": None,
        }
    trigger_path = "/admin/restore/database"
    status, resp = request("POST", target, p["ravendb_domain"], trigger_path,
                           p["client_cert"], p["ca_cert"], body=body)
    if status not in (200, 201):
        raise RuntimeError("trigger restore failed: HTTP %d" % status)
    op_id = int(json.loads(resp)["OperationId"])

    # 2. Wait for the restore to finish (server-level operation, not DB-scoped).
    state, elapsed = wait_for_operation(p, target, "", op_id, timeout, interval, p["progress_log"])

    if state.get("Status") != "Completed":
        raise RuntimeError("restore ended with Status=%s: %s" %
                           (state.get("Status"), json.dumps(state)))

    return ("RESTORED %s -> new DB '%s' on %s "
            "(OperationId=%d, took %.1fs)" %
            (backup_path, new_db, target, op_id, elapsed))


def k_restore_points(p):
    """Lists the backups under s3_folder (S3) or backup_path (local), newest first.
    Returns (message, {"restore_points": [...]})."""
    s3 = s3_settings(p)
    if s3:
        path, body, where = "/admin/restore/points?type=S3", s3, s3_url(p)
    else:
        if not p.get("backup_path"):
            raise ValueError("kind=restore_points needs s3_bucket (+ s3_folder) or backup_path")
        path = "/admin/restore/points?type=Local"
        body = {"FolderPath": p["backup_path"]}
        where = p["backup_path"]
    status, resp = request("POST", p["target"], p["ravendb_domain"], path,
                           p["client_cert"], p["ca_cert"], body=body, timeout=120)
    # RavenDB 6.2 answers an empty (or missing) folder with HTTP 500 InvalidOperationException
    # "Couldn't locate any backup files." instead of an empty list -- that means "none yet".
    if status == 500 and b"Couldn't locate any backup files" in (resp or b""):
        return ("0 restore point(s) in %s" % where, {"restore_points": []})
    if status != 200:
        raise RuntimeError("listing restore points in %s failed: HTTP %d %s"
                           % (where, status, (resp or b"")[:300]))
    points = [{
        "location": rp.get("Location"),
        "file_name": rp.get("FileName"),
        "date_time": rp.get("DateTime"),
        "is_snapshot": bool(rp.get("IsSnapshotRestore")),
        "database_name": rp.get("DatabaseName"),
        "node_tag": rp.get("NodeTag"),
    } for rp in (json.loads(resp).get("List") or [])]
    points.sort(key=lambda x: x["date_time"] or "", reverse=True)
    return ("%d restore point(s) in %s" % (len(points), where), {"restore_points": points})


KINDS = {
    "backup":          k_backup,
    "restore":         k_restore,
    "restore_points":  k_restore_points,
}


def main():
    module = AnsibleModule(argument_spec=dict(
        kind=dict(required=True, choices=list(KINDS)),
        ravendb_domain=dict(required=True),
        client_cert=dict(required=True, type="path"),
        ca_cert=dict(required=True, type="path"),
        target=dict(required=True),
        # backup
        db_name=dict(default=None),
        backup_type=dict(default=None, choices=["Backup", "Snapshot", None]),
        backup_path=dict(default=None),
        # restore
        new_db_name=dict(default=None),
        # S3 (backup destination / restore source / restore_points).  Unset bucket = local.
        s3_bucket=dict(default=None),
        s3_region=dict(default=None),
        s3_folder=dict(default=None),
        s3_access_key=dict(default=None, no_log=True),
        s3_secret_key=dict(default=None, no_log=True),
        s3_session_token=dict(default=None, no_log=True),
        # timing (used by backup + restore)
        timeout=dict(type="int", default=300),
        poll_interval=dict(type="int", default=3),
        # optional: write per-poll progress lines here so users can tail -F it
        progress_log=dict(type="path", default=None),
    ))

    handler = KINDS[module.params["kind"]]
    try:
        result = handler(module.params)
    except Exception as e:
        module.fail_json(msg=str(e))

    message, extra = result if isinstance(result, tuple) else (result, {})
    module.exit_json(changed=module.params["kind"] != "restore_points", msg=message, **extra)


if __name__ == "__main__":
    main()
