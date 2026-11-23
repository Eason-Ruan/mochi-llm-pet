"""Bind model actions to explicit user input and the objects presented in its turn."""

from __future__ import annotations

from datetime import datetime, time, timedelta
import re
import unicodedata
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


READ_TOOLS = frozenset({
    "context.read", "item.get", "item.list", "phase.get", "habit.get", "habit.list",
    "settings.get", "command.get_result", "decision.list",
})
_IDENTIFIED_ACTIONS = {
    "phase.pause", "phase.resume", "phase.adjust", "phase.end", "phase.skip_break", "phase.set_links",
    "phase.credit_sleep", "phase.credit_recovery", "phase.reassign_interval", "phase.set_details", "phase.review",
    "item.update", "item.set_status", "item.archive", "reminder.configure",
    "reminder.snooze", "reminder.ack", "habit.update", "habit.archive", "proposal.resolve",
}
_CANCEL_START = re.compile(
    r"^(?:算了[，,、\s]*)?(?:别|不要|先别|不用|取消)\s*(?:再)?(?:开始|启动|开启|开)(?:[了吧。.!！\s]|专注|番茄|钟|计时|$)"
    r"|^(?:never\s+mind[,.\s]*)?(?:don't|do not|cancel)\s+(?:the\s+)?start\b", re.I,
)


def normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def ref_id(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("id", value.get("task_id", value.get("subject_id")))
    if isinstance(value, str) and value:
        kind, split, identity = value.partition(":")
        return identity if split and kind in {"item", "reminder", "occurrence", "phase", "habit", "proposal", "interval", "recovery"} else value
    return None


def ref_kind(value: object) -> str | None:
    if isinstance(value, dict):
        return value.get("kind", value.get("type"))
    if isinstance(value, str) and ":" in value:
        return value.partition(":")[0]
    return None


def cancels_start(utterance: str) -> bool:
    return bool(_CANCEL_START.search(normalized(utterance)))


def without_cancellation(utterance: str) -> str:
    head, *tail = re.split(r"[，,；;。]", utterance, maxsplit=1)
    if head.strip() in {"算了", "算了吧"} and tail and cancels_start(utterance):
        _, *tail = re.split(r"[，,；;。]", tail[0], maxsplit=1)
        return tail[0].strip() if tail else ""
    if cancels_start(head):
        return tail[0].strip() if tail else ""
    return utterance


def independent_intents(utterance: str) -> tuple[str, ...]:
    segments = tuple(part.strip() for part in re.split(
        r"[+＋；;]|[，,]\s*(?=再|另外|并且|然后|记(?:一)?下|记录|开始|取消)",
        utterance,
    ) if part.strip())
    if len(segments) < 2:
        return (utterance,)
    result = []
    for part in segments:
        if _explicit_action(normalized(part)) or cancels_start(part):
            result.append(part)
            continue
        part = re.sub(r"^(?:然后|另外|并且|再)\s*", "", part)
        if not _explicit_action(normalized(part)):
            return (utterance,)
        result.append(part)
    return tuple(result)


def start_request(utterance: str) -> bool:
    return _explicit_action(normalized(without_cancellation(utterance))) == "phase.start"


def _chinese_integer(raw: str) -> int:
    if raw.isascii():
        return int(raw)
    digits = "零一二三四五六七八九"
    number = current = 0
    for digit in raw:
        if digit in {"十", "百"}:
            number += (current or 1) * (10 if digit == "十" else 100)
            current = 0
        else:
            current = digits.index("二" if digit == "两" else digit)
    return number + current


def _seconds(text: str) -> int | None:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百]+)\s*"
                      r"(分钟|分(?:钟)?|小时|秒|hours?\b|hrs?\b|minutes?\b|mins?\b|seconds?\b|secs?\b)", text)
    if not match:
        return None
    raw = match[1]
    number = float(raw) if raw.isascii() else _chinese_integer(raw)
    unit = match[2]
    factor = 3600 if unit == "小时" or unit.startswith(("hour", "hr")) else 1 if unit == "秒" or unit.startswith("sec") else 60
    seconds = number * factor
    return int(seconds) if 0 < seconds <= 86400 and int(seconds) == seconds else None


def _mentions(text: str, candidates: list[dict], selected_ref: str | None = None) -> set[str]:
    matches = {
        value for candidate in candidates if (value := ref_id(candidate.get("id", candidate.get("task_id"))))
        and any(isinstance(candidate.get(key), str) and normalized(candidate[key]) in text
                for key in ("title", "label") if candidate.get(key))
    }
    if selected_ref:
        matches.add(selected_ref)
    return matches


