"""Pytest fixtures: import the backend app with a throwaway SQLite DB and no Gemini key."""
import os
import sys
import tempfile

# Must be set before importing app.py so the engine points at a temp DB.
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.gettempdir(), "dispatchdesk_test.db"))
os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("API_TOKEN", "")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import app as backend  # noqa: E402
