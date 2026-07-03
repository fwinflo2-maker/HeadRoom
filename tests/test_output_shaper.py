"""Tests for headroom.proxy.output_shaper.

Covers turn classification (structural only), cache-safe verbosity steering,
effort routing on mechanical continuations, and the env-driven gate.
"""

from __future__ import annotations

import copy
from typing import Any

from headroom.proxy.output_shaper import (
    LEGACY_THINKING_FLOOR,
    OutputShaperSettings,
    TurnKind,
    apply_verbosity_steering,
    classify_turn,
    route_effort,
    shape_request,
    steering_text,
)

ENABLED = OutputShaperSettings(enabled=True)


def _tool_result(is_error: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": "toolu_01",
        "content": "ok",
    }
    if is_error:
        block["is_error"] = True
    return block


def _mechanical_messages() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "fix the bug in foo.py"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Reading the file."},
                {"type": "tool_use", "id": "toolu_01", "name": "Read", "input": {}},
            ],
        },
        {"role": "user", "content": [_tool_result()]},
    ]


# ---------------------------------------------------------------------------
# classify_turn
# ---------------------------------------------------------------------------


class TestClassifyTurn:
    def test_string_user_message_is_new_ask(self):
        assert classify_turn([{"role": "user", "content": "explain this"}]) == TurnKind.NEW_USER_ASK

    def test_clean_tool_result_is_mechanical(self):
        assert classify_turn(_mechanical_messages()) == TurnKind.MECHANICAL_CONTINUATION

    def test_multiple_clean_tool_results_are_mechanical(self):
        msgs = _mechanical_messages()
        msgs[-1]["content"].append(_tool_result())
        assert classify_turn(msgs) == TurnKind.MECHANICAL_CONTINUATION

    def test_error_tool_result_is_error_continuation(self):
        msgs = _mechanical_messages()
        msgs[-1]["content"] = [_tool_result(), _tool_result(is_error=True)]
        assert classify_turn(msgs) == TurnKind.ERROR_CONTINUATION

    def test_text_block_alongside_tool_result_is_new_ask(self):
        msgs = _mechanical_messages()
        msgs[-1]["content"].append({"type": "text", "text": "also check bar.py"})
        assert classify_turn(msgs) == TurnKind.NEW_USER_ASK

    def test_image_block_is_new_ask(self):
        msgs = [{"role": "user", "content": [{"type": "image", "source": {}}]}]
        assert classify_turn(msgs) == TurnKind.NEW_USER_ASK

    def test_assistant_last_is_unknown(self):
        msgs = [{"role": "assistant", "content": "hello"}]
        assert classify_turn(msgs) == TurnKind.UNKNOWN

    def test_empty_messages_is_unknown(self):
        assert classify_turn([]) == TurnKind.UNKNOWN

    def test_empty_content_list_is_unknown(self):
        assert classify_turn([{"role": "user", "content": []}]) == TurnKind.UNKNOWN

    def test_whitespace_string_content_is_unknown(self):
        assert classify_turn([{"role": "user", "content": "  "}]) == TurnKind.UNKNOWN


# ---------------------------------------------------------------------------
# apply_verbosity_steering
# ---------------------------------------------------------------------------


