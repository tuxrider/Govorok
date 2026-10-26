"""Проверка готового portable-архива перед публикацией как релиза."""
import hashlib
import io
import json
import os
import sys
import zipfile

ZIP = r"D:\Project\release\Govorok-0.1.0-win-x64.zip"

with zipfile.ZipFile(ZIP) as z:
    names = z.namelist()
    bad = z.testzip()
    infos = z.infolist()
    total = sum(i.file_size for i in infos)

print(f"файл: {os.path.getsize(ZIP)/1048576:.0f} МБ на диске, {len(names)} записей, "
      f"{total/1048576:.0f} МБ в распакованном виде")
print("целостность (CRC):", "OK" if bad is None else f"ПОВРЕЖДЁН: {bad}")
print("разделители путей:", "backslash!" if any("\\" in n for n in names) else "slash (OK)")
top = sorted({n.split("/")[1] for n in names if len(n.split("/")) > 1})
print("корень пакета:", ", ".join(top))

junk = [n for n in names if any(x in n for x in
        (".selftest-tmp", "selftest-report", "__pycache__", "/t.cmd", "emotes_cache.json"))]
print("мусор/секреты внутри:", junk[:5] or "нет")

# версии внутри архива: читаем wheel-метаданные из dist-info
want = ("flet", "faster_whisper", "ctranslate2", "numpy", "websockets", "govorok")
for meta in sorted(n for n in names if n.endswith("METADATA")):
    base = meta.split("/")[-2]
    name = base.rsplit("-", 1)[0].lower()
    if name not in want:
        continue
    with zipfile.ZipFile(ZIP) as z:
        head = z.read(meta).split(b"\n")[0:8]
    ver = next((l.decode("utf-8", "replace")[8:] for l in head if l.startswith(b"Version:")), "?")
    print(f"  {name:16s} {ver}")

# есть ли в Scripts шимы с зашитым абсолютным путём (они нерабочи после переезда)
scripts = [n for n in names if "/Scripts/" in n and n.endswith(".exe")]
if scripts:
    print("EXE-шимы в Scripts:", len(scripts), "шт —", ", ".join(os.path.basename(s) for s in scripts[:6]))
with zipfile.ZipFile(ZIP) as z:
    exe = next((n for n in names if n.endswith("Scripts/flet.exe") or n.endswith("Scripts\\flet.exe")), None)
    if exe:
        blob = z.read(exe)[:400]
        i = blob.find(b"D:\\")
        print("  путь, зашитый в flet.exe:", blob[i:i + 60].decode("latin-1") if i >= 0 else "не найден")

# ключевые файлы приложения
for need in ("main.py", "selftest.py", "Govorok.cmd", "python.exe", "python312._pth",
             "README.txt", "LICENSE-Govorok.txt", "govorok/config.py",
             "assets/subs/index.html", "assets/dock/index.html", "requirements.txt",
             "config.example.json", "Models"):
    hit = any(n.endswith("/" + need) or n.endswith(need + "/") for n in names)
    print(("  есть   " if hit else "  НЕТ!   ") + need)

sha = hashlib.sha256()
with open(ZIP, "rb") as f:
    for chunk in iter(lambda: f.read(1 << 22), b""):
        sha.update(chunk)
print("sha256:", sha.hexdigest()[:32] + "…")
print("\n=== реальный тест: распаковка в другое место и запуск selftest ===")
