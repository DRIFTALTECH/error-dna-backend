"""Scraper — drives the SAP login as a per-page state machine, then extracts.

Instead of a fixed navigate→user→pw→signin script, we probe the page every step,
classify what it is (login form / account chooser / consent / MFA / target
content / ...), take the one action that page needs, and loop until we reach the
article or hit something a human must clear (MFA). This survives SAP inserting or
reordering interstitial pages — the old linear script did not.
"""

import json
import subprocess
import threading
import time
import logging
from datetime import datetime, timezone, timedelta

from config import CHAIN_PAGE_RETRIES, PREFERRED_SUSER

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

MAX_STEPS = 18          # hard cap so a redirect loop can't spin forever
STEP_COOLDOWN = 5       # openclaw cooldown after each browser command
MAX_LOADING_WAITS = 6   # extra probes to allow while a page is still rendering
NAV_RETRIES = 3         # openclaw navigate is flaky under load — retry before failing
CHALLENGE_WAITS = 12    # probes to allow while a Cloudflare interstitial clears
CHALLENGE_WAIT_SEC = 5  # pause between those probes

# One Chrome / one openclaw gateway: notes scraper, community ingest, and
# credential test-login must never interleave browser commands.
_BROWSER_LOCK = threading.Lock()


def _ts() -> str:
    return datetime.now(IST).strftime("%H:%M:%S")


# Placeholder/skeleton markers — a page that shows these hasn't rendered yet.
_LOADING_KW = ("not shown", "please wait", "loading…", "loading...", "just a moment",
               "header title", "header subtitle")

# Cloudflare bot-verification. Two variants and the old code only knew the first:
# the no-JS page says "Just a moment...", but the JS-rendered Turnstile page says
# "Verify you are human", carries a Ray ID, and sets its heading to the bare
# hostname — ~200-300 chars that read as a fully rendered page. That is how a
# challenge used to sail through as "Page rendered" and reach the LLM as content.
_CHALLENGE_KW = (
    "verify you are human", "verifying you are human",
    "needs to review the security of your connection",
    "enable javascript and cookies to continue",
    "checking your browser before accessing",
    "performance & security by cloudflare",
    "ray id:", "cf-ray", "just a moment",
    "attention required! | cloudflare",
)


def is_challenge(sig: dict) -> bool:
    """True if this page is a bot-verification interstitial, not the site."""
    lc = (sig.get("lc", "") or "")
    head = (sig.get("heading", "") or "").strip().lower()
    if any(k in lc for k in _CHALLENGE_KW):
        return True
    # Turnstile sets the heading to the bare hostname over a very short body.
    if head and "." in head and " " not in head and sig.get("len", 0) < 600:
        return True
    return False


def _looks_loading(sig: dict) -> bool:
    """True if the page is a still-rendering skeleton, not a real state to act on."""
    lc = (sig.get("lc", "") or "")
    head = (sig.get("heading", "") or "").lower()
    if any(k in lc or k in head for k in _LOADING_KW):
        return True
    # Near-empty page with no form and no tiles = almost certainly mid-navigation.
    return (sig.get("len", 0) < 120 and not sig.get("hasPass")
            and not sig.get("hasUser") and not sig.get("suserTiles")
            and not sig.get("acctTiles"))


import os as _os_run

# OPENCLAW_PROFILE isolates openclaw to a machine-local profile/gateway/browser so
# the box drives its OWN headless Chrome instead of routing to the account's other
# node (e.g. the laptop). Empty = default profile.
_OPENCLAW_PROFILE = _os_run.getenv("OPENCLAW_PROFILE", "").strip()
_OPENCLAW_BASE = (["openclaw", "--profile", _OPENCLAW_PROFILE] if _OPENCLAW_PROFILE else ["openclaw"])
# Always target the managed local profile (not chrome/user extension → laptop).
_BROWSER_PROFILE = ["--browser-profile", "openclaw"]


def _run(cmd: list, timeout: int = 30) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            _OPENCLAW_BASE + ["browser"] + _BROWSER_PROFILE + cmd,
            capture_output=True, text=True, timeout=timeout,
        )
        time.sleep(STEP_COOLDOWN)
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        return r.returncode == 0, out or err
    except Exception as e:
        time.sleep(STEP_COOLDOWN)
        return False, str(e)


def _navigate(url: str, timeout: int = 30) -> tuple[bool, str]:
    """Navigate with retries — openclaw CDP occasionally returns non-zero under contention."""
    last = ""
    for attempt in range(1, NAV_RETRIES + 1):
        ok, out = _run(["navigate", url], timeout=timeout)
        if ok:
            return True, out
        last = out or f"exit non-zero (attempt {attempt})"
        logger.warning(f"  navigate attempt {attempt}/{NAV_RETRIES} failed: {last[:240]}")
        time.sleep(3)
    return False, last


def _get_text() -> tuple[bool, str]:
    ok, t = _run(["evaluate", "--fn", "()=>{const m=document.querySelector('[role=main]')||document.body;return m?m.innerText:''}"], timeout=30)
    return ok, t


