"""VS Code Copilot Chat BYOK integration (`wrap vscode-chat`).

The proxy already serves the three API shapes VS Code's Custom Endpoint provider
can speak, so this layer is config generation plus file surgery on two files the
user also edits by hand. That makes the destructive-edit properties the important
ones: never clobber another provider, never claim an entry we did not write.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest

from headroom.providers.copilot.vscode_chat import (
    BYOK_ENABLED_SETTING,
    HEADROOM_PROVIDER_NAME,
    build_model_entries,
    build_provider_block,
    byok_entitlement_enabled,
    configure_chat_models,
    disable_byok_setting,
    enable_byok_setting,
    remove_chat_models,
)

FIXTURE = Path(__file__).parent / "fixtures" / "copilot_models" / "models_list.json"
BASE = "http://127.0.0.1:8787/p/proj"


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Entitlement preflight
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("tid=abc;client_byok=1;chat=1:tok", True),
        ("tid=abc;client_byok=0;chat=1:tok", False),
        ("tid=abc;chat=1:tok", None),  # claim absent => unknown, not "denied"
        ("garbage", None),
    ],
)
def test_byok_entitlement_is_read_from_the_token(token: str, expected: bool | None) -> None:
    """VS Code hides the whole Custom Endpoint feature when the org disables BYOK.

    Distinguishing "denied" from "unknown" matters: denied should refuse at
    launch, unknown should proceed with a note rather than block a working setup.
    """
    assert byok_entitlement_enabled(token) is expected


# ---------------------------------------------------------------------------
# Model entry generation
# ---------------------------------------------------------------------------


def test_entries_cover_every_selectable_chat_model(payload: dict) -> None:
    entries = build_model_entries(payload, BASE)
    assert len(entries) == 22
    ids = {e["id"] for e in entries}
    assert "claude-opus-4.8" in ids
    assert "gpt-5.4" in ids


def test_non_chat_and_unselectable_models_are_excluded(payload: dict) -> None:
    """Embeddings in a chat picker would be user-visible nonsense."""
    ids = {e["id"] for e in build_model_entries(payload, BASE)}
    assert "text-embedding-3-small" not in ids
    assert "trajectory-compaction" not in ids  # picker-disabled
    assert "gpt-4o" not in ids  # picker-disabled


def test_api_type_never_selects_messages(payload: dict) -> None:
    """The Anthropic wire rejects VS Code's placeholder key, so it is avoided.

    Claude models are reachable on chat-completions, so nothing is lost.
    """
    types = {e["apiType"] for e in build_model_entries(payload, BASE)}
    assert "messages" not in types
    assert types <= {"chat-completions", "responses"}


def test_responses_only_models_get_the_responses_wire(payload: dict) -> None:
    entries = {e["id"]: e for e in build_model_entries(payload, BASE)}
    assert entries["mai-code-1-flash-picker"]["apiType"] == "responses"
    assert entries["mai-code-1-flash-picker"]["url"].endswith("/v1/responses")
    assert entries["claude-opus-4.8"]["apiType"] == "chat-completions"
    assert entries["claude-opus-4.8"]["url"].endswith("/v1/chat/completions")


def test_capabilities_come_from_the_payload_not_hardcoded(payload: dict) -> None:
    """`toolCalling: false` hides a model from agent mode, so it must be real.

    Asserted by mutating the payload rather than by naming a model that happens to
    lack a capability today: every selectable model in the current live catalog
    advertises both tool calling and vision, so a fixture-value assertion would
    pass even if the fields were hardcoded.
    """
    entries = {e["id"]: e for e in build_model_entries(payload, BASE)}
    assert entries["claude-opus-4.8"]["toolCalling"] is True
    assert entries["claude-opus-4.8"]["vision"] is True
    assert entries["claude-opus-4.8"]["maxOutputTokens"] == 64000

    import copy

    mutated = copy.deepcopy(payload)
    for model in mutated["data"]:
        if model.get("id") == "claude-opus-4.8":
            model["capabilities"]["supports"]["tool_calls"] = False
            model["capabilities"]["supports"]["vision"] = False
            model["capabilities"]["limits"]["max_output_tokens"] = 1234
    changed = {e["id"]: e for e in build_model_entries(mutated, BASE)}
    assert changed["claude-opus-4.8"]["toolCalling"] is False
    assert changed["claude-opus-4.8"]["vision"] is False
    assert changed["claude-opus-4.8"]["maxOutputTokens"] == 1234


def test_urls_keep_the_project_prefix(payload: dict) -> None:
    """Per-project savings attribution rides in the base URL."""
    for entry in build_model_entries(payload, BASE):
        assert entry["url"].startswith("http://127.0.0.1:8787/p/proj/")


def test_empty_payload_yields_no_entries() -> None:
    assert build_model_entries({}, BASE) == []
    assert build_model_entries({"data": "nonsense"}, BASE) == []


# ---------------------------------------------------------------------------
# chatLanguageModels.json surgery
# ---------------------------------------------------------------------------


def _block(payload: dict, n: int = 2) -> dict:
    return build_provider_block(build_model_entries(payload, BASE)[:n])


def test_provider_is_added_to_an_empty_config(tmp_path: Path, payload: dict) -> None:
    p = tmp_path / "chatLanguageModels.json"
    assert configure_chat_models(p, _block(payload)) == "added"
    written = json.loads(p.read_text(encoding="utf-8"))
    assert len(written) == 1
    assert written[0]["vendor"] == "customendpoint"
    assert written[0]["name"] == HEADROOM_PROVIDER_NAME


def test_other_providers_are_preserved(tmp_path: Path, payload: dict) -> None:
    """The file is shared: a user's own providers must survive untouched."""
    p = tmp_path / "chatLanguageModels.json"
    mine = {"name": "My Ollama", "vendor": "customendpoint", "models": [{"id": "llama"}]}
    p.write_text(json.dumps([mine]), encoding="utf-8")
    configure_chat_models(p, _block(payload))
    written = json.loads(p.read_text(encoding="utf-8"))
    assert mine in written
    assert len(written) == 2


