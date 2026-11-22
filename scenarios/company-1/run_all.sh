#!/usr/bin/env bash
# scenarios/company-1/run_all.sh -- run several RKC tests ONE AFTER ANOTHER on this machine, keep
# going after a failure, and print a PASSED / FAILED summary.
#
# Usage: run_all.sh <v62_version> <deb> [options] [-- -e foo=bar ...]
#   <v62_version>   6.2 version every lab starts on, e.g. 6.2.20
#   <deb>           the build under test (.deb), e.g. builds/raven-pr23962.deb
# Options:
#   --tests "A B"   tests to run, in order (default: "RKC1 RKC2 RKC6 RKC9 RKC4 RKC8 RKC5 RKC10 RKC7" -- the
#                   quick correctness tests first, so a basic failure shows in minutes)
#                   RKC4-repro = RKC4 against a RELEASED version (--repro-version): expects the bug
#                   to REPRODUCE (proves the test can catch it); plain RKC4 expects the .deb to FIX it.
#                   RKC3 (300M, ~14 h+) is added LAST with --kaiju; on a laptop list it explicitly.
#   --kaiju         the big machine: RKC7 gets -e d1_profile=kaiju, and RKC3 runs last (default list)
#                   with -e dataset_source=local (its 300M dataset kept as a snapshot on this machine)
#   --repro-version V   released version for RKC4-repro (default 7.2.7)
#   --out DIR       where logs, reports and graphs go (default $RKC_RESULTS/full-run-<date>-<time>;
#                   remote.sh sets RKC_RESULTS=~/$RKC_HOME/results on kaiju; without it /tmp/rkc-all-...)
#   --force         start even if an ansible-playbook is already running here
#   -- ...          everything after -- is passed to every test (e.g. -- -e debug=true)
#
# Each test's run.sh TEARS DOWN the whole Docker lab first -- so the tests cannot run in parallel,
# and nothing else may use the lab meanwhile (the script refuses to start if an ansible-playbook
# is running, unless --force).  CONTAINER_MEMORY, if set, applies to every test (RKC5 and RKC7
# default to 10g on their own).
#
# Output in --out:  <test>.log (full output), <test>/ (that test's report files), summary.txt,
#   graphs/<test>.html -- CPU / memory / disk / IOPS / write latency, drawn right after each test
#   that records them (RKC3, RKC5, RKC7, RKC8, RKC10), passed or failed -- and index.html: the
#   PASSED / FAILED table with a link to each graph, rewritten after every test.
# Exit code: 0 if every test passed, 1 otherwise.
set -uo pipefail

usage() { sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

POS=(); TESTS=""; REPRO=7.2.7; OUT=""; FORCE=0; KAIJU=0; EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --tests) TESTS="$2"; shift 2 ;;
    --repro-version) REPRO="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --kaiju) KAIJU=1; shift ;;
    --) shift; EXTRA=("$@"); break ;;
    -h|--help) usage ;;
    -*) echo "unknown option $1"; usage ;;
    *) POS+=("$1"); shift ;;
  esac