def _clear_session() -> None:
    """Drop all cookies so a persisted login doesn't make every creds 'succeed'."""
    _run(["cookies", "clear"], timeout=15)


# Account of the last successful login. When ensure_session is handed a DIFFERENT
# account (e.g. after /rotate), we clear the persisted session so it logs in fresh
# as the new account instead of riding the old "keep me signed in" cookie.
# ponytail: process-global, resets on restart. On restart _last_account=None so the
# FIRST scrape rides the existing cookie (no clear) — avoids an MFA tax on every
# reboot, and the persisted session normally matches the DB-active account anyway.
# Only an in-process switch (old != new) forces the re-login.
_last_account: str | None = None


# ---- page signals ---------------------------------------------------------

# One DOM probe that returns every signal classify() needs, as JSON. openclaw
# serializes a string return; we json-decode (possibly twice — see _probe).
_PROBE_FN = r"""()=>{
  const vis = s => [...document.querySelectorAll(s)].filter(e=>e.offsetParent);
  const main = document.querySelector('[role=main]') || document.body;
  const txt = (main && main.innerText) || '';
  const clean = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const userIsh = i => /user|email|login/i.test((i.name||'')+(i.id||'')+(i.placeholder||'')+(i.getAttribute('aria-label')||''));
  return JSON.stringify({
    url: location.href,
    len: txt.length,
    lc: txt.toLowerCase().slice(0, 4000),
    hasPass: vis('input[type=password]').length > 0,
    hasUser: vis('input:not([type=password]):not([type=hidden]):not([type=checkbox]):not([type=submit])').some(userIsh)
             || /email, user id/i.test(txt),
    heading: (vis('h1,h2,[role=heading]').map(clean).filter(Boolean)[0] || '').slice(0, 120),
    // S-user id tiles (S + 6+ digits) — the ideal signal.
    suserTiles: vis('button,a,li,[role=button],[tabindex],[role=listitem],[role=option]').map(clean).filter(t => /\bS\d{6,}\b/i.test(t)).slice(0, 8),
    // Broader account-chooser tiles: short clickable rows carrying an S-id OR an email.
    acctTiles: vis('li,[role=listitem],[role=option],button,a,[tabindex]').map(clean).filter(t => t && t.length < 80 && (/\bS\d{6,}\b/i.test(t) || /@/.test(t))).slice(0, 8),
    btns: vis('button,a[role=button],[role=button]').map(clean).filter(Boolean).slice(0, 60)
  });
}"""


def _probe() -> dict | None:
    """Return the page-signal dict, or None if the probe failed."""
    ok, out = _run(["evaluate", "--fn", _PROBE_FN], timeout=30)
    if not ok or not out:
        return None
    sig = out.strip()
    # openclaw may wrap the returned string in quotes (single-encoded) or emit the
    # object directly. Decode up to twice to land on a dict either way.
    for _ in range(2):
        if isinstance(sig, dict):
            break
        try:
            sig = json.loads(sig)
        except Exception:
            break
    return sig if isinstance(sig, dict) else None


_MFA_KW = ("verification code", "one-time passcode", "one time passcode", "authenticator app",
           "two-factor", "2-step", "enter the code", "otp")
_ACCT_KW = ("account selection", "choose an account", "select an account", "choose account",
            "select account", "continue as", "choose a profile", "select a profile",
            "which account", "pick an account", "use another account")
_LANDING_KW = ("say hello", "digital companion", "sap for me")
_TARGET_KW = ("symptom", "resolution")


def classify(sig: dict) -> str:
    """Map page signals to a state name. Order matters — most specific first."""
    lc = sig.get("lc", "")
    length = sig.get("len", 0)

    # Real article content — check first so a logged-in page never looks like a login.
    if length > 200 and any(k in lc for k in _TARGET_KW) and not sig.get("hasPass") and "account selection" not in lc:
        return "target"
    # MFA / OTP — a human must clear this; we can't.
    if any(k in lc for k in _MFA_KW):
        return "mfa"
    # Profile chooser: explicit phrase, S-user/email tiles, or a "choose account" heading.
    # Checked before login_pass/login_user so the post-auth chooser wins over stray inputs.
    head_lc = (sig.get("heading", "") or "").lower()
    if (sig.get("suserTiles") or sig.get("acctTiles")
            or "account selection" in lc
            or any(k in head_lc + " " + lc for k in _ACCT_KW)):
        return "account_select"
    if sig.get("hasPass"):
        return "login_pass"
    if "keep me signed in" in lc and not sig.get("hasUser"):
        return "keep_signed"
    if sig.get("hasUser"):
        return "login_user"
    if "sign in" in lc and any(k in lc for k in _LANDING_KW):
        return "landing"
    # Consent / cookie / terms gate with an accept-style button.
    btns_lc = " || ".join(sig.get("btns", [])).lower()
    if any(w in btns_lc for w in ("accept", "agree", "allow all", "continue")) and any(
            w in lc for w in ("cookie", "terms", "privacy", "consent", "conditions")):
        return "consent"
    return "unknown"


