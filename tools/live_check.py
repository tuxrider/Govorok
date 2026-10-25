"""Энд-ту-энд проверка субтитров: без OBS, через тот же движок, что и окно.

    .venv\\Scripts\\python.exe tools\\live_check.py                 # записать 16 с и разобрать
    .venv\\Scripts\\python.exe tools\\live_check.py --gain 6         # с усилением входа 6×
    .venv\\Scripts\\python.exe tools\\live_check.py --speak          # проиграть речь из колонок
    .venv\\Scripts\\python.exe tools\\live_check.py --tail           # слушать запущенного бота
    .venv\\Scripts\\python.exe tools\\live_check.py --tail http://127.0.0.1:8765/subs?token=...

Скрипт поднимает тот же SubtitleService, что и приложение, и печатает каждый шаг
цепочки: уровни, решение silero, отбраковку мусора, реплики, текст. ``--tail`` —
клиент-оверлей: подключается к уже запущенному боту и показывает ровно то, что
должен рисовать OBS.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from govorok.config import AppConfig, CONFIG_PATH, DATA_DIR  # noqa: E402
from govorok.stt import (BLOCK_SIZE, SAMPLE_RATE, Segmenter, SubtitleService,  # noqa: E402
                        device_label, input_candidates, make_speech_provider, sanitize_block)

PHRASES = [
    "Привет, чат! Сегодня играем в Доту, погнали.",
    "Смотри, какой интересный момент, сейчас покажу.",
    "Ставь лайк и подписывайся, если понравилось.",
]


def db(value: float) -> float:
    return -100.0 if value <= 0 else 20 * float(np.log10(max(value, 1e-12)))


def speak_async(phrases: list[str], delay: float = 1.5) -> None:
    """Прочитать фразы вслух через System.Speech (звук из колонок → микрофон)."""

    def run() -> None:
        script = ["Add-Type -AssemblyName System.Speech",
                  "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
                  "$ru = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture -like 'ru*' }",
                  "if ($ru) { $s.SelectVoice($ru[0].VoiceInfo.Name) }",
                  "$s.Rate = -1", "$s.SetOutputToDefaultAudioDevice()"]
        script += [f"$s.Speak('{text.replace(chr(39), chr(39) * 2)}')" for text in phrases]
        script.append("$s.Dispose()")
        time.sleep(delay)
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", "; ".join(script)],
                           check=False, timeout=120)
        except Exception as exc:  # noqa: BLE001
            print("не удалось запустить синтез речи:", exc)

    threading.Thread(target=run, daemon=True).start()


async def capture(cfg: AppConfig, seconds: float) -> np.ndarray:
    """Записать сигнал коллбэком (путь один в один с движком) по всем API подряд."""
    import sounddevice as sd

    collected: list[np.ndarray] = []

    def callback(indata, _frames, _t, _status) -> None:
        block = np.array(indata[:, 0], dtype=np.float32, copy=True)
        if sanitize_block(block):
            collected.append(block)

    for index in input_candidates(cfg.stt.input_device):
        try:
            stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                    blocksize=BLOCK_SIZE, device=index, callback=callback)
            stream.start()
        except Exception as exc:  # noqa: BLE001
            print(f"  {device_label(index)}: не открылся ({str(exc)[:60]})")
            continue
        collected.clear()
        dropped = 0
        deadline = time.time() + seconds
        while time.time() < deadline:
            await asyncio.sleep(0.1)
        stream.stop()
        stream.close()
        audio = np.concatenate(collected) if collected else np.zeros(0, dtype=np.float32)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if audio.size:
            print(f"  {device_label(index)}: {audio.size / SAMPLE_RATE:.1f} c, "
                  f"пик {db(peak):.1f} дБ")
        if audio.size and peak <= 1.5:
            print(f"  ✔ беру {device_label(index)}")
            return audio
        print(f"  ✖ {device_label(index)} не годится (мусор или пусто)")
    return np.zeros(0, dtype=np.float32)


def analyse(audio: np.ndarray, cfg: AppConfig, label: str) -> list[dict]:
    """Прогнать сигнал через VAD + распознавание и рассказать, что произошло."""
    provider = make_speech_provider(cfg.stt.vad_engine, log=lambda text: print("   " + text))
    segmenter = Segmenter(lambda: cfg.stt, speech=provider)
    service = SubtitleService(lambda: cfg.stt, log=lambda text: print("   " + text))
    service.recognizer.ensure()
    packets: list[dict] = []
    service._publish = packets.append

    gain = cfg.stt.clamped_gain()
    loud_blocks = speech_blocks = empty = 0
    for start in range(0, audio.size - BLOCK_SIZE + 1, BLOCK_SIZE):
        block = audio[start:start + BLOCK_SIZE]
        utterance = segmenter.feed(block)
        if segmenter.speaking:
            loud_blocks += 1
        if utterance is None:
            continue
        speech_blocks += 1
        boosted = utterance.audio * np.float32(gain)     # то же, что делает движок
        text = service.recognizer.transcribe(np.clip(boosted, -1.0, 1.0),
                                             beam_size=cfg.stt.beam_size)
        if text:
            service.utterances += 1
            service._finish(utterance)
        else:
            empty += 1

    stats = service.stats()
    print(f"\n[{label}] {audio.size / SAMPLE_RATE:.1f} c · VAD {cfg.stt.vad}/"
          f"{cfg.stt.vad_engine} · усиление {gain:.1f}×")
    print(f"   блоков «говорим»: {loud_blocks} · реплик дошло до распознавания: "
          f"{speech_blocks} · пустых расшифровок: {empty} · "
          f"отброшено как не-речь: {stats['rejected_speech']} · "
          f"отброшено плохого текста: {stats['rejected_quality']}")
    finals = [p for p in packets if p.get("type") == "subtitle" and p.get("final")]
    for packet in finals:
        print(f"   СУБТИТР: {packet['text']}")
    if not finals:
        print("   финальных субтитров нет")
    return packets


def tail(url: str, seconds: float = 120.0) -> int:
    """Клиент-оверлей: подключаемся к запущенному боту и печатаем всё подряд."""
    from websockets.asyncio.client import connect as ws_connect

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme in {"http", "https"}:
        scheme = "wss" if parsed.scheme == "https" else "ws"
        ws_url = urllib.parse.urlunsplit((scheme, parsed.netloc, "/ws", parsed.query, ""))
    else:
        ws_url = url
    print(f"подключаюсь как оверлей: {ws_url}")

    async def run() -> int:
        try:
            async with ws_connect(ws_url, open_timeout=5) as ws:
                hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
                print("hello:", {key: hello.get(key) for key in
                                 ("type", "channels", "can_send", "subs")})
                print("дальше идут пакеты; субтитры — type=subtitle\n")
                seen = 0
                deadline = time.time() + seconds
                while time.time() < deadline:
                    try:
                        packet = json.loads(await asyncio.wait_for(ws.recv(), 1.0))
                    except asyncio.TimeoutError:
                        continue
                    kind = packet.get("type")
                    if kind == "subtitle":
                        seen += 1
                        mark = "ФИНАЛ  " if packet.get("final") else "гипотеза"
                        print(f"  {time.strftime('%H:%M:%S')} {mark} {packet.get('text')}")
                    elif kind == "subtitle_translation":
                        print(f"  {time.strftime('%H:%M:%S')} перевод {packet.get('text')}")
                    elif kind == "pong":
                        continue
                    else:
                        print(f"  {time.strftime('%H:%M:%S')} ({kind})")
                print(f"\nпакетов субтитров получено: {seen}")
                return 0 if seen else 2
        except Exception as exc:  # noqa: BLE001
            print("не подключился:", type(exc).__name__, exc)
            return 1

    return asyncio.run(run())


def main() -> int:
    cfg = AppConfig.load() if CONFIG_PATH.exists() else AppConfig()
    args = sys.argv[1:]
    do_speak = "--speak" in args
    if "--tail" in args:
        index = args.index("--tail")
        target = args[index + 1] if index + 1 < len(args) and not args[index + 1].startswith("-") \
            else ""
        if not target:
            target = f"http://{cfg.dock.host or '127.0.0.1'}:{cfg.dock.port}/subs" + \
                (f"?token={cfg.dock.token}" if cfg.dock.token else "")
            print(f"ссылка из конфига: {target}")
        return tail(target, float(os.environ.get("GOVOROK_TAIL_SECONDS")
               or os.environ.get("BOTTTV_TAIL_SECONDS") or "120"))

    for i, arg in enumerate(args):
        if arg == "--gain" and i + 1 < len(args):
            try:
                cfg.stt.input_gain = float(args[i + 1])
            except ValueError:
                pass

    print(f"модель {cfg.stt.model} · устройство {cfg.stt.device} · "
          f"VAD {cfg.stt.vad}/{cfg.stt.vad_engine} · вход '{cfg.stt.input_device}' · "
          f"усиление {cfg.stt.clamped_gain():.1f}×")

    if do_speak:
        print("проигрываю речь из колонок — убедись, что звук вывода не выключен")
        speak_async(PHRASES)
    else:
        print("говори в микрофон всё это время")
    seconds = 16.0
    audio = asyncio.run(capture(cfg, seconds))
    if audio.size == 0:
        print("\nСИГНАЛА НЕТ ни в одном API — микрофон молчит: проверь, что он не "
              "замьючен и что Windows даёт приложениям доступ к микрофону.")
        return 1

    path = DATA_DIR / "live_check.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((audio * 32767).astype("<i2").tobytes())
    rms = float(np.sqrt(np.mean(np.square(audio.astype(np.float64)))))
    print(f"записано в {path} · RMS {db(rms):.1f} дБ · "
          f"пик {db(float(np.max(np.abs(audio)))):.1f} дБ")

    packets = analyse(audio, cfg, "записанный сигнал")
    if not packets:
        print("\nподсказка: попробуй tools\\live_check.py --gain 6 "
              "(усиление входа) или --speak (проиграть известную речь)")
    return 0 if packets else 2


if __name__ == "__main__":
    raise SystemExit(main())
