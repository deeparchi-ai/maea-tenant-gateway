# MAEA Multi-Tenant Gateway

Enterprise isolation middleware for Dify. Sits in front of the Dify API to provide tenant-aware routing, SSO/OIDC authentication, usage metering, and audit trails.

Part of the [MAEA](https://deeparchi.ai) framework by [DeepArchi](https://deeparchi.ai).

## Why This Exists

Dify's workspace model provides basic separation but enterprises need more: department data isolation, per-tenant SSO, usage-based chargeback, and non-repudiable audit trails. This gateway adds those layers without modifying Dify.

## Architecture

```
Browser/Client
    │
    ▼
┌──────────────────────┐
│  MAEA Tenant Gateway │  ← JWT → tenant resolution
│  - Auth (JWT/OIDC)   │  ← Header → isolation rules
│  - Rate limiting      │  ← Metrics → Prometheus
│  - Usage metering     │
│  - Audit logging      │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│     Dify API         │
│  (unmodified)        │
└──────────────────────┘
```

## Quick Start

```bash
pip install -e .
cp config/tenants.yaml.example config/tenants.yaml
# Edit tenants.yaml with your Dify upstream URL and tenant configs
uvicorn maea_gateway.main:app --host 0.0.0.0 --port 8080
```

Or with Docker:

```bash
docker build -t maea-tenant-gateway .
docker run -p 8080:8080 -v ./config:/etc/maea maea-tenant-gateway
```

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check |
| `GET /metrics` | Prometheus metrics |
| `GET /admin/tenants` | List configured tenants |
| `GET /admin/tenants/{id}/usage` | Per-tenant usage stats |
| `ANY /{path}` | Proxied to Dify upstream with tenant injection |

## License

MIT © 2026 DeepArchi
