"""Public image serving for LOCAL storage mode (dev). In S3 mode this is unused —
the frontend loads presigned S3 URLs directly. Public (no auth) because <img> tags
can't send a bearer token; the key is an unguessable uuid so it's effectively opaque.
"""

from fastapi import APIRouter, HTTPException, Response

router = APIRouter(prefix="/api/community/images", tags=["images"])

_CT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
       "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml"}


@router.get("/{key:path}")
async def serve_image(key: str):
    from services.image_store import read_local, using_s3
    if using_s3():
        raise HTTPException(404, "images are served via presigned S3 URLs")
    data = read_local(key)
    if data is None:
        raise HTTPException(404, "image not found")
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    return Response(content=data, media_type=_CT.get(ext, "application/octet-stream"))
