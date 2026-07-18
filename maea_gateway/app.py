"""
MAEA Tenant Gateway — Enterprise Multi-Tenant Middleware for Dify.

Sits between clients and Dify API, providing:
  1. JWT/OIDC-based tenant resolution
  2. Per-tenant data isolation (dataset/app filtering)
  3. Usage metering (token/storage/API calls)
  4. Audit trail logging

Part of the MAEA framework by DeepArchi (deeparchi.ai).
"""

import json
import time
from collections import defaultdict
from typing import Any

import httpx
import jwt
import yaml
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Histogram, generate_latest

app = FastAPI(
    title="MAEA Tenant Gateway",
    version="0.1.0",
    description="Enterprise multi-tenant isolation middleware for Dify",
)

# ── Metrics ──────────────────────────────────────────────
request_count = Counter("maea_requests", "Total requests", ["tenant", "method", "endpoint"])
request_latency = Histogram("maea_latency_seconds", "Request latency", ["tenant"])
token_usage = Counter("maea_token_usage", "Token usage", ["tenant", "model"])


# ── Tenant Store ─────────────────────────────────────────
class TenantStore:
    """In-memory tenant config. Replace with DB for production."""

    def __init__(self, config_path: str = "config/tenants.yaml"):
        self.tenants: dict[str, dict] = {}
        self._usage: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        if config_path:
            self._load(config_path)

    def _load(self, path: str):
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
                self.tenants = data.get("tenants", {})
        except FileNotFoundError:
            pass

    def get(self, tenant_id: str) -> dict | None:
        return self.tenants.get(tenant_id)

    def record_usage(self, tenant_id: str, tokens: int, model: str = "unknown"):
        self._usage[tenant_id][f"tokens_{model}"] += tokens
        token_usage.labels(tenant=tenant_id, model=model).inc(tokens)

    def get_usage(self, tenant_id: str) -> dict:
        return dict(self._usage.get(tenant_id, {}))


store = TenantStore()
JWT_SECRETS: dict[str, str] = {}
DIFY_UPSTREAM = "http://localhost:5001"


# ── Auth Middleware ───────────────────────────────────────
@app.middleware("http")
async def tenant_middleware(request: Request, call_next):
    """Extract tenant identity from JWT Bearer token or X-Tenant-ID header."""
    start = time.time()
    tenant_id = request.headers.get("X-Tenant-ID", "")

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        try:
            for tid, secret in JWT_SECRETS.items():
                try:
                    payload = jwt.decode(token, secret, algorithms=["HS256"])
                    tenant_id = payload.get("tenant_id", tid)
                    request.state.user = payload.get("sub", "")
                    request.state.roles = payload.get("roles", [])
                    break
                except jwt.InvalidTokenError:
                    continue
            else:
                payload = jwt.decode(token, options={"verify_signature": False})
                tenant_id = payload.get("tenant_id", "")
                request.state.user = payload.get("sub", "")
        except Exception:
            pass

    if not tenant_id:
        tenant_id = request.query_params.get("tenant_id", "default")

    request.state.tenant_id = tenant_id
    request.state.tenant_config = store.get(tenant_id) or {}

    request_count.labels(tenant=tenant_id, method=request.method, endpoint=request.url.path).inc()
    response = await call_next(request)
    request_latency.labels(tenant=tenant_id).observe(time.time() - start)
    return response


# ── Dify API Proxy ───────────────────────────────────────
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_to_dify(request: Request, path: str):
    """Proxy requests to Dify API with tenant isolation."""
    tenant_id = getattr(request.state, "tenant_id", "default")
    tenant_config = getattr(request.state, "tenant_config", {})

    url = f"{DIFY_UPSTREAM.rstrip('/')}/{path}"
    headers = dict(request.headers)
    for h in ("host", "transfer-encoding"):
        headers.pop(h, None)

    if tenant_config:
        headers["X-MAEA-Tenant-ID"] = tenant_id
        iso = tenant_config.get("isolation", {})
        headers["X-MAEA-Dataset-Filter"] = iso.get("dataset_filter", "")
        headers["X-MAEA-App-Visibility"] = iso.get("app_visibility", "workspace_only")

    body = await request.body()
    if path.startswith("v1/chat-messages") or path.startswith("v1/workflows/run"):
        try:
            payload = json.loads(body) if body else {}
            if "user" not in payload:
                payload["user"] = f"{tenant_id}-user"
            body = json.dumps(payload).encode()
        except Exception:
            pass

    async with httpx.AsyncClient(timeout=180.0) as client:
        try:
            upstream_resp = await client.request(
                method=request.method, url=url, headers=headers,
                content=body, params=dict(request.query_params),
            )
            # Track token usage
            if "application/json" in upstream_resp.headers.get("content-type", ""):
                try:
                    data = upstream_resp.json()
                    usage = data.get("metadata", {}).get("usage", {})
                    if usage:
                        store.record_usage(tenant_id, usage.get("total_tokens", 0),
                                          usage.get("model_name", "unknown"))
                except Exception:
                    pass
            response = Response(content=upstream_resp.content,
                                status_code=upstream_resp.status_code,
                                headers=dict(upstream_resp.headers))
            response.headers.pop("transfer-encoding", None)
            return response
        except httpx.ConnectError:
            return JSONResponse(status_code=502, content={"detail": f"Dify unreachable: {url}"})
        except httpx.TimeoutException:
            return JSONResponse(status_code=504, content={"detail": f"Dify timeout: {url}"})


# ── Management API ───────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "app": "MAEA Tenant Gateway", "version": "0.1.0"}

@app.get("/admin/tenants")
async def list_tenants():
    return {"tenants": list(store.tenants.keys())}

@app.get("/admin/tenants/{tenant_id}")
async def get_tenant(tenant_id: str):
    t = store.get(tenant_id)
    if not t:
        raise HTTPException(404, f"Tenant {tenant_id} not found")
    return {"tenant_id": tenant_id, **t}

@app.get("/admin/tenants/{tenant_id}/usage")
async def get_tenant_usage(tenant_id: str):
    return {"tenant_id": tenant_id, "usage": store.get_usage(tenant_id)}

@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type="text/plain")

@app.post("/admin/reload")
async def reload_config():
    store._load("config/tenants.yaml")
    return {"ok": True, "count": len(store.tenants)}
