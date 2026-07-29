"""Dangling ``tool_reference`` recovery + sticky tool-search deferral.

Regression guard for the 400 ``Tool reference 'X' not found in available tools``.
The failure is not specific to the proxy-injected ``headroom_retrieve``: every
tool ``inject_tool_search_deferral`` defers gets a ``tool_reference`` in the
transcript, and any of them can leave ``tools`` on a later turn.
"""

from __future__ import annotations

import pytest

from headroom.proxy.helpers import inject_tool_search_deferral
from headroom.proxy.tool_search_recovery import (
    collect_tool_reference_names,
    reconcile_tool_references,
    remember_deferred_tools,
    reset_state,
    session_has_deferred,
)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_state()
    yield
    reset_state()


CORE = ["bash", "read", "write", "edit", "glob", "grep", "task", "todowrite"]


def _tool(name):
    return {"name": name, "description": "d", "input_schema": {"type": "object"}}


def _tools(mcp_count):
    return [_tool(n) for n in CORE] + [_tool(f"mcp__linear__op{i}") for i in range(mcp_count)]


def _search_result_msg(*names):
    """The server-side shape: references nested in a DICT ``content``."""
    return {
        "role": "assistant",
        "content": [
            {
                "type": "server_tool_use",
                "id": "srvtoolu_1",
                "name": "tool_search_tool_regex",
                "input": {"pattern": "op"},
            },
            {
                "type": "tool_search_tool_result",
                "tool_use_id": "srvtoolu_1",
                "content": {
                    "type": "tool_search_tool_search_result",
                    "tool_references": [{"type": "tool_reference", "tool_name": n} for n in names],
                },
            },
        ],
    }


def _tool_result_msg(*names):
    """The custom client-side shape: references in a LIST ``content``."""
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": [{"type": "tool_reference", "tool_name": n} for n in names],
            }
        ],
    }


# --- collection -------------------------------------------------------------


def test_collect_finds_all_reference_shapes():
    messages = [
        _search_result_msg("a"),
        _tool_result_msg("b"),
        {"role": "user", "content": [{"type": "tool_reference", "tool_name": "c"}]},
        # Mid-conversation shape wraps the reference under "tool" and uses "name".
        {
            "role": "system",
            "content": [{"type": "tool_addition", "tool": {"type": "tool_reference", "name": "d"}}],
        },
    ]
    assert collect_tool_reference_names(messages) == {"a", "b", "c", "d"}


def test_collect_tolerates_malformed_input():
    assert collect_tool_reference_names(None) == set()
    assert collect_tool_reference_names([]) == set()
    assert (
        collect_tool_reference_names(
            [
                "not-a-dict",
                {"role": "user", "content": "plain string"},
                {"role": "user", "content": [None, {"type": "text"}]},
                {"no_content": True},
                # A reference with no usable name must not yield an empty string.
                {"role": "user", "content": [{"type": "tool_reference", "tool_name": ""}]},
            ]
        )
        == set()
    )


# --- tier 1: re-injection from the deferred cache ---------------------------


def test_reinjects_any_deferred_tool_not_just_headroom():
    deferred = inject_tool_search_deferral(_tools(6), session_id="s1")
    assert any(t.get("defer_loading") for t in deferred)

    # Next turn the Linear MCP server is gone: its tools are absent from `tools`
    # while the transcript still references one of them.
    shrunk = [_tool(n) for n in CORE]
    messages = [_search_result_msg("mcp__linear__op4")]

    result = reconcile_tool_references(messages, shrunk, session_id="s1")

    assert result.reinjected == ["mcp__linear__op4"]
    assert result.sanitized == []
    assert result.changed is True
    names = [t["name"] for t in result.tools]
    assert "mcp__linear__op4" in names
    # Restored verbatim and still deferred → no context cost.
    restored = next(t for t in result.tools if t["name"] == "mcp__linear__op4")
    assert restored["defer_loading"] is True
    assert restored["input_schema"] == {"type": "object"}
    # Transcript untouched when tier 1 resolves everything.
    assert result.messages is messages


def test_reinjection_is_session_scoped():
    inject_tool_search_deferral(_tools(6), session_id="s1")
    messages = [_search_result_msg("mcp__linear__op4")]

    other = reconcile_tool_references(messages, [_tool(n) for n in CORE], session_id="s2")
    assert other.reinjected == []
    # Nothing cached for s2 → falls through to sanitization.
    assert other.sanitized == ["mcp__linear__op4"]


def test_noop_when_every_reference_is_declared():
    tools = _tools(6)
    messages = [_search_result_msg("mcp__linear__op1")]
    result = reconcile_tool_references(messages, tools, session_id="s1")
    assert result.changed is False
    assert result.tools is tools
    assert result.messages is messages


def test_noop_when_transcript_has_no_references():
    tools = _tools(6)
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    result = reconcile_tool_references(messages, tools, session_id="s1")
    assert result.changed is False
    assert result.tools is tools
    assert result.messages is messages


# --- tier 2: sanitization backstop -----------------------------------------