# ---- per-state actions ----------------------------------------------------

def _check_keep_signed() -> tuple[bool, str]:
    """Tick the 'Keep me signed in' checkbox so the session persists across runs.

    Prefers a checkbox labelled keep/remember/signed/stay; falls back to the sole
    checkbox on the page. Uses .click() so the framework's handler fires.
    """
    fn = ("()=>{const cbs=[...document.querySelectorAll('input[type=checkbox]')].filter(e=>e.offsetParent);"
          "const kw=/keep|remember|signed|stay/i;"
          "for(const c of cbs){const lbl=(c.getAttribute('aria-label')||'')+(c.name||'')+(c.id||'');"
          "if(kw.test(lbl)){if(!c.checked)c.click();return'checked'}}"
          "if(cbs.length===1){if(!cbs[0].checked)cbs[0].click();return'only'}return'none'}")
    return _run(["evaluate", "--fn", fn], timeout=15)


def _click_containing(words: list, extra_js: str = "") -> tuple[bool, str]:
    """Click the first visible button/link whose text contains any of `words`."""
    arr = "[" + ",".join("'" + w.replace("'", "\\'") + "'" for w in words) + "]"
    fn = ("()=>{const ws=" + arr + ".map(w=>w.toLowerCase());"
          "for(const b of document.querySelectorAll('button,a,[role=button],[tabindex]')){"
          "if(!b.offsetParent)continue;const t=(b.textContent||'').toLowerCase();"
          "if(ws.some(w=>t.includes(w))){b.click();return'ok'}}return'no'}" + extra_js)
    return _run(["evaluate", "--fn", fn], timeout=15)


def _fill(selector: str, value: str) -> tuple[bool, str]:
    v = value.replace("\\", "\\\\").replace("'", "\\'")
    fn = ("()=>{for(const i of document.querySelectorAll(" + json.dumps(selector) + ")){"
          "if(i.offsetParent){i.value='" + v + "';"
          "i.dispatchEvent(new Event('input',{bubbles:true}));"
          "i.dispatchEvent(new Event('change',{bubbles:true}));return'ok'}}return'no'}")
    return _run(["evaluate", "--fn", fn], timeout=15)


def _pick_suser() -> tuple[bool, str]:
    """Click the S-user profile tile. Prefer PREFERRED_SUSER; else any S-user tile.

    SAP nests the account as UL>LI>BUTTON — the LI wrapper also carries the id text
    but has no click handler. So we only scan genuinely interactive nodes and, if the
    match sits on an inner node, climb to the closest button/a/[role=button] to click.
    """
    want = (PREFERRED_SUSER or "").upper().replace("'", "")
    fn = ("()=>{const want='" + want + "';"
          "const cs=[...document.querySelectorAll('button,a,[role=button],[tabindex]')]"
          ".filter(e=>e.offsetParent);"
          "const tx=e=>(e.textContent||'').replace(/\\s+/g,' ').trim().toUpperCase();"
          "const clk=e=>{const b=e.closest('button,a,[role=button]')||e;b.click();};"
          "if(want){for(const e of cs){if(tx(e).includes(want)){clk(e);return'exact'}}}"
          "for(const e of cs){if(/\\bS\\d{6,}\\b/.test(tx(e))){clk(e);return'suser'}}"
          "for(const e of cs){if(/S[-\\s]?USER/.test(tx(e))){clk(e);return'label'}}"
          "return'none'}")
    return _run(["evaluate", "--fn", fn], timeout=15)


def _act(state: str, username: str, password: str) -> None:
    """Do the one thing this page needs to advance toward content."""
    if state == "landing":
        _click_containing(["sign in"])
        time.sleep(7)
    elif state == "login_user":
        _fill('input:not([type="password"]):not([type="hidden"]):not([type="checkbox"])', username or "")
        time.sleep(2)
        _check_keep_signed()   # persist session if the checkbox is on this page
        _click_containing(["continue", "next", "sign in"])
        time.sleep(5)
    elif state == "login_pass":
        # Some SAP tenants show user + password on ONE page (j_username + j_password).
        # Fill the user field too if present — a no-op on password-only pages.
        _fill('input:not([type="password"]):not([type="hidden"]):not([type="checkbox"]):not([type="submit"])', username or "")
        time.sleep(1)
        _fill('input[type="password"]', password or "")
        time.sleep(1)
        _check_keep_signed()   # tick "Keep me signed in" before submitting
        time.sleep(1)
        _click_containing(["continue", "sign in", "log on", "log in"])
        time.sleep(7)
    elif state == "account_select":
        ok, res = _pick_suser()
        logger.info(f"  account_select → {res}")
        time.sleep(7)
    elif state == "keep_signed":
        _click_containing(["yes", "continue", "no"])
        time.sleep(5)
    elif state == "consent":
        _click_containing(["accept", "agree", "allow all", "continue"])
        time.sleep(4)


