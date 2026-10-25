"""Субтитры со своего микрофона: захват звука → VAD → faster-whisper → оверлей.

Устройство модуля:

* :class:`Segmenter` — энергетический VAD: режет поток на реплики, не даёт
  отправить в распознавание тишину и хранит предбуфер, чтобы не срезать первое
  слово;
* :class:`Recognizer` — ленивая обёртка над faster-whisper: сама выбирает
  устройство (CUDA, если получится, иначе CPU), кэширует модель на диске;
* :class:`SubtitleService` — мост между ними: поток из микрофона пишется в
  очередь, рабочий поток распознаёт реплики и промежуточные гипотезы, а наружу
  уходят готовые пакеты субтитров через ``publish`` (уже потокобезопасно).

Всё, что нужно знать остальному приложению: :meth:`SubtitleService.start`,
:meth:`SubtitleService.stop` и :meth:`SubtitleService.stats`.
"""

from __future__ import annotations

import asyncio
import gc
import os
import queue
import re
import sys
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .config import DATA_DIR, STTConfig
from .translator import Translator

#: частота дискретизации, которую ждёт Whisper
SAMPLE_RATE = 16000
#: размер блока аудио: 100 мс — достаточно мелко для VAD и дёшево по CPU
BLOCK_MS = 100
BLOCK_SIZE = SAMPLE_RATE * BLOCK_MS // 1000
#: дольше этого одну реплику не копим — режем принудительно
MAX_UTTERANCE_SECONDS = 12.0
#: минимум звука, чтобы реплика вообще считалась речью
MIN_UTTERANCE_SECONDS = 0.35
#: окно, за которое помним пиковый уровень (для авто-подстройки порога)
PEAK_WINDOW_SECONDS = 10.0
#: порог входа «это речь», порог удержания и «пробный» порог ниже (гистерезис)
SPEECH_ON = 0.5
SPEECH_OFF = 0.25
SPEECH_FLOOR = 0.15
#: предбуфер, чтобы первое слово не терялось
PREROLL_BLOCKS = 3
#: как часто имеет смысл показывать промежуточную (серую) гипотезу
PARTIAL_MIN_SECONDS = 1.0
PARTIAL_EVERY_SECONDS = 1.3
#: на быстром устройстве (GPU) гипотезу можно обновлять заметно чаще
PARTIAL_EVERY_FAST = 0.6
PARTIAL_FAST_LATENCY = 0.35
#: сколько блоков держать в очереди (1 блок = 100 мс); больше — отстаём, дропаем
MAX_QUEUE_BLOCKS = 100
#: размер окна silero VAD в отсчётах (32 мс при 16 кГц)
SILERO_WINDOW = 512

#: явные «подписи» Whisper к не-речи — выкидываем текст целиком
HALLUCINATION_WORDS = {
    "музыка", "спокойная музыка", "игровая музыка", "фоновая музыка", "ритмичная музыка",
    "громкая музыка", "веселая музыка", "тревожная музыка", "грустная музыка", " музыка",
    "звучит музыка", "играет музыка", "игграет музыка",
    "неприятные звуки", "посторонние звуки", "громкий звук", "щелчок", "клик", "стук",
    "звук клавиатуры", "стук клавиш", "шелест", "журчание", "бульканье", "бип",
    "кашель", "смех", "аплодисменты", "бормотание", "мычание",
    "не разбираемо", "неразборчиво", "неизвестный говорящий", "non-speech", "music",
    "background noise", "static", "unintelligible", "silence", "[music]", "♪",
}

#: типичные галлюцинации Whisper на тишине, музыке и шуме — в субтитры они не нужны
HALLUCINATIONS = (
    "субтитры создавал",
    "субтитры делал",
    "субтитры делала",
    "спасибо за внимание",
    "спасибо за просмотр",
    "подписывайтесь",
    "ставьте лайки",
    "редактор субтитров",
    "корректор субтитров",
    "продолжение следует",
    "amara.org",
    "dimatorzok",
    "transcribed",
    "captioned",
    "subtitles by",
    "music",              # «music» внутри строки тоже признак домысла
    "non-speech",
    "speaking slowly",
    "speaking fast",
    "unintelligible",
)


@dataclass(slots=True)
class Utterance:
    """Готовая к распознаванию реплика."""

    audio: np.ndarray
    duration: float
    started: float

    @property
    def id(self) -> str:
        return f"sub-{int(self.started * 1000)}"


class SpeechProbability:
    """Вероятность речи по окнам silero VAD (модель идёт в комплекте faster-whisper).

    Энергетический порог не различает речь и музыку/клики мыши — а Whisper на
    не-речи уверенно выдаёт «спокойная музыка». Silero отвечает на вопрос «речь ли
    это вообще» и стоит две десятых миллисекунды на окно.
    """

    def __init__(self) -> None:
        from faster_whisper.vad import get_vad_model  # noqa: PLC0415 - тяжёлый импорт
        self.model = get_vad_model()
        self.window = SILERO_WINDOW
        self.leftover = np.zeros(0, dtype=np.float32)

    def __call__(self, block: np.ndarray) -> float:
        """Средняя вероятность речи по блоку; блоки короче окна копятся до следующего."""
        audio = np.concatenate((self.leftover, np.asarray(block, dtype=np.float32).ravel()))
        usable = (audio.size // self.window) * self.window
        if usable <= 0:
            self.leftover = audio.astype(np.float32)
            return 0.0
        self.leftover = audio[usable:].astype(np.float32)
        probabilities = self.model(audio[:usable].astype(np.float32), num_samples=self.window)
        return float(np.max(probabilities)) if len(probabilities) else 0.0


def make_speech_provider(engine: str, log: Callable[[str], None] | None = None):
    """Собрать детектор речи по настройке; silero не поднялся — молча остаёмся на RMS."""
    if (engine or "silero") != "silero":
        return None
    try:
        provider = SpeechProbability()
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"субтитры: silero VAD недоступен ({str(exc)[:80]}) — работаю по громкости")
        return None
    if log:
        log("субтитры: VAD silero поднят — музыка и клики в распознавание не пойдут")
    return provider


