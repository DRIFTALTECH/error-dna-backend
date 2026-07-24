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

from config import PREFERRED_SUSER

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

MAX_STEPS = 18          # hard cap so a redirect loop can't spin forever
STEP_COOLDOWN = 5       # openclaw cooldown after each browser command
MAX_LOADING_WAITS = 6   # extra probes to allow while a page is still rendering
NAV_RETRIES = 3         # openclaw navigate is flaky under load — retry before failing

# One Chrome / one openclaw gateway: notes scraper, community ingest, and
# credential test-login must never interleave browser commands.
_BROWSER_LOCK = threading.Lock()


def _ts() -> str:
    return datetime.now(IST).strftime("%H:%M:%S")


# Placeholder/skeleton markers — a page that shows these hasn't rendered yet.
_LOADING_KW = ("not shown", "please wait", "loading…", "loading...", "just a moment",
               "header title", "header subtitle")


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


# Account of the last successful login. When scrape_note is handed a DIFFERENT
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


def _is_login(text: str) -> bool:
    """Kept for callers/tests: does this text look like an auth wall, not content?"""
    t = (text or "").lower()
    if any(k in t for k in ("sign in", "email, user id", "forgot password", "keep me signed in", "account selection")):
        return True
    if "sap for me" in t and len(text or "") < 300:
        return True
    return False


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


def _drive_to_content(url: str, username: str, password: str) -> tuple[bool, str, str, list]:
    """Loop probe→classify→act until the target article is on screen.

    Returns (ok, text, error, trace). ok=False carries a machine-readable error:
    mfa_required / needs_login (no creds) / stuck:<state> / probe_failed / max_steps.
    `trace` is an ordered list of {at, phase, status, message, detail} steps for the UI.
    """
    trace: list = []

    def rec(phase, status, message, detail=None):
        trace.append({"at": _ts(), "phase": phase, "status": status,
                      "message": message, "detail": detail})

    last = None
    repeats = 0
    loading_waits = 0
    for step in range(MAX_STEPS):
        sig = _probe()
        if sig is None:
            # Probes can miss a mid-navigation frame — retry twice with a pause.
            rec("probe", "warn", f"Probe {step} returned nothing — retrying")
            time.sleep(4)
            sig = _probe() or (time.sleep(4) or _probe())
            if sig is None:
                rec("probe", "error", "Browser probe failed after retries",
                    "openclaw returned no DOM — is the headless browser running/logged in?")
                return False, "", "probe_failed", trace

        state = classify(sig)
        snippet = (sig.get("lc", "")[:120]).replace("\n", " ").strip()
        cururl = sig.get("url", "")
        logger.info(f"  [step {step}] state={state} len={sig.get('len')} "
                    f"tiles={len(sig.get('suserTiles', []))}")
        detail = f"url={cururl} len={sig.get('len')} · {snippet}" if cururl else f"len={sig.get('len')} · {snippet}"

        if state == "target":
            # Guard against a stale-DOM race: during navigation the probe can still
            # see the PREVIOUS note's content while the URL is already the login IdP.
            # A real article is never on accounts.sap.com — wait it out.
            if "accounts.sap.com" in (cururl or ""):
                loading_waits += 1
                rec("loading", "info", "Target-like content but still on login domain — waiting", detail)
                if loading_waits <= MAX_LOADING_WAITS:
                    time.sleep(5)
                    continue
            rec("extract", "ok", _STATE_MSG["target"], detail)
            ok, text = _get_text()
            # Page may still be settling when it first matches — retry a few times.
            tries = 0
            while (not ok or not text or len(text) < 100) and tries < 3:
                tries += 1
                rec("extract", "info", f"Content thin ({len((text or '').strip())} chars) — waiting (retry {tries})")
                time.sleep(5)
                ok, text = _get_text()
            if ok and text and len(text.strip()) >= 100:
                rec("done", "ok", f"Extracted {len(text)} chars")
                return True, text, "", trace
            rec("done", "error", "Extraction failed — page matched but content too thin", detail)
            return False, "", "extraction_failed", trace

        if state == "mfa":
            rec("mfa", "error", _STATE_MSG["mfa"], detail)
            return False, "", "mfa_required", trace

        needs_creds = state in ("landing", "login_user", "login_pass")
        if needs_creds and (not username or not password):
            rec("login", "error", "Login required but no credentials for this account", detail)
            return False, "", "needs_login", trace

        # Still-rendering skeleton — wait it out instead of calling it stuck.
        if state == "unknown" and _looks_loading(sig):
            loading_waits += 1
            rec("loading", "info", f"Page still rendering (wait {loading_waits}/{MAX_LOADING_WAITS})", detail)
            if loading_waits <= MAX_LOADING_WAITS:
                time.sleep(6)
                continue
            rec("done", "error", "Page never finished loading", detail)
            return False, "", f"stuck:loading:{snippet}", trace

        if state == "unknown":
            repeats += 1
            rec("unknown", "warn", f"Unrecognized page (attempt {repeats})", detail)
            if repeats >= 3:
                return False, "", f"stuck:{snippet}", trace
            time.sleep(5)
            continue

        # Loop guard: same actionable state 3× running = we're not advancing.
        repeats = repeats + 1 if state == last else 0
        last = state
        if repeats >= 3:
            rec("done", "error", f"Stuck on '{state}' — not advancing", detail)
            return False, "", f"stuck:{state}", trace

        rec(state, "ok", _STATE_MSG.get(state, f"Handling {state}"), detail)
        _act(state, username, password)

    rec("done", "error", "Gave up after max steps", f"MAX_STEPS={MAX_STEPS}")
    return False, "", "max_steps", trace


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