def _explicit_action(text: str) -> str | None:
    request = re.match(r"^(?:能(?:不能)?|可不可以|可以|可否)(?:请)?(?:你|帮我)\s*|^(?:can|could|would) you\s+", text)
    if request:
        text = text[request.end():].rstrip("吗?？ ")
    text = re.sub(r"^(?:请(?:你|帮我)?|帮我)\s*", "", text)
    if (text.endswith(("?", "？")) and not request) or re.match(r"^(?:如果|假如|我刚才|怎么|如何|能否|what\b|how\b|why\b|if\b)", text):
        return None
    rules = (
        ("phase.start", r"^(?:请(?:帮我)?\s*|帮我\s*|现在\s*)?(?:开始|启动|开启|开|来个?|start\b|begin\b).*(?:专注|番茄|休息|分钟|小时|计时|focus\b|minutes?\b|min\b|pomodoro\b|break\b)"),
        ("phase.pause", r"^(?:请|帮我\s*)?(?:暂停(?:专注|休息|计时|番茄|一下|[。！! ]*$)|pause\b)"),
        ("phase.resume", r"^(?:请|帮我\s*)?(?:继续(?:专注|休息|计时|番茄|[。！! ]*$)|恢复(?:专注|休息|计时|番茄|[。！! ]*$)|resume\b)"),
        ("phase.end", r"^(?:请|帮我\s*)?(?:结束|停止|end\b|stop\b)(?:\s*(?:专注|计时|番茄|休息|focus\b|timer\b|break\b).*)?$"),
        ("phase.skip_break", r"^(?:请|帮我\s*)?(?:跳过\s*休息|skip (?:the )?break\b)"),
        ("phase.adjust", r"^(?:请|帮我\s*)?(?:(?:把)?(?:这次|本轮|本次|这轮|当前|专注|时间|计时).*?(?:改|调整)|改(?:成|为)|调整(?:为|到)|再加|延长|缩短|add\b|extend\b|change\b).*(?:分钟|小时|秒|min(?:ute)?s?\b|sec(?:ond)?s?\b)"),
        ("phase.set_links", r"^(?:请|帮我\s*)?(?:把.*?专注.*?关联|(?:取消|解除)?关联(?:到)?|link\b|unlink\b)"),
        ("phase.credit_sleep", r"^(?:请|帮我\s*)?(?:补记|补录|credit\b)"),
        ("phase.reassign_interval", r"^(?:把.*?(?:时间|工时|区间).*?(?:改归|改到|重归)|重分配.*?工时|reassign\b)"),
        ("phase.set_details", r"^(?:把.*?(?:专注|计时|番茄).*?(?:标题|备注|描述).*?(?:改|设)|修改(?:本轮|这次|当前)(?:专注|计时)?(?:标题|备注|描述)|rename (?:this |the )?(?:focus|session)\b)"),
        ("phase.review", r"^(?:记录|保存|更新)(?:本轮|这次|刚才)?(?:专注)?(?:复盘|总结)|^save (?:this |the )?review\b"),
        ("habit.create", r"^(?:创建|新建|新增|添加|建立)(?:个|一个)?(?:专注|番茄)?习惯|^create (?:a )?(?:focus )?habit\b"),
        ("habit.archive", r"^(?:归档|删除).*?(?:专注|番茄)?习惯|^archive (?:a |the )?(?:focus )?habit\b"),
        ("habit.update", r"^(?:修改|调整|把).*?习惯.*?(?:时长|分钟|改|设定)|^update (?:the )?(?:focus )?habit\b"),
        ("item.create", r"^(?:请|帮我\s*)?(?:记(?:一)?下|记录|添加(?:一条|一个)?|新建|创建(?:一条|一个)?事项|提醒我|remind me\b|add (?:an? )?(?:item|task|reminder)\b|note\b|remember\b)|^(?:有空|最迟|最晚|只在今天|仅在今天|今天|明天|后天|上午|下午|[0-9零一二两三四五六七八九十]+\s*[点时:]).{0,30}?提醒我"),
        ("reminder.configure", r"^(?:请|帮我\s*)?(?:给.*?(?:加|设).*?提醒|设置.*?提醒)"),
        ("reminder.snooze", r"^(?:请|帮我\s*)?(?:推迟|延后|稍后提醒|晚.*?提醒|snooze\b|postpone\b|[0-9零一二两三四五六七八九十百]+\s*(?:分钟|小时|秒)\s*(?:后\s*(?:准点|准时)|内\s*(?:别|不要|不再|不)\s*提醒))"),
        ("reminder.ack", r"^(?:请|帮我\s*)?(?:确认收到|收到提醒|acknowledge\b)"),
        ("item.set_status", r"^(?:请|帮我\s*)?(?:完成|标记.*?完成|重开|重新打开|取消|complete\b|reopen\b|cancel\b)"),
        ("item.archive", r"^(?:请|帮我\s*)?(?:归档|删除|archive\b|delete\b)"),
        ("item.update", r"^(?:请|帮我\s*)?(?:编辑|修改|把.*?(?:标题|描述|截止时间).*?(?:改|设)|edit\b|rename\b|update\b)"),
    )
    return next((action for action, pattern in rules if re.search(pattern, text)), None)


