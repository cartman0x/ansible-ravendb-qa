#!/usr/bin/env bash
# scenarios/company-1/RKC3/run.sh -- teardown + 1-node lab on 6.2 + rkc3.yml.
# Usage: run.sh <v62_version> <v_new> [-e foo=bar ...]
#   <v62_version>  6.2 version the lab starts on, e.g. 6.2.20
#   <v_new>        the .deb under test, e.g. builds/raven-pr12345.deb
#
# dataset 2: 300M documents + revisions (run it on kaiju: ../rkc-common/remote.sh)
# With -e dataset_source=existing the lab is left as it is (no teardown/provision/install/
# form): the test continues on the node and db already there.
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
[[ "$V_NEW" = /* ]] || V_NEW="$PWD/$V_NEW"
[ -f "$V_NEW" ] || { echo "ERROR: $V_NEW not found"; exit 3; }
NEW_ARGS=( -e custom_build="$V_NEW" --skip-tags download )

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

C=( -e clusters_count=1 -e nodes_per_cluster=1 )
MEM=(); [ -n "${CONTAINER_MEMORY:-}" ] && MEM=( -e container_memory="$CONTAINER_MEMORY" )   # e.g. CONTAINER_MEMORY=10g run.sh ...

step() { printf '\n\033[1;36m[%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }

echo "RKC-3  lab=1x1  from=$V62  to=$V_NEW"

if [[ " ${EXTRA[*]} " == *"dataset_source=existing"* ]]; then
  step "existing lab -- no teardown / provision / install / form"
  docker inspect -f '{{.State.Running}}' 1a 2>/dev/null | grep -qx true \
    || { echo "ERROR: dataset_source=existing needs the lab's container 1a running"; exit 5; }
else
  step "teardown";  ansible-playbook playbooks/teardown_containers.yml "${C[@]}"
  step "provision"; ansible-playbook playbooks/provision_nodes.yml     "${C[@]}" "${MEM[@]}"
  step "install";   ansible-playbook playbooks/install_ravendb.yml     "${C[@]}" -e rdb_version="$V62"
  step "form";      ansible-playbook playbooks/form_clusters.yml       "${C[@]}"
fi
step "rkc3";      ansible-playbook scenarios/company-1/RKC3/rkc3.yml \
                      "${NEW_ARGS[@]}" -e rkc_v62_version="$V62" "${EXTRA[@]}"

step "RKC-3 done"
