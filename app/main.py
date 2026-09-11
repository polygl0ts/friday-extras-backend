from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth import get_current_identity
from app.config import settings
from app.db import create_db_and_tables
from app.rctf_client import TeamIdentity
from app.routers import admin, intro2, writeups


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(title="friday-extras-backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(writeups.router, prefix="/api")
app.include_router(intro2.router, prefix="/api")
app.include_router(admin.router, prefix="/api")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/me")
def me(identity: TeamIdentity = Depends(get_current_identity)) -> dict[str, object]:
    """Echoes back who a bearer token resolves to."""
    return {
        "team_id": identity.team_id,
        "team_name": identity.team_name,
        "is_admin": identity.is_admin,
    }
