#!/usr/bin/env bash
# scenarios/company-1/rkc-common/remote.sh -- run an RKC test on the lab machine (KAIJU by default),
# in tmux, and watch it from here.  Any Linux machine with Docker, Ansible and the lab certs works.
#
#   export RKC_SSH_PASS='...'           # optional: the ssh password (else ssh asks once); it stays in
#                                       # memory -- an askpass helper reads it from the environment
#   Everything goes under ONE folder there: ~/rkc-<your username here> (RKC_HOME=<name> for another).
#   remote.sh sync                      # upload the working tree to <that folder>/rkc-qa -- NOT the machine's
#                                       # own clone; inventory/group_vars/all.yml comes from git HEAD (the
#                                       # machine's cert_dir), plus this machine's lab certs to <that folder>/rkc-qa-certs
#                                       # (RKC_HOST_CERTS=1: use the host's own); then checks docker / tmux /
#                                       # ansible / certs (not expired)
#   remote.sh run RKC8 6.2.20 builds/raven-pr23962.deb [-e ...]   # starts <test>/run.sh in tmux "rkc"
#   remote.sh run all 6.2.20 builds/raven-pr23962.deb --kaiju     # every test (run_all.sh)
#   remote.sh status                    # last lines of the run + key-cleanup progress + memory/disk
#   remote.sh ssh                       # shell there (tmux attach -t rkc to watch the run)
#   remote.sh fetch                     # copy run log, results, /tmp/rkc-*, RavenDB logs to ./rkc-results/<host>-<time>/
#   Another machine: RKC_HOST=<user>@<host> remote.sh ...   (default kaiju-1@10.0.0.182)
#   run.sh TEARS DOWN the Docker lab (1a, 1b, ...) first -- make sure nobody else is using it.
#   sudo: if it needs a password, run asks for it (or takes ANSIBLE_BECOME_PASS), stdin only.
#   S3 snapshots (-e dataset_source=build|restore): AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
#   AWS_REGION and RAVENDB_QA_S3_BUCKET in this shell are passed to the run (stdin only).
set -euo pipefail
# Never stop silently: name the command that failed (a remote step's own output is above it).
trap 'rc=$?; echo "ERROR: remote.sh stopped -- \"$BASH_COMMAND\" failed (exit $rc, line $LINENO)" >&2' ERR
export PYTHONDONTWRITEBYTECODE=1

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
HERE="$(cd "$(dirname "$0")" && pwd)"; HERE="${HERE#"$REPO"/}"   # this folder, relative to the repo
TESTS="${HERE%/*}"                                             # scenarios/company-1
RKC_HOST="${RKC_HOST:-kaiju-1@10.0.0.182}"; ME="$0"
# Everything this script puts on the host lives under one folder W (left unquoted in remote
# commands, so ~ expands there):
#   rkc-qa/ (the tree), rkc-qa-certs/, rkc-qa-collections/, rkc-snapshots/, results/, rkc-run.log, logs/, rkc-fetch/
WREL="${RKC_HOME:-rkc-${USER:-$(id -un)}}"; W="~/$WREL"; RDIR="$W/rkc-qa"
# This machine's lab certs: CERT_DIR (as for the playbooks), else the inventory's default path
# (the line is "{{ lookup('env', 'CERT_DIR') or '<path>' }}", or a plain path).
CERT_DIR_LOCAL="${CERT_DIR_LOCAL:-${CERT_DIR:-$(sed -n 's/^cert_dir: *//p' "$REPO/inventory/group_vars/all.yml" \
  | sed -E "s/.* or '([^']+)'.*/\1/; s/^\"(.*)\"$/\1/")}}"

die() { echo "ERROR: $*" >&2; exit 1; }
say() { printf '\033[1;36m# %s\033[0m\n' "$*"; }

# ssh: password (RKC_SSH_PASS) or the user's own keys; one connection reused for 15 min.
SSH_ID=()
ssh_setup() {
  mkdir -p "$HOME/.ssh"
  SSH_ID=( -o ControlMaster=auto -o "ControlPath=$HOME/.ssh/rkc-cm-%C" -o ControlPersist=15m )
  if [ -n "${RKC_SSH_PASS:-}" ]; then     # the helper only echoes the variable: no secret in the file
    ASKPASS="$(mktemp)"; printf '#!/bin/sh\nprintf "%%s\\n" "$RKC_SSH_PASS"\n' > "$ASKPASS"; chmod 700 "$ASKPASS"
    trap 'rm -f "$ASKPASS"' EXIT
    export SSH_ASKPASS="$ASKPASS" SSH_ASKPASS_REQUIRE=force DISPLAY="${DISPLAY:-:0}" RKC_SSH_PASS
  fi
}
ssh_i() { ssh "${SSH_ID[@]}" -o ConnectTimeout=15 -o ServerAliveInterval=60 -o ServerAliveCountMax=15 "$RKC_HOST" "$@"; }

