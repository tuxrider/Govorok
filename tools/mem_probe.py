"""Сколько памяти ест движок субтитров: сравнение «выключен» и «включен».

    .venv\\Scripts\\python.exe tools\\mem_probe.py
    .venv\\Scripts\\python.exe tools\\mem_probe.py medium

Запускает два изолированных процесса с настоящим BotApp: в первом субтитры
выключены (окно настроек собрано, движок не поднят), во втором — включены и модель
реально загружена. Печатает пик RAM процесса и пик видеопамяти по nvidia-smi.
Нужно, чтобы видеть: при выключенных субтитрах модель не грузится вообще.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PYTHON = sys.executable

CHILD = '''
import asyncio, json, sys, threading, time
import ctypes

cfg_path, want_engine, root = sys.argv[1], sys.argv[2] == "1", sys.argv[3]
sys.path.insert(0, root)

from pathlib import Path as P
from selftest import FakePage
import main as m
from govorok.config import AppConfig


class CTR(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32), ("pf", ctypes.c_uint32),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("a", ctypes.c_size_t), ("b", ctypes.c_size_t),
                ("c", ctypes.c_size_t), ("d", ctypes.c_size_t),
                ("e", ctypes.c_size_t), ("f", ctypes.c_size_t)]


async def go():
    app = m.BotApp(FakePage())
    app.config = AppConfig.load(P(cfg_path))
    app.translator.configure(app.config)
    app.config.twitch.channels = []
    app.channel_field.value = ""
    app.config.dock.enabled = False
    app.config.viewers.enabled = False
    app.config.stt.enabled = want_engine
    if want_engine:
        await app.start_stt()
    await asyncio.sleep(1.0)

    stop = threading.Event()
    box = {}

    def spin():
        # argtypes обязателен: без него псевдо-хендл процесса режется до int32,
        # и GetProcessMemoryInfo возвращает ошибку вместо цифр
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        fn = ctypes.windll.psapi.GetProcessMemoryInfo
        fn.restype = ctypes.c_bool
        fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(CTR), ctypes.c_uint32]
        info = CTR()
        info.cb = ctypes.sizeof(CTR)
        handle = k32.GetCurrentProcess()
        top, fails = 0, 0
        while not stop.is_set():
            if fn(handle, ctypes.byref(info), info.cb):
                top = max(top, int(info.WorkingSetSize))
            else:
                fails += 1
            time.sleep(0.2)
        box["peak_ws_mb"] = round(top / 1048576, 1)
        box["api_fails"] = fails

    thread = threading.Thread(target=spin, daemon=True)
    thread.start()
    await asyncio.sleep(3.0)
    stop.set()
    thread.join(timeout=3)
    rec = app.stt_service.recognizer
    print(json.dumps({**box, "loaded": rec.loaded, "device": rec.device,
                      "compute": rec.compute_type, "model": rec.model_name}), flush=True)
    await app.shutdown()

asyncio.run(go())
'''


def gpu_used() -> int:
    """Текущая занятая видеопамята всего GPU (по-процессной у этого драйвера нет)."""
    try:
        done = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5)
        return int((done.stdout or "0").strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return 0


def watch_gpu(stop: threading.Event, out: list[int]) -> None:
    top = 0
    while not stop.is_set():
        top = max(top, gpu_used())
        time.sleep(0.3)
    out.append(top)


def run_case(label: str, cfg_path: Path, engine: bool) -> dict:
    stop = threading.Event()
    peaks: list[int] = []
    watcher = threading.Thread(target=watch_gpu, args=(stop, peaks), daemon=True)
    watcher.start()
    try:
        proc = subprocess.run([PYTHON, "-c", CHILD, str(cfg_path), "1" if engine else "0", str(ROOT)],
                              capture_output=True, text=True, timeout=240, cwd=str(ROOT),
                              encoding="utf-8", errors="replace")
    finally:
        stop.set()
        watcher.join(timeout=4)
    lines = [l for l in (proc.stdout or "").splitlines() if l.startswith("{")]
    if not lines:
        return {"label": label, "error": (proc.stderr or "").strip()[-300:]}
    data = json.loads(lines[-1])
    data["label"] = label
    data["gpu_peak"] = peaks[0] if peaks else 0
    data["gpu_before"] = peaks[0] if peaks else 0
    return data


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else ""
    tmp = ROOT / ".selftest-tmp"
    tmp.mkdir(exist_ok=True)
    cfg_path = tmp / "mem_probe_config.json"

    from govorok.config import AppConfig, CONFIG_PATH
    cfg = AppConfig.load() if CONFIG_PATH.exists() else AppConfig()
    if model:
        cfg.stt.model = model
    cfg.stt.enabled = False
    cfg_path.write_text(json.dumps(cfg.redacted(), ensure_ascii=False, indent=2), encoding="utf-8")

    off = run_case("субтитры выключены", cfg_path, False)
    on = run_case("субтитры включены", cfg_path, True)

    print(f"модель: {cfg.stt.model} · устройство в конфиге: {cfg.stt.device}")
    print(f"{'состояние':22s} {'пик RAM':>12s} {'видео пик':>12s} {'модель':>26s}")
    for case in (off, on):
        if "error" in case:
            print(f"{case['label']:22s} {'—':>12s} {'ошибка: ' + case['error'][:60]:>22s}")
            continue
        ram = f"{case.get('peak_ws_mb', 0):.0f} МБ"
        loaded = ("да · " + f"{case.get('model')}/{case.get('device')}-{case.get('compute')}"
                  if case.get("loaded") else "нет (не загружена)")
        print(f"{case['label']:22s} {ram:>12s} {str(case.get('gpu_peak', 0)) + ' МБ':>12s} "
              f"{loaded:>26s}")
    print(f"видеопамять (весь GPU) в моментах: выкл {off.get('gpu_peak', 0)} МБ · "
          f"вкл {on.get('gpu_peak', 0)} МБ — на фоне остальных процессов")
    if isinstance(on.get("peak_ws_mb"), (int, float)) and isinstance(off.get("peak_ws_mb"), (int, float)):
        print(f"разница: {on['peak_ws_mb'] - off['peak_ws_mb']:.0f} МБ RAM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