def scrape_note(url: str, username: str = None, password: str = None) -> dict:
    """
    Scrape a SAP note. Drives through login/account-select/consent as needed.
    Also downloads + extracts attachment text (best-effort) and appends it.
    Returns { success, raw_text, clean_text, title, error, trace, attachments }.
    """
    with _BROWSER_LOCK:
        return _scrape_note_locked(url, username, password)


def _scrape_note_locked(url: str, username: str = None, password: str = None) -> dict:
    global _last_account
    # Account changed since last login (rotate) → drop old session, force fresh login.
    switched = bool(username) and username != _last_account
    if switched and _last_account is not None:
        _clear_session()

    nav_ok, nav_out = _navigate(url, timeout=30)
    time.sleep(5)
    nav_step = [{"at": _ts(), "phase": "navigate",
                 "status": "ok" if nav_ok else "error",
                 "message": f"Opened {url}" if nav_ok else "Navigate command failed",
                 "detail": None if nav_ok else (nav_out[:300] or "openclaw browser navigate returned non-zero")}]

    ok, text, err, trace = _drive_to_content(url, username, password)
    trace = nav_step + trace
    if not ok:
        # Preserve the old error vocabulary the scheduler/UI already understand.
        mapped = {"needs_login": "session_expired"}.get(err, err)
        logger.warning(f"  ⚠️ drive failed: {mapped}")
        return {"success": False, "error": mapped, "trace": trace}

    # Passed the login wall as this account — remember it so we only re-login on switch.
    if username:
        _last_account = username

    if len(text) < 100:
        trace.append({"at": _ts(), "phase": "done", "status": "error",
                      "message": "Extracted text too short", "detail": f"{len(text)} chars"})
        return {"success": False, "error": "too_short", "raw_text": text, "trace": trace}

    # ---- extract sections from the raw text (unchanged) ----
    lines = text.split("\n")
    title = ""
    sections = {}
    current = None
    content = []

    for line in lines:
        s = line.strip()
        if s.startswith("3780") or s.startswith("377") or s.startswith("376"):
            if " - " in s:
                title = s.split(" - ", 1)[1].strip() if " - " in s else s.strip()

        low = s.lower()
        if low in ("symptom", "environment", "resolution", "keywords"):
            if current and content:
                sections[current] = "\n".join(content).strip()
            current = low
            content = []
        elif current and s:
            if low not in ("object status", "quality rating", "description", "products", "attributes", "available languages", "rate this document", "see also"):
                content.append(s)

    if current and content:
        sections[current] = "\n".join(content).strip()

    clean = []
    if title:
        clean.append(f"TITLE: {title}")
    for k in ("symptom", "environment", "resolution", "keywords"):
        if sections.get(k):
            clean.append(f"{k.upper()}:\n{sections[k]}")
    clean_text = "\n\n".join(clean) if clean else text

    logger.info(f"  ✅ Scraped {len(text)} chars, title: {title[:60]}")
    trace.append({"at": _ts(), "phase": "parse", "status": "ok",
                  "message": f"Parsed article: {title[:60]}" if title else "Parsed article",
                  "detail": f"{len(clean_text)} chars cleaned"})

    # Best-effort: download + extract attachment text, append for the LLM. Never blocks.
    # attachments = [{name, ext, data}] — scheduler persists data to S3/local.
    attachments = []
    try:
        att_text, attachments = _fetch_attachments()
        if attachments:
            if att_text:
                clean_text = clean_text + "\n\n" + att_text
                text = text + "\n\n" + att_text
            trace.append({"at": _ts(), "phase": "attachments", "status": "ok",
                          "message": f"Attached {len(attachments)} file(s)",
                          "detail": ", ".join(a["name"] for a in attachments)})
        else:
            trace.append({"at": _ts(), "phase": "attachments", "status": "info",
                          "message": "No attachments"})
    except Exception as e:
        logger.warning(f"  ⚠️ attachment fetch failed: {e}")
        trace.append({"at": _ts(), "phase": "attachments", "status": "warn",
                      "message": "Attachment fetch failed — using note text only", "detail": str(e)})

    return {
        "success": True,
        "raw_text": text,
        "clean_text": clean_text,
        "title": title,
        "trace": trace,
        "attachments": attachments,
    }


