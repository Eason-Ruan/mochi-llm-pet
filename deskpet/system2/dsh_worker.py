"""Run the official SDK in a process whose environment the Mochi host controls."""

from __future__ import annotations

import json
import sys
from time import monotonic


def reply(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=True, separators=(",", ":")), flush=True)


def main() -> None:
    try:
        from deepseek_harness import DeepSeekHarness
        from deepseek_harness_runtime import bundled_runtime_path

        config = json.loads(sys.stdin.readline())
        with DeepSeekHarness(dsh_bin=str(bundled_runtime_path()), **config["harness"]) as harness:
            sid = config["session_id"]
            session = harness.resume_session(sid) if config["resume"] else harness.start_session(sid)
            session.get_working_directory()
            reply({"kind": "ready"})
            for line in sys.stdin:
                request = json.loads(line)
                if request["kind"] == "close":
                    break
                if request["kind"] != "run":
                    raise ValueError("Unsupported Mochi SDK worker request")
                started = monotonic()
                result = session.run(request["text"])
                elapsed = monotonic() - started
                answer_id = None
                for event in reversed(result.events):
                    if event.get("type") != "assistant/message":
                        continue
                    message = event.get("data", {}).get("message", {})
                    if any(block.get("type") == "text" and block.get("text")
                           for block in message.get("content", [])):
                        answer_id = message.get("id")
                        break
                reply({
                    "kind": "result", "text": result.final_response, "answer_id": answer_id,
                    "finish_reason": result.finish_reason,
                    "metadata": metadata(result.events, sid, elapsed),
                })
    except Exception as error:
        reply({"kind": "failure", "error": type(error).__name__})


def metadata(events: list[dict], session_id: str, elapsed: float) -> dict:
    usage_keys = {
        "inputTokens": "input_tokens", "outputTokens": "output_tokens", "totalTokens": "total_tokens",
        "cacheReadTokens": "cache_read_tokens", "cacheWriteTokens": "cache_write_tokens",
    }
    usage = {}
    tools = []
    for event in events:
        data = event.get("data", {})
        event_type = event.get("type")
        if event_type == "assistant/message":
            counts = data.get("usage") or {}
            for field, key in usage_keys.items():
                count = counts.get(field)
                if type(count) is int and count >= 0:
                    usage[key] = usage.get(key, 0) + count
        elif event_type == "tool/call":
            tools.append({
                "name": data.get("name"),
                "call_id": data.get("callId"),
                "step_id": f'{session_id}:{data.get("turn")}:{data.get("step")}',
            })
    return {"duration_s": round(elapsed, 3), "token_usage": usage, "tool_calls": tools}


if __name__ == "__main__":
    main()