class TestVerbositySteering:
    def test_level_zero_is_noop(self):
        body = {"system": "You are helpful."}
        assert apply_verbosity_steering(body, 0) is False
        assert body["system"] == "You are helpful."

    def test_string_system_converted_to_blocks_with_original_bytes_first(self):
        body = {"system": "You are helpful."}
        assert apply_verbosity_steering(body, 2) is True
        assert body["system"][0] == {"type": "text", "text": "You are helpful."}
        assert body["system"][1]["text"] == steering_text(2)

    def test_missing_system_creates_steering_only_block(self):
        body: dict[str, Any] = {}
        assert apply_verbosity_steering(body, 2) is True
        assert body["system"] == [{"type": "text", "text": steering_text(2)}]

    def test_block_system_appends_after_cache_control(self):
        cached = {
            "type": "text",
            "text": "Big system prompt.",
            "cache_control": {"type": "ephemeral"},
        }
        body = {"system": [copy.deepcopy(cached)]}
        assert apply_verbosity_steering(body, 2) is True
        # The cached block is byte-identical and still first — prefix intact.
        assert body["system"][0] == cached
        assert body["system"][1] == {"type": "text", "text": steering_text(2)}
        # Our block carries no cache_control (breakpoints are a scarce resource).
        assert "cache_control" not in body["system"][1]

    def test_idempotent_at_same_level(self):
        body = {"system": [{"type": "text", "text": "Sys."}]}
        assert apply_verbosity_steering(body, 2) is True
        snapshot = copy.deepcopy(body)
        assert apply_verbosity_steering(body, 2) is False
        assert body == snapshot

    def test_level_change_replaces_block_in_place(self):
        body = {"system": [{"type": "text", "text": "Sys."}]}
        apply_verbosity_steering(body, 2)
        assert apply_verbosity_steering(body, 4) is True
        steering_blocks = [
            b for b in body["system"] if b["text"].startswith("<headroom_output_shaping>")
        ]
        assert len(steering_blocks) == 1
        assert steering_blocks[0]["text"] == steering_text(4)

    def test_steering_text_is_deterministic(self):
        for level in (1, 2, 3, 4):
            assert steering_text(level) == steering_text(level)


# ---------------------------------------------------------------------------
# route_effort
# ---------------------------------------------------------------------------


class TestRouteEffort:
    def test_lowers_explicit_effort_on_mechanical_turn(self):
        body = {"output_config": {"effort": "xhigh"}}
        labels = route_effort(body, TurnKind.MECHANICAL_CONTINUATION, ENABLED)
        assert body["output_config"]["effort"] == "low"
        assert labels == ["output_shaper:effort:xhigh->low"]

    def test_never_injects_effort_when_absent(self):
        body: dict[str, Any] = {"messages": []}
        labels = route_effort(body, TurnKind.MECHANICAL_CONTINUATION, ENABLED)
        assert "output_config" not in body
        assert labels == []

    def test_effort_untouched_on_new_ask(self):
        body = {"output_config": {"effort": "xhigh"}}
        assert route_effort(body, TurnKind.NEW_USER_ASK, ENABLED) == []
        assert body["output_config"]["effort"] == "xhigh"

    def test_effort_untouched_on_error_continuation(self):
        body = {"output_config": {"effort": "xhigh"}}
        assert route_effort(body, TurnKind.ERROR_CONTINUATION, ENABLED) == []
        assert body["output_config"]["effort"] == "xhigh"

    def test_effort_already_at_target_untouched(self):
        body = {"output_config": {"effort": "low"}}
        assert route_effort(body, TurnKind.MECHANICAL_CONTINUATION, ENABLED) == []

    def test_unknown_effort_value_untouched(self):
        body = {"output_config": {"effort": "turbo"}}
        assert route_effort(body, TurnKind.MECHANICAL_CONTINUATION, ENABLED) == []
        assert body["output_config"]["effort"] == "turbo"

    def test_configurable_mechanical_effort(self):
        settings = OutputShaperSettings(enabled=True, mechanical_effort="medium")
        body = {"output_config": {"effort": "xhigh"}}
        route_effort(body, TurnKind.MECHANICAL_CONTINUATION, settings)
        assert body["output_config"]["effort"] == "medium"

    def test_legacy_thinking_budget_clamped(self):
        body = {"thinking": {"type": "enabled", "budget_tokens": 32000}}
        labels = route_effort(body, TurnKind.MECHANICAL_CONTINUATION, ENABLED)
        assert body["thinking"]["budget_tokens"] == LEGACY_THINKING_FLOOR
        assert body["thinking"]["type"] == "enabled"  # never toggled
        assert labels == [f"output_shaper:thinking_budget:32000->{LEGACY_THINKING_FLOOR}"]

    def test_legacy_budget_at_floor_untouched(self):
        body = {"thinking": {"type": "enabled", "budget_tokens": LEGACY_THINKING_FLOOR}}
        assert route_effort(body, TurnKind.MECHANICAL_CONTINUATION, ENABLED) == []

    def test_adaptive_thinking_untouched(self):
        body = {"thinking": {"type": "adaptive"}}
        assert route_effort(body, TurnKind.MECHANICAL_CONTINUATION, ENABLED) == []
        assert body["thinking"] == {"type": "adaptive"}