# ---- SAP Community (public, no login) ------------------------------------

# Cloudflare's "just a moment" JS challenge is in _LOADING_KW, so _looks_loading
# already reports the challenge page as "still loading". A real browser clears it
# on its own within a few seconds; we just keep re-probing until it does.
COMMUNITY_LOADING_WAITS = 10


# Keep image capture tiny on EC2: base64 travels Chrome → openclaw stdout → Python.
# 6×4MB data-URLs previously blew small instances (OOM → whole box dies).
MAX_COMMUNITY_IMAGES = 3
MAX_COMMUNITY_IMAGE_BYTES = 400_000  # ~400 KB each
MAX_COMMUNITY_TEXT_CHARS = 40_000    # trim before returning to ingest/LLM

# In-page: collect content images (skip avatars/icons/emoji), each with its ALT,
# the nearby text (context for placement), and the bytes as a data URL — fetched
# in-page so the browser's Cloudflare cookie is used (the CDN is gated too).
_IMAGES_FN = r"""async ()=>{
  const clean = s => (s||'').replace(/\s+/g,' ').trim();
  const maxBytes = %MAX_BYTES%;
  const imgs = [...document.querySelectorAll('img')].filter(i =>
    i.offsetParent && i.naturalWidth > 80 && i.naturalHeight > 80 &&
    !/avatar|emoji|icon|rank|badge|sprite|logo|smiley/i.test((i.src||'')+(i.className||'')));
  const seen = new Set(); const out = [];
  for (const i of imgs) {
    if (out.length >= %MAX%) break;
    const src = i.src; if (!src || seen.has(src)) continue; seen.add(src);
    // context = alt, figure caption, or the text of the nearest block ancestor.
    let ctx = clean(i.alt);
    const fig = i.closest('figure'); if (fig) ctx = clean(fig.textContent) || ctx;
    if (!ctx) { let n = i.parentElement; for (let k=0;k<4&&n;k++){ const t=clean(n.textContent); if (t.length>15){ctx=t;break;} n=n.parentElement; } }
    let dataUrl = '';
    try { const r = await fetch(src); const b = await r.blob();
      if (b.size > 0 && b.size < maxBytes) dataUrl = await new Promise(res=>{const fr=new FileReader();fr.onload=()=>res(fr.result);fr.onerror=()=>res('');fr.readAsDataURL(b);}); } catch(e){}
    out.push({ src, alt: clean(i.alt).slice(0,120), context: ctx.slice(0,400), dataUrl });
  }
  return JSON.stringify(out);
}""".replace("%MAX%", str(MAX_COMMUNITY_IMAGES)).replace("%MAX_BYTES%", str(MAX_COMMUNITY_IMAGE_BYTES))


def _decode_json(out: str):
    """Decode openclaw evaluate output. JSON.stringify returns are often
    double-wrapped in quotes — peel up to twice (same pattern as _probe)."""
    val = (out or "").strip()
    for _ in range(2):
        if isinstance(val, (dict, list)):
            break
        try:
            val = json.loads(val)
        except Exception:
            return None
    return val


