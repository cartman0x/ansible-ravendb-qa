#!/bin/sh
# scenarios/company-1/RKC9/fill_trigger.sh -- runs INSIDE the container (copied there by rkc9.yml).
# Waits until the key cleanup of <db> has logged <after_lines> progress lines, then fills the
# filesystem at <mount> with one file (fallocate) until only <reserve_mb> MB are left.
# Prints one line:
#   FILLED avail_before=<bytes> filler=<bytes> free_after=<bytes> at=<UTC time>
#   MISSED        the cleanup was done before the trigger fired
#   FILL_FAILED   fallocate failed
#   TIMEOUT
# Usage: fill_trigger.sh <db> <log_dir> <mount> <after_lines> <reserve_mb> <timeout_secs>
DB=$1; L=$2; M=$3; AFTER=$4; RES=$5; T=$6
end=$(( $(date +%s) + T ))
count() { grep -rhF -- "$1" "$L" 2>/dev/null | grep -F "|$DB|" | wc -l; }
while [ "$(date +%s)" -lt "$end" ]; do
  if [ "$(count 'Revisions key cleanup is done')" -gt 0 ]; then echo "MISSED"; exit 0; fi
  if [ "$(count 'Revisions key cleanup progress')" -ge "$AFTER" ]; then
    avail=$(df --output=avail -B1 "$M" | tail -1)
    size=$(( avail - RES * 1024 * 1024 ))
    if ! fallocate -l "$size" "$M/filler"; then echo "FILL_FAILED"; exit 0; fi
    echo "FILLED avail_before=$avail filler=$size free_after=$(df --output=avail -B1 "$M" | tail -1) at=$(date -u '+%Y-%m-%dT%H:%M:%S')"
    exit 0
  fi
  sleep 0.2
done
echo "TIMEOUT"
