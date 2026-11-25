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

import base64
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
from starlette.formparsers import MultiPartParser

from photo_roster import RosterError, build_roster

WEB = Path(__file__).parent / "web"

PASSCODE = os.environ.get("ROSTER_PASSCODE", "").strip()
ALLOW_OPEN = os.environ.get("ROSTER_ALLOW_OPEN") == "1"
MAX_UPLOAD = int(os.environ.get("ROSTER_MAX_UPLOAD_MB", "25")) * 1024 * 1024

# Multipart overhead (boundaries, the small form fields) on top of the file.
BODY_SLACK = 1 << 20

if not PASSCODE and not ALLOW_OPEN:
    sys.exit("Refusing to start: set ROSTER_PASSCODE, or ROSTER_ALLOW_OPEN=1 "
             "to deliberately run the site with no passcode.")

# Starlette parses multipart uploads into a SpooledTemporaryFile that rolls
# over to a real file on disk past 1 MB, which would put a roster's names
# and photos in /tmp. Raise the threshold past the largest body we accept so
# an upload stays in RAM. (Production also mounts /tmp as tmpfs; see
# config/deploy.yml.)
MultiPartParser.spool_max_size = MAX_UPLOAD + BODY_SLACK

COOKIE = "roster_session"
COOKIE_MAX_AGE = 30 * 24 * 3600

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


# ----------------------------------------------------------------- passcode

def _sign(issued):
    return hmac.new(PASSCODE.encode(), f"photoroster/v2|{issued}".encode(),
                    hashlib.sha256).hexdigest()


def _token():
    """Cookie value proving the passcode was entered: "<issued>.<hmac>".

    Signed under the passcode, so rotating it logs everyone out, and no
    session store is needed — the container stays stateless. The issue time
    is checked server-side, so a copied cookie stops working after
    COOKIE_MAX_AGE rather than living until the next rotation.
    """
    issued = str(int(time.time()))
    return f"{issued}.{_sign(issued)}"


def authed(request):
    if not PASSCODE:
        return True
    issued, _, sig = request.cookies.get(COOKIE, "").partition(".")
    if not (issued.isascii() and issued.isdigit()):
        return False
    if time.time() - int(issued) > COOKIE_MAX_AGE:
        return False
    return hmac.compare_digest(sig.encode(), _sign(issued).encode())


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
    now = time.time()
    if len(_fails) > 10_000:
        # Bound memory against a flood of distinct addresses: drop expired
        # windows, and if that is not enough, start over.
        for key, (_, start) in list(_fails.items()):
            if now - start > FAIL_WINDOW:
                del _fails[key]
        if len(_fails) > 10_000:
            _fails.clear()
    count, start = _fails.get(ip, (0, 0.0))
    if now - start > FAIL_WINDOW:
        count, start = 0, now
    _fails[ip] = (count + 1, start)


# ------------------------------------------------------------------ headers

def _script_hash(name):
    """CSP hash of the page's inline <script>, so no 'unsafe-inline'."""
    text = (WEB / f"{name}.html").read_text()
    m = re.search(r"<script>(.*?)</script>", text, re.S)
    if not m:
        return "'none'"
    digest = hashlib.sha256(m.group(1).encode()).digest()
    return f"'sha256-{base64.b64encode(digest).decode()}'"


CSP = ("default-src 'none'; style-src 'self' 'unsafe-inline'; "
       f"script-src {_script_hash('index')}; "
       "connect-src 'self'; form-action 'self'; base-uri 'none'; "
       "frame-ancestors 'none'")

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


@app.middleware("http")
async def guard(request: Request, call_next):
    # Runs before FastAPI parses the form, so an upload without a valid
    # session — or one declaring a body over the limit — is turned away
    # before a byte of it is buffered.
    if request.method == "POST" and request.url.path == "/roster":
        if not authed(request):
            return JSONResponse(
                {"error": "Your session expired. Reload the page."},
                status_code=401, headers=SECURITY_HEADERS)
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_UPLOAD + BODY_SLACK:
            return JSONResponse(_too_big(), status_code=413,
                                headers=SECURITY_HEADERS)

    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if secure_cookie(request):
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


def _too_big():
    return {"error": f"That file is larger than "
                     f"{MAX_UPLOAD // (1024 * 1024)} MB."}


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
    # Compare bytes: compare_digest raises TypeError on non-ASCII str.
    if not hmac.compare_digest(passcode.strip().encode(), PASSCODE.encode()):
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
    # The session was checked by guard() before the body was parsed.

    # Exact cap on the file itself. guard() already refused any body whose
    # Content-Length was over the limit; kamal-proxy's max_request_body
    # bounds a chunked one.
    chunks, size = [], 0
    while chunk := await file.read(1 << 20):
        size += len(chunk)
        if size > MAX_UPLOAD:
            return JSONResponse(_too_big(), status_code=413)
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
