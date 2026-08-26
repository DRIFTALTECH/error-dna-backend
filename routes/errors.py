"""Error diagnose API — OAuth Bearer (or UI JWT) required."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from services.auth import require_auth
from services.error_diagnose import diagnose

router = APIRouter(prefix="/api/errors", tags=["errors"])


class DiagnoseBody(BaseModel):
    error_text: str = Field(..., min_length=1)
    source: str | None = None


@router.post("/diagnose")
async def diagnose_error(body: DiagnoseBody, caller: str = Depends(require_auth)):
    """RAG-first error chain. Returns distinct error + slim solution notes (no SAP note metadata)."""
    return await diagnose(body.error_text, caller=caller, source=body.source)
