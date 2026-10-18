"""Container entrypoint: initialize storage, then run one persistent scheduler."""
from pathlib import Path
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.bootstrap import initialize

initialize()
os.execvp("uvicorn", [
    "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765",
    "--workers", "1", "--timeout-graceful-shutdown", "30",
])
