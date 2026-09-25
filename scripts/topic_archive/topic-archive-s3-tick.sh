#!/bin/bash
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
#
# topic-archive-s3-tick.sh -- the operator Mac's scheduled bridge that keeps
# topic archives flowing to the encrypted S3 archive until
# node_topic_archive_effect runs as a deployed node. Runs under launchd
# (ai.omninode.topic-archive-s3, every 6 hours) because the AWS SSO session
# lives on this Mac.
#
# Each tick:
#   1. stages: runs lab-archive-to-staging.sh on the lab host over ssh, which
#      archives the contract's source topics (yesterday and today, UTC; every
#      retained day with TOPIC_ARCHIVE_ALL_DAYS=1) into a new owner-only
#      directory there. This happens even when the AWS session has expired, so
#      records are saved before retention deletes them.
#   2. uploads: for every staged run on the lab host with a STAGED_OK marker
#      and no local "uploaded" marker, copies it here over ssh, then runs the
#      archive node with the staged run as its source and the S3 sink with KMS
#      envelope encryption as its sink. The node writes each new day with
#      SSE-KMS, head-checks it, downloads and decrypts it and compares it with
#      the staged records; days already covered in S3 are skipped. Only a
#      verified run is marked uploaded, and only then is the local plaintext
#      copy removed. Nothing on the lab host is deleted.
#
# AWS SSO sessions expire. When `aws sts get-caller-identity` fails, step 2 is
# skipped, the tick exits 3 and says so in its log; staging continues every
# tick, and the next tick after `aws sso login` uploads the whole backlog.
#
# Usage:
#   topic-archive-s3-tick.sh                  one tick (what launchd runs)
#   topic-archive-s3-tick.sh --install SHA    snapshot omnimarket at SHA into the
#                                             runtime dir, build its venv, render and
#                                             (re)load the LaunchAgent
#
# Required environment: OMNI_HOME (registry root), TOPIC_ARCHIVE_LAB_HOST
# (ssh destination of the lab host). Never creates or rotates a credential or
# key, never changes a bucket or key policy, never changes broker retention.

set -euo pipefail
umask 077

OMNI_HOME="${OMNI_HOME:?OMNI_HOME must be set to the registry root}"
LAB_HOST="${TOPIC_ARCHIVE_LAB_HOST:?TOPIC_ARCHIVE_LAB_HOST must name the lab host ssh destination}"
ROOT="${OMNI_HOME}/.onex_state/topic-archive-s3"
SRC="${ROOT}/src"
STAGE="${ROOT}/staging"
REPORTS="${ROOT}/reports"
DONE="${ROOT}/uploaded"
LOGS="${ROOT}/logs"
REMOTE_ROOT="${TOPIC_ARCHIVE_REMOTE_ROOT:-/data/omninode/hook-archive}"
S3_PREFIX="${TOPIC_ARCHIVE_S3_PREFIX:-topic-archive/}"
KMS_ALIAS="${TOPIC_ARCHIVE_KMS_ALIAS:-alias/omninode-claude-session-archive}"
BUCKET_PREFIX="omninode-claude-session-archive-"
LABEL="ai.omninode.topic-archive-s3"
BREW_PYTHON="/opt/homebrew/bin/python3.13"
export AWS_PROFILE="${AWS_PROFILE:-default}"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=30)

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

install_runtime() {
  local sha="${1:?--install needs a commit sha}"
  local repo="${OMNI_HOME}/omnimarket"
  local rel="${ROOT}/releases/${sha}"
  mkdir -p "${ROOT}/releases" "${STAGE}" "${REPORTS}" "${DONE}" "${LOGS}"
  git -C "${repo}" fetch --quiet origin "${sha}"
  if [[ ! -f "${rel}/.source-sha" ]]; then
    # Built in its final directory: the venv records absolute paths, so it
    # cannot be moved after `uv sync`.
    rm -rf "${rel}"
    mkdir -p "${rel}"
    git -C "${repo}" archive "${sha}" | tar -x -C "${rel}"
    (cd "${rel}" && uv sync --frozen --quiet --python "${BREW_PYTHON}")
    printf '%s\n' "${sha}" >"${rel}/.source-sha"
  fi
  if [[ -d "${SRC}" && ! -L "${SRC}" ]]; then
    echo "REFUSED: ${SRC} is a directory, expected the release symlink" >&2
    exit 2
  fi
  ln -sfn "releases/${sha}" "${SRC}"
  local plist="${HOME}/Library/LaunchAgents/${LABEL}.plist"
  sed -e "s#__OMNI_HOME__#${OMNI_HOME}#g" -e "s#__HOME__#${HOME}#g" \
    -e "s#__LAB_HOST__#${LAB_HOST}#g" \
    "${SRC}/scripts/topic_archive/${LABEL}.plist.template" >"${plist}"
  plutil -lint "${plist}"
  launchctl bootout "gui/$(id -u)/${LABEL}" || true
  launchctl bootstrap "gui/$(id -u)" "${plist}"
  echo "installed ${LABEL} at ${sha}"
}

