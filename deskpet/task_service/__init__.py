"""B's durable TaskService adapter; public contracts remain in deskpet.contracts."""

from .sqlite import SQLiteTaskService, factory, isolated_factory

__all__ = ["SQLiteTaskService", "factory", "isolated_factory"]
