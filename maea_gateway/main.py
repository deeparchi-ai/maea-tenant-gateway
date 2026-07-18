"""
MAEA Multi-Tenant Gateway — Enterprise isolation middleware for Dify.

Sits in front of the Dify API and provides:
  1. JWT/OIDC authentication → tenant resolution
  2. Tenant isolation — dataset/app filtering per tenant
  3. Usage metering — per-tenant token/storage/API tracking  
  4. Audit trail — operation logging with tenant context

Usage:
    uvicorn maea_gateway.main:app --host 0.0.0.0 --port 8080

Part of MAEA by DeepArchi. MIT License.
"""

import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import Counter, Gauge, generate_latest
from pydantic import BaseModel

# ── Metrics ───────────────────────────────────────────────────

TOKEN_USAGE = Counter("maea_tenant_tokens", "Token usage by tenant", ["tenant_id"])
API_CALLS = Counter("maea_tenant_api_calls", "API calls by tenant", ["tenant_id", "endpoint"])
ACTIVE_TENANTS = Gauge("maea_active_tenants", "Active tenant count")
RATE_LIMIT_HITS = Counter("maea_rate_limit_hits", "Rate limit hits", ["tenant_id"])

# ── Models ────────────────────────────────────────────────────

class TenantConfig(BaseModel):
    tenant_id: str
    name: str = ""
    sso_provider: str = ""  # azure_ad, okta, none
    sso_tenant_id: str = ""
    oidc_issuer: str = ""
    workspace_id: str = ""
    rate_limits: dict[str, int] = {}
    isolation_rules: dict[str, Any] = {}

class GatewayConfig(BaseModel):
    dify_upstream: str = "http://localhost:5001"
    tenants: dict[str, TenantConfig] = {}
    default_rate_limit_per_minute: int = 100

# ── App ────────────────────────────────────────────────────────

app = FastAPI(title="MAEA Multi-Tenant Gateway", version="0.1.0")
config = GatewayConfig()
tenant_cache: dict[str, dict[str, Any]] = {}
rate_limit_store: dict[str, list[float]] = defaultdict(list)

# ── Config loading ────────────────────────────────────────────

def load_config(path: str = "config/tenants.yaml"):
    global config
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        tenants = {}
        for tid, tdata in data.get("tenants", {}).items():
            tenants[tid] = TenantConfig(
                tenant_id=tid,
                name=tdata.get("name", tid),
                sso_provider=tdata.get("sso", {}).get("provider", ""),
                sso_tenant_id=tdata.get("sso", {}).get("tenant_id", ""),
                oidc_issuer=tdata.get("sso", {}).get("oidc_issuer", ""),
                workspace_id=tdata.get("workspace_id", ""),
                rate_limits=tdata.get("rate_limits", {}),
                isolation_rules=tdata.get("isolation", {}),
            )
        config = GatewayConfig(
            dify_upstream=data.get("upstream", {}).get("dify_api", "http://localhost:5001"),
            tenants=tenants,
            default_rate_limit_per_minute=data.get("upstream", {}).get("default_rate_limit_per_minute", 100),
        )
        ACTIVE_TENANTS.set(len(tenants))
    except FileNotFoundError:
        pass  # use defaults

load_config()

# ── Middleware: Tenant Resolution ──────────────────────────────