def _proposal_constraints(text: str) -> dict[str, Any] | None:
    if re.match(r"^(?:拒绝|不用|不要|不需要|先不|算了|不行|no\b|reject\b)", text):
        return {"answer": "reject"}
    if re.match(r"^(?:取消|cancel\b)", text):
        return {"answer": "cancel"}
    seconds = _seconds(text)
    if seconds is not None and re.search(r"(?:改|调整|换|start\b|change\b|开始|启动)", text):
        return {"answer": "accept", "amendments": {"planned_duration_s": seconds}}
    if re.match(r"^(?:好(?:的|啊|吧)?(?:$|[，。！\s])|可以|同意|确认|就这么办|开始吧|接受|yes\b|okay\b|ok\b|accept\b)", text):
        return {"answer": "accept", "amendments": {}}
    return None


def _settings_grant(text: str, selected_ref: str | None, context: dict) -> dict | None:
    if not re.match(r"^(?:请(?:帮我)?|帮我\s*)?(?:关闭|关掉|禁用|取消|不再|开启|打开|启用|启停|设置|本轮|这次|把|disable\b|enable\b|turn\b|set\b)", text):
        return None
    enabled = not re.search(r"(?:关闭|关掉|禁用|取消|不再|disable\b|\boff\b)", text)
    flags = {
        "auto_adjust": r"自动(?:调整|建议)|auto adjust",
        "habits_enabled": r"习惯(?:功能|模式|开关)|(?:开启|关闭|关掉|禁用|启用)习惯(?:[。！! ]|$)|habits?\b",
        "allow_proactive_movement": r"主动移动|proactive movement",
        "reminder_sound": r"提醒(?:声音|音效)|reminder sound",
        "phase_end_sound": r"(?:专注|阶段)结束(?:声音|音效)|phase end sound",
        "allow_system_notifications": r"系统通知|system notifications",
        "link_issues": r"关联事项|link issues",
    }
    patch = {key: enabled for key, pattern in flags.items() if re.search(pattern, text)}
    seconds = _seconds(text)
    if seconds and re.search(r"默认|default", text):
        if re.search(r"休息|break", text):
            patch["default_break_s"] = seconds
        elif re.search(r"专注|番茄|focus|pomodoro", text):
            patch["default_focus_s"] = seconds
    if re.search(r"习惯|habit", text):
        matches = _mentions(text, context.get("habits", []), selected_ref)
        if len(matches) == 1:
            if seconds and re.search(r"休息|break", text):
                behavior_name = "long_break_s" if re.search(r"长休息|long break", text) else "short_break_s"
                patch = {"behavior": {behavior_name: seconds}}
            elif seconds and re.search(r"专注|focus", text):
                patch = {"behavior": {"focus_s": seconds}}
            elif re.search(r"开(?:启|启用)|关(?:闭|掉)|禁用|启用|disable|enable", text):
                patch = {"status": "enabled" if enabled else "disabled"}
            else:
                return None
            return {"equal": {"scope": "habit"}, "patch": patch, "target": matches.pop()}
        if re.search(r"名为|叫做|这个习惯|named habit", text):
            return None
    if re.search(r"本轮|本次|这次|当前|this session", text):
        # The explicit phase tools handle phase durations and links. A default
        # toggle in this sentence must not update the global settings row.
        return None
    return {"equal": {"scope": "default"}, "patch": patch} if patch else None


