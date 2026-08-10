"""Route VS Code Copilot **Chat** through Headroom via the Custom Endpoint provider.

Why this exists alongside ``vscode.py``
---------------------------------------
``vscode.py`` writes ``github.copilot.advanced.debug.overrideProxyUrl``. That knob
targets Copilot's **completions** endpoint; chat talks to the CAPI endpoint, whose
knob is ``overrideCapiUrl`` -- so the existing wrapper never actually redirected
chat traffic. Both are undocumented debug settings, and the Chat extension has
been observed ignoring them outright
(microsoft/vscode-copilot-release#7802, closed unfixed, repo archived).

VS Code ships a supported alternative: the **Custom Endpoint** BYOK provider
(``vendor: "customendpoint"``), configured through ``chatLanguageModels.json``.
It speaks ``chat-completions``, ``responses`` and ``messages`` -- all three of
which the Headroom proxy already serves -- so the proxy can simply *be* the
endpoint, with no protocol work.

What this buys, and what it does not
------------------------------------
Chat and agent traffic flows through Headroom and is compressed, and **every**
model the account is entitled to appears in the picker, so a user can switch
models mid-session exactly as they do natively.

It does **not** cover inline (ghost-text) completions, semantic search, or
embeddings: VS Code routes those through GitHub regardless of BYOK. Callers must
say so rather than implying full coverage.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import click

from headroom import fsutil
from headroom.providers.copilot.vscode import (
    _read_settings,
    _validate_settings,
    vscode_user_dir,
)
from headroom.proxy.project_context import with_project_prefix

#: Provider display name. Also how a human recognises the block in the file.
HEADROOM_PROVIDER_NAME = "Headroom (GitHub Copilot)"
#: VS Code's provider id for a user-supplied HTTP endpoint.
CUSTOM_ENDPOINT_VENDOR = "customendpoint"
#: 1.132+ hides Custom Endpoint models unless this is on. Defaults to false, is
#: experiment-controlled, and is not exposed in the Settings UI, so a user who
#: upgrades silently loses every configured model (microsoft/vscode#329545).
BYOK_ENABLED_SETTING = "chat.agentHost.byokModels.enabled"

#: Endpoint suffix per API shape. VS Code resolves a model's URL from its
#: ``apiType`` when the path is omitted; we always write it explicitly so the
#: mapping is visible in the file rather than inferred.
_API_TYPE_PATHS = {
    "chat-completions": "/v1/chat/completions",
    "responses": "/v1/responses",
    "messages": "/v1/messages",
}


def chat_models_path(
    *, platform: str | None = None, environ: Mapping[str, str] | None = None
) -> Path:
    """Location of VS Code's ``chatLanguageModels.json`` for the default profile."""
    return vscode_user_dir(platform=platform, environ=environ) / "chatLanguageModels.json"


def byok_entitlement_enabled(token: str) -> bool | None:
    """Whether this seat may use BYOK at all, from the Copilot token's claims.

    VS Code gates the entire Custom Endpoint feature on the ``client_byok``
    entitlement, and a Business/Enterprise admin can disable it org-wide. When it
    is off, configuration written here would simply never appear in the picker --
    so it is worth failing loudly at launch instead of leaving the user hunting
    for models that cannot exist.

    Returns ``None`` when the claim is absent (older token shapes): unknown, so
    the caller should proceed with a note rather than refuse.
    """
    body = token.split(":", 1)[0]
    claims = dict(pair.split("=", 1) for pair in body.split(";") if "=" in pair)
    raw = claims.get("client_byok")
    if raw is None:
        return None
    return raw.strip() == "1"


def _api_type_for(endpoints: list[str] | tuple[str, ...]) -> str:
    """Pick the wire VS Code should use for a model.

    Prefers ``chat-completions``: it is the shape verified working end-to-end
    through the proxy with the placeholder key VS Code sends, and Copilot serves
    most families on it. ``responses`` is used for models served only there
    (``gpt-5.6-*``, ``mai-code-1-flash-picker``).

    ``messages`` is deliberately never chosen. The proxy's Anthropic path does not
    substitute the Copilot token, so VS Code's placeholder ``x-api-key`` reaches
    GitHub and is rejected -- and Claude models are reachable on
    ``chat-completions`` anyway, so nothing is lost by avoiding it.
    """
    if not endpoints:
        return "chat-completions"
    if "/chat/completions" in endpoints:
        return "chat-completions"
    if "/responses" in endpoints:
        return "responses"
    return "chat-completions"


