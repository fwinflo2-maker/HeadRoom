from __future__ import annotations

import asyncio
from typing import Any

import pytest

from headroom.memory.backends import direct_mem0
from headroom.memory.backends.direct_mem0 import DirectMem0Adapter, Mem0Config
from headroom.memory.models import Memory


async def _start_background_save(adapter: DirectMem0Adapter, content: str = "fact") -> str:
    result = await adapter.save_memory(
        content=content,
        user_id="alice",
        importance=0.8,
        background=True,
    )
    assert isinstance(result, Memory)
    return str(result.metadata["_task_id"])


async def _wait_for_completion_callback(adapter: DirectMem0Adapter, task_id: str) -> None:
    for _ in range(10):
        if task_id not in adapter._background_tasks:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"Completion callback did not run for {task_id}")


@pytest.fixture
def adapter() -> DirectMem0Adapter:
    instance = DirectMem0Adapter(Mem0Config(enable_graph=False))
    instance._initialized = True
    return instance


@pytest.mark.asyncio
async def test_background_completion_is_captured_without_polling(
    adapter: DirectMem0Adapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored_memory = Memory(content="stored", user_id="alice")

    async def save(**kwargs: Any) -> Memory:
        return stored_memory

    monkeypatch.setattr(adapter, "_save_memory_internal", save)

    task_id = await _start_background_save(adapter)
    await _wait_for_completion_callback(adapter, task_id)

    assert adapter.get_pending_tasks() == []
    assert adapter.get_task_status(task_id) == {
        "status": "completed",
        "result": stored_memory,
    }


@pytest.mark.asyncio
async def test_background_exception_is_captured_without_polling(
    adapter: DirectMem0Adapter,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail(**kwargs: Any) -> Memory:
        raise RuntimeError("save failed")

    monkeypatch.setattr(adapter, "_save_memory_internal", fail)

    task_id = await _start_background_save(adapter)
    await _wait_for_completion_callback(adapter, task_id)

    assert adapter.get_task_status(task_id) == {
        "status": "failed",
        "error": "save failed",
    }
    assert f"task_id={task_id}" in caplog.text


@pytest.mark.asyncio
async def test_task_results_evict_oldest_entry_at_capacity(
    adapter: DirectMem0Adapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    save_number = 0

    async def save(**kwargs: Any) -> Memory:
        nonlocal save_number
        save_number += 1
        return Memory(content=f"stored-{save_number}", user_id="alice")

    monkeypatch.setattr(adapter, "_save_memory_internal", save)
    monkeypatch.setattr(direct_mem0, "_MAX_TASK_RESULTS", 2)
    monkeypatch.setattr(direct_mem0, "monotonic", lambda: now)

    task_ids = []
    for index in range(3):
        now = float(index)
        task_id = await _start_background_save(adapter, content=f"fact-{index}")
        await _wait_for_completion_callback(adapter, task_id)
        task_ids.append(task_id)

    assert adapter.get_task_status(task_ids[0]) == {
        "status": "not_found",
        "task_id": task_ids[0],
    }
    assert adapter.get_task_status(task_ids[1])["status"] == "completed"
    assert adapter.get_task_status(task_ids[2])["status"] == "completed"


@pytest.mark.asyncio
async def test_task_results_expire_after_ttl(
    adapter: DirectMem0Adapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0

    async def save(**kwargs: Any) -> Memory:
        return Memory(content="stored", user_id="alice")

    monkeypatch.setattr(adapter, "_save_memory_internal", save)
    monkeypatch.setattr(direct_mem0, "_TASK_RESULT_TTL_SECONDS", 10)
    monkeypatch.setattr(direct_mem0, "monotonic", lambda: now)

    task_id = await _start_background_save(adapter)
    await _wait_for_completion_callback(adapter, task_id)
    assert adapter.get_task_status(task_id)["status"] == "completed"

    now = 11.0
    assert adapter.get_task_status(task_id) == {
        "status": "not_found",
        "task_id": task_id,
    }


@pytest.mark.asyncio
async def test_wait_timeout_does_not_cancel_background_save(
    adapter: DirectMem0Adapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()

    async def save(**kwargs: Any) -> Memory:
        await release.wait()
        return Memory(content="stored", user_id="alice")

    monkeypatch.setattr(adapter, "_save_memory_internal", save)

    task_id = await _start_background_save(adapter)
    assert await adapter.wait_for_task(task_id, timeout=0.001) == {
        "status": "timeout",
        "task_id": task_id,
    }
    assert task_id in adapter.get_pending_tasks()

    release.set()
    await _wait_for_completion_callback(adapter, task_id)
    assert adapter.get_task_status(task_id)["status"] == "completed"


@pytest.mark.asyncio
async def test_close_flushes_writes_before_closing_resources(
    adapter: DirectMem0Adapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()
    write_completed = asyncio.Event()

    class Driver:
        closed = False

        def close(self) -> None:
            assert write_completed.is_set()
            self.closed = True

    driver = Driver()
    adapter._neo4j_driver = driver

    async def save(**kwargs: Any) -> Memory:
        await release.wait()
        write_completed.set()
        return Memory(content="stored", user_id="alice")

    monkeypatch.setattr(adapter, "_save_memory_internal", save)

    task_id = await _start_background_save(adapter)
    close_task = asyncio.create_task(adapter.close())
    await asyncio.sleep(0)

    assert not close_task.done()
    assert not driver.closed
    assert adapter.get_task_status(task_id)["status"] == "processing"
    with pytest.raises(RuntimeError, match="adapter is closing"):
        await _start_background_save(adapter, content="too late")

    release.set()
    await close_task

    assert driver.closed
    assert adapter._background_tasks == {}
    assert adapter._task_results == {}
    assert adapter._initialized is False