def describe_grants(user: dict | None, context: dict) -> dict[str, dict]:
    if user is None:
        eligible = [ref_id(item["occurrence_id"]) for item in context["eligible_items"] if item.get("occurrence_id")]
        rechecks = [ref_id(item["occurrence_id"]) for item in context.get("recheck_items", ()) if item.get("occurrence_id")]
        reviewable = list(dict.fromkeys(eligible + rechecks))
        focus = context["enabled_settings"].get("auto_adjust", False)
        grants = {"pet.present": {}}
        if focus or reviewable:
            grants["observation.wait_fresh"] = {"occurrences": reviewable, "focus": focus}
        if eligible:
            grants["reminder.present"] = {"occurrences": eligible}
        if reviewable:
            grants["reminder.recheck"] = {"occurrences": reviewable}
        if focus and context["current_status"] == "fresh" and (context.get("current") or {}).get("state") == "available":
            phase = ref_id((context.get("current_phase") or {}).get("id"))
            grants["proposal.offer"] = {"target": phase, "background": True}
        return grants
    if button := user.get("proposal_button"):
        return {"proposal.resolve": {"target": button["id"], "equal": button,
                                     "amendments": button.get("amendments", {})}}
    text = normalized(user["utterance"] if user.get("reply_to_proposal_id") else without_cancellation(user["utterance"]))
    if not text:
        return {}
    selected = ref_id(user.get("selected_ref"))
    proposals = context["pending_proposals"]
    proposal = ref_id(user.get("reply_to_proposal_id"))
    response = _proposal_constraints(text)
    matching_proposal = None
    if proposal:
        matching_proposal = next((value for value in proposals if ref_id(value.get("id")) == proposal), None)
    elif len(proposals) == 1 and response and (
        re.fullmatch(r"(?:好的?|可以|同意|确认|就这么办|接受|拒绝|不用|不要|不需要|先不|算了|取消|yes|no|okay|ok|accept|reject)[。！! ]*", text)
        or re.search(r"(?:改|调整|换|change).*?(?:开始|启动|start\b)", text)
    ):
        matching_proposal = proposals[0]
        proposal = ref_id(matching_proposal.get("id"))
    if proposal and response:
        version = user.get("reply_to_proposal_version")
        if version is None and matching_proposal:
            version = matching_proposal.get("version")
        binding = {"target": proposal, "equal": response, "amendments": response.get("amendments", {})}
        if version is not None:
            binding["proposal_version"] = version
        return {"proposal.resolve": binding}
    if proposals and response and re.search(r"改.*?开始|change.*?start", text):
        return {}
    settings = _settings_grant(text, selected, context)
    if settings:
        return {"settings.update": settings}
    action = _explicit_action(text)
    if not action:
        return {}
    current = context.get("current_phase") or {}
    items = context.get("items", context["eligible_items"])
    selected_item = selected if any(item.get("id") == selected and item.get("type") == "issue" for item in items) else None
    issues = _mentions(text, [item for item in items if item.get("type") == "issue"], selected_item)
    equal: dict[str, Any] = {}
    seconds = _seconds(text)
    if action == "phase.start":
        equal["kind"] = ("long_break" if re.search(r"长休息|long break", text) else
                         "short_break" if re.search(r"休息|\bbreak\b", text) else "focus")
        if seconds:
            equal["planned_duration_s"] = seconds
        habits = _mentions(text, context.get("habits", []))
        default_duration = "default_focus_s" if equal["kind"] == "focus" else "default_break_s"
        return {action: {"equal": equal, "issues": sorted(issues), "habits": sorted(habits),
                         "default_duration": context["enabled_settings"].get(default_duration), "input": text}}
    if action == "item.create":
        title = re.match(r"^(?:请(?:帮我)?|帮我\s*)?(?:记(?:一)?下|记录|note\s+|remember\s+)(?:我)?(?:要)?\s*(\S.*?)\s*[。！!]?\s*$", text)
        equal["type"] = "reminder" if re.search(r"提醒|\bremind(?:er)?\b", text) else "issue"
        return {action: {"equal": equal, "title": title[1].strip('“”"') if title and equal["type"] == "issue" else None,
                         "reminder_constraints": _reminder_constraints(text, context), "input": text,
                         "deadline": bool(re.search(r"截止|ddl|deadline|due", text)),
                         "has_reminder": equal["type"] == "reminder" or bool(re.search(r"提醒|remind", text))}}
    if action == "habit.create":
        named = re.search(r'(?:叫做?|名为|named)\s*[“"「《]?([^“”"」》,，、。；;\n]+)', text)
        if not named:
            return {"proposal.offer": {"action": action}}
        return {action: {"title": named[1].strip(), "duration": seconds}}
    if action == "phase.adjust" and seconds:
        equal["add_s" if re.search(r"再加|延长|\badd\b|\bextend\b", text) else "total_s"] = seconds
    if action == "item.set_status":
        directive = re.sub(r"^(?:请(?:帮我)?|帮我)\s*", "", text)
        equal["status"] = (
            "created" if re.match(r"重开|重新打开|取消完成|reopen\b", directive) else
            "cancelled" if re.match(r"取消|cancel\b", directive) else "completed"
        )
    if action == "reminder.snooze" and seconds:
        equal["delay_s"] = seconds
        equal["mode"] = _snooze_mode(text)
    if action == "phase.reassign_interval":
        interval = context.get("selected_interval") or {}
        if selected and selected == interval.get("id"):
            if re.search(r"未分配|unassigned", text):
                return {action: {"target": selected, "equal": {"target_issue_id": None}}}
            target_text = re.split(r"改(?:归|到)|重归|\bto\b", text)[-1]
            targets = _mentions(target_text, [item for item in items if item.get("type") == "issue"])
            if len(targets) == 1:
                return {action: {"target": selected, "equal": {"target_issue_id": targets.pop()}}}
        return {}
    if action == "phase.credit_sleep":
        recovery = context.get("selected_recovery") or {}
        if seconds and selected and selected == recovery.get("id"):
            return {action: {"target": recovery["phase_id"], "equal": {"interval_id": selected, "seconds": seconds}}}
        return {}
    if action == "phase.review":
        phase = context.get("selected_phase") or current
        target = ref_id(phase.get("id"))
    elif action.startswith("phase."):
        target = ref_id(current.get("id"))
    else:
        candidates = context.get("habits", []) if action.startswith("habit.") else items
        matching = _mentions(text, candidates, selected)
        target = next(iter(matching)) if len(matching) == 1 else None
        if action.startswith("reminder.") and action != "reminder.configure":
            occurrence = next((item.get("occurrence_id") for item in items
                               if ref_id(item.get("id", item.get("task_id"))) == target), None)
            target = ref_id(occurrence) or target
    if not target:
        return {}
    grant = {"target": target, "equal": equal, "issues": sorted(issues)}
    if action == "phase.review":
        summary = re.search(r"(?:复盘|总结|review)\s*[:：]\s*(.+)", text)
        return {action: {**grant, "review": summary[1]}} if summary else {}
    if action in {"phase.set_details", "habit.update", "item.update"}:
        # Object identity is usually clear here, while arbitrary free-form
        # values deserve a visible confirmation unless an explicit value fits.
        field = re.search(r'(?:标题|名称|title).*?(?:改成|改为|设为|to)\s*[“"「]?([^”"」，。；]+)', text)
        details = re.search(r'(?:备注|描述|description).*?(?:改成|改为|设为|to)\s*[“"「]?([^”"」，。；]+)', text)
        if action in {"phase.set_details", "item.update"} and (field or details):
            grant["patch"] = {"title" if field else "description": (field or details)[1].strip()}
        elif action == "phase.set_details":
            return {}
        else:
            return {"proposal.offer": {"action": action, "target": target}}
    if action == "reminder.configure":
        grant["reminder_constraints"] = _reminder_constraints(text, context)
    return {action: grant}


