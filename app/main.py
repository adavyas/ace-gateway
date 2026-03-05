import logging

from fastapi import FastAPI

from .routers.me import router as me_router
from .routers.agent import router as agent_router
from .routers.ace_proxy import router as ace_proxy_router
from .routers.runtime import router as runtime_router
from .tunnel import tunnel_manager

logger = logging.getLogger(__name__)

app = FastAPI(title="Ace Gateway Backend")

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(me_router)
app.include_router(agent_router)
app.include_router(ace_proxy_router)
app.include_router(runtime_router)


# ---------------------------------------------------------------------------
# Lifespan events
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def _startup() -> None:
    """Start the SSH tunnel manager when the FastAPI application boots.

    The tunnel manager opens an SSH port-forward from a random local port to
    the ACE backend HTTP server on the VPS. It also launches a background
    asyncio task that monitors tunnel health and reconnects when the tunnel
    drops.
    """
    logger.info("Application startup: initialising SSH tunnel to ACE VPS.")
    try:
        await tunnel_manager.start()
        logger.info(
            "SSH tunnel started. ACE backend reachable at %s",
            tunnel_manager.base_url,
        )
    except Exception as exc:
        # Log but don't crash the app — ensure_running() will retry per request.
        logger.warning(
            "SSH tunnel could not be established at startup (will retry): %s", exc
        )


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Gracefully close the SSH tunnel on application shutdown."""
    logger.info("Application shutdown: stopping SSH tunnel.")
    await tunnel_manager.stop()


# ---------------------------------------------------------------------------
# Built-in routes
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
