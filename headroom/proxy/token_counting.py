"""Offloaded token-count helpers shared by proxy handlers."""

from __future__ import annotations

import logging
from typing import Any, cast

logger = logging.getLogger("headroom.proxy")


def _record_fallback_model(owner: Any, model: Any, message: str) -> None:
    fallback_models = getattr(owner, "_token_count_fallback_models", None)
    if fallback_models is None:
        fallback_models = set()
        owner._token_count_fallback_models = fallback_models
    if model not in fallback_models:
        fallback_models.add(model)
        logger.warning(message)


async def count_tokens_offloaded(owner: Any, model: Any, messages: Any) -> tuple[Any, int]:
    """Resolve a tokenizer and count messages off the event loop when possible."""
    from headroom.proxy.helpers import COMPRESSION_TIMEOUT_SECONDS
    from headroom.tokenizers import EstimatingTokenCounter, get_tokenizer

    runner = getattr(owner, "_run_compression_in_executor", None)
    if runner is None:
        estimator = EstimatingTokenCounter()
        return estimator, estimator.count_messages(messages)

    def _resolve_and_count() -> tuple[Any, int]:
        tokenizer = get_tokenizer(model)
        return tokenizer, tokenizer.count_messages(messages)

    try:
        result = await runner(
            _resolve_and_count,
            timeout=float(COMPRESSION_TIMEOUT_SECONDS),
        )
        return cast(tuple[Any, int], result)
    except Exception as e:  # fail open — includes asyncio.TimeoutError
        _record_fallback_model(
            owner,
            model,
            f"Token counting for model {model} failed or timed out "
            f"({e.__class__.__name__}); falling back to estimation",
        )
        estimator = EstimatingTokenCounter()
        return estimator, estimator.count_messages(messages)


async def count_texts_offloaded(owner: Any, model: Any, texts: Any) -> tuple[Any, int]:
    """Resolve a tokenizer and count text fragments off the event loop when possible."""
    from headroom.proxy.helpers import COMPRESSION_TIMEOUT_SECONDS
    from headroom.tokenizers import EstimatingTokenCounter, get_tokenizer

    text_list = list(texts)
    runner = getattr(owner, "_run_compression_in_executor", None)
    if runner is None:
        estimator = EstimatingTokenCounter()
        return estimator, sum(estimator.count_text(text) for text in text_list)

    def _resolve_and_count() -> tuple[Any, int]:
        tokenizer = get_tokenizer(model)
        return tokenizer, sum(tokenizer.count_text(text) for text in text_list)

    try:
        result = await runner(
            _resolve_and_count,
            timeout=float(COMPRESSION_TIMEOUT_SECONDS),
        )
        return cast(tuple[Any, int], result)
    except Exception as e:  # fail open — includes asyncio.TimeoutError
        _record_fallback_model(
            owner,
            model,
            f"Token text counting for model {model} failed or timed out "
            f"({e.__class__.__name__}); falling back to estimation",
        )
        estimator = EstimatingTokenCounter()
        return estimator, sum(estimator.count_text(text) for text in text_list)