def _snooze_mode(text):
    if re.search(r"准时|准点|exact(?:ly)?", text):
        return "exact"
    if re.search(r"内\s*(?:别|不要|不再|不)\s*提醒|不早于|no earlier", text):
        return "no_earlier"
    return "flexible_target"


_REMINDER_CLOCK = re.compile(
    r"(?P<day>今天|明天|后天)?\s*(?P<period>早上|上午|中午|下午|傍晚|晚上)?\s*"
    r"(?P<hour>[0-9]{1,2}|[零一二两三四五六七八九十]{1,3})\s*"
    r"(?::(?P<minute>[0-9]{2})|[点时](?:(?P<half>半)|(?P<point_minute>[0-9]{1,2})\s*分?)?)"
)


def _literal_reminder_times(text, context):
    timezone = context.get("timezone")
    if not timezone or not context.get("as_of"):
        return {}
    try:
        now = datetime.fromisoformat(context["as_of"]).astimezone(ZoneInfo(timezone))
    except (ValueError, ZoneInfoNotFoundError):
        return {}
    wording = re.sub(r"^(?:请(?:你|帮我)?|帮我)\s*", "", text)
    before, _, after = wording.partition("提醒我")
    timing = before
    if not before and (_REMINDER_CLOCK.match(after) or re.match(r"\s*(?:最迟|最晚|只在|仅在|大约|准时)", after)):
        timing = after
    fixed = {}
    if re.search(r"(?:只在|仅在|仅限)\s*今天", timing):
        end = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=now.tzinfo)
        fixed["expires_at"] = end.isoformat()
    for match in _REMINDER_CLOCK.finditer(timing):
        left, right = timing[:match.start()].rstrip(), timing[match.end():].lstrip()
        if re.search(r"(?:最迟|最晚|不晚于)$", left) or right.startswith("之前"):
            fields = ("remind_by",)
        elif re.search(r"不早于$", left) or right.startswith(("以后", "之后")):
            fields = ("remind_after",)
        elif re.search(r"(?:准时|准点)$", left) or right.startswith(("整", "准时", "准点")):
            fields = ("remind_after", "remind_by")
        elif re.search(r"(?:大约|大概)$", left) or right.startswith(("左右", "前后")):
            fields = ("target_at",)
        else:
            continue
        hour = _chinese_integer(match["hour"])
        if match["period"] in {"中午", "下午", "傍晚", "晚上"} and 1 <= hour < 12:
            hour += 12
        minute = 30 if match["half"] else int(match["minute"] or match["point_minute"] or 0)
        if hour > 23 or minute > 59:
            continue
        day = {"今天": 0, "明天": 1, "后天": 2}.get(match["day"], 0)
        at = datetime.combine(now.date() + timedelta(days=day), time(hour, minute), tzinfo=now.tzinfo)
        if not match["day"] and at < now.replace(second=0, microsecond=0):
            at += timedelta(days=1)
        fixed.update(dict.fromkeys(fields, at.isoformat()))
    return fixed


