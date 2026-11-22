#!/bin/sh
# scenarios/company-1/RKC6/restart_trigger.sh -- runs INSIDE the node's container (rkc6.yml copies
# it in and starts it; the cleanup may already be running -- it counts lines since startup).
#
# For each restart point, in order:
#   1. wait until the key cleanup has logged <after> progress lines of the wanted phase since the
#      server last came up (documents = steps Revisions/RevisionAttachments/
#      RevisionAttachmentTombstones, tombstones = step RevisionsTombstones);
#      "is done" first -> MISSED (the cleanup was too short for this point; stop);
#   2. write a marker document -- it raises the db's last etag, so a cleanup that STARTED OVER
#      would log a higher LastRawEtag than before (scripts/cleanup_resume.py checks it);
#   3. restart RavenDB: restart = systemctl restart (graceful), kill = SIGKILL (systemd restarts it);
#   4. wait for HTTPS, count the cleanup lines (old process done logging), then wait for the db
#      to come online -- the server loads it at startup and the cleanup continues once loaded.
# Output, one line per point:
#   POINT n=<i> name=<name> outcome=RESTARTED how=<how> c_restart=<lines> c_up=<lines> marker=<http> restart_at=.. up_at=..
#   POINT n=<i> name=<name> outcome=MISSED|TIMEOUT|LOAD_TIMEOUT
# c_restart / c_up are counts of the db's cleanup lines (sorted), the split points for
# cleanup_resume.py -- counts, not timestamps, so the log's time zone does not matter.
#
# Usage: restart_trigger.sh <db> <log_dir> <timeout_secs> <name:phase:after:how> ...

set -u
DB=$1; LOGS=$2; DEADLINE=$(( $(date +%s) + $3 )); shift 3

URL=https://localhost:443
curl_() { curl -sS -k --max-time 30 --cert /etc/ravendb/security/server.pem \
               --key /etc/ravendb/security/server.key "$@"; }
# Every cleanup line of this db, in time order.  -i: the error line says "revisions key cleanup".
lines() { grep -rhiF -- "revisions key cleanup" "$LOGS" 2>/dev/null | grep -F "|$DB|" | LC_ALL=C sort; }
now() { date -u "+%Y-%m-%dT%H:%M:%S.%N" | cut -c1-23; }   # no space: output is key=value
late() { [ "$(date +%s)" -ge "$DEADLINE" ]; }

skip=0          # lines logged before the server last came up
n=0
for point in "$@"; do
  n=$((n + 1))
  name=$(echo "$point" | cut -d: -f1); phase=$(echo "$point" | cut -d: -f2)
  after=$(echo "$point" | cut -d: -f3); how=$(echo "$point" | cut -d: -f4)
  case $phase in
    documents)  re='Revisions key cleanup progress: step (Revisions|RevisionAttachments|RevisionAttachmentTombstones),' ;;
    tombstones) re='Revisions key cleanup progress: step RevisionsTombstones,' ;;
    *) echo "POINT n=$n name=$name outcome=BAD_PHASE"; exit 0 ;;
  esac

  # 1. wait for the phase
  while :; do
    if late; then echo "POINT n=$n name=$name outcome=TIMEOUT"; exit 0; fi
    new=$(lines | tail -n +$((skip + 1)))
    if echo "$new" | grep -qF "Revisions key cleanup is done"; then
      echo "POINT n=$n name=$name outcome=MISSED"; exit 0
    fi
    [ "$(echo "$new" | grep -cE "$re")" -ge "$after" ] && break
    sleep 0.1
  done

  # 2. marker
  marker=$(curl_ -o /dev/null -w '%{http_code}' -X PUT -H 'Content-Type: application/json' \
             -d "{\"restart\": $n, \"@metadata\": {\"@collection\": \"Markers\"}}" \
             "$URL/databases/$DB/docs?id=rkc6-marker/$n") || marker=ERR

  # 3. restart
  c_restart=$(lines | wc -l)
  restart_at=$(now)
  case $how in
    kill)    pkill -9 -f "[R]aven.Server" ;;
    *)       systemctl restart ravendb ;;
  esac

  # 4. back up, then wait for the db to come online (the server loads it at startup)
  waited=0
  until curl_ -o /dev/null "$URL/build/version" 2>/dev/null; do
    if late; then echo "POINT n=$n name=$name outcome=TIMEOUT"; exit 0; fi
    sleep 0.5; waited=$((waited + 1))
    # SIGKILL: systemd restarts it (Restart=on-failure); start it by hand if it did not in 60 s
    [ "$waited" -eq 120 ] && [ "$how" = kill ] && systemctl start ravendb
  done
  up_at=$(now)
  skip=$(lines | wc -l)   # before the db is loaded: every line so far is the old process's
  echo "POINT n=$n name=$name outcome=RESTARTED how=$how c_restart=$c_restart c_up=$skip marker=$marker restart_at=$restart_at up_at=$up_at"

  until [ "$(curl_ -o /dev/null -w '%{http_code}' "$URL/databases/$DB/stats" 2>/dev/null)" = 200 ]; do
    if late; then echo "POINT n=$n name=$name outcome=LOAD_TIMEOUT"; exit 0; fi
    sleep 1
  done
done
echo "ALL_DONE"
