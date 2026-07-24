"""Error DNA Knowledge Base — FastAPI Backend"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware

from db import init_db
from config import PORT, HOST
from services.auth import require_auth
from routes.health import router as health_router
from routes.auth import router as auth_router
from routes.urls import router as urls_router
from routes.summaries import router as summaries_router
from routes.credentials import router as credentials_router
from routes.scheduler import router as scheduler_router
from routes.compat import router as compat_router
from routes.community import router as community_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: initialize DB + scheduler. Shutdown: cleanup."""
    await init_db()
    print(f"🚀 Error DNA Backend starting on port {PORT}")
    
    # Start the background scheduler
    try:
        from services.scheduler import start
        await start()
        print("⏱ Background scheduler started")
    except Exception as e:
        print(f"⚠️ Scheduler start failed: {e} (may need openclaw browser running)")
    
    yield
    
    # Shutdown
    print("🛑 Server shutting down...")


app = FastAPI(
    title="Error DNA Knowledge Base",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — allow all origins during development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Open routes — no token needed.
app.include_router(health_router)   # health checks / load balancers
app.include_router(auth_router)     # POST /api/auth/login
from routes.images import router as images_router
from routes.files import router as files_router
app.include_router(images_router)   # /api/community/images/<key> — <img> can't send a token
app.include_router(files_router)    # /api/files/<key> — note attachments (local mode)

# Everything else requires a valid Bearer token (login gates the whole app).
_auth = [Depends(require_auth)]
app.include_router(urls_router, dependencies=_auth)
app.include_router(summaries_router, dependencies=_auth)
app.include_router(credentials_router, dependencies=_auth)
app.include_router(scheduler_router, dependencies=_auth)
app.include_router(compat_router, dependencies=_auth)  # /api/families — frontend-shaped adapter
app.include_router(community_router, dependencies=_auth)  # /api/community/* — SAP Community


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
