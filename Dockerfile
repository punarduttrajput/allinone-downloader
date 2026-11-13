FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY app.py .
COPY static ./static

ENV PORT=10000
EXPOSE 10000

# One worker: download jobs are tracked in memory, so they must share a process.
CMD gunicorn --workers 1 --threads 16 --timeout 600 --bind 0.0.0.0:$PORT app:app
