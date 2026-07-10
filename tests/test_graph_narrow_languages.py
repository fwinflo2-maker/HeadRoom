"""Token-optimization regression: graph-scoped narrowing must actually
shrink a wide discovery dump for every one of the ten tree-sitter-backed
languages added to `headroom.graph_context` (Go, Java, C#, Ruby, PHP,
Kotlin, Scala, Dart, Lua, Zig) -- not just resolve edges correctly at the
`build_graph` level (that's `test_graph_context_languages.py`).

This exercises the actual path an agent's tokens travel through:
`ContentRouter._graph_narrow`, fed by both a harness's own discovery tool
name (Grep/Glob/LS-equivalent) AND a bash-shelled discovery command
(`grep -r`/`find`/`ls -R`, the #1925 follow-up), for each language, and
measures the real token delta -- proving the savings claim holds, not just
that no exception is raised.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from headroom import graph_context as gc
from headroom.transforms.content_router import ContentRouter, ContentRouterConfig

_UNRELATED_LINE_COUNT = 40


@pytest.fixture(autouse=True)
def _stop_watchers_between_tests():
    yield
    gc.stop_all_watchers()


@dataclass
class _LangCase:
    id: str
    setup: Callable[[Path], tuple[str, str]]  # root -> (entry_rel, related_rel)


def _go_setup(root: Path) -> tuple[str, str]:
    (root / "pkg" / "util").mkdir(parents=True)
    (root / "go.mod").write_text("module myproj\n\ngo 1.21\n", encoding="utf-8")
    (root / "main.go").write_text(
        'package main\n\nimport (\n\t"fmt"\n\t"myproj/pkg/util"\n)\n', encoding="utf-8"
    )
    (root / "pkg" / "util" / "util.go").write_text("package util\nvar X = 1\n", encoding="utf-8")
    return "main.go", "pkg/util/util.go"


def _java_setup(root: Path) -> tuple[str, str]:
    (root / "com" / "acme" / "pkg").mkdir(parents=True)
    (root / "Main.java").write_text("import com.acme.pkg.Util;\nclass Main {}\n", encoding="utf-8")
    (root / "com" / "acme" / "pkg" / "Util.java").write_text(
        "package com.acme.pkg;\nclass Util {}\n", encoding="utf-8"
    )
    return "Main.java", "com/acme/pkg/Util.java"


def _csharp_setup(root: Path) -> tuple[str, str]:
    (root / "Services").mkdir(parents=True)
    (root / "Program.cs").write_text("using Services;\nclass Program {}\n", encoding="utf-8")
    (root / "Services" / "Thing.cs").write_text(
        "namespace Services { class Thing {} }\n", encoding="utf-8"
    )
    return "Program.cs", "Services/Thing.cs"


def _ruby_setup(root: Path) -> tuple[str, str]:
    (root / "lib").mkdir(parents=True)
    (root / "main.rb").write_text('require_relative "lib/helper"\n', encoding="utf-8")
    (root / "lib" / "helper.rb").write_text("def helper; end\n", encoding="utf-8")
    return "main.rb", "lib/helper.rb"


def _php_setup(root: Path) -> tuple[str, str]:
    (root / "lib").mkdir(parents=True)
    (root / "main.php").write_text('<?php\nrequire "lib/helper.php";\n', encoding="utf-8")
    (root / "lib" / "helper.php").write_text("<?php\nfunction helper() {}\n", encoding="utf-8")
    return "main.php", "lib/helper.php"


def _kotlin_setup(root: Path) -> tuple[str, str]:
    (root / "com" / "acme" / "pkg").mkdir(parents=True)
    (root / "Main.kt").write_text("import com.acme.pkg.Util\n", encoding="utf-8")
    (root / "com" / "acme" / "pkg" / "Util.kt").write_text(
        "package com.acme.pkg\nclass Util\n", encoding="utf-8"
    )
    return "Main.kt", "com/acme/pkg/Util.kt"


def _scala_setup(root: Path) -> tuple[str, str]:
    (root / "com" / "acme" / "pkg").mkdir(parents=True)
    (root / "Main.scala").write_text("import com.acme.pkg.Util\n", encoding="utf-8")
    (root / "com" / "acme" / "pkg" / "Util.scala").write_text(
        "package com.acme.pkg\nclass Util\n", encoding="utf-8"
    )
    return "Main.scala", "com/acme/pkg/Util.scala"


def _dart_setup(root: Path) -> tuple[str, str]:
    (root / "lib").mkdir(parents=True)
    (root / "main.dart").write_text('import "lib/helper.dart";\n', encoding="utf-8")
    (root / "lib" / "helper.dart").write_text("void helper() {}\n", encoding="utf-8")
    return "main.dart", "lib/helper.dart"


def _lua_setup(root: Path) -> tuple[str, str]:
    (root / "foo").mkdir(parents=True)
    (root / "main.lua").write_text('local bar = require("foo.bar")\n', encoding="utf-8")
    (root / "foo" / "bar.lua").write_text("return {}\n", encoding="utf-8")
    return "main.lua", "foo/bar.lua"


def _zig_setup(root: Path) -> tuple[str, str]:
    (root / "main.zig").write_text('const foo = @import("./foo.zig");\n', encoding="utf-8")
    (root / "foo.zig").write_text("pub const X = 1;\n", encoding="utf-8")
    return "main.zig", "foo.zig"


_CASES = [
    _LangCase("go", _go_setup),
    _LangCase("java", _java_setup),
    _LangCase("csharp", _csharp_setup),
    _LangCase("ruby", _ruby_setup),
    _LangCase("php", _php_setup),
    _LangCase("kotlin", _kotlin_setup),
    _LangCase("scala", _scala_setup),
    _LangCase("dart", _dart_setup),
    _LangCase("lua", _lua_setup),
    _LangCase("zig", _zig_setup),
]


def _read_then_grep_messages(entry_rel: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_read", "name": "Read", "input": {"file_path": entry_rel}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call_read", "content": "..."}],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_grep", "name": "Grep", "input": {"pattern": "TODO"}}
            ],
        },
    ]


def _read_then_bash_messages(entry_rel: str, command: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_read", "name": "Read", "input": {"file_path": entry_rel}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call_read", "content": "..."}],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_bash", "name": "bash", "input": {"command": command}}
            ],
        },
    ]


def _wide_dump(entry_rel: str, related_rel: str) -> str:
    """`_graph_narrow` requires at least 2 kept (related) lines to bother
    narrowing at all (below that the "gain" isn't worth it) -- BFS from the
    entrypoint always includes the entrypoint itself as the first visited
    node, so one line has to mention `entry_rel` and one `related_rel` to
    clear that floor, matching `_wide_grep_dump()` in test_graph_narrow.py.
    """
    lines = [f"noise_{i}.txt:{i}: unrelated filler line number {i}" for i in range(_UNRELATED_LINE_COUNT)]
    lines.append(f"{entry_rel}:1: the entrypoint's own match")
    lines.append(f"{related_rel}:1: the actually relevant match")
    return "\n".join(lines)


@pytest.mark.parametrize("case", _CASES, ids=[c.id for c in _CASES])
def test_graph_narrow_reduces_tokens_via_native_discovery_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: _LangCase
) -> None:
    """`ContentRouter._graph_narrow`, called the way the harness's own
    Grep/Glob/LS-equivalent tool would trigger it, must drop the unrelated
    noise and keep only the line(s) reachable from the entrypoint via the
    new language's import graph -- and that drop must be a REAL token
    reduction, not a no-op.
    """
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    entry_rel, related_rel = case.setup(root)

    router = ContentRouter()
    router._build_tool_name_map(_read_then_grep_messages(entry_rel))
    assert router._last_read_path == entry_rel

    dump = _wide_dump(entry_rel, related_rel)
    narrowed = router._graph_narrow("grep", "call_grep", dump)

    assert narrowed is not None, f"{case.id}: narrowing did not fire (no edge resolved?)"
    assert related_rel in narrowed
    assert "noise_0.txt" not in narrowed

    original_tokens = len(dump) // 4
    narrowed_tokens = len(narrowed) // 4
    assert narrowed_tokens < original_tokens, f"{case.id}: narrowed output is not smaller"
    savings = 1 - (narrowed_tokens / original_tokens)
    assert savings > 0.5, f"{case.id}: only {savings:.0%} token savings, expected > 50%"


@pytest.mark.parametrize(
    "case,bash_command",
    [
        (_CASES[1], "grep -r Util ."),  # java
        (_CASES[3], "find . -name '*.rb'"),  # ruby
        (_CASES[8], "ls -R"),  # lua
    ],
    ids=["java+grep-r", "ruby+find", "lua+ls-R"],
)
def test_graph_narrow_reduces_tokens_via_bash_shelled_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: _LangCase, bash_command: str
) -> None:
    """Same as above, but through the #1925-follow-up bash path: a harness
    without a dedicated discovery tool shells `grep -r`/`find`/`ls -R`
    instead. Proves the two features (bash-command detection + the new
    language's import resolution) compose correctly, not just each in
    isolation.
    """
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    entry_rel, related_rel = case.setup(root)

    router = ContentRouter()
    router._build_tool_name_map(_read_then_bash_messages(entry_rel, bash_command))
    assert router._last_read_path == entry_rel

    dump = _wide_dump(entry_rel, related_rel)
    narrowed = router._graph_narrow("bash", "call_bash", dump)

    assert narrowed is not None, f"{case.id}: bash-shelled narrowing did not fire"
    assert related_rel in narrowed
    assert "noise_0.txt" not in narrowed
    assert len(narrowed) < len(dump)


def test_graph_narrow_telemetry_records_real_tokens_saved_for_a_new_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through `ContentRouter.apply()` (not the unit-level
    `_graph_narrow` call), matching the telemetry contract the proxy's
    PrometheusMetrics consumes -- proves the savings are visible on the
    metric a production dashboard would actually show, for a non-Python
    language.
    """
    from headroom.providers import OpenAIProvider
    from headroom.tokenizer import Tokenizer

    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    entry_rel, related_rel = _java_setup(root)

    class _SpyObserver:
        def __init__(self) -> None:
            self.route_counts: dict | None = None

        def record_router_route_counts(self, route_counts: dict) -> None:
            self.route_counts = dict(route_counts)

    tokenizer = Tokenizer(OpenAIProvider().get_token_counter("gpt-4o"), "gpt-4o")
    spy = _SpyObserver()
    router = ContentRouter(
        config=ContentRouterConfig(protect_recent_reads_fraction=0.3), observer=spy
    )

    dump = _wide_dump(entry_rel, related_rel)
    messages = [
        *_read_then_grep_messages(entry_rel),
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_grep", "content": dump}]},
    ]
    router.apply(messages, tokenizer)

    assert spy.route_counts is not None
    saved = spy.route_counts.get("graph_narrow_lossless_tokens_saved", 0)
    assert saved > 0, "no token savings recorded for a new-language (Java) graph-narrow pass"