if [[ "${1:-}" == "--install" ]]; then
  install_runtime "${2:-}"
  exit 0
fi

SHA="$(cat "${SRC}/.source-sha")"
LOCK="${ROOT}/.lock"
if ! mkdir "${LOCK}"; then
  holder="$(cat "${LOCK}/pid" || echo unknown)"
  if [[ "${holder}" != unknown ]] && kill -0 "${holder}"; then
    echo "SKIP $(ts): tick pid ${holder} is still running"
    exit 0
  fi
  echo "stale lock from pid ${holder}, taking it over"
fi
echo $$ >"${LOCK}/pid"
trap 'rm -rf "${LOCK}"' EXIT

echo "=== topic-archive-s3 tick $(ts) at omnimarket ${SHA} ==="

# 1. stage on the lab host
RUN="sched-$(date -u +%Y%m%dT%H%M%SZ)"
DAY_ARGS=()
if [[ "${TOPIC_ARCHIVE_ALL_DAYS:-0}" != 1 ]]; then
  DAY_ARGS=(--day "$(date -u -v-1d +%Y-%m-%d)" --day "$(date -u +%Y-%m-%d)")
fi
stage_rc=0
"${SSH[@]}" "${LAB_HOST}" bash -s -- "${SHA}" "${REMOTE_ROOT}/${RUN}" "${DAY_ARGS[@]+"${DAY_ARGS[@]}"}" \
  <"${SRC}/scripts/topic_archive/lab-archive-to-staging.sh" || stage_rc=$?
echo "stage ${RUN}: exit ${stage_rc}"

# 2. upload every staged run not yet uploaded
if ! aws sts get-caller-identity --query Arn --output text; then
  echo "BLOCKED $(ts): AWS session for profile ${AWS_PROFILE} is expired or missing;" \
    "staged runs wait on the lab host under ${REMOTE_ROOT} until 'aws sso login'"
  exit 3
fi
BUCKET="$(aws s3api list-buckets --query "Buckets[?starts_with(Name, '${BUCKET_PREFIX}')].Name" --output text)"
if [[ -z "${BUCKET}" || "${BUCKET}" == *$'\t'* ]]; then
  echo "REFUSED: expected exactly one bucket with prefix ${BUCKET_PREFIX}, got '${BUCKET}'" >&2
  exit 1
fi

failed=0
uploaded=0
PENDING=()
while IFS= read -r r; do
  [[ -n "${r}" && ! -e "${DONE}/${r}" ]] && PENDING+=("${r}")
done < <("${SSH[@]}" "${LAB_HOST}" "cd '${REMOTE_ROOT}' && for d in sched-*; do [ -e \"\$d/STAGED_OK\" ] && echo \"\$d\"; done; true")

for r in "${PENDING[@]+"${PENDING[@]}"}"; do
  rm -rf "${STAGE:?}/${r}"
  "${SSH[@]}" "${LAB_HOST}" "tar -C '${REMOTE_ROOT}' -cf - '${r}'" | tar -x -C "${STAGE}"
  rc=0
  # Run from the release directory: a working directory that holds a
  # directory named like the package would shadow it on sys.path.
  (cd "${SRC}" && env -u PYTHONPATH "${SRC}/.venv/bin/python" -m omnimarket.nodes.node_topic_archive_effect \
    --from-staging "${STAGE}/${r}" \
    --s3-uri "s3://${BUCKET}/${S3_PREFIX}" --kms-key "${KMS_ALIAS}" \
    --report-dir "${REPORTS}") >"${LOGS}/${r}.upload.out" || rc=$?
  echo "upload ${r}: exit ${rc} $(tail -1 "${LOGS}/${r}.upload.out")"
  if [[ ${rc} -eq 0 ]]; then
    : >"${DONE}/${r}"
    rm -rf "${STAGE:?}/${r}"
    uploaded=$((uploaded + 1))
  else
    failed=$((failed + 1))
  fi
done

echo "done $(ts): staged=${RUN} stage_exit=${stage_rc} uploaded=${uploaded} failed=${failed} pending_before=${#PENDING[@]}"
if [[ ${stage_rc} -ne 0 || ${failed} -ne 0 ]]; then
  exit 1
fi
