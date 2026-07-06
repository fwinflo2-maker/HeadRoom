"""Runtime helpers for ZCode (zcode.z.ai desktop app) integrations."""

from __future__ import annotations

from dataclasses import dataclass

from headroom.providers.claude import proxy_base_url as _claude_proxy_base_url


@dataclass(frozen=True)
class ZCodeProxyTargets:
    """Resolved local proxy targets shown in ZCode setup instructions."""

    openai_base_url: str
    anthropic_base_url: str


def build_proxy_targets(port: int) -> ZCodeProxyTargets:
    """Build the local proxy URLs shown to ZCode users."""
    return ZCodeProxyTargets(
        openai_base_url=f"http://127.0.0.1:{port}/v1",
        anthropic_base_url=_claude_proxy_base_url(port),
    )


def render_setup_lines(port: int) -> list[str]:
    """Render the ZCode setup instructions for the local proxy."""
    targets = build_proxy_targets(port)
    return [
        "  Headroom proxy is running. Configure ZCode:",
        "",
        "  Open ZCode > Settings > Model Settings:",
        "",
        f"    OpenAI Base URL:      {targets.openai_base_url}",
        f"    Anthropic Base URL:   {targets.anthropic_base_url}",
        "",
        "  Select a model through the new provider in ZCode's model selector.",
        "",
        "  To add the Headroom MCP server (optional):",
        "    Settings > MCP Servers > New MCP Server > Full configuration",
        '    Paste: {"headroom": {"type": "stdio", "command": "headroom",',
        '             "args": ["mcp", "serve"], "enabled": true}}',
    ]
