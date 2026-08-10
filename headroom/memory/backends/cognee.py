"""Cognee memory backend implementing the MemoryBackend protocol.

cognee (https://github.com/topoteretes/cognee) is an async AI-memory /
knowledge-graph engine. This backend stores headroom memories as cognee data
items tagged via ``node_set`` for user/session/entity scoping, builds a
knowledge graph via ``cognee.cognify()``, and searches via ``cognee.search()``.

Import of the ``cognee`` package is deferred until the backend is first used
and runs in a worker thread (the import takes seconds and must not block the
event loop). cognee's import has side effects — notably
``dotenv.load_dotenv(override=True)``, which would overwrite already-set
process environment variables with values from a ``.env`` in the cwd. This
backend snapshots the environment around the import and restores any
pre-existing variables the import changed, preserving the normal
env-over-``.env`` precedence (keys newly added by cognee's ``.env`` load are
kept so cognee's own configuration keeps working).

Usage:
    from headroom.memory.backends.cognee import CogneeBackend, CogneeConfig
    from headroom.memory.system import MemorySystem

    config = CogneeConfig(dataset_name="my_app_memories")
    backend = CogneeBackend(config)
    memory_system = MemorySystem(backend, user_id="alice")

    result = await memory_system.process_tool_call(
        "memory_save",
        {"content": "User prefers Python", "importance": 0.8},
    )

Known limitations (cognee v1.x):
    - cognee has no per-item update API. ``update_memory`` is implemented
      best-effort as tombstone + re-add: the new content is added to cognee,
      the old content is filtered out of future search results from this
      backend instance, but the old data remains in cognee's stores until the
      dataset is pruned.
    - ``delete_memory`` tombstones the memory (excluded from this backend's
      search results) but cannot remove already-cognified graph nodes.
    - Tombstones are scoped per user and matched against search-result chunk
      text by exact equality or substring containment (a chunk that is a
      piece of the deleted content is filtered). This is best-effort: if
      cognee normalizes or rewrites the text during cognify, transformed
      chunks may still surface after a delete/update.
    - Memory IDs are stable content-derived UUIDs (``uuid5(user_id, content)``),
      so IDs returned by ``search_memories`` can be passed to ``update_memory``
      / ``delete_memory``. ``get_memory`` resolves memories saved or searched
      through this backend instance (cognee has no fetch-by-id for raw
      memories).
    - cognee search results carry no similarity score; scores returned here
      are rank-based, mapped into ``(0.5, 1.0]`` so they always clear the
      proxy's default ``min_similarity`` floor (0.3). ``min_similarity``
      values at or below 0.5 therefore have no filtering effect with this
      backend — the scores encode result order, not semantic similarity.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from headroom.memory import cognee_env
from headroom.memory.models import Memory
from headroom.memory.ports import MemorySearchResult

logger = logging.getLogger(__name__)

_IMPORT_ERROR_MSG = 'cognee package not installed. Install with: pip install "headroom-ai[cognee]"'


def _utcnow() -> datetime:
    """Return current UTC time as timezone-aware datetime."""
    return datetime.now(timezone.utc)


# Namespace for stable, content-derived memory IDs. Fixed so the same
# (user_id, content) pair always maps to the same UUID across instances.
_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "headroom.memory.backends.cognee")


def _stable_memory_id(user_id: str, content: str) -> str:
    """Return a stable, user-scoped memory ID derived from the content.

    cognee has no per-item IDs for raw memories, so IDs must be derivable
    from what search returns. Deriving them from ``(user_id, content)``
    makes the IDs surfaced by ``search_memories`` valid inputs to
    ``update_memory`` / ``delete_memory`` (instead of throwaway UUIDs).
    """
    return str(uuid.uuid5(_ID_NAMESPACE, f"{user_id}\x00{content}"))


def _user_tag(user_id: str) -> str:
    """Build the node_set tag for a user."""
    return f"user:{user_id}"


def _session_tag(session_id: str) -> str:
    """Build the node_set tag for a session."""
    return f"session:{session_id}"


def _entity_tag(entity: str) -> str:
    """Build the node_set tag for an entity."""
    return f"entity:{entity}"


@dataclass
class CogneeConfig:
    """Configuration for the cognee memory backend.

    Fields default to values read from ``HEADROOM_COGNEE_*`` environment
    variables (see :mod:`headroom.memory.cognee_env`). Passing an explicit
    value to the constructor always wins over the environment.

    Attributes:
        dataset_name: cognee dataset that holds all headroom memories.
        system_root: Directory for cognee system state (databases, caches).
            Keeps headroom's cognee state isolated from other cognee installs.
            ``None`` uses cognee's own default location.
        data_root: Directory for cognee data storage. ``None`` uses cognee's
            own default location.
        search_type: cognee ``SearchType`` name used by ``search_memories``.
            ``CHUNKS`` (default) is raw retrieval with no LLM synthesis —
            cheapest and right for a proxy. ``GRAPH_COMPLETION`` retrieves
            graph context (sent with ``only_context=True`` so no LLM answer
            is generated).
        auto_cognify: Whether to run ``cognee.cognify()`` after each save so
            new memories become part of the knowledge graph.
        background_cognify: Whether ``cognify`` runs in the background.
            ``cognify`` is LLM-bound and slow; in a proxy request path this
            should stay ``True``.
    """

    dataset_name: str = field(default_factory=cognee_env.cognee_env_dataset)
    system_root: str | None = field(default_factory=cognee_env.cognee_env_system_root)
    data_root: str | None = field(default_factory=cognee_env.cognee_env_data_root)
    search_type: str = field(default_factory=cognee_env.cognee_env_search_type)
    auto_cognify: bool = field(default_factory=cognee_env.cognee_env_auto_cognify)
    background_cognify: bool = True


class CogneeBackend:
    """Memory backend backed by the cognee knowledge-graph engine.

    Implements headroom's ``MemoryBackend`` protocol on top of cognee:

    - ``save_memory`` -> ``cognee.add`` (tagged via ``node_set``) followed by
      an optional ``cognee.cognify`` to build/extend the knowledge graph.
    - ``search_memories`` -> ``cognee.search`` scoped via ``node_name``.
    - ``update_memory`` / ``delete_memory`` -> best-effort tombstone semantics
      (see module docstring for limitations).

    The cognee package is imported lazily on first use; construction never
    imports cognee.
    """

    def __init__(self, config: CogneeConfig | None = None) -> None:
        """Initialize the cognee backend.

        Args:
            config: Backend configuration. Defaults resolve from
                ``HEADROOM_COGNEE_*`` env vars when omitted.
        """
        self._config = config or CogneeConfig()
        self._cognee: Any = None
        self._search_type_cls: Any = None
        self._initialized = False
        # Singleflight guard for the (slow) lazy import. Created lazily so
        # construction never requires a running event loop.
        self._init_lock: asyncio.Lock | None = None
        # Process-local registry of memories saved or searched through this
        # instance. cognee has no fetch-by-id API for raw memories, so
        # get/update/delete resolve against this registry. Keys are the
        # stable content-derived IDs (see _stable_memory_id).
        self._memories: dict[str, Memory] = {}
        # Per-user tombstones: user_id -> contents of deleted/superseded
        # memories (and their pre-extracted facts), used to filter stale
        # chunks out of search results (cognee cannot hard-delete graph
        # data). Scoped per user so deleting one user's memory never hides
        # byte-identical content saved by another user.
        self._tombstoned: dict[str, set[str]] = {}

    async def _ensure_initialized(self) -> None:
        """Import cognee lazily (off-loop) and apply directory isolation.

        The import runs in a worker thread via ``asyncio.to_thread`` because
        ``import cognee`` takes seconds and would otherwise stall the entire
        event loop (every in-flight proxy request, not just the memory one)
        when initialization happens lazily inside a live request.
        """
        if self._initialized:
            return

        if self._init_lock is None:
            self._init_lock = asyncio.Lock()

        async with self._init_lock:
            if self._initialized:
                return
            cognee, search_type_cls = await asyncio.to_thread(self._import_and_configure)
            self._cognee = cognee
            self._search_type_cls = search_type_cls
            self._initialized = True

    def _import_and_configure(self) -> tuple[Any, Any]:
        """Synchronously import cognee and apply directory isolation config.

        Runs in a worker thread (see ``_ensure_initialized``). cognee's
        import executes ``dotenv.load_dotenv(override=True)``, which would
        silently overwrite already-set process env vars with values from a
        ``.env`` in the cwd — after the caller's configuration was resolved.
        The environment is snapshotted before the import and any
        pre-existing variable the import changed is restored, preserving
        normal env-over-``.env`` precedence. Variables newly added by the
        ``.env`` load are kept so cognee's own configuration (e.g.
        ``LLM_API_KEY``) keeps working.

        Returns:
            ``(cognee_module, SearchType_class)``.

        Raises:
            ImportError: If the cognee package is not installed.
        """
        env_before = dict(os.environ)
        try:
            cognee = importlib.import_module("cognee")
        except ImportError:
            raise ImportError(_IMPORT_ERROR_MSG) from None
        finally:
            for key, value in env_before.items():
                if os.environ.get(key) != value:
                    os.environ[key] = value

        search_type_cls = getattr(cognee, "SearchType", None)
        if search_type_cls is None:
            raise ImportError(_IMPORT_ERROR_MSG)

        if self._config.system_root:
            cognee.config.system_root_directory(self._config.system_root)
        if self._config.data_root:
            cognee.config.data_root_directory(self._config.data_root)

        return cognee, search_type_cls

    async def ensure_initialized(self) -> None:
        """Public initialization hook for callers that need readiness guarantees."""
        await self._ensure_initialized()

    def _resolve_search_type(self) -> Any:
        """Resolve the configured search type name to a cognee ``SearchType``.

        Returns:
            The cognee SearchType enum member.

        Raises:
            ValueError: If the configured name is not a valid SearchType.
        """
        name = self._config.search_type.strip().upper()
        try:
            return self._search_type_cls[name]
        except KeyError:
            valid = ", ".join(m.name for m in self._search_type_cls)
            raise ValueError(
                f"Invalid cognee search type {self._config.search_type!r}; expected one of: {valid}"
            ) from None

    @staticmethod
    def _build_node_set(
        user_id: str,
        session_id: str | None = None,
        entities: list[str] | None = None,
    ) -> list[str]:
        """Build the node_set tags used to scope data in cognee."""
        tags = [_user_tag(user_id)]
        if session_id:
            tags.append(_session_tag(session_id))
        for entity in entities or []:
            tags.append(_entity_tag(entity))
        return tags

    async def _cognify(self) -> None:
        """Run cognee.cognify for the configured dataset (best-effort)."""
        try:
            await self._cognee.cognify(
                datasets=[self._config.dataset_name],
                run_in_background=self._config.background_cognify,
            )
        except Exception:
            # cognify is an enrichment step; a failure must not lose the save.
            logger.exception("cognee.cognify failed for dataset %s", self._config.dataset_name)

    async def save_memory(
        self,
        content: str,
        user_id: str,
        importance: float,
        entities: list[str] | None = None,
        relationships: list[dict[str, str]] | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        # Pre-extraction fields for optimized storage
        facts: list[str] | None = None,
        extracted_entities: list[dict[str, str]] | None = None,
        extracted_relationships: list[dict[str, str]] | None = None,
    ) -> Memory:
        """Save a new memory to cognee.

        The content (plus pre-extracted facts, when provided) is added to the
        configured cognee dataset, tagged via ``node_set`` with user/session/
        entity tags so searches can be scoped. When ``auto_cognify`` is on,
        ``cognee.cognify`` then builds/extends the knowledge graph (in the
        background by default).

        Note: cognee performs its own LLM-based entity/relationship extraction
        during ``cognify``, so ``relationships``, ``extracted_entities``, and
        ``extracted_relationships`` are recorded in the returned Memory's
        metadata but not written to the graph directly.

        Args:
            content: The memory content to store.
            user_id: User identifier for scoping.
            importance: Importance score (0.0 - 1.0). Stored in metadata only;
                cognee does not rank by importance.
            entities: List of entity references (become node_set tags).
            relationships: Relationship dicts (recorded in metadata only).
            session_id: Optional session identifier (becomes a node_set tag).
            metadata: Optional additional metadata.
            facts: Pre-extracted discrete facts, added as extra data items.
            extracted_entities: Pre-extracted entities (metadata only).
            extracted_relationships: Pre-extracted relationships (metadata only).

        Returns:
            The created Memory object.
        """
        await self._ensure_initialized()

        node_set = self._build_node_set(user_id, session_id, entities)
        data: str | list[str] = content if not facts else [content, *facts]

        await self._cognee.add(
            data,
            dataset_name=self._config.dataset_name,
            node_set=node_set,
        )

        if self._config.auto_cognify:
            await self._cognify()

        now = _utcnow()
        combined_metadata: dict[str, Any] = {
            **(metadata or {}),
            "_cognee_dataset": self._config.dataset_name,
            "_cognee_node_set": node_set,
        }
        if relationships:
            combined_metadata["relationships"] = relationships
        if extracted_entities:
            combined_metadata["extracted_entities"] = extracted_entities
        if extracted_relationships:
            combined_metadata["extracted_relationships"] = extracted_relationships
        if facts:
            combined_metadata["_fact_count"] = len(facts)
            # Kept so update/delete can tombstone the facts alongside the
            # main content (facts were added to cognee as separate items).
            combined_metadata["_cognee_facts"] = list(facts)

        memory = Memory(
            id=_stable_memory_id(user_id, content),
            content=content,
            user_id=user_id,
            session_id=session_id,
            importance=importance,
            entity_refs=entities or [],
            metadata=combined_metadata,
            created_at=now,
            valid_from=now,
        )
        self._memories[memory.id] = memory
        logger.info("Saved memory %s to cognee dataset %s", memory.id, self._config.dataset_name)
        return memory

    async def search_memories(
        self,
        query: str,
        user_id: str,
        entities: list[str] | None = None,
        include_related: bool = False,
        top_k: int = 10,
        session_id: str | None = None,
    ) -> list[MemorySearchResult]:
        """Search memories via cognee.

        Uses the configured ``SearchType`` (default ``CHUNKS``: raw retrieval,
        no LLM synthesis). Scoping is done with cognee's ``node_name`` filter
        against the tags written at save time, combined with ``AND`` so every
        listed tag must match — the user tag always applies, and session /
        entity filters narrow (never broaden) the result set. Without ``AND``
        cognee defaults to ``OR``, which would match ANY tag and leak other
        users' memories that share an entity tag.

        Args:
            query: Natural language search query.
            user_id: User identifier for scoping.
            entities: Filter to memories tagged with these entities.
            include_related: Accepted for protocol compatibility; graph
                expansion is controlled by ``CogneeConfig.search_type``
                (e.g. ``GRAPH_COMPLETION``) instead.
            top_k: Maximum number of results.
            session_id: Optional session filter.

        Returns:
            List of MemorySearchResult in relevance order. cognee does not
            expose similarity scores, so scores are rank-based and mapped
            into ``(0.5, 1.0]`` — they encode result order only and always
            clear the proxy's default ``min_similarity`` floor.
        """
        await self._ensure_initialized()

        query_type = self._resolve_search_type()
        node_names = self._build_node_set(user_id, session_id, entities)

        search_kwargs: dict[str, Any] = {
            "query_text": query,
            "query_type": query_type,
            "datasets": [self._config.dataset_name],
            "top_k": top_k,
            "node_name": node_names,
            # Require ALL tags to match (cognee>=1.4.0). The default "OR"
            # would return anything matching any single tag — e.g. other
            # users' memories tagged with the same (global) entity tag.
            "node_name_filter_operator": "AND",
        }
        # Graph retrieval without LLM answer synthesis.
        if query_type.name == "GRAPH_COMPLETION":
            search_kwargs["only_context"] = True

        raw_results = await self._cognee.search(**search_kwargs)

        texts: list[tuple[str, dict[str, Any]]] = []
        for res in raw_results or []:
            payload = getattr(res, "search_result", res)
            res_meta = {
                "_cognee_dataset_id": str(getattr(res, "dataset_id", "") or ""),
                "_cognee_dataset_name": getattr(res, "dataset_name", None)
                or self._config.dataset_name,
            }
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                text = self._extract_text(item)
                if text:
                    texts.append((text, res_meta))

        # Tombstone-filter BEFORE ranking so surviving results keep top
        # ranks (and therefore high scores) when leading chunks were
        # deleted/superseded.
        user_tombstones = self._tombstoned.get(user_id, set())
        visible = [
            (text, res_meta)
            for text, res_meta in texts
            if not self._is_tombstoned(text, user_tombstones)
        ]

        now = _utcnow()
        results: list[MemorySearchResult] = []
        total = len(visible)
        for rank, (text, res_meta) in enumerate(visible[:top_k]):
            # Stable content-derived ID, registered so the model can pass
            # it straight to update_memory / delete_memory. A memory saved
            # through this instance with identical content resolves to the
            # same ID and is reused (preserving its importance/metadata).
            memory_id = _stable_memory_id(user_id, text)
            memory = self._memories.get(memory_id)
            if memory is None:
                memory = Memory(
                    id=memory_id,
                    content=text,
                    user_id=user_id,
                    session_id=session_id,
                    importance=0.5,
                    metadata=res_meta,
                    created_at=now,
                    valid_from=now,
                )
                self._memories[memory_id] = memory
            results.append(
                MemorySearchResult(
                    memory=memory,
                    # Rank-based scores compressed into (0.5, 1.0] so an
                    # ordinal score can never be filtered out by the
                    # proxy's default cosine min_similarity floor (0.3).
                    score=1.0 - (rank / (2 * max(total, 1))),
                    related_entities=entities or [],
                    related_memories=[],
                )
            )

        return results

    @staticmethod
    def _is_tombstoned(text: str, tombstones: set[str]) -> bool:
        """Whether a search-result chunk matches a tombstoned content.

        Matches by exact equality or by substring containment (cognee chunks
        long documents, so a chunk of a deleted memory is a substring of the
        tombstoned original). Best-effort: text that cognee transformed
        during cognify may not match.
        """
        if not tombstones:
            return False
        if text in tombstones:
            return True
        return any(text in tombstoned for tombstoned in tombstones)

    def _tombstone(self, memory: Memory) -> None:
        """Tombstone a memory's content (and its facts) for its user."""
        tombstones = self._tombstoned.setdefault(memory.user_id, set())
        tombstones.add(memory.content)
        for fact in memory.metadata.get("_cognee_facts") or []:
            if isinstance(fact, str) and fact:
                tombstones.add(fact)

    @staticmethod
    def _extract_text(item: Any) -> str:
        """Extract display text from a single cognee search result item."""
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            for key in ("text", "chunk", "content", "memory", "name"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    return value
            return str(item)
        return str(item) if item is not None else ""

    async def update_memory(
        self,
        memory_id: str,
        new_content: str,
        reason: str | None = None,
        user_id: str | None = None,
    ) -> Memory:
        """Update a memory (best-effort: tombstone old content + re-add).

        cognee has no per-item update API, so this tombstones the old content
        (excluded from this backend's future search results for this user;
        best-effort — see module docstring) and adds the new content as a
        fresh cognee data item with the same scoping tags. The old data
        remains in cognee's stores until the dataset is pruned.

        Args:
            memory_id: ID of the memory to update. Must have been saved or
                returned by a search through this backend instance.
            new_content: New content to replace existing.
            reason: Reason for the update (for audit trail).
            user_id: User ID for validation (optional).

        Returns:
            The updated Memory object.

        Raises:
            ValueError: If the memory is not found or belongs to another user.
        """
        await self._ensure_initialized()

        existing = self._memories.get(memory_id)
        if existing is None:
            raise ValueError(
                f"Memory not found: {memory_id}. The cognee backend can only "
                "update memories saved or searched through this backend instance."
            )
        if user_id and existing.user_id and existing.user_id != user_id:
            raise ValueError("Cannot update memories belonging to other users")

        # Tombstone the old content (and its facts) so search stops
        # returning it for this user.
        self._tombstone(existing)

        node_set = list(existing.metadata.get("_cognee_node_set") or []) or self._build_node_set(
            existing.user_id, existing.session_id, existing.entity_refs
        )
        await self._cognee.add(
            new_content,
            dataset_name=self._config.dataset_name,
            node_set=node_set,
        )
        if self._config.auto_cognify:
            await self._cognify()

        now = _utcnow()
        updated_metadata = dict(existing.metadata)
        # The old memory's facts were tombstoned above and do not describe
        # the new content — drop them so a later delete of the updated
        # memory doesn't act on stale fact lists.
        updated_metadata.pop("_cognee_facts", None)
        updated_metadata.pop("_fact_count", None)
        if reason:
            updated_metadata["update_reason"] = reason
            updated_metadata["updated_at"] = now.isoformat()

        updated = Memory(
            id=memory_id,
            content=new_content,
            user_id=existing.user_id,
            session_id=existing.session_id,
            importance=existing.importance,
            entity_refs=existing.entity_refs,
            metadata=updated_metadata,
            created_at=existing.created_at,
            valid_from=now,
        )
        self._memories[memory_id] = updated
        logger.info("Updated memory %s (tombstone + re-add)", memory_id)
        return updated

    async def delete_memory(
        self,
        memory_id: str,
        reason: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        """Delete a memory (best-effort: tombstone).

        Removes the memory from this backend's registry and excludes its
        content (and pre-extracted facts) from future search results for
        this user. Already-cognified graph data remains in cognee's stores
        until the dataset is pruned — cognee has no per-memory hard delete.

        Args:
            memory_id: ID of the memory to delete. Must have been saved or
                returned by a search through this backend instance.
            reason: Reason for deletion (for audit trail; logged only).
            user_id: User ID for validation (optional).

        Returns:
            True if tombstoned, False if not found or owned by another user.
        """
        existing = self._memories.get(memory_id)
        if existing is None:
            return False
        if user_id and existing.user_id and existing.user_id != user_id:
            return False

        self._tombstone(existing)
        del self._memories[memory_id]
        logger.info(
            "Tombstoned memory %s (reason: %s); underlying cognee data persists",
            memory_id,
            reason or "unspecified",
        )
        return True

    async def get_memory(self, memory_id: str) -> Memory | None:
        """Retrieve a specific memory by ID.

        Only resolves memories saved or searched through this backend
        instance (cognee has no fetch-by-id API for raw memories).

        Args:
            memory_id: The memory identifier.

        Returns:
            The Memory if found, None otherwise.
        """
        return self._memories.get(memory_id)

    @property
    def supports_graph(self) -> bool:
        """Whether this backend supports graph/relationship queries."""
        return True

    @property
    def supports_vector_search(self) -> bool:
        """Whether this backend supports vector similarity search."""
        return True

    async def close(self) -> None:
        """Close the backend and release resources."""
        self._cognee = None
        self._search_type_cls = None
        self._initialized = False
