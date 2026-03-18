"""Health check endpoint."""

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health_check():
    return {"status": "ok"}
