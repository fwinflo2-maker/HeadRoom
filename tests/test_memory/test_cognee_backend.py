"""Tests for the cognee memory backend and its env-var resolution.

Covers:
- ``cognee_env`` readers (dataset/system_root/data_root/search_type/auto_cognify)
- ``CogneeConfig`` defaults and env pickup via ``field(default_factory=...)``
- ``save_memory`` mapping to ``cognee.add`` (node_set tags, facts) and the
  ``cognee.cognify`` trigger per config
- ``search_memories`` mapping to ``cognee.search`` (node_name scoping, top_k,
  GRAPH_COMPLETION only_context) and result -> MemorySearchResult conversion
- Tombstone semantics for update/delete
- ImportError guard message when cognee is not installed
- ``supports_graph`` / ``supports_vector_search`` capability flags

cognee is NOT a test dependency: a fake module is injected into
``sys.modules`` before the backend's lazy import runs.
"""

from __future__ import annotations

import os
import sys
import types
from enum import Enum
from types import SimpleNamespace
from typing import Any

import pytest

from headroom.memory import cognee_env
from headroom.memory.backends.cognee import CogneeBackend, CogneeConfig
from headroom.memory.cognee_env import (
    DEFAULT_COGNEE_DATASET,
    DEFAULT_COGNEE_SEARCH_TYPE,
    cognee_env_auto_cognify,
    cognee_env_data_root,
    cognee_env_dataset,
    cognee_env_search_type,
    cognee_env_system_root,
)
from headroom.memory.ports import MemorySearchResult

# All HEADROOM_COGNEE_* vars are cleared before every test so the host
# environment cannot leak into unit tests.
_COGNEE_ENV_VARS = (
    "HEADROOM_COGNEE_DATASET",
    "HEADROOM_COGNEE_SYSTEM_ROOT",
    "HEADROOM_COGNEE_DATA_ROOT",
    "HEADROOM_COGNEE_SEARCH_TYPE",
    "HEADROOM_COGNEE_AUTO_COGNIFY",
)


@pytest.fixture(autouse=True)
def _clear_cognee_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _COGNEE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# =============================================================================
# Fake cognee module
# =============================================================================


class _FakeSearchType(str, Enum):
    """Stand-in for cognee.SearchType (a str enum in the real package)."""

    CHUNKS = "CHUNKS"
    GRAPH_COMPLETION = "GRAPH_COMPLETION"
    SUMMARIES = "SUMMARIES"


def _make_fake_cognee(
    search_results: list[Any] | None = None,
    cognify_error: Exception | None = None,
) -> tuple[types.ModuleType, SimpleNamespace]:
    """Build a fake ``cognee`` module plus a call recorder."""
    calls = SimpleNamespace(add=[], cognify=[], search=[], system_root=[], data_root=[])
    module = types.ModuleType("cognee")

    async def add(data, dataset_name=None, node_set=None, **kwargs):
        calls.add.append({"data": data, "dataset_name": dataset_name, "node_set": node_set})

    async def cognify(datasets=None, run_in_background=None, **kwargs):
        calls.cognify.append({"datasets": datasets, "run_in_background": run_in_background})
        if cognify_error is not None:
            raise cognify_error

    async def search(**kwargs):
        calls.search.append(kwargs)
        return list(search_results or [])

    module.SearchType = _FakeSearchType
    module.add = add
    module.cognify = cognify
    module.search = search
    module.config = SimpleNamespace(
        system_root_directory=calls.system_root.append,
        data_root_directory=calls.data_root.append,
    )
    return module, calls


def _fake_hit(payload: Any, dataset_name: str = "headroom_memories") -> SimpleNamespace:
    """Build a fake cognee SearchResult (.search_result/.dataset_id/.dataset_name)."""
    return SimpleNamespace(search_result=payload, dataset_id="ds-1", dataset_name=dataset_name)


@pytest.fixture
def fake_cognee(monkeypatch: pytest.MonkeyPatch):
    """Inject a default fake cognee module; returns its call recorder.

    Tests that need custom search results or a failing cognify build their
    own module via ``_make_fake_cognee`` and ``monkeypatch.setitem``.
    """
    module, calls = _make_fake_cognee()
    monkeypatch.setitem(sys.modules, "cognee", module)
    return calls


# =============================================================================
# cognee_env readers
# =============================================================================