# ---------------------------------------------------------------------------
# shape_request (end to end)
# ---------------------------------------------------------------------------


class TestShapeRequest:
    def test_disabled_is_noop(self):
        body = {
            "system": "Sys.",
            "messages": _mechanical_messages(),
            "output_config": {"effort": "xhigh"},
        }
        snapshot = copy.deepcopy(body)
        result = shape_request(body, OutputShaperSettings(enabled=False))
        assert result.changed is False
        assert body == snapshot

    def test_enabled_applies_steering_and_effort_routing(self):
        body = {
            "system": "Sys.",
            "messages": _mechanical_messages(),
            "output_config": {"effort": "xhigh"},
            "thinking": {"type": "adaptive"},
        }
        result = shape_request(body, ENABLED)
        assert result.changed is True
        assert result.labels == [
            "output_shaper:verbosity:L2",
            "output_shaper:effort:xhigh->low",
        ]
        assert body["output_config"]["effort"] == "low"
        assert body["system"][1]["text"] == steering_text(2)

    def test_new_ask_gets_steering_but_keeps_effort(self):
        body = {
            "system": "Sys.",
            "messages": [{"role": "user", "content": "design a cache layer"}],
            "output_config": {"effort": "xhigh"},
        }
        result = shape_request(body, ENABLED)
        assert result.labels == ["output_shaper:verbosity:L2"]
        assert body["output_config"]["effort"] == "xhigh"

    def test_second_pass_is_stable(self):
        body = {"system": "Sys.", "messages": _mechanical_messages()}
        shape_request(body, ENABLED)
        snapshot = copy.deepcopy(body)
        result = shape_request(body, ENABLED)
        assert result.changed is False
        assert body == snapshot

    def test_from_env_defaults_off(self, monkeypatch):
        monkeypatch.delenv("HEADROOM_OUTPUT_SHAPER", raising=False)
        assert OutputShaperSettings.from_env().enabled is False

    def test_from_env_enabled_with_overrides(self, monkeypatch):
        monkeypatch.setenv("HEADROOM_OUTPUT_SHAPER", "1")
        monkeypatch.setenv("HEADROOM_VERBOSITY_LEVEL", "3")
        monkeypatch.setenv("HEADROOM_MECHANICAL_EFFORT", "medium")
        settings = OutputShaperSettings.from_env()
        assert settings.enabled is True
        assert settings.verbosity_level == 3
        assert settings.mechanical_effort == "medium"

    def test_from_env_clamps_bad_values(self, monkeypatch):
        monkeypatch.setenv("HEADROOM_OUTPUT_SHAPER", "true")
        monkeypatch.setenv("HEADROOM_VERBOSITY_LEVEL", "99")
        monkeypatch.setenv("HEADROOM_MECHANICAL_EFFORT", "bogus")
        settings = OutputShaperSettings.from_env()
        assert settings.verbosity_level == 4
        assert settings.mechanical_effort == "low"


# ---------------------------------------------------------------------------
# OpenAI provider variants
# ---------------------------------------------------------------------------


