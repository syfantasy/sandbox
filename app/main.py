from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Path

from .auth import make_token_dependency
from .config import Settings
from .models import CleanupResponse, ExecRequest, ExecResponse, HealthResponse
from .runner import SandboxRunner


settings = Settings.from_env()
runner = SandboxRunner(settings)
require_token = make_token_dependency(settings)

app = FastAPI(
    title="Private Robot Sandbox",
    version="0.1.0",
    description="An intentionally permissive, authenticated command runner for a private bot.",
)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"service": "private-robot-sandbox", "docs": "/docs"}


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(
        status="ok",
        authentication="application-bearer-token",
        auth_configured=bool(settings.token_sha256),
        active_jobs=runner.active_jobs,
        max_concurrent_jobs=settings.max_concurrent_jobs,
        default_timeout_seconds=settings.default_timeout_seconds,
        max_timeout_seconds=settings.max_timeout_seconds,
        max_output_bytes=settings.max_output_bytes,
        max_input_bytes=settings.max_input_bytes,
        max_file_output_bytes=settings.max_file_output_bytes,
        max_output_files=settings.max_output_files,
    )


@app.post(
    "/v1/exec",
    response_model=ExecResponse,
    dependencies=[Depends(require_token)],
)
async def execute(request: ExecRequest) -> ExecResponse:
    return await runner.execute(request)


@app.delete(
    "/v1/sessions/{session_id}",
    response_model=CleanupResponse,
    dependencies=[Depends(require_token)],
)
async def cleanup_session(
    session_id: Annotated[
        str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    ],
) -> CleanupResponse:
    return CleanupResponse(
        session_id=session_id,
        removed=runner.cleanup_session(session_id),
    )
