"""In-process P1 work sessions, owned by the host's service thread.

All timing comes from the injected Clock. Persistence and full work/break
cycles are separate milestones; this adapter implements the public v1 port.
"""
from __future__ import annotations

from datetime import timedelta
import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from deskpet.contracts import ErrorCode, SessionCommand, SessionResult, WorkSession


class StartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=500)
    duration_s: float = Field(default=1500, gt=0, allow_inf_nan=False)
    task_id: str | None = None


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirmed_outcomes: list[str] | tuple[str, ...] = ()
    next_steps: list[str] | tuple[str, ...] = ()


class CWorkSessionService:
    def __init__(self, *, clock, emit):
        self.clock = clock
        self.emit = emit
        self._views: dict[str, WorkSession] = {}
        self._deadlines: dict[str, float] = {}
        self._commands: dict[str, tuple[str, SessionResult]] = {}

    def get(self, session_id: str) -> WorkSession | None:
        self.pump()
        return self._views.get(session_id)

    def list_sessions(self) -> list[WorkSession]:
        self.pump()
        return list(self._views.values())

    def apply(self, command: SessionCommand) -> SessionResult:
        # Resolve expiry before version/state validation, including at the boundary.
        self.pump()
        fingerprint = json.dumps(command.model_dump(mode="json"), sort_keys=True,
                                 separators=(",", ":"), ensure_ascii=False)
        previous = self._commands.get(command.command_id)
        if previous is not None:
            if fingerprint == previous[0]:
                result = previous[1].model_copy(update={
                    "status": "duplicate", "error_code": ErrorCode.DUPLICATE,
                })
            else:
                result = SessionResult(command_id=command.command_id, status="rejected",
                    error_code=ErrorCode.INVALID_ARGUMENT, view=self._views.get(command.session_id))
            self._record(command, result)
            return result

        current = self._views.get(command.session_id)
        error = None
        view = current
        if command.kind == "start":
            if current is not None:
                error = ErrorCode.INVALID_TRANSITION
            elif command.expected_version != 0:
                error = ErrorCode.STALE_TASK
            else:
                try:
                    args = StartInput.model_validate(command.payload)
                    if not args.goal.strip():
                        raise ValueError("goal cannot be blank")
                    due_at = self.clock.wall_now() + timedelta(seconds=args.duration_s)
                    deadline = self.clock.monotonic_now() + args.duration_s
                    view = WorkSession(session_id=command.session_id, version=1,
                        goal=args.goal.strip(), task_id=args.task_id, remaining_s=args.duration_s,
                        phase_due_at=due_at)
                    self._deadlines[command.session_id] = deadline
                except (ValidationError, ValueError, OverflowError):
                    error = ErrorCode.INVALID_ARGUMENT
        elif current is None:
            error = ErrorCode.NOT_FOUND
        elif command.expected_version != current.version:
            error = ErrorCode.STALE_TASK
        else:
            try:
                view, error = self._change(command, current)
            except (ValidationError, ValueError, OverflowError):
                error = ErrorCode.INVALID_ARGUMENT

        if error is None:
            self._views[command.session_id] = view
        result = SessionResult(command_id=command.command_id,
            status="rejected" if error else "applied", error_code=error, view=view)
        self._commands[command.command_id] = (fingerprint, result)
        self._record(command, result)
        return result

    def _change(self, command, current):
        if command.kind in ("pause", "resume"):
            if command.payload:
                return current, ErrorCode.INVALID_ARGUMENT
            expected_state = "focusing" if command.kind == "pause" else "paused"
            if current.state != expected_state:
                return current, ErrorCode.INVALID_TRANSITION
            if command.kind == "pause":
                self._deadlines.pop(command.session_id, None)
                updates = {"state": "paused", "phase_due_at": None}
            else:
                # Compute everything before mutating the schedule.
                due_at = self.clock.wall_now() + timedelta(seconds=current.remaining_s)
                self._deadlines[command.session_id] = self.clock.monotonic_now() + current.remaining_s
                updates = {"state": "focusing", "phase_due_at": due_at}
        else:  # Public SessionCommand restricts this branch to end.
            if current.state == "ended":
                return current, ErrorCode.INVALID_TRANSITION
            args = ReviewInput.model_validate(command.payload)
            updates = {"state": "ended", "phase_due_at": None,
                       "confirmed_outcomes": tuple(args.confirmed_outcomes),
                       "next_steps": tuple(args.next_steps)}
            self._deadlines.pop(command.session_id, None)
        return current.model_copy(update={**updates, "version": current.version + 1}), None

    def pump(self) -> None:
        now = self.clock.monotonic_now()
        for session_id, deadline in list(self._deadlines.items()):
            current = self._views[session_id]
            remaining = max(0.0, deadline - now)
            updates = {"remaining_s": remaining}
            if remaining == 0:
                updates.update(state="break_due", version=current.version + 1, phase_due_at=None)
                del self._deadlines[session_id]
            self._views[session_id] = current.model_copy(update=updates)
            if remaining == 0:
                self.emit("session.break_due", session_id=session_id,
                          payload={"version": current.version + 1, "state": "break_due"})

    def _record(self, command, result):
        self.emit("session." + result.status, session_id=command.session_id, payload={
            "command_id": command.command_id, "kind": command.kind,
            "version": result.view.version if result.view else None,
            "state": result.view.state if result.view else None,
            "error_code": result.error_code.value if result.error_code else None,
        })


def factory(*, clock, emit):
    return CWorkSessionService(clock=clock, emit=emit)
