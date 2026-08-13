from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from headroom.cli.main import main
from headroom.config import HeadroomConfig
from headroom.rollout import (
    FEATURES,
    FeatureDecisionReason,
    FeatureSpec,
    RolloutChannel,
    RolloutConfigurationError,
    registry_digest,
    resolve_rollout,
)
from headroom.transforms.pipeline import TransformPipeline


def test_default_stable_resolution_is_versioned_and_eligible() -> None:
    snapshot = resolve_rollout({})

    assert snapshot.channel is RolloutChannel.STABLE
    assert snapshot.schema_version == 1
    assert snapshot.policy_version == "1"
    assert snapshot.qualification_eligible is True


@pytest.mark.parametrize("channel", ["beta", "canary", "dev"])
def test_valid_rollout_channels(channel: str) -> None:
    assert resolve_rollout({"HEADROOM_ROLLOUT_CHANNEL": channel}).channel.value == channel


def test_unknown_channel_fails_closed_with_diagnostic(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        snapshot = resolve_rollout({"HEADROOM_ROLLOUT_CHANNEL": "stabel"})

    assert snapshot.channel is RolloutChannel.STABLE
    assert "unknown rollout channel 'stabel'; falling back to 'stable'" in caplog.text


def test_unknown_requested_and_disabled_features_fail_closed_and_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        snapshot = resolve_rollout(
            {
                "HEADROOM_FEATURES": "typo_requested",
                "HEADROOM_DISABLE_FEATURES": "typo_disabled",
            }
        )

    assert snapshot.config.requested == frozenset()
    assert snapshot.config.disabled == frozenset()
    assert "typo_requested" in caplog.text
    assert "typo_disabled" in caplog.text


def test_strict_configuration_rejects_unknown_input() -> None:
    with pytest.raises(RolloutConfigurationError, match="unknown rollout feature"):
        resolve_rollout({"HEADROOM_FEATURES": "typo"}, strict=True)


def test_stable_blocks_explicit_canary_feature() -> None:
    snapshot = resolve_rollout(
        {
            "HEADROOM_ROLLOUT_CHANNEL": "stable",
            "HEADROOM_FEATURES": "tool-result-interceptors",
        }
    )
    decision = snapshot.decision("tool_result_interceptors")

    assert decision.enabled is False
    assert decision.reason is FeatureDecisionReason.BLOCKED_BY_CHANNEL


def test_canary_allows_explicit_request() -> None:
    snapshot = resolve_rollout(
        {
            "HEADROOM_ROLLOUT_CHANNEL": "canary",
            "HEADROOM_FEATURES": "tool_result_interceptors",
        }
    )

    assert snapshot.decision("tool_result_interceptors").reason is FeatureDecisionReason.EXPLICIT


def test_non_default_feature_remains_off_when_not_requested() -> None:
    decision = resolve_rollout({"HEADROOM_ROLLOUT_CHANNEL": "dev"}).decision(
        "tool_result_interceptors"
    )
    assert decision.enabled is False
    assert decision.reason is FeatureDecisionReason.NOT_REQUESTED


def test_legacy_alias_obeys_channel_and_has_distinct_reason() -> None:
    stable = resolve_rollout(
        {"HEADROOM_ROLLOUT_CHANNEL": "stable", "HEADROOM_INTERCEPT_ENABLED": "1"}
    )
    canary = resolve_rollout(
        {"HEADROOM_ROLLOUT_CHANNEL": "canary", "HEADROOM_INTERCEPT_ENABLED": "1"}
    )

    assert (
        stable.decision("tool_result_interceptors").reason
        is FeatureDecisionReason.BLOCKED_BY_CHANNEL
    )
    assert canary.decision("tool_result_interceptors").reason is FeatureDecisionReason.LEGACY_ALIAS


@pytest.mark.parametrize("request_source", ["HEADROOM_FEATURES", "HEADROOM_INTERCEPT_ENABLED"])
def test_disable_beats_explicit_and_legacy_request(request_source: str) -> None:
    snapshot = resolve_rollout(
        {
            "HEADROOM_ROLLOUT_CHANNEL": "canary",
            request_source: "tool_result_interceptors"
            if request_source.endswith("FEATURES")
            else "1",
            "HEADROOM_DISABLE_FEATURES": "tool_result_interceptors",
        }
    )
    decision = snapshot.decision("tool_result_interceptors")

    assert decision.enabled is False
    assert decision.reason is FeatureDecisionReason.DISABLED


def test_unsafe_override_crosses_channel_and_poisons_qualification() -> None:
    snapshot = resolve_rollout(
        {
            "HEADROOM_ROLLOUT_CHANNEL": "stable",
            "HEADROOM_FEATURES": "tool_result_interceptors",
            "HEADROOM_UNSAFE_ALLOW_UNSTABLE_FEATURES": "1",
        }
    )
    payload = snapshot.to_dict()

    assert (
        snapshot.decision("tool_result_interceptors").reason
        is FeatureDecisionReason.UNSAFE_OVERRIDE
    )
    assert payload["qualification_eligible"] is False
    assert payload["qualification_ineligible_reason"] == "unsafe_rollout_override_active"


def test_disable_still_beats_unsafe_override() -> None:
    snapshot = resolve_rollout(
        {
            "HEADROOM_FEATURES": "tool_result_interceptors",
            "HEADROOM_DISABLE_FEATURES": "tool_result_interceptors",
            "HEADROOM_UNSAFE_ALLOW_UNSTABLE_FEATURES": "1",
        }
    )
    assert snapshot.decision("tool_result_interceptors").reason is FeatureDecisionReason.DISABLED


def test_registry_and_snapshot_digests_are_deterministic_and_policy_sensitive() -> None:
    first = resolve_rollout({"HEADROOM_ROLLOUT_CHANNEL": "canary"})
    second = resolve_rollout({"HEADROOM_ROLLOUT_CHANNEL": "canary"})
    equivalent = dict(reversed(list(FEATURES.items())))
    changed = dict(FEATURES)
    changed["tool_result_interceptors"] = FeatureSpec(
        "tool_result_interceptors", RolloutChannel.BETA
    )

    assert first.registry_digest == second.registry_digest == registry_digest(equivalent)
    assert first.snapshot_digest == second.snapshot_digest
    assert registry_digest(changed) != first.registry_digest
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
        second.to_dict(), sort_keys=True
    )