def _reminder_constraints(text: str, context: dict) -> dict:
    exact = bool(re.search(r"点\s*整|分\s*整|[0-9]{2}\s*整|准时|准点|exact", text))
    fixed = _literal_reminder_times(text, context)
    return {
        "earliest": "remind_after" in fixed or exact or bool(re.search(r"以后|之后|after|不早于", text)),
        "latest": "remind_by" in fixed or exact or bool(re.search(r"最迟|最晚|之前|no later", text)),
        "expiry": "expires_at" in fixed or bool(re.search(r"只在|过期|有效|expires?|only today", text)),
        "exact": exact,
        "urgent": bool(re.search(r"紧急|会议|预约|答辩|起飞|出发|限时|截止|meeting|deadline|appointment", text)),
        "fixed_times": fixed,
        "timezone": context.get("timezone"),
    }


_DURATION_PHRASE = r"[0-9零一二两三四五六七八九十百.]+\s*(?:分钟|小时|秒|分|hours?|hrs?|minutes?|mins?|seconds?|secs?)"
_REQUEST_PREFIX = r"(?:(?:现在|请(?:你|帮我)?|帮我|能(?:不能)?帮我|可以帮我|(?:can|could|would) you)\s*)?"


def timer_query_language(utterance: str) -> str | None:
    """Recognize a direct request for the remaining time of the active timer."""
    text = normalized(utterance).rstrip("。.!！?？")
    if re.fullmatch(
        r"(?:请问\s*)?(?:(?:现在|当前|本轮|本次|这轮|这次|我的)\s*)?"
        r"(?:(?:专注|休息|计时|计时器|番茄钟)\s*)?(?:还剩多久|还剩多长时间|还剩多少时间|还有多久|还剩几分钟|剩余时间)(?:了|啊|呢)?",
        text,
    ):
        return "zh"
    if re.fullmatch(r"(?:how (?:much time|long) (?:is )?(?:left|remaining)(?: (?:on|in) (?:my|the) (?:timer|focus session|break))?|time (?:left|remaining))", text):
        return "en"
    return None


