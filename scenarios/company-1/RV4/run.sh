#!/usr/bin/env bash
# scenarios/company-1/RV4/run.sh -- teardown + 2-node cluster on 6.2 + rv4.yml.
# Usage: run.sh <v62_version> <v_new> [-e foo=bar ...]
#   <v62_version>  6.2 version the lab starts on, e.g. 6.2.20
#   <v_new>        a released version for the destination (e.g. 7.2.7) or a .deb
#
# 6.2 -> 7.2 upgrade of the replication destination only
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1  # CPython 3.12.0-3.12.3 marshal bug

# Pull positionals up to the first -flag; everything from there on is forwarded -e overrides.
POS=()
while [ $# -gt 0 ]; do case "$1" in -*) break;; *) POS+=("$1"); shift;; esac; done
EXTRA=("$@")

V62="${POS[0]:-}"; V_NEW="${POS[1]:-}"
[ -n "$V62" ] && [ -n "$V_NEW" ] || { echo "Usage: $0 <v62_version e.g. 6.2.20> <v_new> [-e foo=bar ...]"; exit 2; }
[[ "$V62" =~ ^6\.2\.[0-9]+$ ]] || { echo "ERROR: v62_version must look like 6.2.N (got '$V62')"; exit 2; }

cd "$(dirname "$0")/../../.."
if [[ "$V_NEW" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  NEW_ARGS=( -e upgrade_version="$V_NEW" )   # released build: downloaded
else
  [[ "$V_NEW" = /* ]] || V_NEW="$PWD/$V_NEW"
  [ -f "$V_NEW" ] || { echo "ERROR: $V_NEW not found"; exit 3; }
  NEW_ARGS=( -e custom_build="$V_NEW" --skip-tags download )
fi

# sudo: -k so a password cached in this terminal doesn't count as passwordless.
if sudo -k -n true 2>/dev/null; then
  export ANSIBLE_BECOME_PASS="${ANSIBLE_BECOME_PASS:-unused}"
else
  if [ -z "${ANSIBLE_BECOME_PASS:-}" ]; then
    IFS= read -rsp "BECOME password: " ANSIBLE_BECOME_PASS; echo
    export ANSIBLE_BECOME_PASS
  fi
  printf '%s\n' "$ANSIBLE_BECOME_PASS" | sudo -S -k -p '' -v 2>/dev/null || { echo "ERROR: sudo rejected the BECOME password"; exit 4; }
fi

C=( -e clusters_count=1 -e nodes_per_cluster=2 )
MEM=(); [ -n "${CONTAINER_MEMORY:-}" ] && MEM=( -e container_memory="$CONTAINER_MEMORY" )   # e.g. CONTAINER_MEMORY=10g run.sh ...

step() { printf '\n\033[1;36m[%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }

echo "RV-4  lab=1x2  from=$V62  to=$V_NEW"

step "teardown";  ansible-playbook playbooks/teardown_containers.yml "${C[@]}"
step "provision"; ansible-playbook playbooks/provision_nodes.yml     "${C[@]}" "${MEM[@]}"
step "install";   ansible-playbook playbooks/install_ravendb.yml     "${C[@]}" -e rdb_version="$V62"
step "form";      ansible-playbook playbooks/form_clusters.yml       "${C[@]}"
step "rv4";      ansible-playbook scenarios/company-1/RV4/rv4.yml \
                      "${NEW_ARGS[@]}" "${EXTRA[@]}"

step "RV-4 done"