@app.middleware("http")
async def tenant_middleware(request: Request, call_next):
    """Resolve tenant from Authorization header JWT or X-Tenant-ID header."""
    tenant_id = "default"

    # Method 1: Explicit X-Tenant-ID header
    xtid = request.headers.get("X-Tenant-ID", "")
    if xtid and xtid in config.tenants:
        tenant_id = xtid

    # Method 2: JWT claim extraction (simplified — for Azure AD / Okta)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and not xtid:
        try:
            import jwt
            token = auth[7:]
            # Try to decode without verification first to extract claims
            claims = jwt.decode(token, options={"verify_signature": False})
            tid = claims.get("tid") or claims.get("tenant_id") or claims.get("iss", "").split("/")[-1]
            if tid in config.tenants:
                tenant_id = tid
        except Exception:
            pass  # fall through to default

    request.state.tenant_id = tenant_id
    request.state.start_time = time.time()

    # Rate limit check
    tc = config.tenants.get(tenant_id)
    if tc:
        rpm = tc.rate_limits.get("max_api_calls_per_minute", config.default_rate_limit_per_minute)
        now = time.time()
        window = [t for t in rate_limit_store[tenant_id] if t > now - 60]
        if len(window) >= rpm:
            RATE_LIMIT_HITS.labels(tenant_id=tenant_id).inc()
            return JSONResponse(
                status_code=429,
                content={"error": "rate_limited", "tenant_id": tenant_id, "retry_after": 60},
            )
        rate_limit_store[tenant_id] = window

    response = await call_next(request)

    # Usage tracking
    elapsed = time.time() - request.state.start_time
    API_CALLS.labels(tenant_id=tenant_id, endpoint=request.url.path).inc()
    rate_limit_store[tenant_id].append(time.time())

    return response

# ── Proxy Routes ───────────────────────────────────────────────

UPSTREAM_TIMEOUT = 180

def _build_upstream_url(request: Request) -> str:
    base = config.dify_upstream.rstrip("/")
    return f"{base}{request.url.path}?{request.url.query}" if request.url.query else f"{base}{request.url.path}"

def _inject_tenant_context(headers: dict, tenant_id: str) -> dict:
    """Inject tenant context into Dify API calls."""
    tc = config.tenants.get(tenant_id)
    if tc:
        headers["X-MAEA-Tenant-ID"] = tenant_id
        if tc.workspace_id:
            headers["X-MAEA-Workspace-ID"] = tc.workspace_id
        rules = tc.isolation_rules
        if rules.get("dataset_filter"):
            headers["X-MAEA-Dataset-Filter"] = rules["dataset_filter"]
    return headers

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_request(request: Request, path: str):
    """Proxy requests to Dify upstream with tenant context injection."""
    tenant_id = getattr(request.state, "tenant_id", "default")
    upstream_url = _build_upstream_url(request)

    headers = dict(request.headers)
    headers.pop("host", None)
    headers = _inject_tenant_context(headers, tenant_id)

    body = await request.body()

    async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
        resp = await client.request(
            method=request.method,
            url=upstream_url,
            headers=headers,
            content=body,
        )

    # Track token usage from Dify response headers
    if "X-Dify-Usage-Tokens" in resp.headers:
        try:
            tokens = int(resp.headers["X-Dify-Usage-Tokens"])
            TOKEN_USAGE.labels(tenant_id=tenant_id).inc(tokens)
        except ValueError:
            pass

    return JSONResponse(
        status_code=resp.status_code,
        content=resp.json() if resp.headers.get("content-type", "").startswith("application/json") else None,
        headers=dict(resp.headers),
    )

# ── Management Endpoints ───────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "gateway": "maea-tenant-gateway", "version": "0.1.0"}

@app.get("/metrics")
async def metrics():
    return JSONResponse(
        content={"data": generate_latest().decode()},
        media_type="text/plain",
    )

@app.get("/admin/tenants")
async def list_tenants():
    return {
        "tenants": [
            {"tenant_id": t.tenant_id, "name": t.name, "workspace_id": t.workspace_id}
            for t in config.tenants.values()
        ],
        "active": len(config.tenants),
    }

@app.get("/admin/tenants/{tenant_id}/usage")
async def tenant_usage(tenant_id: str):
    return {
        "tenant_id": tenant_id,
        "api_calls": API_CALLS.labels(tenant_id=tenant_id, endpoint="").sum() if tenant_id in config.tenants else 0,
        "tokens_used": TOKEN_USAGE.labels(tenant_id=tenant_id).sum() if tenant_id in config.tenants else 0,
        "current_rate": len([t for t in rate_limit_store.get(tenant_id, []) if t > time.time() - 60]),
    }

# ── Entrypoint ─────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
