#!/usr/bin/env python3
"""Web front end for photo_roster.py.

One page: upload a Workday "View Course Section Roster" PDF, get a photo
roster back. The upload is held in memory, converted, and returned in the
same response — nothing is written to disk, queued, or logged, so there is
no copy of a student record left behind for anyone to clean up.

Configuration (all via environment):
    ROSTER_PASSCODE      shared passcode; required unless ROSTER_ALLOW_OPEN=1
    ROSTER_ALLOW_OPEN    "1" to run with no passcode at all
    ROSTER_MAX_UPLOAD_MB largest accepted upload (default 25)
    ROSTER_FONT          path to a .ttf/.ttc to embed (see photo_roster.py)

Copyright (c) 2026 Christopher Slade. Licensed CC BY 4.0; see LICENSE.
"""

import hashlib
import hmac
import html
import os
import re
import sys
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from starlette.concurrency import run_in_threadpool

from photo_roster import RosterError, build_roster

WEB = Path(__file__).parent / "web"

PASSCODE = os.environ.get("ROSTER_PASSCODE", "").strip()
ALLOW_OPEN = os.environ.get("ROSTER_ALLOW_OPEN") == "1"
MAX_UPLOAD = int(os.environ.get("ROSTER_MAX_UPLOAD_MB", "25")) * 1024 * 1024

if not PASSCODE and not ALLOW_OPEN:
    sys.exit("Refusing to start: set ROSTER_PASSCODE, or ROSTER_ALLOW_OPEN=1 "
             "to deliberately run the site with no passcode.")

COOKIE = "roster_session"
COOKIE_MAX_AGE = 30 * 24 * 3600

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


# ----------------------------------------------------------------- passcode

def _token():
    """Cookie value proving the passcode was entered.

    An HMAC of a fixed string under the passcode: it changes when the
    passcode is rotated (logging everyone out) and needs no session store,
    so the container stays stateless and horizontally restartable.
    """
    return hmac.new(PASSCODE.encode(), b"photoroster/v1",
                    hashlib.sha256).hexdigest()


def authed(request):
    if not PASSCODE:
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), _token())


def secure_cookie(request):
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto == "https"


# Failed-passcode throttle: {ip: (count, window_start)}. In-process and
# deliberately so — one small container, no Redis to run alongside it. The
# counter is per uvicorn worker, so the real ceiling is FAIL_LIMIT times the
# worker count; that is still a hard stop on guessing, not an exact budget.
_fails = {}
FAIL_LIMIT, FAIL_WINDOW = 8, 600


def throttled(ip):
    count, start = _fails.get(ip, (0, 0.0))
    if time.time() - start > FAIL_WINDOW:
        return False
    return count >= FAIL_LIMIT


def note_failure(ip):
    count, start = _fails.get(ip, (0, 0.0))
    if time.time() - start > FAIL_WINDOW:
        count, start = 0, time.time()
    _fails[ip] = (count + 1, start)


# --------------------------------------------------------------- templating

def page(name, status=200, **subs):
    """Render web/<name>.html, substituting {{key}} with escaped values."""
    body = (WEB / f"{name}.html").read_text()
    for key, value in subs.items():
        body = body.replace("{{" + key + "}}", html.escape(str(value)))
    body = re.sub(r"\{\{\w+\}\}", "", body)
    return HTMLResponse(body, status_code=status)


# ------------------------------------------------------------------- routes

@app.get("/up", include_in_schema=False)
def up():
    """Kamal's health check. No passcode: it must answer before deploy."""
    return PlainTextResponse("OK")


@app.get("/style.css", include_in_schema=False)
def style():
    return Response((WEB / "style.css").read_text(), media_type="text/css",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/")
def index(request: Request):
    if not authed(request):
        return page("login")
    return page("index", max_mb=MAX_UPLOAD // (1024 * 1024))


@app.post("/login")
def login(request: Request, passcode: str = Form("")):
    ip = request.client.host if request.client else "?"
    if throttled(ip):
        return page("login", status=429,
                    error="Too many tries. Wait ten minutes and try again.")
    if not hmac.compare_digest(passcode.strip(), PASSCODE):
        note_failure(ip)
        return page("login", status=401, error="That passcode is not right.")

    _fails.pop(ip, None)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE, _token(), max_age=COOKIE_MAX_AGE,
                        httponly=True, samesite="lax",
                        secure=secure_cookie(request))
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE)
    return response


def _download_name(upload_name):
    """<upload>_photo_roster.pdf, reduced to characters safe in a header."""
    stem = Path(upload_name or "roster").stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")[:80]
    return f"{stem or 'roster'}_photo_roster.pdf"


@app.post("/roster")
async def roster(request: Request,
                 file: UploadFile = File(...),
                 cols: int = Form(5),
                 rows: int = Form(5),
                 sort: bool = Form(False),
                 course: str = Form(""),
                 title: str = Form(""),
                 subtitle: str = Form("")):
    if not authed(request):
        return JSONResponse({"error": "Your session expired. Reload the page."},
                            status_code=401)

    limit_mb = MAX_UPLOAD // (1024 * 1024)
    too_big = {"error": f"That file is larger than {limit_mb} MB."}

    # Read with a running cap rather than trusting Content-Length, so a
    # lying or absent header cannot push a huge body into memory.
    chunks, size = [], 0
    while chunk := await file.read(1 << 20):
        size += len(chunk)
        if size > MAX_UPLOAD:
            return JSONResponse(too_big, status_code=413)
        chunks.append(chunk)
    data = b"".join(chunks)

    if not data:
        return JSONResponse({"error": "No file was uploaded."}, status_code=400)
    if not data.startswith(b"%PDF"):
        return JSONResponse({"error": "That does not look like a PDF."},
                            status_code=400)

    cols = max(2, min(10, cols))
    rows = max(2, min(10, rows))

    try:
        # PyMuPDF work is CPU-bound and blocking; keep it off the event loop.
        pdf, info = await run_in_threadpool(
            build_roster, data,
            course=course.strip() or None, title=title.strip() or None,
            subtitle=subtitle.strip()[:120], cols=cols, rows=rows, sort=sort)
    except RosterError as err:
        return JSONResponse({"error": str(err)}, status_code=422)
    except Exception:
        # Never surface a traceback: it can quote names out of the roster.
        return JSONResponse(
            {"error": "Something went wrong reading that roster."},
            status_code=500)

    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition":
            f'attachment; filename="{_download_name(file.filename)}"',
        "Cache-Control": "no-store",
        "X-Roster-Students": str(info["students"]),
        "X-Roster-Missing-Photos": str(info["missing_photos"]),
        "X-Roster-Pages": str(info["pages"]),
    })
