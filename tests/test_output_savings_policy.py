"""Tests for pure output savings policy helpers."""

from __future__ import annotations

from headroom.proxy.output_savings_policy import (
    assign_arm,
    conversation_key_from_body,
    input_bucket,
    model_family,
    parse_stratum_label,
    stratum_key,
    stratum_label,
)


def test_stratum_key_is_most_to_least_specific() -> None:
    key = stratum_key(
        turn_kind="new_user_ask",
        input_tokens=5000,
        model="claude-opus-4-8",
        has_tools=True,
    )

    assert key == "opus|new_user_ask|s|tools"


def test_input_bucket_and_model_family_are_coarse() -> None:
    assert [input_bucket(v) for v in (0, 2_000, 8_000, 32_000, 200_000)] == [
        "xs",
        "s",
        "m",
        "l",
        "xl",
    ]
    assert model_family("claude-sonnet-4-6") == "sonnet"
    assert model_family("unknown-model") == "other"


def test_assign_arm_is_stable_and_respects_extreme_holdouts() -> None:
    assert assign_arm("conv-123", 0.0) == "treatment"
    assert assign_arm("conv-123", 1.0) == "control"
    assert assign_arm("conv-123", 0.5) == assign_arm("conv-123", 0.5)


def test_conversation_key_uses_response_create_payload() -> None:
    http_body = {"model": "gpt-5", "input": "build a cache"}
    ws_body = {
        "type": "response.create",
        "response": {"model": "gpt-5", "input": "build a cache"},
    }

    assert conversation_key_from_body(http_body) == conversation_key_from_body(ws_body)


def test_stratum_label_round_trips_arm_key_and_level() -> None:
    key = "opus|code|m|tools"

    assert parse_stratum_label(stratum_label("treatment", key, verbosity_level=3)) == (
        "treatment",
        key,
        3,
    )
    assert parse_stratum_label(stratum_label("control", key, verbosity_level=0)) == (
        "control",
        key,
        0,
    )
    assert parse_stratum_label("unrelated") is None


def test_untagged_stratum_label_round_trips_with_no_level() -> None:
    # The pre-#3395 label shape. It still decodes — the ledger needs the arm and
    # stratum to file it — but with no level, which is what makes it excluded
    # from the estimate rather than credited to shaping.
    key = "opus|code|m|tools"

    assert parse_stratum_label(stratum_label("treatment", key)) == ("treatment", key, None)
    assert parse_stratum_label(stratum_label("control", key)) == ("control", key, None)


def test_level_tag_does_not_disturb_prefix_matching() -> None:
    # Downstream matchers (and the WS lifecycle tests) match on the label prefix
    # plus stratum key; the level rides as a tail so those keep working.
    label = stratum_label("treatment", "gpt|new_user_ask|s|tools", verbosity_level=2)

    assert label.startswith("output_shaper:stratum:gpt|new_user_ask|s|")
    assert label.endswith(":L2")
