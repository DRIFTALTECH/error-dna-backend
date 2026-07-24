"""Persist community post images. S3 when S3_BUCKET is set, else local disk (dev).

Public API:
  save(data: bytes, ext: str) -> key            # store bytes, return an opaque key
  url(key: str) -> str                          # a URL the frontend can load
  delete(key: str) -> None                      # best-effort remove

Keys look like "img/<uuid>.<ext>". On S3 we return a presigned GET URL (private
bucket, no public exposure); on local disk we return "/api/community/images/<key>"
served by a FastAPI route.
"""

import os
import logging

logger = logging.getLogger(__name__)

S3_BUCKET = os.getenv("S3_BUCKET", "").strip()
S3_REGION = os.getenv("S3_REGION", os.getenv("AWS_REGION", "ap-south-2")).strip()
_LOCAL_DIR = os.getenv("IMAGE_LOCAL_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "images"))
_PRESIGN_TTL = 7 * 24 * 3600  # 7 days

_s3 = None


def _client():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3", region_name=S3_REGION)
    return _s3


def using_s3() -> bool:
    return bool(S3_BUCKET)


_CT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
       "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml"}


def save(data: bytes, ext: str) -> str:
    """Store image bytes, return an opaque key. ext without leading dot."""
    import uuid
    ext = (ext or "png").lstrip(".").lower()
    key = f"img/{uuid.uuid4().hex}.{ext}"
    if using_s3():
        _client().put_object(Bucket=S3_BUCKET, Key=key, Body=data,
                             ContentType=_CT.get(ext, "application/octet-stream"))
    else:
        path = os.path.join(_LOCAL_DIR, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    return key


def url(key: str) -> str:
    """A URL the frontend can GET. Presigned for S3, API route for local."""
    if not key:
        return ""
    if using_s3():
        try:
            return _client().generate_presigned_url(
                "get_object", Params={"Bucket": S3_BUCKET, "Key": key}, ExpiresIn=_PRESIGN_TTL)
        except Exception as e:
            logger.warning(f"presign failed for {key}: {e}")
            return ""
    return f"/api/community/images/{key}"


def read_local(key: str) -> bytes | None:
    """Read local bytes for the FastAPI serving route (local mode only)."""
    path = os.path.join(_LOCAL_DIR, key)
    if not os.path.abspath(path).startswith(os.path.abspath(_LOCAL_DIR)):
        return None  # path-traversal guard
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def delete(key: str) -> None:
    if not key:
        return
    try:
        if using_s3():
            _client().delete_object(Bucket=S3_BUCKET, Key=key)
        else:
            os.remove(os.path.join(_LOCAL_DIR, key))
    except Exception:
        pass


if __name__ == "__main__":
    # self-check (local mode): save → url → read → delete
    k = save(b"\x89PNG\r\n\x1a\n-test", "png")
    assert k.startswith("img/") and k.endswith(".png"), k
    assert read_local(k) == b"\x89PNG\r\n\x1a\n-test"
    assert url(k) == f"/api/community/images/{k}"
    assert read_local("../../etc/passwd") is None  # traversal blocked
    delete(k)
    assert read_local(k) is None
    print("✅ image_store self-check passed (local mode)")
