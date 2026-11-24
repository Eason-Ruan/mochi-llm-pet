"""Interrupt a blocking Observation model read when its native turn stops."""

from __future__ import annotations

from collections.abc import Callable
import socket
from threading import Event, Thread

import httpcore


class CancellableRequest:
    def __init__(self, cancelled: Callable[[], bool] | None) -> None:
        self._cancelled = cancelled
        self._socket: socket.socket | None = None
        self._finished = Event()
        self._watcher: Thread | None = None

    def __enter__(self) -> CancellableRequest:
        if self._cancelled is not None and httpcore.__version__ != "1.0.9":
            raise RuntimeError("Unsupported HTTP transport version for Observation cancellation")
        return self

    def __exit__(self, *_):
        self._finished.set()
        if self._watcher is not None:
            self._watcher.join(timeout=0.2)

    def trace(self, event: str, info: dict) -> None:
        if self._cancelled is None or event not in {"connection.connect_tcp.complete", "connection.start_tls.complete"}:
            return
        self._socket = info["return_value"].get_extra_info("socket")
        if self._watcher is None:
            self._watcher = Thread(target=self._watch, daemon=True, name="mochi-observation-http")
            self._watcher.start()

    def _watch(self) -> None:
        assert self._cancelled is not None
        while not self._finished.wait(0.05):
            conn = self._socket
            if conn is not None and self._cancelled():
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    # The TCP socket can be replaced during the TLS upgrade.
                    continue
                return
