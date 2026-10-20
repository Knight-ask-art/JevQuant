# Kubernetes skeleton

Not a production manifest set. It shows the intended object shape and the default
posture: **paper mode, synthetic market, live flags off, no secrets in git**.

## Objects

| File | Contains |
|---|---|
| `namespace.yaml` | `jev-trading` namespace |
| `configmap.yaml` | non-secret settings; paper mode, live flags off |
| `secret.example.yaml` | template only — create the real Secret with `kubectl` |
| `api.yaml` | API Deployment (1 replica) + Service |
| `web.yaml` | static UI Deployment (2 replicas) + Service |
| `ingress.yaml` | `/api`, `/ws` → API; `/` → UI |

## Assumptions

- PostgreSQL and Redis are managed services (or separately deployed); this skeleton
  does not run stateful workloads in the cluster.
- Images are built from `deploy/api.Dockerfile` and `frontend/Dockerfile` and pushed to
  your registry; change `image:` to the real reference.
- The API must run as a **single replica** while the scheduler and in-memory arming
  token live in-process. Scaling out requires the split-worker topology described in
  `docs/architecture.md`.
- Ingress must allow WebSocket upgrades (`/ws`); the annotations raise proxy timeouts.

## Apply

```bash
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/configmap.yaml
kubectl -n jev-trading create secret generic jev-secrets \
  --from-literal=JEV_DATABASE_URL='postgresql+asyncpg://...' \
  --from-literal=JEV_REDIS_URL='redis://...' \
  --from-literal=JEV_ADMIN_API_KEY='...' \
  --from-literal=JEV_INTERNAL_EXECUTION_KEY='...' \
  --from-literal=JEV_MFA_TOTP_SECRET='...'
kubectl apply -f deploy/k8s/api.yaml -f deploy/k8s/web.yaml -f deploy/k8s/ingress.yaml
```

Live mode still requires `JEV_TRADING_MODE=live`, `JEV_ENABLE_LIVE_TRADING=true`,
`JEV_EXECUTION_BACKEND=hummingbot`, a passing preflight and the operator arming
handshake. Do not put those flags in the ConfigMap; treat arming as a runtime action.

## Environment note

`configmap.yaml` uses `JEV_APP_ENV=prod` with `JEV_SYNTHETIC_MARKET=false`, so the cluster
must reach a real market-data endpoint; production refuses to boot with synthetic data
enabled. If your egress is restricted (this project's own environment gets HTTP 451 from
`api.binance.com`), deploy a smoke instance with `JEV_APP_ENV=dev` and
`JEV_SYNTHETIC_MARKET=true` instead — do not proxy around the block.