class Segmenter:
    """VAD: из потока блоков делает реплики.

    Дешёвый RMS-фильтр отсекает тишину, а когда подключён ``speech`` (silero),
    решает ещё и «речь ли это» — иначе фоновая музыка и стрекот клавиатуры
    уходят в Whisper и возвращаются как «спокойная музыка», «татататата».
    """

    def __init__(self, config_provider: Callable[[], STTConfig], *,
                 speech: Callable[[np.ndarray], float] | None = None) -> None:
        self._config = config_provider
        self.speech = speech                    # блок (100 мс) → вероятность речи
        self.speech_prob = 0.0                  # последняя вероятность (для статуса)
        self.noise = 0.004                      # оценка шумового пола (RMS)
        self.level = 0.0                        # текущая громкость (для индикатора)
        self.peak = 0.0                         # максимум RMS за последние секунды
        self._peak_since = 0.0
        self.rejected_speech = 0                # сколько «громко, но не речь» отброшено
        self._speaking = False
        self._buffer: list[np.ndarray] = []
        self._silence_blocks = 0
        self._preroll: deque[tuple[np.ndarray, float]] = deque(maxlen=PREROLL_BLOCKS)
        self._started = 0.0
        self._speech_blocks = 0
        self._loud_blocks = 0
        self.last_utterance: Utterance | None = None

    # ------------------------------------------------------------------ VAD

    def threshold(self) -> float:
        """Порог «это речь».

        Базовый вариант — от шумового пола. Но если микрофон тихий (пик речи
        всё равно ниже абсолютного порога), подстраиваемся под реальный пик:
        иначе можно говорить в микрофон и не получить ни одной реплики.
        """
        # абсолютный порог — физическая громкость, он не зависит от digital-усиления:
        # усиление множит и речь, и шум, поэтому спасает тихий вход не порог, а
        # запас по шуму (ниже) и усиление сигнала для самого Whisper
        floor = max(0.0006, self.noise * 2.6)
        absolute = max(0.0018, self.noise * 3.2)
        if self.peak > self.noise * 5:          # в окне была речь заметно громче шума
            adaptive = self.peak * 0.4
            if adaptive < absolute:
                return max(floor, adaptive)
        return absolute

    @property
    def speaking(self) -> bool:
        return self._speaking

    def reset(self) -> None:
        self._speaking = False
        self._buffer.clear()
        self._silence_blocks = 0
        self._preroll.clear()
        self._speech_blocks = 0
        self._loud_blocks = 0
        self.speech_prob = 0.0
        self.peak = 0.0
        self._peak_since = 0.0

    def _probability(self, block: np.ndarray) -> float | None:
        """Вероятность речи для блока (None — детектор не подключён)."""
        if self.speech is None:
            return None
        try:
            self.speech_prob = float(self.speech(block))
        except Exception:  # noqa: BLE001 - VAD не должен ронять поток
            self.speech_prob = 0.0
        return self.speech_prob

    def _preroll_of(self, probability: float | None) -> list[tuple[np.ndarray, float]]:
        """Блоки предбуфера (с их вероятностью речи), которые стоит сохранить.

        Тишина в фразу не входит: без этого доля речи размывалась бы предбуфером
        и тихая настоящая речь отбрасывалась как «не речь».
        """
        if not self._preroll:
            return []
        if probability is None:
            return list(self._preroll)
        return [pair for pair in list(self._preroll)[-2:] if pair[1] > SPEECH_OFF]

    def feed(self, block: np.ndarray) -> Utterance | None:
        """Скормить один блок (100 мс). Вернуть реплику, если она закрылась."""
        cfg = self._config()
        rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float32)))) if block.size else 0.0
        self.level = rms
        now = time.time()
        if rms > self.peak:
            self.peak = rms
        if not self._peak_since:
            self._peak_since = now
        elif now - self._peak_since > PEAK_WINDOW_SECONDS:
            self.peak = rms
            self._peak_since = now

        if not cfg.vad:
            return self._feed_fixed(block, cfg)

        probability = self._probability(block)
        loud = rms > self.threshold()
        if probability is not None:
            if self._speaking:
                # удержание фразы: порог ниже порога входа, иначе тихие концы слов рвут речь
                loud = rms > self.threshold() and probability > SPEECH_OFF
            else:
                # вход: чуть ниже SPEECH_ON разрешаем «посмотреть» — решающее слово
                # скажет flush; так тихая речь не теряется, а музыка отбрасывается
                loud = rms > self.threshold() and probability > SPEECH_FLOOR

        if not self._speaking:
            self._preroll.append((block, float(probability or 0.0)))
            if not loud:
                # шумовой пол тянем только на тишине, иначе он «съест» речь
                self.noise = 0.97 * self.noise + 0.03 * rms
                return None
            self._speaking = True
            self._silence_blocks = 0
            pairs = self._preroll_of(probability)
            preroll = [block for block, _prob in pairs]
            self._started = time.time() - len(preroll) * BLOCK_MS / 1000
            self._buffer = preroll + [block]
            self._preroll.clear()
            self._speech_blocks = sum(1 for _block, prob in pairs
                                      if prob > SPEECH_ON) + (1 if (probability or 0) > SPEECH_ON else 0)
            self._loud_blocks = len(preroll) + 1
            return None

        self._buffer.append(block)
        if (probability or 1.0) > SPEECH_ON:
            self._speech_blocks += 1
        held = loud if probability is None else (
            rms > self.threshold() and probability > SPEECH_OFF)
        if held:
            self._loud_blocks += 1
            self._silence_blocks = 0
        else:
            self._silence_blocks += 1

        silence_limit = max(1, int(cfg.silence_ms / BLOCK_MS))
        buffered = len(self._buffer) * BLOCK_MS / 1000
        if self._silence_blocks >= silence_limit or buffered >= MAX_UTTERANCE_SECONDS:
            return self._flush()
        return None

    def partial(self) -> Utterance | None:
        """Промежуточная реплика для серой гипотезы (буфер не очищается)."""
        if not self._speaking or not self._buffer:
            return None
        if not self._speech_enough():
            return None
        audio = np.concatenate(self._buffer)
        if audio.size / SAMPLE_RATE < PARTIAL_MIN_SECONDS:
            return None
        return Utterance(audio=audio, duration=audio.size / SAMPLE_RATE, started=self._started)

    def _speech_enough(self) -> bool:
        """Доля «речевых» блоков в громкой части фразы: меньше порога — это не речь."""
        if self.speech is None:
            return True
        loud_blocks = max(1, self._loud_blocks)
        return self._speech_blocks / loud_blocks >= self._config().min_speech_ratio

    def _feed_fixed(self, block: np.ndarray, cfg: STTConfig) -> Utterance | None:
        """Режим без VAD: режем поток ровно по ``chunk_seconds``.

        Нужен, когда автоопределение речи плохо работает (шум, музыка): тогда
        лучше раз в пару секунд отправлять всё подряд.
        """
        self._speaking = True
        if not self._buffer:
            self._started = time.time()
        self._buffer.append(block)
        limit = max(1, int(round(cfg.clamped_chunk() * 1000 / BLOCK_MS)))
        if len(self._buffer) >= limit:
            return self._flush(trim=False)
        return None

    def _flush(self, *, trim: bool = True) -> Utterance | None:
        # хвостовую тишину не распознаём: она только раздувает время и провоцирует галлюцинации
        blocks = self._buffer
        if trim and self._silence_blocks and blocks:
            keep = max(1, len(blocks) - self._silence_blocks)
            blocks = blocks[:keep]
        audio = np.concatenate(blocks) if blocks else np.zeros(0, dtype=np.float32)
        duration = audio.size / SAMPLE_RATE
        ratio = self._speech_blocks / max(1, self._loud_blocks)
        self._speaking = False
        self._buffer = []
        self._silence_blocks = 0
        self._speech_blocks = 0
        self._loud_blocks = 0
        min_amplitude = max(0.0015, self.threshold() * 0.5)
        if duration < MIN_UTTERANCE_SECONDS or float(np.max(np.abs(audio))) < min_amplitude:
            return None
        if self.speech is not None and ratio < self._config().min_speech_ratio:
            # громко, но это музыка или клавиатура — в Whisper не отправляем
            self.rejected_speech += 1
            return None
        utterance = Utterance(audio=audio, duration=duration, started=self._started)
        self.last_utterance = utterance
        return utterance


