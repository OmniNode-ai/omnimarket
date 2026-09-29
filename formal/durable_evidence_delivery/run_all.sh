#!/usr/bin/env bash
# Recreate design and mutation evidence once TLC is available. A tool error
# never counts as either a design pass or an expected mutant violation.
set -euo pipefail
cd "$(dirname "$0")"
matrix=(
    "DurableEvidenceDelivery_design|PASS|"
    "M1_no_off_host_intent|VIOLATION|DispatchUsesRecordedIntent"
    "M2_dispatch_stale_intent|VIOLATION|DispatchUsesExactCurrentIntent"
    "M2_admit_unconfirmed|VIOLATION|NoAdmissionWithoutConfirmation"
    "M3_nonidempotent_confirmation|VIOLATION|OneLogicalAttemptPerExecution"
    "M4_admit_stale_revision|VIOLATION|NoStaleRevisionAdmission"
    "M5_cleanup_drops_history|VIOLATION|RecordedEvidenceIsRetained"
    "M6_admit_older_pass|VIOLATION|HighestAllocatedAttemptSelected"
    "M7_lost_unconfirmed_pass|VIOLATION|LostUnconfirmedPassIsUnresolved"
    "M8_dispatch_foreign_subject|VIOLATION|DispatchUsesExactSubjectIntent"
)

digest="$(shasum -a 256 DurableEvidenceDelivery.tla | cut -d' ' -f1)"
tool_source="${TLA2TOOLS_SOURCE:?set TLA2TOOLS_SOURCE to the checked jar source}"
tool_sha1="$(shasum -a 1 "${TLA2TOOLS_JAR:?set TLA2TOOLS_JAR to a checked tla2tools.jar}" | cut -d' ' -f1)"
tool_sha256="$(shasum -a 256 "$TLA2TOOLS_JAR" | cut -d' ' -f1)"
mkdir -p results
summary="$(mktemp results/SUMMARY.XXXXXX)"
trap 'rm -f "$summary"' EXIT
{
    echo "tool: source=$tool_source sha1=$tool_sha1 sha256=$tool_sha256"
    echo "model: DurableEvidenceDelivery.tla sha256=$digest"
    for row in "${matrix[@]}"; do
        IFS='|' read -r config expected property <<<"$row"
        set +e
        ./run_tlc.sh "$config"
        status=$?
        set -e
        output="results/$config.out"
        if [[ "$expected" == PASS ]]; then
            if [[ $status -ne 0 ]] || ! grep -Fq "No error has been found" "$output"; then
                echo "$config unexpected result: expected design PASS" >&2
                exit 1
            fi
            result=PASS
        else
            if [[ $status -eq 0 ]] || ! grep -Fq "Invariant $property is violated" "$output"; then
                echo "$config unexpected result: expected violation of $property" >&2
                exit 1
            fi
            result="VIOLATED $property"
        fi
        echo "$config cfg_sha256=$(shasum -a 256 "$config.cfg" | cut -d' ' -f1) result=$result"
    done
} | tee "$summary"
mv "$summary" results/SUMMARY.txt
trap - EXIT
