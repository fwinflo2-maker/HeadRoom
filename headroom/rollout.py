"""Deterministic runtime rollout policy and provenance.

Rollout channels control behavior exposed by an already-installed artifact.
They do not select a Headroom release, package, or distribution version.
Environment access is confined to :func:`resolve_rollout`; downstream code
receives the resulting immutable :class:`RolloutSnapshot`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

ROLLOUT_SCHEMA_VERSION = 1
ROLLOUT_POLICY_VERSION = "1"

_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


class RolloutConfigurationError(ValueError):
    """A supplied rollout configuration is invalid."""


class RolloutChannel(str, Enum):
    """Ordered runtime-behavior channels; unrelated to artifact releases."""

    STABLE = "stable"
    BETA = "beta"
    CANARY = "canary"
    DEV = "dev"

    @classmethod
    def parse(cls, value: str | None, *, strict: bool = False) -> RolloutChannel:
        if not value:
            return cls.STABLE
        normalized = value.strip().lower().replace("-", "_")
        aliases = {
            "prod": cls.STABLE,
            "production": cls.STABLE,
            "preview": cls.BETA,
            "nightly": cls.CANARY,
            "development": cls.DEV,
        }
        if normalized in aliases:
            return aliases[normalized]
        try:
            return cls(normalized)
        except ValueError:
            message = f"unknown rollout channel {value!r}"
            if strict:
                raise RolloutConfigurationError(message) from None
            logger.warning("%s; falling back to 'stable'", message)
            return cls.STABLE

    @property
    def order(self) -> int:
        return {
            RolloutChannel.STABLE: 0,
            RolloutChannel.BETA: 1,
            RolloutChannel.CANARY: 2,
            RolloutChannel.DEV: 3,
        }[self]

    def allows(self, required: RolloutChannel) -> bool:
        return self.order >= required.order


class FeatureDecisionReason(str, Enum):
    DEFAULT = "default"
    EXPLICIT = "explicit"
    LEGACY_ALIAS = "legacy_alias"
    DISABLED = "disabled"
    BLOCKED_BY_CHANNEL = "blocked_by_channel"
    UNSAFE_OVERRIDE = "unsafe_override"
    NOT_REQUESTED = "not_requested"


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    available_in: RolloutChannel
    default_enabled_in: RolloutChannel | None = None
    legacy_env: tuple[str, ...] = ()
    description: str = ""

    def default_enabled(self, channel: RolloutChannel) -> bool:
        return self.default_enabled_in is not None and channel.allows(self.default_enabled_in)


FEATURES: dict[str, FeatureSpec] = {
    "tool_result_interceptors": FeatureSpec(
        name="tool_result_interceptors",
        available_in=RolloutChannel.CANARY,
        legacy_env=("HEADROOM_INTERCEPT_ENABLED",),
        description="AST-aware Read/tool-result interceptors used before compression.",
    ),
    "proxy_output_shaper": FeatureSpec(
        name="proxy_output_shaper",
        available_in=RolloutChannel.BETA,
        legacy_env=("HEADROOM_OUTPUT_SHAPER",),
        description="Proxy output-shaping path for response-side experiments.",
    ),
    "read_maturation": FeatureSpec(
        name="read_maturation",
        available_in=RolloutChannel.BETA,
        legacy_env=("HEADROOM_READ_MATURATION",),
        description="Hold-back Read maturation before provider cache entry.",
    ),
}


def _split_names(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {
        part.strip().lower().replace("-", "_")
        for part in raw.replace(";", ",").split(",")
        if part.strip()
    }


def _truthy(value: str | None) -> bool:
    return bool(value and value.strip().lower() in _TRUE_VALUES)


def _falsey(value: str | None) -> bool:
    return bool(value and value.strip().lower() in _FALSE_VALUES)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def registry_digest(registry: Mapping[str, FeatureSpec] = FEATURES) -> str:
    """Return a stable identity for all behavior-affecting registry fields."""

    canonical = [
        {
            "name": spec.name,
            "available_in": spec.available_in.value,
            "default_enabled_in": (
                spec.default_enabled_in.value if spec.default_enabled_in is not None else None
            ),
            "legacy_env": sorted(spec.legacy_env),
        }
        for _, spec in sorted(registry.items())
    ]
    return _sha256(canonical)


@dataclass(frozen=True)
class RolloutConfig:
    channel: RolloutChannel
    requested: frozenset[str]
    disabled: frozenset[str]
    unsafe_allow_unstable: bool


@dataclass(frozen=True)
class FeatureDecision:
    name: str
    available_in: RolloutChannel
    default_enabled_in: RolloutChannel | None
    requested: bool
    disabled: bool
    enabled: bool
    reason: FeatureDecisionReason

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "available_in": self.available_in.value,
            "default_enabled_in": (
                self.default_enabled_in.value if self.default_enabled_in is not None else None
            ),
            "requested": self.requested,
            "disabled": self.disabled,
            "enabled": self.enabled,
            "decision": self.reason.value,
        }


@dataclass(frozen=True)
class RolloutSnapshot:
    schema_version: int
    policy_version: str
    registry_digest: str
    config: RolloutConfig
    decisions: tuple[FeatureDecision, ...]

    @property
    def channel(self) -> RolloutChannel:
        return self.config.channel

    @property
    def unsafe_allow_unstable(self) -> bool:
        return self.config.unsafe_allow_unstable

    @property
    def qualification_eligible(self) -> bool:
        return not self.unsafe_allow_unstable

    @property
    def snapshot_digest(self) -> str:
        return _sha256(self._canonical_dict())

    def decision(self, feature: str) -> FeatureDecision:
        normalized = feature.strip().lower().replace("-", "_")
        for decision in self.decisions:
            if decision.name == normalized:
                return decision
        raise KeyError(feature)

    def is_available(self, feature: str) -> bool:
        decision = self.decision(feature)
        return self.channel.allows(decision.available_in) or self.unsafe_allow_unstable

    def is_enabled(self, feature: str, **_: object) -> bool:
        """Return the pre-resolved decision; extra legacy kwargs are ignored."""

        return self.decision(feature).enabled

    @property
    def enabled(self) -> frozenset[str]:
        return frozenset(item.name for item in self.decisions if item.enabled)

    @property
    def disabled(self) -> frozenset[str]:
        return self.config.disabled

    def _canonical_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "channel": self.channel.value,
            "unsafe_override": self.unsafe_allow_unstable,
            "registry_digest": self.registry_digest,
            "features": [item.to_dict() for item in self.decisions],
        }

    def to_dict(self) -> dict[str, object]:
        result = self._canonical_dict()
        result["snapshot_digest"] = self.snapshot_digest
        result["qualification_eligible"] = self.qualification_eligible
        if not self.qualification_eligible:
            result["qualification_ineligible_reason"] = "unsafe_rollout_override_active"
        return result


def _validate_names(names: set[str], *, source: str, strict: bool) -> frozenset[str]:
    unknown = sorted(names - FEATURES.keys())
    if unknown:
        valid = ", ".join(sorted(FEATURES))
        message = f"unknown rollout feature(s) in {source}: {', '.join(unknown)}; valid: {valid}"
        if strict:
            raise RolloutConfigurationError(message)
        logger.warning("%s; ignoring unknown names (fail-closed)", message)
    return frozenset(names & FEATURES.keys())


def resolve_rollout(
    environ: Mapping[str, str] | None = None,
    *,
    requested: Iterable[str] = (),
    disabled: Iterable[str] = (),
    strict: bool = False,
) -> RolloutSnapshot:
    """Resolve all rollout inputs exactly once into an immutable snapshot."""

    env = os.environ if environ is None else environ
    channel = RolloutChannel.parse(env.get("HEADROOM_ROLLOUT_CHANNEL"), strict=strict)
    requested_names = _split_names(env.get("HEADROOM_FEATURES")) | {
        name.strip().lower().replace("-", "_") for name in requested
    }
    disabled_names = _split_names(env.get("HEADROOM_DISABLE_FEATURES")) | {
        name.strip().lower().replace("-", "_") for name in disabled
    }
    requested_names = set(
        _validate_names(requested_names, source="requested features", strict=strict)
    )
    disabled_names = set(_validate_names(disabled_names, source="disabled features", strict=strict))
    legacy_requested: set[str] = set()
    legacy_disabled: set[str] = set()
    for spec in FEATURES.values():
        for alias in spec.legacy_env:
            if _truthy(env.get(alias)):
                legacy_requested.add(spec.name)
            elif _falsey(env.get(alias)):
                legacy_disabled.add(spec.name)

    disabled_names.update(legacy_disabled)
    unsafe = _truthy(env.get("HEADROOM_UNSAFE_ALLOW_UNSTABLE_FEATURES"))
    config = RolloutConfig(
        channel=channel,
        requested=frozenset(requested_names | legacy_requested),
        disabled=frozenset(disabled_names),
        unsafe_allow_unstable=unsafe,
    )
    decisions: list[FeatureDecision] = []
    for name, spec in sorted(FEATURES.items()):
        is_requested = name in config.requested
        is_disabled = name in config.disabled
        normally_available = channel.allows(spec.available_in)
        if is_disabled:
            enabled, reason = False, FeatureDecisionReason.DISABLED
        elif is_requested and not normally_available and not unsafe:
            enabled, reason = False, FeatureDecisionReason.BLOCKED_BY_CHANNEL
        elif is_requested and not normally_available and unsafe:
            enabled, reason = True, FeatureDecisionReason.UNSAFE_OVERRIDE
        elif name in legacy_requested:
            enabled, reason = True, FeatureDecisionReason.LEGACY_ALIAS
        elif name in requested_names:
            enabled, reason = True, FeatureDecisionReason.EXPLICIT
        elif spec.default_enabled(channel):
            enabled, reason = True, FeatureDecisionReason.DEFAULT
        else:
            enabled, reason = False, FeatureDecisionReason.NOT_REQUESTED
        decisions.append(
            FeatureDecision(
                name=name,
                available_in=spec.available_in,
                default_enabled_in=spec.default_enabled_in,
                requested=is_requested,
                disabled=is_disabled,
                enabled=enabled,
                reason=reason,
            )
        )
    return RolloutSnapshot(
        schema_version=ROLLOUT_SCHEMA_VERSION,
        policy_version=ROLLOUT_POLICY_VERSION,
        registry_digest=registry_digest(),
        config=config,
        decisions=tuple(decisions),
    )


def current_rollout(environ: Mapping[str, str] | None = None) -> RolloutSnapshot:
    """Compatibility name for resolving a snapshot at a composition boundary."""

    return resolve_rollout(environ)


def feature_enabled(
    feature: str,
    *,
    explicit: bool = False,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Compatibility helper for composition roots; do not use in deep components."""

    requested = (feature,) if explicit else ()
    return resolve_rollout(environ, requested=requested).is_enabled(feature)


# The PR was never released, but this narrow source alias keeps in-branch callers
# importable while the correction migrates them. It is intentionally undocumented.
Rollout = RolloutSnapshot
