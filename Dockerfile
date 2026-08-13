# DispatchDesk Sales Inbox Router — single container serving the API + the UI
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first for better layer caching
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy the backend (app.py) and the frontend it serves at /
COPY backend/ backend/
COPY frontend/ frontend/

WORKDIR /app/backend

EXPOSE 8000

# No API key? No problem — the deterministic fallback router takes over.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
