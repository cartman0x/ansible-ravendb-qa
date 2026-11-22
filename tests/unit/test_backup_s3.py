"""
Tests for the S3 paths in ravendb_backup: backup straight to S3, list restore points in an
S3 folder, restore straight from S3.

Hardening focus:  the request bodies carry the right S3Settings (upload-only backup, restore
                  from the chosen restore-point folder), an S3 upload failure is reported as a
                  failure (not as a finished backup), and AWS secrets never leak into a
                  message or an exception.
Unit tests: ravendb_backup.request is monkeypatched.
"""

import json
from collections import defaultdict

import pytest

import ravendb_backup as backup

SECRET = "SECRET-KEY-must-never-be-printed"


def params(**kwargs):
    p = defaultdict(lambda: None)
    p.update({
        "target": "1a", "db_name": "db1", "ravendb_domain": "d", "client_cert": "", "ca_cert": "",
        "timeout": 5, "poll_interval": 0,
        "s3_bucket": "qa-bucket", "s3_region": "eu-west-1", "s3_folder": "datasets/x/6.2.20/abc",
        "s3_access_key": "AKIA-TEST", "s3_secret_key": SECRET,
    })
    p.update(kwargs)
    return p


class FakeServer:
    def __init__(self, backup_status=None, restore_points=None, op_state="Completed"):
        self.calls = []
        self.backup_status = backup_status
        self.restore_points = restore_points or []
        self.op_state = op_state
        self.polls = 0

    def __call__(self, method, target, domain, path, client_cert, ca_cert,
                 body=None, content_type=None, timeout=30):
        self.calls.append((method, path, body))
        if path.endswith("/admin/periodic-backup"):
            return 200, json.dumps({"TaskId": 7}).encode()
        if path.startswith("/periodic-backup/status"):
            self.polls += 1
            # first poll = baseline (no backup yet), then the finished status
            return 200, json.dumps({"Status": None if self.polls == 1 else self.backup_status}).encode()
        if path.startswith("/admin/restore/points"):
            return 200, json.dumps({"List": self.restore_points}).encode()
        if path == "/admin/restore/database":
            return 200, json.dumps({"OperationId": 3}).encode()
        if path.startswith("/operations/state"):
            return 200, json.dumps({"Status": self.op_state}).encode()
        return 200, b"{}"

    def body_for(self, suffix):
        return next(b for m, p, b in self.calls if p.split("?")[0].endswith(suffix))


def test_backup_to_s3_is_upload_only_and_reports_the_s3_folder(monkeypatch):
    srv = FakeServer(backup_status={"NodeTag": "A", "LastFullBackup": "2026-11-11T10:00:00Z",
                                    "LocalBackup": {}, "UploadToS3": {"Skipped": False}})
    monkeypatch.setattr(backup, "request", srv)
    msg = backup.k_backup(params(backup_type="Snapshot"))
    task = srv.body_for("/admin/periodic-backup")
    assert task["BackupType"] == "Snapshot"
    assert task["LocalSettings"] is None
    s3 = task["S3Settings"]
    assert (s3["BucketName"], s3["RemoteFolderName"], s3["AwsRegionName"]) == (
        "qa-bucket", "datasets/x/6.2.20/abc", "eu-west-1")
    assert "s3://qa-bucket/datasets/x/6.2.20/abc" in msg
    assert SECRET not in msg


def test_backup_deletes_its_temporary_task_as_type_backup(monkeypatch):
    srv = FakeServer(backup_status={"NodeTag": "A", "LastFullBackup": "t1", "LocalBackup": {},
                                    "UploadToS3": {"Skipped": False}})
    monkeypatch.setattr(backup, "request", srv)
    backup.k_backup(params())
    deletes = [p for m, p, b in srv.calls if m == "DELETE"]
    assert len(deletes) == 1 and "type=Backup&" in deletes[0] and "id=7" in deletes[0]


def test_backup_fails_if_its_temporary_task_cannot_be_deleted(monkeypatch):
    """A leftover backup task pins every tombstone (seen 2026-11-11) -- never ignore it."""
    srv = FakeServer(backup_status={"NodeTag": "A", "LastFullBackup": "t1", "LocalBackup": {},
                                    "UploadToS3": {"Skipped": False}})
    def with_failing_delete(method, target, domain, path, *a, **kw):
        if method == "DELETE":
            return 500, b'{"Message":"nope"}'
        return srv(method, target, domain, path, *a, **kw)
    monkeypatch.setattr(backup, "request", with_failing_delete)
    with pytest.raises(RuntimeError, match="could not be deleted"):
        backup.k_backup(params())


