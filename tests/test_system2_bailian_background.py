from pathlib import Path
from types import SimpleNamespace

import pytest

from deskpet.observation_v2.routes import BAILIAN, BAILIAN_BASE_URL, BAILIAN_MODEL, SYSTEM2, CloudRoute, CloudRouteError
from deskpet.settings import SettingsStore
from deskpet.system2 import dsh


def test_bailian_background_uses_a_fresh_disposable_session_after_success_or_failure(tmp_path, monkeypatch):
    runtimes = []

    class RuntimeStub:
        def __init__(self, path, policy, *, session_id, private_storage, discard_after_turn, **_):
            assert discard_after_turn and private_storage is not None
            self.path = path
            self.policy = policy
            self.session_id = session_id
            self.discard_after_turn = discard_after_turn
            self.storage = private_storage
            self.closed = False
            self.events = []
            runtimes.append(self)

        def run(self, turn, _call_tool):
            self.events.append(turn.admission_id)
            if turn.admission_id == "overflowed":
                raise CloudRouteError("Provider rejected fixture request")
            return {"text": "ok", "metadata": {}}

        def close(self):
            self.closed = True
            self.storage.cleanup()

    monkeypatch.setattr(dsh, "_Runtime", RuntimeStub)
    settings = SettingsStore(tmp_path / "business.sqlite")
    route = CloudRoute(BAILIAN, BAILIAN_BASE_URL, BAILIAN_MODEL, "Bailian fixture", "fixture")
    settings.routes.record_result(settings.routes.configure(SYSTEM2, route), probe=True)
    engine = dsh.DSHEngine(tmp_path, settings=settings, tools=())
    turn = SimpleNamespace(
        prompt_version="fixture", system_prompt="Use only the current snapshot.",
        user_scope="default", source="background",
    )

    try:
        for identity in ("success", "overflowed", "following"):
            turn.admission_id = identity
            if identity == "overflowed":
                with pytest.raises(CloudRouteError, match="rejected fixture"):
                    engine.run(turn, lambda: None)
            else:
                assert engine.run(turn, lambda: None).text == "ok"
            assert runtimes[-1].closed and not Path(runtimes[-1].path).exists()

        assert len({runtime.session_id for runtime in runtimes}) == 3
        assert [runtime.events for runtime in runtimes] == [["success"], ["overflowed"], ["following"]]
    finally:
        engine.close()