def _cuda_library_names() -> tuple[str, ...]:
    """Имена библиотеки cuBLAS для текущей платформы (CUDA 12)."""
    if sys.platform == "win32":
        return ("cublas64_12.dll",)
    if sys.platform == "darwin":
        return ("libcublas.12.dylib", "libcublas.dylib")
    return ("libcublas.so.12", "libcublas.so")


def _dll_dirs() -> list[Path]:
    """Где искать CUDA-библиотеки: тулкит, PATH, CUDA_PATH и пакеты nvidia-* из pip."""
    dirs: list[Path] = []
    # тулкит: ctranslate2 4.x ждёт CUDA 12, а CUDA_PATH может указывать на v13
    toolkit = (Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
               / "NVIDIA GPU Computing Toolkit" / "CUDA")
    if toolkit.exists():
        dirs += sorted(toolkit.glob("v12*/bin"))
    for key in ("CUDA_PATH", "CUDA_HOME", "CUDNN_PATH"):
        value = os.environ.get(key)
        if value:
            dirs += [Path(value) / "bin", Path(value) / "bin" / "x64", Path(value)]
    for item in (os.environ.get("PATH") or os.environ.get("LD_LIBRARY_PATH") or "").split(os.pathsep):
        if item:
            dirs.append(Path(item))
    if sys.platform != "win32":
        # linux: /usr/local/cuda-12.x/{lib64,lib} и дебиановый путь multiarch
        dirs += sorted(Path("/usr/local").glob("cuda*/lib64"))
        dirs += sorted(Path("/usr/local").glob("cuda*/lib"))
        dirs += [Path("/usr/lib/x86_64-linux-gnu"), Path("/usr/lib64")]
    try:  # pip-пакеты nvidia-cublas-cu12 / nvidia-cudnn-cu12 держат библиотеки здесь
        import site
        roots = list(site.getsitepackages())
        user_site = site.getusersitepackages()
        if user_site:
            roots.append(user_site)
        for root in roots:
            dirs += list((Path(root) / "nvidia").glob("*/lib"))
            dirs += list((Path(root) / "nvidia").glob("*/bin"))
    except Exception:  # noqa: BLE001
        pass
    return dirs


def find_cuda_library() -> Path | None:
    """Первая найденная библиотека cuBLAS 12 (None — CUDA недоступна)."""
    for folder in _dll_dirs():
        for name in _cuda_library_names():
            try:
                candidate = folder / name
                if candidate.is_file():
                    return candidate
            except OSError:                   # битые символьные ссылки, недоступные пути
                continue
    return None


#: хендлы os.add_dll_directory: их нельзя терять, иначе папка перестаёт искаться
_DLL_HANDLES: list = []


@lru_cache(maxsize=1)
def cuda_ready() -> tuple[bool, str]:
    """Есть ли CUDA 12 для ctranslate2 (нужна библиотека cuBLAS 12).

    Проверяем файл заранее: без него ctranslate2 либо падает с невнятной ошибкой,
    либо (на больших моделях) долго висит на инициализации драйвера. Ответ должен
    совпадать с prepare_cuda() на любой платформе — иначе тесты расходятся с CI.
    """
    if os.environ.get("GOVOROK_FORCE_CUDA") == "1" or os.environ.get("BOTTTV_FORCE_CUDA") == "1":
        return True, ""
    if find_cuda_library() is None:
        return False, f"не найдена CUDA 12 ({_cuda_library_names()[0]})"
    return True, ""


