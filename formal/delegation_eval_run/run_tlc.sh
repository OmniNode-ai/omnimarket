#!/usr/bin/env bash
# Runs TLC for one config; writes results/<cfg>.out. Usage: ./run_tlc.sh <cfg-basename>
# Fetches the pinned tla2tools.jar (TLC 2.19, release v1.7.4) on first use and verifies its sha256.
set -euo pipefail
cd "$(dirname "$0")"
JAR_SHA=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
JAR_URL=https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar
if [ ! -f tla2tools.jar ]; then curl -fsSL -o tla2tools.jar "$JAR_URL"; fi
echo "$JAR_SHA  tla2tools.jar" | shasum -a 256 -c - >/dev/null
JAVA="${JAVA:-}"
[ -n "$JAVA" ] || { for j in /opt/homebrew/opt/openjdk/bin/java "$(command -v java)"; do "$j" -version >/dev/null 2>&1 && JAVA="$j" && break; done; }
[ -n "$JAVA" ] || { echo "no working java" >&2; exit 1; }
name="$1"; shift
mkdir -p results
meta="$(mktemp -d)"
trap 'rm -rf "$meta"' EXIT
"$JAVA" -XX:+UseParallelGC -Xmx2g -cp tla2tools.jar tlc2.TLC \
  -config "$name.cfg" -workers "${WORKERS:-2}" -metadir "$meta" -cleanup "$@" DelegationEvalRun.tla \
  2>&1 | sed -e "s#$PWD/##g" -e 's#/private/var/folders/[^ ]*/##g' > "results/$name.out" || true
