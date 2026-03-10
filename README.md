# ace-gateway

Minimal gateway and control-plane repo for the ACE VPS deployment.

This repository is intentionally smaller than the full monorepo. It contains only the pieces the VPS needs to:

- authenticate gateway requests
- proxy requests to per-user runtimes
- manage runtime lifecycle operations such as provision, deploy, restart, and rollback
- persist orchestration and messaging metadata

It does not contain the native client app or the rest of the general ACE application stack.

## Layout

- `gateway/`: FastAPI gateway entrypoint and proxy/auth/runtime routes
- `ace_net_manager/`: control-plane API and Docker orchestration services
- `db/`: SQLAlchemy models, schema bootstrap, and migrations used by the control plane
- `docker-compose.vps.yml`: VPS deployment for `gateway` and `ace-net-manager`
- `Dockerfile.control-plane`: shared image for the control-plane services
- `deploy_ace_backend_to_vps.sh`: rsync + compose deployment helper
- `start_hermes_server.sh`: optional standalone Hermes HTTP entrypoint
- `tests/`: gateway and orchestration tests relevant to this repo

## VPS Deployment

1. Copy `.env.example` to `.env` and fill in the required values.
2. Run `./deploy_ace_backend_to_vps.sh`.

That deploy script syncs this repo to the VPS and starts:

- `gateway`
- `ace-net-manager`

The per-user runtime image is not built from this repository on the VPS. The manager pulls the configured runtime image tag and launches those containers through Docker.

## Local Checks

- `pytest tests/test_auth_unit.py tests/test_gateway_runtime_unit.py tests/test_gateway_messaging_unit.py -q`
- `pytest tests/orchestration -q`
