#!/usr/bin/env python3
"""Run the portable-tree check and a fail-closed external denylist scan.

The denylist is deliberately supplied out of tree. Its entries are never
printed, including when a tracked file matches one.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _load_denylist(path: Path) -> list[bytes]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("denylist is absent or unreadable")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("denylist is absent or unreadable") from exc
    if not raw:
        raise ValueError("denylist is empty")
    entries: list[bytes] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(b"#"):
            continue
        entries.append(stripped)
    if not entries:
        raise ValueError("denylist is empty")
    return entries


def _tracked_files(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("tracked-file enumeration failed") from exc
    if result.returncode != 0:
        raise ValueError("tracked-file enumeration failed")
    return [root / item.decode("utf-8", "surrogateescape") for item in result.stdout.split(b"\0") if item]


def scan_denylist(root: Path, denylist: Path) -> bool:
    entries = _load_denylist(denylist)
    for path in _tracked_files(root):
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ValueError("tracked file is unreadable") from exc
        if any(entry in content for entry in entries):
            return False
    return True


def _portable_scan(root: Path) -> int:
    checker = root / "tools" / "check_portability.py"
    if not checker.is_file():
        print("portable-tree check is unavailable", file=sys.stderr)
        return 1
    try:
        result = subprocess.run(
            [sys.executable, str(checker), str(root)],
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        print("portable-tree check failed to return", file=sys.stderr)
        return 1
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="portable tree plus external denylist check")
    parser.add_argument("--denylist", required=True, type=Path)
    parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if _portable_scan(root) != 0:
        return 1
    try:
        clean = scan_denylist(root, args.denylist.resolve())
    except ValueError:
        print("external denylist check failed", file=sys.stderr)
        return 1
    if not clean:
        print("external denylist check failed", file=sys.stderr)
        return 1
    print("external denylist check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
