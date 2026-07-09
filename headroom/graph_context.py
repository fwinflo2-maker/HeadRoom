"""Graph-scoped context builder — a Graphify-style proxy in front of the LLM.

Instead of reading every file in a project, this module builds (or loads
from cache) a directed import graph over the project's Python files — nodes
are files, edges are internal (project-local) imports extracted via AST.
From a user-supplied entrypoint, it walks the graph with a depth-bounded BFS
and concatenates the full contents of every file reached into one context
string, ready to be injected into an LLM prompt alongside the user's query.

External/third-party imports are not resolvable to a project file and are
simply not added as edges — the graph only ever contains project-internal
files.

No token-limiting, truncation, or summarization happens here by design: the
full content of every reachable file is returned verbatim. Bounding total
size is the caller's (or the LLM API's) responsibility.

The graph is cached under ``headroom.paths.graph_context_cache_dir()``,
keyed by content hashes of every ``.py`` file under the project root. Any
file addition, removal, or edit invalidates the cache and triggers a
rebuild; an unchanged tree is loaded straight from cache.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from headroom.paths import graph_context_cache_dir

logger = logging.getLogger(__name__)

DEFAULT_MAX_DEPTH = 2

_IGNORED_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


@dataclass
class ProjectGraph:
    """Directed import graph for a project: nodes are files, edges are imports.

    Paths in ``edges`` and ``file_hashes`` are project-root-relative, POSIX-
    style strings so the graph is portable across machines/OSes.
    """

    root: Path
    edges: dict[str, list[str]]
    file_hashes: dict[str, str]

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "edges": self.edges,
            "file_hashes": self.file_hashes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ProjectGraph:
        return cls(
            root=Path(data["root"]),
            edges=data["edges"],
            file_hashes=data["file_hashes"],
        )


@dataclass
class GraphContextResult:
    """Result of assembling graph-scoped context for an entrypoint + query."""

    entrypoint: str
    query: str
    max_depth: int
    files: list[str]
    context: str
    prompt: str = field(init=False)

    def __post_init__(self) -> None:
        self.prompt = f"{self.query}\n\n{self.context}" if self.query else self.context


def _cache_key(root: Path) -> str:
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]


def _cache_path(root: Path) -> Path:
    return graph_context_cache_dir() / f"{_cache_key(root)}.json"


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if set(path.relative_to(root).parts) & _IGNORED_DIRS:
            continue
        yield path


def _current_hashes(root: Path) -> dict[str, str]:
    return {f.relative_to(root).as_posix(): _hash_file(f) for f in _iter_python_files(root)}


def _file_for(candidate: Path) -> Path | None:
    """Return ``candidate.py`` or ``candidate/__init__.py`` if either exists."""

    for option in (candidate.with_suffix(".py"), candidate / "__init__.py"):
        if option.is_file():
            return option
    return None


def _resolve_absolute(root: Path, module: str) -> Path | None:
    """Resolve a plain ``import a.b.c`` to a project-internal file, or None."""

    if not module:
        return None
    return _file_for(root.joinpath(*module.split(".")))


def _base_path(root: Path, importer: Path, module: str, level: int) -> Path | None:
    """Directory/module path implied by the ``X`` in ``from X import ...``.

    May point at a location that doesn't exist (e.g. an external package) —
    callers must check with :func:`_file_for` before treating it as a file.
    """

    if level > 0:
        # Relative import: level=1 is the importer's own package.
        base = importer.parent
        for _ in range(level - 1):
            base = base.parent
        return base.joinpath(*module.split(".")) if module else base
    if not module:
        return None
    return root.joinpath(*module.split("."))


def _resolve_from_import(root: Path, importer: Path, node: ast.ImportFrom) -> list[Path]:
    """Resolve ``from X import a, b`` — submodules of X take priority over X itself.

    ``from pkg import util`` should resolve to ``pkg/util.py`` (a submodule)
    rather than ``pkg/__init__.py`` (importing an attribute named ``util``
    from the package) whenever the submodule actually exists on disk.
    """

    base = _base_path(root, importer, node.module or "", node.level)
    if base is None:
        return []

    submodules = [f for alias in node.names if (f := _file_for(base / alias.name)) is not None]
    if submodules:
        return submodules

    module_file = _file_for(base)
    return [module_file] if module_file is not None else []


def _extract_imports(root: Path, file_path: Path) -> list[Path]:
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []

    resolved: list[Path] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = _resolve_absolute(root, alias.name)
                if target is not None:
                    resolved.append(target)
        elif isinstance(node, ast.ImportFrom):
            resolved.extend(_resolve_from_import(root, file_path, node))
    return resolved


def build_graph(root: Path) -> ProjectGraph:
    """Build the import graph for every ``.py`` file under ``root`` (no cache)."""

    root = root.resolve()
    edges: dict[str, list[str]] = {}
    file_hashes: dict[str, str] = {}

    for file_path in _iter_python_files(root):
        rel = file_path.relative_to(root).as_posix()
        file_hashes[rel] = _hash_file(file_path)
        deps = _extract_imports(root, file_path)
        edges[rel] = sorted({d.relative_to(root).as_posix() for d in deps})

    return ProjectGraph(root=root, edges=edges, file_hashes=file_hashes)


def load_or_build_graph(root: Path) -> ProjectGraph:
    """Load the cached graph if file hashes are unchanged; rebuild otherwise."""

    root = root.resolve()
    cache_file = _cache_path(root)
    current_hashes = _current_hashes(root)

    if cache_file.is_file():
        try:
            cached = ProjectGraph.from_dict(json.loads(cache_file.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, OSError):
            cached = None
        if cached is not None and cached.file_hashes == current_hashes:
            return cached

    graph = build_graph(root)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(graph.to_dict(), indent=2), encoding="utf-8")
    return graph


# ---------------------------------------------------------------------------
# Push-based live graph (proxy usage): a background watchdog observer keeps
# an in-memory ProjectGraph current, so a long-running process (the proxy)
# never re-hashes every .py file per request -- it just reads whatever the
# watcher last cached. Falls back to `load_or_build_graph` (the pull/hash
# model above) when `watchdog` isn't installed or a watcher fails to start.
# One-shot callers (the CLI) have no use for a background thread and should
# keep calling `load_or_build_graph`/`assemble_context` directly.
# ---------------------------------------------------------------------------

_live_lock = threading.Lock()
_live_graphs: dict[Path, ProjectGraph] = {}
_live_watchers: dict[Path, "GraphContextWatcher"] = {}

_WATCH_DEBOUNCE_SECONDS = 2.0


def _rebuild_live(root: Path) -> None:
    """Rebuild `root`'s graph and refresh both the in-memory and on-disk cache.

    Writing to the same on-disk cache file `load_or_build_graph` reads/writes
    is what makes this shared across processes: any other process (including
    ones without a watcher of their own) picks up the fresh graph through its
    normal hash-check the next time it calls `load_or_build_graph` — the
    watcher just keeps the shared file current without waiting to be asked.
    """
    try:
        graph = build_graph(root)
    except Exception as exc:  # noqa: BLE001 — background rebuild must never crash the thread
        logger.debug("graph_context: live rebuild failed for %s: %s", root, exc)
        return

    with _live_lock:
        _live_graphs[root] = graph

    try:
        cache_file = _cache_path(root)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(graph.to_dict(), indent=2), encoding="utf-8")
    except OSError as exc:
        logger.debug("graph_context: failed to persist live graph for %s: %s", root, exc)


class GraphContextWatcher:
    """Background watchdog observer that keeps one root's ``ProjectGraph`` live.

    Mirrors `headroom.graph.watcher.CodeGraphWatcher`'s shape (debounced
    watchdog Observer in a daemon thread) applied to the import graph instead
    of the codebase-memory-mcp backend. Debounced so a burst of saves (a
    branch checkout, a formatter run) triggers one rebuild, not one per file.
    """

    def __init__(self, root: Path, debounce_seconds: float = _WATCH_DEBOUNCE_SECONDS) -> None:
        self.root = root
        self.debounce_seconds = debounce_seconds
        self._observer: Any = None
        self._debounce_timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        """Do one synchronous initial build, then start watching. False if watchdog is unavailable."""

        _rebuild_live(self.root)

        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            logger.debug("graph_context: watchdog not installed, staying on pull-based cache")
            return False

        outer = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event: Any) -> None:
                src_path = getattr(event, "src_path", "")
                if not src_path or not src_path.endswith(".py"):
                    return
                if set(Path(src_path).parts) & _IGNORED_DIRS:
                    return
                outer._schedule_rebuild()

        try:
            observer = Observer()
            observer.schedule(_Handler(), str(self.root), recursive=True)
            observer.daemon = True
            observer.start()
        except Exception as exc:  # noqa: BLE001 — watcher setup must never break the proxy
            logger.debug("graph_context: watcher failed to start for %s: %s", self.root, exc)
            return False

        self._observer = observer
        logger.info("graph_context: live-watching %s (debounce=%.1fs)", self.root, self.debounce_seconds)
        return True

    def _schedule_rebuild(self) -> None:
        with self._lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(self.debounce_seconds, self._rebuild)
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _rebuild(self) -> None:
        _rebuild_live(self.root)

    def stop(self) -> None:
        with self._lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
                self._debounce_timer = None
        if self._observer:
            observer = self._observer
            self._observer = None
            observer.stop()
            observer.join(timeout=3)


def get_live_graph(root: Path) -> ProjectGraph:
    """Push-based graph access: in-memory read once a watcher is live for ``root``.

    Starts a `GraphContextWatcher` for `root` on first call (which does one
    synchronous initial build so this call always returns a graph). Later
    calls for the same root are a plain dict read — no filesystem walk, no
    hashing. Falls back to `load_or_build_graph` (hash-checked pull) when
    `watchdog` is unavailable or the watcher fails to start, so behavior is
    identical either way — only the cost model changes.
    """
    root = root.resolve()

    # Reserve the right to start `root`'s watcher atomically: without this,
    # concurrent callers can all observe "no watcher yet" and each start
    # their own Observer for the same root (verified as a real race by
    # inspection -- multiple threads can interleave between the absence
    # check and registration). The reservation itself is instant (just a
    # dict insert); the slow part (`watcher.start()`, which hashes/builds
    # the tree) runs OUTSIDE the lock so concurrent callers for OTHER roots
    # are never blocked on it.
    with _live_lock:
        cached = _live_graphs.get(root)
        if cached is not None:
            return cached
        existing_watcher = _live_watchers.get(root)
        reserved_watcher = None
        if existing_watcher is None:
            reserved_watcher = GraphContextWatcher(root)
            _live_watchers[root] = reserved_watcher

    if existing_watcher is not None:
        # Another caller already owns this root's watcher (starting or
        # already live). Don't block on its first build -- fall back to a
        # direct load, which is at worst one extra hash-check.
        return load_or_build_graph(root)

    started = reserved_watcher.start()
    if started:
        with _live_lock:
            graph = _live_graphs.get(root)
        if graph is not None:
            return graph
    else:
        with _live_lock:
            # Only remove if it's still OUR reservation -- a concurrent
            # `stop_all_watchers()` may have already cleared it.
            if _live_watchers.get(root) is reserved_watcher:
                _live_watchers.pop(root, None)

    return load_or_build_graph(root)


def stop_all_watchers() -> None:
    """Stop every live watcher. For clean shutdown/tests only."""
    with _live_lock:
        watchers = list(_live_watchers.values())
        _live_watchers.clear()
        _live_graphs.clear()
    for watcher in watchers:
        watcher.stop()


def bfs_related_files(
    graph: ProjectGraph, entrypoint: str, max_depth: int = DEFAULT_MAX_DEPTH
) -> list[str]:
    """BFS from ``entrypoint`` over the import graph, bounded by ``max_depth``.

    Returns unique files in visit order (entrypoint first).
    """

    entry = Path(entrypoint).as_posix()
    if entry not in graph.edges:
        raise KeyError(f"entrypoint not found in graph: {entrypoint!r}")

    visited: list[str] = []
    seen = {entry}
    queue: deque[tuple[str, int]] = deque([(entry, 0)])

    while queue:
        node, depth = queue.popleft()
        visited.append(node)
        if depth >= max_depth:
            continue
        for neighbor in graph.edges.get(node, []):
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append((neighbor, depth + 1))

    return visited


def assemble_context(
    root: Path,
    entrypoint: str,
    query: str = "",
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> GraphContextResult:
    """Build the graph-scoped, full-content context for an entrypoint + query.

    This is the Headroom "graph proxy" entrypoint: given where to start and
    what the caller is trying to find out, it returns the concatenated file
    contents (each prefixed with its path) ready to inject into an LLM
    prompt — no truncation, no summarization.
    """

    root = root.resolve()
    graph = load_or_build_graph(root)
    files = bfs_related_files(graph, entrypoint, max_depth=max_depth)

    sections = []
    for rel in files:
        content = (root / rel).read_text(encoding="utf-8")
        sections.append(f"---- Arquivo: {rel} ----\n{content}")

    return GraphContextResult(
        entrypoint=entrypoint,
        query=query,
        max_depth=max_depth,
        files=files,
        context="\n\n".join(sections),
    )


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "ProjectGraph",
    "GraphContextResult",
    "GraphContextWatcher",
    "build_graph",
    "load_or_build_graph",
    "get_live_graph",
    "stop_all_watchers",
    "bfs_related_files",
    "assemble_context",
]
