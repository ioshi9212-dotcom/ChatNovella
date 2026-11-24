"""Entrypoint for ChatNovella V2 on the existing Amvera project.

It safeguards the persistent V2 storage directory and never uses Lite
session data. No API token is required.
"""
from __future__ import annotations

import os
from pathlib import Path

# V2 sessions must survive service rebuilds, in their own persistent Data.
os.environ.setdefault("CHATNOVELLA_V2_DATA_DIR", "/data/chatnovella-v2")
directory = Path(os.environ["CHATNOVELLA_V2_DATA_DIR"]).resolve()
unsafe = (Path("/data"), Path("/data/chatnovella-lite"),
          Path("/data/chatnovella"), Path("/app"))
if not directory.is_absolute() or any(
    directory == item or item in directory.parents for item in unsafe[1:]):
    raise RuntimeError("V2 requires its own absolute persistent data directory")
if directory == unsafe[0]:
    raise RuntimeError("Do not use the shared /data root directly for V2 sessions")
if not str(directory).startswith("/data/"):
    raise RuntimeError("For Amvera V2, data must live beneath the persistent /data mount")

from .api import app  # noqa: E402 (check storage isolation before initializing engine)
