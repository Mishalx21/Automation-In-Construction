"use strict";
let registerMode = false;
const auth$ = (id) => document.getElementById(id);

async function authApi(path, options = {}) {
  const response = await fetch("/auth" + path, { credentials: "same-origin", ...options });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "Request failed");
  }
  return response.status === 204 ? null : response.json();
}

function setAuthError(message = "") {
  const el = auth$("auth-error");
  el.textContent = message;
  el.classList.toggle("hidden", !message);
}

function renderHistory(rows) {
  const el = auth$("history-list");
  if (!rows.length) { el.textContent = "No completed jobs yet."; return; }
  const renderRows = (limit) => {
    const shown = rows.slice(0, limit);
    el.innerHTML = shown.map((row) => `<div class="history-item"><b>${row.filename}</b><span>${row.engine} · ${row.state} · ${new Date(row.created_at).toLocaleString()}</span></div>`).join("") +
      `<div class="history-actions">${rows.length > limit ? `<button class="btn small history-more" type="button">Load ${Math.min(5, rows.length - limit)} more</button>` : ""}${limit > 5 ? `<button class="btn small history-less" type="button">Show less</button>` : ""}</div>`;
    el.querySelector(".history-more")?.addEventListener("click", () => renderRows(limit + 5));
    el.querySelector(".history-less")?.addEventListener("click", () => renderRows(5));
  };
  renderRows(5);
}

async function refreshHistory() {
  try { renderHistory(await authApi("/api/history")); } catch (_) {}
}

async function setSignedIn(user) {
  auth$("auth-main").classList.add("hidden");
  auth$("workspace").classList.remove("hidden");
  auth$("user-bar").classList.remove("hidden");
  auth$("user-name").textContent = user.display_name || user.email;
  await refreshHistory();
}

window.recordHistory = async (engine, job) => {
  try {
    await authApi("/api/history", { method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({engine, job_id: job.job_id, filename: job.filename, state: job.state,
        summary: engine === "bnbc" ? `${(job.results || []).length} rule result(s)` : `${(job.mutations || []).length} mutation(s)`})});
    await refreshHistory();
  } catch (_) {}
};

window.authReady = (async () => {
  try {
    const {user} = await authApi("/api/auth/me");
    await setSignedIn(user);
  } catch (_) {}
})();

auth$("auth-mode").addEventListener("click", () => {
  registerMode = !registerMode;
  auth$("auth-name-row").classList.toggle("hidden", !registerMode);
  auth$("auth-submit").textContent = registerMode ? "Create account" : "Sign in";
  auth$("auth-mode").textContent = registerMode ? "I already have an account" : "Create an account";
  auth$("auth-password").autocomplete = registerMode ? "new-password" : "current-password";
  setAuthError();
});

auth$("auth-form").addEventListener("submit", async (event) => {
  event.preventDefault(); setAuthError();
  const button = auth$("auth-submit"); button.disabled = true;
  try {
    const body = {email: auth$("auth-email").value, password: auth$("auth-password").value};
    if (registerMode) body.display_name = auth$("auth-name").value;
    const {user} = await authApi(registerMode ? "/api/auth/register" : "/api/auth/login",
      {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    await setSignedIn(user);
  } catch (error) { setAuthError(error.message); }
  finally { button.disabled = false; }
});

auth$("logout-btn").addEventListener("click", async () => {
  await authApi("/api/auth/logout", {method:"POST"}).catch(() => {});
  location.reload();
});
