"""Deployment and release helpers for Flightctl.

The module is deliberately stdlib-only. Product packages can inject their
real authority, executor, client, adapter, and watcher implementations once
the assembled release exists.
"""

