"""DS4 (DeepSeek V4) subscription usage, savings, and budget tracking.

Detects DS4 API keys from ``Authorization: Bearer sk-ds4-*`` headers in
proxied requests, tracks per-key token usage and compression savings using
``model_cost_map`` pricing, enforces budget limits, and exposes stats for
the ``/stats`` endpoint.

Architecture follows ``headroom/subscription/tracker.py``:
- QuotaTracker subclass for pluggable registry integration
- Thread-safe state updates
- Per-key accounting for multi-key deployments
- Budget enforcement with configurable period
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from headroom.subscription.base import QuotaTracker

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_float(value: Any, default: float = 0.0) -> float:
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return default


def _ensure_int(value: Any, default: int = 0) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# DS4 API key detection
# ---------------------------------------------------------------------------

# DS4 API keys start with this prefix. Defined here so the same constant
# is used by both the tracker and the proxy handler hooks.
DS4_API_KEY_PREFIX = "sk-ds4-"


def is_ds4_api_key(token: str) -> bool:
    """Return True when *token* looks like a DS4 API key."""
    return token.startswith(DS4_API_KEY_PREFIX)


def extract_ds4_auth(auth_header: str) -> str | None:
    """Extract the DS4 token from an ``Authorization`` header, or ``None``.

    Returns ``None`` when the header is missing, is not ``Bearer ...``, or
    the bearer token does not start with ``sk-ds4-``.
    """
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer ") :]
    if is_ds4_api_key(token):
        return token
    return None


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Ds4Contribution:
    """Cumulative DS4 usage and savings counters for one scope."""

    tokens_submitted: int = 0
    tokens_saved: int = 0
    cost_with_headroom_usd: float = 0.0
    savings_usd: float = 0.0
    requests: int = 0
    last_active_at: str | None = None
    started_at: str | None = None

    @property
    def cost_without_headroom(self) -> float:
        return self.cost_with_headroom_usd + self.savings_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "tokens_submitted": self.tokens_submitted,
            "tokens_saved": self.tokens_saved,
            "cost_with_headroom_usd": round(self.cost_with_headroom_usd, 4),
            "cost_without_headroom_usd": round(self.cost_without_headroom, 4),
            "savings_usd": round(self.savings_usd, 4),
            "requests": self.requests,
            "last_active_at": self.last_active_at or "",
            "started_at": self.started_at or "",
        }


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class Ds4SubscriptionTracker(QuotaTracker):
    """Tracks DS4 subscription usage, compression savings, and budgets.

    Implements :class:`~headroom.subscription.base.QuotaTracker` so it
    can be registered with the quota registry alongside the Anthropic,
    Codex, and Copilot trackers.

    Detection is passive (header-driven): the proxy handler calls
    :meth:`notify_active` when a request carries a ``sk-ds4-*`` Bearer
    token, and calls :meth:`update_contribution` after the request
    completes.

    Budget enforcement uses a simple rolling-cost check: the total cost
    (with-headroom + savings) accumulated within the configured period
    must not exceed ``budget_limit_usd``.
    """

    key = "ds4_subscription"
    label = "DS4 (DeepSeek V4)"

    def __init__(
        self,
        budget_limit_usd: float | None = None,
        budget_period: str = "daily",
        model_cost_map: dict[str, dict[str, Any]] | None = None,
        enabled: bool = True,
    ) -> None:
        self._enabled = enabled
        self._budget_limit_usd = budget_limit_usd
        self._budget_period = budget_period
        self._model_cost_map = dict(model_cost_map) if model_cost_map else {}

        self._lock = threading.RLock()
        self._contribution = Ds4Contribution()
        self._tracked_keys: dict[str, Ds4Contribution] = {}

        self._cost_entries: list[tuple[float, float]] = []  # (timestamp, total_cost)
        self._last_prune_time: float = 0.0

    # ------------------------------------------------------------------
    # QuotaTracker interface
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return self._enabled

    def get_stats(self) -> dict[str, Any] | None:
        with self._lock:
            return self._snapshot_locked()

    async def start(self) -> None:
        logger.info("DS4 subscription tracker started")

    async def stop(self) -> None:
        logger.info("DS4 subscription tracker stopped")

    # ------------------------------------------------------------------
    # Proxy integration hooks
    # ------------------------------------------------------------------

    def notify_active(self, auth_header: str) -> None:
        """Called by the proxy handler when a DS4 request comes through.

        Only processes Bearer tokens that start with ``sk-ds4-``.
        """
        token = extract_ds4_auth(auth_header)
        if token is None:
            return
        with self._lock:
            now_str = _to_utc_iso(_utc_now())
            self._contribution.last_active_at = now_str
            if self._contribution.started_at is None:
                self._contribution.started_at = now_str
            prefix = token[:16]
            entry = self._tracked_keys.setdefault(prefix, Ds4Contribution())
            entry.last_active_at = now_str
            if entry.started_at is None:
                entry.started_at = now_str

    def update_contribution(
        self,
        *,
        tokens_submitted: int = 0,
        tokens_saved: int = 0,
        cost_with_headroom: float = 0.0,
        cost_saved: float = 0.0,
        token_prefix: str = "",
    ) -> None:
        """Update usage counters after a DS4 request completes.

        Args:
            tokens_submitted: Compressed input tokens sent upstream.
            tokens_saved: Tokens removed by compression.
            cost_with_headroom: Actual cost of the proxied request (USD).
            cost_saved: Dollar savings from compression (USD).
            token_prefix: First 16 chars of the DS4 API key (for per-key
                breakdown).  Empty string means the key was not tracked by
                ``notify_active`` — we still count it in the aggregate.
        """
        tokens_submitted = _ensure_int(tokens_submitted)
        tokens_saved = _ensure_int(tokens_saved)
        cost_with_headroom = _ensure_float(cost_with_headroom)
        cost_saved = _ensure_float(cost_saved)

        with self._lock:
            c = self._contribution
            c.tokens_submitted += tokens_submitted
            c.tokens_saved += tokens_saved
            c.cost_with_headroom_usd += cost_with_headroom
            c.savings_usd += cost_saved
            c.requests += 1
            c.last_active_at = _to_utc_iso(_utc_now())

            if token_prefix and token_prefix in self._tracked_keys:
                k = self._tracked_keys[token_prefix]
                k.tokens_submitted += tokens_submitted
                k.tokens_saved += tokens_saved
                k.cost_with_headroom_usd += cost_with_headroom
                k.savings_usd += cost_saved
                k.requests += 1
                k.last_active_at = _to_utc_iso(_utc_now())

            self._cost_entries.append((time.time(), cost_with_headroom + cost_saved))
            self._prune_old_costs_locked()

    # ------------------------------------------------------------------
    # Budget
    # ------------------------------------------------------------------

    def check_budget(self) -> tuple[bool, float]:
        """Check if within budget.  Returns ``(allowed, remaining)``.

        When no budget is configured returns ``(True, inf)``.
        """
        if self._budget_limit_usd is None:
            return True, float("inf")
        period_cost = self._get_period_cost_locked()
        remaining = self._budget_limit_usd - period_cost
        return remaining > 0, max(0.0, remaining)

    def budget_status(self) -> dict[str, Any]:
        """Return a detailed budget-status dict for ``/stats``.

        Safe to call while ``self._lock`` is held (uses ``RLock``).
        """
        with self._lock:
            period_cost = self._get_period_cost_locked()
        result: dict[str, Any] = {
            "budget_limit_usd": self._budget_limit_usd,
            "budget_period": self._budget_period,
            "period_cost_usd": round(period_cost, 4),
        }
        if self._budget_limit_usd is not None:
            remaining = max(0.0, self._budget_limit_usd - period_cost)
            result["remaining_usd"] = round(remaining, 4)
            result["within_budget"] = period_cost <= self._budget_limit_usd
            result["utilization_pct"] = (
                round(period_cost / self._budget_limit_usd * 100, 2)
                if self._budget_limit_usd > 0
                else 0.0
            )
        else:
            result["remaining_usd"] = None
            result["within_budget"] = True
            result["utilization_pct"] = None
        return result

    def _get_period_cost_locked(self) -> float:
        """Sum of cost entries within the current budget period."""
        if not self._cost_entries:
            return 0.0
        now = time.time()
        if self._budget_period == "hourly":
            cutoff = now - 3600
        elif self._budget_period == "daily":
            cutoff = now - 86400
        else:
            cutoff = now - 2592000  # 30-day approximation
        return sum(cost for ts, cost in self._cost_entries if ts >= cutoff)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _snapshot_locked(self) -> dict[str, Any]:
        per_key = {prefix: c.to_dict() for prefix, c in sorted(self._tracked_keys.items())}
        return {
            "contribution": self._contribution.to_dict(),
            "tracked_keys": per_key,
            "tracked_key_count": len(self._tracked_keys),
            "budget": self.budget_status(),
        }

    def _prune_old_costs_locked(self) -> None:
        """Drop cost entries older than the retention window."""
        now = time.time()
        if now - self._last_prune_time < 300:
            return
        self._last_prune_time = now
        cutoff = now - 86400
        self._cost_entries = [(ts, c) for ts, c in self._cost_entries if ts >= cutoff]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_tracker_lock = threading.Lock()
_tracker_instance: Ds4SubscriptionTracker | None = None


def get_ds4_tracker() -> Ds4SubscriptionTracker | None:
    """Return the global DS4 tracker, or ``None`` if not configured."""
    return _tracker_instance


def configure_ds4_tracker(
    budget_limit_usd: float | None = None,
    budget_period: str = "daily",
    model_cost_map: dict[str, dict[str, Any]] | None = None,
    enabled: bool = True,
) -> Ds4SubscriptionTracker:
    """Create (or return existing) global tracker singleton."""
    global _tracker_instance
    with _tracker_lock:
        if _tracker_instance is None:
            _tracker_instance = Ds4SubscriptionTracker(
                budget_limit_usd=budget_limit_usd,
                budget_period=budget_period,
                model_cost_map=model_cost_map,
                enabled=enabled,
            )
    return _tracker_instance
