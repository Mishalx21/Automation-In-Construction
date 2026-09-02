# bnbc-web — run the accepted BNBC rule checkers from a browser

A FastAPI layer over the **accepted** rule checkers (`rule-*/check_*.py`):
upload an IFC model, pick rules, get verdicts. It adds no new checking
logic — the worker calls the same `check_rule(model)` entry point the
acceptance harness (agentic_pipeline_v3/v4) uses.

Same discipline as the generation pipeline:

- **No LLM.** Checker generation happens offline with the agentic pipeline;
  this service only *runs* accepted checkers. The web dependency set
  (`requirements-web.txt`) deliberately excludes langgraph/google-genai/openai,
  so the deployed image carries no LLM SDKs and needs no API keys.
- **One job at a time.** Checks run on a single background worker thread
  (ifcopenshell holds whole files in memory), so several large uploads can
  never blow up memory.
- **Fresh open per checker.** Each checker gets its own `ifcopenshell.open()`
  — a buggy checker cannot mutate the model and poison the next rule's
  verdict.

## Run it (local / demo machine)

```bash
python -m venv .venv && .venv/Scripts/activate    # or Activate.ps1
pip install -r requirements-web.txt

uvicorn bnbcweb.app:app --host 0.0.0.0 --port 8001
```

Or, with the full stack (frontend + this service + ifcinject):

```bash
cd ../webapp && docker compose up --build     # -> http://localhost:8080
```

## Configuration (environment variables)

| Variable | Meaning | Default |
|---|---|---|
| `BNBCWEB_MAX_UPLOAD_MB` | Max accepted upload size | `400` |
| `BNBCWEB_DATA_ROOT` | Where uploads are stored | `<repo>/out/bnbcweb` |

## API

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/checkers` | GET | The accepted-checker catalogue (metadata parsed from checker source, never imported at list time) |
| `/api/jobs` | POST | Multipart upload of an `.ifc` → `{job_id}` |
| `/api/jobs/{id}` | GET | Job state: `uploaded → queued_check → checking → done/failed`, with per-rule results streamed as they finish |
| `/api/jobs/{id}/check` | POST | `{"rules": ["A1", "S3"]}` — must be known rule ids |
| `/api/jobs/{id}/download/report` | GET | The full report JSON (all results verbatim) |

## Tests

```bash
python -m pytest tests/test_bnbcweb.py -q
```

Endpoint tests drive the real accepted checkers against a synthetic bare
model (authored by `bnbc.fixtures.synthetic`): they prove the plumbing, not
rule semantics — those are proven by the fixture gate at acceptance time.

## Adding a newly accepted checker

The catalogue is discovered by scanning `rule-*/check_*.py`, so a new
accepted checker appears automatically **locally**. For the Docker image,
also add its directory to the `COPY rule-…` list in the `Dockerfile` (the
`.dockerignore` whitelist already covers `rule-*/check_*.py`).
