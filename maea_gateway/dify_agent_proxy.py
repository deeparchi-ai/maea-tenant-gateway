"""
MAEA Gateway adapter for Dify Agent Backend (dify-agent).

Sits between dify-agent and plugin daemon, adding:
  - Audit logging per agent run (W3C traceparent)
  - Cost tracking (token usage per tool call)
  - Rate limiting (per tenant, per agent run)

Dify-agent configuration (in .env):
  DIFY_AGENT_PLUGIN_DAEMON_URL=http://maea-gateway:8080/dify-agent
"""

import time
import json

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Histogram

from maea_gateway.audit import audit_log
from maea_gateway.rate_limiter import limiter

# ── Metrics ──────────────────────────────────────────────
agent_run_counter = Counter(
    "maea_agent_runs", "Agent runs", ["tenant", "agent_app_id"]
)
tool_call_counter = Counter(
    "maea_agent_tool_calls", "Tool calls", ["tenant", "tool_name"]
)
agent_token_usage = Counter(
    "maea_agent_tokens", "Token usage per agent", ["tenant", "model"]
)
agent_cost = Counter(
    "maea_agent_cost_cents", "Estimated cost in cents", ["tenant", "model"]
)


class DifyAgentProxy:
    """Proxy that wraps dify-agent ↔ plugin_daemon calls with MAEA governance.

    Integrates into the existing app.py proxy middleware — no separate
    server needed.  When the upstream path matches ``/dify-agent/``,
    the proxy adds agent-specific audit/trace/cost headers.
    """

    # ── Cost estimation (USD per 1M tokens) ──────────────
    MODEL_COST_PER_1M = {
        "gpt-4o":         (2.50,  10.00),   # input, output
        "gpt-4o-mini":    (0.15,   0.60),
        "gpt-4-turbo":    (10.00, 30.00),
        "claude-3-opus":  (15.00, 75.00),
        "claude-3-sonnet": (3.00, 15.00),
        "claude-3-haiku":  (0.25,  1.25),
    }

    @classmethod
    def estimate_cost(cls, model: str, input_tokens: int,
                      output_tokens: int) -> float:
        """Estimate cost in cents from token counts."""
        input_price, output_price = cls.MODEL_COST_PER_1M.get(model, (0, 0))
        cost = (input_tokens / 1_000_000 * input_price +
                output_tokens / 1_000_000 * output_price)
        return round(cost * 100, 4)  # cents

    @staticmethod
    async def process_request(request: Request, tenant_id: str,
                              tenant_config: dict) -> dict | None:
        """Pre-process a dify-agent request. Returns rate-limit denial or None."""
        allowed, reason = limiter.check(tenant_id, tenant_config)
        if not allowed:
            return {"status_code": 429, "content": {"detail": reason}}
        return None

    @staticmethod
    def extract_agent_context(request: Request) -> dict:
        """Pull agent context from headers injected by dify-agent."""
        return {
            "agent_run_id": request.headers.get("X-Dify-Agent-Run-Id", ""),
            "agent_app_id": request.headers.get("X-Dify-Agent-App-Id", ""),
            "traceparent": request.headers.get("traceparent", ""),
            "model": request.headers.get("X-Dify-Agent-Model", "unknown"),
        }

    @classmethod
    def record_tool_call(cls, ctx: dict, response_json: dict,
                         tenant_id: str, latency_ms: float) -> None:
        """Record metrics and audit for a completed tool call."""
        model = ctx["model"]
        tool_name = response_json.get("tool_name", "unknown")

        # Token estimation from response
        usage = response_json.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        total_tokens = input_tokens + output_tokens

        tool_call_counter.labels(tenant=tenant_id, tool_name=tool_name).inc()
        agent_token_usage.labels(tenant=tenant_id, model=model).inc(total_tokens)

        cost = cls.estimate_cost(model, input_tokens, output_tokens)
        if cost > 0:
            agent_cost.labels(tenant=tenant_id, model=model).inc(cost)

        agent_run_counter.labels(
            tenant=tenant_id,
            agent_app_id=ctx["agent_app_id"],
        ).inc()

        audit_log(
            tenant_id=tenant_id,
            user=tenant_id,
            method="AGENT_TOOL",
            path=f"agent/{ctx['agent_app_id']}/tool/{tool_name}",
            status_code=200,
            latency_ms=latency_ms,
            upstream="dify-agent/plugin-daemon",
        )
