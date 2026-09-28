#!/usr/bin/env python3
from __future__ import annotations

import os

from shims import bridge_main


os.environ["ROSTER_SHIM_MODE"] = "bridge"
raise SystemExit(bridge_main())
