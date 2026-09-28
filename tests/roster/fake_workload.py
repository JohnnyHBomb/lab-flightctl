#!/usr/bin/env python3
from __future__ import annotations

import os

from shims import workload_main


os.environ["ROSTER_SHIM_MODE"] = "workload"
raise SystemExit(workload_main())
