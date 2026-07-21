"""
Token-bucket rate limiter with per-tenant quotas.

Supports in-memory fallback by default. Uncomment Redis paths for
production deployments.

Quotas are configured in ``config/tenants.yaml`` per tenant:

.. code-block:: yaml

    tenants:
      acme:
        rate_limits:
          max_requests_per_minute: 600
          max_tokens_per_day: 5000000
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field

# --- Uncomment for Redis-backed rate limiting:
# import redis.asyncio as redis


@dataclass
class TokenBucket:
    """Thread-safe token bucket for a single rate limit."""

    max_tokens: float
    refill_rate: float  # tokens per second
    tokens: float = field(default=0.0)
    last_refill: float = field(default_factory=time.monotonic)

    def __post_init__(self):
        self.tokens = self.max_tokens

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def consume(self, tokens: float = 1.0) -> bool:
        """Try to consume *tokens*.  Returns True if allowed."""
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    def available(self) -> float:
        """How many tokens are available right now."""
        self._refill()
        return self.tokens


class RateLimiter:
    """Per-tenant rate limiting with in-memory buckets.

    For multi-process deployments replace the internal dict with Redis
    (see commented-out Redis client).
    """

    def __init__(self) -> None:
        # tenant_id: bucket_name → TokenBucket
        self._buckets: dict[str, dict[str, TokenBucket]] = defaultdict(dict)

    # async def __init_redis__(self, redis_url: str):
    #     self._redis = await redis.from_url(redis_url)

    def get_or_create_bucket(self, tenant_id: str, bucket_name: str,
                             max_tokens: float, refill_rate: float) -> TokenBucket:
        """Return (and cache) the bucket for *tenant_id* / *bucket_name*."""
        buckets = self._buckets[tenant_id]
        if bucket_name not in buckets:
            buckets[bucket_name] = TokenBucket(max_tokens=max_tokens, refill_rate=refill_rate)
        return buckets[bucket_name]

    def check(self, tenant_id: str, tenant_config: dict,
              estimated_tokens: int = 0) -> tuple[bool, str]:
        """Check all rate limits for *tenant_id*.

        Returns ``(allowed, reason)``.  *reason* is empty when allowed.
        """
        limits = tenant_config.get("rate_limits", {})
        if not limits:
            return True, ""

        # --- Per-minute request limit ---------------------------------------
        rpm = limits.get("max_requests_per_minute")
        if rpm:
            bucket = self.get_or_create_bucket(tenant_id, "rpm", rpm, rpm / 60.0)
            if not bucket.consume(1):
                return False, f"rate limit exceeded: {rpm} requests/min"

        # --- Per-day token limit -------------------------------------------
        tpd = limits.get("max_tokens_per_day")
        if tpd and estimated_tokens > 0:
            bucket = self.get_or_create_bucket(tenant_id, "tpd", tpd, tpd / 86400.0)
            if not bucket.consume(estimated_tokens):
                return False, f"rate limit exceeded: {tpd} tokens/day"

        return True, ""


# Singleton — replace with dependency injection for production.
limiter = RateLimiter()
