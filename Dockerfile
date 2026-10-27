# syntax=docker/dockerfile:1
FROM python:3.13-slim

# Noto Sans covers the Latin Extended letters and modifier letters that
# Polynesian names need — kahakō (ā ē ī ō ū) and the ʻokina (U+02BB) — which
# the built-in Helvetica cannot draw. photo_roster.py finds it by path.
#
# Do not reach for Debian's fonts-noto-cjk if you need Chinese, Japanese or
# Korean names: it ships a .ttc collection that MuPDF mis-subsets, producing
# a ~14 MB PDF with glyphs missing. COPY a single-face .ttf into the image
# and set ROSTER_FONT to it instead.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-core \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt \
 && python -m pip uninstall -y pip

COPY photo_roster.py app.py ./
COPY web/ ./web/

RUN useradd --system --create-home app
USER app

EXPOSE 8000

# --forwarded-allow-ips: kamal-proxy runs in its own container, so its address
# is not loopback; without this uvicorn ignores its X-Forwarded-* headers and
# every request looks like it came from the proxy over plain http.
CMD ["uvicorn", "app:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--no-server-header"]
