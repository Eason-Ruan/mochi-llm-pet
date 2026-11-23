"""Messages shared by the System 2 host, model engine, and desktop."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class TurnRequest:
    admission_id: str
    step_id: str
    system_prompt: str
    input_json: str
    user_scope: str
    prompt_version: str
    source: str
    allowed_tools: tuple[str, ...] | None = None
    expires_at: datetime | None = None
    observation_generation: int | None = None
    observation_privacy_marker: tuple[str | None, int, bool | None] | None = None
    observation_document_query: bool = False


@dataclass(frozen=True, slots=True)
class EngineReply:
    text: str
    answer_id: str | None = None
    session_id: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ToolAuthority:
    admission_id: str
    step_id: str
    call_id: str
    intent_generation: int
    user_scope: str
    origin: str
    trigger_id: str
    created_at: datetime
    expires_at: datetime
    subject_refs: tuple[str, ...] = ()
    context_revision: int | None = None
    observation_cursor: int | None = None
    observation_generation: str | None = None
    observation_valid_until: datetime | None = None
    opportunity_id: str | None = None
    proposal_id: str | None = None
    expected_versions: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UiEvent:
    sequence: int
    kind: str
    answer_id: str
    reply_to_message_id: str
    payload: dict[str, Any]
