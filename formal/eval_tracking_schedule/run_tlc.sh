#!/usr/bin/env bash
# Runs TLC for one config; writes results/<cfg>.out ending in exit=<code>. Usage: ./run_tlc.sh <cfg-basename>
# Needs tla2tools.jar (TLC 2.19, release v1.7.4, sha256 below) beside this script and a JRE, or docker
# with the eclipse-temurin:17-jre-alpine image.
set -uo pipefail
cd "$(dirname "$0")"
JAR_SHA=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
echo "$JAR_SHA  tla2tools.jar" | sha256sum -c - >/dev/null || { echo "tla2tools.jar missing or not the pinned one" >&2; exit 1; }
name="$1"; shift
mkdir -p results
if command -v java >/dev/null; then RUN=(java); else
  RUN=(docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" -w /work eclipse-temurin:17-jre-alpine java); fi
"${RUN[@]}" -XX:+UseParallelGC -Xmx2g -cp tla2tools.jar tlc2.TLC \
  -config "$name.cfg" -workers "${WORKERS:-2}" -metadir "states/$name" -cleanup "$@" EvalTrackingSchedule.tla \
  > "results/$name.out" 2>&1
echo "exit=$?" >> "results/$name.out"
rm -rf "states/$name"