_STATE_MSG = {
    "landing": "SAP for Me landing — clicking Sign In",
    "login_user": "Login form — entering username",
    "login_pass": "Password form — entering credentials",
    "account_select": "Account selection — picking S-user profile",
    "keep_signed": "'Keep me signed in?' prompt — dismissing",
    "consent": "Cookie/consent gate — accepting",
    "target": "Article content reached — extracting",
    "mfa": "MFA / OTP wall — needs a human",
}


# ---- chain step primitives ------------------------------------------------
# One Chrome, one openclaw gateway: the ingest chain holds BROWSER_LOCK across
# steps 2-4 so a credential test-login can never interleave mid-run.
BROWSER_LOCK = _BROWSER_LOCK

# Pages that mean "you are not through the auth wall yet".
LOGIN_STATES = ("landing", "login_user", "login_pass", "account_select", "keep_signed", "consent")


def _tracer() -> tuple[list, callable]:
    trace: list = []

    def rec(phase, status, message, detail=None):
        trace.append({"at": _ts(), "phase": phase, "status": status,
                      "message": message, "detail": detail})

    return trace, rec


def _probe_retrying(rec, tries: int = 3) -> dict | None:
    """Probe, tolerating a mid-navigation frame that returns nothing."""
    for attempt in range(tries):
        sig = _probe()
        if sig is not None:
            return sig
        rec("probe", "warn", f"Probe returned nothing (attempt {attempt + 1}/{tries})")
        time.sleep(4)
    return None


def _detail(sig: dict) -> str:
    snippet = (sig.get("lc", "")[:120]).replace("\n", " ").strip()
    cururl = sig.get("url", "")
    return f"url={cururl} len={sig.get('len')} · {snippet}" if cururl else f"len={sig.get('len')} · {snippet}"


def ensure_session(url: str, username: str = None, password: str = None) -> dict:
    """Step 2 — open `url` and clear whatever auth wall appears. Reuses a live session.

    Navigating to the target directly means an already-signed-in run costs zero extra
    page loads; only a bounce to the IdP triggers the login states.

    Returns {ok, state, mode, error, trace}. mode is 'reused_session' | 'logged_in'.
    error is machine-readable: navigate_failed / probe_failed / mfa_required /
    needs_login / stuck:<state>.
    """
    global _last_account
    trace, rec = _tracer()

    # Account changed since the last login (rotate) → drop the old cookie so we
    # sign in as the NEW account instead of riding "keep me signed in".
    #
    # Claim the account HERE, not on success. _last_account means "the jar belongs
    # to this account", and after the clear it does. Setting it only on success let
    # a failing account re-clear on every single attempt, which wiped the Cloudflare
    # clearance cookie before each try and made recovery impossible.
    if username and _last_account is not None and username != _last_account:
        _clear_session()
        rec("session", "info", "Account switched — cleared persisted session", username)
        _last_account = username

    nav_ok, nav_out = _navigate(url, timeout=30)
    rec("navigate", "ok" if nav_ok else "error",
        f"Opened {url}" if nav_ok else "Navigate command failed",
        None if nav_ok else (nav_out[:300] or "openclaw browser navigate returned non-zero"))
    if not nav_ok:
        return {"ok": False, "state": "unknown", "mode": None, "error": "navigate_failed", "trace": trace}
    time.sleep(5)

    last, repeats, waits, saw_login, challenges = None, 0, 0, False, 0

    for step in range(MAX_STEPS):
        sig = _probe_retrying(rec)
        if sig is None:
            rec("probe", "error", "Browser probe failed after retries",
                "openclaw returned no DOM — is the headless browser running?")
            return {"ok": False, "state": "unknown", "mode": None, "error": "probe_failed", "trace": trace}

        state = classify(sig)
        detail = _detail(sig)
        logger.info(f"  [login {step}] state={state} len={sig.get('len')}")

        if state == "mfa":
            rec("mfa", "error", _STATE_MSG["mfa"], detail)
            return {"ok": False, "state": state, "mode": None, "error": "mfa_required", "trace": trace}

        # Cloudflare interstitial. A real browser clears it on its own within a few
        # seconds; we only wait, never touch the widget.
        if is_challenge(sig):
            challenges += 1
            rec("challenge", "info",
                f"Bot verification — waiting for it to clear ({challenges}/{CHALLENGE_WAITS})", detail)
            if challenges <= CHALLENGE_WAITS:
                time.sleep(CHALLENGE_WAIT_SEC)
                continue
            rec("challenge", "error", "Bot verification never cleared", detail)
            return {"ok": False, "state": "challenge", "mode": None,
                    "error": "cloudflare_challenge", "trace": trace}

        if state not in LOGIN_STATES:
            # Still-rendering skeleton is not a state to act on — wait it out.
            if state == "unknown" and _looks_loading(sig):
                waits += 1
                rec("loading", "info", f"Page still rendering (wait {waits}/{MAX_LOADING_WAITS})", detail)
                if waits <= MAX_LOADING_WAITS:
                    time.sleep(6)
                    continue
                rec("login", "error", "Page never finished loading", detail)
                return {"ok": False, "state": state, "mode": None, "error": "stuck:loading", "trace": trace}
            # Past the wall (article, or some other authenticated page) — step 3 judges.
            mode = "logged_in" if saw_login else "reused_session"
            if username:
                _last_account = username
            rec("login", "ok",
                "Signed in" if saw_login else "Existing session reused — no login needed", detail)
            return {"ok": True, "state": state, "mode": mode, "error": "", "trace": trace}

        saw_login = True
        if not username or not password:
            rec("login", "error", "Login required but no credentials for this account", detail)
            return {"ok": False, "state": state, "mode": None, "error": "needs_login", "trace": trace}

        # Loop guard: same actionable state 3× running = we are not advancing.
        repeats = repeats + 1 if state == last else 0
        last = state
        if repeats >= 3:
            rec("login", "error", f"Stuck on '{state}' — not advancing", detail)
            return {"ok": False, "state": state, "mode": None, "error": f"stuck:{state}", "trace": trace}

        rec(state, "ok", _STATE_MSG.get(state, f"Handling {state}"), detail)
        _act(state, username, password)

    rec("login", "error", "Gave up after max steps", f"MAX_STEPS={MAX_STEPS}")
    return {"ok": False, "state": last or "unknown", "mode": None, "error": "max_steps", "trace": trace}


