# MAEA Tenant Gateway — Enterprise Multi-Tenancy for Dify

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-green.svg)](https://python.org)

Enterprise multi-tenant isolation middleware for [Dify](https://dify.ai).  
Part of the [MAEA](https://deeparchi.ai) framework by [DeepArchi](https://deeparchi.ai).

## Why

Dify provides a `tenant_id` column — enough for a single organization, but not enough for:

- **Authenticating tenants** — no JWT/OIDC enforcement at the API layer
- **Enforcing per-tenant quotas** — rate limits are global, not per tenant
- **Auditing who did what** — logs lack tenant context and structured event format
- **Metering usage** — no built-in per-tenant token/API-call tracking for billing

MAEA Tenant Gateway adds these without touching Dify's codebase.  
It sits **in front** of Dify as a reverse proxy — zero code changes required.

## What It Does

| Capability | Description |
|------------|-------------|
| **JWT/OIDC Auth** | Resolve tenant from JWT bearer tokens (Azure AD, Okta, custom IdP) |
| **Tenant Isolation** | Per-tenant dataset filtering, app visibility control |
| **Rate Limiting** | Per-tenant token-bucket (requests/min, tokens/day) with 429 responses |
| **Audit Trail** | Structured JSON audit log (every request → stdout) for SIEM/Vector/Fluentd |
| **Usage Metering** | Track token consumption, API calls, and latency per tenant |
| **Prometheus Metrics** | Built-in `/metrics` endpoint for Grafana dashboards |

## Architecture

```
Client (JWT token)
    │
    ▼
┌──────────────────────────────────┐
│     MAEA Tenant Gateway          │  ← Port 8080
│  ├─ Auth (JWT/OIDC)              │
│  ├─ Tenant Resolver              │
│  ├─ Rate Limiter (token-bucket)  │
│  ├─ Audit Logger (JSON → stdout) │
│  └─ Usage Metering + Prometheus  │
└──────────────┬───────────────────┘
               │ X-MAEA-Tenant-ID, X-MAEA-Dataset-Filter
               ▼
┌──────────────────────────────────┐
│         Dify API                 │  ← Port 5001
└──────────────────────────────────┘
```

## Quick Start

```bash
pip install -e .
uvicorn maea_gateway.app:app --host 0.0.0.0 --port 8080
```

Point your Dify clients to `http://localhost:8080` instead of Dify directly.

## Deploy with Docker Compose

```yaml
# docker-compose.yml
services:
  gateway:
    build: .
    ports:
      - "8080:8080"
    environment:
      - DIFY_UPSTREAM=http://dify-api:5001
    volumes:
      - ./config:/app/config
    restart: always

  dify-api:
    image: langgenius/dify-api:1.16.0
    # ... standard Dify config
```

```bash
docker compose up -d
# Gateway at :8080, Dify at :5001 (internal only)
```

## Configuration

Edit `config/tenants.yaml`:

```yaml
tenants:
  finance:
    workspace_id: "ws_finance_001"
    sso:
      provider: "azure_ad"
      tenant_id: "contoso.com"
    isolation:
      dataset_filter: "tenant=finance"
      app_visibility: "workspace_only"
    rate_limits:
      max_requests_per_minute: 600
      max_tokens_per_day: 5000000

  engineering:
    workspace_id: "ws_eng_002"
    isolation:
      dataset_filter: "tenant=engineering"
    rate_limits:
      max_requests_per_minute: 300
      max_tokens_per_day: 2000000
```

## Management Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check |
| `GET /admin/tenants` | List all tenants |
| `GET /admin/tenants/{id}` | Get tenant config |
| `GET /admin/tenants/{id}/usage` | Per-tenant usage stats |
| `GET /metrics` | Prometheus metrics |
| `POST /admin/reload` | Hot-reload tenant config |

## Audit Log Format

Every proxied request emits one JSON line to stdout:

```json
{"timestamp":"2026-07-21T12:00:00Z","tenant_id":"finance","user":"alice@corp.com","method":"POST","path":"v1/chat-messages","status_code":200,"latency_ms":342.5,"client_ip":"10.0.1.5","rate_limited":false,"upstream":"http://dify-api:5001","user_agent":"Dify-Web/1.16"}
```

Pipe to Vector, Fluentd, or any JSON-line consumer.

## Integration with MAEA

```
┌───────────────────────────────────────┐
│           MAEA Governance             │
│  ┌──────────┐ ┌──────────┐ ┌───────┐ │
│  │Trust Tier│ │A2A Bridge│ │Tenant │ │
│  │          │ │          │ │Gateway│ │
│  └──────────┘ └──────────┘ └───┬───┘ │
└────────────────────────────────┼─────┘
                                 │
                    ┌────────────▼──────────┐
                    │    Dify (Build)        │
                    └───────────────────────┘
```

## Roadmap

- [ ] **A2A Agent Governance** — extend MAEA CostLimiter + AuditLogger to Dify Agent nodes
- [ ] **W3C Traceparent** — distributed tracing across Gateway → Dify → MCP tools
- [ ] **Dify Plugin** — native Dify marketplace plugin for one-click integration
- [ ] **Redis Backend** — shared rate-limit state for multi-process deployments
- [ ] **gRPC Auth Proxy** — sub-millisecond tenant resolution for high-throughput deployments

## License

MIT © 2026 DeepArchi
