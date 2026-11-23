"""Optional in-process accounting for completed Observation responses."""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ObservationUsage:
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def report_usage(
    observer: Callable[[ObservationUsage], None] | None, *, model: str,
    prompt: object = None, completion: object = None, total: object = None,
) -> None:
    if observer is None:
        return
    try:
        observer(ObservationUsage(model, _count(prompt), _count(completion), _count(total)))
    except Exception:
        # Accounting is optional and must not invalidate an observation.
        pass