class TestCogneeEnvReaders:
    def test_dataset_defaults_when_unset(self) -> None:
        assert cognee_env_dataset() == DEFAULT_COGNEE_DATASET

    def test_dataset_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEADROOM_COGNEE_DATASET", "my_dataset")
        assert cognee_env_dataset() == "my_dataset"

    def test_dataset_trims_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEADROOM_COGNEE_DATASET", "  padded  ")
        assert cognee_env_dataset() == "padded"

    def test_dataset_empty_string_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEADROOM_COGNEE_DATASET", "")
        assert cognee_env_dataset() == DEFAULT_COGNEE_DATASET

    def test_system_root_none_when_unset(self) -> None:
        assert cognee_env_system_root() is None

    def test_system_root_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEADROOM_COGNEE_SYSTEM_ROOT", "/var/cognee/system")
        assert cognee_env_system_root() == "/var/cognee/system"

    def test_data_root_none_when_unset(self) -> None:
        assert cognee_env_data_root() is None

    def test_data_root_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEADROOM_COGNEE_DATA_ROOT", "/var/cognee/data")
        assert cognee_env_data_root() == "/var/cognee/data"

    def test_search_type_defaults_when_unset(self) -> None:
        assert cognee_env_search_type() == DEFAULT_COGNEE_SEARCH_TYPE == "CHUNKS"

    def test_search_type_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEADROOM_COGNEE_SEARCH_TYPE", "GRAPH_COMPLETION")
        assert cognee_env_search_type() == "GRAPH_COMPLETION"

    def test_auto_cognify_defaults_true(self) -> None:
        assert cognee_env_auto_cognify() is True

    @pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "y", "on"])
    def test_auto_cognify_truthy_values(self, monkeypatch: pytest.MonkeyPatch, truthy: str) -> None:
        monkeypatch.setenv("HEADROOM_COGNEE_AUTO_COGNIFY", truthy)
        assert cognee_env_auto_cognify() is True

    @pytest.mark.parametrize("falsy", ["0", "false", "FALSE", "no", "n", "off"])
    def test_auto_cognify_falsy_values(self, monkeypatch: pytest.MonkeyPatch, falsy: str) -> None:
        monkeypatch.setenv("HEADROOM_COGNEE_AUTO_COGNIFY", falsy)
        assert cognee_env_auto_cognify() is False

    def test_auto_cognify_rejects_garbage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEADROOM_COGNEE_AUTO_COGNIFY", "maybe")
        with pytest.raises(ValueError, match="Invalid boolean value"):
            cognee_env_auto_cognify()

    def test_module_exports_readers(self) -> None:
        assert callable(cognee_env.cognee_env_dataset)
        assert callable(cognee_env.cognee_env_system_root)
        assert callable(cognee_env.cognee_env_data_root)
        assert callable(cognee_env.cognee_env_search_type)
        assert callable(cognee_env.cognee_env_auto_cognify)


# =============================================================================
# CogneeConfig
# =============================================================================


class TestCogneeConfig:
    def test_defaults_when_no_env(self) -> None:
        cfg = CogneeConfig()
        assert cfg.dataset_name == "headroom_memories"
        assert cfg.system_root is None
        assert cfg.data_root is None
        assert cfg.search_type == "CHUNKS"
        assert cfg.auto_cognify is True
        assert cfg.background_cognify is True

    def test_defaults_read_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEADROOM_COGNEE_DATASET", "env_dataset")
        monkeypatch.setenv("HEADROOM_COGNEE_SYSTEM_ROOT", "/sys/root")
        monkeypatch.setenv("HEADROOM_COGNEE_DATA_ROOT", "/data/root")
        monkeypatch.setenv("HEADROOM_COGNEE_SEARCH_TYPE", "GRAPH_COMPLETION")
        monkeypatch.setenv("HEADROOM_COGNEE_AUTO_COGNIFY", "false")

        cfg = CogneeConfig()
        assert cfg.dataset_name == "env_dataset"
        assert cfg.system_root == "/sys/root"
        assert cfg.data_root == "/data/root"
        assert cfg.search_type == "GRAPH_COMPLETION"
        assert cfg.auto_cognify is False

    def test_explicit_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEADROOM_COGNEE_DATASET", "env_dataset")
        monkeypatch.setenv("HEADROOM_COGNEE_AUTO_COGNIFY", "false")

        cfg = CogneeConfig(dataset_name="explicit_dataset", auto_cognify=True)
        assert cfg.dataset_name == "explicit_dataset"
        assert cfg.auto_cognify is True


