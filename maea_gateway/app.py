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

from maea_gateway.audit import audit_log
from maea_gateway.rate_limiter import limiter
from maea_gateway.dify_agent_proxy import DifyAgentProxy

agent_proxy = DifyAgentProxy()

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



@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_to_dify(request: Request, path: str):
    """Proxy requests to Dify API with tenant isolation, rate limiting, and audit."""
    t0 = time.time()
    tenant_id = getattr(request.state, "tenant_id", "default")
    tenant_config = getattr(request.state, "tenant_config", {})
    user = getattr(request.state, "user", "")
    client_ip = request.client.host if request.client else ""

    # ── Dify-Agent proxy path ──────────────────────────
    if path.startswith("dify-agent/"):
        agent_ctx = agent_proxy.extract_agent_context(request)
        body = await request.body()
        allowed, reason = limiter.check(tenant_id, tenant_config,
                                         estimated_tokens=1000)
        if not allowed:
            return JSONResponse(status_code=429, content={"detail": reason})

        t1 = time.time()
        upstream_path = path[len("dify-agent/"):]
        agent_url = f"{DIFY_UPSTREAM.rstrip('/')}/{upstream_path}"
        agent_headers = dict(request.headers)
        for h in ("host", "transfer-encoding"):
            agent_headers.pop(h, None)
        if agent_ctx["traceparent"]:
            agent_headers["traceparent"] = agent_ctx["traceparent"]

        async with httpx.AsyncClient(timeout=300.0) as client:
            try:
                agent_resp = await client.request(
                    method=request.method, url=agent_url,
                    headers=agent_headers, content=body,
                )
                agent_status = agent_resp.status_code
                agent_response = Response(
                    content=agent_resp.content,
                    status_code=agent_status,
                    headers=dict(agent_resp.headers),
                )
                agent_response.headers.pop("transfer-encoding", None)
                if "application/json" in agent_resp.headers.get(
                        "content-type", ""):
                    try:
                        resp_json = agent_resp.json()
                        agent_proxy.record_tool_call(
                            agent_ctx, resp_json, tenant_id,
                            (time.time() - t1) * 1000,
                        )
                    except Exception:
                        pass
                return agent_response
            except httpx.ConnectError:
                return JSONResponse(
                    status_code=502,
                    content={"detail": f"Plugin daemon unreachable: {agent_url}"},
                )
            except httpx.TimeoutException:
                return JSONResponse(
                    status_code=504,
                    content={"detail": f"Plugin daemon timeout: {agent_url}"},
                )

    # ── Dify API Proxy ─────────────────────────────────
    # ── Rate limiting ──────────────────────────────────
    allowed, reason = limiter.check(tenant_id, tenant_config)
    if not allowed:
        audit_log(tenant_id=tenant_id, user=user, method=request.method,
                  path=path, status_code=429, latency_ms=0,
                  client_ip=client_ip, rate_limited=True,
                  upstream=DIFY_UPSTREAM,
                  user_agent=request.headers.get("user-agent", ""))
        return JSONResponse(status_code=429, content={"detail": reason})

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
            status = upstream_resp.status_code
            response = Response(content=upstream_resp.content,
                                status_code=status,
                                headers=dict(upstream_resp.headers))
            response.headers.pop("transfer-encoding", None)

            # ── Audit log ───────────────────────────
            audit_log(tenant_id=tenant_id, user=user,
                      method=request.method, path=path,
                      status_code=status,
                      latency_ms=(time.time() - t0) * 1000,
                      client_ip=client_ip,
                      upstream=DIFY_UPSTREAM,
                      user_agent=request.headers.get("user-agent", ""))
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
