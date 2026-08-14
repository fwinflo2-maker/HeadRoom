"""Post-response memory extraction — analyze model responses for extractable facts.

Two extraction strategies, tried in order:
1. **Inline parsing** (zero-cost): check if the model already output a
   ``<memory>`` block in its response text (uses ``inline_extractor.py``).
2. **LLM extraction** (costs tokens): call a lightweight LLM with the
   conversation to extract facts the model didn't explicitly express as
   memories.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from headroom.memory.inline_extractor import parse_response_with_memory

logger = logging.getLogger(__name__)


@dataclass
class ExtractedMemory:
    """A single extracted memory with metadata."""

    content: str
    importance: float = 0.5
    facts: list[str] = field(default_factory=list)
    entities: list[dict[str, str]] = field(default_factory=list)
    relationships: list[dict[str, str]] = field(default_factory=list)

    def to_save_input(self) -> dict[str, Any]:
        """Convert to the input dict expected by ``memory_save``."""
        return {
            "content": self.content,
            "importance": self.importance,
            "facts": self.facts or [self.content],
        }


@dataclass
class ExtractionResult:
    """Result of extracting memories from a conversation turn."""

    memories: list[ExtractedMemory]
    strategy: str  # "inline", "llm", or "none"
    raw_response: str = ""


_EXTRACTION_SYSTEM_PROMPT = """You are a precise memory extractor. Given a conversation turn
(user message + assistant response), extract facts worth remembering.

Guidelines:
- Extract discrete, self-contained facts
- Use the actual speaker names (never "user" or "assistant")
- Be specific — use exact terms from the conversation
- Skip greetings, thanks, small talk, and one-off questions
- Rate importance 0.0–1.0 (1.0 = critical, 0.3 = nice to know)
- Output max 5 memories per turn

Output ONLY valid JSON:
{"memories": [{"content": "fact", "importance": 0.7}, ...]}"""


def _extract_text_from_anthropic_response(response_body: dict[str, Any]) -> str:
    """Extract the assistant's text content from an Anthropic-format response."""
    texts: list[str] = []
    for block in response_body.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(block.get("text", ""))
    return "\n".join(texts)


def _extract_text_from_openai_response(response_body: dict[str, Any]) -> str:
    """Extract the assistant's text content from an OpenAI-format response."""
    choices = response_body.get("choices", [])
    if not choices:
        return ""
    message = choices[0].get("message", {})
    content = message.get("content", "")
    if content is None:
        return ""
    return str(content)


def extract_inline(response_text: str) -> ExtractionResult:
    """Try inline ``<memory>`` block parsing (zero-cost).

    Args:
        response_text: The raw assistant response text.

    Returns:
        An ``ExtractionResult`` with any memories found via inline blocks.
    """
    parsed = parse_response_with_memory(response_text)
    if parsed.memories:
        memories = [
            ExtractedMemory(
                content=m.get("content", ""),
                importance=m.get("importance", 0.5),
            )
            for m in parsed.memories
            if m.get("content")
        ]
        return ExtractionResult(
            memories=memories,
            strategy="inline",
            raw_response=response_text,
        )
    return ExtractionResult(memories=[], strategy="none", raw_response=response_text)


def _parse_llm_extraction_output(text: str) -> list[ExtractedMemory]:
    """Parse the JSON output from the extraction LLM."""
    # Try to find a JSON block in the response
    json_pattern = r"\{(?:[^{}]|(?:\{[^{}]*\}))*\}"

    text = text.strip()
    match = re.search(json_pattern, text, re.DOTALL)
    if match:
        text = match.group()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse extraction LLM output as JSON: %s", text[:200])
        return []

    raw_memories: list[Any] = []
    if isinstance(data, dict):
        raw_memories = data.get("memories", data.get("facts", [])) or []
    elif isinstance(data, list):
        raw_memories = data

    if isinstance(raw_memories, list):
        return [
            ExtractedMemory(
                content=m.get("content", m) if isinstance(m, dict) else str(m),
                importance=float(m.get("importance", 0.5)) if isinstance(m, dict) else 0.5,
            )
            for m in raw_memories
        ]
    return []


async def extract_with_llm(
    messages: list[dict[str, Any]],
    *,
    model: str = "gpt-4o-mini",
    api_key: str | None = None,
    base_url: str | None = None,
    max_memories: int = 5,
) -> ExtractionResult:
    """Extract memories by calling a lightweight LLM with the conversation.

    Args:
        messages: The conversation messages (list of ``{role, content}`` dicts).
        model: The extraction model to use.
        api_key: API key for the extraction model provider.
        base_url: Base URL for the extraction model provider.
        max_memories: Maximum number of memories to extract.

    Returns:
        An ``ExtractionResult`` with extracted memories.
    """
    extraction_messages = [
        {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
        *messages,
    ]

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        response = await client.chat.completions.create(
            model=model,
            messages=extraction_messages,  # type: ignore[arg-type]
            temperature=0.1,
            max_tokens=1024,
        )
        raw = response.choices[0].message.content or ""
    except Exception as exc:
        logger.warning("LLM extraction failed: %s", exc)
        return ExtractionResult(memories=[], strategy="none", raw_response="")

    memories = _parse_llm_extraction_output(raw)[:max_memories]
    return ExtractionResult(
        memories=memories,
        strategy="llm",
        raw_response=raw,
    )


async def extract_memories_from_turn(
    messages: list[dict[str, Any]],
    response_text: str,
    *,
    model: str = "gpt-4o-mini",
    api_key: str | None = None,
    base_url: str | None = None,
    use_llm: bool = False,
) -> ExtractionResult:
    """Extract memories from a conversation turn.

    Tries inline parsing first (zero-cost). Falls back to LLM extraction
    if ``use_llm=True`` and no inline memories were found.

    Args:
        messages: The full conversation messages.
        response_text: The assistant's response text.
        model: Extraction model (used only for LLM path).
        api_key: API key for the extraction model.
        base_url: Base URL for the extraction model provider.
        use_llm: Whether to fall back to LLM extraction.

    Returns:
        An ``ExtractionResult``.
    """
    result = extract_inline(response_text)
    if result.memories:
        return result

    if use_llm:
        result = await extract_with_llm(
            messages,
            model=model,
            api_key=api_key,
            base_url=base_url,
        )
        if result.memories:
            return result

    return ExtractionResult(memories=[], strategy="none", raw_response=response_text)
