"""Portable command-line dispatcher for the frozen Flightctl v1 client."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

try:  # package execution: python -m flightctl.flightctl
    from .client import (
        DEFAULT_TTL_MIN,
        DEFAULT_WAIT_MAX_MIN,
        ClientError,
        InvalidRequest,
        RpcClient,
        _status_exit,
        encode_signature,
        failure_response,
        rpc_stdin,
        validate_operation,
    )
except ImportError:  # direct execution remains useful for a local shim
    from client import (  # type: ignore[no-redef]
        DEFAULT_TTL_MIN,
        DEFAULT_WAIT_MAX_MIN,
        ClientError,
        InvalidRequest,
        RpcClient,
        _status_exit,
        encode_signature,
        failure_response,
        rpc_stdin,
        validate_operation,
    )


USAGE = """usage:
  flightctl acquire <lane> <purpose> [ttl-min]
  flightctl renew <lane> <token> [ttl-min]
  flightctl release <lane> <token>
  flightctl wait <lane> <purpose> [ttl-min] [max-wait-min] [--yield]
  flightctl run <lane> <purpose> [--class CLASS] [--est MIN] [--max MIN] -- <workload...>
  flightctl cal [lane]
  flightctl free <lane>
  flightctl book <lane> <start> <end> <purpose>
  flightctl cancel <lane> <booking-id> [revision]
  flightctl preempt <lane> <approval-id> [token]
  flightctl approve <approval-id> <signature>
  flightctl chat load <lane> <pipeline> <purpose>
  flightctl chat unload <lane> <occupant-id> <generation>
  flightctl report <lane>
  flightctl status <lane>
  flightctl discover --output <proposal> [--current <inventory>]
  flightctl --rpc-stdin