def test_refresh_replaces_in_place_without_duplicating(tmp_path: Path, payload: dict) -> None:
    p = tmp_path / "chatLanguageModels.json"
    configure_chat_models(p, _block(payload, 2))
    assert configure_chat_models(p, _block(payload, 3)) == "updated"
    written = json.loads(p.read_text(encoding="utf-8"))
    assert len(written) == 1, "refresh appended instead of replacing"
    assert len(written[0]["models"]) == 3


def test_a_same_named_provider_we_did_not_write_is_never_replaced(
    tmp_path: Path, payload: dict
) -> None:
    """Ownership is proven by digest, not by name.

    A user may copy our entry and edit it; silently overwriting their edits would
    be the same class of bug that marker-based ownership caused elsewhere.
    """
    p = tmp_path / "chatLanguageModels.json"
    theirs = {"name": HEADROOM_PROVIDER_NAME, "vendor": "customendpoint", "models": [{"id": "x"}]}
    p.write_text(json.dumps([theirs]), encoding="utf-8")
    with pytest.raises(click.ClickException, match="did not write"):
        configure_chat_models(p, _block(payload))
    assert json.loads(p.read_text(encoding="utf-8")) == [theirs]


def test_remove_takes_only_our_entry(tmp_path: Path, payload: dict) -> None:
    p = tmp_path / "chatLanguageModels.json"
    mine = {"name": "My Ollama", "vendor": "customendpoint", "models": []}
    p.write_text(json.dumps([mine]), encoding="utf-8")
    configure_chat_models(p, _block(payload))
    assert remove_chat_models(p) is True
    assert json.loads(p.read_text(encoding="utf-8")) == [mine]


