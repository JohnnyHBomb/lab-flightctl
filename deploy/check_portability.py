#!/usr/bin/env python3
"""Run the portable-tree check and a fail-closed external denylist scan.

The denylist is deliberately supplied out of tree. Its entries are never
printed, including when a tracked file matches one.

With ``--data-rev COMMIT`` the scan reads the committed tree of COMMIT from the
git repository at ROOT as git objects only: nothing is checked out and no code
from that tree is run or imported (the portable-tree check, which runs the
tree's own ``tools/check_portability.py``, is skipped). The trusted denylist
workflow uses this mode on pull-request content.
"""

from __future__ import annotations

import argparse
import re
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
        result = subprocess.run(  # noqa: S603 -- argv list with shell=False, no untrusted input
            ["git", "-C", str(root), "ls-files", "-z"],  # noqa: S607 -- fixed tool name resolved on PATH
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


def _git_objects(root: Path, args: list[str], stdin: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(  # noqa: S603 -- argv list with shell=False; the revision is a validated commit id
            ["git", "-C", str(root), *args],  # noqa: S607 -- fixed tool name resolved on PATH
            input=stdin,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("git object read failed") from exc
    if result.returncode != 0:
        raise ValueError("git object read failed")
    return result.stdout


def _committed_blobs(root: Path, rev: str) -> list[bytes]:
    """Every blob in the tree of commit ``rev``, read with ls-tree and cat-file --batch (no checkout, no filters)."""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", rev):
        raise ValueError("revision must be a full commit id")
    listing = _git_objects(root, ["ls-tree", "-r", "-z", "--full-tree", rev + "^{commit}"])
    oids: list[bytes] = []
    seen: set[bytes] = set()
    for record in listing.split(b"\0"):
        if not record:
            continue
        meta, _, _ = record.partition(b"\t")
        fields = meta.split()
        if len(fields) == 3 and fields[1] == b"blob" and fields[2] not in seen:
            seen.add(fields[2])
            oids.append(fields[2])
    output = _git_objects(root, ["cat-file", "--batch"], stdin=b"".join(oid + b"\n" for oid in oids))
    blobs: list[bytes] = []
    offset = 0
    for oid in oids:
        end = output.find(b"\n", offset)
        header = output[offset:end].split() if end >= 0 else []
        if len(header) != 3 or header[0] != oid or header[1] != b"blob" or not header[2].isdigit():
            raise ValueError("git object read failed")
        size = int(header[2])
        blobs.append(output[end + 1 : end + 1 + size])
        offset = end + 1 + size + 1
        if offset > len(output):
            raise ValueError("git object read failed")
    return blobs


def scan_denylist_rev(root: Path, rev: str, denylist: Path) -> bool:
    entries = _load_denylist(denylist)
    return not any(entry in blob for blob in _committed_blobs(root, rev) for entry in entries)


def _portable_scan(root: Path) -> int:
    checker = root / "tools" / "check_portability.py"
    if not checker.is_file():
        print("portable-tree check is unavailable", file=sys.stderr)
        return 1
    try:
        result = subprocess.run(  # noqa: S603 -- argv list with shell=False, no untrusted input
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
    parser.add_argument("--data-rev", metavar="COMMIT", help="scan this commit's tree as data only (no portable check)")
    parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.data_rev is None and _portable_scan(root) != 0:
        return 1
    try:
        if args.data_rev is None:
            clean = scan_denylist(root, args.denylist.resolve())
        else:
            clean = scan_denylist_rev(root, args.data_rev, args.denylist.resolve())
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
