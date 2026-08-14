"""Vercel build step.

Publishes the self-contained dashboard (frontend/index.html) to public/ so
Vercel serves the UI from the CDN, independent of the Python API function.
The FastAPI backend (entrypoint backend.app:app) handles /api, /ingest, etc.
"""

import pathlib
import shutil

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "frontend" / "index.html"
DST = ROOT / "public" / "index.html"


def main() -> int:
    if not SRC.exists():
        print(f"ERROR: {SRC} not found", flush=True)
        return 1
    DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SRC, DST)
    print(f"published {DST.relative_to(ROOT)} ({DST.stat().st_size} bytes)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
