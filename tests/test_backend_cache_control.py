"""cache_control must survive the Anthropic -> LiteLLM/OpenAI conversion.

The compression router explicitly protects ``cache_control`` blocks
(`transforms/content_router.py`: "we never modify cache_control'd blocks"),
and LiteLLM translates them into provider cache points (e.g. Bedrock
``cachePoint``). But the backend conversion used to flatten system blocks,
message text blocks, tool_result blocks, and tool definitions into plain
strings/dicts — silently discarding every marker, so Anthropic-ingress
clients got zero prompt caching. These tests pin the passthrough.
"""

from headroom.backends.litellm import (
    LiteLLMBackend,
    _convert_anthropic_tool,
    _system_to_openai_content,
)

CC = {"type": "ephemeral"}


class TestSystemToOpenAIContent:
    """System blocks: legacy string join without markers, blocks with."""

    def test_no_markers_keeps_joined_string(self):
        system = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        assert _system_to_openai_content(system) == "a b"

    def test_plain_strings_join(self):
        assert _system_to_openai_content(["a", "b"]) == "a b"

    def test_marker_preserved_as_blocks(self):
        system = [
            {"type": "text", "text": "manual", "cache_control": CC},
            {"type": "text", "text": "suffix"},
        ]
        result = _system_to_openai_content(system)
        assert result == [
            {"type": "text", "text": "manual", "cache_control": CC},
            {"type": "text", "text": "suffix"},
        ]


class TestConvertAnthropicToolCacheControl:
    def test_tool_cache_control_preserved(self):
        tool = {
            "name": "get_weather",
            "description": "Get weather",
            "input_schema": {"type": "object", "properties": {}},
            "cache_control": CC,
        }
        result = _convert_anthropic_tool(tool)
        assert result["cache_control"] == CC
        assert result["function"]["name"] == "get_weather"

    def test_tool_without_cache_control_unchanged(self):
        tool = {"name": "t", "input_schema": {"type": "object"}}
        result = _convert_anthropic_tool(tool)
        assert "cache_control" not in result


class TestConvertMessagesCacheControl:
    def _backend(self):
        return object.__new__(LiteLLMBackend)  # method under test needs no state

    def test_text_block_marker_preserved(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "big doc", "cache_control": CC},
                    {"type": "text", "text": "question"},
                ],
            }
        ]
        converted = LiteLLMBackend._convert_messages_for_litellm(self._backend(), msgs)
        assert converted == [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "big doc", "cache_control": CC},
                    {"type": "text", "text": "question"},
                ],
            }
        ]

    def test_text_blocks_without_marker_keep_joined_string(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "a"},
                    {"type": "text", "text": "b"},
                ],
            }
        ]
        converted = LiteLLMBackend._convert_messages_for_litellm(self._backend(), msgs)
        assert converted == [{"role": "user", "content": "a\nb"}]

    def test_tool_result_marker_preserved(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "rows"}],
                        "cache_control": CC,
                    }
                ],
            }
        ]
        converted = LiteLLMBackend._convert_messages_for_litellm(self._backend(), msgs)
        assert converted == [
            {
                "role": "tool",
                "tool_call_id": "t1",
                "content": [{"type": "text", "text": "rows", "cache_control": CC}],
            }
        ]

    def test_tool_result_inner_block_marker_lifted(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "content": [{"type": "text", "text": "rows", "cache_control": CC}],
                    }
                ],
            }
        ]
        converted = LiteLLMBackend._convert_messages_for_litellm(self._backend(), msgs)
        assert converted[0]["content"] == [{"type": "text", "text": "rows", "cache_control": CC}]

    def test_tool_result_without_marker_unchanged(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t3",
                        "content": [{"type": "text", "text": "rows"}],
                    }
                ],
            }
        ]
        converted = LiteLLMBackend._convert_messages_for_litellm(self._backend(), msgs)
        assert converted == [{"role": "tool", "tool_call_id": "t3", "content": "rows"}]

    def test_string_content_unchanged(self):
        msgs = [{"role": "user", "content": "hello"}]
        converted = LiteLLMBackend._convert_messages_for_litellm(self._backend(), msgs)
        assert converted == [{"role": "user", "content": "hello"}]
