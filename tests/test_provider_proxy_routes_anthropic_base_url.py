from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from headroom.providers.proxy_routes import register_provider_routes


def test_v1_messages_forwards_x_headroom_base_url() -> None:
    captured: list[str | None] = []

    class Proxy:
        def __init__(self) -> None:
            self.config = SimpleNamespace(bedrock_api_url=None)
            self.provider_runtime = SimpleNamespace(
                api_target=lambda provider: f"https://runtime.{provider}.test",
                model_metadata_provider=lambda headers: "anthropic",
            )
            self.ANTHROPIC_API_URL = "https://api.anthropic.test"
            self.OPENAI_API_URL = "https://api.openai.test"
            self.GEMINI_API_URL = "https://api.gemini.test"
            self.CLOUDCODE_API_URL = "https://cloudcode.test"
            self.VERTEX_API_URL = "https://vertex.test"

        async def handle_anthropic_messages(
            self,
            request,
            upstream_base_url=None,
            provider_name="anthropic",
            model_override=None,
            force_stream=False,
        ):  # type: ignore[no-untyped-def]
            captured.append(upstream_base_url)
            return JSONResponse(
                {
                    "path": request.url.path,
                    "upstream_base_url": upstream_base_url,
                    "provider": provider_name,
                    "model": model_override,
                    "force_stream": force_stream,
                }
            )

    app = FastAPI()
    register_provider_routes(app, Proxy())

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-headroom-base-url": "https://custom.anthropic.example/base/"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "path": "/v1/messages",
        "upstream_base_url": "https://custom.anthropic.example/base",
        "provider": "anthropic",
        "model": None,
        "force_stream": False,
    }
    assert captured == ["https://custom.anthropic.example/base"]
