"""Pure output-savings stratification and holdout policy helpers."""

from __future__ import annotations

import hashlib
from typing import Any, cast

# Coarse input-token buckets. Coarse on purpose: too many strata make
# per-stratum baselines sparse and noisy. Boundaries in tokens.
_INPUT_BUCKETS = (2_000, 8_000, 32_000, 128_000)

_STRATUM_LABEL = "output_shaper:stratum:"
_CONTROL_LABEL = "output_shaper:control:"

# Optional ``:L<n>`` tail on a stratum label carrying the verbosity level the
# shaper resolved for that request. A stratum key is built from a fixed
# vocabulary joined with "|" (see :func:`stratum_key`) and can never contain a
# colon, so the tail is unambiguous: the text after the last colon is the tag
# if and only if it looks like ``L<digits>``.
_LEVEL_TAG_PREFIX = "L"


def input_bucket(input_tokens: int) -> str:
    """Map an input-token count to a coarse bucket label."""
    if input_tokens < _INPUT_BUCKETS[0]:
        return "xs"
    if input_tokens < _INPUT_BUCKETS[1]:
        return "s"
    if input_tokens < _INPUT_BUCKETS[2]:
        return "m"
    if input_tokens < _INPUT_BUCKETS[3]:
        return "l"
    return "xl"


def model_family(model: str) -> str:
    """Collapse a model id to a coarse family for stratification.

    Token-spend behaviour clusters by family far more than by point release,
    so we bucket (e.g.) every ``claude-opus-*`` together.
    """
    m = model.lower()
    for fam in ("opus", "sonnet", "haiku", "fable", "mythos", "gpt", "gemini"):
        if fam in m:
            return fam
    return "other"


def stratum_key(
    *,
    turn_kind: str,
    input_tokens: int,
    model: str,
    has_tools: bool,
) -> str:
    """Build a stratum key from request features observable before the response.

    Order is most-to-least specific so baseline lookup can back off by trimming
    trailing fields.
    """
    return "|".join(
        (
            model_family(model),
            turn_kind,
            input_bucket(input_tokens),
            "tools" if has_tools else "notools",
        )
    )


def _unwrap_response_create_body(body: dict[str, Any]) -> dict[str, Any]:
    response = body.get("response")
    if body.get("type") == "response.create" and isinstance(response, dict):
        return cast("dict[str, Any]", response)
    return body


def _stable_response_identifier(body: dict[str, Any]) -> str:
    def _string_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("id", "conversation_id", "session_id", "thread_id"):
                nested = value.get(key)
                if isinstance(nested, str) and nested:
                    return nested
        return ""

    for key in ("conversation", "conversation_id", "session_id", "thread_id"):
        value = _string_value(body.get(key))
        if value and value.lower() != "auto":
            return f"{key}:{value}"

    for container_key in ("client_metadata", "metadata"):
        container = body.get(container_key)
        if not isinstance(container, dict):
            continue
        for key in (
            "conversation_id",
            "conversation_key",
            "session_id",
            "thread_id",
            "codex_session_id",
        ):
            value = _string_value(container.get(key))
            if value and value.lower() != "auto":
                return f"{container_key}.{key}:{value}"

    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        return f"instructions:{instructions[:512]}"
    return ""


def conversation_key_from_body(body: dict[str, Any]) -> str:
    """Derive a conversation-stable key for holdout assignment."""
    body = _unwrap_response_create_body(body)
    model = str(body.get("model", ""))
    seed = model
    for msg in body.get("messages", []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                seed += "\x00" + content[:512]
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        seed += "\x00" + str(block.get("text", ""))[:512]
                        break
            break
    if "input" in body:
        stable_response_key = _stable_response_identifier(body)
        if stable_response_key:
            seed += "\x00" + stable_response_key
        elif not body.get("messages"):
            seed += "\x00responses"
    return hashlib.sha256(seed.encode("utf-8", "ignore")).hexdigest()


def conversation_key_from_responses_body(body: dict[str, Any]) -> str:
    """Conversation-stable key for an OpenAI Responses payload."""
    body = _unwrap_response_create_body(body)
    model = str(body.get("model", ""))
    seed = model
    input_data = body.get("input")
    if isinstance(input_data, str):
        seed += "\x00" + input_data[:512]
    elif isinstance(input_data, list):
        for item in input_data:
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            content = item.get("content")
            if isinstance(content, str):
                seed += "\x00" + content[:512]
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        seed += "\x00" + part["text"][:512]
                        break
            break
    return hashlib.sha256(seed.encode("utf-8", "ignore")).hexdigest()


def assign_arm(conversation_key: str, holdout_fraction: float) -> str:
    """Deterministically assign a conversation to ``treatment`` or ``control``."""
    if holdout_fraction <= 0.0:
        return "treatment"
    if holdout_fraction >= 1.0:
        return "control"
    digest = hashlib.sha256(("arm:" + conversation_key).encode()).hexdigest()
    frac = int(digest[:8], 16) / 0xFFFFFFFF
    return "control" if frac < holdout_fraction else "treatment"


def stratum_label(arm: str, key: str, *, verbosity_level: int | None = None) -> str:
    """Encode (arm, stratum, shaper level) as a transforms_applied label.

    ``verbosity_level`` is the level :func:`output_shaper.resolve_verbosity_level`
    returned for THIS request, and it is what tells the ledger whether the
    shaper was actually live when the observation was recorded. The stratum
    label is attached whenever the shaper is *configured* on, but shaping can
    still be inert — level 0 means no steering at all (cache mode forces it, and
    a learned or env level of 0 selects it), so both arms fill with unshaped
    traffic that a naive estimator reads as a shaping effect.

    The level rides as a ``:L<n>`` tail rather than a separate label so nothing
    downstream sees a new transform (``telemetry.session`` counts one entry per
    label, keyed on the text before the first colon) and existing
    ``startswith(prefix + key)`` matches keep working.

    Omitting ``verbosity_level`` produces the historical untagged label, which
    the ledger records as "shaper inactive". That direction is deliberate:
    activity we cannot prove must never be able to inflate a savings claim.
    """
    prefix = _STRATUM_LABEL if arm == "treatment" else _CONTROL_LABEL
    if verbosity_level is None:
        return prefix + key
    return f"{prefix}{key}:{_LEVEL_TAG_PREFIX}{int(verbosity_level)}"


def parse_stratum_label(label: str) -> tuple[str, str, int | None] | None:
    """Decode a label into ``(arm, stratum, verbosity_level)``, or None.

    ``verbosity_level`` is ``None`` for an untagged label — one written before
    the level tag existed, or by a caller that did not supply it.
    """
    if label.startswith(_STRATUM_LABEL):
        arm, rest = "treatment", label[len(_STRATUM_LABEL) :]
    elif label.startswith(_CONTROL_LABEL):
        arm, rest = "control", label[len(_CONTROL_LABEL) :]
    else:
        return None
    key, sep, tail = rest.rpartition(":")
    if sep and tail.startswith(_LEVEL_TAG_PREFIX):
        digits = tail[len(_LEVEL_TAG_PREFIX) :]
        if digits.isdigit():
            return arm, key, int(digits)
    return arm, rest, None
