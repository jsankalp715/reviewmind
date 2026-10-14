"""Backwards-compatible entry point: ``uvicorn main:app`` still works.

The application now lives in the ``app`` package; prefer ``uvicorn app.main:app``.
"""

from app.main import create_app

app = create_app()
