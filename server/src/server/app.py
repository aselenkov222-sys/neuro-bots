from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from server.auth import router
from server.bots import router as bots_router
from server.browser_action import router as browser_router
from server.db import database_responds
from server.desktops import router as desktops_router
from server.routines import router as routines_router

app = FastAPI()

_WEB_DIR = Path(__file__).resolve().parents[3] / "web"


@app.get("/health")
def health() -> Response:
    try:
        ready = database_responds()
    except SQLAlchemyError:
        ready = False
    if not ready:
        return Response(status_code=503)
    return JSONResponse({"status": "ok"})


app.include_router(router)
app.include_router(bots_router)
app.include_router(routines_router)
app.include_router(desktops_router)
app.include_router(browser_router)
app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")