def _openai_mechanical_messages(tool_content: Any = "file contents...") -> list[dict[str, Any]]:
    """A representative OpenAI Chat Completions turn ending in a clean tool result."""
    return [
        {"role": "user", "content": "fix the bug in foo.py"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_01",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"foo.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_01", "content": tool_content},
    ]


class TestClassifyTurnOpenAI:
    def test_string_user_message_is_new_ask(self):
        msgs = [{"role": "user", "content": "explain this"}]
        assert classify_turn(msgs, provider="openai") == TurnKind.NEW_USER_ASK

    def test_list_content_user_message_is_new_ask(self):
        msgs = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "look at this"}],
            }
        ]
        assert classify_turn(msgs, provider="openai") == TurnKind.NEW_USER_ASK

    def test_tool_message_string_content_is_mechanical(self):
        assert (
            classify_turn(_openai_mechanical_messages(), provider="openai")
            == TurnKind.MECHANICAL_CONTINUATION
        )

    def test_tool_message_error_prefix_is_error_continuation(self):
        msgs = _openai_mechanical_messages(tool_content="Error: file not found")
        assert classify_turn(msgs, provider="openai") == TurnKind.ERROR_CONTINUATION

    def test_tool_message_traceback_prefix_is_error(self):
        msgs = _openai_mechanical_messages(tool_content="Traceback (most recent call last):\n  ...")
        assert classify_turn(msgs, provider="openai") == TurnKind.ERROR_CONTINUATION

    def test_tool_message_dict_error_key_is_error(self):
        msgs = _openai_mechanical_messages(tool_content={"error": "boom"})
        assert classify_turn(msgs, provider="openai") == TurnKind.ERROR_CONTINUATION

    def test_tool_message_dict_no_error_is_mechanical(self):
        msgs = _openai_mechanical_messages(tool_content={"output": "ok"})
        assert classify_turn(msgs, provider="openai") == TurnKind.MECHANICAL_CONTINUATION

    def test_one_error_in_multi_tool_block_is_error(self):
        base = _openai_mechanical_messages()
        base.append({"role": "tool", "tool_call_id": "call_02", "content": "Error: boom"})
        base.append({"role": "tool", "tool_call_id": "call_03", "content": "ok"})
        assert classify_turn(base, provider="openai") == TurnKind.ERROR_CONTINUATION

    def test_assistant_last_is_unknown(self):
        msgs = [{"role": "assistant", "content": "hello"}]
        assert classify_turn(msgs, provider="openai") == TurnKind.UNKNOWN

    def test_empty_messages_is_unknown(self):
        assert classify_turn([], provider="openai") == TurnKind.UNKNOWN

    def test_empty_user_content_is_unknown(self):
        assert (
            classify_turn(
                [{"role": "user", "content": ""}],
                provider="openai",
            )
            == TurnKind.UNKNOWN
        )


class TestVerbositySteeringOpenAI:
    def test_level_zero_is_noop(self):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        snapshot = copy.deepcopy(body)
        assert apply_verbosity_steering(body, 0, provider="openai") is False
        assert body == snapshot

    def test_missing_messages_creates_system_message(self):
        body: dict[str, Any] = {}
        assert apply_verbosity_steering(body, 2, provider="openai") is True
        assert body["messages"] == [{"role": "system", "content": steering_text(2)}]

    def test_user_only_prepends_steering(self):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        assert apply_verbosity_steering(body, 2, provider="openai") is True
        # Steering must appear before the user turn so the model sees it as
        # an instruction, not as prior context.
        assert body["messages"][0] == {"role": "system", "content": steering_text(2)}
        assert body["messages"][1] == {"role": "user", "content": "hi"}

    def test_existing_system_gets_steering_after_leading_block(self):
        body = {
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "hi"},
            ]
        }
        assert apply_verbosity_steering(body, 2, provider="openai") is True
        # Existing system message is byte-identical (prefix cache preserved).
        assert body["messages"][0] == {"role": "system", "content": "You are helpful."}
        # Steering slotted immediately after the last leading system message.
        assert body["messages"][1] == {"role": "system", "content": steering_text(2)}
        assert body["messages"][2] == {"role": "user", "content": "hi"}

    def test_idempotent_at_same_level(self):
        body = {
            "messages": [
                {"role": "system", "content": "Sys."},
                {"role": "user", "content": "hi"},
            ]
        }
        assert apply_verbosity_steering(body, 2, provider="openai") is True
        snapshot = copy.deepcopy(body)
        assert apply_verbosity_steering(body, 2, provider="openai") is False
        assert body == snapshot

    def test_level_change_replaces_block_in_place(self):
        body = {
            "messages": [
                {"role": "system", "content": "Sys."},
                {"role": "user", "content": "hi"},
            ]
        }
        apply_verbosity_steering(body, 2, provider="openai")
        original_len = len(body["messages"])
        assert apply_verbosity_steering(body, 4, provider="openai") is True
        # Level change replaces text in place, doesn't duplicate the block.
        assert len(body["messages"]) == original_len
        steering_msgs = [
            m
            for m in body["messages"]
            if m.get("role") == "system"
            and isinstance(m.get("content"), str)
            and "<headroom_output_shaping>" in m["content"]
        ]
        assert len(steering_msgs) == 1
        assert steering_msgs[0]["content"] == steering_text(4)