""".rstrip()

_MINUTES = re.compile(r"^[0-9]+$")


class UsageError(InvalidRequest):
    """CLI syntax or positional compatibility error."""


@dataclass(frozen=True)
class RunSpec:
    lane: str
    purpose: str
    owner_class: str
    est_s: int
    max_s: int
    workload: tuple[str, ...]


def _require_lane(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise UsageError("lane is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise UsageError("lane is invalid")
    return value


def _require_purpose(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise UsageError("purpose is required")
    if len(value) > 512:
        raise UsageError("purpose is too long")
    return value


def _minutes(value: str, field: str) -> int:
    if not _MINUTES.fullmatch(value):
        raise UsageError(f"{field} must be a non-negative whole number of minutes")
    seconds = int(value) * 60
    if seconds < 1:
        raise UsageError(f"{field} must be at least one minute")
    return seconds


def _revision(value: str) -> int:
    if not _MINUTES.fullmatch(value) or int(value) < 1:
        raise UsageError("revision must be a positive integer")
    return int(value)


def _token(value: object) -> str:
    if not isinstance(value, str) or len(value) < 16 or len(value) > 512:
        raise UsageError("token is invalid")
    return value


def _strip_global(argv: Sequence[str]) -> tuple[list[str], bool]:
    result: list[str] = []
    json_output = False
    before_separator = True
    for value in argv:
        if before_separator and value == "--json":
            json_output = True
            continue
        result.append(value)
        if value == "--":
            before_separator = False
    return result, json_output


def _parse_run(args: list[str]) -> RunSpec:
    if len(args) < 4:
        raise UsageError("run requires lane, purpose, and a workload after --")
    lane = _require_lane(args[0])
    purpose = _require_purpose(args[1])
    options = args[2:]
    if "--" not in options:
        raise UsageError("run requires -- before the workload")
    separator = options.index("--")
    flags = options[:separator]
    workload = tuple(options[separator + 1 :])
    if not workload:
        raise UsageError("run workload is empty")
    owner_class = "batch"
    est_s = DEFAULT_TTL_MIN * 60
    max_s = DEFAULT_TTL_MIN * 60
    seen: set[str] = set()
    index = 0
    while index < len(flags):
        flag = flags[index]
        if flag in seen or flag not in {"--class", "--est", "--max"}:
            raise UsageError(f"unexpected run option: {flag}")
        seen.add(flag)
        if index + 1 >= len(flags):
            raise UsageError(f"{flag} requires a value")
        value = flags[index + 1]
        if flag == "--class":
            if value not in {"operator", "booked", "batch", "service", "resident", "standby"}:
                raise UsageError("invalid run class")
            owner_class = value
        elif flag == "--est":
            est_s = _minutes(value, "--est")
        else:
            max_s = _minutes(value, "--max")
        index += 2
    if est_s > max_s:
        raise UsageError("--est cannot exceed --max")
    return RunSpec(lane, purpose, owner_class, est_s, max_s, workload)


def _parse_approve(args: list[str], client: RpcClient) -> dict[str, object]:
    if len(args) < 2:
        raise UsageError("approve requires approval id and signature")
    approval_id = args[0]
    if not approval_id:
        raise UsageError("approval id is required")
    signature = args[1]
    signature_b64: str | None = None
    key_id = "key-a"
    verifier = _controller_from_client(client)
    verified_at = _clock_time(client, offset_s=60)
    user_presence = "verified"
    user_verification = "verified"
    options = args[2:]
    index = 0
    option_names = {
        "--key-id": "key_id",
        "--verifier": "verifier",
        "--verified-at": "verified_at",
        "--user-presence": "user_presence",
        "--user-verification": "user_verification",
        "--signature-b64": "signature_b64",
    }
    values: dict[str, str] = {}
    while index < len(options):
        name = options[index]
        if name not in option_names or index + 1 >= len(options):
            raise UsageError(f"unexpected approve option: {name}")
        key = option_names[name]
        if key in values:
            raise UsageError(f"duplicate approve option: {name}")
        values[key] = options[index + 1]
        index += 2
    key_id = values.get("key_id", key_id)
    verifier = values.get("verifier", verifier)
    verified_at = values.get("verified_at", verified_at)
    user_presence = values.get("user_presence", user_presence)
    user_verification = values.get("user_verification", user_verification)
    signature_b64 = values.get("signature_b64")
    return {
        "approval_id": approval_id,
        "proof": {
            "scheme": "ssh-sk",
            "key_id": key_id,
            "namespace": "flightctl/approval/v1",
            "encoding": "openssh-ssh-sk-signature/base64",
            "signature_b64": signature_b64 or encode_signature(signature),
        },
        "evidence": {
            "verifier": verifier,
            "verified_at": verified_at,
            "user_presence": user_presence,
            "user_verification": user_verification,
        },
    }


def _controller_from_client(client: RpcClient) -> str:
    admission = client.admission
    ingress = admission.get("ingress") if isinstance(admission, Mapping) else None
    if isinstance(ingress, Mapping) and isinstance(ingress.get("controller_id"), str):
        return str(ingress["controller_id"])
    return os.environ.get("FLIGHTCTL_CONTROLLER_ID", "controller")


def _clock_time(client: RpcClient, *, offset_s: int = 0) -> str:
    value = client.clock.utc() + timedelta(seconds=offset_s)
    value = value.astimezone(timezone.utc).replace(microsecond=0)
    return value.isoformat().replace("+00:00", "Z")


def _resolve_preempt_token(
    client: RpcClient,
    lane: str,
    explicit: str | None,
    lookup: Callable[[str], str | None] | None = None,
) -> str:
    if explicit is not None:
        return _token(explicit)
    inherited = os.environ.get("LANE_TOKEN")
    if inherited:
        return _token(inherited)
    token = lookup(lane) if lookup is not None else client.lookup_token_for_lane(lane)
    if token is None:
        raise UsageError("preempt requires an authenticated token")
    return _token(token)


def _run_stdin_with_client(client: RpcClient, *, stream_in: TextIO, stream_out: TextIO, stream_err: TextIO) -> int:
    raw = stream_in.read()
    try:
        stripped = raw.lstrip()
        decoder = json.JSONDecoder()
        value, index = decoder.raw_decode(stripped)
        if stripped[index:].strip():
            raise UsageError("stdin contains more than one JSON value")
        if not isinstance(value, Mapping):
            raise UsageError("stdin RPC value must be an object")
        validate_operation(value)
    except (json.JSONDecodeError, ClientError) as exc:
        print(f"flightctl: invalid RPC stdin: {exc}", file=stream_err)
        return 2
    response = client.request(value)
    json.dump(response, stream_out, ensure_ascii=False, separators=(",", ":"))
    stream_out.write("\n")
    stream_out.flush()
    return _status_exit(response)


def _parse_discover(args: list[str]) -> dict[str, object]:
    output: str | None = None
    current: str | None = None
    index = 0
    while index < len(args):
        flag = args[index]
        if flag not in {"--output", "--current"} or index + 1 >= len(args):
            raise UsageError("discover requires --output and optionally --current")
        value = args[index + 1]
        if not value or value.startswith("--"):
            raise UsageError(f"{flag} requires a path")
        if flag == "--output":
            if output is not None:
                raise UsageError("duplicate --output")
            output = value
        else:
            if current is not None:
                raise UsageError("duplicate --current")
            current = value
        index += 2
    if output is None:
        raise UsageError("discover requires --output")
    return {"output": output, "current": current}


def _dispatch(
    args: list[str],
    client: RpcClient,
    *,
    handoff: Callable[[Mapping[str, object], Sequence[str]], object] | None,
    discovery_handler: Callable[[Mapping[str, object]], Mapping[str, object]] | None,
    token_lookup: Callable[[str], str | None] | None,
    stdout: TextIO,
    stderr: TextIO,
) -> tuple[Mapping[str, object], int | None]:
    if not args:
        raise UsageError("a command is required")
    command = args[0]
    values = args[1:]
    if command == "acquire":
        if len(values) not in {2, 3}:
            raise UsageError("acquire requires lane, purpose, and optional ttl-min")
        lane = _require_lane(values[0])
        purpose = _require_purpose(values[1])
        ttl_s = _minutes(values[2], "ttl-min") if len(values) == 3 else DEFAULT_TTL_MIN * 60
        return client.call("acquire", lane, {"purpose": purpose, "class": "batch", "est_s": ttl_s, "max_s": ttl_s}), None
    if command == "renew":
        if len(values) not in {2, 3}:
            raise UsageError("renew requires lane, token, and optional ttl-min")
        lane = _require_lane(values[0])
        token = _token(values[1])
        ttl_s = _minutes(values[2], "ttl-min") if len(values) == 3 else DEFAULT_TTL_MIN * 60
        return client.call("renew", lane, {"token": token, "extend_s": ttl_s}), None
    if command == "release":
        if len(values) != 2:
            raise UsageError("release requires lane and token")
        lane = _require_lane(values[0])
        return client.call("release", lane, {"token": _token(values[1])}), None
    if command == "wait":
        yield_after_queue = False
        if "--yield" in values:
            if values.count("--yield") != 1 or values[-1] != "--yield":
                raise UsageError("--yield must be the final wait option")
            yield_after_queue = True
            values = values[:-1]
        if len(values) not in {2, 3, 4}:
            raise UsageError("wait requires lane, purpose, and optional durations")
        lane = _require_lane(values[0])
        purpose = _require_purpose(values[1])
        ttl_s = _minutes(values[2], "ttl-min") if len(values) >= 3 else DEFAULT_TTL_MIN * 60
        max_wait_s = _minutes(values[3], "max-wait-min") if len(values) == 4 else DEFAULT_WAIT_MAX_MIN * 60
        response, explicit_yield = client.wait_for_lane(
            lane,
            purpose,
            ttl_s=ttl_s,
            max_wait_s=max_wait_s,
            yield_after_queue=yield_after_queue,
        )
        return response, 4 if explicit_yield else None
    if command == "run":
        spec = _parse_run(values)
        inherited_token = os.environ.get("LANE_TOKEN")
        inherited_generation = os.environ.get("LANE_GENERATION")
        if inherited_token and not inherited_generation:
            raise UsageError("LANE_GENERATION is required with LANE_TOKEN")
        if inherited_generation and not inherited_token:
            raise UsageError("LANE_TOKEN is required with LANE_GENERATION")
        token: str | None = None
        generation: int | None = None
        if inherited_token:
            token = _token(inherited_token)
            if not re.fullmatch(r"[0-9]+", inherited_generation or "") or int(inherited_generation or "0") < 1:
                raise UsageError("LANE_GENERATION must be a positive integer")
            generation = int(inherited_generation)
        return client.lifecycle(
            spec.lane,
            spec.purpose,
            spec.workload,
            owner_class=spec.owner_class,
            est_s=spec.est_s,
            max_s=spec.max_s,
            token=token,
            generation=generation,
            handoff=handoff,
        ), None
    if command in {"cal", "free", "report", "status"}:
        if command == "cal":
            if len(values) > 1:
                raise UsageError("cal accepts at most one lane")
            lane = _require_lane(values[0]) if values else None
        else:
            if len(values) != 1:
                raise UsageError(f"{command} requires lane")
            lane = _require_lane(values[0])
        return client.call(command, lane, {}), None
    if command == "book":
        if len(values) != 4:
            raise UsageError("book requires lane, start, end, and purpose")
        return client.call("book", _require_lane(values[0]), {"start": values[1], "end": values[2], "purpose": _require_purpose(values[3])}), None
    if command == "cancel":
        if len(values) not in {2, 3}:
            raise UsageError("cancel requires lane, booking id, and optional revision")
        args: dict[str, object] = {"booking_id": values[1]}
        if not values[1]:
            raise UsageError("booking id is required")
        if len(values) == 3:
            args["revision"] = _revision(values[2])
        return client.call("cancel", _require_lane(values[0]), args), None
    if command == "preempt":
        if len(values) not in {2, 3}:
            raise UsageError("preempt requires lane, approval id, and optional token")
        lane = _require_lane(values[0])
        approval_id = values[1]
        if not approval_id:
            raise UsageError("approval id is required")
        token = _resolve_preempt_token(client, lane, values[2] if len(values) == 3 else None, token_lookup)
        return client.call("preempt", lane, {"token": token, "approval_id": approval_id}), None
    if command == "approve":
        return client.call("approve", None, _parse_approve(values, client)), None
    if command == "chat":
        if not values:
            raise UsageError("chat requires load or unload")
        if values[0] == "load":
            if len(values) != 4:
                raise UsageError("chat load requires lane, pipeline, and purpose")
            return client.call("chat-load", _require_lane(values[1]), {"pipeline_ref": values[2], "purpose": _require_purpose(values[3])}), None
        if values[0] == "unload":
            if len(values) != 4:
                raise UsageError("chat unload requires lane, occupant id, and generation")
            return client.call("chat-unload", _require_lane(values[1]), {"occupant_id": values[2], "generation": _revision(values[3])}), None
        raise UsageError("chat requires load or unload")
    if command == "discover":
        if discovery_handler is None:
            return failure_response("discover", "unavailable: discovery handler is not configured"), None
        options = _parse_discover(values)
        try:
            proposal = discovery_handler(options)
        except Exception as exc:
            return failure_response("discover", f"unavailable: discovery failed: {exc}"), None
        if not isinstance(proposal, Mapping):
            return failure_response("discover", "unavailable: discovery returned no proposal"), None
        output = Path(str(options["output"]))
        try:
            if not output.exists():
                output.write_text(json.dumps(proposal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            return failure_response("discover", f"unavailable: cannot write proposal: {exc}"), None
        return {
            "schema": 1,
            "request_id": "discover",
            "status": 200,
            "data": {"kind": "discovery", "proposal": dict(proposal), "output": str(output)},
            "error": None,
        }, None
    raise UsageError(f"unknown command: {command}")


def _calendar_time(value: str, display_zone: Any) -> str:
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    return parsed.astimezone(display_zone).isoformat()


def _human_message(
    response: Mapping[str, object],
    *,
    command: str | None = None,
    display_timezone: str = "UTC",
    display_zone: Any | None = None,
) -> str:
    status = response.get("status")
    data = response.get("data")
    error = response.get("error")
    if isinstance(data, Mapping) and data.get("kind") == "pending":
        return f"pending: {data.get('reason', 'request is pending')}"
    if status != 200:
        if isinstance(error, Mapping):
            return str(error.get("message", "request failed"))
        return "request failed"
    if not isinstance(data, Mapping):
        return "completed"
    kind = data.get("kind")
    if kind == "discovery":
        return f"proposal written to {data.get('output', 'the requested output')}"
    if kind == "grant":
        op = str(data.get("operation", command or "grant"))
        lane_ref = _lease_lane(data)
        generation = data.get("generation")
        token = data.get("token")
        if op == "claim":
            return f"claimed {lane_ref} generation={generation}"
        return f"acquired {lane_ref} generation={generation} token={token}"
    if kind == "mutation":
        op = str(data.get("operation", command or "mutation"))
        state = data.get("state")
        return f"{op} {state}"
    if kind == "projection":
        observation = data.get("observation")
        certainty = observation.get("certainty") if isinstance(observation, Mapping) else "unknown"
        reason = observation.get("reason") if isinstance(observation, Mapping) else None
        line = f"{data.get('scope', 'projection')} certainty={certainty} timezone={display_timezone}"
        if isinstance(reason, str) and reason:
            line += f" reason={reason}"
        zone = display_zone
        if zone is None:
            from zoneinfo import ZoneInfo

            zone = ZoneInfo(display_timezone)
        windows = data.get("windows")
        if isinstance(windows, list):
            rendered = [line]
            for window in windows:
                if not isinstance(window, Mapping):
                    continue
                start = _calendar_time(str(window["start"]), zone)
                end = _calendar_time(str(window["end"]), zone)
                rendered.append(f"window {start}..{end} state={window.get('state')} certainty={window.get('certainty')}")
            return "\n".join(rendered)
        return line
    if kind == "status":
        return f"status={data.get('state')}"
    if kind == "queue":
        return f"queue entries={len(data.get('entries', [])) if isinstance(data.get('entries'), list) else 0}"
    if kind == "booking":
        booking = data.get("booking")
        return f"booking {booking.get('state') if isinstance(booking, Mapping) else 'updated'}"
    if kind == "approval":
        approval = data.get("approval")
        return f"approval {approval.get('state') if isinstance(approval, Mapping) else 'updated'}"
    if kind == "report":
        return f"report events={len(data.get('events', [])) if isinstance(data.get('events'), list) else 0}"
    return "completed"


def _lease_lane(data: Mapping[str, object]) -> str:
    lease = data.get("lease")
    lane = lease.get("lane") if isinstance(lease, Mapping) else None
    return str(lane.get("lane_id", "lane")) if isinstance(lane, Mapping) else "lane"


def _emit(
    response: Mapping[str, object],
    *,
    json_output: bool,
    explicit_exit: int | None,
    command: str,
    stdout: TextIO,
    stderr: TextIO,
    display_timezone: str = "UTC",
    display_zone: Any | None = None,
) -> int:
    if json_output:
        value: object = response
        if command == "discover" and isinstance(response.get("data"), Mapping):
            value = response["data"].get("proposal", response)
        json.dump(value, stdout, ensure_ascii=False, separators=(",", ":"))
        stdout.write("\n")
        stdout.flush()
    else:
        message = _human_message(response, command=command, display_timezone=display_timezone, display_zone=display_zone)
        if response.get("status") == 200 and (explicit_exit is None or explicit_exit == 0):
            print(message, file=stdout)
        elif explicit_exit == 4:
            print(message, file=stdout)
        else:
            print(f"flightctl: {message}", file=stderr)
    return explicit_exit if explicit_exit is not None else _status_exit(response)


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Any | None = None,
    clock: Any | None = None,
    handoff: Callable[[Mapping[str, object], Sequence[str]], object] | None = None,
    discovery_handler: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
    token_lookup: Callable[[str], str | None] | None = None,
    admission: Mapping[str, object] | None = None,
    endpoint: str | None = None,
    client: RpcClient | None = None,
    sleeper: Callable[[float], object] | None = None,
    request_id_factory: Callable[[], str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the adapter and return the frozen process exit code."""

    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    raw_args = list(sys.argv[1:] if argv is None else argv)
    args, json_output = _strip_global(raw_args)
    if args == ["--help"] or args == ["help"]:
        print(USAGE, file=output_stream)
        return 0
    if args and args[0] == "--rpc-stdin":
        if len(args) != 1:
            print("flightctl: --rpc-stdin does not accept positional arguments", file=error_stream)
            return 2
        if client is not None:
            return _run_stdin_with_client(client, stream_in=sys.stdin, stream_out=output_stream, stream_err=error_stream)
        return rpc_stdin(
            transport=transport,
            clock=clock,
            stream_in=sys.stdin,
            stream_out=output_stream,
            stream_err=error_stream,
            endpoint=endpoint,
            admission=admission,
        )
    if not args:
        print(USAGE, file=error_stream)
        return 2
    try:
        selected_client = client or RpcClient(
            transport,
            clock=clock,
            endpoint=endpoint,
            admission=admission,
            sleeper=sleeper,
            request_id_factory=request_id_factory,
            token_lookup=token_lookup,
        )
        response, explicit_exit = _dispatch(
            args,
            selected_client,
            handoff=handoff,
            discovery_handler=discovery_handler,
            token_lookup=token_lookup,
            stdout=output_stream,
            stderr=error_stream,
        )
    except (ClientError, ValueError) as exc:
        print(f"flightctl: invalid arguments: {exc}", file=error_stream)
        return 2
    command = args[0]
    return _emit(
        response,
        json_output=json_output,
        explicit_exit=explicit_exit,
        command=command,
        stdout=output_stream,
        stderr=error_stream,
        display_timezone=selected_client.display_timezone,
        display_zone=selected_client.display_zone,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["RunSpec", "USAGE", "main"]
