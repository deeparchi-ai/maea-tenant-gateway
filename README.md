# MAEA Tenant Gateway

Enterprise multi-tenant isolation middleware for Dify. Part of the [MAEA](https://deeparchi.ai) framework by [DeepArchi](https://deeparchi.ai).

## What It Does

Sits between your clients and Dify API, adding enterprise multi-tenancy:

| Capability | Description |
|------------|-------------|
| **JWT/OIDC Auth** | Resolve tenant from JWT bearer tokens (Azure AD, Okta, custom IdP) |
| **Tenant Isolation** | Per-tenant dataset filtering, app visibility control |
| **Usage Metering** | Track token consumption, API calls, and latency per tenant |
| **Prometheus Metrics** | Built-in `/metrics` endpoint for Grafana dashboards |
| **Audit Trail** | Request logging with tenant context |

## Quick Start

```bash
pip install -e .
uvicorn maea_gateway.app:app --host 0.0.0.0 --port 8080
```

Then point your Dify clients to `http://localhost:8080` instead of Dify directly.

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
    rate_limits:
      max_tokens_per_day: 5000000
```

## Architecture

```
Client (JWT token)
    │
    ▼
┌──────────────────────┐
│ MAEA Tenant Gateway  │  ← Port 8080
│  ├─ Auth (JWT/OIDC)  │
│  ├─ Tenant Resolver  │
│  ├─ Usage Metering   │
│  └─ Audit Trail      │
└──────────┬───────────┘
           │ X-MAEA-Tenant-ID, X-MAEA-Dataset-Filter
           ▼
┌──────────────────────┐
│    Dify API          │  ← Port 5001
└──────────────────────┘
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

## License

MIT © 2026 DeepArchi
