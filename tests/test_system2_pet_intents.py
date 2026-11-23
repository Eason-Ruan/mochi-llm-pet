from contextlib import closing
from dataclasses import replace
from datetime import timedelta
import sqlite3

import pytest

from deskpet.c_ui.desktop_system2 import PetIntentStore
from deskpet.mocks.clock import FakeClock
from deskpet.mocks.delivery import FakeDeliveryPort
from deskpet.observation_v2.context import RealtimeBridge, RealtimeHead
from deskpet.observation_v2.routes import SYSTEM2, CloudRoute
from deskpet.observation_v2.state import Observation
from deskpet.settings import SettingsStore
from deskpet.system2.protocol import ToolAuthority
from deskpet.system2.scheduler import Scheduler
from deskpet.task_service.sqlite import SQLiteTaskService


class LiveObservation:
    def __init__(self, now):
        self.current = Observation("work", "available", None, (), now, now + timedelta(seconds=10), 71)
        self.cursor = 1

    def read_realtime_bridge(self, now, **_):
        if now >= self.current.valid_until:
            return RealtimeBridge(RealtimeHead(None, "expired", self.cursor, "producer-test"))
        return RealtimeBridge(RealtimeHead(self.current, "fresh", self.cursor, "producer-test"))


@pytest.fixture
def pet(tmp_path):
    path = tmp_path / "business.sqlite3"
    clock = FakeClock()
    tasks = SQLiteTaskService(clock, FakeDeliveryPort(clock), lambda *a, **k: None, path=path)
    observation = LiveObservation(clock.wall_now())
    settings = SettingsStore(path)
    lock = {"active": False}

    def locked():
        return lock["active"]

    scheduler = Scheduler(tasks, observation=observation, clock=clock, lock_probe=locked)
    store = PetIntentStore(path, locked, scheduler, settings.routes)
    scheduler.poll()
    try:
        yield clock, observation, settings, lock, scheduler, store
    finally:
        tasks.close()


def offer(pet):
    clock, observation, settings, _, scheduler, store = pet
    now = clock.wall_now()
    authority = ToolAuthority(
        "turn", "step", "call", 1, "default", "agent", "wake", now, now + timedelta(seconds=30),
        context_revision=observation.current.context_revision, observation_cursor=observation.cursor,
        observation_generation="producer-test", observation_valid_until=observation.current.valid_until,
        opportunity_id=scheduler.get_opportunity(),
    )
    with closing(sqlite3.connect(settings.path)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        result = store.present(intent="wave", position_hint="screen_edge", sound=True,
                               authority=authority, now=now, db=db)
    assert result["status"] == "pending"
    return result["delivery_id"], authority.opportunity_id


def test_motion_and_proposal_share_slot_and_motion_is_consumed_once(pet):
    clock, _, _, _, scheduler, store = pet
    delivery_id, opportunity_id = offer(pet)
    assert not scheduler.claim_opportunity(opportunity_id=opportunity_id, delivery_id="proposal",
                                           now=clock.wall_now())
    effect = store.claim(delivery_id, clock.wall_now())
    assert effect == {"position_hint": "screen_edge", "animation": "wave", "sound": True}
    store.receipt(delivery_id, sound_requested=True)
    assert store.claim(delivery_id, clock.wall_now()) is None
    assert not scheduler.claim_opportunity(opportunity_id=opportunity_id, delivery_id="proposal",
                                           now=clock.wall_now())


@pytest.mark.parametrize("invalidate", ["revision", "expiry", "lock", "recipient", "consent"])
def test_queued_pet_intent_rechecks_live_authority_at_claim(pet, invalidate):
    clock, observation, settings, lock, scheduler, store = pet
    if invalidate == "consent":
        route = CloudRoute("openai_compatible", "https://test.example/v1", "test-model", "Test endpoint")
        routes = settings.routes
        configured = routes.configure(SYSTEM2, route)
        allowed = routes.set_permission(SYSTEM2, True, expected=configured)
    delivery_id, opportunity_id = offer(pet)
    if invalidate == "revision":
        observation.cursor += 1
        observation.current = replace(observation.current, context_revision=72)
    elif invalidate == "expiry":
        clock.advance(10)
    elif invalidate == "lock":
        lock["active"] = True
    elif invalidate == "consent":
        routes.set_permission(SYSTEM2, False, expected=allowed)
    else:
        route = CloudRoute("openai_compatible", "https://test.example/v1", "test-model", "Test endpoint")
        settings.routes.configure(SYSTEM2, route)
    assert store.claim(delivery_id, clock.wall_now()) is None
    assert store.pending(clock.wall_now()) == ()
    if invalidate in {"revision", "consent", "recipient"}:
        assert scheduler.claim_opportunity(opportunity_id=opportunity_id, delivery_id="proposal",
                                          now=clock.wall_now())
