#!/usr/bin/env python3
from __future__ import annotations

import os

from shims import client_main


os.environ["ROSTER_SHIM_MODE"] = "client"
raise SystemExit(client_main())
