"""Background sweep so no-show reservations vacate without an incoming call."""

from __future__ import annotations

import threading


class TimeoutSweeper:
    """Calls :meth:`Arbiter.sweep_timeouts` on a fixed cadence."""

    def __init__(self, arbiter, interval_seconds: float = 30.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("sweep interval must be positive")
        self._arbiter = arbiter
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="berth-timeout-sweep", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._wait():
            try:
                self._arbiter.sweep_timeouts()
            except Exception:  # pragma: no cover - defensive daemon boundary
                pass

    def _wait(self) -> bool:
        return self._stop.wait(self._interval)
