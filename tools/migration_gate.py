#!/usr/bin/env python3
"""GATES G1b: checked migration gate (Sol 6 B7; round 4: replacement tests are verified by COLLECTION).

Given a packet's base and head commits (run in a checkout of head), list every PRE-EXISTING file under tests/ and
roster/ that the diff modifies or deletes, and check each against docs/v2/migration-map.tsv rows owned by the packet:
  * a touched pre-existing file with no row for this packet                              -> FAIL
  * a 'delete' row whose file is touched but not deleted                                 -> FAIL
  * a 'rewrite' row whose replacement is not a COLLECTED pytest node at head in a test file this
    packet added or modified (or the exact path::name node the map names)                -> FAIL
    (`pytest --collect-only -q` node ids; a name in a comment or string does not count)
Usage: python tools/migration_gate.py --base <ref> --head <ref> --packet <ID> [--map docs/v2/migration-map.tsv]
Exit 0 = pass; 1 = fail (problems printed); 2 = usage/git/collection error. Read-only: git diff + pytest collection.
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
from pathlib import Path

WATCHED = ("tests/", "roster/")


def expand(pattern: str) -> list[str]:
    path = re.split(r"::| \(", pattern, maxsplit=1)[0].strip()
    m = re.search(r"\{([^}]*)\}", path)
    if not m:
        return [path]
    out = []
    for alt in m.group(1).split(","):
        out += expand(path[: m.start()] + alt + path[m.end():])
    return out


def load_rows(map_path: Path) -> list[dict[str, str]]:
    lines = [l for l in map_path.read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
    return list(csv.DictReader(lines, delimiter="\t"))


def collected_functions(node_ids: set[str]) -> set[str]:
    """Function names of collected pytest node ids ('path::[Class::]name[params]')."""
    return {re.sub(r"\[.*\]$", "", nid.rsplit("::", 1)[-1]) for nid in node_ids if "::" in nid}


def replacement_present(replacement: str, collected: set[str], packet_test_files: set[str]) -> bool:
    """Round 5 (Sol 6 r4 B7): a bare name counts only if a collected node with that name lives in a test file this
    packet added or modified; an explicit 'path::name' counts only if exactly that node is collected."""
    for nid in collected:
        base = re.sub(r"\[.*\]$", "", nid)
        path, _, rest = base.partition("::")
        name = rest.rsplit("::", 1)[-1]
        if "::" in replacement:
            if base == replacement or base.startswith(replacement + "::"):
                return True
        elif name == replacement and path in packet_test_files:
            return True
    return False


def check(touched: dict[str, str], collected: set[str], rows: list[dict[str, str]], packet: str,
          packet_test_files: set[str] | None = None) -> list[str]:
    """touched: {path: git status letter (M or D)} for pre-existing watched files; collected: pytest node ids at head;
    packet_test_files: every test file this packet's diff added or modified (A or M)."""
    problems = []
    files = set(packet_test_files or ())
    covered: dict[str, list[dict[str, str]]] = {}
    for row in (r for r in rows if r["packet"] == packet):
        for path in expand(row["v1_test"]):
            covered.setdefault(path, []).append(row)
    for path, status in sorted(touched.items()):
        rows_for = covered.get(path)
        if not rows_for:
            problems.append(f"{path} ({status}) is a pre-existing test/roster file with no migration-map row for {packet}")
            continue
        for row in rows_for:
            if row["action"] == "delete" and "::" not in row["v1_test"] and status != "D":
                problems.append(f"{path}: row says delete but the file is {status}, not deleted")
            if row["action"] == "rewrite" and not replacement_present(row["replacement"], collected, files):
                problems.append(f"{path}: replacement test {row['replacement']} is not a collected test in this packet's test files")
    return problems




def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True, timeout=60).stdout


def collect() -> set[str]:
    run = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts=", "-p", "no:cacheprovider"],
                         capture_output=True, text=True, timeout=600)
    if run.returncode not in (0, 5):
        raise RuntimeError(f"pytest collection failed (exit {run.returncode}): {run.stdout[-500:]}")
    return {l.strip() for l in run.stdout.splitlines() if "::" in l}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--packet", required=True)
    ap.add_argument("--map", default="docs/v2/migration-map.tsv")
    a = ap.parse_args(argv)
    try:
        status = git("diff", "--name-status", "--no-renames", a.base, a.head, "--", *WATCHED)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"migration_gate: git failed: {exc}", file=sys.stderr)
        return 2
    touched, packet_files = {}, set()
    for line in status.splitlines():
        code, _, path = line.partition("\t")
        if code[:1] in {"A", "M"} and path.startswith("tests/"):
            packet_files.add(path)
        if code[:1] in {"M", "D"}:
            touched[path] = code[:1]
    rows = load_rows(Path(a.map))
    try:
        collected = collect() if touched else set()
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        print(f"migration_gate: {exc}", file=sys.stderr)
        return 2
    problems = check(touched, collected, rows, a.packet, packet_files)
    for p in problems:
        print(p)
    print(f"migration_gate: packet {a.packet}: {len(touched)} pre-existing files touched, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