def test_sanitizes_unknown_reference_in_search_result():
    messages = [_search_result_msg("ghost_tool", "mcp__linear__op1")]
    tools = _tools(6)  # op1 declared, ghost_tool never seen

    result = reconcile_tool_references(messages, tools, session_id="s1")

    assert result.reinjected == []
    assert result.sanitized == ["ghost_tool"]
    refs = result.messages[0]["content"][1]["content"]["tool_references"]
    assert [r["tool_name"] for r in refs] == ["mcp__linear__op1"]
    # Inputs are never mutated in place.
    assert len(messages[0]["content"][1]["content"]["tool_references"]) == 2
    # The paired server_tool_use block survives, so the turn stays well-formed.
    assert result.messages[0]["content"][0]["type"] == "server_tool_use"


def test_sanitizing_every_reference_leaves_the_documented_empty_shape():
    messages = [_search_result_msg("ghost_tool")]
    result = reconcile_tool_references(messages, _tools(6), session_id="s1")
    block = result.messages[0]["content"][1]
    assert block["content"]["type"] == "tool_search_tool_search_result"
    assert block["content"]["tool_references"] == []


def test_sanitizes_tool_result_shape_with_text_placeholder():
    messages = [_tool_result_msg("ghost_tool")]
    result = reconcile_tool_references(messages, _tools(6), session_id="s1")
    content = result.messages[0]["content"][0]["content"]
    # A tool_result needs content, so an emptied list gets an explanatory block.
    assert content == [{"type": "text", "text": "(referenced tool is no longer available)"}]


def test_sanitizes_top_level_reference_block():
    """A bare reference IS the orphan, so the whole block is dropped."""
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "searching"},
                {"type": "tool_reference", "tool_name": "ghost_tool"},
            ],
        }
    ]

    result = reconcile_tool_references(messages, _tools(6), session_id="s1")

    assert result.reinjected == []
    # A trailing orphan shortens the list without changing any surviving block,
    # so only a length comparison notices the rewrite.
    assert result.sanitized == ["ghost_tool"]
    assert result.messages[0]["content"] == [{"type": "text", "text": "searching"}]
    # Inputs are never mutated in place.
    assert len(messages[0]["content"]) == 2


def test_sanitizing_a_lone_top_level_reference_keeps_content_non_empty():
    messages = [
        {"role": "assistant", "content": [{"type": "tool_reference", "tool_name": "ghost"}]}
    ]

    result = reconcile_tool_references(messages, _tools(6), session_id="s1")

    assert result.sanitized == ["ghost"]
    # An empty content array is itself a 400, so the block is replaced, not removed.
    assert result.messages[0]["content"] == [
        {"type": "text", "text": "(referenced tool is no longer available)"}
    ]


def test_tool_addition_is_left_alone():
    """Explicit client instructions are never rewritten — only reported."""
    messages = [
        {
            "role": "system",
            "content": [
                {"type": "tool_addition", "tool": {"type": "tool_reference", "name": "ghost"}}
            ],
        }
    ]
    result = reconcile_tool_references(messages, _tools(6), session_id="s1")
    assert result.reinjected == []
    assert result.sanitized == []
    assert result.messages is messages


# --- sticky deferral --------------------------------------------------------


def test_deferral_is_sticky_once_a_session_has_deferred():
    assert session_has_deferred("s1") is False

    first = inject_tool_search_deferral(_tools(6), session_id="s1")  # 14 tools
    assert first is not _tools(6)
    assert session_has_deferred("s1") is True

    # Turn N+1: 11 tools — below _TOOL_SEARCH_MIN_TOOLS. Without stickiness this
    # returned the input untouched, dropping the search tool mid-session.
    shrunk = _tools(3)
    second = inject_tool_search_deferral(shrunk, session_id="s1")
    assert second is not shrunk
    assert any(str(t.get("type", "")).startswith("tool_search_tool_") for t in second)
    assert [t["name"] for t in second if t.get("defer_loading")] == [
        f"mcp__linear__op{i}" for i in range(3)
    ]


def test_below_threshold_still_noops_without_a_session():
    shrunk = _tools(3)
    assert inject_tool_search_deferral(shrunk) is shrunk
    assert inject_tool_search_deferral(shrunk, session_id="unknown") is shrunk


def test_client_owned_tool_search_is_never_touched_even_when_sticky():
    inject_tool_search_deferral(_tools(6), session_id="s1")
    client_defers = [{"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"}]
    client_defers += _tools(3)
    assert inject_tool_search_deferral(client_defers, session_id="s1") is client_defers


# --- cache bookkeeping ------------------------------------------------------


def test_remember_ignores_non_deferred_and_nameless_tools():
    remember_deferred_tools("s1", [_tool("resident"), {"defer_loading": True}, "junk"])
    assert session_has_deferred("s1") is False


def test_remember_accumulates_across_turns():
    remember_deferred_tools("s1", [{**_tool("a"), "defer_loading": True}])
    remember_deferred_tools("s1", [{**_tool("b"), "defer_loading": True}])
    result = reconcile_tool_references(
        [_search_result_msg("a", "b")], [_tool(n) for n in CORE], session_id="s1"
    )
    assert result.reinjected == ["a", "b"]


def test_no_session_id_disables_cache_and_stickiness():
    remember_deferred_tools(None, [{**_tool("a"), "defer_loading": True}])
    assert session_has_deferred(None) is False
    result = reconcile_tool_references(
        [_search_result_msg("a")], [_tool(n) for n in CORE], session_id=None
    )
    assert result.reinjected == []
    assert result.sanitized == ["a"]