class TestRouteEffortOpenAI:
    def test_lowers_reasoning_effort_on_mechanical_turn(self):
        body = {"reasoning_effort": "high"}
        labels = route_effort(body, TurnKind.MECHANICAL_CONTINUATION, ENABLED, provider="openai")
        assert body["reasoning_effort"] == "low"
        assert labels == ["output_shaper:effort:high->low"]

    def test_lowers_responses_reasoning_effort_on_mechanical(self):
        body = {"reasoning": {"effort": "high", "summary": "auto"}}
        labels = route_effort(body, TurnKind.MECHANICAL_CONTINUATION, ENABLED, provider="openai")
        assert body["reasoning"]["effort"] == "low"
        assert body["reasoning"]["summary"] == "auto"  # sibling fields untouched
        assert labels == ["output_shaper:effort:high->low"]

    def test_never_injects_effort_when_absent(self):
        body: dict[str, Any] = {"messages": []}
        labels = route_effort(body, TurnKind.MECHANICAL_CONTINUATION, ENABLED, provider="openai")
        assert "reasoning_effort" not in body
        assert "reasoning" not in body
        assert labels == []

    def test_effort_untouched_on_new_ask(self):
        body = {"reasoning_effort": "high"}
        labels = route_effort(body, TurnKind.NEW_USER_ASK, ENABLED, provider="openai")
        assert body["reasoning_effort"] == "high"
        assert labels == []

    def test_effort_untouched_on_error_continuation(self):
        body = {"reasoning_effort": "high"}
        labels = route_effort(body, TurnKind.ERROR_CONTINUATION, ENABLED, provider="openai")
        assert body["reasoning_effort"] == "high"
        assert labels == []

    def test_effort_already_at_target_untouched(self):
        body = {"reasoning_effort": "low"}
        assert (
            route_effort(body, TurnKind.MECHANICAL_CONTINUATION, ENABLED, provider="openai") == []
        )

    def test_effort_below_target_untouched(self):
        body = {"reasoning_effort": "minimal"}
        assert (
            route_effort(body, TurnKind.MECHANICAL_CONTINUATION, ENABLED, provider="openai") == []
        )

    def test_unknown_effort_value_untouched(self):
        body = {"reasoning_effort": "turbo"}
        assert (
            route_effort(body, TurnKind.MECHANICAL_CONTINUATION, ENABLED, provider="openai") == []
        )
        assert body["reasoning_effort"] == "turbo"

    def test_configurable_mechanical_effort(self):
        settings = OutputShaperSettings(enabled=True, mechanical_effort="medium")
        body = {"reasoning_effort": "high"}
        route_effort(body, TurnKind.MECHANICAL_CONTINUATION, settings, provider="openai")
        assert body["reasoning_effort"] == "medium"

    def test_both_reasoning_shapes_present_both_clamped(self):
        # If a client sends both (unusual but valid), we clamp both — the
        # transform is symmetric on the wire-shape axis.
        body = {
            "reasoning_effort": "high",
            "reasoning": {"effort": "high"},
        }
        labels = route_effort(body, TurnKind.MECHANICAL_CONTINUATION, ENABLED, provider="openai")
        assert body["reasoning_effort"] == "low"
        assert body["reasoning"]["effort"] == "low"
        assert labels.count("output_shaper:effort:high->low") == 2


