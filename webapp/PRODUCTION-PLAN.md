# Production plan: authentication and user job history

## Goal

Evolve the anonymous demo into a multi-tenant engineering-review application. A signed-in user can upload an IFC model once, run either engine, return later to see its job/report, and share a project with permitted colleagues.

## Target architecture

```
browser
  -> TLS / API gateway
      -> web SPA
      -> identity provider       (login, logout, refresh)
      -> application API         (projects, models, jobs, reports)
          -> PostgreSQL          (ownership, job state, audit events)
          -> object storage      (IFC, generated IFC, JSON/PDF reports)
          -> queue
              -> ifcinject worker
              -> BNBC checker worker
```

Only the gateway is public. The database, queue, object storage and workers remain private services. Do not expose `ifcweb` or `bnbc-web` directly.

## Login and authorization

Use OpenID Connect rather than custom password handling. Keycloak is a good self-hosted option; Auth0, Clerk, and Firebase Authentication are managed alternatives.

- Use authorization-code flow with PKCE for the browser.
- Store short-lived access tokens and rotating refresh tokens in `HttpOnly`, `Secure`, `SameSite=Lax` cookies.
- Enable email verification, password reset and MFA for administrators.
- The gateway/application API validates the token and forwards verified `user_id`, `organization_id`, and roles internally.
- Internal services never accept a browser-supplied user id as authority.

Do not implement a custom plaintext-password database or keep JWTs in browser local storage. A login screen is simple; safe credential and session handling is not.

### Roles

| Role | Capabilities |
|---|---|
| Organization admin | Manage members, projects, retention and settings |
| Engineer | Upload models, submit jobs, view/download project reports |
| Reviewer | View project models/reports and, later, comment on issues |
| Client/viewer | Read-only access to explicitly shared reports |

Authorization is organization- and project-scoped: users must have project membership before they can access a model, job, report or download.

## Persistent user history

Show **My work** (recent jobs) and **Model history** (all versions and their reports). Store metadata in PostgreSQL and binary files in object storage.

```
organizations 1--* memberships *--1 users
organizations 1--* projects 1--* models 1--* model_versions
model_versions 1--* jobs 1--* job_events
jobs 1--* reports
```

| Entity | Minimum fields |
|---|---|
| `users` | identity-provider subject, email, display name, created time |
| `memberships` | organization, user, role, active flag |
| `projects` | organization, name, archive state |
| `model_versions` | project, uploader, source name, checksum, schema, object key, size |
| `jobs` | model version, requester, engine, requested rules, status, timestamps, error |
| `job_events` | job, state, time, progress detail, correlation id |
| `reports` | job, artifact key, result summary, completed time |

Every query filters by verified organization and project membership. Downloads are short-lived signed URLs issued only after authorization.

## Service changes

### Application API (new)

Add a FastAPI service which owns PostgreSQL transactions and exposes:

```
POST /api/projects
GET  /api/projects
POST /api/projects/{project_id}/models
GET  /api/models/{model_id}/versions
POST /api/model-versions/{version_id}/jobs
GET  /api/jobs/{job_id}
GET  /api/me/history
GET  /api/reports/{report_id}/download
```

The SPA uses this API for user-facing work. It creates queue jobs; it does not parse IFC files or run rule checkers.

### Shared storage and workers

Replace the separate `JobStore` upload folders with one object-storage object per model version. The browser uploads once; jobs reference an immutable model-version ID, not a local filename. This removes the current double upload to `/ifc/` and `/bnbc/`.

Convert the current in-memory worker loops into queue consumers. Each worker claims a job, downloads input, runs in a memory/CPU/time-limited process, writes artifacts/reports to storage, updates the durable job record, and publishes progress. The existing checking and injection logic remains reusable inside those workers.

### Gateway and user interface

Keep nginx initially and add token validation, rate limiting, request IDs, TLS/security headers and an SSE endpoint for progress (replacing polling).

Add these SPA views before considering a framework migration:

1. **Sign in** — redirect to the identity provider, not a custom password form.
2. **Project picker** — select/create a project before upload.
3. **My work** — recent jobs, status, engine, model version, requester and report link.
4. **Model history** — immutable model versions and report comparison.
5. **Job details** — retain the current helpful checker explanations plus live progress.

Preserve the existing rule tables and plain-language violation explanations. Add reusable checking profiles such as Architectural, Structural, Fire Safety, or custom selections.

## Delivery phases

### Phase 1 — secure foundation

- Add Keycloak/managed OIDC, PostgreSQL, database migrations, and the application API.
- Add organizations, memberships, projects, and an authenticated SPA shell.
- Keep current engine services behind nginx; pass only verified identity claims.

### Phase 2 — persistent history

- Add MinIO/S3 for model versions, generated files and reports.
- Add My work and Model history pages.
- Make PostgreSQL the job source of truth; worker directories are temporary scratch space.

### Phase 3 — reliable processing

- Add RabbitMQ or Redis and durable workers.
- Add retries, cancellation, dead-letter handling, quotas and SSE progress.
- Isolate every checker in a subprocess/container with hard resource limits.

### Phase 4 — operations

- Add audit logs, backups, retention/deletion controls, monitoring, alerts, malware scanning, report sharing, and Bengali UI support if required.

## Local development stack

In addition to the existing `frontend`, `ifcweb`, and `bnbc-web` images, add:

```
postgres        metadata and job state
minio           local S3-compatible artifact storage
keycloak        OpenID Connect identity provider
application-api projects, models, jobs, history and authorization
queue           RabbitMQ or Redis
ifc-worker      injected-case processing
bnbc-worker     accepted-checker processing
```

Keep database credentials, OIDC secrets, signing keys and storage credentials outside Git.

## Acceptance criteria

- Unauthenticated users cannot upload, view, or download project assets.
- A model is uploaded once and used by either workflow.
- Completed reports survive every service restart.
- Users from one organization cannot access another organization's data.
- Worker crashes preserve a visible retry/failure state in job history.
- Artifact downloads are authorized and expire.

