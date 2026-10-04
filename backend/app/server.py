"""
The deployed app: the API under /api, plus the built React app on everything else.

One service for both keeps the sign-in cookie on a single origin, which is simpler and safer than
cross-site cookies. Locally you run the API and the Vite dev server separately (see dev.sh) and
never use this file.
"""

import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException  # the base class StaticFiles raises, not FastAPI's subclass

from app.main import app as api

log = logging.getLogger(__name__)

DEFAULT_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


class SinglePageApp(StaticFiles):
    """
    Serves the built React app. React draws pages like /buy and /orders/7 in the browser, so
    a request for a page that is not a real file must be answered with index.html.
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or "." in path.rsplit("/", 1)[-1]:
                raise  # a missing FILE (app.js, logo.png) is a real 404, not a page for React to draw
            return await super().get_response("index.html", scope)


def start_deadline_loop(interval: float) -> threading.Event:
    """
    Settle due drops on a timer, inside this process, so no second service is needed. Safe to
    run anywhere and as often as you like: every step is idempotent and row-locked.
    """
    from app.db import get_session_factory
    from app.deps import get_paypal
    from app.drops import run_deadline_job

    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(interval):
            try:
                summary = run_deadline_job(get_session_factory(), get_paypal())
                if summary["settled"] or summary["expired"] or summary["errors"]:
                    log.info("deadline job: %s", summary)
            except Exception:  # keep the loop alive whatever happens
                log.exception("deadline job crashed")

    threading.Thread(target=loop, name="deadline-job", daemon=True).start()
    return stop


def build_site(dist: Path | None = DEFAULT_DIST, job_interval: float = 0) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop = start_deadline_loop(job_interval) if job_interval > 0 else None
        yield
        if stop:
            stop.set()

    site = FastAPI(title="fullbatch", lifespan=lifespan)
    site.mount("/api", api)
    if dist and dist.is_dir():
        site.mount("/", SinglePageApp(directory=dist, html=True), name="web")
    else:

        @site.get("/")
        def no_frontend():
            return JSONResponse({"message": "The web app has not been built. The API is at /api."}, status_code=200)

    return site


site = build_site(
    Path(os.getenv("FRONTEND_DIST", DEFAULT_DIST)),
    job_interval=float(os.getenv("JOB_INTERVAL_SECONDS", "0") or 0),
)
