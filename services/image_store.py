"""Persist blobs (community images + note attachments). S3 when S3_BUCKET is set, else local disk.

Public API:
  save(data, ext, kind="img"|"doc") -> key
  url(key, filename=None) -> str
  delete(key) -> None
  read_local(key) -> bytes | None

Keys: "img/<uuid>.<ext>" or "doc/<uuid>.<ext>". S3 → presigned GET; local → "/api/files/<key>".
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


_CT = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml",
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "zip": "application/zip",
    "xml": "application/xml", "xsd": "application/xml", "wsdl": "application/xml",
    "json": "application/json", "txt": "text/plain", "log": "text/plain",
    "csv": "text/csv", "tsv": "text/tab-separated-values",
    "yaml": "text/yaml", "yml": "text/yaml", "md": "text/markdown",
    "html": "text/html", "sql": "application/sql",
    "properties": "text/plain", "groovy": "text/plain", "java": "text/plain",
}


def save(data: bytes, ext: str, kind: str = "img") -> str:
    """Store bytes, return an opaque key. ext without leading dot. kind: img|doc."""
    import uuid
    ext = (ext or "bin").lstrip(".").lower()
    kind = "doc" if kind == "doc" else "img"
    key = f"{kind}/{uuid.uuid4().hex}.{ext}"
    if using_s3():
        _client().put_object(Bucket=S3_BUCKET, Key=key, Body=data,
                             ContentType=_CT.get(ext, "application/octet-stream"))
    else:
        path = os.path.join(_LOCAL_DIR, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    return key


def url(key: str, filename: str | None = None) -> str:
    """A URL the frontend can GET. Presigned for S3, API route for local."""
    if not key:
        return ""
    if using_s3():
        try:
            params: dict = {"Bucket": S3_BUCKET, "Key": key}
            if filename:
                # Force download with the original note filename.
                safe = filename.replace('"', "")
                params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
            return _client().generate_presigned_url(
                "get_object", Params=params, ExpiresIn=_PRESIGN_TTL)
        except Exception as e:
            logger.warning(f"presign failed for {key}: {e}")
            return ""
    # Local: community images historically used /api/community/images/; both work.
    if key.startswith("img/"):
        return f"/api/community/images/{key}"
    return f"/api/files/{key}"


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
    k = save(b"\x89PNG\r\n\x1a\n-test", "png")
    assert k.startswith("img/") and k.endswith(".png"), k
    assert read_local(k) == b"\x89PNG\r\n\x1a\n-test"
    assert url(k) == f"/api/community/images/{k}"
    d = save(b"%PDF-test", "pdf", kind="doc")
    assert d.startswith("doc/") and url(d) == f"/api/files/{d}"
    assert read_local("../../etc/passwd") is None
    delete(k)
    delete(d)
    assert read_local(k) is None
    print("✅ image_store self-check passed (local mode)")