def _extract_community_images() -> list:
    """Return [{ref,'src','alt','context','data_b64','ext'}] for content images on the page."""
    ok, out = _run(["evaluate", "--fn", _IMAGES_FN], timeout=45)
    if not ok or not out:
        return []
    raw = _decode_json(out)
    if not isinstance(raw, list):
        return []
    images = []
    for idx, im in enumerate(raw, 1):
        if not isinstance(im, dict):
            continue
        data_url = im.get("dataUrl") or ""
        if not data_url.startswith("data:image/"):
            continue
        try:
            header, b64 = data_url.split(",", 1)
            ext = header.split("/", 1)[1].split(";", 1)[0].lower()  # data:image/png;base64
            ext = {"jpeg": "jpg", "svg+xml": "svg"}.get(ext, ext)
        except Exception:
            continue
        images.append({
            "ref": f"image_{idx}",
            "src": im.get("src", ""),
            "alt": im.get("alt", ""),
            "context": im.get("context", ""),
            "data_b64": b64,
            "ext": ext,
        })
    return images


def scrape_community(url: str) -> dict:
    """Scrape a public SAP Community page. No login — navigate, wait out the
    Cloudflare challenge + render, extract the main text + content images.
    Returns { success, raw_text, clean_text, title, images, error, trace }."""
    with _BROWSER_LOCK:
        return _scrape_community_locked(url)


def _scrape_community_locked(url: str) -> dict:
    trace = []

    def tr(phase, status, message, detail=None):
        trace.append({"at": _ts(), "phase": phase, "status": status,
                      "message": message, "detail": detail})

    nav_ok, nav_out = _navigate(url, timeout=30)
    tr("navigate", "ok" if nav_ok else "error",
       f"Opened {url}" if nav_ok else "Navigate command failed",
       None if nav_ok else (nav_out[:300] or None))
    if not nav_ok:
        _release_browser_page()
        return {"success": False, "error": "navigate_failed", "trace": trace}

    # Poll until the challenge clears and real content renders (or we give up).
    sig = _probe()
    waits = 0
    while (sig is None or _looks_loading(sig)) and waits < COMMUNITY_LOADING_WAITS:
        time.sleep(STEP_COOLDOWN)
        sig = _probe()
        waits += 1
    cleared = sig is not None and not _looks_loading(sig)
    tr("render", "ok" if cleared else "warn",
       "Page rendered" if cleared else f"Still loading after {waits} waits",
       None if cleared else "Cloudflare challenge may not have cleared")

    ok, text = _get_text()
    text = text or ""
    if not ok or len(text) < 100:
        tr("done", "error", "Extracted text too short", f"{len(text)} chars")
        _release_browser_page()
        return {"success": False, "error": "too_short", "raw_text": text[:500], "trace": trace}

    # Title: page heading if we have one, else the first non-trivial line.
    title = (sig or {}).get("heading") or ""
    if not title:
        for line in text.split("\n"):
            if len(line.strip()) > 8:
                title = line.strip()[:120]
                break

    # Cap text — community SPA dumps are huge; LLM only needs the article body.
    if len(text) > MAX_COMMUNITY_TEXT_CHARS:
        tr("parse", "warn", f"Truncated text {len(text)} → {MAX_COMMUNITY_TEXT_CHARS} chars",
           title[:60] if title else None)
        text = text[:MAX_COMMUNITY_TEXT_CHARS]
    else:
        tr("parse", "ok", f"Extracted {len(text)} chars",
           title[:60] if title else None)

    # Content images + their placement context (best-effort; never blocks).
    images = []
    try:
        images = _extract_community_images()
        if images:
            tr("images", "ok", f"Captured {len(images)} image(s)",
               ", ".join(i["ref"] for i in images))
        else:
            tr("images", "info", "No content images")
    except Exception as e:
        tr("images", "warn", "Image capture failed — text only", str(e))

    # Drop the heavy SPA from Chrome RAM before the next URL.
    _release_browser_page()

    return {"success": True, "raw_text": text, "clean_text": text,
            "title": title, "images": images, "trace": trace}


def _release_browser_page() -> None:
    """Navigate away so Chromium can GC the previous community SPA + image blobs."""
    try:
        _run(["navigate", "about:blank"], timeout=15)
    except Exception:
        pass


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
    print(f"✅ classify() self-check passed ({len(cases)} cases)")
