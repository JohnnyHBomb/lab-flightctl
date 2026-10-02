#!/usr/bin/env python3
"""Amendment 3 touch-set gate (owner's C9 decision): a packet that declares a TOUCH SET in docs/v2/SLICES.md (today: C9)
may add files only under its 'new:' paths and may modify only its 'extension:' files. Every other path in its diff is
an R1 file and is refused.

Amendment 3 rev 2 (Sol 6.1 amd3 P2): the three documentation extension points are checked LINE BY LINE against the
base, not only by name:
  * docs/v2/CONFORMANCE.tsv  only the status and note columns of rows whose 'v2 home' names the packet and no other
                             packet may change; no row is added, removed or reordered;
  * docs/v2/migration-map.tsv the base file is an unchanged prefix; appended rows belong to the packet;
  * docs/v2/SLICES.md         everything outside the packet's own brief is unchanged, and inside it the TOUCH SET block
                             and the R1 PATHWAYS USED table are unchanged.
Usage: python tools/touch_set_gate.py --base <ref> --head <ref> --packet C9 [--slices docs/v2/SLICES.md]
Exit 0 = pass; 1 = fail (problems printed); 2 = usage/git error. Read-only: git diff and git show only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

CONFORMANCE = "docs/v2/CONFORMANCE.tsv"
MIGRATION_MAP = "docs/v2/migration-map.tsv"
SLICES = "docs/v2/SLICES.md"
PACKET_TOKEN = r"(?<![\w-])([A-D][0-9][0-9a-z]*|[A-C]-ASM)(?![\w-])"


def load_touch_set(slices_text: str, packet: str) -> dict[str, list[str]]:
    """The 'new:' and 'extension:' entries of the packet brief's TOUCH SET block."""
    brief = slices_text.split(f"\n### {packet}:", 1)[1].split("\n### ", 1)[0]
    block = brief.split("- TOUCH SET", 1)[1].split("\n- ", 1)[0]
    touch: dict[str, list[str]] = {"new": [], "extension": []}
    for kind, path in re.findall(r"^\s+- (new|extension): `([^`]+)`", block, flags=re.M):
        touch[kind].append(path)
    return touch


def _under(path: str, entry: str) -> bool:
    return path == entry or (entry.endswith("/") and path.startswith(entry))


def _conformance_problems(base: str, head: str, packet: str) -> list[str]:
    b, h = base.splitlines(), head.splitlines()
    if len(b) != len(h):
        return [f"{CONFORMANCE}: rows were added or removed ({len(b)} -> {len(h)} lines); the packet may only update its own rows"]
    problems = []
    for n, (old, new) in enumerate(zip(b, h), 1):
        if old == new:
            continue
        oc, nc = old.split("\t"), new.split("\t")
        if old.startswith("#") or len(oc) != 6 or len(nc) != 6 or oc[0] == "source":
            problems.append(f"{CONFORMANCE}:{n}: a header or malformed line changed")
            continue
        homes = set(re.findall(PACKET_TOKEN, oc[4]))
        if homes != {packet}:
            problems.append(f"{CONFORMANCE}:{n} ({oc[0]}): the row is homed at {sorted(homes) or 'no packet'}, not at {packet} alone")
        if [oc[i] for i in (0, 1, 2, 4)] != [nc[i] for i in (0, 1, 2, 4)]:
            problems.append(f"{CONFORMANCE}:{n} ({oc[0]}): only the status and note columns may change")
    return problems


def _migration_problems(base: str, head: str, packet: str) -> list[str]:
    b, h = base.splitlines(), head.splitlines()
    if h[: len(b)] != b:
        return [f"{MIGRATION_MAP}: existing rows changed; the packet may only append"]
    problems = []
    for n, line in enumerate(h[len(b):], len(b) + 1):
        cells = line.split("\t")
        if line.startswith("#") or len(cells) != 5 or cells[1] != packet:
            problems.append(f"{MIGRATION_MAP}:{n}: an appended line must be a 5-column row owned by {packet}")
    return problems


def _split_brief(text: str, packet: str) -> tuple[str, str, str]:
    head, _, rest = text.partition(f"\n### {packet}:")
    brief, sep, tail = rest.partition("\n### ")
    return head, brief, sep + tail


def _pinned_blocks(brief: str) -> tuple[str, str]:
    touch = brief.split("- TOUCH SET", 1)[1].split("\n- ", 1)[0] if "- TOUCH SET" in brief else ""
    table = brief.split("| Capability | R1 deliverer | Pathway |", 1)[1].split("\n\n", 1)[0] if "| Capability |" in brief else ""
    return touch, table


def _slices_problems(base: str, head: str, packet: str) -> list[str]:
    if f"\n### {packet}:" not in head:
        return [f"{SLICES}: the {packet} brief was removed or renamed"]
    b0, bb, b1 = _split_brief(base, packet)
    h0, hb, h1 = _split_brief(head, packet)
    problems = []
    if b0 != h0 or b1 != h1:
        problems.append(f"{SLICES}: text outside the {packet} brief changed")
    if _pinned_blocks(bb) != _pinned_blocks(hb):
        problems.append(f"{SLICES}: the {packet} brief's TOUCH SET block or R1 PATHWAYS USED table changed")
    return problems


EXTENSION_RULES = {CONFORMANCE: _conformance_problems, MIGRATION_MAP: _migration_problems, SLICES: _slices_problems}


def check(changed: dict[str, str], touch: dict[str, list[str]], contents: dict[str, tuple[str, str]] | None = None,
          packet: str = "C9") -> list[str]:
    """changed: {path: git status letter (A, M, D)} for the packet's diff; contents: {extension path: (base text, head
    text)} for every modified extension point (a modified extension without contents is refused: fail closed)."""
    problems = []
    contents = contents or {}
    for path, status in sorted(changed.items()):
        if any(_under(path, e) for e in touch["new"]):
            if status != "A":
                problems.append(f"{path} ({status}): a touch-set 'new' path must be ADDED by the packet, not changed or deleted")
        elif path in touch["extension"]:
            if status != "M":
                problems.append(f"{path} ({status}): an extension point may only be modified")
            elif path not in EXTENSION_RULES:
                problems.append(f"{path}: no line-level rule exists for this extension point")
            elif path not in contents:
                problems.append(f"{path}: modified extension point not content-checked")
            else:
                problems += EXTENSION_RULES[path](*contents[path], packet)
        else:
            problems.append(f"{path} ({status}) is an R1 file outside the packet's touch set")
    return problems


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True, timeout=60).stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--packet", required=True)
    ap.add_argument("--slices", default=SLICES)
    a = ap.parse_args(argv)
    try:
        out = _git("diff", "--name-status", "--no-renames", a.base, a.head)
        changed = {}
        for line in out.splitlines():
            code, _, path = line.partition("\t")
            changed[path] = code[:1]
        # the touch set is read from the BASE, so the packet cannot widen its own touch set
        touch = load_touch_set(_git("show", f"{a.base}:{a.slices}"), a.packet)
        contents = {p: (_git("show", f"{a.base}:{p}"), _git("show", f"{a.head}:{p}"))
                    for p, s in changed.items() if s == "M" and p in touch["extension"]}
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"touch_set_gate: git failed: {exc}", file=sys.stderr)
        return 2
    problems = check(changed, touch, contents, a.packet)
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
