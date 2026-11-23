"""Run every existing shared assertion against a real isolated SQLite database."""
import pytest

from tests.m0.test_task_service import *  # noqa: F403 - intentionally unchanged contract suite
from deskpet.task_service import isolated_factory


@pytest.fixture
def task_service_factory():
    return isolated_factory