# =============================================================================
# Lazy import / ImportError guard
# =============================================================================


class TestImportGuard:
    def test_construction_does_not_import_cognee(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Poison the import: construction must still succeed (lazy import).
        monkeypatch.setitem(sys.modules, "cognee", None)
        backend = CogneeBackend(CogneeConfig())
        assert backend is not None

    async def test_save_raises_install_hint_when_cognee_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "cognee", None)
        backend = CogneeBackend(CogneeConfig())
        with pytest.raises(ImportError, match=r'pip install "headroom-ai\[cognee\]"'):
            await backend.save_memory(content="x", user_id="alice", importance=0.5)

    async def test_search_raises_install_hint_when_cognee_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "cognee", None)
        backend = CogneeBackend(CogneeConfig())
        with pytest.raises(ImportError, match=r'pip install "headroom-ai\[cognee\]"'):
            await backend.search_memories(query="x", user_id="alice")


# =============================================================================
# Initialization (directory isolation)
# =============================================================================


class TestInitialization:
    async def test_applies_root_directories_when_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module, calls = _make_fake_cognee()
        monkeypatch.setitem(sys.modules, "cognee", module)

        backend = CogneeBackend(CogneeConfig(system_root="/sys/x", data_root="/data/y"))
        await backend.ensure_initialized()

        assert calls.system_root == ["/sys/x"]
        assert calls.data_root == ["/data/y"]

    async def test_skips_root_directories_when_unset(self, fake_cognee) -> None:
        backend = CogneeBackend(CogneeConfig())
        await backend.ensure_initialized()

        assert fake_cognee.system_root == []
        assert fake_cognee.data_root == []

    async def test_close_resets_and_allows_reinit(self, fake_cognee) -> None:
        backend = CogneeBackend(CogneeConfig())
        await backend.ensure_initialized()
        await backend.close()
        assert backend._initialized is False

        # Reusable after close.
        await backend.save_memory(content="again", user_id="alice", importance=0.5)
        assert len(fake_cognee.add) == 1


# =============================================================================
# save_memory
# =============================================================================


