"""Public file serving for LOCAL storage mode (dev). S3 mode uses presigned URLs.
Same opacity model as community images — unguessable uuid keys, no auth on GET.
"""

from fastapi import APIRouter, HTTPException, Response

router = APIRouter(prefix="/api/files", tags=["files"])


@router.get("/{key:path}")
async def serve_file(key: str, download: str | None = None):
    from services.image_store import read_local, using_s3, _CT
    if using_s3():
        raise HTTPException(404, "files are served via presigned S3 URLs")
    data = read_local(key)
    if data is None:
        raise HTTPException(404, "file not found")
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{download.replace(chr(34), "")}"'
    return Response(
        content=data,
        media_type=_CT.get(ext, "application/octet-stream"),
        headers=headers,
    )
