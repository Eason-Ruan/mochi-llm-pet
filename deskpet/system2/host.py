"""Production entry point for the single Mochi System 2 session."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
import json
import logging
from math import ceil
import re
from pathlib import Path
import sqlite3
from threading import Event, Lock, Thread
from uuid import uuid4

from pydantic import ValidationError

from deskpet.m0.observation_input import DesktopObservationInput
from deskpet.mocks.clock import RealClock
from deskpet.observation_v2.context import OBSERVATION_CONTEXT_FIELDS, ObservationContext, project_observation_context
from deskpet.settings import SettingsStore
from deskpet.timezones import resolve_timezone

from .admission import AdmissionStore, json_text, stable_id, timestamp
from .dsh_projection import project_business_result
from .intent import (
    READ_TOOLS, bind_tool_args, chat_grants, describe_grants, deterministic_command, independent_intents,
    literal_chat_reminder, literal_start_cancellation, materialize_chat_times, normalized, ref_id, ref_kind, start_request,
    timer_query_language, without_cancellation,
)
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT
from .protocol import EngineReply, ToolAuthority, TurnRequest
from .semantic_policy import semantic_background_enabled, semantic_chat_enabled
from .tool_schema import canonical_tool_name, validate_tool_args


log = logging.getLogger(__name__)
_SUCCESS = frozenset({"applied", "accepted", "pending"})
_ACTION_LABELS = {
    "item.create": "已创建事项。",
    "item.update": "已更新事项。",
    "item.set_status": "已更新事项状态。",
    "item.archive": "已归档事项。",
    "reminder.configure": "已设置提醒。",
    "reminder.snooze": "已推迟提醒。",
    "reminder.ack": "已确认提醒。",
    "phase.start": "已开始计时。",
    "phase.pause": "已暂停计时。",
    "phase.resume": "已继续计时。",
    "phase.adjust": "已调整计时。",
    "phase.end": "已结束计时。",
    "phase.skip_break": "已跳过休息。",
    "phase.set_links": "已更新专注关联。",
    "settings.update": "已更新设置。",
    "proposal.offer": "已生成待确认提议。",
    "proposal.resolve": "已处理提议。",
    "reminder.present": "已提交提醒展示。",
    "reminder.recheck": "已登记复查。",
    "observation.wait_fresh": "已登记等待新观测。",
    "pet.present": "已提交呈现请求。",
}
_REASONS = {
    "quiet": "这次没有需要主动打扰的事项。",
    "busy": "当时的状态适合继续等待。",
    "focus_active": "当时正在专注，普通提醒继续等待。",
    "not_fresh": "当时没有可用于判断打扰时机的新观测。",
    "no_eligible_items": "当时没有符合时间条件的提醒。",
    "meeting_preparation": "该事项有临近的准备时限。",
    "time_sensitive": "继续等待会影响事项所需的时间。",
    "available": "当时符合提醒的空档条件。",
    "working": "当时正在工作。",
    "disabled": "相关设置已关闭。",
    "model_unavailable": "这次对话模型没能完成回复。",
    "superseded": "这条请求已经被后续操作替代。",
    "interrupted": "上一轮未完成，已核对保存的执行结果。",
    "expired_request": "这条请求已超过可执行时限。",
}
_CHAT_CANDIDATE_LIMIT = 20
_STALE = {"STALE_TASK", "STALE_CONTEXT", "STALE_VERSION", "STALE_PROPOSAL"}
_CAPABILITY_QUESTION = re.compile(r"(?:请问\s*)?(?:你)?(?:现在|当前)?有(?:操作)?权限吗[?。！!]*")
_CHAT_CAPABILITY_QUESTION = re.compile(
    r"(?:请问\s*)?(?:你)?(?:能|可以|会|能不能|可不可以)(?:帮人)?(?:设(?:置|定)?提醒|记事项)(?:吗[?。！! ]*|[?])"
    r"|(?:can|could) you (?:set reminders|save tasks)[?.! ]*",
)
_ACTIVITY_QUESTION = re.compile(
    r"(?:刚才|刚刚|方才|之前|上一|过去|近一小时|这段时间|此前).{0,20}(?:做|干|改|看|打开|活动|那个|哪)"
    r"|(?:哪|第[一二三四五六七八九十\d]+)(?:一)?(?:份|个|篇|段)(?:文档|文件|活动|[呢吗？?])?"
    r"|(?:现在|正在).{0,15}(?:做什么|干嘛|看什么|改什么|开着什么)"
    r"|(?:最近|之前|过去|刚才).{0,10}(?:活动|观测|观察)"
    r"|^(?:那份|另一份|另一个|那另一个|另一个呢|那第二个).{0,8}[呢吗？?]?$"
    r"|\b(?:what|which|second|third)\b.{0,55}\b(?:document|file|activity|doing|working on|looking at|editing|opened)\b"
    r"|\b(?:last hour|just now|earlier|previous activity|last activity)\b"
    r"|^(?:what about )?(?:the )?(?:other|next|second|third) one[?.]?$", re.IGNORECASE,
)
_ITEM_LIST_QUERY = re.compile(
    r"^(?:请(?:帮我)?|帮我)?\s*(?:(?:查看|列出|显示|看看)(?:我(?:的)?)?\s*(?:所有|全部|现有|当前)?"
    r"\s*(?:待办事项|事项|待办|任务|提醒)(?:列表)?[？?。！! ]*$"
    r"|(?:我)?(?:还有|有)(?:什么|哪些|多少)(?:待办事项|事项|待办|任务|提醒))"
    r"|^(?:please\s+)?(?:list|show|what are|how many)\b.{0,30}\b(?:items|issues|tasks|reminders)\b",
)
_HABIT_LIST_QUERY = re.compile(
    r"^(?:请(?:帮我)?|帮我)?\s*(?:(?:查看|列出|显示|看看)(?:我(?:的)?)?\s*(?:所有|全部|现有|当前)?"
    r"\s*习惯(?:列表)?[？?。！! ]*$|(?:我)?(?:还有|有)(?:什么|哪些)习惯)"
    r"|^(?:please\s+)?(?:list|show|what are)\b.{0,30}\bhabits\b",
)
_UNSUPPORTED_WRITE = re.compile(
    r"(?:已经?|成功|给你|帮你|替你|按你的要求|为你).{0,12}?"
    r"(?:开始|启动|暂停|恢复|继续|结束|创建|新建|添加|记下|记录|保存|归档|取消|完成|更改|修改|删除|调整|设置|推迟|确认)"
    r"|(?:开始|启动|暂停|恢复|继续|结束|创建|添加|新建|记录|保存|删除|调整|推迟)(?:好)?了"
    r"|(?:给|帮|替|为)你.{0,12}(?:做好了|弄好了|安排好了|处理好了|整理好了|办好了|搞定了)"
    r"|(?:^|[。！？!?]\s*)已(?:经)?(?:开始|开启|启动|暂停|恢复|结束|创建|新建|记录|保存|添加|取消|调整|推迟|安排|归档|修改|设好)"
    r"|\b(?:i(?:'ve| have)?|we(?:'ve| have)?|successfully|just)\s+"
    r"(?:started|created|updated|saved|added|completed|cancelled|deleted|paused|resumed|stopped|adjusted|scheduled)\b"
    r"|^\s*(?:started|created|updated|saved|added|completed|cancelled|deleted|paused|resumed|done)\b",
    re.IGNORECASE,
)


def _as_dict(value) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value.to_dict()


def _reason(result: dict) -> str:
    code = result.get("error_code") or result.get("code") or result.get("status", "quiet")
    return str(code).lower().replace(" ", "_")[:80]


def _original_status(result: dict) -> str:
    if result["status"] == "duplicate":
        return result.get("original_status") or "pending"
    return result["status"]


def _tool_outcome(function: str, result: dict) -> str:
    if outcome := result.get("outcome"):
        return outcome
    status = _original_status(result)
    if status in _SUCCESS:
        if function in {"reminder.recheck", "observation.wait_fresh", "decision.recheck"}:
            return "deferred"
        if status == "pending" or result.get("delivery_id"):
            return "pending_display"
        return "committed"
    if status in {"timeout", "failed"}:
        return "failed"
    return "rejected"


def _result_reply(function: str, result: dict, timezone: str | None = None) -> dict:
    status = _original_status(result)
    view = result.get("view")
    view = view if isinstance(view, dict) else {}
    if status in {"applied", "accepted"}:
        text = result.get("message") or _ACTION_LABELS.get(function, "已处理。")
        phase = view.get("phase", view)
        seconds = phase.get("planned_duration_s") if isinstance(phase, dict) else None
        if function == "item.create" and view.get("type") == "reminder" and (title := view.get("title")):
            text = f"已设置提醒「{title}」。"
            if timezone and (rule := view.get("rule")):
                earliest, latest, target = (rule.get(field) for field in ("remind_after", "remind_by", "target_at"))
                due = latest or target or earliest
                if due:
                    local_time = datetime.fromisoformat(due).astimezone(resolve_timezone(timezone))
                    created_at = view.get("created_at")
                    same_day = created_at and datetime.fromisoformat(created_at).astimezone(local_time.tzinfo).date() == local_time.date()
                    label = local_time.strftime("%H:%M:%S" if same_day else "%m月%d日 %H:%M:%S")
                    prefix = "" if earliest == latest else "最晚 " if latest else "约 " if target else "最早 "
                    text = f"已设置提醒「{title}」，{prefix}{label}。"
        phase_action = result.get("resolved_function") if function == "proposal.resolve" else function
        if phase_action in {"phase.start", "phase.adjust"} and isinstance(seconds, (int, float)):
            length = f"{seconds / 60:g} 分钟" if seconds % 60 == 0 else f"{seconds:g} 秒"
            text = f"{'已调整' if phase_action == 'phase.adjust' else '已开始'} · {length}"
    elif status == "pending":
        text = result.get("message") or "已提交，正在等待确认。"
    elif result.get("error_code") in _STALE:
        text = "相关事项或设置已经更新。请刷新后再确认。"
    elif status == "timeout":
        text = "这次未能确认操作结果。"
    elif timezone and result.get("error_code") == "INVALID_ARGUMENT":
        text = "这条事项没有保存。请核对内容和时间。" if function == "item.create" else "操作参数无效，未能保存更改。"
    elif timezone and result.get("error_code") == "NOT_AUTHORIZED":
        text = "找不到可操作的对应对象，未能保存更改。"
    else:
        text = result.get("message") or "这次没有执行成功。"
    return {"text": text, "status": status, "outcome": _tool_outcome(function, result),
            "command_id": result.get("call_id"), "error_code": result.get("error_code"),
            "result_ref": result.get("subject_ref"), "delivery_id": result.get("delivery_id")}


def _with_reply_source(reply: dict, source: str) -> dict:
    text = reply["text"]
    return {**reply, "parts": [{"source": source, "text": text}] if text.strip() else []}


def _chat_attempt_target(function: str, args: dict) -> str | None:
    """Identify repeated work on the same subject without persisting untrusted arguments."""
    if function == "phase.start":
        return stable_id(function, str(args.get("kind", "focus")))
    if function == "settings.update" and args.get("scope") == "default":
        return stable_id(function, "default")
    if function in {"item.create", "habit.create"}:
        title = args.get("title")
        if isinstance(title, str) and title:
            return stable_id(function, str(args.get("type", "")), title)
        return None
    for field in ("occurrence_id", "item_id", "recovery_interval_id", "interval_id", "id"):
        if isinstance(args.get(field), str):
            return stable_id(function, args[field])
    return None


def _timer_reply(result: dict, language: str) -> dict:
    phase = result.get("view")
    if result["status"] != "applied":
        text = "I couldn't read the timer." if language == "en" else "这次未能读取当前计时。"
        return {"text": text, "status": "failed"}
    if not isinstance(phase, dict):
        text = "No timer is currently active." if language == "en" else "当前没有活动计时。"
    else:
        minutes, seconds = divmod(ceil(phase["remaining_s"]), 60)
        if language == "en":
            text = f"{minutes}m {seconds:02d}s remaining."
            if phase.get("state") == "paused":
                text = "Paused. " + text
        else:
            text = f"还剩 {minutes} 分 {seconds:02d} 秒。"
            if phase.get("state") == "paused":
                text = "计时已暂停，" + text
    return {"text": text, "status": "answered", "result_ref": result.get("subject_ref")}


class System2Host:
    def __init__(
        self, data_dir: Path | str, *, scheduler, gateway, engine=None, context_reader=None,
        settings_store=None, clock=None, user_scope="default", display_capabilities=None,
        timezone=None, manage_scheduler=True, turn_timeout_s=45.0,
    ):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.user_scope = user_scope
        self.clock = clock or RealClock()
        self.settings = settings_store or SettingsStore(self.data_dir / "business.sqlite3", user_scope=user_scope)
        self.path = self.settings.path
        if self.settings.user_scope != user_scope:
            raise ValueError("System 2 settings must match the user's scope")
        self.gateway = gateway
        self.scheduler = scheduler
        self.context_reader = context_reader or DesktopObservationInput(
            self.data_dir / "observation.sqlite3", settings_path=self.path,
        )
        self.display_capabilities = display_capabilities
        self.timezone = resolve_timezone(timezone).key
        if engine is None:
            from .dsh import DSHEngine

            engine = DSHEngine(self.data_dir, settings=self.settings, tools=self.gateway.definitions,
                               observation_guard=self._observation_privacy_marker)
            self._trusted_projection = True
        else:
            self._trusted_projection = False
        self.engine = engine
        self._ttl = timedelta(seconds=turn_timeout_s)
        if self._ttl <= timedelta(0):
            raise ValueError("System 2 turn timeout must be positive")
        self._store = AdmissionStore(self.path, user_scope)
        self._manage_scheduler = manage_scheduler
        self._stopping = Event()
        self._background_suspended = Event()
        self._awake = Event()
        self._pump_lock = Lock()
        self._lifecycle_lock = Lock()
        self._worker = None
        self._maintenance = None

    @staticmethod
    def _event_id(value) -> str:
        if value is None:
            return uuid4().hex
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError("A nonempty user event ID is required")
        return value

    def submit_user(self, utterance, *, event_id=None, selected_ref=None, reply_to_proposal_id=None,
                    reply_to_proposal_version=None, supersedes_admission_id=None, created_at=None):
        """Durably enqueue one trusted user message and return its admission ID."""
        if self._stopping.is_set():
            raise RuntimeError("System 2 host is closed")
        if not isinstance(utterance, str) or not utterance.strip() or len(utterance) > 10000:
            raise ValueError("A nonempty user message is required")
        if reply_to_proposal_version is not None and (
            type(reply_to_proposal_version) is not int or reply_to_proposal_version < 1 or not reply_to_proposal_id
        ):
            raise ValueError("A proposal reply needs its positive version and ID")
        now = self.clock.wall_now()
        created_at = now if created_at is None else created_at
        if not isinstance(created_at, datetime) or created_at.utcoffset() is None or created_at > now:
            raise ValueError("A user event must have an aware timestamp no later than submission")
        event_id = self._event_id(event_id)
        intent = {"utterance": utterance, "message_id": event_id, "selected_ref": selected_ref,
                  "reply_to_proposal_id": ref_id(reply_to_proposal_id),
                  "reply_to_proposal_version": reply_to_proposal_version}
        semantic = semantic_chat_enabled()
        parts = (utterance,) if semantic or reply_to_proposal_id else independent_intents(utterance)
        messages = []
        for index, part in enumerate(parts):
            trigger_id = event_id if index == 0 else "s2sub-" + stable_id(event_id, str(index))
            user = intent if len(parts) == 1 else {**intent, "utterance": part, "original_utterance": utterance}
            event = self._user_event(trigger_id, created_at, "user_message", selected_ref)
            event.update(semantic_chat=semantic, timezone=self.timezone)
            messages.append((event, user, start_request(part)))
        admission_ids = self._store.submit_many(
            messages, created_at, self._ttl, explicit_supersedes=supersedes_admission_id,
        )
        self._awake.set()
        return admission_ids[0]

    def resolve_proposal(self, proposal_id, answer, *, version, event_id=None, amendments=None):
        """Enqueue a versioned Qt proposal action without consulting the model."""
        if self._stopping.is_set():
            raise RuntimeError("System 2 host is closed")
        proposal_id = ref_id(proposal_id)
        if not proposal_id or type(version) is not int or version < 1 or answer not in {"accept", "reject", "cancel"}:
            raise ValueError("A valid proposal, version, and answer are required")
        now = self.clock.wall_now()
        event_id = self._event_id(event_id)
        button = {"id": proposal_id, "answer": answer, "expected_version": version}
        if amendments is not None:
            if not isinstance(amendments, dict) or answer != "accept":
                raise ValueError("Only an accepted proposal can be amended")
            button["amendments"] = amendments
        intent = {"message_id": event_id, "reply_to_proposal_id": proposal_id, "proposal_button": button}
        admission_id = self._store.submit(
            self._user_event(event_id, now, "proposal_button", proposal_id), intent, now, self._ttl,
        )
        self._awake.set()
        return admission_id

    def supersede_for_direct_action(self, function, *, subject_ref=None, event_id=None):
        """Fence a matching pending chat command before a direct desktop write."""
        self._store.supersede_direct(canonical_tool_name(function), ref_id(subject_ref), self._event_id(event_id))
        self._awake.set()

    def fence_local(self, db, function, args, event_id):
        """Order an applied local event and matching queued model effects in one transaction."""
        subject = next((args[key] for key in ("id", "item_id", "occurrence_id") if key in args), None)
        self._store.supersede_direct(canonical_tool_name(function), ref_id(subject), event_id, db=db)
        self._awake.set()

    @staticmethod
    def _user_event(event_id, now, reason, selected_ref):
        identity = ref_id(selected_ref)
        return {"trigger_ids": [event_id], "trigger_reasons": [reason], "kind": "user_message",
                "at": timestamp(now), "subject_refs": [identity] if identity else []}

    @property
    def turn_timeout(self) -> timedelta:
        return self._ttl

    def admission_for_message(self, event_id) -> str | None:
        """Find an already admitted desktop input by its original event ID."""
        admission_id = "s2a-" + stable_id(self.user_scope, "user", self._event_id(event_id))
        return admission_id if self._store.state(admission_id) is not None else None

    def ui_events(self):
        return self._store.ui_events()

    def ack_ui_event(self, sequence):
        self._store.ack_ui_event(sequence)

    def decision_records(self, *, limit=12):
        return self._store.decisions(min(max(limit, 1), 50))

    def pending_proposals(self):
        return self._pending_proposals(self.clock.wall_now())

    def _pending_proposals(self, as_of):
        return tuple(_as_dict(value) for value in self.gateway.pending_proposals(
            self.user_scope, as_of=as_of,
        ))

    def start(self):
        with self._lifecycle_lock:
            if self._worker is not None or self._stopping.is_set():
                return
            self._worker = Thread(target=self._work, name="mochi-system2", daemon=True)
            self._worker.start()
            if self.scheduler is not None and self._manage_scheduler:
                self._maintenance = Thread(target=self._schedule, name="mochi-system2-wakes", daemon=True)
                self._maintenance.start()

    def _work(self):
        while not self._stopping.is_set():
            try:
                if self.pump():
                    continue
            except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
                log.warning("System 2 host worker failed (%s)", type(exc).__name__)
            self._awake.wait(0.25)
            self._awake.clear()

    def _schedule(self):
        while not self._stopping.is_set():
            try:
                self.scheduler.poll()
                if self.scheduler.pending_wakes():
                    self._awake.set()
            except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
                log.warning("System 2 scheduler poll failed (%s)", type(exc).__name__)
            self._stopping.wait(0.5)

    def close(self):
        with self._lifecycle_lock:
            if self._stopping.is_set():
                return
            self._stopping.set()
            self._store.stop()
            self._awake.set()
            self.engine.close()
            if self._worker is not None:
                self._worker.join(timeout=2.0)
            if self._maintenance is not None:
                self._maintenance.join(timeout=2.0)

    def suspend_background(self):
        self._background_suspended.set()

    def fence_background(self, db):
        self._store.interrupt_background(db)

    def resume_background(self):
        self._background_suspended.clear()
        self._awake.set()

    def _background(self, now):
        if self.scheduler is None or self._background_suspended.is_set():
            return None
        wakes = [wake for wake in self.scheduler.pending_wakes() if wake.user_scope == self.user_scope]
        return self._wake_event(wakes, now)

    def _wake_event(self, wakes, now):
        if not wakes:
            return None
        event = {
            "trigger_ids": [wake.event_id for wake in wakes],
            "trigger_reasons": list(dict.fromkeys(wake.reason for wake in wakes)),
            "kinds": list(dict.fromkeys(wake.kind for wake in wakes)),
            "kind": "background", "at": timestamp(now),
            "subject_refs": list(dict.fromkeys(ref for wake in wakes for ref in wake.subject_refs)),
            "occurrence_ids": list(dict.fromkeys(wake.occurrence_id for wake in wakes if wake.occurrence_id)),
        }
        if elapsed := [wake.phase_elapsed for wake in wakes if getattr(wake, "phase_elapsed", None)]:
            event["phase_elapsed"] = elapsed
        return event, now + self._ttl

    def _claim_wake_events(self, ids, now, db):
        if self._background_suspended.is_set():
            return None
        options = {"coalesce": True} if semantic_background_enabled() and hasattr(self.scheduler, "retry_wakes") else {}
        return self._wake_event(self.scheduler.claim_wakes(ids, now=now, db=db, **options), now)

    def _business_context(self, admission, as_of) -> dict:
        with self._store.connect() as db:
            db.execute("BEGIN")
            settings = self.settings.read(db=db)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            phase = None
            if "phase_views" in tables:
                current = db.execute("SELECT view FROM phase_views WHERE active = 1").fetchone()
                phase = json.loads(current[0]) if current else None
            items = []
            intent = admission.user_intent or {}
            original = normalized(intent.get("utterance") or "")
            selected_ref = intent.get("selected_ref")
            selected = ref_id(selected_ref)
            selection = {}
            if (admission.source == "background" and semantic_background_enabled() and "phase_views" in tables
                    and (phase is None or phase.get("state") == "elapsed")):
                latest = db.execute("SELECT view FROM phase_views ORDER BY rowid DESC LIMIT 1").fetchone()
                predecessor = json.loads(latest[0]) if latest else None
                if predecessor and predecessor.get("state") == "elapsed":
                    selection["phase_predecessor"] = {key: predecessor[key] for key in ("id", "version", "kind")}
            semantic = admission.source == "user" and semantic_chat_enabled() and not intent.get("proposal_button")
            recent = self._store.decisions(6, user_only=True, db=db) if semantic else ()
            recent_actions = [{key: action[key] for key in ("action", "subject_ref", "outcome")}
                              for decision in recent for action in decision.get("action_results", ())
                              if action.get("outcome") == "committed" and action.get("subject_ref")]
            turn_refs = set()
            if semantic:
                for _, name, _, receipt in self._store.calls(admission.admission_id, db=db):
                    if receipt and _tool_outcome(name, receipt) == "committed" and (subject := receipt.get("subject_ref")):
                        turn_refs.add(ref_id(subject))
            recent_refs = {ref_id(action["subject_ref"]) for action in recent_actions}
            list_items = _ITEM_LIST_QUERY.search(original) is not None
            list_habits = _HABIT_LIST_QUERY.search(original) is not None
            selector = {
                "phase": ("phase_views", "view", "selected_phase"),
                "interval": ("phase_intervals", "interval", "selected_interval"),
                "recovery": ("phase_recovery", "interval", "selected_recovery"),
            }.get(ref_kind(selected_ref))
            if selector is not None and selected is not None:
                table, column, key = selector
                if table in tables:
                    selected_row = db.execute(f"SELECT {column} FROM {table} WHERE id = ?", (selected,)).fetchone()
                    if selected_row:
                        selection[key] = json.loads(selected_row[0])
            if admission.source == "user" and "tasks" in tables:
                for row in db.execute("SELECT view FROM tasks ORDER BY rowid DESC"):
                    task = json.loads(row[0])
                    draft = task["draft"]
                    identity = task["task_id"]
                    occurrence = f'{identity}:{task["occurrence"]}'
                    named = (selected in (identity, occurrence) or normalized(draft["content"]) in original
                             or identity in original)
                    recent_candidate = semantic and (identity in turn_refs or (not selected and identity in recent_refs))
                    if task.get("archived_at") or not (named or list_items or recent_candidate):
                        continue
                    item = {"id": identity, "version": task["version"], "type": draft["item_type"],
                            "status": task.get("issue_status") or task["status"]}
                    if named or list_items or identity in turn_refs:
                        item["title"] = draft["content"]
                    if not semantic or draft.get("reminder_enabled", draft["item_type"] == "reminder"):
                        item["occurrence_id"] = occurrence
                    if semantic:
                        item["updated_at"] = task.get("updated_at") or task.get("confirmed_at") or task.get("created_at")
                        item["mentioned"] = named
                    items.append(item)
            habits = []
            if admission.source == "user" and "pet_habits" in tables:
                for row in db.execute("SELECT habit FROM pet_habits WHERE user_scope = ?", (self.user_scope,)):
                    habit = json.loads(row[0])
                    identity = habit["id"]
                    named = selected == identity or normalized(habit["title"]) in original or identity in original
                    if named or list_habits or (semantic and identity in turn_refs):
                        habits.append(habit)
                    elif semantic and not selected and identity in recent_refs:
                        habits.append({key: habit[key] for key in ("id", "version", "status")})
            if semantic:
                items.sort(key=lambda item: (item["id"] in turn_refs, item.pop("mentioned"),
                                             item["id"] in recent_refs, item.get("updated_at") or ""), reverse=True)
                habits.sort(key=lambda habit: (habit["id"] in turn_refs, "title" in habit), reverse=True)
                selection.update(recent_user_actions=recent_actions,
                                 candidate_items_truncated=len(items) > _CHAT_CANDIDATE_LIMIT and not list_items,
                                 candidate_habits_truncated=len(habits) > _CHAT_CANDIDATE_LIMIT and not list_habits)
                if not list_items:
                    items = items[:_CHAT_CANDIDATE_LIMIT]
                if not list_habits:
                    habits = habits[:_CHAT_CANDIDATE_LIMIT]
        enabled = {key: value for key, value in settings.to_dict().items()
                   if not key.startswith(("observation_", "document_")) and key != "input_mode"}
        return {"current_phase": phase, "items": items, "habits": habits, "enabled_settings": enabled, **selection}

    def _observation_privacy_marker(self):
        reader = self.context_reader
        if isinstance(reader, DesktopObservationInput):
            reader = reader.reader
        read = getattr(reader, "read_privacy_marker", None)
        return read() if read is not None else (None, 0, None)

    def _observation(self, as_of, *, historical=False, document_query=False, include_capture_health=False,
                     history_start=None, history_end=None):
        try:
            reader = self.context_reader
            if historical and isinstance(reader, DesktopObservationInput):
                reader = reader.reader
            expansion = {key: value for key, value in {"history_start": history_start, "history_end": history_end}.items()
                         if value is not None}
            observation = reader.read_observation_context(as_of, **expansion)
            settings = self.settings.read()
            locked = self._observation_privacy_marker()[2] is True
        except (sqlite3.Error, OSError, ValueError):
            return project_observation_context(ObservationContext.empty(as_of))
        # The DSH engine reprojects before injection and rechecks each recipient
        # on the actual send. Keep injectable engines on the default projection.
        return project_observation_context(
            observation, share_summary=self._trusted_projection,
            document_sources=settings.document_local_sources if self._trusted_projection and document_query else (),
            include_document_source=self._trusted_projection,
            excluded_apps=settings.observation_excluded_apps, excluded_domains=settings.observation_excluded_domains,
            paused=settings.observation_paused, suppress_details=locked, historical=historical,
            include_capture_health=include_capture_health, restrict_legacy_summaries=self._trusted_projection,
        )

    def _snapshot(self, admission, as_of, **observation_options):
        schedule = self.scheduler.snapshot(as_of) if self.scheduler is not None else {}
        observation = self._observation(as_of, **observation_options)
        domain = self._business_context(admission, as_of)
        relevant_ids = {item["id"] for item in domain["items"] if "title" in item}
        eligible = [item for value in schedule.get("eligible_items", ()) if
                    (item := _as_dict(value)) and (admission.source == "background" or item.get("id") in relevant_ids)]
        rechecks = []
        occurrences = admission.event.get("occurrence_ids", ())
        if occurrences and self.scheduler is not None and hasattr(self.scheduler, "recheck_items"):
            rechecks = [_as_dict(value) for value in self.scheduler.recheck_items(occurrences, as_of)]
        for item in eligible + rechecks:
            if isinstance(item.get("occurrence"), dict):
                item.setdefault("occurrence_id", item["occurrence"].get("id"))
        caps = self.display_capabilities() if callable(self.display_capabilities) else self.display_capabilities
        context = {
            **domain, **observation,
            "as_of": as_of.isoformat(),
            "timezone": self.timezone,
            "trigger_reasons": admission.event["trigger_reasons"],
            "eligible_items": eligible,
            "recheck_items": rechecks,
            "pending_proposals": list(self._pending_proposals(as_of)),
            "display_capabilities": caps or {},
            "recent_decisions": (list(self._store.decisions(6, background_only=True))
                                 if admission.source == "user" or semantic_background_enabled() else []),
            "opportunity_id": schedule.get("opportunity_id"),
            "publication_cursor": schedule.get("publication_cursor"),
        }
        if "recent_contacts" in schedule:
            context["recent_contacts"] = schedule["recent_contacts"]
        if "current_phase" in schedule:
            context["current_phase"] = schedule["current_phase"]
        if elapsed := admission.event.get("phase_elapsed"):
            context["phase_elapsed"] = elapsed
        observed = context["current"]
        head = schedule.get("current")
        if schedule and observed and (head is None
                or observed.get("context_revision") != getattr(head, "context_revision", None)
                or observed.get("observed_at") != head.observed_at.isoformat()):
            context["current"], context["current_status"] = None, "context_changed"
            context["interruptibility"] = {"status": "context_changed", "current": None}
        observed_fields = {key: context[key] for key in OBSERVATION_CONTEXT_FIELDS if key in context}
        return project_business_result(context) | observed_fields, schedule

    @staticmethod
    def _authority_context(context, schedule):
        versions = {"settings:default": context["enabled_settings"]["version"]}
        for kind, values in (
            ("item", context["items"] + context["eligible_items"] + context["recheck_items"]),
            ("habit", context["habits"]),
            ("proposal", context["pending_proposals"]),
            ("phase", [phase for phase in (context["current_phase"], context.get("selected_phase"),
                                        context.get("phase_predecessor")) if phase]),
            ("interval", [context["selected_interval"]] if context.get("selected_interval") else []),
            ("recovery", [context["selected_recovery"]] if context.get("selected_recovery") else []),
        ):
            for value in values:
                identity = ref_id(value.get("id", value.get("task_id", value.get("proposal_id"))))
                version = value.get("version")
                if identity is not None and type(version) is int:
                    versions[f"{kind}:{identity}"] = version
                    versions.setdefault(identity, version)
                    if occurrence := value.get("occurrence_id"):
                        versions[f"occurrence:{occurrence}"] = version
        observed = context["current"] or {}
        return {
            "context_revision": observed.get("context_revision"),
            "observation_valid_until": observed.get("valid_until"),
            "observation_cursor": schedule.get("publication_cursor"),
            "observation_generation": schedule.get("producer_generation"),
            "opportunity_id": schedule.get("opportunity_id"),
            "expected_versions": versions,
        }

    def _call(self, admission, bound, context, grants, name, native_call_id, args, *,
              step_id=None, check_model_policy=None):
        if (not isinstance(name, str) or not isinstance(native_call_id, str) or not native_call_id
                or not isinstance(args, dict)):
            return {"call_id": native_call_id, "status": "rejected", "error_code": "INVALID_ARGUMENT"}
        name = canonical_tool_name(name)
        now = self.clock.wall_now()
        step_id = step_id or admission.step_id
        args = bind_tool_args(grants, name, args)
        semantic = grants.get(name, {}).get("semantic_chat") is True
        invalid_fields = None
        try:
            if name != "decision.list":
                args = validate_tool_args(name, args, relative_time=semantic)
                if semantic:
                    args = materialize_chat_times(name, args, admission.created_at,
                                                  admission.event.get("timezone", self.timezone))
                    args = validate_tool_args(name, args)
        except ValidationError as exc:
            invalid_fields = [{"field": ".".join(map(str, error["loc"])), "code": error["type"]}
                              for error in exc.errors(include_url=False, include_context=False, include_input=False)][:8]
        except (KeyError, ValueError, TypeError) as exc:
            invalid_fields = [{"field": "timing", "code": str(exc)}] if semantic else []
        if invalid_fields is not None and not semantic:
            return {"call_id": native_call_id, "status": "rejected", "error_code": "INVALID_ARGUMENT"}
        attempt_target = _chat_attempt_target(name, args) if semantic else None
        command_id, prior = self._store.register_call(
            admission, step_id, native_call_id, name, args, now, validation_error=invalid_fields,
            attempt_target=attempt_target,
        )
        if prior is not None:
            if name not in READ_TOOLS:
                prior.setdefault("outcome", _tool_outcome(name, prior))
            return project_business_result({key: value for key, value in prior.items() if key != "_attempt_target"})
        authority = ToolAuthority(
            admission.admission_id, step_id, command_id, admission.generation, self.user_scope,
            "user" if admission.source == "user" else "agent", admission.event["trigger_ids"][0],
            admission.created_at, admission.expires_at,
            subject_refs=tuple(admission.event["subject_refs"]),
            context_revision=bound["context_revision"], observation_cursor=bound["observation_cursor"],
            observation_generation=bound["observation_generation"],
            observation_valid_until=datetime.fromisoformat(bound["observation_valid_until"])
            if bound["observation_valid_until"] else None,
            opportunity_id=bound["opportunity_id"],
            proposal_id=(admission.user_intent or {}).get("reply_to_proposal_id"),
            expected_versions=bound["expected_versions"],
            check_model_policy=check_model_policy,
        )
        result = self._execute(name, args, authority, context, admission)
        if name not in READ_TOOLS:
            result.setdefault("outcome", _tool_outcome(name, result))
        # Observation and other private read views need not survive the turn.
        persisted = {key: value for key, value in result.items()
                     if name not in READ_TOOLS or key in {"call_id", "status", "error_code"}}
        if attempt_target is not None:
            persisted["_attempt_target"] = attempt_target
        self._store.save_call_result(command_id, persisted)
        return result if name == "context.read" else project_business_result(result)

    def _read_scope(self, name, args, context, admission):
        """Limit model reads to current-context objects or a user-selected record."""
        intent = admission.user_intent or {}
        selected = ref_id(intent.get("selected_ref"))
        utterance = intent.get("utterance", "")
        target = ref_id(args.get("id"))
        if name in {"item.get", "item.list"}:
            items = context["items"] + context["eligible_items"] + context.get("recheck_items", [])
            permitted = {ref_id(view.get("id")) for view in items}
            return permitted if name == "item.list" else target in permitted
        if name in {"habit.get", "habit.list"} or (name == "settings.get" and args["scope"] == "habit"):
            permitted = {ref_id(view.get("id")) for view in context["habits"]}
            return permitted if name == "habit.list" else target in permitted
        if name == "phase.get" or (name == "settings.get" and args["scope"] == "current"):
            return not target or target in {selected, (context["current_phase"] or {}).get("id")} or target in utterance
        if name == "command.get_result":
            command_id = args["command_id"]
            return command_id == selected or command_id in utterance or any(
                call[0] == command_id for call in self._store.calls(admission.admission_id)
            )
        return True

    def _execute(self, name, args, authority, context, admission):
        try:
            if name == "context.read":
                query_now = self.clock.wall_now()
                at = datetime.fromisoformat(args["as_of"]) if args.get("as_of") else query_now
                history_start = datetime.fromisoformat(args["history_start"]) if args.get("history_start") else None
                history_end = datetime.fromisoformat(args["history_end"]) if args.get("history_end") else None
                if at > query_now or not at - timedelta(hours=1) <= (history_start or at - timedelta(hours=1)) < (
                    history_end or at
                ) <= at:
                    return {"call_id": authority.call_id, "status": "rejected", "error_code": "INVALID_ARGUMENT"}
                observation_options = {"document_query": admission.source == "user" or semantic_background_enabled(),
                                       "history_start": history_start,
                                       "history_end": history_end,
                                       "include_capture_health": args.get("include_capture_health") is True}
                if args.get("as_of"):
                    view = {**context, **self._observation(at, historical=True, **observation_options),
                            "as_of": at.isoformat()}
                else:
                    view, _ = self._snapshot(admission, at, **observation_options)
                return {"call_id": authority.call_id, "status": "applied", "view": view}
            if name == "decision.list":
                return {"call_id": authority.call_id, "status": "applied", "view": context["recent_decisions"]}
            permitted = self._read_scope(name, args, context, admission) if name in READ_TOOLS else True
            if permitted is False:
                return {"call_id": authority.call_id, "status": "rejected", "error_code": "NOT_AUTHORIZED"}
            if isinstance(permitted, set) and not permitted:
                return {"call_id": authority.call_id, "status": "applied", "view": []}
            result = _as_dict(self.gateway.execute(name, args, authority))
            result.setdefault("call_id", authority.call_id)
            if isinstance(permitted, set) and isinstance(result.get("view"), list):
                result["view"] = [view for view in result["view"] if ref_id(view.get("id")) in permitted]
            return result
        except (TimeoutError, sqlite3.Error, OSError, RuntimeError) as exc:
            log.warning("System 2 tool result unavailable (%s)", type(exc).__name__)
            result = self._lookup(authority.call_id)
            return result or {"call_id": authority.call_id, "status": "timeout", "error_code": "RESULT_UNKNOWN"}
        except (TypeError, ValueError):
            return {"call_id": authority.call_id, "status": "rejected", "error_code": "INVALID_ARGUMENT"}

    def _lookup(self, command_id):
        try:
            value = self.gateway.get_result(command_id, user_scope=self.user_scope)
        except (TimeoutError, sqlite3.Error, OSError):
            return None
        return _as_dict(value) if value is not None else None

    def _receipts(self, admission_id):
        receipts = []
        for command_id, name, declared_reason, recorded in self._store.calls(admission_id):
            if name in READ_TOOLS:
                continue
            if recorded is None or recorded.get("status") == "timeout":
                recorded = self._lookup(command_id) or recorded
            if recorded is None:
                recorded = {"call_id": command_id, "status": "timeout", "error_code": "RESULT_UNKNOWN"}
            if declared_reason in _REASONS:
                recorded = {**recorded, "decision_reason": declared_reason}
            receipts.append((name, recorded))
        return receipts

    def _decision(self, admission, now, receipts, engine_reply, failure, context):
        refs = list(admission.event["subject_refs"])
        code = failure or "quiet"
        action = "quiet"
        result_ref = next_check = None
        if receipts:
            name, result = receipts[-1]
            action = name
            code = result.get("decision_reason") if _original_status(result) in _SUCCESS else None
            code = code or _reason(result)
            result_ref = result.get("delivery_id") or result.get("subject_ref") or result.get("call_id")
            if result.get("subject_ref"):
                refs.append(result["subject_ref"])
            next_check = result.get("retry_at")
            explanation = (
                _REASONS.get(code, _ACTION_LABELS.get(action, "已处理。"))
                if _original_status(result) in _SUCCESS else "这次未能完成该操作。"
            )
        elif failure:
            action = failure
            explanation = _REASONS.get(code, "这次未能完成判断。")
        else:
            if admission.source == "background" and engine_reply.text.startswith("{"):
                try:
                    parsed = json.loads(engine_reply.text)
                    proposed_code = parsed.get("reason_code") if isinstance(parsed, dict) else None
                    if isinstance(proposed_code, str) and proposed_code in _REASONS:
                        code = proposed_code
                except (ValueError, TypeError):
                    pass
            elif admission.source == "user":
                action = "reply"
            explanation = _REASONS[code]
        return {"decision_id": admission.admission_id, "trigger_ids": admission.event["trigger_ids"],
                "as_of": timestamp(now), "context_revision": (context.get("current") or {}).get("context_revision"),
                "action": action, "reason_code": code, "explanation": explanation,
                "subject_refs": refs, "next_check_at": next_check, "result_ref": result_ref,
                "action_results": [
                    {key: result[key] for key in ("subject_ref", "delivery_id", "call_id", "error_code",
                                                  "retryable", "presentation_receipt") if key in result}
                    | {"action": name, "outcome": _tool_outcome(name, result)} for name, result in receipts
                ]}

    def _user_reply(self, admission, receipts, reply, failure, grants):
        if admission.source != "user":
            return None
        semantic = semantic_chat_enabled() and not (admission.user_intent or {}).get("proposal_button")
        if receipts:
            if not semantic:
                return _with_reply_source(_result_reply(*receipts[-1]), "program")
            actions = []
            for index, (name, result) in enumerate(receipts):
                target = result.get("_attempt_target")
                corrected = (target is not None and result.get("error_code") in _STALE | {"INVALID_ARGUMENT", "NOT_AUTHORIZED"}
                             and any(later.get("_attempt_target") == target and _original_status(later) in _SUCCESS
                                     for _, later in receipts[index + 1:]))
                if not corrected:
                    actions.append(_result_reply(name, result, admission.event.get("timezone", self.timezone)))
            follow_up = ""
            if not failure:
                sentences = re.split(r"(?<=[。！？!?])\s*", reply.text.strip()[:4000])
                follow_up = "".join(sentence for sentence in sentences if sentence and not _UNSUPPORTED_WRITE.search(sentence)
                                    and not re.fullmatch(r"(?:好的?|明白|收到|了解|已完成|已处理|已执行|完成|quiet)[。！! ]*", sentence))
            if len(actions) == 1 and not follow_up:
                return _with_reply_source(actions[0], "program")
            texts = [action["text"] for action in actions]
            parts = [{"source": "program", "text": text} for text in texts if text.strip()]
            if follow_up:
                texts.append(follow_up)
                parts.append({"source": "model", "text": follow_up})
            success = sum(action["status"] in _SUCCESS for action in actions)
            unresolved = follow_up.endswith(("？", "?"))
            status = ("applied" if success == len(actions) and not unresolved else
                      "partial" if success else "rejected")
            return {"text": "\n".join(texts), "status": status, "actions": actions, "parts": parts}
        if failure:
            return _with_reply_source(
                {"text": _REASONS.get(failure, "这次判断没有完成。"), "status": failure}, "program",
            )
        text = reply.text.strip()
        if grants and not semantic:
            if text.endswith(("？", "?")) and not _UNSUPPORTED_WRITE.search(text):
                text, status = "请再说明你要操作的对象或参数。", "clarification"
            else:
                text, status = "这次没有确认任何操作。", "failed"
        elif _UNSUPPORTED_WRITE.search(text):
            text, status = "这次没有执行成功。" if semantic else "这次没有确认任何操作。", "failed"
        elif text and text.casefold() != "quiet":
            status = "clarification" if text.endswith(("？", "?")) else "answered"
            return _with_reply_source({"text": text[:4000], "status": status}, "model")
        else:
            text, status = "这次没有收到答复。", "failed"
        return _with_reply_source({"text": text, "status": status}, "program")

    def _finish(self, admission, engine_reply, failure, grants, context, as_of, *, local_reply=None):
        current = self._store.state(admission.admission_id)
        receipts = self._receipts(admission.admission_id)
        if current.state in {"superseded", "interrupted"}:
            failure = current.state
            engine_reply = EngineReply("")
        if local_reply is not None and not failure:
            response = _with_reply_source(local_reply, "program")
        else:
            response = self._user_reply(admission, receipts, engine_reply, failure, grants)
        decision = self._decision(admission, as_of, receipts, engine_reply, failure, context)
        recover_wakes = None
        if (admission.source == "background" and failure in {"interrupted", "model_unavailable", "expired_request"}
                and semantic_background_enabled() and hasattr(self.scheduler, "retry_wakes")):
            recover_at = self.clock.wall_now()

            def recover_wakes(db):
                self.scheduler.retry_wakes(admission.event["trigger_ids"], now=recover_at,
                                           immediate=failure == "interrupted", db=db)

        self._store.finish(admission, decision, response, engine_reply.session_id, PROMPT_VERSION,
                           expected_state=current.state, recover_wakes=recover_wakes)

    def _chat_after_commit(self, admission, intent, bound, subject_ref, version):
        context, schedule = self._snapshot(admission, self.clock.wall_now())
        versions = self._authority_context(context, schedule)["expected_versions"]
        kind, _, identity = subject_ref.partition(":")

        def affected(key):
            return key in {subject_ref, identity} or (kind == "item" and key.startswith(f"occurrence:{identity}:"))

        # Carry forward this receipt's changes without swapping version guards on
        # unrelated objects that may have changed while the model was running.
        expected = {key: value for key, value in bound["expected_versions"].items() if not affected(key)}
        if type(version) is int:
            expected[subject_ref] = version
            if kind != "settings":
                expected[identity] = version
            expected.update((key, value) for key, value in versions.items() if affected(key) and value == version)
        updated = {**bound, "expected_versions": expected}
        return context, updated, chat_grants(intent, context)

    def _run(self, admission, as_of):
        if not admission.created_at <= as_of < admission.expires_at:
            self._finish(admission, EngineReply(""), "expired_request", {}, {}, as_of)
            return
        intent = admission.user_intent if admission.source == "user" else None
        semantic_chat = bool(intent is not None and semantic_chat_enabled())
        semantic_background = admission.source == "background" and semantic_background_enabled()
        document_query = semantic_background or bool(intent and _ACTIVITY_QUESTION.search(intent.get("utterance") or ""))
        observation_generation = self.settings.read().observation_generation
        observation_marker = self._observation_privacy_marker()
        context, schedule = self._snapshot(admission, as_of, document_query=document_query)
        if intent and intent.get("utterance") and not intent.get("reply_to_proposal_id"):
            non_cancelled = without_cancellation(intent["utterance"])
            literal_cancel = literal_start_cancellation(intent["utterance"])
            if non_cancelled != intent["utterance"]:
                if not non_cancelled and (not semantic_chat or literal_cancel and admission.supersedes):
                    old = self._receipts(admission.supersedes) if admission.supersedes else ()
                    committed = any(name == "phase.start" and _original_status(result) in _SUCCESS
                                    for name, result in old)
                    if committed:
                        text = "先前的开始请求已生效。"
                    elif admission.supersedes:
                        text = "已取消尚未生效的开始请求。"
                    else:
                        text = "你想取消哪一次开始请求？"
                    self._finish(admission, EngineReply(""), None, {}, context, as_of,
                                 local_reply={"text": text, "status": "answered"})
                    return
        timezone = admission.event.get("timezone", self.timezone)
        if intent is not None:
            intent = {**intent, "requested_at": admission.created_at.astimezone(resolve_timezone(timezone)).isoformat(),
                      "timezone": timezone}
        grants = chat_grants(intent, context) if semantic_chat else describe_grants(intent, context, requested_at=admission.created_at)
        bound = self._authority_context(context, schedule)
        if not self._store.bind(admission, grants, bound):
            return
        tool_lock = Lock()
        cached = {}
        needs_refresh = refreshed = attempted_write = False

        def call_tool(name, call_id, args, *, step_id=None, check_model_policy=None):
            nonlocal bound, context, needs_refresh, refreshed, attempted_write
            with tool_lock:
                if isinstance(name, str) and canonical_tool_name(name) not in READ_TOOLS:
                    attempted_write = True
                try:
                    key = name, call_id, json_text(args)
                except (TypeError, ValueError):
                    return {"call_id": call_id, "status": "rejected", "error_code": "INVALID_ARGUMENT"}
                if key in cached:
                    return cached[key]
                if (needs_refresh and not refreshed and canonical_tool_name(name) == "context.read"
                        and isinstance(args, dict) and not args.get("as_of")):
                    current, current_schedule = self._snapshot(admission, self.clock.wall_now())
                    new_grants = (chat_grants(intent, current) if semantic_chat else
                                  describe_grants(intent, current, requested_at=admission.created_at))
                    if semantic_chat or semantic_background or new_grants == grants:
                        updated = self._authority_context(current, current_schedule)
                        # The native tool inventory is admitted at the start of the turn.
                        # Retain only initially available functions during the replan.
                        new_grants = {name: value for name, value in new_grants.items() if name in grants}
                        if self._store.bind(admission, new_grants, updated):
                            context, bound = current, updated
                            grants.clear()
                            grants.update(new_grants)
                    refreshed = True
                result = self._call(admission, bound, context, grants, name, call_id, args, step_id=step_id,
                                    check_model_policy=check_model_policy)
                cached[key] = result
                if (semantic_chat and canonical_tool_name(name) not in READ_TOOLS and result.get("outcome") == "committed"
                        and (subject_ref := result.get("subject_ref"))):
                    current, updated, new_grants = self._chat_after_commit(
                        admission, intent, bound, subject_ref, result.get("actual_version"),
                    )
                    if self._store.bind(admission, new_grants, updated):
                        context, bound = current, updated
                        grants.clear()
                        grants.update(new_grants)
                needs_refresh = needs_refresh or result.get("error_code") in _STALE
                return result

        if intent and (button := intent.get("proposal_button")):
            call_tool("proposal.resolve", intent["message_id"], button, step_id=f'ui:{intent["message_id"]}')
            self._finish(admission, EngineReply(""), None, grants, context, as_of)
            return
        if intent and (language := timer_query_language(intent.get("utterance") or "")):
            result = call_tool("phase.get", "local:time-remaining", {}, step_id=admission.step_id)
            self._finish(admission, EngineReply(""), None, grants, context, as_of,
                         local_reply=_timer_reply(result, language))
            return
        command = None
        if semantic_chat:
            if intent.get("utterance") and not intent.get("reply_to_proposal_id"):
                args = literal_chat_reminder(intent["utterance"], admission.created_at, timezone)
                if args is not None:
                    command = "item.create", args
        else:
            command = deterministic_command(intent, grants)
        if command and command[0] == "item.create":
            # A literal short delay can expire while the cloud model is thinking.
            function, args = command
            due = datetime.fromisoformat(args["reminder_rule"]["remind_by"])
            if due <= self.clock.wall_now():
                self._finish(admission, EngineReply(""), "expired_request", grants, context, as_of)
            else:
                call_tool(function, "explicit:" + function, args, step_id=admission.step_id)
                self._finish(admission, EngineReply(""), None, grants, context, as_of)
            return
        utterance = normalized((intent or {}).get("utterance") or "")
        if (intent and not intent.get("reply_to_proposal_id")
                and (((not grants or semantic_chat) and _CAPABILITY_QUESTION.fullmatch(utterance))
                     or (semantic_chat and _CHAT_CAPABILITY_QUESTION.fullmatch(utterance)))):
            self._finish(admission, EngineReply(""), None, grants, context, as_of, local_reply={
                "text": "可以直接叫我设提醒、记事项或开始专注。比如「一分钟后提醒我喝水」。我会告诉你这件事是否已办成。",
                "status": "answered",
            })
            return
        request = TurnRequest(
            admission.admission_id, admission.step_id, SYSTEM_PROMPT, json_text({
                "origin": admission.source,
                "semantic_chat": semantic_chat,
                "semantic_background": semantic_background,
                "event": admission.event,
                "context": context,
                "user_intent": intent,
                "grants": grants,
            }), self.user_scope, PROMPT_VERSION, admission.source,
            allowed_tools=tuple(sorted(READ_TOOLS | grants.keys())), expires_at=admission.expires_at,
            observation_generation=observation_generation, observation_privacy_marker=observation_marker,
            observation_document_query=document_query,
        )
        failure = None
        try:
            response = self.engine.run(request, call_tool)
            reply = response if isinstance(response, EngineReply) else EngineReply(str(response or ""))
        except Exception as exc:
            log.info("System 2 model turn failed (%s)", type(exc).__name__)
            reply, failure = EngineReply(""), "model_unavailable"
        if not admission.created_at <= self.clock.wall_now() < admission.expires_at:
            reply, failure = EngineReply(""), "expired_request"
        if not self._stopping.is_set():
            if not semantic_chat and failure is None and not attempted_write and command:
                function, args = command
                call_tool(function, "explicit:" + function, args, step_id=admission.step_id)
            self._finish(admission, reply, failure, grants, context, as_of)

    def pump(self) -> bool:
        """Run one admitted turn. Qt uses start() and consumes the durable UI outbox."""
        if self._stopping.is_set() or not self._pump_lock.acquire(blocking=False):
            return False
        try:
            now = self.clock.wall_now()
            for interrupted in self._store.interrupted():
                self._finish(interrupted, EngineReply(""), interrupted.state, {}, {}, now)
            background = self._background(now)
            admission = self._store.claim(
                now, background=background, ack_wakes=self.scheduler.ack_wakes if background else None,
                claim_wakes=self._claim_wake_events if background and hasattr(self.scheduler, "claim_wakes") else None,
            )
            if admission is None:
                return False
            # A user command can commit between the polling sample and the claim.
            as_of = self.clock.wall_now()
            try:
                self._run(admission, as_of)
            except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
                log.warning("System 2 admission could not run (%s)", type(exc).__name__)
                self._finish(admission, EngineReply(""), "interrupted", {}, {}, as_of)
            return True
        finally:
            self._pump_lock.release()