def open_page(url: str, username: str = None, password: str = None,
              retries: int = None, target: str = "note",
              navigate: bool = False, min_chars: int = 200) -> dict:
    """Step 3 — confirm the page we want is actually on screen.

    3.1 thin / still-rendering / bot check → wait, re-probe, re-navigate near the end.
    3.2 bounced back to a login            → re-run step 2 once, then re-verify.
    3.3 still not there                    → fail with a machine-readable error.

    target="note"    the note-article classifier must fire (Symptom/Resolution).
    target="content" any rendered page whose URL still carries the id we asked for
                     — a forum thread never matches the note classifier.
    navigate=True    load `url` first; the login step landed us somewhere else.

    Returns {ok, state, error, trace}.
    """
    if retries is None:
        retries = CHAIN_PAGE_RETRIES
    trace, rec = _tracer()
    relogins = 0
    challenges = 0

    # The id in .../-p/<id>: proof we are on the page we asked for, not a redirect.
    _, _sep, _tail = url.rstrip("/").rpartition("-p/")
    want_id = _tail if _sep and _tail.isdigit() else ""

    if navigate:
        nav_ok, nav_out = _navigate(url, timeout=30)
        rec("navigate", "ok" if nav_ok else "error",
            f"Opened {url}" if nav_ok else "Navigate command failed",
            None if nav_ok else (nav_out[:300] or None))
        if not nav_ok:
            return {"ok": False, "state": "unknown", "error": "navigate_failed", "trace": trace}
        time.sleep(5)

    for attempt in range(1, retries + 1):
        sig = _probe_retrying(rec)
        if sig is None:
            rec("verify", "error", "Browser probe failed after retries")
            return {"ok": False, "state": "unknown", "error": "probe_failed", "trace": trace}

        state = classify(sig)
        detail = _detail(sig)
        cururl = sig.get("url", "") or ""

        if state == "mfa":
            rec("verify", "error", _STATE_MSG["mfa"], detail)
            return {"ok": False, "state": state, "error": "mfa_required", "trace": trace}

        # 3.2 — session died mid-run and we are back at the IdP.
        if state in LOGIN_STATES:
            if relogins >= 1:
                rec("verify", "error", f"Bounced to login again ({state}) after re-auth", detail)
                return {"ok": False, "state": state, "error": "session_expired", "trace": trace}
            relogins += 1
            rec("verify", "warn", f"Bounced to login ({state}) — re-authenticating", detail)
            again = ensure_session(url, username, password)
            trace.extend(again.get("trace") or [])
            if not again["ok"]:
                return {"ok": False, "state": again["state"], "error": again["error"], "trace": trace}
            continue

        # Bot verification — wait it out, never touch the widget.
        if is_challenge(sig):
            challenges += 1
            rec("challenge", "info",
                f"Bot verification — waiting for it to clear ({challenges}/{CHALLENGE_WAITS})", detail)
            if challenges <= CHALLENGE_WAITS:
                time.sleep(CHALLENGE_WAIT_SEC)
                continue
            rec("challenge", "error", "Bot verification never cleared", detail)
            return {"ok": False, "state": "challenge", "error": "cloudflare_challenge", "trace": trace}

        rendered = not _looks_loading(sig) and sig.get("len", 0) >= min_chars
        # Stale-DOM race: the probe can still show the PREVIOUS article while the URL
        # is already the IdP. A real article is never served from accounts.sap.com.
        on_idp = "accounts.sap.com" in cururl
        # For a thread, the id must still be in the URL — otherwise we were redirected
        # (sign-in bounce, "topic moved", 404 shell) and are reading the wrong page.
        right_page = (want_id in cururl) if (want_id and cururl) else True

        if target == "note":
            reached = state == "target" and not on_idp
        else:
            reached = rendered and not on_idp and right_page

        if reached:
            rec("verify", "ok", "Page open and rendered", detail)
            return {"ok": True, "state": state, "error": "", "trace": trace}

        # 3.1 — not there yet: wait, and re-navigate on the final attempt.
        why = "wrong page after redirect" if not right_page else "not rendered yet"
        rec("verify", "info", f"Page not ready — {why} (attempt {attempt}/{retries})", detail)
        time.sleep(6)
        if attempt == retries - 1:
            rec("verify", "warn", "Re-navigating to the page")
            _navigate(url, timeout=30)
            time.sleep(5)

    # 3.3
    rec("verify", "error", "Page never reached", f"after {retries} attempts")
    return {"ok": False, "state": "unknown", "error": "page_not_reached", "trace": trace}


