#!/bin/bash
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
#
# lab-archive-to-staging.sh -- runs ON THE LAB HOST, fed over ssh by
# topic-archive-s3-tick.sh (`ssh HOST bash -s -- SHA OUT_DIR [--day D ...] < this`).
#
# Archives the node_topic_archive_effect contract's source topics from the
# lab broker into an owner-only local staging directory, read-only against the
# broker (no consumer group, no commits). The broker's SASL credential is
# copied from the broker container's environment into this process's
# environment only: it is never printed and never placed on a command line.
# On a verified run it writes OUT_DIR/STAGED_OK, which is the only signal the
# uploader acts on. Nothing is deleted and no retention is changed.
#
# Args: SHA   omnimarket commit to run (checked out detached in a private clone)
#       OUT   staging directory to create (must not exist)
#       ...   passed through to the archive node (e.g. --day YYYY-MM-DD)

set -euo pipefail
umask 077

SHA="${1:?commit sha}"
OUT="${2:?staging dir}"
shift 2

CLONE="${HOME}/topic-archive-s3/omnimarket"
UV="${HOME}/.local/bin/uv"
BROKER_CONTAINER="${TOPIC_ARCHIVE_BROKER_CONTAINER:-omnibase-infra-redpanda}"

if [[ -e "${OUT}" ]]; then
  echo "REFUSED: ${OUT} already exists" >&2
  exit 2
fi

if [[ ! -d "${CLONE}/.git" ]]; then
  mkdir -p "$(dirname "${CLONE}")"
  git clone --quiet https://github.com/OmniNode-ai/omnimarket.git "${CLONE}"
fi
if [[ "$(git -C "${CLONE}" rev-parse HEAD)" != "${SHA}" ]]; then
  git -C "${CLONE}" fetch --quiet origin "${SHA}"
  git -C "${CLONE}" checkout --quiet --detach "${SHA}"
  (cd "${CLONE}" && "${UV}" sync --frozen --quiet)
fi

ENVS="$(docker inspect "${BROKER_CONTAINER}" --format '{{range .Config.Env}}{{println .}}{{end}}')"
KAFKA_SASL_USERNAME="$(printf '%s\n' "${ENVS}" | sed -n 's/^DEV_KAFKA_SASL_USERNAME=//p')"
KAFKA_SASL_PASSWORD="$(printf '%s\n' "${ENVS}" | sed -n 's/^DEV_KAFKA_SASL_PASSWORD=//p')"
unset ENVS
if [[ -z "${KAFKA_SASL_USERNAME}" || -z "${KAFKA_SASL_PASSWORD}" ]]; then
  echo "REFUSED: broker SASL credential not found in ${BROKER_CONTAINER}'s environment" >&2
  exit 2
fi
export KAFKA_SASL_USERNAME KAFKA_SASL_PASSWORD
export KAFKA_SECURITY_PROTOCOL=SASL_PLAINTEXT KAFKA_SASL_MECHANISM=SCRAM-SHA-256
export KAFKA_BOOTSTRAP_SERVERS=localhost:19092

mkdir -p "${OUT}/runs"
cd "${CLONE}"
rc=0
nice -n 10 "${UV}" run --frozen --no-sync python -m omnimarket.nodes.node_topic_archive_effect \
  --bootstrap "${KAFKA_BOOTSTRAP_SERVERS}" --staging-dir "${OUT}" "$@" \
  >"${OUT}/runs/node.out" 2>"${OUT}/runs/node.err" || rc=$?
tail -1 "${OUT}/runs/node.out"
if [[ ${rc} -eq 0 ]]; then
  : >"${OUT}/STAGED_OK"
fi
exit "${rc}"
