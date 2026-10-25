"""Записать несколько секунд с живого микрофона и разобрать сигнал.

    .venv\\Scripts\\python.exe tools\\mic_probe.py              # 12 секунд
    .venv\\Scripts\\python.exe tools\\mic_probe.py 20 fifine     # дольше, свой вход

Пишет %APPDATA%\\Govorok\\mic_probe.wav (16 кГц mono) и печатает: какой host API
отдал живой сигнал, RMS/пик в дБ, сколько блоков silero считает речью. Это первый
шаг, когда субтитров нет вообще: по цифрам видно, виноват ли уровень записи.
"""

from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from govorok.config import AppConfig, CONFIG_PATH, DATA_DIR  # noqa: E402
from govorok.stt import (BLOCK_SIZE, SAMPLE_RATE, device_label, input_candidates,  # noqa: E402
                        make_speech_provider, sanitize_block)


def db(value: float) -> float:
    return -100.0 if value <= 0 else 20 * float(np.log10(max(value, 1e-12)))


def main() -> int:
    seconds, wanted = 12.0, ""
    for arg in sys.argv[1:]:
        try:
            seconds = float(arg)
        except ValueError:
            wanted = arg

    cfg = AppConfig.load() if CONFIG_PATH.exists() else AppConfig()
    if wanted:
        cfg.stt.input_device = wanted
    print(f"вход: {cfg.stt.input_device or '(системный)'} · VAD {cfg.stt.vad}/"
          f"{cfg.stt.vad_engine} · усиление {cfg.stt.clamped_gain():.1f}×")
    print("говори всё это время в микрофон\n")

    import sounddevice as sd

    blocks: list[np.ndarray] = []

    def callback(indata, _frames, _t, _status) -> None:
        # копия обязательна: indata — вид на буфер PortAudio
        block = np.array(indata[:, 0], dtype=np.float32, copy=True)
        if sanitize_block(block):                    # без усиления: меряем акустику
            blocks.append(block)

    audio = np.zeros(0, dtype=np.float32)
    used = None
    for index in input_candidates(cfg.stt.input_device):
        try:
            stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                    blocksize=BLOCK_SIZE, device=index, callback=callback)
            stream.start()
        except Exception as exc:  # noqa: BLE001
            print(f"  {device_label(index)}: не открылся ({str(exc)[:60]})")
            continue
        blocks.clear()
        time.sleep(seconds)
        stream.stop()
        stream.close()
        peak = max((float(np.max(np.abs(b))) for b in blocks), default=0.0)
        garbage = 0
        if peak > 1.5:
            garbage = sum(1 for b in blocks if float(np.max(np.abs(b))) > 1.5)
        if blocks and peak <= 1.5:
            print(f"  ✔ {device_label(index)}: пик {db(peak):.0f} дБ, блоков {len(blocks)}")
            audio = np.concatenate(blocks)
            used = index
            break
        print(f"  ✖ {device_label(index)}: блоков {len(blocks)}, пик {db(peak):.1f} дБ "
              f"(мусорных {garbage}) — не годится")

    if audio.size == 0:
        print("\nживого сигнала нет ни в одном API: микрофон замьючен, выключен, "
              "или Windows запрещает приложениям доступ к микрофону")
        return 1

    path = DATA_DIR / "mic_probe.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((audio * 32767).astype("<i2").tobytes())

    rms = float(np.sqrt(np.mean(np.square(audio.astype(np.float64)))))
    print(f"\n{device_label(used)}: {audio.size / SAMPLE_RATE:.1f} c → {path}")
    print(f"RMS {db(rms):.1f} дБ · пик {db(float(np.max(np.abs(audio)))):.1f} дБ")

    provider = make_speech_provider(cfg.stt.vad_engine, log=lambda text: print("  " + text))
    threshold = max(0.0018, 0.0002 * 3.2)
    loud = speech = 0
    probabilities: list[float] = []
    for start in range(0, audio.size - BLOCK_SIZE + 1, BLOCK_SIZE):
        block = audio[start:start + BLOCK_SIZE]
        level = float(np.sqrt(np.mean(np.square(block.astype(np.float64)))))
        if provider is not None:
            probability = float(provider(block))
            probabilities.append(probability)
            if level > threshold:
                loud += 1
                if probability > 0.5:
                    speech += 1
        elif level > threshold:
            loud += 1
            speech += 1
    total = max(1, audio.size // BLOCK_SIZE)
    print(f"громких блоков {loud}/{total} ({100 * loud / total:.0f} %), "
          f"из них silero считает речью {speech} ({100 * speech / total:.0f} %)")
    if probabilities:
        ordered = sorted(probabilities)
        print(f"silero: медиана {ordered[len(ordered) // 2]:.2f} · "
              f"90-й процентиль {ordered[int(0.9 * (len(ordered) - 1))]:.2f} · "
              f"максимум {ordered[-1]:.2f}  (речь — это > 0.5)")

    print("\nвывод:")
    if db(float(np.max(np.abs(audio)))) < -50:
        print("  · сигнал очень тихий: подними «Усиление входа» на вкладке "
              "«Субтитры» до 4–8× или усиление микрофона в Windows")
    elif provider is not None and max(probabilities) < 0.5:
        print("  · громко, но silero не видит речи: говорит не этот вход "
              "(эхо/музыка) или речь тонет в шуме")
    elif speech < total * 0.05:
        print("  · за это время почти не было речи громче порога")
    else:
        print("  · сигнал выглядит как речь: смотри дальше по цепочке — "
              "tools\\live_check.py и статус в окне бота")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
