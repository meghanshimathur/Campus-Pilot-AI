// Sign-in screen, user chip, sign-out and session expiry.
//
// What this module guarantees (the server still decides everything else):
//  - the password is read from the field, the field is emptied immediately, and the value lives only in a local variable for the
//    duration of one request; it is never stored, logged, put in the DOM or sent anywhere but POST /api/auth/login;
//  - the bearer token goes straight into api.js (memory only) and never touches the DOM, the store, storage or the console;
//  - signing out and an expired/revoked session both end the same way: token dropped, everything shown for that user wiped,
//    sign-in screen shown with a plain explanation.
import { api, clearSessionToken, setSessionToken, setUnauthorizedHandler } from "./api.js";
import { announce } from "./dom.js";

export const LIMITS = { userId: 32, password: 128 };    // the same limits the server enforces on /api/auth/login

// One readable sentence for a failed sign-in. 401 never says whether the user exists or the account is locked (the server
// answers identically, so the page must not guess); other problems are described as they are.
export function loginErrorText(err) {
  if (err?.status === 401) return "Sign-in failed. Check your user ID and password and try again. After several wrong attempts an account is locked for a while.";
  if (err?.status === 422) return "Enter your user ID and password as they were given to you.";
  if (err?.kind === "network" || err?.kind === "timeout") return err.message;
  if (err?.status === 503) return err.message;
  return `Could not sign in: ${err?.message ?? "unknown error"}`;
}

export function validateLogin(userId, password) {
  const id = String(userId ?? "").trim();
  if (!id) return { error: "Enter your user ID.", field: "user" };
  if (id.length > LIMITS.userId) return { error: `A user ID has at most ${LIMITS.userId} characters.`, field: "user" };
  if (!password) return { error: "Enter your password.", field: "password" };
  if (password.length > LIMITS.password) return { error: `A password has at most ${LIMITS.password} characters.`, field: "password" };
  return { userId: id };
}

export function userLabel(user) {
  const name = user.display_name || user.user_id;
  return `${name} · ${user.role}`;
}

export function initAuth({ store, onSignedIn, onSignedOut }) {
  const $ = (id) => document.getElementById(id);
  const form = $("login-form"), userInput = $("login-user"), passInput = $("login-password"), submit = $("login-submit");
  const errorEl = $("login-error"), noticeEl = $("login-notice"), signoutBtn = $("signout"), userChip = $("user-chip");
  const label = submit.querySelector(".btn-label");
  let busy = false;

  function showError(message, field) {
    errorEl.hidden = !message;
    errorEl.textContent = message || "";
    for (const el of [userInput, passInput]) el.removeAttribute("aria-invalid");
    if (message && field) (field === "user" ? userInput : passInput).setAttribute("aria-invalid", "true");
  }

  function showNotice(message, tone = "info", alert = false) {
    noticeEl.hidden = !message;
    noticeEl.className = `alert alert-${tone}`;
    noticeEl.setAttribute("role", alert ? "alert" : "status");
    noticeEl.textContent = message || "";
  }

  function setBusy(on) {
    busy = on;
    submit.disabled = on;
    userInput.disabled = on;
    passInput.disabled = on;
    label.textContent = on ? "Signing in…" : "Sign in";
  }

  async function onSubmit(event) {
    event.preventDefault();
    if (busy) return;
    const checked = validateLogin(userInput.value, passInput.value);
    if (checked.error) { showError(checked.error, checked.field); (checked.field === "user" ? userInput : passInput).focus(); return; }
    showError("");
    showNotice("");
    let password = passInput.value;
    passInput.value = "";                               // never leave the password sitting in the page
    let failed = false;
    setBusy(true);
    try {
      const res = await api.login(checked.userId, password);
      password = "";
      setSessionToken(res.token);                       // memory only (api.js)
      store.setUser(res.user);
      userInput.value = "";
      userChip.textContent = userLabel(store.state.user);
      userChip.title = `Signed in as ${store.state.user.user_id}`;
      announce(`Signed in as ${userLabel(store.state.user)}.`);
      onSignedIn(store.state.user);
    } catch (err) {
      password = "";
      failed = true;
      showError(loginErrorText(err));
      announce("Sign-in failed.");
    } finally {
      password = "";
      setBusy(false);
    }
    if (failed) passInput.focus();                      // only an enabled field can take focus
  }
  form.addEventListener("submit", onSubmit);
  userInput.addEventListener("input", () => { if (!errorEl.hidden) showError(""); });
  passInput.addEventListener("input", () => { if (!errorEl.hidden) showError(""); });

  // The one place a session ends. Idempotent: expiry and a click on "Sign out" can race.
  function endSession(message, tone, alert) {
    if (!store.state.user) return;
    clearSessionToken();
    store.signOut();
    onSignedOut();
    passInput.value = "";
    showError("");
    showNotice(message, tone, alert);
    announce(message);
    userInput.focus();
  }

  async function signOut() {
    if (!store.state.user) return;
    signoutBtn.disabled = true;
    let ended = true;
    try { await api.logout(); } catch (err) { ended = err?.kind === "unauthorized"; }    // 401: the session was already over
    signoutBtn.disabled = false;
    if (!store.state.user) return;                      // expiry got there first
    if (ended) endSession("You have been signed out.", "info", false);
    else endSession("You were signed out on this device, but the server could not be reached to end the session. It will expire on its own.", "warning", true);
  }
  signoutBtn.addEventListener("click", signOut);

  // Any protected request the server refuses with 401 (expired, revoked or unknown token) lands here.
  setUnauthorizedHandler(() => endSession("Your session has expired or was ended. Please sign in again.", "warning", true));

  return { signOut, focus: () => userInput.focus(), showNotice };
}
