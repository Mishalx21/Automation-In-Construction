# IFC Compliance Workbench web application

The web application is an authenticated browser interface for two IFC
workflows:

- **Check compliance** runs accepted BNBC checkers against an uploaded IFC
  model and presents pass, fail, unknown, or not-applicable results.
- **Create test cases** uses `ifcfault` to inject selected, known violations,
  independently verify the generated model, and provide the resulting files.

Users select a model once and choose the workflow they need. Completed work is
recorded in their account history.

## Run locally

Requirements: Docker Desktop with the Linux engine and Docker Compose.

The injector service reads its OpenRouter configuration from
`../ifc-fault-injector/.env`. Create it once before starting the stack:

```powershell
Copy-Item ..\ifc-fault-injector\.env.example ..\ifc-fault-injector\.env
# Add OPENROUTER_API_KEY to the new .env file.
docker compose up --build
```

Open [http://localhost:8080](http://localhost:8080), create an account, and
sign in. The first build downloads the IFC-processing dependencies and can take
several minutes. Later builds use Docker's cache.

To stop the stack:

```powershell
docker compose down
```

## Test-case creation workflow

1. Pick an IFC model (maximum 400 MB).
2. Choose **Create test cases**.
3. Select applicable architectural and/or structural rules.
4. Select **Inject selected**. The button stays in a loading state while the
   server emits the standalone script, creates the files, and validates them.
5. Review each mutation and download the outputs:
   - plain violating IFC for the compliance checker under test;
   - coloured IFC for human review only;
   - emitted standalone Python injection script;
   - machine-readable verification report.

The operation is all-or-nothing: if a requested violation cannot be applied
and verified, no partial test case is delivered.

## IFC preview

The server preprocesses the original and coloured generated IFC files into XKT
for the browser. This avoids parsing large IFC files in the browser.

- **Original IFC** displays the uploaded model.
- **Violating IFC** displays the generated review model.
- After injection, the Violating IFC opens with the model transparent and the
  injected element highlighted red. The focus persists if the user switches to
  Original IFC and back.
- **Show in model** appears on an injected mutation and on eligible compliance
  findings; it focuses that affected element in the preview.
- The text toolbar offers Reset view, Isolate selected, X-ray selected, and
  Show all. Click an empty part of the canvas or press Escape to deselect.

Selecting an element exposes its IFC identifier and available properties.

## Services

```text
Browser
  |
  v
Nginx frontend (:8080)
  |- static SPA
  |- auth-api       account, session, and history API
  |- ifcweb         IFC analysis, injection, verification, XKT conversion
  `- bnbc-web       accepted BNBC compliance checkers
       |
       `- PostgreSQL  users, sessions, completed-job history
```

Only Nginx publishes a host port. The internal APIs and PostgreSQL are reached
only over Docker's private network. Session cookies are `HttpOnly`; set
`COOKIE_SECURE=true` when deploying behind HTTPS.

## Storage and limitations

Docker volumes retain uploads, generated files, the LLM cache, and PostgreSQL
data across container restarts. The engine job queues are in memory and process
one job at a time to control IFC memory use. A service restart clears active
job state, so this is suitable for a capstone prototype rather than a
high-throughput production deployment.

Large production deployments should add object storage, durable queues,
background workers, HTTPS, backups, monitoring, and organization/project-level
authorization. See [PRODUCTION-PLAN.md](PRODUCTION-PLAN.md) for the proposed
evolution.