def test_pipeline_uses_config_snapshot_after_environment_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HEADROOM_ROLLOUT_CHANNEL", raising=False)
    monkeypatch.delenv("HEADROOM_FEATURES", raising=False)
    config = HeadroomConfig()
    original_digest = config.rollout.snapshot_digest if config.rollout else None
    monkeypatch.setenv("HEADROOM_ROLLOUT_CHANNEL", "canary")
    monkeypatch.setenv("HEADROOM_FEATURES", "tool_result_interceptors")

    pipeline = TransformPipeline(config)

    assert config.rollout is not None
    assert config.rollout.snapshot_digest == original_digest
    assert all(
        type(transform).__name__ != "ToolResultInterceptorTransform"
        for transform in pipeline.transforms
    )


def test_cli_json_status_and_strict_error() -> None:
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "rollout",
            "status",
            "--channel",
            "canary",
            "--features",
            "tool_result_interceptors",
            "--json",
        ],
    )
    invalid = runner.invoke(main, ["rollout", "status", "--features", "typo", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["channel"] == "canary"
    assert payload["features"][2]["name"] == "tool_result_interceptors"
    assert invalid.exit_code != 0
    assert "unknown rollout feature" in invalid.output


def test_shared_python_rust_policy_vectors() -> None:
    vectors = json.loads(
        (Path(__file__).parent / "fixtures" / "rollout_policy_vectors.json").read_text()
    )
    for vector in vectors:
        env = {"HEADROOM_ROLLOUT_CHANNEL": vector["channel"]}
        if vector["requested"]:
            env["HEADROOM_FEATURES"] = "tool_result_interceptors"
        if vector["disabled"]:
            env["HEADROOM_DISABLE_FEATURES"] = "tool_result_interceptors"
        if vector["unsafe"]:
            env["HEADROOM_UNSAFE_ALLOW_UNSTABLE_FEATURES"] = "1"
        decision = resolve_rollout(env).decision("tool_result_interceptors")
        assert decision.enabled is vector["enabled"]
        assert decision.reason.value == vector["decision"]
