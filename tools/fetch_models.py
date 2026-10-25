"""Разово скачать модели faster-whisper в папку данных Govorok."""

import sys
import time
import pathlib

from huggingface_hub import snapshot_download

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from govorok.config import DATA_DIR  # noqa: E402

TARGETS = [("small", "Systran/faster-whisper-small"),
           ("medium", "Systran/faster-whisper-medium")]

root = DATA_DIR / "models"
root.mkdir(parents=True, exist_ok=True)
for name, repo in TARGETS:
    dest = root / name
    before = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) if dest.exists() else 0
    started = time.time()
    try:
        path = snapshot_download(repo, local_dir=str(dest), max_workers=4, etag_timeout=60)
        size = sum(f.stat().st_size for f in pathlib.Path(path).rglob("*") if f.is_file())
        print(f"OK {name}: {size / 1e6:.0f} МБ (было {before / 1e6:.0f}) "
              f"за {time.time() - started:.0f} c -> {path}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL {name}: {type(exc).__name__} {str(exc)[:300]}", flush=True)
