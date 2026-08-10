"""Tests for the Direct Mem0 adapter lifecycle."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from headroom.memory.backends.direct_mem0 import DirectMem0Adapter, Mem0Config


def _adapter() -> DirectMem0Adapter:
    return DirectMem0Adapter(Mem0Config(enable_graph=True))


@pytest.mark.asyncio
async def test_close_drains_tasks_and_closes_initialized_resources() -> None:
    adapter = _adapter()
    resources = {
        "_mem0_client": MagicMock(),
        "_openai_client": MagicMock(),
        "_qdrant_client": MagicMock(),
        "_neo4j_driver": MagicMock(),
    }
    resources["_mem0_client"].close = AsyncMock()
    for name, resource in resources.items():
        setattr(adapter, name, resource)

    task = asyncio.create_task(asyncio.sleep(0, result="saved"))
    adapter._background_tasks["task_1"] = task

    await adapter.close(timeout=1.0)

    assert adapter.get_pending_tasks() == []
    assert adapter.get_task_status("task_1") == {
        "status": "completed",
        "result": "saved",
    }
    resources["_mem0_client"].close.assert_awaited_once_with()
    for resource in resources.values():
        resource.close.assert_called_once_with()
    assert adapter._initialized is False
    assert adapter._mem0_client is None
    assert adapter._openai_client is None
    assert adapter._qdrant_client is None
    assert adapter._neo4j_driver is None

    await adapter.close(timeout=0.01)
    resources["_mem0_client"].close.assert_awaited_once_with()
    for resource in resources.values():
        resource.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_close_cancels_tasks_that_exceed_timeout() -> None:
    adapter = _adapter()
    task = asyncio.create_task(asyncio.Event().wait())
    adapter._background_tasks["task_1"] = task
    await asyncio.sleep(0)

    await adapter.close(timeout=0.01)

    assert task.cancelled()
    assert adapter.get_pending_tasks() == []
    assert adapter.get_task_status("task_1") == {
        "status": "cancelled",
        "task_id": "task_1",
    }
