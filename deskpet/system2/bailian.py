"""Qwen4-Max thinking and tool continuation inside one admitted turn."""

from collections.abc import Iterable, Iterator
import json


def _sse_events(lines: Iterable[str]) -> Iterator[list[str]]:
    event = []
    for line in lines:
        if line:
            event.append(line)
        elif event:
            yield event
            event = []
    if event:
        yield event


class BailianTurn:
    def __init__(self) -> None:
        self._reasoning: dict[frozenset[str], str] = {}

    def prepare(self, body: dict) -> None:
        # Qwen4-Max counts thinking and the answer against the shared output cap.
        body.update(enable_thinking=True, preserve_thinking=True, thinking_budget=1024)
        if (max_tokens := body.pop("max_tokens", None)) is not None:
            body.setdefault("max_completion_tokens", max_tokens)
        # The provider accepts earlier turns without reasoning_content.
        for message in reversed(body["messages"]):
            if message["role"] == "user":
                break
            calls = message.get("tool_calls")
            if message["role"] == "assistant" and isinstance(calls, list):
                ids = frozenset(call["id"] for call in calls)
                if reasoning := self._reasoning.get(ids):
                    message["reasoning_content"] = reasoning

    def stream(self, lines: Iterable[str]) -> Iterator[bytes]:
        reasoning: list[str] = []
        call_ids: dict[int, str] = {}
        for event in _sse_events(lines):
            data = "\n".join(line[5:].lstrip(" ") for line in event if line.startswith("data:"))
            if data and data != "[DONE]":
                frame = json.loads(data)
                for choice in frame.get("choices", []):
                    delta = choice.get("delta") or {}
                    if fragment := delta.pop("reasoning_content", None):
                        reasoning.append(fragment)
                    for call in delta.get("tool_calls") or []:
                        if call_id := call.get("id"):
                            index = call["index"]
                            call_ids[index] = call_ids.get(index, "") + call_id
                    if reasoning and call_ids and choice.get("finish_reason") == "tool_calls":
                        # Keep reasoning in the host. DSH receives only the tool calls and answer.
                        self._reasoning[frozenset(call_ids.values())] = "".join(reasoning)
                event = [line for line in event if not line.startswith("data:")]
                event.append("data: " + json.dumps(frame, ensure_ascii=False))
            yield ("\n".join(event) + "\n\n").encode("utf-8")
