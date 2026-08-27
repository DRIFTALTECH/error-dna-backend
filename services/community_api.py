"""SAP Community reader — the Khoros public API, no browser and no login.

community.sap.com is Cloudflare-fronted: a headless browser on a datacenter IP
gets a managed challenge it cannot clear, so the browser path could never read a
thread. The site's own frontend reads through `/api/2.0/search`, which answers
anonymously (verified 200 from the EC2 box), and hands back more than the DOM did:

  * every message in the thread, not just what rendered
  * `is_solution` per message and `conversation.solved` per thread — the accepted
    answer is a fact here, not something the LLM has to infer from prose
  * image URLs that fetch with a plain GET, so no base64 through openclaw stdout

Thread ids are the `-p/<id>` segment of the URL, which is what community_urls
already stores as source_id — no migration.
"""

from __future__ import annotations

import html as _html
import logging
import re

import httpx

from config import (
    COMMUNITY_API_TIMEOUT,
    COMMUNITY_API_URL,
    COMMUNITY_IMAGE_MAX_BYTES,
    COMMUNITY_MAX_IMAGES,
)

logger = logging.getLogger(__name__)

# Fields worth pulling. `body` is HTML; everything else is flat.
_FIELDS = ("id,subject,body,post_time,author.login,is_solution,depth,"
           "conversation.solved,board.id")


def topic_id(url: str) -> str:
    """SAP Community URLs end in .../{slug}-p/<id>. That id is the Khoros topic id."""
    m = re.search(r"-p/(\d+)", url or "")
    return m.group(1) if m else (url or "").strip()


def html_to_text(raw: str) -> str:
    """Message HTML → readable plain text, list structure preserved."""
    h = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw or "", flags=re.S | re.I)
    h = re.sub(r"<li[^>]*>", "\n- ", h, flags=re.I)
    h = re.sub(r"<br\s*/?>|</(p|div|h[1-6]|tr|li|ol|ul|pre)>", "\n", h, flags=re.I)
    h = re.sub(r"<[^>]+>", " ", h)
    text = _html.unescape(h)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def image_urls(raw: str) -> list[str]:
    """Content image URLs from message HTML, avatars and emoji dropped."""
    found = re.findall(r'<img[^>]+src=["\']([^"\']+)', raw or "", re.I)
    out = []
    for u in found:
        u = _html.unescape(u)
        if re.search(r"avatar|emoji|icon|rank|badge|sprite|smiley", u, re.I):
            continue
        if u not in out:
            out.append(u)
    return out


async def _liql(query: str) -> dict:
    async with httpx.AsyncClient(timeout=COMMUNITY_API_TIMEOUT) as client:
        resp = await client.get(COMMUNITY_API_URL, params={"q": query},
                                headers={"Accept": "application/json"})
        resp.raise_for_status()
        return resp.json()


async def fetch_thread(url: str) -> dict:
    """One thread as text plus image references.

    Returns {ok, source_id, title, solved, answered, board, message_count, text,
    images, error}. images = [{ref, url, alt}] capped at COMMUNITY_MAX_IMAGES.
    """
    tid = topic_id(url)
    try:
        payload = await _liql(f"SELECT {_FIELDS} FROM messages WHERE topic.id='{tid}'")
    except Exception as e:
        return {"ok": False, "error": f"api_unreachable: {e}"}

    if payload.get("status") != "success":
        return {"ok": False, "error": payload.get("message") or "api_error"}
    items = payload.get("data", {}).get("items") or []
    if not items:
        return {"ok": False, "error": "not_found"}

    # Question first, then replies oldest to newest.
    items.sort(key=lambda m: (m.get("depth") or 0, m.get("post_time") or ""))
    root = items[0]

    parts, images = [], []
    for msg in items:
        who = (msg.get("author") or {}).get("login") or "unknown"
        if (msg.get("depth") or 0) == 0:
            label = "QUESTION"
        elif msg.get("is_solution"):
            label = "ACCEPTED SOLUTION"
        else:
            label = "REPLY"
        parts.append(f"{label} by {who}:\n{html_to_text(msg.get('body'))}")
        for u in image_urls(msg.get("body")):
            if len(images) >= COMMUNITY_MAX_IMAGES:
                break
            images.append({"ref": f"image_{len(images) + 1}", "url": u, "alt": ""})

    title = root.get("subject") or ""
    solved = bool((root.get("conversation") or {}).get("solved"))
    text = f"TITLE: {title}\n\n" + "\n\n".join(parts)

    return {
        "ok": True,
        "source_id": tid,
        "title": title,
        "solved": solved,
        "answered": len(items) > 1,
        "board": (root.get("board") or {}).get("id") or "",
        "message_count": len(items),
        "text": text,
        "images": images,
        "error": "",
    }


async def download_image(url: str) -> tuple[bytes | None, str]:
    """Fetch one image. Returns (data, ext); (None, '') on any failure or oversize."""
    try:
        async with httpx.AsyncClient(timeout=COMMUNITY_API_TIMEOUT,
                                     follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content
    except Exception as e:
        logger.warning("community image fetch failed (%s): %s", url[:80], e)
        return None, ""
    if not data or len(data) > COMMUNITY_IMAGE_MAX_BYTES:
        return None, ""
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
           "image/webp": "webp", "image/svg+xml": "svg"}.get(ctype, "")
    if not ext:
        ext = (re.search(r"\.(png|jpe?g|gif|webp)(?:$|\?)", url, re.I) or [None, "png"])[1].lower()
        ext = "jpg" if ext == "jpeg" else ext
    return data, ext


if __name__ == "__main__":
    # ponytail: parsing is the only branching worth a check; the network calls are
    # exercised by a live ingest run.
    assert topic_id("https://community.sap.com/t5/technology-q-a/x/qaq-p/14444408") == "14444408"
    assert topic_id("https://community.sap.com/t5/technology-blog-posts-by-sap/y/ba-p/99") == "99"
    assert topic_id("no-id-here") == "no-id-here"

    t = html_to_text("<P>Set the <B>host</B>:</P><OL><LI>Open cockpit</LI><LI>Pick&nbsp;443</LI></OL>")
    assert "Set the host :" in t.replace("  ", " ") or "Set the host" in t
    assert "- Open cockpit" in t and "- Pick" in t and "&nbsp;" not in t

    urls = image_urls('<img src="https://c/t5/image/serverpage/image-id/1i2/image-size/large?v=v2&amp;px=999">'
                      '<img src="https://c/avatar/thing.png"><img src="https://c/t5/image/serverpage/image-id/1i2">')
    assert urls == ["https://c/t5/image/serverpage/image-id/1i2/image-size/large?v=v2&px=999",
                    "https://c/t5/image/serverpage/image-id/1i2"], urls

    print("✅ community_api self-check passed")