# Upload the working tree, then check the machine has what run.sh needs.
cmd_sync() {
  # sync replaces $RDIR -- never under a running test
  ssh_i "pgrep -x ansible-playboo >/dev/null || pgrep -f '^bash [^-].*run(_all)?\\.sh' >/dev/null" \
    && die "a test is running on $RKC_HOST -- sync would replace its files.  Wait for it ('$ME status')."
  say "upload the working tree to $RKC_HOST:$RDIR (incl. builds/; all.yml from git HEAD)"
  ssh_i "mkdir -p $W && rm -rf $RDIR && mkdir -p $RDIR"
  tar -C "$REPO" --exclude=.git --exclude='__pycache__' --exclude='.pytest_cache' --exclude=rkc-results \
      --exclude=./inventory/group_vars/all.yml -czf - . | ssh_i "tar -C $RDIR -xzf -"
  git -C "$REPO" show HEAD:inventory/group_vars/all.yml | ssh_i "cat > $RDIR/inventory/group_vars/all.yml"
  # The lab certs: this machine's cert_dir goes to $W/rkc-qa-certs (mode 700) and the uploaded tree
  # points at it -- the host's own cert folder (shared with others) is not touched.
  # RKC_HOST_CERTS=1 keeps the host's cert_dir instead.
  if [ -z "${RKC_HOST_CERTS:-}" ]; then
    [ -d "$CERT_DIR_LOCAL" ] || die "cert dir $CERT_DIR_LOCAL not found (export CERT_DIR=<your lab cert folder>)"
    say "upload the lab certs ($CERT_DIR_LOCAL) to $RKC_HOST:$W/rkc-qa-certs"
    tar -C "$CERT_DIR_LOCAL" --exclude='*:Zone.Identifier' --exclude='*.expired-*' -czf - . \
      | ssh_i "rm -rf $W/rkc-qa-certs && mkdir -m 700 $W/rkc-qa-certs && tar -C $W/rkc-qa-certs -xzf -"
    ssh_i "sed -i \"s|^cert_dir: .*|cert_dir: \$HOME/$WREL/rkc-qa-certs|\" $RDIR/inventory/group_vars/all.yml"
  fi
  # The collection versions this repo needs (requirements.yml), in a folder of their own: the host's
  # shared collections (kaiju: community.docker 3.7, too old) are left as they are.  run uses it.
  say "collections from requirements.yml -> $RKC_HOST:$W/rkc-qa-collections"
  ssh_i "cd $RDIR && ansible-galaxy collection install -r requirements.yml -p $W/rkc-qa-collections >/dev/null \
         && ansible-galaxy collection install 'ravendb.ravendb==1.1.0' -p $W/rkc-qa-collections --force >/dev/null \
         && ANSIBLE_COLLECTIONS_PATH=$W/rkc-qa-collections ansible-galaxy collection list -p $W/rkc-qa-collections 2>/dev/null \
            | grep -E '^(community|ravendb|ansible)\.'"
  say "what run.sh needs on $RKC_HOST"
  ssh_i "cd $RDIR && W=$W bash -s" <<'EOF'
ok=1
chk() { if eval "$2" >/dev/null 2>&1; then echo "  ok    $1"; else echo "  MISSING  $1"; ok=0; fi; }
cert_dir=$(sed -n 's/^cert_dir: *//p' inventory/group_vars/all.yml | sed -E "s/.* or '([^']+)'.*/\1/; s/^\"(.*)\"$/\1/")
chk "docker (without sudo)"         "docker ps"
chk "tmux"                          "command -v tmux"
chk "ansible-playbook: $(ansible-playbook --version 2>/dev/null | head -1)" "command -v ansible-playbook"
chk "python3 + venv"                "python3 -m venv --help"
chk "cert_dir $cert_dir"            "test -f $cert_dir/server.pfx -a -f $cert_dir/ca.crt"
chk "server certificate not expired" "openssl pkcs12 -in $cert_dir/server.pfx -nokeys -passin pass: 2>/dev/null | openssl x509 -noout -checkend 86400"
chk "license.json in cert_dir"      "test -f $cert_dir/license.json"
export ANSIBLE_COLLECTIONS_PATH=$W/rkc-qa-collections:$HOME/.ansible/collections:/usr/share/ansible/collections
chk "collection ravendb.ravendb"    "ansible-galaxy collection list ravendb.ravendb | grep -q ravendb"
chk "collection community.docker 4.x" "ansible-galaxy collection list community.docker | grep -E 'community.docker +4\.'"
sudo -n true >/dev/null 2>&1 && echo "  ok    passwordless sudo" || echo "  note  sudo needs a password -- run asks for it"
echo "  ----  $(nproc) CPUs, $(free -g | awk '/^Mem:/{print $2}') GB RAM, docker disk free: $(df -h --output=avail "$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /)" | tail -1)"
echo "  ----  docker containers now (run.sh removes the lab's): $(docker ps -a --format '{{.Names}}' 2>/dev/null | tr '\n' ' ')"
[ $ok = 1 ] && echo "ready" || echo "NOT ready -- fix the MISSING lines"
EOF
}

