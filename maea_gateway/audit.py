"""
Structured JSON audit logger for the MAEA Tenant Gateway.

Every proxied request is logged as a single JSON line to stdout,
designed for ingestion by Fluentd, Vector, or any JSON-line log
aggregator.

Field reference
---------------
* timestamp      — ISO-8601 UTC
* tenant_id      — resolved tenant
* user           — JWT ``sub`` claim (empty if unauthenticated)
* method         — HTTP method
* path           — upstream path
* status_code    — upstream response status
* latency_ms     — round-trip milliseconds
* client_ip      — originating IP
* rate_limited   — True if the request was rejected by RateLimiter
* upstream       — upstream base URL (from config)
* user_agent     — trimmed User-Agent header
"""

import json
import sys
from datetime import datetime, timezone


def audit_log(*, tenant_id: str, user: str, method: str, path: str,
              status_code: int, latency_ms: float, client_ip: str = "",
              rate_limited: bool = False, upstream: str = "",
              user_agent: str = "") -> None:
    """Emit one audit record to stdout."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "user": user,
        "method": method,
        "path": path,
        "status_code": status_code,
        "latency_ms": round(latency_ms, 2),
        "client_ip": client_ip,
        "rate_limited": rate_limited,
        "upstream": upstream,
        "user_agent": user_agent[:256] if user_agent else "",
    }
    # Atomic JSON line — stdout is line-buffered by default when piped.
    print(json.dumps(record, ensure_ascii=False), file=sys.stdout, flush=True)
