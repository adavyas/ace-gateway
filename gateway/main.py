import logging

from fastapi import FastAPI

from .routers.me import router as me_router
from .routers.agent import router as agent_router
from .routers.ace_proxy import router as ace_proxy_router
from .routers.messaging import router as messaging_router
from .routers.runtime import router as runtime_router

logger = logging.getLogger(__name__)

app = FastAPI(title="Ace Gateway Backend")

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(me_router)
app.include_router(agent_router)
app.include_router(ace_proxy_router)
app.include_router(messaging_router)
app.include_router(runtime_router)


# ---------------------------------------------------------------------------
# Built-in routes
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
