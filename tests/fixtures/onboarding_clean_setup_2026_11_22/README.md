# Clean-machine `onex setup` failure recorded on 2026-11-22

`recorded_setup_error.txt` is the one-line error `onex setup` printed on a clean machine (an empty home, no
`ONEX_*` variables, a fresh empty workspace) before it exited 1 and wrote nothing. It was recorded on omnimarket
dev 49169e870 and on the released omnimarket 0.4.324, and again on 0.4.325 in a clean container on 2026-11-24.
