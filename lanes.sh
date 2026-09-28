#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_bin=${FLIGHTCTL_PYTHON:-python3}

# Keep every caller argument as one argv element.  The Python adapter owns all
# parsing and transport; this wrapper never evaluates a purpose or workload.
PYTHONPATH="$script_dir${PYTHONPATH:+:$PYTHONPATH}" exec "$python_bin" -m flightctl.flightctl "$@"