def test_backup_without_s3_still_writes_locally(monkeypatch):
    srv = FakeServer(backup_status={"NodeTag": "A", "LastFullBackup": "t1",
                                    "LocalBackup": {"BackupDirectory": "/backups/db1-x"}})
    monkeypatch.setattr(backup, "request", srv)
    msg = backup.k_backup(params(s3_bucket=None, backup_path="/backups/db1-x"))
    task = srv.body_for("/admin/periodic-backup")
    assert task["S3Settings"] is None
    assert task["LocalSettings"]["FolderPath"] == "/backups/db1-x"
    assert "/backups/db1-x" in msg


def test_s3_upload_exception_fails_the_backup_without_leaking_secrets(monkeypatch):
    srv = FakeServer(backup_status={"NodeTag": "A", "LastFullBackup": "t1", "LocalBackup": {},
                                    "UploadToS3": {"Exception": "AccessDenied"}})
    monkeypatch.setattr(backup, "request", srv)
    with pytest.raises(RuntimeError) as e:
        backup.k_backup(params())
    assert "AccessDenied" in str(e.value)
    assert SECRET not in str(e.value)


def test_skipped_s3_upload_fails_the_backup(monkeypatch):
    srv = FakeServer(backup_status={"NodeTag": "A", "LastFullBackup": "t1", "LocalBackup": {},
                                    "UploadToS3": {"Skipped": True}})
    monkeypatch.setattr(backup, "request", srv)
    with pytest.raises(RuntimeError, match="skipped"):
        backup.k_backup(params())


def test_restore_points_newest_first(monkeypatch):
    srv = FakeServer(restore_points=[
        {"Location": "f/old", "FileName": "a.ravendb-snapshot", "DateTime": "2026-11-10T10:00:00",
         "IsSnapshotRestore": True, "DatabaseName": "db1", "NodeTag": "A"},
        {"Location": "f/new", "FileName": "b.ravendb-snapshot", "DateTime": "2026-11-11T10:00:00",
         "IsSnapshotRestore": True, "DatabaseName": "db1", "NodeTag": "A"}])
    monkeypatch.setattr(backup, "request", srv)
    msg, extra = backup.k_restore_points(params())
    assert [p["location"] for p in extra["restore_points"]] == ["f/new", "f/old"]
    assert extra["restore_points"][0]["is_snapshot"] is True
    method, path, body = srv.calls[0]
    assert path == "/admin/restore/points?type=S3" and body["BucketName"] == "qa-bucket"
    assert msg.startswith("2 restore point(s)") and SECRET not in msg


def test_restore_points_empty_folder_is_zero_points_not_an_error(monkeypatch):
    """6.2.20 answers an empty S3 folder with HTTP 500 'Couldn't locate any backup files.'
    (seen on the first dataset build, 2026-11-11) -- that must read as 'no snapshots yet'."""
    def fake(method, target, domain, path, client_cert, ca_cert, body=None, content_type=None, timeout=30):
        return 500, (b'{"Url":"/admin/restore/points?type=S3","Type":"System.InvalidOperationException",'
                     b'"Message":"Couldn\'t locate any backup files."}')
    monkeypatch.setattr(backup, "request", fake)
    msg, extra = backup.k_restore_points(params())
    assert extra == {"restore_points": []}
    assert msg.startswith("0 restore point(s)")


def test_restore_points_other_server_errors_still_fail(monkeypatch):
    def fake(method, target, domain, path, client_cert, ca_cert, body=None, content_type=None, timeout=30):
        return 500, b'{"Message":"Access Denied"}'
    monkeypatch.setattr(backup, "request", fake)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        backup.k_restore_points(params())


def test_restore_from_s3_points_at_the_restore_point_folder(monkeypatch):
    srv = FakeServer()
    monkeypatch.setattr(backup, "request", srv)
    msg = backup.k_restore(params(backup_path="datasets/x/6.2.20/abc/2026-11-11.ravendb-db1-A-snapshot",
                                  new_db_name="db1"))
    body = srv.body_for("/admin/restore/database")
    assert body["Type"] == "S3"
    assert body["Settings"]["RemoteFolderName"] == "datasets/x/6.2.20/abc/2026-11-11.ravendb-db1-A-snapshot"
    assert body["DatabaseName"] == "db1"
    assert "s3://qa-bucket/datasets/x/6.2.20/abc/2026-11-11.ravendb-db1-A-snapshot" in msg
    assert SECRET not in msg


def test_missing_credentials_fail_without_printing_any(monkeypatch):
    monkeypatch.setattr(backup, "request", FakeServer())
    with pytest.raises(ValueError) as e:
        backup.k_restore_points(params(s3_access_key=None))
    assert "AWS_ACCESS_KEY_ID" in str(e.value) and SECRET not in str(e.value)