def deterministic_command(user: dict | None, grants: dict[str, dict]) -> tuple[str, dict] | None:
    """Recover a literal timer request when a completed model made no write call."""
    if not user or not user.get("utterance") or len(grants) != 1:
        return None
    text = normalized(without_cancellation(user["utterance"])).rstrip("。.!！?？")
    action, binding = next(iter(grants.items()))
    exact = binding.get("equal", {})
    duration = _DURATION_PHRASE
    if action == "phase.start" and "planned_duration_s" in exact:
        pattern = (_REQUEST_PREFIX + r"(?:开始|启动|开启|开|来|start|begin)\s*(?:一个|个|a\s+)?"
                   + duration + r"\s*(?:的|of\s+)?(?:专注|计时|番茄钟?|(?:长|短)?休息|focus|pomodoro|break|timer)?(?:吗|吧)?")
        if re.fullmatch(pattern, text):
            return action, exact
    if action == "phase.adjust" and binding.get("target"):
        pattern = (_REQUEST_PREFIX + r"(?:(?:把)?(?:这次|本轮|这轮|当前|this)\s*(?:专注|休息|计时|时间|focus|timer)?\s*)?"
                   + r"(?:改成|改为|调整为|调整到|再加|延长|add|extend by|change to)\s*" + duration + r"(?:吗|吧)?")
        if len(exact) == 1 and re.fullmatch(pattern, text):
            return action, {"id": binding["target"], **exact}
    if action == "proposal.resolve" and binding.get("proposal_version") is not None and exact.get("answer") == "accept":
        amendments = binding.get("amendments", {})
        if amendments and re.fullmatch(_REQUEST_PREFIX + r"改(?:成|为)\s*" + duration + r"\s*(?:再)?开始(?:吧)?", text):
            return action, {"id": binding["target"], "answer": "accept", "amendments": amendments,
                            "expected_version": binding["proposal_version"]}
    return None


def bind_tool_args(grants: dict[str, dict], function: str, args: dict) -> dict:
    """Use literal user parameters when a matching tool left the value to the model."""
    binding = grants.get(function, {})
    exact = binding.get("equal", {})
    if function == "phase.start" and exact.get("kind"):
        duration = exact.get("planned_duration_s")
        if duration is None and not args.get("habit_id"):
            duration = binding.get("default_duration")
        return {**args, "kind": exact["kind"], "planned_duration_s": duration}
    if function == "phase.adjust" and ref_id(args.get("id")) == binding.get("target"):
        if any(key in exact for key in ("total_s", "add_s")):
            return {key: value for key, value in args.items() if key not in {"total_s", "add_s"}} | exact
    if function in {"item.create", "reminder.configure"}:
        constraints = binding.get("reminder_constraints", {})
        rule_key = "reminder_rule" if function == "item.create" else "rule"
        if constraints.get("fixed_times") and isinstance(args.get(rule_key), dict):
            rule = args[rule_key]
            if "timezone" not in rule:
                args = {**args, rule_key: {**rule, "timezone": constraints["timezone"]}}
    if function == "proposal.resolve" and ref_id(args.get("id")) == binding.get("target"):
        if binding.get("proposal_version") is not None:
            args = {"expected_version": binding["proposal_version"], **args}
        if exact.get("answer") == args.get("answer") and "amendments" in binding:
            args = {**args, "amendments": binding["amendments"]}
    return args


def _same_time(value, expected) -> bool:
    try:
        return datetime.fromisoformat(value) == datetime.fromisoformat(expected)
    except (TypeError, ValueError):
        return False


