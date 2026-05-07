#!/usr/bin/env bash
# Run the local delegation-cost projection lane without touching .201.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

ENV_FILE="${OMNIMARKET_PROJECTION_ENV_FILE:-${REPO_ROOT}/.env}"
STATE_DIR="${OMNIMARKET_PROJECTION_STATE_DIR:-${REPO_ROOT}/.onex_state/delegation-cost-projection}"
LOG_DIR="${STATE_DIR}/logs"
SUPERVISOR_PID_FILE="${STATE_DIR}/supervisor.pid"
CHILD_PID_FILE="${STATE_DIR}/children.pid"

SERVICE_NAMES=(
  "projection-delegation"
  "projection-llm-cost"
  "projection-savings"
)
SERVICE_GROUPS=(
  "local.omnimarket.projection-delegation.consume.v1"
  "local.omnimarket.projection-llm-cost.consume.v1"
  "local.omnibase_infra.node_projection_savings.consume.v1"
)
SERVICE_MODULES=(
  "omnimarket.nodes.node_projection_delegation.handlers.handler_delegation"
  "omnimarket.nodes.node_projection_llm_cost.handlers.handler_llm_cost"
  "omnimarket.nodes.node_projection_savings.handlers.handler_savings"
)

usage() {
  cat <<'EOF'
Usage:
  scripts/run_delegation_cost_projection_process.sh [--detach|--check|--stop|--status]

Starts only:
  - projection-delegation
  - projection-llm-cost
  - projection-savings

Required:
  - .env, or OMNIMARKET_PROJECTION_ENV_FILE=/path/to/env
  - OMNIDASH_ANALYTICS_DB_URL in that env/environment
  - KAFKA_BROKERS or KAFKA_BOOTSTRAP_SERVERS in that env/environment

Safety:
  The wrapper refuses 192.168.86.201 endpoints so the local lane cannot mutate .201.  # onex-allow-internal-ip OMN-10580 reason="safety guard docstring; the IP appears as a string to reject, not a connection target"
  Secrets are never printed.
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

load_env() {
  [[ -f "${ENV_FILE}" ]] || die "env file is missing: ${ENV_FILE}"
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
}

require_value() {
  local name="$1"
  local value="${!name:-}"
  [[ -n "${value}" ]] || die "${name} is required; set it in ${ENV_FILE}"
}

refuse_protected_runtime() {
  local name="$1"
  local value="${!name:-}"
  [[ -z "${value}" ]] && return 0
  if [[ "${value}" == *"192.168.86.201"* ]]; then  # onex-allow-internal-ip OMN-10580 reason="safety guard that BLOCKS connections to .201; IP used as a pattern to reject, not a target"
    die "${name} points at protected .201 runtime; use local bus/database endpoints"
  fi
}

prepare_env() {
  load_env
  if [[ -z "${KAFKA_BROKERS:-}" && -n "${KAFKA_BOOTSTRAP_SERVERS:-}" ]]; then
    export KAFKA_BROKERS="${KAFKA_BOOTSTRAP_SERVERS}"
  fi

  require_value "OMNIDASH_ANALYTICS_DB_URL"
  require_value "KAFKA_BROKERS"
  refuse_protected_runtime "OMNIDASH_ANALYTICS_DB_URL"
  refuse_protected_runtime "KAFKA_BROKERS"
  refuse_protected_runtime "KAFKA_BOOTSTRAP_SERVERS"
}

ensure_runtime_tools() {
  command -v uv >/dev/null 2>&1 || die "uv is required to start projection processes"
}

is_pid_alive() {
  local pid="$1"
  [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1
}

read_pid_file() {
  local path="$1"
  [[ -f "${path}" ]] || return 1
  tr -d '[:space:]' <"${path}"
}

check_running_supervisor() {
  local pid=""
  local child_pid=""
  pid="$(read_pid_file "${SUPERVISOR_PID_FILE}" || true)"
  if is_pid_alive "${pid}"; then
    die "projection supervisor is already running with pid ${pid}"
  fi
  if [[ -f "${CHILD_PID_FILE}" ]]; then
    while IFS= read -r child_pid; do
      if is_pid_alive "${child_pid}"; then
        die "projection child process is already running with pid ${child_pid}; run --stop first"
      fi
    done <"${CHILD_PID_FILE}"
  fi
}

write_child_pids() {
  : >"${CHILD_PID_FILE}"
  for pid in "$@"; do
    printf '%s\n' "${pid}" >>"${CHILD_PID_FILE}"
  done
}

stop_processes() {
  local stopped=0
  if [[ -f "${CHILD_PID_FILE}" ]]; then
    while IFS= read -r pid; do
      if is_pid_alive "${pid}"; then
        kill "${pid}" >/dev/null 2>&1 || true
        stopped=1
      fi
    done <"${CHILD_PID_FILE}"
  fi

  local supervisor_pid=""
  supervisor_pid="$(read_pid_file "${SUPERVISOR_PID_FILE}" || true)"
  if is_pid_alive "${supervisor_pid}"; then
    kill "${supervisor_pid}" >/dev/null 2>&1 || true
    stopped=1
  fi

  rm -f "${SUPERVISOR_PID_FILE}" "${CHILD_PID_FILE}"
  if [[ "${stopped}" -eq 1 ]]; then
    printf 'stopped delegation-cost projection processes\n'
  else
    printf 'no running delegation-cost projection processes found\n'
  fi
}

show_status() {
  local supervisor_pid=""
  supervisor_pid="$(read_pid_file "${SUPERVISOR_PID_FILE}" || true)"
  if is_pid_alive "${supervisor_pid}"; then
    printf 'supervisor: running pid=%s\n' "${supervisor_pid}"
  else
    printf 'supervisor: not running\n'
  fi

  for i in "${!SERVICE_NAMES[@]}"; do
    local name="${SERVICE_NAMES[$i]}"
    local line_number=$((i + 1))
    local pid=""
    if [[ -f "${CHILD_PID_FILE}" ]]; then
      pid="$(sed -n "${line_number}p" "${CHILD_PID_FILE}" | tr -d '[:space:]')"
    fi
    if is_pid_alive "${pid}"; then
      printf '%s: running pid=%s log=%s/%s.log\n' "${name}" "${pid}" "${LOG_DIR}" "${name}"
    else
      printf '%s: not running log=%s/%s.log\n' "${name}" "${LOG_DIR}" "${name}"
    fi
  done
}

run_foreground() {
  prepare_env
  ensure_runtime_tools
  check_running_supervisor
  mkdir -p "${LOG_DIR}"
  printf '%s\n' "$$" >"${SUPERVISOR_PID_FILE}"

  local child_pids=()
  cleanup() {
    for pid in "${child_pids[@]:-}"; do
      if is_pid_alive "${pid}"; then
        kill "${pid}" >/dev/null 2>&1 || true
      fi
    done
    rm -f "${SUPERVISOR_PID_FILE}" "${CHILD_PID_FILE}"
  }
  trap cleanup EXIT
  trap 'cleanup; exit 0' INT TERM

  for i in "${!SERVICE_NAMES[@]}"; do
    local name="${SERVICE_NAMES[$i]}"
    local group="${SERVICE_GROUPS[$i]}"
    local module="${SERVICE_MODULES[$i]}"
    (
      export KAFKA_CONSUMER_GROUP="${group}"
      export PYTHONUNBUFFERED=1
      exec uv run python -m "${module}"
    ) >>"${LOG_DIR}/${name}.log" 2>&1 &
    child_pids+=("$!")
  done
  write_child_pids "${child_pids[@]}"

  printf 'started delegation-cost projection lane: %s\n' "${SERVICE_NAMES[*]}"
  printf 'logs: %s\n' "${LOG_DIR}"
  wait "${child_pids[@]}"
}

run_detached() {
  prepare_env
  ensure_runtime_tools
  check_running_supervisor
  mkdir -p "${LOG_DIR}"
  nohup "${BASH_SOURCE[0]}" --foreground >"${LOG_DIR}/supervisor.log" 2>&1 &
  printf 'started delegation-cost projection supervisor pid=%s\n' "$!"
  printf 'logs: %s\n' "${LOG_DIR}"
}

mode="--foreground"
if [[ $# -gt 0 ]]; then
  mode="$1"
fi

case "${mode}" in
  --foreground)
    run_foreground
    ;;
  --detach)
    run_detached
    ;;
  --check)
    prepare_env
    ensure_runtime_tools
    printf 'delegation-cost projection preflight ok; services=%s\n' "${SERVICE_NAMES[*]}"
    ;;
  --stop)
    stop_processes
    ;;
  --status)
    show_status
    ;;
  -h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