class TestShapeRequestOpenAI:
    def test_disabled_is_noop(self):
        body = {
            "messages": _openai_mechanical_messages(),
            "reasoning_effort": "high",
        }
        snapshot = copy.deepcopy(body)
        result = shape_request(body, OutputShaperSettings(enabled=False), provider="openai")
        assert result.changed is False
        assert body == snapshot

    def test_enabled_applies_steering_and_effort_routing(self):
        body = {
            "messages": _openai_mechanical_messages(),
            "reasoning_effort": "high",
        }
        result = shape_request(body, ENABLED, provider="openai")
        assert result.changed is True
        assert result.labels == [
            "output_shaper:verbosity:L2",
            "output_shaper:effort:high->low",
        ]
        assert body["reasoning_effort"] == "low"
        # Steering is present as a system message at the head of the list.
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][0]["content"] == steering_text(2)

    def test_new_ask_gets_steering_but_keeps_effort(self):
        body = {
            "messages": [{"role": "user", "content": "design a cache layer"}],
            "reasoning_effort": "high",
        }
        result = shape_request(body, ENABLED, provider="openai")
        assert result.labels == ["output_shaper:verbosity:L2"]
        assert body["reasoning_effort"] == "high"

    def test_error_continuation_gets_steering_but_keeps_effort(self):
        msgs = _openai_mechanical_messages(tool_content="Error: boom")
        body = {"messages": msgs, "reasoning_effort": "high"}
        result = shape_request(body, ENABLED, provider="openai")
        assert result.labels == ["output_shaper:verbosity:L2"]
        assert body["reasoning_effort"] == "high"

    def test_second_pass_is_stable(self):
        body = {
            "messages": _openai_mechanical_messages(),
            "reasoning_effort": "high",
        }
        shape_request(body, ENABLED, provider="openai")
        snapshot = copy.deepcopy(body)
        result = shape_request(body, ENABLED, provider="openai")
        assert result.changed is False
        assert body == snapshot

    def test_responses_shape_only_effort(self):
        # Simulate a Responses-style body sans messages: only reasoning.effort
        # exists to be clamped. shape_request should not blow up on a body
        # that lacks a `messages` field.
        body = {"reasoning": {"effort": "high"}}
        # No messages => classify_turn returns UNKNOWN => no effort routing.
        # But verbosity steering will still create a messages list. This
        # documents the current behavior — for /v1/responses we bypass
        # shape_request and call route_effort directly (see handler wiring).
        result = shape_request(body, ENABLED, provider="openai")
        assert "output_shaper:verbosity:L2" in (result.labels or [])
        # Turn is UNKNOWN so effort untouched.
        assert body["reasoning"]["effort"] == "high"

    def test_label_vocabulary_matches_anthropic(self):
        # The savings ledger and outcome funnel key off label prefixes;
        # verify the OpenAI path emits identically-prefixed labels.
        openai_body = {
            "messages": _openai_mechanical_messages(),
            "reasoning_effort": "high",
        }
        anthropic_body = {
            "system": "Sys.",
            "messages": [
                {"role": "user", "content": "fix foo.py"},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01",
                            "content": "ok",
                        }
                    ],
                },
            ],
            "output_config": {"effort": "high"},
        }
        openai_result = shape_request(openai_body, ENABLED, provider="openai")
        anthropic_result = shape_request(anthropic_body, ENABLED, provider="anthropic")
        # Both emit verbosity:L2 + effort:high->low, in the same order.
        assert openai_result.labels == anthropic_result.labels
