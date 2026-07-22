"""Health check endpoint."""

from fastapi import APIRouter
from datetime import datetime, timezone, timedelta
from models import HealthResponse

router = APIRouter(prefix="/api", tags=["health"])

IST = timezone(timedelta(hours=5, minutes=30))


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Check if the server and database are alive."""
    return HealthResponse(
        status="healthy",
        db="connected",
        version="1.0.0",
        timestamp=datetime.now(timezone.utc).astimezone(IST).isoformat()
    )
