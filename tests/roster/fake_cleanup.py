#!/usr/bin/env python3
from __future__ import annotations

import os

from shims import cleanup_main


os.environ["ROSTER_SHIM_MODE"] = "cleanup"
raise SystemExit(cleanup_main())
