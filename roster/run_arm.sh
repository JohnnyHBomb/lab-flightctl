#!/usr/bin/env bash
# Run one roster arm through the configured flightctl lifecycle owner.
#
# This wrapper deliberately does not acquire, release, start, stop, or renew a
# lease. The configured client owns that lifecycle and receives the workload
# as an argv vector after --. Keeping that boundary here makes an inherited
# token a claim input rather than an instruction to skip authentication.
set -euo pipefail

usage() {
	cat >&2 <<'EOF'
usage: run_arm.sh <lane> <purpose> [options] -- <workload> [args ...]
       run_arm.sh --lane <lane> --purpose <purpose> [options] -- <workload> [args ...]

options:
  --class <class>  ownership class (default: batch)
  --est <minutes>  estimated runtime in minutes (default: 240)
  --max <minutes>  approved maximum runtime in minutes (default: 240)

The lifecycle client is selected by FLIGHTCTL_CLIENT or ROSTER_CLIENT.
LANE_TOKEN, when supplied, must be accompanied by LANE_GENERATION; the
configured client performs the authenticated claim.
EOF
}

fail() {
	local message=$1
	printf 'run_arm: %s\n' "$message" >&2
	exit "${2:-2}"
}

is_positive_integer() {
	[[ $1 =~ ^[1-9][0-9]*$ ]]
}

lane=
purpose=
owner_class=${ROSTER_CLASS:-batch}
est_min=${ROSTER_EST_MIN:-240}
max_min=${ROSTER_MAX_MIN:-240}
workload=()
positional=()
after_separator=false

while (($#)); do
	arg=$1
	shift
	if $after_separator; then
		workload+=("$arg")
		continue
	fi
	case $arg in
		--)
			after_separator=true
			;;
		-h|--help)
			usage
			exit 0
			;;
		--lane)
			(($#)) || fail 'missing value for --lane'
			lane=$1
			shift
			;;
		--purpose)
			(($#)) || fail 'missing value for --purpose'
			purpose=$1
			shift
			;;
		--class)
			(($#)) || fail 'missing value for --class'
			owner_class=$1
			shift
			;;
		--est)
			(($#)) || fail 'missing value for --est'
			est_min=$1
			shift
			;;
		--max)
			(($#)) || fail 'missing value for --max'
			max_min=$1
			shift
			;;
		--*)
			fail "unknown option: $arg"
			;;
		*)
			positional+=("$arg")
			;;
	esac
done

if [[ -z $lane && ${#positional[@]} -gt 0 ]]; then
	lane=${positional[0]}
fi
if [[ -z $purpose && ${#positional[@]} -gt 1 ]]; then
	purpose=${positional[1]}
fi
if [[ ${#positional[@]} -gt 2 ]]; then
	if [[ ${#positional[@]} -gt 3 ]]; then
		fail 'unexpected positional arguments; put workload after --'
	fi
	if [[ $est_min == "${ROSTER_EST_MIN:-240}" && $max_min == "${ROSTER_MAX_MIN:-240}" ]]; then
		est_min=${positional[2]}
		max_min=${positional[2]}
	else
		fail 'unexpected positional duration'
	fi
fi

[[ -n $lane ]] || fail 'lane is required'
[[ -n $purpose ]] || fail 'purpose is required'
is_positive_integer "$est_min" || fail 'estimated minutes must be a positive integer'
is_positive_integer "$max_min" || fail 'maximum minutes must be a positive integer'
((max_min >= est_min)) || fail 'maximum minutes must not be below estimated minutes'

case $owner_class in
	operator|booked|batch|service|resident|standby) ;;
	*) fail "unsupported class: $owner_class" ;;
esac

if [[ -n ${LANE_TOKEN:-} ]]; then
	[[ -n ${LANE_GENERATION:-} ]] || fail 'LANE_TOKEN requires LANE_GENERATION'
	is_positive_integer "$LANE_GENERATION" || fail 'LANE_GENERATION must be a positive integer'
elif [[ -n ${LANE_GENERATION:-} ]]; then
	fail 'LANE_GENERATION requires LANE_TOKEN'
fi

if ((${#workload[@]} == 0)); then
	if [[ -n ${ROSTER_WORKLOAD_EXECUTABLE:-} ]]; then
		workload=("$ROSTER_WORKLOAD_EXECUTABLE")
	elif [[ -n ${ROSTER_WORKLOAD:-} ]]; then
		# ROSTER_WORKLOAD is one executable path, never a shell command string.
		workload=("$ROSTER_WORKLOAD")
	else
		fail 'workload is required after --'
	fi
fi

client=${FLIGHTCTL_CLIENT:-${ROSTER_CLIENT:-}}
[[ -n $client ]] || fail 'FLIGHTCTL_CLIENT or ROSTER_CLIENT is required' 3
if [[ $client == */* ]]; then
	[[ -x $client ]] || fail "configured client is not executable: $client" 3
else
	command -v "$client" >/dev/null 2>&1 || fail "configured client was not found: $client" 3
fi

# exec is intentional: signal delivery and exit status belong to the lifecycle
# owner. No outer EXIT/INT/TERM trap can release a lease it did not own.
exec "$client" run "$lane" "$purpose" --class "$owner_class" --est "$est_min" --max "$max_min" -- "${workload[@]}"