def test_remove_is_a_noop_when_nothing_is_ours(tmp_path: Path) -> None:
    p = tmp_path / "chatLanguageModels.json"
    theirs = [{"name": "Someone else", "vendor": "customendpoint", "models": []}]
    p.write_text(json.dumps(theirs), encoding="utf-8")
    assert remove_chat_models(p) is False
    assert json.loads(p.read_text(encoding="utf-8")) == theirs


def test_malformed_config_is_refused_not_overwritten(tmp_path: Path, payload: dict) -> None:
    p = tmp_path / "chatLanguageModels.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(click.ClickException, match="Could not parse"):
        configure_chat_models(p, _block(payload))
    assert p.read_text(encoding="utf-8") == "{not json"


def test_json_object_instead_of_array_is_refused(tmp_path: Path, payload: dict) -> None:
    p = tmp_path / "chatLanguageModels.json"
    p.write_text('{"providers": []}', encoding="utf-8")
    with pytest.raises(click.ClickException, match="JSON array"):
        configure_chat_models(p, _block(payload))


# ---------------------------------------------------------------------------
# settings.json: the 1.132 visibility gate
# ---------------------------------------------------------------------------


def test_byok_setting_is_added_and_removed(tmp_path: Path) -> None:
    """Without this, 1.132+ silently hides every Custom Endpoint model."""
    p = tmp_path / "settings.json"
    p.write_text('{\n\t"editor.fontSize": 14\n}\n', encoding="utf-8")
    assert enable_byok_setting(p) == "added"
    text = p.read_text(encoding="utf-8")
    assert BYOK_ENABLED_SETTING in text
    assert '"editor.fontSize": 14' in text
    assert json.loads(_strip(text))[BYOK_ENABLED_SETTING] is True

    assert disable_byok_setting(p) is True
    after = p.read_text(encoding="utf-8")
    assert BYOK_ENABLED_SETTING not in after
    assert '"editor.fontSize": 14' in after


def test_byok_setting_respects_a_user_value(tmp_path: Path) -> None:
    """If the user already set it, leave their choice alone."""
    p = tmp_path / "settings.json"
    p.write_text(f'{{\n\t"{BYOK_ENABLED_SETTING}": false\n}}\n', encoding="utf-8")
    assert enable_byok_setting(p) == "already set by user"
    assert "false" in p.read_text(encoding="utf-8")


def test_byok_setting_is_idempotent(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_text("{}\n", encoding="utf-8")
    enable_byok_setting(p)
    assert enable_byok_setting(p) == "already set"
    assert p.read_text(encoding="utf-8").count(BYOK_ENABLED_SETTING) == 1


def _strip(text: str) -> str:
    import re

    from headroom.providers.copilot.vscode import _strip_jsonc_comments

    return re.sub(r",\s*([}\]])", r"\1", _strip_jsonc_comments(text))


def test_api_key_is_an_inert_literal_not_an_input_prompt(payload: dict) -> None:
    """An ``${input:...}`` variable can prompt the user to type a key.

    The proxy substitutes the real Copilot credential itself, so the value is
    never read — but a user who pastes a live key in response to that prompt would
    be putting a credential somewhere it serves no purpose. A fixed inert literal
    removes the prompt path entirely.
    """
    from headroom.providers.copilot.vscode_chat import PLACEHOLDER_API_KEY

    block = build_provider_block(build_model_entries(payload, BASE))
    assert block["apiKey"] == PLACEHOLDER_API_KEY
    assert "${input:" not in block["apiKey"]
    assert "unused" in PLACEHOLDER_API_KEY


def test_generated_config_contains_no_credential_material(payload: dict, tmp_path: Path) -> None:
    """Nothing token-shaped may reach a file the user might share."""
    p = tmp_path / "chatLanguageModels.json"
    configure_chat_models(p, build_provider_block(build_model_entries(payload, BASE)))
    text = p.read_text(encoding="utf-8")
    for pattern in ("gho_", "ghs_", "ghu_", "github_pat_", "sk-", "Bearer ", "tid=", "sku="):
        assert pattern not in text, f"{pattern!r} leaked into the generated config"
