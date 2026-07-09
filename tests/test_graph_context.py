"""Tests for ``headroom.graph_context`` -- graph-scoped context builder."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from headroom import graph_context as gc


@pytest.fixture(autouse=True)
def _stop_watchers_between_tests():
    """Watchers are module-global state (one background thread per root).

    Without this, a watcher started in one test keeps running (and its
    debounce timer keeps firing) after the test's tmp_path is gone.
    """
    yield
    gc.stop_all_watchers()


@pytest.fixture
def fake_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect the Headroom workspace (graph cache root) into tmp_path."""

    workspace = tmp_path / "_workspace"
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(workspace))
    return workspace


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A small project: main -> util (absolute import) -> helper (relative import).

    ``unused`` is not reachable from main and must never show up in BFS
    results. ``main`` also imports a third-party package, which must not
    appear as a graph edge at all.
    """

    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")

    (root / "main.py").write_text(
        "import os\nfrom pkg import util\n\ndef run():\n    return util.value()\n",
        encoding="utf-8",
    )
    (root / "pkg" / "util.py").write_text(
        "from . import helper\n\ndef value():\n    return helper.CONST\n",
        encoding="utf-8",
    )
    (root / "pkg" / "helper.py").write_text("CONST = 42\n", encoding="utf-8")
    (root / "unused.py").write_text("X = 1\n", encoding="utf-8")

    return root


def test_build_graph_resolves_internal_imports_only(project: Path) -> None:
    graph = gc.build_graph(project)

    assert graph.edges["main.py"] == ["pkg/util.py"]
    assert graph.edges["pkg/util.py"] == ["pkg/helper.py"]
    assert graph.edges["pkg/helper.py"] == []
    assert "unused.py" in graph.edges  # present as a node, just unreferenced


def test_bfs_respects_max_depth(project: Path) -> None:
    graph = gc.build_graph(project)

    assert gc.bfs_related_files(graph, "main.py", max_depth=0) == ["main.py"]
    assert gc.bfs_related_files(graph, "main.py", max_depth=1) == ["main.py", "pkg/util.py"]
    assert gc.bfs_related_files(graph, "main.py", max_depth=2) == [
        "main.py",
        "pkg/util.py",
        "pkg/helper.py",
    ]
    assert "unused.py" not in gc.bfs_related_files(graph, "main.py", max_depth=5)


def test_bfs_unknown_entrypoint_raises(project: Path) -> None:
    graph = gc.build_graph(project)
    with pytest.raises(KeyError):
        gc.bfs_related_files(graph, "does_not_exist.py")


def test_assemble_context_concatenates_full_file_contents(
    fake_workspace: Path, project: Path
) -> None:
    result = gc.assemble_context(project, "main.py", "how does run() work?", max_depth=2)

    assert result.files == ["main.py", "pkg/util.py", "pkg/helper.py"]
    assert "---- Arquivo: main.py ----" in result.context
    assert "---- Arquivo: pkg/util.py ----" in result.context
    assert "CONST = 42" in result.context  # helper.py content present verbatim
    assert "how does run() work?" in result.prompt
    assert result.context in result.prompt


def test_cache_is_reused_when_files_unchanged(fake_workspace: Path, project: Path) -> None:
    first = gc.load_or_build_graph(project)
    cache_file = gc._cache_path(project.resolve())
    assert cache_file.is_file()

    mtime_before = cache_file.stat().st_mtime_ns
    second = gc.load_or_build_graph(project)

    assert second.edges == first.edges
    assert cache_file.stat().st_mtime_ns == mtime_before  # not rewritten


def test_cache_invalidated_on_file_change(fake_workspace: Path, project: Path) -> None:
    gc.load_or_build_graph(project)

    (project / "pkg" / "helper.py").write_text("CONST = 99\nEXTRA = True\n", encoding="utf-8")

    graph = gc.load_or_build_graph(project)
    result = gc.assemble_context(project, "main.py", max_depth=2)

    assert graph.file_hashes["pkg/helper.py"] != ""
    assert "EXTRA = True" in result.context


def test_cache_invalidated_on_new_file(fake_workspace: Path, project: Path) -> None:
    first = gc.load_or_build_graph(project)

    (project / "pkg" / "new_module.py").write_text(
        "from . import helper\n\nVALUE = helper.CONST\n", encoding="utf-8"
    )

    second = gc.load_or_build_graph(project)
    assert "pkg/new_module.py" in second.edges
    assert "pkg/new_module.py" not in first.edges


# =============================================================================
# Push-based live graph: GraphContextWatcher / get_live_graph
# =============================================================================


def test_get_live_graph_starts_watcher_and_returns_correct_graph(
    fake_workspace: Path, project: Path
) -> None:
    root = project.resolve()
    assert root not in gc._live_watchers

    graph = gc.get_live_graph(root)

    assert root in gc._live_watchers  # watchdog is a hard dependency here, must have started
    assert graph.edges["main.py"] == ["pkg/util.py"]


def test_get_live_graph_second_call_is_in_memory_reuse(fake_workspace: Path, project: Path) -> None:
    root = project.resolve()
    first = gc.get_live_graph(root)
    second = gc.get_live_graph(root)

    assert second is first  # exact same object -- dict read, not a rebuild
    assert len(gc._live_watchers) == 1  # didn't start a second watcher for the same root


def test_watcher_picks_up_new_file_via_real_filesystem_event(
    fake_workspace: Path, project: Path
) -> None:
    """End-to-end: an actual watchdog Observer notices a real file write.

    Uses a short debounce directly (not `get_live_graph`'s 2s default) so the
    test doesn't need to sleep that long.
    """
    root = project.resolve()
    watcher = gc.GraphContextWatcher(root, debounce_seconds=0.3)
    try:
        assert watcher.start() is True  # watchdog is installed in this env
        assert "pkg/new_mod.py" not in gc._live_graphs[root].edges

        (project / "pkg" / "new_mod.py").write_text(
            "from . import helper\n\nVALUE = helper.CONST\n", encoding="utf-8"
        )

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if "pkg/new_mod.py" in gc._live_graphs[root].edges:
                break
            time.sleep(0.1)

        assert "pkg/new_mod.py" in gc._live_graphs[root].edges
    finally:
        watcher.stop()


def test_get_live_graph_falls_back_to_pull_model_when_watcher_cannot_start(
    fake_workspace: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `GraphContextWatcher.start()` fails (e.g. no watchdog), behavior is
    unchanged -- just costed differently (falls back to `load_or_build_graph`).
    """
    monkeypatch.setattr(gc.GraphContextWatcher, "start", lambda self: False)
    root = project.resolve()

    graph = gc.get_live_graph(root)

    assert root not in gc._live_watchers
    assert graph.edges["main.py"] == ["pkg/util.py"]


def test_stop_all_watchers_clears_state(fake_workspace: Path, project: Path) -> None:
    root = project.resolve()
    gc.get_live_graph(root)
    assert gc._live_watchers  # something is running

    gc.stop_all_watchers()

    assert gc._live_watchers == {}
    assert gc._live_graphs == {}
