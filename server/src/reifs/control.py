"""Cooperative cancellation of long operations (scans, job runs)."""

from __future__ import annotations

from typing import Callable

StopFn = Callable[[], bool]


class Stopped(Exception):
    """Raised inside a scan or job run when a stop was requested."""


def check(stop: StopFn | None) -> None:
    if stop is not None and stop():
        raise Stopped("stopped on request")