def build_model_entries(payload: Any, base_url: str) -> list[dict[str, Any]]:
    """Translate a Copilot ``/models`` payload into VS Code model entries.

    Capabilities are read from the **raw payload**, not from
    ``headroom.models.copilot_catalog.ModelCard`` -- that dataclass carries no
    tool-calling or vision fields, and it belongs to a separate change that must
    not be extended from here.

    ``toolCalling`` matters more than it looks: VS Code hides a model from agent
    mode entirely when it is false, so hardcoding ``true`` would surface models
    that then fail, and hardcoding ``false`` would silently remove good ones.
    """
    entries: list[dict[str, Any]] = []
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return entries

    for model in data:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        capabilities = model.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        if capabilities.get("type") != "chat":
            continue  # embeddings and similar must never reach the chat picker
        policy = model.get("policy")
        policy_ok = not isinstance(policy, dict) or policy.get("state") in (None, "enabled")
        if not (model.get("model_picker_enabled") and policy_ok):
            continue

        supports = capabilities.get("supports")
        supports = supports if isinstance(supports, dict) else {}
        limits = capabilities.get("limits")
        limits = limits if isinstance(limits, dict) else {}

        api_type = _api_type_for(model.get("supported_endpoints") or [])
        display = model.get("name") if isinstance(model.get("name"), str) else model_id
        entry: dict[str, Any] = {
            "id": model_id,
            "name": f"{display} (Headroom)",
            "url": f"{base_url.rstrip('/')}{_API_TYPE_PATHS[api_type]}",
            "apiType": api_type,
            "toolCalling": bool(supports.get("tool_calls")),
            "vision": bool(supports.get("vision")),
        }
        max_input = limits.get("max_prompt_tokens") or limits.get("max_context_window_tokens")
        if isinstance(max_input, int) and max_input > 0:
            entry["maxInputTokens"] = max_input
        max_output = limits.get("max_output_tokens")
        if isinstance(max_output, int) and max_output > 0:
            entry["maxOutputTokens"] = max_output
        entries.append(entry)

    entries.sort(key=lambda e: e["name"].lower())
    return entries