cmd_run() {
  [ $# -ge 3 ] || die "usage: $ME run <test e.g. RKC3 | all> <v62_version> <v_new_deb relative to the repo> [-e ...]"
  local test="${1%/}"; shift
  local script="$test/run.sh"
  # "all": run_all.sh (every RKC test, one after another); options such as --kaiju / --tests go
  # after the two versions.
  if [ "$test" = all ]; then script=run_all.sh; fi
  [ -x "$REPO/$TESTS/$script" ] || die "no test '$test' -- choose one of: $(cd "$REPO/$TESTS" && ls -d */run.sh | cut -d/ -f1 | tr '\n' ' ')"
  local here_sum there_sum
  here_sum=$(sha1sum < "$REPO/$TESTS/$script" | cut -c1-40)
  there_sum=$(ssh_i "sha1sum < $RDIR/$TESTS/$script 2>/dev/null | cut -c1-40" || true)
  [ "$here_sum" = "$there_sum" ] \
    || die "$RKC_HOST has a different $script than this machine (here ${here_sum:0:12}, there ${there_sum:-missing}) -- run '$ME sync' first"
  if ssh_i 'tmux has-session -t rkc 2>/dev/null'; then
    # ansible-playbook by process NAME (cut to 15 chars); the test scripts run as "bash <path>/run.sh"
    # (or run_all.sh) -- matched from the start of the command line, so the tmux server's and the
    # "bash -c ..." wrapper's command lines (which also mention run.sh) do not count.
    ssh_i "pgrep -x ansible-playboo >/dev/null || pgrep -f '^bash [^-].*run(_all)?\\.sh' >/dev/null" \
      && die "a run is still going (tmux session rkc) -- '$ME status', or '$ME ssh' then: tmux attach -t rkc"
    say "removing the finished run's tmux session (its log is kept as $W/logs/rkc-run.<time>.log)"
    ssh_i 'tmux kill-session -t rkc'
  fi
  local mem_mb mem
  mem_mb=$(ssh_i "free -m | awk '/^Mem:/{print \$2}'")
  mem="${CONTAINER_MEMORY:-$((mem_mb * 90 / 100 / 1024))g}"
  local args; args=$(printf '%q ' "$@")
  local become=unused
  if ! ssh_i 'sudo -n true' 2>/dev/null; then
    [ -n "${ANSIBLE_BECOME_PASS:-}" ] || { IFS= read -rsp "sudo password on $RKC_HOST: " ANSIBLE_BECOME_PASS; echo; }
    become=$(printf '%q' "$ANSIBLE_BECOME_PASS")
  fi
  say "starting $script $* on $RKC_HOST (CONTAINER_MEMORY=$mem) in tmux session rkc"
  # Secrets travel in this script's stdin only.  The tmux server started here inherits the
  # environment, so run.sh sees them; nothing is written to disk or put on a command line.
  # The AWS variables are for S3 snapshots only (dataset_source=build|restore).
  ssh_i 'bash -s' <<EOF
set -euo pipefail
${AWS_ACCESS_KEY_ID:+export AWS_ACCESS_KEY_ID='$AWS_ACCESS_KEY_ID'}
${AWS_SECRET_ACCESS_KEY:+export AWS_SECRET_ACCESS_KEY='$AWS_SECRET_ACCESS_KEY'}
${AWS_SESSION_TOKEN:+export AWS_SESSION_TOKEN='$AWS_SESSION_TOKEN'}
${AWS_REGION:+export AWS_REGION='$AWS_REGION'}
${RAVENDB_QA_S3_BUCKET:+export RAVENDB_QA_S3_BUCKET='$RAVENDB_QA_S3_BUCKET'}
export ANSIBLE_BECOME_PASS=$become   # the sudo password, if the host needs one
export CONTAINER_MEMORY='$mem'
export ANSIBLE_COLLECTIONS_PATH=$W/rkc-qa-collections:\$HOME/.ansible/collections:/usr/share/ansible/collections
export RKC_SNAPSHOTS=$W/rkc-snapshots RKC_RESULTS=$W/results
cd $RDIR
mkdir -p $W/logs
[ -f $W/rkc-run.log ] && mv $W/rkc-run.log $W/logs/rkc-run.\$(date +%Y%m%d-%H%M%S).log
echo "=== \$(date -u '+%Y-%m-%d %H:%M UTC')  $script $args" > $W/rkc-run.log
tmux new-session -d -s rkc "set -o pipefail; $TESTS/$script $args 2>&1 | tee -a $W/rkc-run.log; echo; echo 'run.sh exited -- $ME fetch'; exec bash"
EOF
  echo "watch: $ME status   |   $ME ssh  then  tmux attach -t rkc  (detach: Ctrl-b d)"
}

cmd_status() {
  ssh_i "W=$W bash -s" <<'EOF'
echo "== run: $(head -1 $W/rkc-run.log 2>/dev/null | cut -c1-200)"
tmux has-session -t rkc 2>/dev/null && { { pgrep -x ansible-playboo || pgrep -f '^bash [^-].*run(_all)?\.sh'; } >/dev/null && echo "   (running)" || echo "   (finished)"; } || echo "   (no run started)"
echo "== last lines"; tail -n 8 $W/rkc-run.log 2>/dev/null | cut -c1-220
echo "== key cleanup (last progress line)"
docker exec 1a sh -c 'grep -h "Revisions key cleanup" /var/log/ravendb/logs/*.log 2>/dev/null | tail -1 | cut -c1-250' 2>/dev/null || echo "(container 1a not up)"
echo "== key cleanup ETA (every 15 min)"; tail -n 1 /tmp/rkc-cleanup-eta-*.log 2>/dev/null || echo "(no ETA yet)"
echo "== machine"; uptime; free -g | sed -n 1,2p; df -h / /var/lib/docker | sed 1d
docker stats --no-stream --format '1a: {{.MemUsage}}  cpu {{.CPUPerc}}' 1a 2>/dev/null || true
EOF
}

cmd_ssh() { ssh_i -t "$@"; }

cmd_fetch() {
  local out="$REPO/rkc-results/${RKC_HOST#*@}-$(date +%Y%m%d-%H%M%S)"; mkdir -p "$out"
  say "fetching into $out"
  ssh_i "W=$W bash -s" <<'EOF'
rm -rf $W/rkc-fetch && mkdir -p $W/rkc-fetch
cp $W/rkc-run.log /tmp/rkc-*.log /tmp/rkc-*.json /tmp/rkc-*.csv $W/rkc-fetch/ 2>/dev/null || true
# RKC5 / RKC7 / RKC10 write into their own folders (rkc5_out_dir, d1_out_dir): metrics.csv, probe.csv, report
cp -r /tmp/rkc5 /tmp/rkc7 /tmp/rkc10 /tmp/rkc-all-* $W/rkc-fetch/ 2>/dev/null || true
# run_all results (logs, reports, graphs/, index.html): kept in $W/results
cp -r $W/results $W/rkc-fetch/ 2>/dev/null || true
cp -r ~/.cache/ravendb-qa/revision-key-cleanup $W/rkc-fetch/baselines 2>/dev/null || true
docker cp 1a:/var/log/ravendb/logs $W/rkc-fetch/ravendb-logs 2>/dev/null || true
EOF
  ssh_i "tar -C $W/rkc-fetch -czf - ." | tar -C "$out" -xzf -
  # Charts (CPU / memory / disk I/O / write latency) of whatever was fetched -- also mid-run: they
  # then end at the last sample, and the cleanup is shaded only once the test saved its report.
  local d c
  for d in "$out"/rkc5 "$out"/rkc7 "$out"/rkc10; do
    [ -s "$d/metrics.csv" ] && python3 "$REPO/scripts/plot_metrics.py" --dir "$d" || true
  done
  for c in "$out"/rkc-metrics-*.csv; do
    [ -s "$c" ] && python3 "$REPO/scripts/plot_metrics.py" --csv "$c" || true
  done
  ls -la "$out"
}

case "${1:-}" in
  sync|run|status|ssh|fetch) c=$1; shift; ssh_setup; "cmd_$c" "$@";;
  *) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 2;;
esac
