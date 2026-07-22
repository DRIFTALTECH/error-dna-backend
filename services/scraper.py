"""Scraper — drives the SAP login as a per-page state machine, then extracts.

Instead of a fixed navigate→user→pw→signin script, we probe the page every step,
classify what it is (login form / account chooser / consent / MFA / target
content / ...), take the one action that page needs, and loop until we reach the
article or hit something a human must clear (MFA). This survives SAP inserting or
reordering interstitial pages — the old linear script did not.
"""

import json
import subprocess
import time
import logging

from config import PREFERRED_SUSER

logger = logging.getLogger(__name__)

MAX_STEPS = 14          # hard cap so a redirect loop can't spin forever
STEP_COOLDOWN = 5       # openclaw cooldown after each browser command


def _run(cmd: list, timeout: int = 30) -> tuple[bool, str]:
    try:
        r = subprocess.run(["openclaw", "browser"] + cmd, capture_output=True, text=True, timeout=timeout)
        time.sleep(STEP_COOLDOWN)
        return r.returncode == 0, r.stdout.strip() or r.stderr.strip()
    except Exception as e:
        time.sleep(STEP_COOLDOWN)
        return False, str(e)


def _get_text() -> tuple[bool, str]:
    ok, t = _run(["evaluate", "--fn", "()=>{const m=document.querySelector('[role=main]')||document.body;return m?m.innerText:''}"], timeout=30)
    return ok, t


def _clear_session() -> None:
    """Drop all cookies so a persisted login doesn't make every creds 'succeed'."""
    _run(["cookies", "clear"], timeout=15)


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
        _click_containing(["continue", "next", "sign in"])
        time.sleep(5)
    elif state == "login_pass":
        # Some SAP tenants show user + password on ONE page (j_username + j_password).
        # Fill the user field too if present — a no-op on password-only pages.
        _fill('input:not([type="password"]):not([type="hidden"]):not([type="checkbox"]):not([type="submit"])', username or "")
        time.sleep(1)
        _fill('input[type="password"]', password or "")
        time.sleep(2)
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


def _drive_to_content(url: str, username: str, password: str) -> tuple[bool, str, str]:
    """Loop probe→classify→act until the target article is on screen.

    Returns (ok, text, error). ok=False carries a machine-readable error:
    mfa_required / needs_login (no creds) / stuck:<state> / probe_failed / max_steps.
    """
    last = None
    repeats = 0
    for step in range(MAX_STEPS):
        sig = _probe()
        if sig is None:
            # One retry — probes can miss a mid-navigation frame.
            time.sleep(4)
            sig = _probe()
            if sig is None:
                return False, "", "probe_failed"

        state = classify(sig)
        logger.info(f"  [step {step}] state={state} len={sig.get('len')} "
                    f"tiles={len(sig.get('suserTiles', []))}")

        if state == "target":
            ok, text = _get_text()
            return (True, text, "") if ok and text else (False, "", "extraction_failed")

        if state == "mfa":
            return False, "", "mfa_required"

        needs_creds = state in ("landing", "login_user", "login_pass")
        if needs_creds and (not username or not password):
            return False, "", "needs_login"

        if state == "unknown":
            repeats += 1
            if repeats >= 2:
                snippet = (sig.get("lc", "")[:80]).replace("\n", " ")
                return False, "", f"stuck:{snippet}"
            time.sleep(5)
            continue

        # Loop guard: same actionable state 3× running = we're not advancing.
        repeats = repeats + 1 if state == last else 0
        last = state
        if repeats >= 3:
            return False, "", f"stuck:{state}"

        _act(state, username, password)

    return False, "", "max_steps"


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
    url = (login_url or "https://me.sap.com").strip()
    user = (username or "").strip()
    pw = password or ""
    if not user or not pw:
        return {"ok": False, "message": "Username and password required", "state": "needs_creds"}

    _clear_session()   # else a persisted login makes any creds look valid
    _run(["navigate", url], timeout=30)
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


def scrape_note(url: str, username: str = None, password: str = None) -> dict:
    """
    Scrape a SAP note. Drives through login/account-select/consent as needed.
    Returns { success, raw_text, clean_text, title, error }.
    """
    _run(["navigate", url], timeout=30)
    time.sleep(5)

    ok, text, err = _drive_to_content(url, username, password)
    if not ok:
        # Preserve the old error vocabulary the scheduler/UI already understand.
        mapped = {"needs_login": "session_expired"}.get(err, err)
        logger.warning(f"  ⚠️ drive failed: {mapped}")
        return {"success": False, "error": mapped}

    if len(text) < 100:
        return {"success": False, "error": "too_short", "raw_text": text}

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

    return {
        "success": True,
        "raw_text": text,
        "clean_text": clean_text,
        "title": title,
    }


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
