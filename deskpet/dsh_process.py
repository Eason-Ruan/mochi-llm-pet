"""Authenticated runtime subprocess plumbing shared by the two DSH profiles."""

from __future__ import annotations

import json
import os
from pathlib import Path
from queue import Empty, Queue
import signal
import subprocess
import sys
from threading import Event, Lock, Thread
import time

from .dsh_profile import isolated_environment
from .observation_v2.routes import CloudRouteError

_STOP = object()


class SDKWorker:
    def __init__(self, home: Path, token: str, *, label: str) -> None:
        self._label = label
        if not sys.executable:
            raise CloudRouteError(f"{self._label} requires a Python executable")
        worker = Path(__file__).with_name("dsh_worker.py")
        self._process = subprocess.Popen(
            [sys.executable, "-I", "-u", str(worker)], cwd=home,
            env=isolated_environment(home, token), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
            start_new_session=os.name == "posix",
        )
        self._stdin_lock = Lock()
        self._output: Queue = Queue()
        self._reader = Thread(target=self._read, daemon=True, name="mochi-dsh-sdk")
        self._reader.start()

    def _read(self) -> None:
        assert self._process.stdout is not None
        try:
            for line in self._process.stdout:
                self._output.put(json.loads(line))
        except (ValueError, OSError):
            pass
        finally:
            self._output.put(_STOP)

    def _write(self, value: dict) -> None:
        assert self._process.stdin is not None
        with self._stdin_lock:
            self._process.stdin.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
            self._process.stdin.flush()

    def request(self, value: dict, *, timeout: float, expected: str, cancelled: Event | None = None) -> dict:
        expires = time.monotonic() + timeout
        write_done: Queue = Queue(1)

        def write() -> None:
            try:
                self._write(value)
            except (OSError, TypeError, ValueError):
                write_done.put(False)
            else:
                write_done.put(True)

        def receive(queue: Queue):
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise CloudRouteError(f"{self._label} runtime was closed")
                remaining = expires - time.monotonic()
                if remaining <= 0:
                    raise CloudRouteError(f"{self._label} runtime timed out")
                try:
                    return queue.get(timeout=min(0.1, remaining))
                except Empty:
                    continue

        Thread(target=write, daemon=True, name="mochi-dsh-sdk-input").start()
        if not receive(write_done):
            raise CloudRouteError(f"{self._label} runtime is unavailable")
        response = receive(self._output)
        if response is _STOP or not isinstance(response, dict):
            raise CloudRouteError(f"{self._label} runtime exited unexpectedly")
        if response.get("kind") != expected:
            if response.get("error") == "ModuleNotFoundError":
                raise CloudRouteError(f"The {self._label} runtime is unavailable on this platform")
            raise CloudRouteError(f"{self._label} runtime could not complete the request")
        return response

    def close(self, *, in_flight: bool, abort: bool = False) -> None:
        process = self._process

        def kill() -> None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass

        try:
            if process.poll() is None:
                if abort:
                    kill()
                else:
                    self._write({"kind": "close"})
                    process.wait(1.5 if in_flight else 4.0)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            if process.poll() is None:
                kill()
        finally:
            process.wait()
            self._reader.join(timeout=1)
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