def build_provider_block(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """The single provider object Headroom owns in ``chatLanguageModels.json``.

    ``apiKey`` is an ``${input:...}`` variable rather than a literal. The proxy
    substitutes the real Copilot credential itself, so the value is never used
    upstream -- but VS Code always sends *something*, and an input variable keeps
    a placeholder out of a file users may share.
    """
    return {
        "name": HEADROOM_PROVIDER_NAME,
        "vendor": CUSTOM_ENDPOINT_VENDOR,
        "apiKey": "${input:headroomProxyKey}",
        "models": entries,
    }


def _provenance_path(path: Path) -> Path:
    from headroom import paths

    digest = hashlib.sha256(str(path.resolve()).encode("utf-8", "replace")).hexdigest()[:16]
    return paths.workspace_dir() / "vscode_chat_models" / f"{digest}.json"


def _record_provenance(path: Path, block: dict[str, Any]) -> None:
    """Remember the exact block written, so a later run can prove ownership.

    Ownership is deliberately **not** inferred from the file's contents. A user
    may legitimately hand-edit or copy our provider entry, and a name match alone
    would then license overwriting their edits. Recording a digest out of band
    means only bytes Headroom actually wrote are ever replaced.
    """
    try:
        record = _provenance_path(path)
        record.parent.mkdir(parents=True, exist_ok=True)
        fsutil.write_text(
            record,
            json.dumps({"path": str(path), "sha256": _block_digest(block)}),
        )
    except OSError:
        # Losing the record only means the next run appends instead of replacing.
        pass


def _read_provenance(path: Path) -> str | None:
    try:
        rec = json.loads(fsutil.read_text(_provenance_path(path)))
    except (OSError, ValueError):
        return None
    sha = rec.get("sha256") if isinstance(rec, dict) else None
    return sha if isinstance(sha, str) and sha else None


def _clear_provenance(path: Path) -> None:
    try:
        _provenance_path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _block_digest(block: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(block, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _load_providers(path: Path) -> list[Any]:
    """Parse the existing provider list, or return ``[]`` for a fresh file."""
    if not path.exists():
        return []
    try:
        raw = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise click.ClickException(
            f"Could not read {path} as UTF-8 ({exc}); refusing to rewrite it."
        ) from exc
    if not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            f"Could not parse {path}: {exc}. Headroom did not overwrite it."
        ) from exc
    if not isinstance(parsed, list):
        raise click.ClickException(
            f"{path} must contain a JSON array of providers; refusing to overwrite it."
        )
    return parsed


def configure_chat_models(path: Path, block: dict[str, Any]) -> str:
    """Write/refresh Headroom's provider entry, preserving every other provider.

    Returns ``"added"`` or ``"updated"``. Raises rather than clobbering a
    same-named entry Headroom cannot prove it wrote.
    """
    providers = _load_providers(path)
    expected = _read_provenance(path)

    owned_indexes = [
        index
        for index, entry in enumerate(providers)
        if isinstance(entry, dict) and expected and _block_digest(entry) == expected
    ]
    if len(owned_indexes) > 1:
        raise click.ClickException(
            f"{path} contains multiple identical Headroom provider entries; "
            "remove the duplicate so Headroom can tell which to update."
        )

    if owned_indexes:
        providers[owned_indexes[0]] = block
        action = "updated"
    else:
        conflicting = [
            entry
            for entry in providers
            if isinstance(entry, dict) and entry.get("name") == HEADROOM_PROVIDER_NAME
        ]
        if conflicting:
            raise click.ClickException(
                f"{path} already has a provider named {HEADROOM_PROVIDER_NAME!r} that "
                "Headroom did not write; refusing to replace it. Rename or remove it, "
                "or pass --no-configure."
            )
        providers.append(block)
        action = "added"

    path.parent.mkdir(parents=True, exist_ok=True)
    fsutil.write_text(path, json.dumps(providers, indent=2, ensure_ascii=False) + "\n")
    _record_provenance(path, block)
    return action


def remove_chat_models(path: Path) -> bool:
    """Drop Headroom's provider entry, leaving every other provider untouched."""
    if not path.exists():
        return False
    try:
        providers = _load_providers(path)
    except click.ClickException:
        return False
    expected = _read_provenance(path)
    if not expected:
        return False
    remaining = [
        entry
        for entry in providers
        if not (isinstance(entry, dict) and _block_digest(entry) == expected)
    ]
    if len(remaining) == len(providers):
        return False
    fsutil.write_text(path, json.dumps(remaining, indent=2, ensure_ascii=False) + "\n")
    _clear_provenance(path)
    return True


_BYOK_MARKER_START = "// --- Headroom VS Code chat models ---"
_BYOK_MARKER_END = "// --- end Headroom VS Code chat models ---"


def enable_byok_setting(path: Path) -> str:
    """Turn on ``chat.agentHost.byokModels.enabled`` in ``settings.json``.

    VS Code 1.132 hides Custom Endpoint models without it, and the setting is not
    surfaced in the Settings UI -- so a user who upgrades sees their models vanish
    with no explanation. Writing it is necessary but not sufficient: the agent
    host process must restart before the models reappear.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _read_settings(path) if path.exists() else "{}\n"
    _validate_settings(raw, path)

    if _BYOK_MARKER_START in raw:
        return "already set"
    if BYOK_ENABLED_SETTING in raw:
        # The user already configured it themselves; leave their value alone.
        return "already set by user"

    close = raw.rfind("}")
    if close < 0:
        raise click.ClickException(f"Could not locate the root object in {path}.")
    before = raw[:close].rstrip()
    after = raw[close:]
    from headroom.providers.copilot.vscode import _strip_jsonc_comments

    inner = _strip_jsonc_comments(before).rstrip()
    separator = "" if inner.endswith("{") or inner.endswith(",") else ","
    line_sep = "\r\n" if "\r\n" in raw else "\n"
    block = (
        f"\t{_BYOK_MARKER_START}{line_sep}"
        f"\t{json.dumps(BYOK_ENABLED_SETTING)}: true{line_sep}"
        f"\t{_BYOK_MARKER_END}"
    )
    updated = before + separator + line_sep + block + line_sep + after
    _validate_settings(updated, path)
    fsutil.write_text(path, updated)
    return "added"


def disable_byok_setting(path: Path) -> bool:
    """Remove only the marker block this module added to ``settings.json``."""
    if not path.exists():
        return False
    raw = _read_settings(path)
    start = raw.find(_BYOK_MARKER_START)
    end = raw.find(_BYOK_MARKER_END)
    if start < 0 or end < 0 or end < start:
        return False
    line_start = raw.rfind("\n", 0, start) + 1
    line_end = raw.find("\n", end)
    line_end = len(raw) if line_end < 0 else line_end + 1
    prefix = raw[:line_start]
    suffix = raw[line_end:]
    trimmed = prefix.rstrip()
    if trimmed.endswith(","):
        prefix = trimmed[:-1] + prefix[len(trimmed) :]
    updated = prefix + suffix
    _validate_settings(updated, path)
    fsutil.write_text(path, updated)
    return True


def proxy_base_url(port: int, project: str | None = None) -> str:
    """Base URL VS Code should call, carrying the per-project savings prefix."""
    return str(with_project_prefix(f"http://127.0.0.1:{port}", project))
