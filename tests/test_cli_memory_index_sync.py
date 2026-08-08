"""Tests for memory CLI index synchronization (issue #2856).

Verifies that headroom memory delete/prune/purge/edit remove stale entries
from the FTS5 and vector search indexes, not just from the primary store.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from headroom.cli.memory import (
    _clear_all_search_indexes,
    _remove_from_search_indexes,
)
from headroom.memory.adapters.fts5 import FTS5TextIndex
from headroom.memory.adapters.sqlite import SQLiteMemoryStore
from headroom.memory.models import Memory


def _make_memory(memory_id: str, content: str = "test content") -> Memory:
    return Memory(
        id=memory_id,
        content=content,
        user_id="test-user",
    )


def _seed_fts(db_path: Path, memories: list[Memory]) -> None:
    """Index memories into the FTS5 table."""
    fts = FTS5TextIndex(db_path=str(db_path))
    for mem in memories:
        asyncio.run(fts.index_memory(mem))


def _seed_vector(db_path: Path, memory_ids: list[str]) -> None:
    """Manually insert rows into vec_metadata (no sqlite-vec extension needed)."""
    vector_db = db_path.parent / f"{db_path.stem}_vectors.db"
    with sqlite3.connect(str(vector_db)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vec_metadata (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id TEXT NOT NULL UNIQUE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vec_embeddings (
                rowid INTEGER PRIMARY KEY,
                data BLOB
            )
        """)
        for mid in memory_ids:
            conn.execute("INSERT OR IGNORE INTO vec_metadata (memory_id) VALUES (?)", (mid,))
            rowid = conn.execute(
                "SELECT rowid FROM vec_metadata WHERE memory_id = ?", (mid,)
            ).fetchone()[0]
            conn.execute(
                "INSERT OR IGNORE INTO vec_embeddings (rowid, data) VALUES (?, ?)",
                (rowid, b"fake-embedding"),
            )
        conn.commit()


def _fts_count(db_path: Path) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]


def _fts_ids(db_path: Path) -> set[str]:
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute("SELECT memory_id FROM memory_fts").fetchall()
    return {r[0] for r in rows}


def _vector_ids(db_path: Path) -> set[str]:
    vector_db = db_path.parent / f"{db_path.stem}_vectors.db"
    if not vector_db.exists():
        return set()
    with sqlite3.connect(str(vector_db)) as conn:
        rows = conn.execute("SELECT memory_id FROM vec_metadata").fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# _remove_from_search_indexes
# ---------------------------------------------------------------------------

def test_remove_from_search_indexes_clears_fts_entries(tmp_path):
    db_path = tmp_path / "memory.db"
    mems = [_make_memory(f"id-{i}") for i in range(3)]
    _seed_fts(db_path, mems)
    assert _fts_count(db_path) == 3

    _remove_from_search_indexes(str(db_path), ["id-0", "id-2"])

    assert _fts_ids(db_path) == {"id-1"}


def test_remove_from_search_indexes_clears_vector_entries(tmp_path):
    db_path = tmp_path / "memory.db"
    _seed_fts(db_path, [])  # ensure db exists
    _seed_vector(db_path, ["id-0", "id-1", "id-2"])
    assert _vector_ids(db_path) == {"id-0", "id-1", "id-2"}

    _remove_from_search_indexes(str(db_path), ["id-0", "id-2"])

    assert _vector_ids(db_path) == {"id-1"}


def test_remove_from_search_indexes_no_vector_db_is_noop(tmp_path):
    db_path = tmp_path / "memory.db"
    mems = [_make_memory("id-0")]
    _seed_fts(db_path, mems)

    # No vector DB → should not raise
    _remove_from_search_indexes(str(db_path), ["id-0"])

    assert _fts_count(db_path) == 0


def test_remove_from_search_indexes_empty_list_is_noop(tmp_path):
    db_path = tmp_path / "memory.db"
    _seed_fts(db_path, [_make_memory("id-0")])
    assert _fts_count(db_path) == 1

    _remove_from_search_indexes(str(db_path), [])

    assert _fts_count(db_path) == 1


# ---------------------------------------------------------------------------
# _clear_all_search_indexes
# ---------------------------------------------------------------------------

def test_clear_all_search_indexes_removes_all_fts_entries(tmp_path):
    db_path = tmp_path / "memory.db"
    _seed_fts(db_path, [_make_memory(f"id-{i}") for i in range(5)])
    assert _fts_count(db_path) == 5

    _clear_all_search_indexes(str(db_path))

    assert _fts_count(db_path) == 0


def test_clear_all_search_indexes_removes_all_vector_entries(tmp_path):
    db_path = tmp_path / "memory.db"
    _seed_fts(db_path, [])
    _seed_vector(db_path, ["id-0", "id-1"])

    _clear_all_search_indexes(str(db_path))

    assert _vector_ids(db_path) == set()


def test_clear_all_search_indexes_no_vector_db_is_noop(tmp_path):
    db_path = tmp_path / "memory.db"
    _seed_fts(db_path, [_make_memory("id-0")])

    _clear_all_search_indexes(str(db_path))

    assert _fts_count(db_path) == 0  # FTS cleared; no vector DB is fine


# ---------------------------------------------------------------------------
# Integration: delete command wires up index sync
# ---------------------------------------------------------------------------

def test_delete_command_removes_from_fts(tmp_path, monkeypatch):
    """Simulate what the delete command does: delete_batch then _remove_from_search_indexes."""
    db_path = tmp_path / "memory.db"
    store = SQLiteMemoryStore(str(db_path))
    mem = _make_memory("abc123")
    asyncio.run(store.save(mem))
    _seed_fts(db_path, [mem])
    assert _fts_count(db_path) == 1

    asyncio.run(store.delete_batch(["abc123"]))
    _remove_from_search_indexes(str(db_path), ["abc123"])

    assert _fts_count(db_path) == 0


def test_purge_command_clears_fts(tmp_path):
    """Simulate what the purge command does: clear_all then _clear_all_search_indexes."""
    db_path = tmp_path / "memory.db"
    store = SQLiteMemoryStore(str(db_path))
    for i in range(3):
        asyncio.run(store.save(_make_memory(f"id-{i}")))
    _seed_fts(db_path, [_make_memory(f"id-{i}") for i in range(3)])
    assert _fts_count(db_path) == 3

    asyncio.run(store.clear_all())
    _clear_all_search_indexes(str(db_path))

    assert _fts_count(db_path) == 0
