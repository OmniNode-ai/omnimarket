# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host scripts the frozen runner sent for two provisioning steps of one lane start, as recorded (OMN-23425).

Recorded from the runner's ``omni_home_script`` (with ``--remote-omni-home /srv/example/registry``) and
``scratch_script`` (run id ``rlane-example-000001``) on 2026-11-10. The parity tests compare the ssh transport's
scripts and printed lines with these; a change in either side's text fails them.
"""

RECORDED_OMNI_HOME_SCRIPT = "\n".join(
    [
        'H=""',
        "for d in /srv/example/registry; do",
        '  if [ -n "$d" ] && [ -d "$d/omnibase_infra" ]; then H="$d"; break; fi',
        "done",
        'if [ -z "$H" ]; then echo "REMOTE-OMNI-HOME-NONE $(hostname): none of /srv/example/registry holds omnibase_infra"; exit 96; fi',
        'echo "REMOTE-OMNI-HOME $H"',
    ]
)

RECORDED_SCRATCH_SCRIPT = "\n".join(
    [
        'LR="$HOME/lab-run"; W="$LR/runs/rlane-example-000001"; C="$LR/runs/rlane-example-000001.ctx"',
        'mkdir -p "$W" "$C" || exit 70',
        'echo "REMOTE-HOME $HOME"',
    ]
)
