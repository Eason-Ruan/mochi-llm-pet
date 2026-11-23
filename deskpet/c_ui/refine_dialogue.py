"""Fixed local preview conversation for the Figma user journeys."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import re
from uuid import uuid4

from .time_format import local_time

GOAL = "完成论文的方法部分"
MILK_REQUEST = "把买牛奶延后到明晚六点。"
MILK_CONFIRM = "确认，明晚六点提醒我。"
DEMO_HINT = "这是对话演示。可以输入「把买牛奶延后到明晚六点」或「先暂停这段专注」。"

@dataclass(frozen=True)
class DemoTurn:
    text: str
    follow_up: bool = False
    action: dict | None = None


def _normalize(text: str) -> str:
    return re.sub(r"[\s，,。！？?!；;]", "", text).replace("：", ":")


class DemoConversation:
    """Recognizes the original scripted journeys and writes via existing UiAction."""

    def __init__(self):
        self.confirmation: DemoTurn | None = None

    @staticmethod
    def _task(snapshot: dict, title: str) -> dict | None:
        return next((task for task in snapshot.get("tasks", ())
                     if task["status"] == "registered" and task["draft"]["content"] == title), None)

    @staticmethod
    def _reminder(verb: str, task: dict, payload: dict | None = None) -> dict:
        return {
            "event_id": uuid4().hex, "action_id": f"reminder.{verb}", "task_id": task["task_id"],
            "expected_task_version": task["version"], "surface_revision": task["version"],
            "payload": payload or {},
        }

    @staticmethod
    def _session(verb: str, current: dict, payload: dict | None = None) -> dict:
        return {
            "event_id": uuid4().hex, "action_id": f"session.{verb}",
            "surface_revision": max(1, current["version"]),
            "payload": {"session_id": current["session_id"],
                        "expected_session_version": current["version"], **(payload or {})},
        }

    def _milk(self, snapshot: dict) -> DemoTurn:
        task = self._task(snapshot, "买牛奶")
        if task is None:
            return DemoTurn("活跃提醒里没有「买牛奶」。", True)
        now = local_time(snapshot["now"])
        target = (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
        action = self._reminder("edit", task, {
            "content": task["draft"]["content"], "mode": "hard", "earliest_at": target.isoformat(),
        })
        self.confirmation = DemoTurn("已将「买牛奶」延后至明天 18:00。", action=action)
        return DemoTurn("可以。明天 18:00 到点提醒你买牛奶，确认这个时间吗？", True)

    def _today(self, snapshot: dict) -> DemoTurn:
        now = local_time(snapshot["now"])
        rows = []
        for task in snapshot.get("tasks", ()):
            if task["status"] != "registered":
                continue
            draft = task["draft"]
            first = local_time(draft["earliest_at"])
            last = local_time(draft["latest_first_delivery_at"])
            if not first.date() <= now.date() <= last.date():
                continue
            time = first.strftime("%H:%M")
            if draft["mode"] == "soft":
                time = f"{time}–{last:%H:%M}" if first.date() == last.date() else f"截至 {last:%H:%M}"
            rows.append((first, f"{time} {draft['content']}"))
        return DemoTurn("，".join(row for _, row in sorted(rows)) + "。" if rows else "今天没有活跃提醒。", True)

    def _work(self, verb: str, snapshot: dict) -> DemoTurn:
        current = next((session for session in reversed(snapshot.get("sessions", ()))
                        if session["state"] != "ended"), None)
        if verb == "start":
            if current:
                return DemoTurn("已有一段进行中的专注。", True)
            report = self._task(snapshot, "交小组报告")
            payload = {"goal": GOAL, "duration_s": 1500}
            if report:
                payload["task_id"] = report["task_id"]
            action = self._session(verb, {"session_id": uuid4().hex, "version": 0}, payload)
            return DemoTurn(f"已开始 25 分钟专注「{GOAL}」。", action=action)
        if current is None:
            return DemoTurn("当前没有进行中的专注。", True)
        state = current["state"]
        if (verb, state) == ("pause", "paused"):
            return DemoTurn("这段专注已经暂停。", True)
        if (verb, state) == ("resume", "focusing"):
            return DemoTurn("这段专注正在计时。", True)
        if verb in {"pause", "resume"} and state == "break_due":
            return DemoTurn("专注计时已结束，可以输入「结束专注」进入回顾。", True)
        goal = current["goal"]
        text = {
            "pause": f"已暂停「{goal}」。",
            "resume": f"已继续「{goal}」。",
            "end": f"已结束「{goal}」。可以在专注与回顾里查看记录。",
        }[verb]
        return DemoTurn(text, action=self._session(verb, current))

    def reply(self, text: str, snapshot: dict) -> DemoTurn:
        phrase = _normalize(text)
        if not snapshot.get("now"):
            return DemoTurn("演示正在启动，请稍后再试。", True)
        if self.confirmation:
            confirmed, self.confirmation = self.confirmation, None
            if phrase in {"是", "确认", "确认延后", "确认这个时间", "确认明晚六点提醒我",
                          "确认明晚6点提醒我", "好", "好的"}:
                target = local_time(confirmed.action["payload"]["earliest_at"])
                now = local_time(snapshot["now"])
                if target <= now:
                    return DemoTurn("原先确认的时间已经过去。请重新安排提醒。", True)
                if target.date() == now.date():
                    return DemoTurn("已将「买牛奶」延后至今天 18:00。", action=confirmed.action)
                return confirmed
            if phrase in {"否", "不用", "取消", "不改了", "不要延后"}:
                return DemoTurn("好，保留原提醒时间。")
        if re.fullmatch(r"(请|帮我)?把?买牛奶(延后|改|推迟)到(明晚(六|6)点|明天18:00)", phrase):
            return self._milk(snapshot)
        if phrase in {"今天还有什么提醒", "今天有什么提醒", "有哪些提醒"}:
            return self._today(snapshot)
        if phrase in {"今天五点提醒我交报告", "今天17:00提醒我交报告"}:
            report = self._task(snapshot, "交小组报告")
            if report:
                due = local_time(report["draft"]["earliest_at"])
                return DemoTurn(f"「交小组报告」已登记，{due:%H:%M} 提醒你。", True)
            return DemoTurn("活跃提醒里没有「交小组报告」。", True)
        if phrase in {"我已经交了小组报告", "交小组报告已完成", "完成交小组报告"}:
            report = self._task(snapshot, "交小组报告")
            if report:
                return DemoTurn("已完成「交小组报告」。", action=self._reminder("done", report))
            return DemoTurn("活跃提醒里没有「交小组报告」。", True)
        work = re.fullmatch(r"(先|请|帮我)?(开始|暂停|继续|结束)(这段|本次)?(25分钟)?专注", phrase)
        if work:
            verb = {"开始": "start", "暂停": "pause", "继续": "resume", "结束": "end"}[work[2]]
            return self._work(verb, snapshot)
        return DemoTurn(DEMO_HINT, True)


class LocalConversation:
    """Recognize explicit work controls when the model connection is offline."""

    def reply(self, text: str, snapshot: dict) -> DemoTurn:
        phrase, _separator, title = text.replace("：", ":", 1).partition(":")
        phrase = _normalize(phrase)
        phase = snapshot.get("active_phase")
        start = re.fullmatch(
            r"(?:请|帮我)?开始(?:(?:一段|一轮)?(?:专注|番茄钟)(?:(\d+)分钟)?|(\d+)分钟(?:专注|番茄钟)?)",
            phrase,
        )
        if start:
            if phase is not None:
                return DemoTurn("已有一段计时。可以在专注面板查看或调整。", True)
            minutes = start[1] or start[2]
            payload = {"title": title.strip()}
            if minutes is not None:
                if not 1 <= int(minutes) <= 24 * 60:
                    return DemoTurn("请输入 1 到 1440 分钟。", True)
                payload["duration_s"] = int(minutes) * 60
            return self._action("start", uuid4().hex, 0, payload, "已开始专注。")

        change = re.fullmatch(r"(?:请|帮我)?(?:把专注)?(?:改成|再加)(\d+)分钟(?:专注)?", phrase)
        command = re.fullmatch(r"(?:请|帮我|先)?(暂停|继续|结束|跳过)(?:这段|本次)?(?:专注|计时|休息)?", phrase)
        if change or command:
            if phase is None:
                return DemoTurn("当前没有进行中的计时。", True)
            phase_id, version = phase["id"], phase["version"]
            if change:
                minutes = int(change[1])
                if not 1 <= minutes <= 24 * 60:
                    return DemoTurn("请输入 1 到 1440 分钟。", True)
                field = "total_s" if "改成" in phrase else "add_s"
                return self._action("adjust", phase_id, version, {field: minutes * 60}, "已调整计时时长。")
            verb, reply = {
                "暂停": ("pause", "已暂停计时。"),
                "继续": ("resume", "已继续计时。"),
                "结束": ("end", "已结束计时。"),
                "跳过": ("skip_break", "已跳过休息。"),
            }[command[1]]
            return self._action(verb, phase_id, version, {}, reply)

        return DemoTurn("离线时可以输入「开始专注」「开始 25 分钟」「暂停专注」或「再加 10 分钟」。", True)

    @staticmethod
    def _action(verb, phase_id, version, payload, reply):
        return DemoTurn(reply, True, {
            "event_id": uuid4().hex,
            "action_id": f"session.{verb}",
            "surface_revision": max(1, version),
            "payload": {"session_id": phase_id, "expected_session_version": version, "_origin": "chat", **payload},
        })
