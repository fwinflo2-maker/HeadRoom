"""Headroom Proxy.

The proxy request path is served by the Rust binary ``headroom-proxy``.
``headroom proxy start`` (via ``headroom/cli/proxy.py``) discovers and
exec-replaces into that binary.

Surviving Python modules in this package are utilities used by the CLI
and agent-wrap infrastructure:

- :mod:`headroom.proxy.modes` — token/cache mode normalization
- :mod:`headroom.proxy.project_context` — per-request project attribution
- :mod:`headroom.proxy.forwarded_headers` — X-Forwarded-* header helpers
- :mod:`headroom.proxy.runtime_env` — runtime environment helpers
- :mod:`headroom.proxy.ssl_context` — TLS context construction
"""
