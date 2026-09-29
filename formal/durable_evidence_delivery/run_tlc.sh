#!/usr/bin/env bash
# Run one OR.3 model configuration.  TLA2TOOLS_JAR is intentionally explicit:
# this preparatory branch never downloads a verifier or selects a runtime.
set -euo pipefail
cd "$(dirname "$0")"

name="${1:?usage: ./run_tlc.sh <config-basename>}"
jar="${TLA2TOOLS_JAR:?set TLA2TOOLS_JAR to a checked tla2tools.jar}"
java_bin="${JAVA:-$(command -v java)}"

test -f "$jar"
"$java_bin" -version >/dev/null 2>&1
mkdir -p results
meta="$(mktemp -d)"
trap 'rm -rf "$meta"' EXIT
set +e
"$java_bin" -XX:+UseParallelGC -Xmx2g -cp "$jar" tlc2.TLC \
    -config "$name.cfg" -workers "${WORKERS:-2}" -metadir "$meta" -cleanup \
    DurableEvidenceDelivery.tla 2>&1 | \
    sed -e "s#$PWD/##g" -e 's#/private/var/folders/[^ ]*/##g' \
    > "results/$name.out"
status=${PIPESTATUS[0]}
set -e
exit "$status"