class TestSaveMemory:
    async def test_maps_content_and_scoping_to_add_node_set(self, fake_cognee) -> None:
        backend = CogneeBackend(CogneeConfig(dataset_name="ds"))
        memory = await backend.save_memory(
            content="Alice prefers Python",
            user_id="alice",
            importance=0.8,
            entities=["python", "ml"],
            session_id="s1",
        )

        assert len(fake_cognee.add) == 1
        call = fake_cognee.add[0]
        assert call["data"] == "Alice prefers Python"
        assert call["dataset_name"] == "ds"
        assert call["node_set"] == ["user:alice", "session:s1", "entity:python", "entity:ml"]

        assert memory.content == "Alice prefers Python"
        assert memory.user_id == "alice"
        assert memory.session_id == "s1"
        assert memory.importance == 0.8
        assert memory.entity_refs == ["python", "ml"]
        assert memory.id

    async def test_node_set_omits_session_and_entities_when_absent(self, fake_cognee) -> None:
        backend = CogneeBackend(CogneeConfig())
        await backend.save_memory(content="c", user_id="bob", importance=0.5)
        assert fake_cognee.add[0]["node_set"] == ["user:bob"]

    async def test_facts_are_added_alongside_content(self, fake_cognee) -> None:
        backend = CogneeBackend(CogneeConfig())
        memory = await backend.save_memory(
            content="Alice works at Netflix using Python",
            user_id="alice",
            importance=0.5,
            facts=["Alice works at Netflix", "Alice uses Python"],
        )
        assert fake_cognee.add[0]["data"] == [
            "Alice works at Netflix using Python",
            "Alice works at Netflix",
            "Alice uses Python",
        ]
        assert memory.metadata["_fact_count"] == 2

    async def test_triggers_cognify_by_default_in_background(self, fake_cognee) -> None:
        backend = CogneeBackend(CogneeConfig(dataset_name="ds"))
        await backend.save_memory(content="c", user_id="alice", importance=0.5)

        assert fake_cognee.cognify == [{"datasets": ["ds"], "run_in_background": True}]

    async def test_no_cognify_when_auto_cognify_disabled(self, fake_cognee) -> None:
        backend = CogneeBackend(CogneeConfig(auto_cognify=False))
        await backend.save_memory(content="c", user_id="alice", importance=0.5)
        assert fake_cognee.cognify == []

    async def test_foreground_cognify_when_background_disabled(self, fake_cognee) -> None:
        backend = CogneeBackend(CogneeConfig(background_cognify=False))
        await backend.save_memory(content="c", user_id="alice", importance=0.5)
        assert fake_cognee.cognify[0]["run_in_background"] is False

    async def test_cognify_failure_does_not_lose_save(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module, calls = _make_fake_cognee(cognify_error=RuntimeError("LLM down"))
        monkeypatch.setitem(sys.modules, "cognee", module)

        backend = CogneeBackend(CogneeConfig())
        memory = await backend.save_memory(content="kept", user_id="alice", importance=0.5)

        assert len(calls.add) == 1
        assert memory.content == "kept"
        assert await backend.get_memory(memory.id) is memory

    async def test_relationships_and_extractions_recorded_in_metadata(self, fake_cognee) -> None:
        backend = CogneeBackend(CogneeConfig(dataset_name="ds"))
        memory = await backend.save_memory(
            content="Alice works at Netflix",
            user_id="alice",
            importance=0.5,
            relationships=[{"source": "Alice", "relationship": "works_at", "target": "Netflix"}],
            extracted_entities=[{"entity": "Alice", "entity_type": "person"}],
            extracted_relationships=[{"source": "Alice", "target": "Netflix"}],
            metadata={"origin": "test"},
        )
        assert memory.metadata["origin"] == "test"
        assert memory.metadata["_cognee_dataset"] == "ds"
        assert memory.metadata["_cognee_node_set"] == ["user:alice"]
        assert memory.metadata["relationships"] == [
            {"source": "Alice", "relationship": "works_at", "target": "Netflix"}
        ]
        assert memory.metadata["extracted_entities"] == [
            {"entity": "Alice", "entity_type": "person"}
        ]
        assert memory.metadata["extracted_relationships"] == [
            {"source": "Alice", "target": "Netflix"}
        ]


# =============================================================================
# search_memories
# =============================================================================


class TestSearchMemories:
    async def test_passes_query_scoping_and_top_k_to_cognee(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module, calls = _make_fake_cognee(search_results=[])
        monkeypatch.setitem(sys.modules, "cognee", module)

        backend = CogneeBackend(CogneeConfig(dataset_name="ds"))
        await backend.search_memories(
            query="python preferences",
            user_id="alice",
            entities=["python"],
            top_k=7,
            session_id="s1",
        )

        assert len(calls.search) == 1
        kwargs = calls.search[0]
        assert kwargs["query_text"] == "python preferences"
        assert kwargs["query_type"] is _FakeSearchType.CHUNKS
        assert kwargs["datasets"] == ["ds"]
        assert kwargs["top_k"] == 7
        assert kwargs["node_name"] == ["user:alice", "session:s1", "entity:python"]
        # AND semantics: every tag must match. cognee's default is OR, which
        # would leak other users' memories sharing a (global) entity tag.
        assert kwargs["node_name_filter_operator"] == "AND"
        assert "only_context" not in kwargs

    async def test_maps_results_to_memory_search_results(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module, _ = _make_fake_cognee(
            search_results=[_fake_hit(["first chunk", "second chunk"], dataset_name="ds")]
        )
        monkeypatch.setitem(sys.modules, "cognee", module)

        backend = CogneeBackend(CogneeConfig(dataset_name="ds"))
        results = await backend.search_memories(query="q", user_id="alice", session_id="s1")

        assert len(results) == 2
        assert all(isinstance(r, MemorySearchResult) for r in results)
        assert [r.memory.content for r in results] == ["first chunk", "second chunk"]
        assert all(r.memory.user_id == "alice" for r in results)
        assert all(r.memory.session_id == "s1" for r in results)
        assert all(r.memory.metadata["_cognee_dataset_name"] == "ds" for r in results)
        # Rank-based scores, descending, compressed into (0.5, 1.0] so the
        # proxy's default min_similarity floor (0.3) never filters them.
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)
        assert scores[0] == 1.0
        assert all(0.5 < s <= 1.0 for s in scores)

    async def test_result_ids_are_stable_and_registered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Search-result IDs are content-derived, repeatable, and resolvable."""
        module, _ = _make_fake_cognee(search_results=[_fake_hit(["a fact"])])
        monkeypatch.setitem(sys.modules, "cognee", module)

        backend = CogneeBackend(CogneeConfig())
        first = await backend.search_memories(query="q", user_id="alice")
        second = await backend.search_memories(query="q", user_id="alice")

        assert first[0].memory.id == second[0].memory.id
        assert await backend.get_memory(first[0].memory.id) is first[0].memory

        # Same content under a different user gets a different ID.
        other = await backend.search_memories(query="q", user_id="bob")
        assert other[0].memory.id != first[0].memory.id

    async def test_saved_memory_id_matches_search_result_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A saved memory surfaces from search with its original ID."""
        module, _ = _make_fake_cognee(search_results=[_fake_hit(["remember me"])])
        monkeypatch.setitem(sys.modules, "cognee", module)

        backend = CogneeBackend(CogneeConfig())
        memory = await backend.save_memory(content="remember me", user_id="alice", importance=0.9)
        results = await backend.search_memories(query="q", user_id="alice")

        assert results[0].memory.id == memory.id
        # The registered (saved) memory is reused, keeping its importance.
        assert results[0].memory.importance == 0.9

    async def test_search_result_id_roundtrips_to_update_and_delete(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """IDs surfaced by search work with update_memory / delete_memory."""
        module, _ = _make_fake_cognee(search_results=[_fake_hit(["fact one", "fact two"])])
        monkeypatch.setitem(sys.modules, "cognee", module)

        backend = CogneeBackend(CogneeConfig())
        results = await backend.search_memories(query="q", user_id="alice")

        updated = await backend.update_memory(results[0].memory.id, "fact one, revised")
        assert updated.content == "fact one, revised"

        assert await backend.delete_memory(results[1].memory.id) is True

    async def test_extracts_text_from_dict_payloads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module, _ = _make_fake_cognee(
            search_results=[_fake_hit([{"text": "from text key"}, {"chunk": "from chunk key"}])]
        )
        monkeypatch.setitem(sys.modules, "cognee", module)

        backend = CogneeBackend(CogneeConfig())
        results = await backend.search_memories(query="q", user_id="alice")
        assert [r.memory.content for r in results] == ["from text key", "from chunk key"]

    async def test_truncates_to_top_k(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module, _ = _make_fake_cognee(search_results=[_fake_hit([f"chunk {i}" for i in range(5)])])
        monkeypatch.setitem(sys.modules, "cognee", module)

        backend = CogneeBackend(CogneeConfig())
        results = await backend.search_memories(query="q", user_id="alice", top_k=2)
        assert len(results) == 2

    async def test_empty_results(self, fake_cognee) -> None:
        backend = CogneeBackend(CogneeConfig())
        results = await backend.search_memories(query="q", user_id="alice")
        assert results == []

    async def test_graph_completion_sets_only_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module, calls = _make_fake_cognee(search_results=[])
        monkeypatch.setitem(sys.modules, "cognee", module)

        # Lowercase on purpose: resolution is case-insensitive.
        backend = CogneeBackend(CogneeConfig(search_type="graph_completion"))
        await backend.search_memories(query="q", user_id="alice")

        kwargs = calls.search[0]
        assert kwargs["query_type"] is _FakeSearchType.GRAPH_COMPLETION
        assert kwargs["only_context"] is True

    async def test_invalid_search_type_raises(self, fake_cognee) -> None:
        backend = CogneeBackend(CogneeConfig(search_type="BOGUS"))
        with pytest.raises(ValueError, match="Invalid cognee search type"):
            await backend.search_memories(query="q", user_id="alice")


# =============================================================================
# update / delete / get (tombstone semantics)
# =============================================================================


class TestUpdateDeleteGet:
    async def test_get_memory_roundtrip(self, fake_cognee) -> None:
        backend = CogneeBackend(CogneeConfig())
        memory = await backend.save_memory(content="c", user_id="alice", importance=0.5)
        assert await backend.get_memory(memory.id) is memory
        assert await backend.get_memory("nonexistent") is None

    async def test_update_tombstones_and_readds(self, fake_cognee) -> None:
        backend = CogneeBackend(CogneeConfig(dataset_name="ds"))
        memory = await backend.save_memory(
            content="old fact", user_id="alice", importance=0.7, session_id="s1"
        )

        updated = await backend.update_memory(memory.id, "new fact", reason="correction")

        assert updated.id == memory.id
        assert updated.content == "new fact"
        assert updated.importance == 0.7
        assert updated.metadata["update_reason"] == "correction"
        # Re-added with the same scoping tags.
        assert len(fake_cognee.add) == 2
        assert fake_cognee.add[1]["data"] == "new fact"
        assert fake_cognee.add[1]["node_set"] == fake_cognee.add[0]["node_set"]
        assert await backend.get_memory(memory.id) is updated

    async def test_update_unknown_id_raises(self, fake_cognee) -> None:
        backend = CogneeBackend(CogneeConfig())
        with pytest.raises(ValueError, match="Memory not found"):
            await backend.update_memory("missing-id", "new content")

    async def test_update_wrong_user_raises(self, fake_cognee) -> None:
        backend = CogneeBackend(CogneeConfig())
        memory = await backend.save_memory(content="c", user_id="alice", importance=0.5)
        with pytest.raises(ValueError, match="other users"):
            await backend.update_memory(memory.id, "new", user_id="bob")

    async def test_delete_tombstones_memory(self, fake_cognee) -> None:
        backend = CogneeBackend(CogneeConfig())
        memory = await backend.save_memory(content="c", user_id="alice", importance=0.5)
        assert await backend.delete_memory(memory.id) is True
        assert await backend.get_memory(memory.id) is None

    async def test_delete_unknown_id_returns_false(self, fake_cognee) -> None:
        backend = CogneeBackend(CogneeConfig())
        assert await backend.delete_memory("missing-id") is False

    async def test_delete_wrong_user_returns_false(self, fake_cognee) -> None:
        backend = CogneeBackend(CogneeConfig())
        memory = await backend.save_memory(content="c", user_id="alice", importance=0.5)
        assert await backend.delete_memory(memory.id, user_id="bob") is False
        assert await backend.get_memory(memory.id) is memory

    async def test_deleted_content_filtered_from_search(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module, _ = _make_fake_cognee(search_results=[_fake_hit(["stale fact", "fresh fact"])])
        monkeypatch.setitem(sys.modules, "cognee", module)

        backend = CogneeBackend(CogneeConfig())
        memory = await backend.save_memory(content="stale fact", user_id="alice", importance=0.5)
        await backend.delete_memory(memory.id)

        results = await backend.search_memories(query="fact", user_id="alice")
        assert [r.memory.content for r in results] == ["fresh fact"]

    async def test_superseded_content_filtered_after_update(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module, _ = _make_fake_cognee(search_results=[_fake_hit(["old fact", "new fact"])])
        monkeypatch.setitem(sys.modules, "cognee", module)

        backend = CogneeBackend(CogneeConfig())
        memory = await backend.save_memory(content="old fact", user_id="alice", importance=0.5)
        await backend.update_memory(memory.id, "new fact")

        results = await backend.search_memories(query="fact", user_id="alice")
        assert [r.memory.content for r in results] == ["new fact"]

    async def test_tombstones_are_scoped_per_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Deleting one user's memory never hides identical content of another user."""
        module, _ = _make_fake_cognee(search_results=[_fake_hit(["shared fact"])])
        monkeypatch.setitem(sys.modules, "cognee", module)

        backend = CogneeBackend(CogneeConfig())
        alice_memory = await backend.save_memory(
            content="shared fact", user_id="alice", importance=0.5
        )
        await backend.save_memory(content="shared fact", user_id="bob", importance=0.5)
        await backend.delete_memory(alice_memory.id)

        assert await backend.search_memories(query="q", user_id="alice") == []
        bob_results = await backend.search_memories(query="q", user_id="bob")
        assert [r.memory.content for r in bob_results] == ["shared fact"]

    async def test_tombstone_filters_chunks_of_deleted_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Chunks that are substrings of a deleted memory are filtered too."""
        long_content = "First sentence of a long memory. Second sentence with more detail."
        module, _ = _make_fake_cognee(
            search_results=[_fake_hit(["Second sentence with more detail.", "unrelated fact"])]
        )
        monkeypatch.setitem(sys.modules, "cognee", module)

        backend = CogneeBackend(CogneeConfig())
        memory = await backend.save_memory(content=long_content, user_id="alice", importance=0.5)
        await backend.delete_memory(memory.id)

        results = await backend.search_memories(query="q", user_id="alice")
        assert [r.memory.content for r in results] == ["unrelated fact"]

    async def test_facts_tombstoned_alongside_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-extracted facts (saved as separate items) are tombstoned on delete."""
        module, _ = _make_fake_cognee(
            search_results=[_fake_hit(["Alice works at Netflix", "kept fact"])]
        )
        monkeypatch.setitem(sys.modules, "cognee", module)

        backend = CogneeBackend(CogneeConfig())
        memory = await backend.save_memory(
            content="Alice works at Netflix using Python",
            user_id="alice",
            importance=0.5,
            facts=["Alice works at Netflix"],
        )
        await backend.delete_memory(memory.id)

        results = await backend.search_memories(query="q", user_id="alice")
        assert [r.memory.content for r in results] == ["kept fact"]

    async def test_survivors_keep_top_ranks_after_tombstone_filter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ranks (and scores) are computed after tombstone filtering."""
        module, _ = _make_fake_cognee(search_results=[_fake_hit(["deleted", "survivor"])])
        monkeypatch.setitem(sys.modules, "cognee", module)

        backend = CogneeBackend(CogneeConfig())
        memory = await backend.save_memory(content="deleted", user_id="alice", importance=0.5)
        await backend.delete_memory(memory.id)

        results = await backend.search_memories(query="q", user_id="alice")
        assert [r.memory.content for r in results] == ["survivor"]
        assert results[0].score == 1.0


# =============================================================================
# Environment isolation around the deferred import
# =============================================================================


class TestImportEnvIsolation:
    async def test_import_side_effects_on_env_are_reverted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """cognee's import-time env mutations (dotenv override) are undone.

        A real module is executed (written to a temp dir, not pre-seeded in
        sys.modules) so import side effects actually run. Pre-existing env
        vars it overwrites must be restored; vars it newly sets are kept.
        """
        fake_src = """
import os
from enum import Enum

# Simulate dotenv.load_dotenv(override=True) side effects.
os.environ["HEADROOM_TEST_PREEXISTING"] = "overwritten-by-dotenv"
os.environ["HEADROOM_TEST_NEW_FROM_DOTENV"] = "added-by-dotenv"


class SearchType(str, Enum):
    CHUNKS = "CHUNKS"
    GRAPH_COMPLETION = "GRAPH_COMPLETION"


async def add(data, dataset_name=None, node_set=None, **kwargs):
    pass


async def cognify(datasets=None, run_in_background=None, **kwargs):
    pass


async def search(**kwargs):
    return []


class config:
    @staticmethod
    def system_root_directory(path):
        pass

    @staticmethod
    def data_root_directory(path):
        pass
"""
        (tmp_path / "cognee.py").write_text(fake_src)
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.setenv("HEADROOM_TEST_PREEXISTING", "original")
        monkeypatch.delenv("HEADROOM_TEST_NEW_FROM_DOTENV", raising=False)
        sys.modules.pop("cognee", None)

        try:
            backend = CogneeBackend(CogneeConfig())
            await backend.ensure_initialized()

            # Pre-existing value restored (env-over-.env precedence kept).
            assert os.environ["HEADROOM_TEST_PREEXISTING"] == "original"
            # Newly added key kept (cognee's own .env config still works).
            assert os.environ["HEADROOM_TEST_NEW_FROM_DOTENV"] == "added-by-dotenv"
        finally:
            sys.modules.pop("cognee", None)
            os.environ.pop("HEADROOM_TEST_NEW_FROM_DOTENV", None)


# =============================================================================
# Capability flags / package exports
# =============================================================================


class TestCapabilities:
    def test_supports_graph(self) -> None:
        assert CogneeBackend(CogneeConfig()).supports_graph is True

    def test_supports_vector_search(self) -> None:
        assert CogneeBackend(CogneeConfig()).supports_vector_search is True

    def test_lazy_exports_from_backends_package(self) -> None:
        from headroom.memory import backends

        assert backends.CogneeBackend is CogneeBackend
        assert backends.CogneeConfig is CogneeConfig
        assert "CogneeBackend" in backends.__all__
        assert "CogneeConfig" in backends.__all__
