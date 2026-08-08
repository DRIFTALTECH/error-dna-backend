"""Error diagnose + cluster graph API — OAuth Bearer (or UI JWT) required."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from services.auth import require_auth
from services.error_clusters import build_graph, get_cluster, list_clusters
from services.error_diagnose import diagnose

router = APIRouter(prefix="/api/errors", tags=["errors"])


class DiagnoseBody(BaseModel):
    error_text: str = Field(..., min_length=1)
    source: str | None = None


@router.post("/diagnose")
async def diagnose_error(body: DiagnoseBody, caller: str = Depends(require_auth)):
    """RAG-first error chain. Authorization: Bearer access_token from POST /api/oauth/token."""
    return await diagnose(body.error_text, caller=caller, source=body.source)


@router.get("/clusters")
async def clusters_list(_: str = Depends(require_auth)):
    """All distinct error clusters (table view)."""
    return await list_clusters()


@router.get("/clusters/graph")
async def clusters_graph(
    min_similarity: float | None = None,
    _: str = Depends(require_auth),
):
    """Embedding-similarity cluster graph — SIMILAR edges from distinct_error_embeddings."""
    return await build_graph(min_similarity=min_similarity)


@router.get("/clusters/{cluster_id}")
async def cluster_detail(cluster_id: int, _: str = Depends(require_auth)):
    """One cluster with events + persisted solution links."""
    detail = await get_cluster(cluster_id)
    if not detail:
        raise HTTPException(404, "Cluster not found")
    return detail
