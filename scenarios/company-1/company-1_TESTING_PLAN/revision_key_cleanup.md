# Revisions key cleanup tests (RKC)

These tests cover the PR **RavenDB-27675** ("Move revisions stored under 6.2 keys to hashed
keys"). It adds the **revisions key cleanup**: a one-time migration that runs the first time a
6.2 database loads on a build that has it. The cleanup reports its progress only in the server log.

Each RKC test follows the same steps:

1. Write a dataset on **6.2**.
2. Upgrade **in place** to the build under test.
3. Wait for the cleanup to finish.
4. Check that nothing was lost or changed: documents, revisions, attachments and tombstones.
5. Restart, and check that the cleanup does not run again.

RKC4 is the exception: it reproduces the bug itself.

| Test | What it checks | Lab | Time |
|---|---|---|---|
| `RKC1` | Smoke test: 5 documents with 5 revisions each | 1 node | minutes |
| `RKC2` | Dataset 3: revisions with attachments, plus ~100k revision tombstones. Every revision is checked. The tombstone cleaner runs during the cleanup and must keep every revision tombstone. | 1 node | minutes |
| `RKC3` | Dataset 2: 60.9M documents + 239.1M revisions = 300M, 15M tombstones, 500k attachments | 1 node | ~13 h on kaiju; days on a laptop |
| `RKC4` | **The bug itself**: a 6.2 hub and sink are upgraded, then the hub sends everything again | 2x2 | ~20-30 min |
| `RKC5` | Dataset 4: 3 databases (RKC3's shapes scaled down, ~6M revisions each) clean up **at once**, plus a bystander database taking user writes. Reports CPU, memory and disk I/O of the server and the bystander's write latency (idle / during / after), per-db cleanup time; checks the data of every db. | 1 node | ~1 h on a laptop |
| `RKC6` | **Restart in the middle of the cleanup**: a graceful restart while it walks documents, a SIGKILL while it walks revision tombstones. The progress lines must show it continued, not started over. Small dataset (200k revisions, ~200k tombstones, attachments). | 1 node | minutes |
| `RKC7` | Dataset 1: a few documents with **millions of revisions each** (laptop: 4 x 2.5M + one doc 1M over a keep-1000 limit; `-e d1_profile=kaiju`: 10 x 10M + 5M). Writes and enforce on the over-limit doc while the cleanup has not reached it must not prune it; enforce after the cleanup brings it to exactly its limit. Reports the cleanup's timeline per document, CPU / memory / disk I/O, and the latency of user writes to the same database. | 1 node | hours on a laptop (estimate) |
| `RKC8` | **Rolling upgrade under load**: a 3-node 6.2 cluster (4M revisions, replicated) is upgraded one node at a time while a writer keeps updating 2000 of the documents the cleanup walks, on each document's home node. Every node must end with the same revisions: untouched documents as on 6.2, written ones with exactly the seeded + accepted writes. Also: no HTTP 500, no conflicts, no orphans, tombstones unchanged; and a check that the overlap really happened. | 1x3 | ~1 h (estimate) |
| `RKC9` | **Disk full in the middle of the cleanup**: the database sits on its own small filesystem (a loop-mounted ext4 image inside the container), which is filled once the cleanup is running. The server must stay up; after the space is freed and the server restarted, the cleanup must finish with every revision, attachment and tombstone intact. Reports how the database and a user write answered while the disk was full. | 1 node | ~20-30 min (estimate) |
| `RKC10` | **Databases bigger than memory**: RKC5 (3 databases cleaning up at once, CPU / memory / disk / write latency) at 4x the size, ~24 GB of data, in a container capped at 8 GB (`RKC10_MEMORY`), **on a disk capped like a cloud volume: 1500 IOPS, 100 MB/s** (applied right before the upgrade, so the 6.2 seed runs at full speed). The cleanup has to read from a slow disk, as it does on customer cloud servers whose database is bigger than RAM. It also times user writes to one of the databases **while it is being cleaned**, not only to the bystander. Every other test runs with the data in memory. | 1 node | unknown (hours?) |


## Running a test

From the repo root:

```bash
scenarios/company-1/RKC1/run.sh 6.2.20 builds/raven-prNNN.deb
```

- **Arguments:**
  - the 6.2 version to start from;
  - the build to upgrade to. That is the `.deb` under test, or, for RKC4, a released version such as `7.2.7`, which is downloaded.
- **What `run.sh` does:** it builds a fresh lab (teardown, provision, install 6.2, form the cluster), then runs the test's `rkcN.yml`.
- **Extra flags:** anything after the two arguments is passed to `ansible-playbook`, for example `-e debug=true`.
- **Memory limit:** put `CONTAINER_MEMORY=10g` in front of the command to set the node's memory limit.

A passing run ends with a `RESULT ... PASSED` summary. A failed check stops the run with a
message that says what differed.

## Running several tests

```bash
scenarios/company-1/run_all.sh 6.2.20 builds/raven-prNNN.deb                       # RKC1 RKC2 RKC6 RKC4 RKC5 RKC7
scenarios/company-1/run_all.sh 6.2.20 builds/raven-prNNN.deb --tests "RKC1 RKC4-repro RKC4"
```

- **One after another**, never in parallel: every `run.sh` tears down the whole lab first. The script refuses to start while an `ansible-playbook` is running (`--force` to override).
- **Keeps going** after a failure, then prints a summary: each test PASSED / FAILED and how long it took.
- **RKC4 vs RKC4-repro:** RKC4 gets the `.deb` and expects the bug **FIXED**; `RKC4-repro` runs RKC4 against a released version (`--repro-version`, default 7.2.7) and expects it to **REPRODUCE** -- proof the test can catch the bug. Not in the default list.
- **RKC3** is not in the default list (days on a laptop): run it on kaiju (`rkc-common/remote.sh`), or list it explicitly. RKC8 and RKC9 are not in it yet either.
- **Output** in `/tmp/rkc-all-<date>-<time>/` (or `--out`): `<test>.log`, `<test>/` with that test's report files, `summary.txt`.

## RKC4 -- reproducing the bug

The other tests check the cleanup on a single upgraded server. The bug that the PR fixes needs
more than that, so RKC4 rebuilds the PR's own `InterversionTests` flow
(`SetupAffectedSinkAsync`) on a 2x2 lab:

1. **Set up on 6.2:**
   - A hub database on 1a + 1b pulls to a sink on 2a.
   - Some documents are written in a **cluster-wide** transaction (change vector `RAFT:..,TRXN:..`); others are written normally, as controls.
2. **Upgrade the hub:**
   - The sink goes down.
   - The hub is upgraded and gets a new node, 1c, and the hub task moves to it.
   - A 7.2.5-7.2.7 hub writes cluster-wide change vectors in a different form (`RAFT:.., TRXN:..`). The new node sends every revision again, so the sink stores those revisions a second time (**twins**).
3. **Upgrade the sink and check it:**
   - The sink is upgraded, and its revision limit is lowered.
   - The test then writes each document again, deletes all revisions of a document, and enforces the revisions configuration.
   - On 7.2.7, deleting one twin deletes both. The operation then fails with a NullReferenceException or deletes the wrong revision.

```bash
scenarios/company-1/RKC4/run.sh 6.2.20 7.2.7                       # expects REPRODUCED
scenarios/company-1/RKC4/run.sh 6.2.20 builds/raven-prNNN.deb      # expects FIXED
```

The run prints one line per check (`BUG` / `OK`) and saves the results to `/tmp/rkc-repro-<build>.json`.

**Result on 2026-11-14:** on 7.2.7 the bug **reproduced**.

- **Twins:** after the hub sent everything again, each cluster-wide document listed 6 revisions instead of 3.
- **Failing write:** writing such a document past the limit returned HTTP 500, `NullReferenceException` at `RevisionsStorage.DeleteRevisionFromTable` (`RevisionsStorage.cs:933`).
- **Controls:** the normal documents behaved correctly.

**Separate finding: hub access lost on a new node.** The sink's hub-access certificate was registered on 6.2. It is **missing on a node that joins the hub cluster after the upgrade to 7.2.7**:

- 1a and 1b list it, but 1c lists nothing, although all three are at the same cluster index.
- As a result, 1c refuses the sink (403), and nothing is sent again.

RKC4 reports the missing certificate and registers it again as a workaround. This needs to go to DEV as its own issue.

## Tombstones while the key cleanup runs

**DEV, 2026-11-17:** while the key cleanup runs, only **revision** tombstones are kept. The tombstone cleaner may delete the other kinds (attachment and document tombstones).

- RKC2 and RKC3 run the tombstone cleaner as soon as the key cleanup starts. They then check that the **revision** tombstones are exactly as they were on 6.2, and that the total dropped by exactly what that run of the cleaner deleted.
- `/stats` only gives the total, so the tests count each kind from the storage tables (`rkc-common/tombstone_kinds.yml`, reading `Revisions.Tombstones`, `Attachments.Tombstones` and `Collection.Tombstones.*`).
- **First full run on kaiju (2026-11-17):** RKC2 failed on the old check, which required the cleaner to delete nothing. The cleaner deleted 112,635 tombstones. That is exactly the total (212,625) minus the revision-tombstone floor (99,990), so it most likely deleted only non-revision tombstones. The check has been corrected; rerun RKC2 to confirm.

## Where the dataset comes from (`-e dataset_source=...`)

These options apply to RKC2 and RKC3. **S3 is optional.** The default (`seed`) writes the dataset on the machine and needs no AWS keys; only `build` and `restore` use S3.

| Value | What happens |
|---|---|
| `seed` | Write the dataset with `scripts/revisions_loader.py` (the default) |
| `build` | Write the dataset, check it, upload a snapshot to S3, then run the test |
| `restore` | Restore that snapshot from S3 instead of writing it (much faster for RKC3) |
| `existing` | RKC3 only: keep the lab as it is and continue from the first load after the upgrade (no teardown) |

- **Snapshot location:** `s3://<bucket>/ravendb-qa/revision-key-cleanup/<dataset>/<6.2 version>/<shape hash>/`. The dataset folder names (`attachments_tombstones`, `large_dataset`) predate the RKC IDs. They are kept so that existing snapshots are still found.
- **Shape hash:** it covers the dataset settings, so a changed `vars.yml` never restores old data.
- **Only `build` and `restore` need** these environment variables: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` and `RAVENDB_QA_S3_BUCKET`. They are never written to a file.

## Layout

```
RKC1/ ... RKC9/            one test each:
    run.sh                  start here: builds the lab, then runs rkcN.yml
    rkcN.yml                the test steps (its header says what it checks and why)
    vars.yml                the test's settings; any of them can be changed with -e
    *.yml, *.sh             files only this test uses (RKC4 writes, RKC6 restart / RKC9 fill triggers, ...)
run_all.sh                  runs several tests one after another, with a summary
rkc-common/                 shared by the RKC tests
    vars.yml                cleanup log patterns, timeouts, early stop, S3 settings
    run_key_cleanup.yml, wait_cleanup.yml, count_cleanup_lines.yml,
    dataset_points.yml, dataset_restored.yml, wait_parity.yml, ...   shared steps
    remote.sh               runs a test on kaiju (or another host), in tmux
```

## Running on kaiju (the default machine)

`rkc-common/remote.sh` runs a test on kaiju (`kaiju-1@10.0.0.182`) in `tmux` and lets you watch it
from your machine. Run `remote.sh help` to see every command.

```bash
export RKC_SSH_PASS='...'      # optional: else ssh asks for the password once (kept in memory only)
export CERT_DIR=~/selfsignedmaterials   # your lab certs; sync uploads them
scenarios/company-1/rkc-common/remote.sh sync     # upload the working tree to ~/rkc-<you>/rkc-qa and check the machine
scenarios/company-1/rkc-common/remote.sh run RKC8 6.2.20 builds/raven-prNNN.deb
scenarios/company-1/rkc-common/remote.sh status   # / ssh / fetch (into ./rkc-results/)
```

- **Kaiju:** 32 CPUs, 187 GB RAM, 1.3 TB free disk.
- **Its own folder:** everything goes under `~/rkc-<your username>` on kaiju (`RKC_HOME=<name>` for another): the tree, your uploaded certs, collections, dataset snapshots, `results/` (logs, reports, graphs and `index.html` of every `run all`). Kaiju's other clones are left alone.
- **Run `sync` again** after changing anything locally; `run` refuses a `run.sh` that differs from the local one.
- **The lab is torn down first:** `run.sh` removes the Docker lab (1a, 1b, ...) before it starts. `sync` lists the containers that are there, so check that nobody else is using them.
- **Another machine:** `RKC_HOST=<user>@<host> remote.sh ...`.

## Watching the cleanup

The cleanup reports its progress only in the server log:

```bash
docker exec 1a sh -c 'tail -F /var/log/ravendb/logs/* | grep --line-buffered "Revisions key cleanup"'
```

RKC3 also writes progress and an ETA every 15 min to `/tmp/rkc-cleanup-eta-<db>.log`.

**Schema upgrade.** A database created on 6.2 also goes through RavenDB's storage schema
upgrade when it first loads. This is separate from the cleanup, and the database is offline
until it ends. On the laptop it took **16 h** for the old RKC3 dataset, mostly converting its
10M attachment entries.
