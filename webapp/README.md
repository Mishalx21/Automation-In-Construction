# Capstone Web App — one frontend, two engine services

A browser UI over both capstone engines:

- **ifcinject** (`IFC-Test-Case-Generator`) — inject documented building-code
  violations into an uploaded IFC model, then independently re-verify the
  written file. Deterministic, no LLM.
- **bnbc-web** (`BNBC-Checking-Code-Generator/bnbcweb`) — run the **accepted**
  BNBC rule checkers (`rule-*/check_*.py`, produced offline by the FIV agentic
  pipeline) against an uploaded IFC model. No LLM at request time; the image
  carries no LLM SDKs and needs no API keys.

Architecture: **two engine services + one nginx frontend**, wired with
docker-compose. Each engine is internally a modular monolith with a single
sequential worker (ifcopenshell holds whole 342 MB files in memory — the
deliberate design both engines already use). They are separate services
because their failure modes and dependency sets differ: one parses huge IFC
files, the other must never depend on the LLM stack.

```
browser → :8080 nginx ─┬─ / → static SPA
                       ├─ /ifc/...  → ifcweb:8000     (existing Dockerfile)
                       └─ /bnbc/... → bnbc-web:8000   (new Dockerfile)
```

## Run it

Requires Docker Desktop (WSL2 backend on Windows). From this directory:

```bash
docker compose up --build
# → http://localhost:8080
```

First build is slow (ifcopenshell images are ~1 GB each). Subsequent
builds are cached.

## Deploying later (VPS)

The same compose file runs in production:

```bash
git pull && docker compose up -d --build
```

Put TLS in front (Caddy/nginx/certbot) — the frontend listens on :8080.
The two engine services are `expose`-only, never published to the host.
No authentication anywhere: treat it as a demo tool, not a public service.

## Moving to a multi-user application

The current stack is intentionally an anonymous demonstration. The production
architecture for login, organization/project access control, durable user job
history, shared IFC storage, and background workers is documented in
[PRODUCTION-PLAN.md](PRODUCTION-PLAN.md). It keeps the two existing engines
but moves identity, job ownership, and persistence into shared platform
services.

## File layout

```
webapp/
  docker-compose.yml       the three services + volumes
  frontend/
    Dockerfile             nginx:alpine + static + proxy config
    nginx.conf             /ifc/ and /bnbc/ reverse proxies, 400 MB body limit
    static/                the SPA (vanilla JS, no build step)
```

The bnbc-web service itself lives with its engine, next to the code it runs:
`../BNBC-Checking-Code-Generator/bnbcweb/` (app.py, jobs.py, schemas.py)
plus that repo's `Dockerfile`, `requirements-web.txt`, `.dockerignore`.

## Design notes / known limitations

- **The file is uploaded twice** if you use both panels on the same model —
  each engine has its own upload store and in-memory job registry. Sharing
  one upload across services is the natural next step (a shared volume or
  object store both work); deliberately not done yet to keep each service
  standalone.
- **Job state is in-memory.** Restarting a service drops its job registry
  (uploads survive in the volume). Fine for a demo; add Redis/Postgres only
  when it actually hurts.
- **No per-checker timeout.** A hung checker blocks the single worker queue
  for that service. If that ever happens in practice, isolate checkers in
  subprocesses with a wall-clock limit.
- **CORS: none.** Everything is same-origin through the nginx proxy.
