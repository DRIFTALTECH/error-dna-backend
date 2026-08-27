"""Error diagnose API — OAuth Bearer (or UI JWT) required."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from services.auth import require_auth
from services.error_diagnose import DiagnoseBusy, diagnose

router = APIRouter(prefix="/api/errors", tags=["errors"])


class DiagnoseBody(BaseModel):
    error_text: str = Field(..., min_length=1)
    source: str | None = None


@router.post("/diagnose")
async def diagnose_error(body: DiagnoseBody, caller: str = Depends(require_auth)):
    """RAG-first error chain. Returns the distinct error plus slim solution notes.

    409 when another diagnose is running: calls are refused, never queued.
    """
    try:
        return await diagnose(body.error_text, caller=caller, source=body.source)
    except DiagnoseBusy:
        raise HTTPException(409, "A diagnose is already running. Trigger again.")
    except ValueError as e:
        # min_length=1 lets a whitespace-only body through; this is that case.
        raise HTTPException(400, str(e))
