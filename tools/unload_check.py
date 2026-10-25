"""Проверка: при выключенных субтитрах модель не грузится, а unload освобождает память.

    .venv\\Scripts\\python.exe tools\\unload_check.py

Поднимает настоящий BotApp (без окна) два раза: с выключенными и с включёнными
субтитрами, и печатает, загружены ли весы модели. Затем выгружает их и проверяет,
что движок умеет загрузиться заново.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from govorok.config import AppConfig, CONFIG_PATH  # noqa: E402


async def case(enabled: bool, model: str) -> dict:
    from selftest import FakePage
    import main as m

    cfg = AppConfig.load() if CONFIG_PATH.exists() else AppConfig()
    cfg.stt.model = model
    cfg.stt.enabled = enabled
    app = m.BotApp(FakePage())
    app.config = cfg
    app.translator.configure(cfg)
    app.config.twitch.channels = []
    app.channel_field.value = ""
    app.config.dock.enabled = False
    app.config.viewers.enabled = False
    app.config.stt.enabled = enabled
    loaded_before = app.stt_service.recognizer.loaded
    if enabled:
        await app.start_stt()
    await asyncio.sleep(0.5)
    result = {
        "enabled": enabled,
        "loaded_before": loaded_before,
        "loaded_after": app.stt_service.recognizer.loaded,
        "vad_loaded": app.stt_service.segmenter.speech is not None,
    }
    await app.shutdown()
    return result


async def unload_case(model: str) -> dict:
    from selftest import FakePage
    import main as m
    from govorok.stt import SAMPLE_RATE, Utterance

    cfg = AppConfig.load() if CONFIG_PATH.exists() else AppConfig()
    cfg.stt.model = model
    app = m.BotApp(FakePage())
    app.config = cfg
    app.translator.configure(cfg)
    app.config.twitch.channels = []
    app.config.dock.enabled = False
    app.config.viewers.enabled = False
    app.config.stt.enabled = True
    await app.start_stt()
    await asyncio.sleep(0.5)
    started = app.stt_service.recognizer.loaded
    message = await app.stt_service.unload(to_cpu=True)
    await asyncio.sleep(0.3)
    unloaded = app.stt_service.recognizer.loaded
    await app.start_stt()                       # после выгрузки движок должен воскреснуть
    await asyncio.sleep(0.5)
    again = app.stt_service.recognizer.loaded
    await app.shutdown()
    return {"started": started, "message": message, "unloaded": unloaded, "again": again}


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else "small"
    print(f"модель: {model}\n")
    t0 = time.time()
    off = asyncio.run(case(False, model))
    on = asyncio.run(case(True, model))
    unload = asyncio.run(unload_case(model))
    print(f"{'субтитры':12s} {'модель до':>10s} {'модель после':>13s} {'silero':>8s}")
    for case_result in (off, on):
        flag = "выключены" if not case_result["enabled"] else "включены"
        print(f"{flag:12s} {str(case_result['loaded_before']):>10s} "
              f"{str(case_result['loaded_after']):>13s} {str(case_result['vad_loaded']):>8s}")
    print()
    print("unload: " + unload["message"])
    print(f"загружена до unload: {unload['started']} · после: {unload['unloaded']} · "
          f"после повторного старта: {unload['again']}")
    ok = (not off["loaded_after"] and on["loaded_after"]
          and unload["started"] and not unload["unloaded"] and unload["again"])
    print(f"\n{'OK' if ok else 'FAIL'} · {time.time() - t0:.0f} с")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
