#!/usr/bin/env python3
"""Reject machine-specific paths and non-portable private address literals.

The scanner walks the checkout, including untracked files.  RFC1918 and
CGNAT examples are allowed only in the exact ``config/*.example`` namespace.
Other private/reserved ranges belong to the later deployment scanner.
"""

from __future__ import annotations

import ipaddress
import re
import sys
from pathlib import Path


_IP_RE = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
_PATH_RE = re.compile(r"/(?:home|root|opt|srv|mnt|var)/[A-Za-z0-9_.-]+")


def _network(first: tuple[int, int, int, int], prefix: int) -> ipaddress.IPv4Network:
    return ipaddress.ip_network((int(ipaddress.IPv4Address(bytes(first))), prefix))


_PORTABLE_PRIVATE_RANGES = (
    _network((10, 0, 0, 0), 8),
    _network((172, 16, 0, 0), 12),
    _network((192, 168, 0, 0), 16),
    _network((100, 64, 0, 0), 10),
)


def _is_config_example(path: str) -> bool:
    parts = Path(path).as_posix().split("/")
    return len(parts) == 2 and parts[0] == "config" and parts[1].endswith(".example")


def scan_text(text: str, path: str) -> list[str]:
    findings: list[str] = []
    private_allowed = _is_config_example(path)
    for match in _IP_RE.finditer(text):
        literal = match.group(0)
        try:
            address = ipaddress.ip_address(literal)
        except ValueError:
            continue
        if any(address in network for network in _PORTABLE_PRIVATE_RANGES):
            if not private_allowed:
                findings.append(f"{path}: RFC1918/CGNAT address literal outside config example")
    if _PATH_RE.search(text):
        findings.append(f"{path}: machine path literal")
    return findings


def scan_tree(root: Path) -> list[str]:
    findings: list[str] = []
    ignored = {".git", ".venv", "__pycache__", ".pytest_cache"}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in ignored for part in path.relative_to(root).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(scan_text(text, path.relative_to(root).as_posix()))
    return findings


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    root = Path(args[0]) if args else Path(".")
    findings = scan_tree(root)
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