done
V62="${POS[0]:-}"; DEB="${POS[1]:-}"
DEFAULT_TESTS="RKC1 RKC2 RKC6 RKC9 RKC4 RKC8 RKC5 RKC10 RKC7"
[ -n "$TESTS" ] || { TESTS="$DEFAULT_TESTS"; [ "$KAIJU" = 1 ] && TESTS="$TESTS RKC3"; }
[ -n "$V62" ] && [ -n "$DEB" ] || usage
[[ "$V62" =~ ^6\.2\.[0-9]+$ ]] || { echo "ERROR: v62_version must look like 6.2.N (got '$V62')"; exit 2; }

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
cd "$REPO"
[[ "$DEB" = /* ]] || DEB="$PWD/$DEB"
[ -f "$DEB" ] || { echo "ERROR: $DEB not found"; exit 3; }
for t in $TESTS; do
  [ -x "$HERE/${t%-repro}/run.sh" ] || { echo "ERROR: unknown test $t (no ${t%-repro}/run.sh)"; exit 2; }
done
if [ -n "${RKC_RESULTS:-}" ]; then OUT="${OUT:-$RKC_RESULTS/full-run-$(date +%Y%m%d-%H%M%S)}"
else OUT="${OUT:-/tmp/rkc-all-$(date +%Y%m%d-%H%M%S)}"; fi
mkdir -p "$OUT"

# The first teardown would destroy whatever another run is doing.
# Only processes that RUN ansible-playbook ("[python3] .../ansible-playbook ..."), not every
# command line that merely mentions it.
PLAYBOOK_RE='^([^ ]*/)?(python[0-9.]* +([^ ]*/)?)?ansible-playbook( |$)'
if [ "$FORCE" = 0 ] && pgrep -f "$PLAYBOOK_RE" >/dev/null; then
  echo "ERROR: an ansible-playbook is running on this machine -- the first test's teardown would"
  echo "       destroy its lab.  Wait for it, or pass --force:"
  pgrep -af "$PLAYBOOK_RE" | cut -c1-150
  exit 5
fi

# Ask for the sudo password once; every run.sh takes it from ANSIBLE_BECOME_PASS.
if sudo -k -n true 2>/dev/null; then
  export ANSIBLE_BECOME_PASS="${ANSIBLE_BECOME_PASS:-unused}"
elif [ -z "${ANSIBLE_BECOME_PASS:-}" ]; then
  IFS= read -rsp "BECOME password: " ANSIBLE_BECOME_PASS; echo
  export ANSIBLE_BECOME_PASS
  printf '%s\n' "$ANSIBLE_BECOME_PASS" | sudo -S -k -p '' -v 2>/dev/null || { echo "ERROR: sudo rejected the BECOME password"; exit 4; }
fi

# Report files each test leaves behind (copied into $OUT/<test>/ after it).
reports() {
  case "$1" in
    RKC3) echo /tmp/rkc-cleanup-eta-*.log /tmp/rkc-sizes-*.json /tmp/rkc-sizes-*.csv /tmp/rkc-metrics-*.json /tmp/rkc-metrics-*.csv /tmp/rkc-large-*.log ;;
    RKC4|RKC4-repro) echo /tmp/rkc-repro-*.json ;;
    RKC5) echo /tmp/rkc5/* ;;
    RKC7) echo /tmp/rkc7/* ;;
    RKC8) echo /tmp/rkc8-report.json /tmp/rkc8-writer.csv ;;
    RKC9) echo /tmp/rkc9-report.json ;;
    RKC10) echo /tmp/rkc10/* ;;
  esac
}

# Graph of one finished test -> $OUT/graphs/<test>[-<db>].html.  Never fails the run.
PLOT="$REPO/scripts/plot_metrics.py"
graph() {   # <test> <title>
  local t="$1" title="$2" d="$OUT/$1" g="$OUT/graphs" c
  mkdir -p "$g"
  case "${t%-repro}" in
    RKC3) for c in "$d"/rkc-metrics-*.csv; do
            [ -s "$c" ] || continue
            python3 "$PLOT" --csv "$c" --out "$g/$t-$(basename "$c" .csv | sed 's/^rkc-metrics-//').html" --title "$title"
          done ;;
    RKC5|RKC7|RKC10)
          [ -s "$d/metrics.csv" ] && python3 "$PLOT" --dir "$d" --out "$g/$t.html" --title "$title" ;;
    RKC8) [ -s "$d/rkc8-writer.csv" ] && python3 "$PLOT" --probe "$d/rkc8-writer.csv" \
            --report "$d/rkc8-report.json" --out "$g/$t.html" --title "$title -- writes through the rolling upgrade" ;;
  esac >/dev/null 2>"$g/.$t.err" || echo "(graph for $t failed -- see $g/.$t.err)"
  [ -s "$g/.$t.err" ] || rm -f "$g/.$t.err"
  return 0
}

# index.html: the results table so far, with links to the graphs.
index() {   # [still running: <test>]
  local r t links f
  {
    echo '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
    echo '<title>RKC results</title><style>:root{color-scheme:light dark}body{font:14px/1.5 system-ui,sans-serif;max-width:900px;margin:0 auto;padding:20px 16px}'
    echo 'table{border-collapse:collapse;width:100%}td,th{padding:6px 10px;border-bottom:1px solid #8884;text-align:left}.f{color:#c0392b;font-weight:600}.p{color:#1e8449;font-weight:600}</style></head><body>'
    echo "<h1>RKC results</h1><p>6.2 = $V62, build = $(basename "$DEB"), started $(date -d @"$ALL_START" '+%Y-%m-%d %H:%M')$([ -n "${1:-}" ] && echo ", <b>still running: $1</b>")</p>"
    echo '<table><tr><th>Test</th><th>Result</th><th>Time</th><th>Graph</th><th>Log</th></tr>'
    for r in "${RESULTS[@]}"; do
      read -r t _ <<<"$r"
      links=""
      for f in "$OUT"/graphs/"$t".html "$OUT"/graphs/"$t"-*.html; do
        [ -f "$f" ] && links="$links <a href=\"graphs/$(basename "$f")\">$(basename "$f" .html)</a>"
      done
      case "$r" in *PASSED*) cls=p ;; *) cls=f ;; esac
      echo "<tr><td>$t</td><td class=\"$cls\">$(echo "$r" | awk '{$1=""; $NF=""; print}')</td><td>$(echo "$r" | awk '{print $NF}')</td><td>${links:-&ndash;}</td><td><a href=\"$t.log\">log</a></td></tr>"
    done
    echo '</table></body></html>'
  } > "$OUT/index.html"
}

hms() { printf '%dh%02dm%02ds' $(($1 / 3600)) $(($1 % 3600 / 60)) $(($1 % 60)); }
trap 'echo; echo "interrupted -- partial results in $OUT"; exit 130' INT

echo "run_all: $TESTS  (6.2 = $V62, build = $DEB)  -> $OUT"
RESULTS=(); FAILED=0; ALL_START=$(date +%s)
for t in $TESTS; do
  base="${t%-repro}"
  if [ "$t" = RKC4-repro ]; then new="$REPRO"; else new="$DEB"; fi
  printf '\n\033[1;35m===== %s  (%s -> %s)  %s =====\033[0m\n' "$t" "$V62" "$(basename "$new")" "$(date '+%H:%M:%S')"
  start=$(date +%s)
  own=()
  if [ "$KAIJU" = 1 ]; then
    case "$base" in
      RKC7) own=( -e d1_profile=kaiju ) ;;
      RKC3) own=( -e dataset_source=local ) ;;   # restore the local 300M snapshot, or seed + save it
    esac
  fi
  "$HERE/$base/run.sh" "$V62" "$new" "${own[@]}" "${EXTRA[@]}" 2>&1 | tee "$OUT/$t.log"
  rc=${PIPESTATUS[0]}
  secs=$(( $(date +%s) - start ))
  files=$(reports "$t")
  if [ -n "$files" ]; then
    mkdir -p "$OUT/$t"
    # shellcheck disable=SC2086  # globs on purpose
    cp -r $files "$OUT/$t/" 2>/dev/null || true
  fi
  if [ "$rc" = 0 ]; then verdict=PASSED; else verdict="FAILED (exit $rc)"; FAILED=$((FAILED + 1)); fi
  RESULTS+=("$(printf '%-12s %-18s %s' "$t" "$verdict" "$(hms $secs)")")
  graph "$t" "$t  $V62 -> $(basename "$new")  ${verdict%% *}"
  index "$(echo $TESTS | tr ' ' '\n' | grep -A1 -x "$t" | sed -n 2p)"
done
index

{
  echo "run_all  6.2 = $V62  build = $(basename "$DEB")  $(date '+%Y-%m-%d %H:%M')  total $(hms $(( $(date +%s) - ALL_START )))"
  printf '%s\n' "${RESULTS[@]}"
  echo "$FAILED of $(echo $TESTS | wc -w) failed.  Logs and reports: $OUT  (graphs: $OUT/index.html)"
} | tee "$OUT/summary.txt"
[ "$FAILED" = 0 ]