# Post-auth signals only. NOT consent — a cookie/privacy banner shows pre-login,
# so treating it as success would pass any creds. It's dismissed and we loop.
_AUTH_OK = ("account_select", "keep_signed", "target")
_BAD_CREDS_KW = (
    "incorrect email", "incorrect password", "invalid password", "wrong password",
    "we didn't recognize", "didn't recognize", "authentication failed",
    "invalid email", "invalid user", "user id or password", "login failed",
    "unable to sign in", "couldn't find your account", "could not find your account",
)


def test_login(login_url: str, username: str, password: str) -> dict:
    """Drive a real SAP login via openclaw. Success = past the credential wall.

    Returns {ok, message, state}. MFA after accepted creds counts as ok=True
    (password was right; a human must finish).
    """
    with _BROWSER_LOCK:
        return _test_login_locked(login_url, username, password)


def _test_login_locked(login_url: str, username: str, password: str) -> dict:
    url = (login_url or "https://me.sap.com").strip()
    user = (username or "").strip()
    pw = password or ""
    if not user or not pw:
        return {"ok": False, "message": "Username and password required", "state": "needs_creds"}

    _clear_session()   # else a persisted login makes any creds look valid
    _navigate(url, timeout=30)
    time.sleep(5)

    last = None
    repeats = 0
    last_state = "unknown"

    for step in range(MAX_STEPS):
        sig = _probe()
        if sig is None:
            time.sleep(4)
            sig = _probe()
            if sig is None:
                return {"ok": False, "message": "Browser probe failed — is openclaw browser running?", "state": "probe_failed"}

        state = classify(sig)
        last_state = state
        lc = sig.get("lc", "")
        length = sig.get("len", 0)
        logger.info(f"  [test_login {step}] state={state} len={length}")

        if any(k in lc for k in _BAD_CREDS_KW):
            return {"ok": False, "message": "Invalid username or password", "state": state}

        if state in _AUTH_OK:
            return {"ok": True, "message": f"Login succeeded ({state})", "state": state}

        if state == "mfa":
            return {"ok": True, "message": "Credentials accepted — MFA required to finish", "state": "mfa"}

        # Logged-in SAP For Me home won't look like a note "target".
        if state == "unknown" and length > 400 and not sig.get("hasPass") and not sig.get("hasUser"):
            if any(k in lc for k in _LANDING_KW) or "sap" in lc:
                return {"ok": True, "message": "Login succeeded (authenticated session)", "state": "authenticated"}

        if state in ("landing", "login_user", "login_pass"):
            repeats = repeats + 1 if state == last else 0
            last = state
            if repeats >= 3:
                return {"ok": False, "message": f"Stuck on login step ({state}) — check credentials", "state": state}
            _act(state, user, pw)
            continue

        if state == "unknown":
            repeats += 1
            if repeats >= 3:
                snippet = lc[:80].replace("\n", " ")
                return {"ok": False, "message": f"Could not complete login: {snippet or 'unknown page'}", "state": "stuck"}
            time.sleep(5)
            continue

        # account_select / keep_signed / consent already returned above
        repeats = repeats + 1 if state == last else 0
        last = state
        if repeats >= 3:
            return {"ok": False, "message": f"Stuck on {state}", "state": state}
        _act(state, user, pw)

    return {"ok": False, "message": f"Login timed out (last state: {last_state})", "state": last_state}


import os as _os
from config import SCRAPE_DOWNLOAD_DIR as _DOWNLOAD_DIR

# Make sure the download dir exists so openclaw/Chrome + our scan agree on a path.
try:
    _os.makedirs(_DOWNLOAD_DIR, exist_ok=True)
except OSError:
    pass
_ATTACH_EXT_RE = r"\\.(xsd|xml|wsdl|pdf|txt|json|log|csv|tsv|docx|xlsx|zip|properties|groovy|sql|yaml|yml)$"


