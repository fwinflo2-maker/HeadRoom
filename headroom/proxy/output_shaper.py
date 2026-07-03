"""Output token shaping for proxied requests (Anthropic + OpenAI-compatible).

Headroom's transforms compress what goes INTO the model. This module is the
first request-side lever on what comes OUT of it. The proxy never generates
output tokens, so every lever here works by reshaping the request:

1. Verbosity steering — a deterministic instruction block appended to the
   TAIL of the system prompt (after any ``cache_control`` breakpoint, so the
   provider prefix cache is preserved). Five levels, from "no ceremony" to
   full caveman.

2. Effort routing — agentic loops are mostly mechanical continuations (the
   last message is a clean tool_result: a file read, a passing test). Thinking
   bills as output tokens, and harnesses like Claude Code pin
   ``output_config.effort`` at ``xhigh`` for every turn. On turns classified
   as mechanical we lower an explicitly-present effort; on errors or new user
   asks we leave it alone. For legacy models still sending
   ``thinking.budget_tokens`` we clamp the budget to the API floor instead.

Provider dispatch: ``shape_request(body, ..., provider="anthropic"|"openai")``
routes to a provider-specific implementation. The wire shape differs enough
that a single implementation would be fragile:

- Anthropic Messages: ``body["system"]`` is str or list of blocks;
  ``body["messages"][*]["content"]`` uses typed blocks including ``tool_result``
  with ``is_error``; effort lives in ``output_config.effort`` and legacy
  ``thinking.budget_tokens``.
- OpenAI Chat Completions / Responses: system is a message with
  ``role="system"``; tool results are ``role="tool"`` messages;
  reasoning effort lives in top-level ``reasoning_effort`` (Chat) or
  ``reasoning.effort`` (Responses).

The label vocabulary (``output_shaper:*``) is provider-agnostic so the
downstream savings ledger and outcome funnel work identically on either path.

Safety rules (each prevents a concrete failure mode):
- Never INJECT effort where the client didn't send it — models without effort
  support 400 on it. Lowering an existing value is always valid.
- Never toggle ``thinking.type`` — disabling thinking while history carries
  thinking blocks 400s on some models, and the toggle busts the messages
  cache tier.
- Steering text is byte-stable per level and applied idempotently, so
  repeated requests keep an identical prefix.

Turn classification is purely structural (block types, roles, ``is_error``
flags) — no content regexes or keyword patterns.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from headroom.proxy import runtime_env

logger = logging.getLogger(__name__)

# Documented Anthropic API minimum for thinking.budget_tokens on models
# that still accept the legacy enabled/budget_tokens form.
LEGACY_THINKING_FLOOR = 1024

# Ordering for effort values. Unknown values are left alone. This vocabulary
# is used for BOTH Anthropic (output_config.effort) and OpenAI
# (reasoning_effort / reasoning.effort). OpenAI's canonical values are
# {"minimal","low","medium","high"} (Chat Completions / Responses reasoning);
# we accept the Anthropic-ish "xhigh"/"max" as extensions for cross-provider
# uniformity — clamp-only semantics mean an unknown value is never injected
# where the client didn't already send it, so accepting extras is safe.
_EFFORT_RANK = {"minimal": 0, "low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5}

# Sentinel prefix marks the steering block so application is idempotent and
# the block is recognizable in logs/diffs.
_STEERING_SENTINEL = "<headroom_output_shaping>"
_STEERING_SUFFIX = "</headroom_output_shaping>"

# Provider type. Kept narrow — every dispatch site is exhaustive on this.
Provider = Literal["anthropic", "openai"]

# Levels are cumulative: each includes everything above it. Text must stay
# byte-stable across releases for prefix-cache friendliness — treat edits to
# these strings as cache-busting changes.
_VERBOSITY_LEVELS = {
    1: (
        "Skip preamble and postamble. Do not announce what you are about to "
        "do or recap what you just did; start with the substance."
    ),
    2: (
        "Skip preamble and postamble; start with the substance. Never restate "
        "code, file contents, diffs, or tool output that already appear in "
        "this conversation — reference them by path and line instead. After a "
        "tool call succeeds, continue without narrating the result."
    ),
    3: (
        "Skip preamble and postamble. Never restate code, file contents, "
        "diffs, or tool output already in this conversation — reference by "
        "path and line. Give conclusions only; omit rationale unless the user "
        "asks why. Prefer the smallest edit over rewriting whole files. Keep "
        "prose to the minimum needed to be unambiguous."
    ),
    4: (
        "Minimum tokens. Fragments fine. No preamble, no postamble, no "
        "restating context, no rationale. Answer, smallest-possible edits, "
        "nothing else."
    ),
}


class TurnKind(Enum):
    """Structural classification of the latest conversation turn."""

    NEW_USER_ASK = "new_user_ask"
    MECHANICAL_CONTINUATION = "mechanical_continuation"
    ERROR_CONTINUATION = "error_continuation"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OutputShaperSettings:
    """Runtime settings, resolved once per request from the environment.

    Env-driven (like HEADROOM_INTERCEPT_ENABLED) so the proxy picks it up
    without config plumbing through the server. Off by default.
    """

    enabled: bool = False
    verbosity_level: int = 2
    effort_router_enabled: bool = True
    mechanical_effort: str = "low"

    @classmethod
    def from_env(cls) -> OutputShaperSettings:
        enabled = runtime_env.getenv("HEADROOM_OUTPUT_SHAPER", "").lower() in (
            "1",
            "true",
            "yes",
        )
        try:
            level = int(runtime_env.getenv("HEADROOM_VERBOSITY_LEVEL", "2"))
        except ValueError:
            level = 2
        level = max(0, min(4, level))
        router = runtime_env.getenv("HEADROOM_EFFORT_ROUTER", "1").lower() not in (
            "0",
            "false",
            "no",
        )
        mech = runtime_env.getenv("HEADROOM_MECHANICAL_EFFORT", "low")
        if mech not in _EFFORT_RANK:
            mech = "low"
        return cls(
            enabled=enabled,
            verbosity_level=level,
            effort_router_enabled=router,
            mechanical_effort=mech,
        )


def resolve_verbosity_level(settings: OutputShaperSettings) -> tuple[int, str]:
    """Resolve the live verbosity level and its source.

    Precedence:
      1. ``HEADROOM_VERBOSITY_LEVEL`` set explicitly → manual override.
      2. AIMD controller state (when ``HEADROOM_VERBOSITY_AUTOTUNE`` is on).
      3. Learned ``verbosity.json`` from ``learn --verbosity``.
      4. The settings default.

    Returns ``(level, source)``. Kept separate from :func:`shape_request` so the
    body-mutating core stays a pure function of an explicit level.
    """
    if runtime_env.getenv("HEADROOM_VERBOSITY_LEVEL"):
        return settings.verbosity_level, "env"

    try:
        from ..paths import workspace_dir

        ws = workspace_dir()
    except Exception:
        return settings.verbosity_level, "default"

    autotune = runtime_env.getenv("HEADROOM_VERBOSITY_AUTOTUNE", "").lower() in ("1", "true", "yes")
    if autotune:
        ctrl_path = ws / "verbosity_controller.json"
        if ctrl_path.exists():
            try:
                import json as _json

                level = int(
                    _json.loads(ctrl_path.read_text()).get("level", settings.verbosity_level)
                )
                return max(0, min(4, level)), "controller"
            except (OSError, ValueError):
                pass

    prof_path = ws / "verbosity.json"
    if prof_path.exists():
        try:
            import json as _json

            level = int(_json.loads(prof_path.read_text()).get("verbosity_level", -1))
            if 0 <= level <= 4:
                return level, "learned"
        except (OSError, ValueError):
            pass

    return settings.verbosity_level, "default"


@dataclass
class ShapeResult:
    """What the shaper did to a request body."""

    changed: bool = False
    labels: list[str] | None = None

    def __post_init__(self) -> None:
        if self.labels is None:
            self.labels = []


# ---------------------------------------------------------------------------
# Turn classification — provider-specific, common vocabulary
# ---------------------------------------------------------------------------


def _classify_turn_anthropic(messages: list[dict[str, Any]]) -> TurnKind:
    """Classify the latest turn on the Anthropic Messages wire shape.

    - Any text block in the last user message → the user is asking something
      new: full effort.
    - Only tool_result blocks, none flagged ``is_error`` → mechanical
      continuation: the model is resuming after a routine tool call.
    - Any tool_result with ``is_error: true`` → error continuation: the model
      must reason about a failure, keep full effort.
    """
    if not messages:
        return TurnKind.UNKNOWN
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return TurnKind.UNKNOWN

    content = last.get("content")
    if isinstance(content, str):
        return TurnKind.NEW_USER_ASK if content.strip() else TurnKind.UNKNOWN
    if not isinstance(content, list) or not content:
        return TurnKind.UNKNOWN

    saw_tool_result = False
    saw_error = False
    for block in content:
        if not isinstance(block, dict):
            return TurnKind.UNKNOWN
        btype = block.get("type")
        if btype == "tool_result":
            saw_tool_result = True
            if block.get("is_error") is True:
                saw_error = True
        elif btype == "text":
            # Fresh user text alongside (or instead of) tool results means
            # the user interjected — treat as a new ask.
            return TurnKind.NEW_USER_ASK
        elif btype in ("image", "document"):
            return TurnKind.NEW_USER_ASK
        # Unknown block types are ignored rather than guessed at.

    if saw_error:
        return TurnKind.ERROR_CONTINUATION
    if saw_tool_result:
        return TurnKind.MECHANICAL_CONTINUATION
    return TurnKind.UNKNOWN


# Markers OpenAI clients (and OpenAI itself) use to flag a tool call whose
# execution failed. There is no ``is_error`` field in the OpenAI wire shape;
# error signalling is out-of-band (the client puts an error message in the
# tool message content or wraps it in JSON). We treat these prefixes as
# structural, not semantic — same principle as the Anthropic classifier: no
# content regex over arbitrary text, just prefix / key checks.
_OPENAI_TOOL_ERROR_PREFIXES = ("Error:", "error:", "ERROR:", "Traceback")
_OPENAI_TOOL_ERROR_KEYS = ("error", "is_error", "exception")


def _openai_tool_content_is_error(content: Any) -> bool:
    """Structural error check for an OpenAI tool-message content payload.

    OpenAI's spec allows string OR (post-2024) a list of typed content parts.
    Both shapes are handled; unknown shapes fall through to False so we
    default to "mechanical" (the safer classification — we won't drop effort
    on a request that might actually be an error continuation).
    """
    if isinstance(content, str):
        stripped = content.lstrip()
        return any(stripped.startswith(p) for p in _OPENAI_TOOL_ERROR_PREFIXES)
    if isinstance(content, dict):
        # Some clients wrap tool output in a JSON object. Any of these keys
        # set truthy is enough signal.
        return any(bool(content.get(k)) for k in _OPENAI_TOOL_ERROR_KEYS)
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                # OpenAI content parts have a ``type`` and shape-specific
                # fields; the common text case is {"type":"text","text":...}.
                text = part.get("text")
                if isinstance(text, str):
                    stripped = text.lstrip()
                    if any(stripped.startswith(p) for p in _OPENAI_TOOL_ERROR_PREFIXES):
                        return True
                if any(bool(part.get(k)) for k in _OPENAI_TOOL_ERROR_KEYS):
                    return True
    return False


def _classify_turn_openai(messages: list[dict[str, Any]]) -> TurnKind:
    """Classify the latest turn on the OpenAI Chat Completions wire shape.

    Turn semantics under the OpenAI schema:
    - ``role=="user"`` with non-empty content → new user ask.
    - Trailing block of ``role=="tool"`` messages (one per resolved tool_call)
      with no error signal → mechanical continuation.
    - Any of those tool messages carrying an error signal → error continuation.

    Note on the Responses API: it uses ``body["input"]`` instead of
    ``body["messages"]``. For classification we operate on whatever list is
    passed in; the shaper wiring at the call site is responsible for pulling
    the right field.
    """
    if not messages:
        return TurnKind.UNKNOWN
    last = messages[-1]
    if not isinstance(last, dict):
        return TurnKind.UNKNOWN

    role = last.get("role")

    if role == "user":
        content = last.get("content")
        if isinstance(content, str):
            return TurnKind.NEW_USER_ASK if content.strip() else TurnKind.UNKNOWN
        if isinstance(content, list) and content:
            # Any non-empty content list from a user is a new ask (text,
            # image_url, input_image, etc.).
            return TurnKind.NEW_USER_ASK
        return TurnKind.UNKNOWN

    if role == "tool":
        # Walk backwards over the contiguous block of tool messages so a
        # single-error case is still classified as ERROR_CONTINUATION even
        # if it sits alongside successful tool results.
        saw_error = False
        for msg in reversed(messages):
            if not isinstance(msg, dict) or msg.get("role") != "tool":
                break
            if _openai_tool_content_is_error(msg.get("content")):
                saw_error = True
        return TurnKind.ERROR_CONTINUATION if saw_error else TurnKind.MECHANICAL_CONTINUATION

    # role=="assistant" (mid-stream), or "system", or anything else at the
    # tail — unknown, don't shape.
    return TurnKind.UNKNOWN


def classify_turn(
    messages: list[dict[str, Any]],
    provider: Provider = "anthropic",
) -> TurnKind:
    """Classify the latest conversation turn for the given provider.

    Defaults to ``"anthropic"`` so pre-existing callers (``learn/verbosity``,
    the Anthropic handler at ``handlers/anthropic.py:1973``) keep the same
    behaviour without a code change.
    """
    if provider == "openai":
        return _classify_turn_openai(messages)
    return _classify_turn_anthropic(messages)


# ---------------------------------------------------------------------------
# Verbosity steering — provider-specific application
# ---------------------------------------------------------------------------


def steering_text(level: int) -> str | None:
    """The full steering block for a verbosity level, or None for level 0."""
    text = _VERBOSITY_LEVELS.get(level)
    if text is None:
        return None
    return f"{_STEERING_SENTINEL}\n{text}\n{_STEERING_SUFFIX}"


def _apply_verbosity_steering_anthropic(body: dict[str, Any], level: int) -> bool:
    """Append the steering block to the tail of the Anthropic system prompt.

    Appending AFTER the last system block keeps any ``cache_control``
    breakpoint on an earlier block intact — the cached prefix is unchanged
    and only the (small, byte-stable) steering block is reprocessed.

    A string system prompt is converted to block form so the original text
    keeps its exact bytes as the first block.
    """
    text = steering_text(level)
    if text is None:
        return False

    system = body.get("system")
    if system is None:
        body["system"] = [{"type": "text", "text": text}]
        return True
    if isinstance(system, str):
        body["system"] = [
            {"type": "text", "text": system},
            {"type": "text", "text": text},
        ]
        return True
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("text", "").startswith(_STEERING_SENTINEL):
                if block["text"] == text:
                    return False  # already applied at this level
                block["text"] = text  # level changed mid-session
                return True
        system.append({"type": "text", "text": text})
        return True
    return False


def _openai_message_content_is_steering(content: Any) -> bool:
    """True if this content payload already carries our steering block."""
    if isinstance(content, str):
        return _STEERING_SENTINEL in content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and _STEERING_SENTINEL in text:
                    return True
    return False


def _replace_openai_steering_content(content: Any, text: str) -> Any:
    """In-place style replacement of an existing steering payload with ``text``.

    Returns the new content value (string or list of parts). Preserves the
    original shape (string in → string out, list in → list out) so we don't
    accidentally break provider validators that reject mixed shapes.
    """
    if isinstance(content, str):
        return text
    if isinstance(content, list):
        new_parts = []
        replaced = False
        for part in content:
            if (
                not replaced
                and isinstance(part, dict)
                and isinstance(part.get("text"), str)
                and _STEERING_SENTINEL in part["text"]
            ):
                new_part = dict(part)
                new_part["text"] = text
                new_parts.append(new_part)
                replaced = True
            else:
                new_parts.append(part)
        return new_parts if replaced else [*content, {"type": "text", "text": text}]
    # Unknown shape → wrap in a fresh string block.
    return text


def _apply_verbosity_steering_openai(body: dict[str, Any], level: int) -> bool:
    """Append the steering block as a trailing ``system`` message.

    OpenAI Chat Completions treats each system message as a separate item in
    ``body["messages"]``. Two design choices here:

    1. We APPEND a new system message rather than mutate any existing system
       message. Mutating the existing one would bust any provider-side
       prefix cache the client established; appending leaves earlier bytes
       untouched. This mirrors the Anthropic "append after cache_control"
       strategy.

    2. We place the new system message at the TAIL of ``messages`` (right
       before the trailing user/tool turn). Placement is important because
       OpenAI's own guidance is that later system messages override earlier
       ones; the steering block is meant to be the last instruction the
       model sees before generating.

    Idempotency: if a trailing steering block is already present, we either
    leave it alone (same level) or replace its text in place (level changed).
    """
    text = steering_text(level)
    if text is None:
        return False

    messages = body.get("messages")
    if not isinstance(messages, list):
        # Empty / malformed body — insert a fresh messages list with just the
        # steering. This matches the Anthropic behaviour of creating a
        # system field when none exists.
        body["messages"] = [{"role": "system", "content": text}]
        return True

    # Look for an existing steering message anywhere in the list (idempotency
    # + mid-session level change). Scan back-to-front because that's where a
    # previous shape_request call would have placed it.
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "system":
            continue
        if _openai_message_content_is_steering(msg.get("content")):
            existing = msg.get("content")
            # Byte-stable check across string + list shapes.
            if isinstance(existing, str) and existing == text:
                return False
            new_content = _replace_openai_steering_content(existing, text)
            if new_content == existing:
                return False
            msg["content"] = new_content
            return True

    # No steering block yet. Insert one at the tail of the leading system-
    # message block if the conversation opens with system messages; otherwise
    # append to the end. The former keeps the "system messages come first"
    # invariant that many clients rely on when they inspect the outgoing
    # request.
    insert_at = 0
    while insert_at < len(messages):
        msg = messages[insert_at]
        if not isinstance(msg, dict) or msg.get("role") != "system":
            break
        insert_at += 1
    if insert_at == 0:
        # No leading system messages — put the steering FIRST so it precedes
        # the user turn. Placing it at the tail of a user-only conversation
        # would flip the conventional message order.
        messages.insert(0, {"role": "system", "content": text})
    else:
        messages.insert(insert_at, {"role": "system", "content": text})
    return True


def apply_verbosity_steering(
    body: dict[str, Any],
    level: int,
    provider: Provider = "anthropic",
) -> bool:
    """Apply the verbosity steering block. Default provider is Anthropic for
    back-compat with any external caller."""
    if provider == "openai":
        return _apply_verbosity_steering_openai(body, level)
    return _apply_verbosity_steering_anthropic(body, level)


# ---------------------------------------------------------------------------
# Effort routing — provider-specific application
# ---------------------------------------------------------------------------


def _route_effort_anthropic(
    body: dict[str, Any],
    kind: TurnKind,
    settings: OutputShaperSettings,
) -> list[str]:
    """Lower thinking/effort spend on mechanical continuations (Anthropic).

    Returns labels for each mutation made (empty list = untouched).
    """
    if kind is not TurnKind.MECHANICAL_CONTINUATION:
        return []

    labels: list[str] = []

    # Modern lever: output_config.effort. Only lower a value the client
    # explicitly sent — presence proves the target model accepts the param.
    output_config = body.get("output_config")
    if isinstance(output_config, dict):
        effort = output_config.get("effort")
        if (
            isinstance(effort, str)
            and effort in _EFFORT_RANK
            and settings.mechanical_effort in _EFFORT_RANK
            and _EFFORT_RANK[effort] > _EFFORT_RANK[settings.mechanical_effort]
        ):
            output_config["effort"] = settings.mechanical_effort
            labels.append(f"output_shaper:effort:{effort}->{settings.mechanical_effort}")

    # Legacy lever: clamp thinking.budget_tokens on models still using the
    # enabled/budget_tokens form. The type field itself is never touched.
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        budget = thinking.get("budget_tokens")
        if isinstance(budget, int) and budget > LEGACY_THINKING_FLOOR:
            thinking["budget_tokens"] = LEGACY_THINKING_FLOOR
            labels.append(f"output_shaper:thinking_budget:{budget}->{LEGACY_THINKING_FLOOR}")

    return labels


def _route_effort_openai(
    body: dict[str, Any],
    kind: TurnKind,
    settings: OutputShaperSettings,
) -> list[str]:
    """Lower reasoning effort on mechanical continuations (OpenAI-compatible).

    Handles both wire shapes:

    - Chat Completions: top-level ``body["reasoning_effort"]`` — a string
      one of {"minimal","low","medium","high"} for o-series / gpt-5-class
      reasoning models. Ignored by non-reasoning models. Present on some
      responses too, but the request-side field is what we mutate.

    - Responses API: nested ``body["reasoning"]["effort"]`` with the same
      value set. The Responses shape uses a reasoning object with additional
      fields (``summary`` etc.); we mutate only ``effort``.

    Returns labels for each mutation made (empty list = untouched).
    Same clamp-only invariant as the Anthropic path: we never INJECT effort
    the client didn't send, only lower an explicitly-present value.
    """
    if kind is not TurnKind.MECHANICAL_CONTINUATION:
        return []

    labels: list[str] = []
    target = settings.mechanical_effort
    if target not in _EFFORT_RANK:
        return labels

    # Chat Completions lever.
    effort = body.get("reasoning_effort")
    if (
        isinstance(effort, str)
        and effort in _EFFORT_RANK
        and _EFFORT_RANK[effort] > _EFFORT_RANK[target]
    ):
        body["reasoning_effort"] = target
        labels.append(f"output_shaper:effort:{effort}->{target}")

    # Responses API lever.
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        r_effort = reasoning.get("effort")
        if (
            isinstance(r_effort, str)
            and r_effort in _EFFORT_RANK
            and _EFFORT_RANK[r_effort] > _EFFORT_RANK[target]
        ):
            reasoning["effort"] = target
            labels.append(f"output_shaper:effort:{r_effort}->{target}")

    return labels


def route_effort(
    body: dict[str, Any],
    kind: TurnKind,
    settings: OutputShaperSettings,
    provider: Provider = "anthropic",
) -> list[str]:
    """Route effort down on mechanical continuations. Provider-dispatched."""
    if provider == "openai":
        return _route_effort_openai(body, kind, settings)
    return _route_effort_anthropic(body, kind, settings)


# ---------------------------------------------------------------------------
# End-to-end shaping entry point
# ---------------------------------------------------------------------------


def shape_request(
    body: dict[str, Any],
    settings: OutputShaperSettings | None = None,
    level_override: int | None = None,
    provider: Provider = "anthropic",
) -> ShapeResult:
    """Apply all output-shaping levers to a request body in place.

    ``level_override`` supersedes ``settings.verbosity_level`` when given — the
    handler passes the level resolved by :func:`resolve_verbosity_level` (learned
    profile / controller / env) so the body-mutating core stays level-agnostic.

    ``provider`` selects the wire shape:

    - ``"anthropic"`` (default): mutates ``body["system"]``, classifies from
      ``body["messages"]`` content blocks with ``tool_result``/``is_error``,
      clamps ``output_config.effort`` and ``thinking.budget_tokens``.
    - ``"openai"``: inserts a trailing system message in ``body["messages"]``,
      classifies from OpenAI-shape ``role=="tool"`` messages, clamps
      ``reasoning_effort`` and ``reasoning.effort``.

    The emitted label vocabulary is identical across providers so the
    savings ledger and outcome funnel remain provider-agnostic.
    """
    if settings is None:
        settings = OutputShaperSettings.from_env()
    result = ShapeResult()
    if not settings.enabled:
        return result

    assert result.labels is not None  # __post_init__ guarantees this

    level = settings.verbosity_level if level_override is None else level_override
    if level > 0 and apply_verbosity_steering(body, level, provider=provider):
        result.changed = True
        result.labels.append(f"output_shaper:verbosity:L{level}")

    if settings.effort_router_enabled:
        kind = classify_turn(body.get("messages", []), provider=provider)
        labels = route_effort(body, kind, settings, provider=provider)
        if labels:
            result.changed = True
            result.labels.extend(labels)
        logger.debug(
            "OutputShaper: provider=%s turn=%s mutations=%s",
            provider,
            kind.value,
            labels,
        )

    return result