def prepare_cuda() -> bool:
    """Прописать папки с CUDA 12 в PATH перед загрузкой модели.

    На Windows ctranslate2 тянет cuBLAS через LoadLibrary, а тот ищет DLL по PATH,
    поэтому просто «увидеть» файл на диске недостаточно — папку надо добавить.
    На Linux поиск идёт через ldconfig/LD_LIBRARY_PATH, поэтому достаточно того,
    что библиотеку нашли.
    """
    library = find_cuda_library()
    if library is None:
        return False
    if sys.platform != "win32":
        return True
    markers = ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll", "cudnn_ops64_9.dll")
    for folder in _dll_dirs():
        try:
            if not folder.is_dir():
                continue
        except OSError:
            continue
        if not any((folder / name).exists() for name in markers):
            continue
        path_text = str(folder)
        current = os.environ.get("PATH") or ""
        if path_text not in current.split(os.pathsep):
            os.environ["PATH"] = path_text + os.pathsep + current
        try:
            _DLL_HANDLES.append(os.add_dll_directory(path_text))
        except (OSError, AttributeError):
            pass
    return True


def clean_text(raw: str) -> str:
    """Привести результат распознавания к виду, пригодному для субтитра."""
    text = " ".join(str(raw or "").split())
    if not text:
        return ""
    lowered = text.casefold().strip(" .,!?…—-\"'«»")
    if not lowered:
        return ""
    if lowered in HALLUCINATION_WORDS:       # одиночная подпись «музыка» и т.п.
        return ""
    for bad in HALLUCINATIONS:
        if bad in lowered:
            return ""
    # одиночные знаки и «шумовые» ответы модели
    if not any(char.isalpha() for char in text):
        return ""
    if repeated_text(lowered):
        return ""
    return text


def repeated_text(text: str, share: float = 0.4, times: int = 3) -> bool:
    """Похоже ли на «татататата»: один и тот же слог повторяется без конца.

    Смотрим на самый частый биграмм: у нормального текста он не набирает и трети,
    а у «та-та-та» и «аааа» — больше половины. На коротких фразах («мама мыла раму»)
    признак молчит.
    """
    text = re.sub(r"\s+", "", text or "")
    if len(text) < 6:
        return False
    bigrams = Counter(text[i:i + 2] for i in range(len(text) - 1))
    top, count = bigrams.most_common(1)[0]
    if count < times:
        return False
    # «аааа»: один и тот же знак во всех биграммах
    uniform = len(set(top)) == 1
    return count / max(1, len(text) - 1) > (0.2 if uniform else share)


class Recognizer:
    """Ленивый faster-whisper: сам выбирает устройство и держит модель в памяти."""

    def __init__(self, config_provider: Callable[[], STTConfig], *,
                 log: Callable[[str], None] | None = None,
                 models_dir: Path | None = None) -> None:
        self._config = config_provider
        self._log = log or (lambda _text: None)
        self.models_dir = Path(models_dir or (DATA_DIR / "models"))
        self._model: Any = None
        self._signature: tuple = ()
        self.device = ""
        self.compute_type = ""
        self.model_name = ""
        self.threads = 0
        self.last_latency = 0.0
        self.rejected_quality = 0        # сегментов, отбракованных как «не речь»
        self.last_no_speech_prob = 0.0   # последняя оценка «тут точно не речь»

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _signature_of(self, cfg: STTConfig) -> tuple:
        return (cfg.model, cfg.device, cfg.compute_type)

    def _candidates(self, cfg: STTConfig) -> list[tuple[str, str]]:
        """Кого пробовать в каком порядке: уважаем настройку, но не падаем."""
        auto_compute = "float16" if cfg.device in {"auto", "cuda"} else "int8"
        preferred = cfg.compute_type if cfg.compute_type != "auto" else auto_compute
        ready, _reason = cuda_ready()
        if cfg.device == "cpu":
            return [("cpu", "int8" if preferred not in {"int8", "float32"} else preferred)]
        if not ready:
            return [("cpu", "int8")]
        if cfg.device == "cuda":
            return [("cuda", preferred), ("cpu", "int8")]
        return [("cuda", "float16"), ("cpu", "int8")]

    def ensure(self) -> None:
        """Загрузить модель, если она ещё не загружена или сменились настройки."""
        cfg = self._config()
        signature = self._signature_of(cfg)
        if self._model is not None and signature == self._signature:
            return
        self._model = None
        try:
            from faster_whisper import WhisperModel  # noqa: PLC0415 - тяжёлый импорт
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"faster-whisper не установлен: {exc}") from exc

        # потоков CPU — половина ядер (на 16 потоках это 8): small на 8 потоках
        # быстрее в 1.3 раза, чем на 4, а больше 8 уже почти не даёт выигрыша
        threads = max(2, min(8, (os.cpu_count() or 4) // 2))
        ready, reason = cuda_ready()
        if cfg.device in {"auto", "cuda"}:
            if ready:
                prepare_cuda()
            else:
                self._log(f"субтитры: CUDA недоступна ({reason}) — считаю на CPU; "
                          f"нужна CUDA 12 (cuBLAS), cuDNN не обязателен")
        local = self.models_dir / cfg.model
        source = str(local) if local.exists() else cfg.model
        last_error = ""
        for device, compute in self._candidates(cfg):
            started = time.time()
            try:
                model = WhisperModel(source, device=device, compute_type=compute,
                                     cpu_threads=threads, download_root=str(self.models_dir))
                # именно прочитать сегменты: transcribe ленивый, ошибки CUDA вылетают
                # только при первом обращении к генератору
                warmup, _ = model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32),
                                             language="ru", beam_size=1, vad_filter=False)
                list(warmup)
            except Exception as exc:  # noqa: BLE001
                last_error = f"{device}/{compute}: {exc}"
                self._log(f"субтитры: {device}/{compute} не подошёл ({str(exc)[:90]})")
                continue
            self._model = model
            self.device, self.compute_type = device, compute
            self.model_name = cfg.model
            self.threads = threads if device == "cpu" else 0
            self._signature = signature
            seconds = time.time() - started
            extra = f" · {threads} потоков CPU" if device == "cpu" else ""
            self._log(f"субтитры: модель {cfg.model} на {device}/{compute}{extra} "
                      f"загружена за {seconds:.1f} с")
            return
        raise RuntimeError(f"не удалось загрузить модель: {last_error}")

    def unload(self, *, to_cpu: bool = True) -> str:
        """Отпустить весы модели; `to_cpu=True` — сдвинуть в RAM, а не выкинуть.

        Возвращает, что получилось. Нужно, чтобы выключенные субтитры не держали
        гигабайты видеопамяти: ctranslate2 умеет выгружать веса, оставив контекст
        устройства, — следующее включение поднимает модель быстрее, чем с нуля.
        """
        if self._model is None:
            return "модель и не была загружена"
        inner = getattr(self._model, "model", None)
        unloader = getattr(inner, "unload_model", None)
        model, self._model = self._model, None
        self._signature = ()
        if callable(unloader):
            try:
                unloader(to_cpu=to_cpu)         # весотдача на стороне ctranslate2
            except Exception as exc:  # noqa: BLE001
                self._log(f"субтитры: выгрузить модель не вышло ({str(exc)[:80]})")
        del model, inner, unloader
        gc.collect()
        return ("веса сдвинуты в RAM: видеопамять свободна, включение будет быстрым"
                if to_cpu else
                "веса отпущены: видеопамять свободна, при включении загрузка с нуля")

    def transcribe(self, audio: np.ndarray, *, beam_size: int = 1) -> str:
        """Распознать кусок аудио 16 кГц mono. Возвращает очищенный текст или "".

        Отбраковка по метаданным декодера: ``no_speech_prob`` — уверенность модели
        «здесь нет речи», ``avg_logprob`` — насколько уверенно вообще подобран текст.
        Именно так ловятся «спокойная музыка» и «татататата».
        """
        if self._model is None:
            self.ensure()
        cfg = self._config()
        started = time.time()
        segments, _info = self._model.transcribe(
            audio,
            language=None if cfg.language == "auto" else cfg.language,
            beam_size=max(1, int(beam_size)),
            vad_filter=False,                  # реплики уже отобрал VAD
            condition_on_previous_text=False,  # иначе модель «залипает» на прошлой фразе
            without_timestamps=True,
        )
        kept, dropped = [], 0
        for segment in segments:
            if segment.no_speech_prob > cfg.max_no_speech_prob:
                dropped += 1
                continue
            if segment.avg_logprob < cfg.min_avg_logprob:
                dropped += 1
                continue
            kept.append(segment.text)
        self.rejected_quality += dropped
        self.last_no_speech_prob = max((segment.no_speech_prob for segment in segments),
                                       default=0.0)
        text = clean_text(" ".join(kept))
        self.last_latency = time.time() - started
        return text