def allows(grants: dict[str, dict], function: str, args: dict) -> bool:
    if function in READ_TOOLS:
        return True
    if function not in grants:
        return False
    binding = grants[function]
    if any(args.get(key) != value for key, value in binding.get("equal", {}).items()
           if key != "amendments" or value):
        return False
    if function in _IDENTIFIED_ACTIONS:
        identity_keys = ("interval_id",) if function == "phase.reassign_interval" else ("id", "item_id", "occurrence_id", "phase_id")
        target = next((args[key] for key in identity_keys if key in args), None)
        if binding.get("target") != ref_id(target):
            return False
    if function == "proposal.resolve" and binding.get("proposal_version") is not None:
        if args.get("expected_version") != binding["proposal_version"]:
            return False
    if function == "proposal.resolve" and (args.get("amendments") or {}) != binding.get("amendments", {}):
        return False
    if function == "settings.update":
        requested = args.get("patch")
        if not isinstance(requested, dict) or not requested or any(
            key not in binding["patch"] or binding["patch"][key] != value for key, value in requested.items()
        ):
            return False
        if binding.get("target") and ref_id(args.get("id")) != binding["target"]:
            return False
    if function in {"phase.set_details", "item.update"} and (patch := binding.get("patch")):
        values = args.get("patch", args)
        if any(not isinstance(values.get(key), str) or normalized(values[key]) != value for key, value in patch.items()):
            return False
        fields = set(patch) if function == "item.update" else set(patch) | {"id", "expected_version"}
        if any(value is not None and key not in fields for key, value in values.items()):
            return False
    if function == "phase.review":
        review = args["review"]
        if review.get("count_as_completed"):
            return False
        text = binding["review"]
        if any(normalized(entry) not in text for key in ("confirmed_outcomes", "next_steps") for entry in review.get(key, ())):
            return False
    if function == "phase.start":
        for field in ("title", "description"):
            value = args.get(field)
            if value and normalized(value) not in binding["input"] and not (field == "title" and value in {"专注", "休息"}):
                return False
    if function == "item.create" and (title := binding.get("title")):
        if not isinstance(args.get("title"), str) or normalized(args["title"]).rstrip("。！!") != title:
            return False
    if function == "item.create":
        description = args.get("description")
        if description and normalized(description) not in binding["input"]:
            return False
        if args.get("due_at") and not binding["deadline"]:
            return False
        if args.get("reminder_rule") and not binding["has_reminder"]:
            return False
    if function == "habit.create":
        if normalized(args["title"]) != binding["title"]:
            return False
        if duration := binding.get("duration"):
            if args.get("behavior", {}).get("focus_s") != duration:
                return False
    if function in {"item.create", "reminder.configure"}:
        rule = args.get("reminder_rule") if function == "item.create" else args.get("rule")
        bounds = binding.get("reminder_constraints", {})
        fixed = bounds.get("fixed_times", {})
        if fixed and (not rule or rule.get("timezone", bounds["timezone"]) != bounds["timezone"]
                      or any(not _same_time(rule.get(field), value) for field, value in fixed.items())):
            return False
        if fixed and rule.get("target_at"):
            exact_target = fixed.get("remind_by") if _same_time(fixed.get("remind_after"), fixed.get("remind_by")) else None
            if not _same_time(rule["target_at"], fixed.get("target_at") or exact_target):
                return False
        if rule and ((rule.get("remind_after") and not bounds.get("earliest"))
                     or (rule.get("remind_by") and not bounds.get("latest"))
                     or (rule.get("expires_at") and not bounds.get("expiry"))
                     or (rule.get("urgency_until") and not bounds.get("urgent"))):
            return False
        if bounds.get("exact") and (not rule or not _same_time(rule.get("remind_after"), rule.get("remind_by"))):
            return False
    if function in {"phase.start", "phase.set_links"}:
        if any(args.get(key) is not None for key in ("previous_phase_id", "expected_previous_version", "habit_run_id", "expected_habit_run_version")):
            return False
        links = args.get("issue_links") or []
        if not isinstance(links, list) or any(ref_id(link) not in binding.get("issues", []) for link in links):
            return False
        if args.get("primary_issue_id") and ref_id(args["primary_issue_id"]) not in binding.get("issues", []):
            return False
        if args.get("habit_id") and ref_id(args["habit_id"]) not in binding.get("habits", []):
            return False
    if function == "reminder.present":
        ids = args.get("occurrence_ids")
        return bool(ids) and isinstance(ids, list) and all(ref_id(identity) in binding["occurrences"] for identity in ids)
    if function in {"reminder.recheck", "observation.wait_fresh"}:
        if occurrence := args.get("occurrence_id"):
            return ref_id(occurrence) in binding["occurrences"]
        return function == "observation.wait_fresh" and binding.get("focus") and args.get("purpose") == "focus_suggestion"
    if function == "proposal.offer":
        name, payload = args.get("function"), args.get("args")
        if not isinstance(payload, dict):
            return False
        if binding.get("background"):
            if name == "phase.start":
                return not any(payload.get(key) for key in ("habit_id", "primary_issue_id", "issue_links", "previous_phase_id", "habit_run_id"))
            return name in {"phase.pause", "phase.resume", "phase.adjust"} and bool(binding["target"]) and ref_id(payload.get("id")) == binding["target"]
        return name == binding.get("action") and (not binding.get("target") or ref_id(payload.get("id")) == binding["target"])
    return True