def _fetch_attachments() -> tuple[str, list]:
    """Open the note's Attachments tab, download each file, extract text + keep bytes.

    Returns (combined_text, [{name, ext, data}]). Best-effort — any failure → ("", []).
    Caller persists `data` (S3/local) then discards; we still wipe the download dir.
    """
    from services.attachments import extract_text, SUPPORTED_EXTS, MAX_ATTACH_CHARS

    # 1. reveal the Attachments tab (a leaf node whose text is exactly 'Attachments').
    _run(["evaluate", "--fn",
          "()=>{const es=[...document.querySelectorAll('*')].filter(e=>e.offsetParent&&(e.textContent||'').trim()==='Attachments'&&e.children.length<=1);"
          "if(es.length){es[es.length-1].click();return'ok'}return'no'}"], timeout=15)
    time.sleep(4)

    # 2. enumerate attachment filenames rendered in the tab.
    ok, out = _run(["evaluate", "--fn",
                    "()=>{const s=[...document.querySelectorAll('span,td,a')].filter(e=>e.offsetParent&&/" + _ATTACH_EXT_RE +
                    "/i.test((e.textContent||'').trim()));"
                    "return JSON.stringify([...new Set(s.map(e=>(e.textContent||'').trim()))].slice(0,10))}"], timeout=15)
    files = _decode_json(out) if out else []
    if not isinstance(files, list):
        files = []
    files = [f for f in files if isinstance(f, str) and f.strip()]
    if not files:
        return "", []

    before = set(_os.listdir(_DOWNLOAD_DIR)) if _os.path.isdir(_DOWNLOAD_DIR) else set()

    # 3. click each filename cell → downloads to disk.
    for fname in files:
        _run(["evaluate", "--fn",
              "()=>{const e=[...document.querySelectorAll('span,td,a')].find(x=>x.offsetParent&&(x.textContent||'').trim()==" +
              json.dumps(fname) + ");if(e){e.click();return'ok'}return'no'}"], timeout=15)
        time.sleep(5)

    # 4. resolve ONLY the files we just downloaded (after - before) — never touch
    #    the user's pre-existing files. Handles openclaw's "name (1).xsd" dedupe too.
    after = set(_os.listdir(_DOWNLOAD_DIR)) if _os.path.isdir(_DOWNLOAD_DIR) else set()
    downloaded = [_os.path.join(_DOWNLOAD_DIR, f) for f in sorted(after - before)]

    # Diagnose a wrong download dir (common after moving to a new host): files were
    # listed in the tab but nothing landed where we're looking → SCRAPE_DOWNLOAD_DIR
    # doesn't match the browser's actual download directory.
    if files and not downloaded:
        logger.warning(f"  ⚠️ {len(files)} attachment(s) detected but none captured in "
                       f"{_DOWNLOAD_DIR} — set SCRAPE_DOWNLOAD_DIR to the browser's real download dir")

    # 5. read bytes for storage + extract text for the LLM, then wipe local downloads.
    combined, attachments, total = [], [], 0
    try:
        for p in downloaded:
            name = _os.path.basename(p)
            ext = _os.path.splitext(name)[1].lstrip(".").lower() or "bin"
            try:
                with open(p, "rb") as f:
                    data = f.read()
            except OSError as e:
                logger.warning(f"  ⚠️ could not read attachment {name}: {e}")
                continue
            if not data:
                continue
            attachments.append({"name": name, "ext": ext, "data": data})
            if f".{ext}" not in SUPPORTED_EXTS:
                continue
            text = extract_text(p)
            if not text.strip() or total >= MAX_ATTACH_CHARS:
                continue
            chunk = text[: max(0, MAX_ATTACH_CHARS - total)]
            combined.append(f"--- ATTACHMENT: {name} ---\n{chunk}")
            total += len(chunk)
        return "\n\n".join(combined), attachments
    finally:
        for p in downloaded:
            try:
                _os.remove(p)
            except OSError:
                pass


def _parse_note_sections(text: str) -> tuple[str, str]:
    """Raw article text → (title, clean_text). Section-based, chrome dropped."""
    title = ""
    sections: dict = {}
    current, content = None, []
    _DROP = ("object status", "quality rating", "description", "products", "attributes",
             "available languages", "rate this document", "see also")

    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith(("3780", "377", "376")) and " - " in stripped:
            title = stripped.split(" - ", 1)[1].strip()
        low = stripped.lower()
        if low in ("symptom", "environment", "resolution", "keywords"):
            if current and content:
                sections[current] = "\n".join(content).strip()
            current, content = low, []
        elif current and stripped and low not in _DROP:
            content.append(stripped)
    if current and content:
        sections[current] = "\n".join(content).strip()

    clean = [f"TITLE: {title}"] if title else []
    for k in ("symptom", "environment", "resolution", "keywords"):
        if sections.get(k):
            clean.append(f"{k.upper()}:\n{sections[k]}")
    return title, ("\n\n".join(clean) if clean else text)


