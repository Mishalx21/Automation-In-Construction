"""Password authentication and per-user history service.

Passwords are encoded using scrypt with a per-password random salt. Sessions are
opaque, random values stored only in Secure/HttpOnly cookies; the database holds
only a SHA-256 digest of each session token.
"""
from __future__ import annotations
import base64, hashlib, hmac, os, secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal
import psycopg
from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field

DATABASE_URL = os.environ["DATABASE_URL"]
COOKIE_NAME = "ifc_session"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "14"))

app = FastAPI(title="capstone-auth")

DDL = """
CREATE TABLE IF NOT EXISTS users (
 id UUID PRIMARY KEY DEFAULT gen_random_uuid(), email TEXT UNIQUE NOT NULL,
 display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 expires_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS history (
 id BIGSERIAL PRIMARY KEY, user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 engine TEXT NOT NULL, job_id TEXT NOT NULL, filename TEXT NOT NULL,
 state TEXT NOT NULL, summary TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(user_id, engine, job_id)
);
"""

@contextmanager
def db():
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        yield conn

@app.on_event("startup")
def startup():
    with db() as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        conn.execute(DDL)

class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=256)
    display_name: str | None = Field(default=None, max_length=120)

class HistoryCreate(BaseModel):
    engine: Literal["ifcfault", "ifcinject", "bnbc"]
    job_id: str = Field(min_length=1, max_length=80)
    filename: str = Field(min_length=1, max_length=255)
    state: str = Field(min_length=1, max_length=40)
    summary: str | None = Field(default=None, max_length=500)

def password_encode(password: str) -> str:
    salt = secrets.token_bytes(16)
    value = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(value).decode()

def password_ok(password: str, stored: str) -> bool:
    try:
        salt64, digest64 = stored.split("$", 1)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt64), n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, base64.b64decode(digest64))
    except Exception:
        return False

def user_from_request(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in required")
    with db() as conn:
        row = conn.execute(
            "SELECT u.id, u.email, u.display_name FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=%s AND s.expires_at > now()",
            (hashlib.sha256(token.encode()).hexdigest(),),
        ).fetchone()
    if not row:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired")
    return {"id": str(row[0]), "email": row[1], "display_name": row[2]}

def start_session(response: Response, user_id: str):
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    with db() as conn:
        conn.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES (%s,%s,%s)",
                     (hashlib.sha256(token.encode()).hexdigest(), user_id, expires))
    response.set_cookie(COOKIE_NAME, token, httponly=True, secure=COOKIE_SECURE,
                        samesite="lax", max_age=SESSION_DAYS*86400, path="/")

@app.post("/api/auth/register", status_code=201)
def register(data: Credentials, response: Response):
    name = (data.display_name or data.email.split("@")[0]).strip()
    with db() as conn:
        try:
            row = conn.execute("INSERT INTO users(email,display_name,password_hash) VALUES (%s,%s,%s) RETURNING id",
                               (str(data.email).lower(), name, password_encode(data.password))).fetchone()
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, "an account with this email already exists")
    start_session(response, str(row[0]))
    return {"user": {"id": str(row[0]), "email": str(data.email).lower(), "display_name": name}}

@app.post("/api/auth/login")
def login(data: Credentials, response: Response):
    with db() as conn:
        row = conn.execute("SELECT id,email,display_name,password_hash FROM users WHERE email=%s",
                           (str(data.email).lower(),)).fetchone()
    if not row or not password_ok(data.password, row[3]):
        raise HTTPException(401, "email or password is incorrect")
    start_session(response, str(row[0]))
    return {"user": {"id": str(row[0]), "email": row[1], "display_name": row[2]}}

@app.post("/api/auth/logout", status_code=204)
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        with db() as conn: conn.execute("DELETE FROM sessions WHERE token_hash=%s", (hashlib.sha256(token.encode()).hexdigest(),))
    response.delete_cookie(COOKIE_NAME, path="/")

@app.get("/api/auth/me")
def me(request: Request): return {"user": user_from_request(request)}

@app.get("/api/session/verify", status_code=204)
def verify(request: Request, response: Response):
    user = user_from_request(request)
    response.headers["X-User-Id"] = user["id"]

@app.get("/api/history")
def list_history(request: Request):
    user = user_from_request(request)
    with db() as conn:
        rows = conn.execute("SELECT engine,job_id,filename,state,summary,created_at FROM history WHERE user_id=%s ORDER BY created_at DESC LIMIT 50", (user["id"],)).fetchall()
    return [{"engine":r[0],"job_id":r[1],"filename":r[2],"state":r[3],"summary":r[4],"created_at":r[5].isoformat()} for r in rows]

@app.post("/api/history", status_code=201)
def add_history(data: HistoryCreate, request: Request):
    user = user_from_request(request)
    with db() as conn:
        conn.execute("INSERT INTO history(user_id,engine,job_id,filename,state,summary) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,engine,job_id) DO UPDATE SET state=EXCLUDED.state,summary=EXCLUDED.summary",
                     (user["id"],data.engine,data.job_id,data.filename,data.state,data.summary))
    return {"ok": True}