def sanitize_block(block: np.ndarray, gain: float = 1.0) -> bool:
    """Очистить блок от мусора PortAudio и применить усиление входа.

    На некоторых устройствах часть кадров приходит незаполненной: вместо долей
    -1…+1 там мусор вида 9e33 (на этой машине — fifine). Один такой образец
    поднимает RMS на порядки: VAD «открывает» фразу на пустоте, а Whisper на
    таком входе молчит или додумывает текст.

    ``gain`` — усиление в разах (1…8): спасает тихие микрофоны, когда системные
    ползунки трогать не хочется. Клиппинг после усиления не отбраковывается: это
    осознанная перегрузка, а не мусор драйвера.
    """
    if block.size == 0:
        return False
    if not bool(np.all(np.isfinite(block))):
        np.nan_to_num(block, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    # мусор ловим до усиления: значения вне [-1; 1] в float-режиме PortAudio
    # бывают только когда буфер не заполнен
    if float(np.max(np.abs(block))) > 1.5:
        return False
    gain = max(0.1, min(64.0, float(gain or 1.0)))
    if gain != 1.0:
        block *= np.float32(gain)
    np.clip(block, -1.0, 1.0, out=block)
    return True



def _hostapi_rank(index: int, device: dict) -> int:
    """Приоритет host API для захвата.

    MME первым: он единственный стабильно отдаёт чистый сигнал в произвольной
    частоте (WASAPI у многих драйверов требует родную частоту и падает с
    «Invalid sample rate», DirectSound на этой машине примешивает мусор).
    """
    order = (("mme", 0), ("wasapi", 1), ("directsound", 2), ("wdm-ks", 3))
    try:
        import sounddevice as sd  # noqa: PLC0415
        name = str(sd.query_hostapis(int(device.get("hostapi") or 0)).get("name", "")).lower()
    except Exception:  # noqa: BLE001
        name = ""
    for marker, rank in order:
        if marker in name:
            return rank
    return 4


def device_key(name: str) -> str:
    """Ключ железа для дедупликации: один микрофон PortAudio отдаёт в 4 API.

    В MME/DirectSound/WASAPI имя английское («Microphone (fifine SC3)»), в WDM-KS
    локализованное («Микрофон (fifine SC3)»), поэтому сравниваем хвост в скобках,
    а не имя целиком.
    """
    match = re.search(r"\(([^()]*)\)\s*$", name or "")
    base = match.group(1) if match else (name or "")
    return re.sub(r"\s+", " ", base).strip().casefold()


def input_candidates(wanted: str) -> list[int | None]:
    """Все индексы одного микрофона по приоритету API, в конце — «по умолчанию».

    Один и тот же микрофон виден в нескольких host API, и не каждый принимает
    16 кГц (WASAPI и WDM-KS часто требуют родную частоту), — поэтому микрофон
    открывается по очереди, пока какой-то вариант не заработает.
    """
    try:
        import sounddevice as sd  # noqa: PLC0415 - необязательная зависимость
        devices = list(sd.query_devices())
    except Exception:  # noqa: BLE001
        return [None]
    name = (wanted or "").strip()
    if not name:
        return [None]
    lowered = name.casefold()
    key = device_key(name)
    matches: list[tuple[int, dict]] = []
    for index, device in enumerate(devices):
        if int(device.get("max_input_channels") or 0) <= 0:
            continue
        candidate = str(device.get("name") or "")
        if lowered in candidate.casefold() or (key and device_key(candidate) == key):
            matches.append((index, device))
    if not matches:
        return [None]
    matches.sort(key=lambda item: _hostapi_rank(item[0], item[1]))
    return [index for index, _device in matches] + [None]


def device_label(index: int | None) -> str:
    """Имя устройства и его host API — для лога и настроек."""
    try:
        import sounddevice as sd  # noqa: PLC0415
        device = sd.query_devices(index, "input")
        api = str(sd.query_hostapis(int(device.get("hostapi") or 0)).get("name", ""))
        name = str(device.get("name") or "микрофон")
        return f"{name} · {api.replace('Windows ', '')}" if api else name
    except Exception:  # noqa: BLE001
        return "микрофон по умолчанию"


def resolve_input_device(wanted: str) -> int | None:
    """Первый (самый предпочтительный) индекс микрофона. ``None`` = по умолчанию."""
    return input_candidates(wanted)[0]


class SubtitleService:
    """Микрофон → VAD → распознавание → пакеты субтитров.

    ``publish`` вызывается из рабочего потока, поэтому внутри сервис сам
    переносит вызов в цикл событий через ``loop.call_soon_threadsafe``.
    """

    def __init__(self, config_provider: Callable[[], STTConfig], *,
                 log: Callable[[str], None] | None = None,
                 models_dir: Path | None = None,
                 translator: Any | None = None,
                 speech: Callable[[np.ndarray], float] | None = None) -> None:
        self._config = config_provider
        self._log = log or (lambda _text: None)
        self._translator = translator
        self._speech = speech
        self.segmenter = Segmenter(config_provider, speech=speech)
        self.recognizer = Recognizer(config_provider, log=self._log, models_dir=models_dir)
        self._publish: Callable[[dict], None] = lambda _payload: None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._audio_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=MAX_QUEUE_BLOCKS)
        self._stop = threading.Event()
        self._capture: Any = None
        self._capture_thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._last_partial_at = 0.0
        self._last_partial_text = ""
        self._last_final_text = ""
        self.started_at = 0.0
        self.error = ""
        self.input_name = ""
        self.input_index: int | None = None
        self.garbage_blocks = 0
        self.translate_requests = 0
        self.translate_done = 0
        self.last_translation = ""
        self._translate_future: object | None = None
        self._subtitle_translator_obj = None
        self._subtitle_signature: object | None = None
        self._gain = 1.0
        self.blocks = 0
        self.utterances = 0
        self.partials = 0
        self.subtitles = 0
        self.last_text = ""

    # ------------------------------------------------------------- свойства

    @property
    def running(self) -> bool:
        return self._worker is not None and self._worker.is_alive() and not self._stop.is_set()

    @property
    def device_label(self) -> str:
        return f"{self.recognizer.model_name or '—'} · {self.recognizer.device or '—'}/" \
               f"{self.recognizer.compute_type or '—'}"

    def stats(self) -> dict:
        return {
            "running": self.running,
            "device": self.device_label,
            "input": self.input_name,
            "level": round(self.segmenter.level, 5),
            "peak": round(self.segmenter.peak, 5),
            "threshold": round(self.segmenter.threshold(), 5),
            "speech_prob": round(self.segmenter.speech_prob, 3),
            "speech_engine": ("silero" if self.segmenter.speech is not None else "rms"),
            "speaking": self.segmenter.speaking,
            "blocks": self.blocks,
            "garbage_blocks": self.garbage_blocks,
            "utterances": self.utterances,
            "partials": self.partials,
            "subtitles": self.subtitles,
            "translate_requests": self.translate_requests,
            "translate_done": self.translate_done,
            "last_translation": self.last_translation,
            "rejected_speech": self.segmenter.rejected_speech,
            "rejected_quality": self.recognizer.rejected_quality,
            "latency_ms": int(self.recognizer.last_latency * 1000),
            "last_text": self.last_text,
            "error": self.error,
        }

    # ---------------------------------------------------------------- запуск

    async def start(self, publish: Callable[[dict], None]) -> tuple[bool, str]:
        """Поднять микрофон и рабочий поток. Возвращает (получилось, причина)."""
        if self.running:
            return True, ""
        self._loop = asyncio.get_running_loop()
        self._publish = lambda payload: self._emit(payload, publish)
        self.error = ""
        self.blocks = self.utterances = self.partials = self.subtitles = 0
        self.translate_requests = self.translate_done = 0
        self.last_translation = ""
        self.garbage_blocks = 0
        self.last_text = ""
        self._last_partial_text = ""
        self._last_final_text = ""
        try:
            import sounddevice as sd  # noqa: PLC0415 - необязательная зависимость
        except Exception as exc:  # noqa: BLE001
            self.error = f"sounddevice не установлен: {exc}"
            return False, self.error

        try:
            self.recognizer.ensure()
        except Exception as exc:  # noqa: BLE001
            self.error = f"модель не загрузилась: {exc}"
            return False, self.error

        cfg = self._config()
        self._gain = cfg.clamped_gain()
        # детектор речи поднимаем здесь: silero тяжёлый, а без него музыка уходит в
        # распознавание и возвращается «спокойной музыкой»
        if self.segmenter.speech is None and cfg.vad:
            provider = self._speech if self._speech is not None else make_speech_provider(
                cfg.vad_engine, log=self._log)
            if provider is not None:
                self._speech = provider
                self.segmenter.speech = provider
        self.segmenter.reset()
        while True:                                  # старые блоки не нужны
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break
        self._stop.clear()

        # микрофон виден в нескольких host API — пробуем по очереди, пока
        # какой-то не согласится работать на 16 кГц и не отдаст живой звук
        last_error = ""
        candidates = input_candidates(cfg.input_device)
        for position, index in enumerate(candidates):
            try:
                stream = sd.InputStream(
                    samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                    blocksize=BLOCK_SIZE, device=index, callback=self._on_audio,
                )
                stream.start()
            except Exception as exc:  # noqa: BLE001
                last_error = f"{device_label(index)}: {exc}"
                continue
            # бывает, что API открывается, но отдаёт нули или мусор (DirectSound на
            # этой машине умеет и то, и другое): без этой проверки бот «слушал» бы
            # мёртвый поток и молчал
            await asyncio.sleep(0.4)
            peak = self._queue_peak()
            if peak <= 1e-7 or peak > 1.5:
                last_error = (f"{device_label(index)}: "
                              + ("не отдаёт звук" if peak <= 1e-7 else f"мусор в потоке ({peak:.1e})"))
                self._log(f"субтитры: {last_error} — пробую другой API")
                try:
                    stream.stop()
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
                continue
            self._capture = stream
            self.input_index = index
            if position and cfg.input_device:
                self._log(f"субтитры: {device_label(candidates[0])} не подошёл — "
                          f"беру {device_label(index)} (последняя ошибка: {last_error[:80]})")
            break
        if self._capture is None:
            self.error = f"микрофон не открылся: {last_error or 'нет подходящих устройств'}"
            return False, self.error

        self._worker = threading.Thread(target=self._work, name="stt-worker", daemon=True)
        self._worker.start()
        self.started_at = time.time()
        self.input_name = device_label(self.input_index)
        self._log(f"субтитры слушают: {self.input_name} · {self.device_label}")
        return True, ""

    async def unload(self, *, to_cpu: bool = True) -> str:
        """Отпустить весы модели и переводчик субтитров (память, которой уже не пользуемся)."""
        await self.stop()
        message = self.recognizer.unload(to_cpu=to_cpu)
        translator = self._subtitle_translator_obj
        self._subtitle_translator_obj, self._subtitle_signature = None, None
        if translator is not None and self._loop is not None and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(translator.aclose(), self._loop)
            except Exception:  # noqa: BLE001
                pass
        self._log(f"субтитры: {message}")
        return message

    async def stop(self) -> None:
        self._stop.set()
        if self._subtitle_translator_obj is not None:
            translator, self._subtitle_translator_obj = self._subtitle_translator_obj, None
            self._subtitle_signature = None
            try:
                if self._loop is not None and self._loop.is_running():
                    asyncio.run_coroutine_threadsafe(translator.aclose(), self._loop)
            except Exception:  # noqa: BLE001
                pass
        if self._capture is not None:
            try:
                self._capture.stop()
                self._capture.close()
            except Exception:  # noqa: BLE001
                pass
            self._capture = None
        for thread in (self._worker, self._capture_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
        self._worker = self._capture_thread = None
        self.input_name = ""
        self.input_index = None
        self.segmenter.reset()
        if self.started_at:
            self._log("субтитры остановлены")

    # ------------------------------------------------------------- внутреннее

    def _queue_peak(self) -> float:
        """Пик «правильного» звука в накопленных блоках (блоки остаются в очереди)."""
        peak = 0.0
        drained: list[np.ndarray] = []
        while True:
            try:
                block = self._audio_queue.get_nowait()
            except queue.Empty:
                break
            drained.append(block)
            if block.size and bool(np.all(np.isfinite(block))) and float(np.max(np.abs(block))) <= 1.5:
                peak = max(peak, float(np.max(np.abs(block))))
        for block in drained:
            try:
                self._audio_queue.put_nowait(block)
            except queue.Full:
                break
        return peak

    def _on_audio(self, indata, _frames, _time, status) -> None:  # noqa: ANN001 - PortAudio
        # copy обязателен: indata — вид на живой буфер PortAudio, и без копии
        # блок мог «поехать» вместе с памятью стрима (мусорные значения 9e33)
        block = np.array(indata[:, 0], dtype=np.float32, copy=True)
        if not sanitize_block(block, self._gain):
            # PortAudio отдал незаполненный буфер: блок выбрасываем, иначе RMS
            # взлетает, VAD открывается на пустоте, а Whisper молчит или додумывает
            self.garbage_blocks += 1
            return
        try:
            self._audio_queue.put_nowait(block)
        except queue.Full:                           # отстали — лучше потерять блок
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._audio_queue.put_nowait(block)
            except queue.Full:
                pass

    def _emit(self, payload: dict, publish: Callable[[dict], None]) -> None:
        """Отправить пакет в цикл событий: publish не потокобезопасен."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(publish, payload)
        except RuntimeError:
            pass

    def _work(self) -> None:
        partial_seconds = PARTIAL_EVERY_SECONDS
        while not self._stop.is_set():
            try:
                block = self._audio_queue.get(timeout=0.15)
            except queue.Empty:
                self._maybe_partial(partial_seconds)
                continue
            self.blocks += 1
            try:
                utterance = self.segmenter.feed(block)
            except Exception as exc:  # noqa: BLE001
                self._log(f"субтитры: сбой VAD ({exc!r})")
                continue
            if utterance is not None:
                self._log(f"субтитры: реплика {utterance.duration:.1f} c")
                self._finish(utterance)
            else:
                self._maybe_partial(partial_seconds)

    def _maybe_partial(self, interval: float) -> None:
        # распознавание укладывается в сотни миллисекунд (GPU) — можно частить;
        # на медленном CPU частые гипотезы только отодвигают финальный текст
        fast = 0.0 < self.recognizer.last_latency < PARTIAL_FAST_LATENCY
        interval = min(interval, PARTIAL_EVERY_FAST) if fast else interval
        if time.time() - self._last_partial_at < interval:
            return
        utterance = self.segmenter.partial()
        if utterance is None:
            return
        self._last_partial_at = time.time()
        try:
            text = self.recognizer.transcribe(utterance.audio)
        except Exception as exc:  # noqa: BLE001
            self._log(f"субтитры: промежуточное распознавание сорвалось ({exc!r})")
            return
        if not text or text == self._last_partial_text:
            return
        self._last_partial_text = text
        self.partials += 1
        self._publish({"type": "subtitle", "id": utterance.id, "text": text,
                       "final": False, "ts": time.time()})

    def _finish(self, utterance: Utterance) -> None:
        self.utterances += 1
        self._log(f"субтитры: реплика {utterance.duration:.1f} c")
        try:
            text = self.recognizer.transcribe(utterance.audio,
                                              beam_size=self._config().beam_size)
        except Exception as exc:  # noqa: BLE001
            self._log(f"субтитры: распознавание сорвалось ({exc!r})")
            return
        if not text:
            return
        if text == self._last_final_text and utterance.duration < 2.0:
            return                                   # дубль после промежуточной гипотезы
        self._last_final_text = text
        self.last_text = text
        self.subtitles += 1
        self._log(f"субтитры: {text[:100]}")
        mode = self._config().clamped_mode()
        # в режиме «только перевод» оригинал зрителю не нужен: показываем строку,
        # когда придёт перевод (или сразу, если перевод недоступен)
        if mode != "en":
            self._publish({"type": "subtitle", "id": utterance.id, "text": text,
                           "original": text, "final": True, "ts": time.time()})
        self._translate(utterance.id, text, mode)

    def _translate(self, subtitle_id: str, text: str, mode: str | None = None) -> None:
        """Перевод субтитра: «ru» — не нужен, «both» — вторая строка, «en» — вместо оригинала.

        Здесь текст — наша же речь, поэтому эвристики чата (пропуск кириллицы,
        "!команд", коротких строк) не подходят: они бы всегда отбраковывали русскую
        речь. Просим перевод принудительно и НЕ блокируем worker-поток ожиданием
        ответа — иначе перевод одной фразы задержал бы все последующие субтитры.
        """
        cfg = self._config()
        mode = mode or cfg.clamped_mode()
        if mode == "ru":
            return
        target = (cfg.translate_to or "").strip()
        if not target:
            # режим требует перевода, а язык цели не выбран — берём язык перевода чата
            base = getattr(self._translator, "config", None)
            target = (base.translation.target_lang or "").strip() if base else ""
            if not target:
                if mode == "en":
                    self._publish({"type": "subtitle", "id": subtitle_id, "text": text,
                                   "original": text, "final": True, "ts": time.time()})
                    self._log("субтитры: режим «только перевод», но язык цели пуст — "
                              "показываю оригинал")
                return
        if mode == "en" and self._translator is None:
            self._publish({"type": "subtitle", "id": subtitle_id, "text": text,
                           "original": text, "final": True, "ts": time.time()})
            return
        self.translate_requests += 1
        if self._translator is None or self._loop is None:
            self._log("субтитры: перевод не настроен (нет переводчика или цикла)")
            if mode == "en":
                self._publish({"type": "subtitle", "id": subtitle_id, "text": text,
                               "original": text, "final": True, "ts": time.time()})
            return
        if target[:2].casefold() == (cfg.language or "ru")[:2].casefold():
            self._log(f"субтитры: язык речи и перевод на «{target}» совпадают — "
                      f"показываю оригинал")
            if mode == "en":
                self._publish({"type": "subtitle", "id": subtitle_id, "text": text,
                               "original": text, "final": True, "ts": time.time()})
            return
        task = asyncio.run_coroutine_threadsafe(
            self._translate_task(subtitle_id, text, target, mode), self._loop)
        self._translate_future = task

    def _subtitle_translator(self, target: str):
        """Переводчик субтитров: свой экземпляр со своим target_lang.

        Чатный переводчик трогать нельзя — он параллельно переводит чат на свой
        язык, и общий конфиг устроил бы гонку.
        """
        base = self._translator
        if base is None:
            return None
        signature = (target, getattr(base, "config", None))
        if self._subtitle_translator_obj is not None and self._subtitle_signature == signature:
            return self._subtitle_translator_obj
        try:
            from dataclasses import replace
            from govorok.config import AppConfig as _App, TranslationConfig
            sub_config = replace(base.config,
                                 translation=replace(base.config.translation, target_lang=target))
            self._subtitle_translator_obj = Translator(sub_config, on_log=self._log)
        except Exception as exc:  # noqa: BLE001
            self._log(f"субтитры: свой переводчик не собрался ({exc!r}) — беру общий")
            self._subtitle_translator_obj, self._subtitle_signature = base, None
            return base
        self._subtitle_signature = signature
        return self._subtitle_translator_obj

    async def _translate_task(self, subtitle_id: str, text: str, target: str,
                              mode: str = "both") -> None:
        translator = self._subtitle_translator(target)
        if translator is None:
            if mode == "en":
                self._publish({"type": "subtitle", "id": subtitle_id, "text": text,
                               "original": text, "final": True, "ts": time.time()})
            return
        try:
            result = await translator.translate(text, force=True)
        except Exception as exc:  # noqa: BLE001
            self._log(f"субтитры: перевод не вышел ({type(exc).__name__}: {exc})")
            result = None
        if result is not None and getattr(result, "error", None):
            self._log(f"субтитры: перевод не вышел ({result.error})")
        translated = clean_text(getattr(result, "text", "") or "") if result else ""
        if not translated or translated == text:
            if mode == "en":                       # без перевода зритель увидит оригинал
                self._publish({"type": "subtitle", "id": subtitle_id, "text": text,
                               "original": text, "final": True, "ts": time.time()})
            return
        self.translate_done += 1
        self.last_translation = translated
        if mode == "en":
            # replace=True: строка с тем же id показывается уже переводом
            self._publish({"type": "subtitle", "id": subtitle_id, "text": translated,
                           "replace": True, "original": text, "lang": target,
                           "final": True, "ts": time.time()})
        else:
            self._publish({"type": "subtitle_translation", "id": subtitle_id,
                           "text": translated, "lang": target, "ts": time.time()})
