from contextlib import asynccontextmanager
import json

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes.jobs import router as jobs_router
from app.config import get_settings
from app.database import init_db
from app.errors import AppError
from app.logging_config import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    init_db()
    yield


settings = get_settings()
app = FastAPI(title="Video → AI Dataset API", version=settings.APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_origin_regex=settings.CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "detail": exc.detail}},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": "http_error", "message": str(exc.detail), "detail": None}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "validation_error", "message": "Request validation failed", "detail": json.loads(json.dumps(exc.errors(), default=str))}},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": str(exc), "detail": None}},
    )


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": settings.APP_VERSION}


@app.get("/api/capabilities")
def local_capabilities() -> dict:
    from app.utils.runtime_capabilities import capabilities
    return capabilities()


@app.get("/api/health/extraction")
def extraction_health() -> dict:
    """Everything that decides whether a URL can be fetched, in one place.

    Exists because the failure this diagnoses ("is yt-dlp current? is
    impersonation actually working? are the cookies stale?") previously
    required reading job logs and guessing. Contains no cookie values, no
    tokens and no environment secrets — only shape and age.
    """
    from app.utils import ytdlp
    from app.utils.diagnostics import ffmpeg_available

    try:
        version = ytdlp.get_version()
        installed = True
    except Exception as exc:  # noqa: BLE001 - a missing binary is a finding, not a 500
        version, installed = str(exc), False

    impersonation = ytdlp.impersonation_available() if installed else False
    return {
        "yt_dlp": {
            "installed": installed,
            "version": version,
            "channel": settings.YTDLP_CHANNEL,
            "update_on_startup": settings.YTDLP_UPDATE_ON_STARTUP,
        },
        "impersonation": {
            "available": impersonation,
            "configured_target": settings.YTDLP_IMPERSONATE_TARGET,
            "targets": ytdlp.impersonate_targets() if installed else [],
        },
        "ffmpeg": ffmpeg_available(),
        "cookies": ytdlp.cookie_file_report(),
        # Redacted: a proxy URL routinely carries credentials.
        "proxy": {"configured": bool(settings.YTDLP_PROXY), "target": ytdlp.proxy_status()},
        "tiktok": {
            "device_id_configured": bool(settings.TIKTOK_DEVICE_ID),
            "mobile_api_enabled": bool(settings.TIKTOK_DEVICE_ID),
        },
    }


app.include_router(jobs_router)
