#!/usr/bin/env bash
# Local arm entrypoint: use the same controller-owned lifecycle as remote arms.
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec "$script_dir/run_arm.sh" "$@"
