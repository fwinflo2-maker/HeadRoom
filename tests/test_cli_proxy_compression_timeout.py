from __future__ import annotations

import sys
import types

from click.testing import CliRunner

from headroom.cli.main import main


def test_compression_timeout_option_is_applied_before_proxy_import(monkeypatch) -> None:
    captured: dict[str, str | None] = {}
    fake = types.ModuleType("headroom.proxy.server")

    class ProxyConfig:
        def __init__(self, **kwargs):
            captured["timeout"] = __import__("os").environ.get(
                "HEADROOM_COMPRESSION_TIMEOUT_SECONDS"
            )
            self.__dict__.update(kwargs)
            self.host = kwargs.get("host", "127.0.0.1")
            self.port = kwargs.get("port", 8787)
            self.mode = kwargs.get("mode", "auto")
            self.optimize = kwargs.get("optimize", True)
            self.cache_enabled = kwargs.get("cache_enabled", True)
            self.rate_limit_enabled = kwargs.get("rate_limit_enabled", True)
            self.memory_enabled = kwargs.get("memory_enabled", False)
            self.offline = kwargs.get("offline", False)
            self.proxy_token = kwargs.get("proxy_token")

    def run_server(*args, **kwargs):
        captured["timeout"] = __import__("os").environ.get("HEADROOM_COMPRESSION_TIMEOUT_SECONDS")

    fake.ProxyConfig = ProxyConfig
    fake._parse_csv_tools = lambda value: []
    fake._parse_exclude_tools = lambda value: set()
    fake._parse_tool_profiles = lambda value: None
    fake._get_code_aware_banner_status = lambda config: "disabled"
    fake.run_server = run_server
    monkeypatch.setitem(sys.modules, "headroom.proxy.server", fake)

    CliRunner().invoke(
        main,
        ["proxy", "--compression-timeout-seconds", "75", "--no-telemetry"],
    )

    assert captured["timeout"] == "75.0"
