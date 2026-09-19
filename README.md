# IFC Compliance Workbench

A capstone project for IFC-based building-code compliance work. It provides a browser interface for two complementary workflows:

1. **Check compliance** — run accepted BNBC rule checkers against an IFC model.
2. **Create test cases** — inject documented building-code violations into an IFC model and independently verify the generated fixture.

The application is designed for engineering review: reports identify violated rules, affected IFC elements, measurements, rule references, and practical explanations.

## What it does

### Compliance checking

Upload an IFC file, select BNBC checks, and receive a structured report with one of four verdicts per rule:

- **pass** — the rule was checked and no violation was found.
- **fail** — one or more violations were found.
- **unknown** — the IFC lacks the data required for a reliable result.
- **not applicable** — the rule does not apply to the model.

The accepted checkers cover architectural and structural rules, including door width, stair geometry, fire ratings, accessible door clearance, dead-end corridors, beam proportions, column dimensions, slab thickness, floating columns, and soft storeys.

### Test-case generation

Upload an IFC file, let the system determine which rules can be applied, choose violations to inject, and download:

- a modified IFC file containing the documented violation(s);
- an independent verification report;
- mutation details such as target GUID, before/after values, and source clause.

## Architecture

```text
Browser
  │
  ▼
Nginx gateway (:8080)
  ├── Frontend SPA          HTML, CSS, vanilla JavaScript
  ├── Auth API              FastAPI: account/session/history
  ├── IFC service           FastAPI: analyse, inject, verify
  └── BNBC service          FastAPI: run accepted checkers
          │
          └── PostgreSQL    users, sessions, completed-job history
```

Nginx is the only public service. The internal FastAPI services and PostgreSQL communicate over Docker's private network.

## Technology stack

| Area | Technology |
|---|---|
| Frontend | HTML5, CSS3, vanilla JavaScript |
| API services | Python 3.12, FastAPI, Uvicorn |
| IFC processing | IfcOpenShell |
| Personal-use 3D preview | xeokit SDK (AGPLv3) + web-ifc |
| Authentication | Email/password accounts, scrypt password hashes, HttpOnly sessions |
| Database | PostgreSQL 16 |
| Gateway | Nginx |
| Deployment | Docker, Docker Compose, Docker Desktop/WSL2 |

## Repository layout

```text
BNBC-Checking-Code-Generator/  Fixture-Injected Verification pipeline and BNBC checker service
ifc-fault-injector/            IFC violation-injection engine and service
webapp/                         Unified frontend, Nginx gateway, auth service, Compose stack
  auth/                         FastAPI password authentication and history service
  frontend/                     Static SPA and Nginx configuration
  PRODUCTION-PLAN.md            Multi-user and production evolution plan
```

## Run locally

Prerequisites:

- Docker Desktop with the Linux engine running
- Docker Compose

From the web application directory:

```powershell
cd webapp
docker compose up --build
```

Open [http://localhost:8080](http://localhost:8080).

The first startup downloads/builds the required images. Create an account from the sign-in screen, then upload an IFC model and choose a workflow.

To stop the local stack:

```powershell
docker compose down
```

To remove local containers, database data, and uploads as well:

```powershell
docker compose down -v
```

> This last command removes local development data.

## Multi-user behavior

- Users register and sign in with email and password.
- Passwords are stored only as salted `scrypt` hashes.
- Browser sessions use `HttpOnly` cookies.
- Nginx requires an authenticated session before allowing access to IFC or BNBC APIs.
- PostgreSQL keeps each signed-in user's completed-work history.

## Design principles

- **Separation of concerns:** authentication, IFC injection, compliance checking, and UI are separate services.
- **Independent verification:** injected test cases are verified independently after writing the IFC file.
- **Fail-safe reporting:** missing model information produces `unknown`, rather than an unsafe `pass`.
- **Least exposure:** only Nginx publishes a host port; application services remain internal.
- **Reproducible deployment:** Docker Compose defines the development environment.

## Current limitations and next steps

The system is a strong capstone prototype, not yet a high-scale production platform.

- IFC engine jobs are currently held in memory and processed sequentially to control memory use.
- Job history records completed actions, but the engines do not yet have durable, database-backed job queues.
- Large IFC models are uploaded separately to each current engine workflow.
- Production deployment should enable HTTPS and set `COOKIE_SECURE=true`.
- A larger deployment should add S3/MinIO object storage, RabbitMQ/Redis queues, worker retries, organization/project roles, audit logs, backups, monitoring, and engine-level job ownership enforcement.

See [webapp/PRODUCTION-PLAN.md](webapp/PRODUCTION-PLAN.md) for the proposed production architecture.

## Notes on IFC assets

Large `.ifc` source models and fixtures are intentionally excluded from normal Git history. Keep them in approved external storage or use Git LFS only where sufficient storage quota is available.

## Viewer license

The in-browser IFC preview uses the xeokit SDK under AGPLv3 for personal use.
Before distributing a closed-source or commercial version, review the xeokit
license and obtain a commercial license if necessary.