def extract_note() -> dict:
    """Step 4 (notes) — read the open article + its attachments.

    Returns {ok, raw_text, clean_text, title, attachments, error, trace}.
    attachments = [{name, ext, data}] — the caller persists `data` and drops it.
    """
    trace, rec = _tracer()

    ok, text = _get_text()
    tries = 0
    while (not ok or not text or len(text.strip()) < 100) and tries < 3:
        tries += 1
        rec("extract", "info", f"Content thin ({len((text or '').strip())} chars) — waiting (retry {tries})")
        time.sleep(5)
        ok, text = _get_text()
    if not ok or not text or len(text.strip()) < 100:
        rec("extract", "error", "Extracted text too short", f"{len(text or '')} chars")
        return {"ok": False, "error": "too_short", "trace": trace}

    rec("extract", "ok", f"Extracted {len(text)} chars")
    title, clean_text = _parse_note_sections(text)
    rec("parse", "ok", f"Parsed article: {title[:60]}" if title else "Parsed article",
        f"{len(clean_text)} chars cleaned")

    # Attachments are best-effort — a failure here never fails the run.
    attachments = []
    try:
        att_text, attachments = _fetch_attachments()
        if attachments:
            if att_text:
                clean_text += "\n\n" + att_text
                text += "\n\n" + att_text
            rec("attachments", "ok", f"Attached {len(attachments)} file(s)",
                ", ".join(a["name"] for a in attachments))
        else:
            rec("attachments", "info", "No attachments")
    except Exception as e:
        logger.warning(f"  ⚠️ attachment fetch failed: {e}")
        rec("attachments", "warn", "Attachment fetch failed — using note text only", str(e))

    return {"ok": True, "raw_text": text, "clean_text": clean_text, "title": title,
            "attachments": attachments, "error": "", "trace": trace}


if __name__ == "__main__":
    # ponytail: self-check for the classifier — the one piece with real branching.
    cases = [
        ({"len": 900, "lc": "symptom ... resolution ...", "hasPass": False, "suserTiles": [], "btns": []}, "target"),
        ({"len": 200, "lc": "please enter the verification code", "btns": []}, "mfa"),
        ({"len": 300, "lc": "account selection", "suserTiles": ["S0012345678 Lokesh"], "btns": []}, "account_select"),
        ({"len": 300, "lc": "choose a profile", "suserTiles": ["S0012345678 Lokesh"], "btns": []}, "account_select"),
        # real SAP chooser: tiles shown by name/email (no S-id in lc), heading carries it
        ({"len": 300, "heading": "Account selection", "lc": "email: lokesh@driftal.tech",
          "acctTiles": ["S0028040509 Lokesh Pathangi"], "btns": []}, "account_select"),
        ({"len": 200, "lc": "enter your password", "hasPass": True, "btns": []}, "login_pass"),
        # combined user+pass page (this tenant) — password wins, _act fills both
        ({"len": 200, "lc": "sign in", "hasPass": True, "hasUser": True, "btns": ["Continue"]}, "login_pass"),
        ({"len": 200, "lc": "email, user id", "hasUser": True, "btns": []}, "login_user"),
        ({"len": 200, "lc": "keep me signed in?", "hasUser": False, "btns": ["Yes", "No"]}, "keep_signed"),
        ({"len": 150, "lc": "say hello ... sign in", "btns": ["Sign In"]}, "landing"),
        ({"len": 400, "lc": "we use cookies ... privacy", "btns": ["Accept all", "Reject"]}, "consent"),
        ({"len": 50, "lc": "loading", "btns": []}, "unknown"),
    ]
    for sig, want in cases:
        got = classify(sig)
        assert got == want, f"classify {sig.get('lc')!r} → {got}, expected {want}"

    # Bot-verification detection. The Turnstile variant is the one that used to slip
    # through as "Page rendered" and reach the LLM as if it were the article.
    def _sig(text, heading=""):
        return {"len": len(text), "lc": text.lower(), "heading": heading,
                "hasPass": False, "hasUser": False, "suserTiles": [], "acctTiles": [], "btns": []}

    challenges = [
        ("community.sap.com\nVerify you are human by completing the action below.\n"
         "community.sap.com needs to review the security of your connection before "
         "proceeding.\nRay ID: 9c1f2a0b\nPerformance & security by Cloudflare",
         "community.sap.com"),
        ("Just a moment... Enable JavaScript and cookies to continue", ""),
        ("Checking your browser before accessing community.sap.com", ""),
        ("me.sap.com", "me.sap.com"),                      # bare-hostname heading, tiny body
    ]
    for text, head in challenges:
        assert is_challenge(_sig(text, head)), f"missed challenge: {text[:50]!r}"

    not_challenges = [
        ("Not able to see APIM in integration suite " + "body text " * 120,
         "Not able to see APIM in integration suite"),
        ("Symptom The iFlow fails with HTTP 500. Resolution Redeploy the artifact. " * 12,
         "HTTP 500 from receiver"),
    ]
    for text, head in not_challenges:
        assert not is_challenge(_sig(text, head)), f"false challenge: {head!r}"

    print(f"✅ scraper self-check passed ({len(cases)} classify, "
          f"{len(challenges) + len(not_challenges)} challenge cases)")
