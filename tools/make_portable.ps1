# Собирает portable-пакет Govorok со встроенным Python («распакуй и запусти»).
#
#   powershell -ExecutionPolicy Bypass -File tools\make_portable.ps1
#   ... -Zip                    # ещё и собрать .zip для GitHub Release
#   ... -SkipTest               # не гонять selftest (быстрая пересборка)
#   ... -SkipInstall            # пересобрать без переустановки зависимостей
#   ... -Out D:\Releases        # куда класть пакет (по умолчанию — рядом с репозиторием)
#   ... -Clean                  # выбросить существующий пакет и собрать заново
#
# Шаги:
#   1. скачиваются python-embeddable и get-pip (кэш в ..\pycache — повтор без сети)
#   2. каркас: embedded python поднимается в корень пакета, правкой python312._pth
#      включаются Lib\site-packages и корень пакета (там main.py и govorok/)
#   3. бутстрап pip, установка requirements (--no-cache-dir, чтобы не раздувать архив)
#   4. копирование приложения: main.py, selftest.py, govorok/, assets/, tools/, LICENSE
#   5. selftest ВСТРОЕННЫМ python'ом — проверка, что пакет живой
#   6. уборка (кеши, временные файлы теста, дубли лицензий) и README.txt получателю
#   7. по -Zip — архив через bsdtar: Compress-Archive пишет пути с обратным слэшем
#
# Веса faster-whisper в пакет не кладутся (сотни МБ): приложение скачивает их само
# в %APPDATA%\Govorok\models при первом включении субтитров.

[CmdletBinding()]
param(
    [string]$Repo = "",
    [string]$Out = "",
    [string]$Cache = "",
    [string]$PythonVersion = "3.12.10",
    [switch]$Zip,
    [switch]$SkipTest,
    [switch]$SkipInstall,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# --- где что лежит -----------------------------------------------------------
$root = if ($Repo) { $Repo } else { Split-Path -Parent $PSScriptRoot }
if (-not (Test-Path (Join-Path $root "main.py"))) { throw "в $root нет main.py - укажите -Repo" }

# версию читаем разбивкой строки: регулярки с кавычками в PowerShell 5.1 ломаются,
# а здесь достаточно split по знаку равенства
$line = Select-String -Path (Join-Path $root "govorok\__init__.py") -Pattern "__version__" |
        Select-Object -First 1 -ExpandProperty Line
$version = if ($line) { ($line -split "=", 2)[1].Trim([char]39, [char]34, [char]32) } else { "" }
if (-not $version) { throw "не удалось прочитать __version__ из govorok\__init__.py" }

$name = "Govorok-$version-win-x64"
$outRoot = if ($Out) { $Out } else { Split-Path -Parent $root }
$pkg = Join-Path $outRoot $name
$cacheDir = if ($Cache) { $Cache } else { Join-Path $outRoot "pycache" }
$libDir = Join-Path $pkg "Lib"
$py = Join-Path $pkg "python.exe"

Write-Host "сборка $name (python $PythonVersion)" -ForegroundColor Cyan
Write-Host "  источник: $root"
Write-Host "  пакет:    $pkg"
Write-Host "  кэш:      $cacheDir"

New-Item -ItemType Directory -Force -Path $outRoot | Out-Null
New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null
if ($Clean -and (Test-Path $pkg)) { Remove-Item -Recurse -Force $pkg }

# --- 1. загрузка --------------------------------------------------------------
$embedZip = Join-Path $cacheDir "python-$PythonVersion-embed-amd64.zip"
$getPip = Join-Path $cacheDir "get-pip.py"
if (-not (Test-Path $embedZip)) {
    Write-Host "  скачиваю embedded python…"
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" `
        -OutFile $embedZip
}
if (-not (Test-Path $getPip)) {
    Write-Host "  скачиваю get-pip…"
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip
}

# --- 2. каркас окружения ------------------------------------------------------
if (-not (Test-Path $py)) {
    New-Item -ItemType Directory -Force -Path $libDir | Out-Null
    Write-Host "  распаковываю python…"
    Expand-Archive -LiteralPath $embedZip -DestinationPath $libDir -Force
    Get-ChildItem $libDir -File | Move-Item -Destination $pkg -Force
}
# ._pth рядом с python.exe задаёт sys.path: zip-stdlib, Lib, site-packages, DLLs и
# корень пакета. «.» в конце — чтобы пакеты из site-packages не перекрывались кодом.
Remove-Item (Join-Path $libDir "python312._pth") -Force -ErrorAction SilentlyContinue
Set-Content -Path (Join-Path $pkg "python312._pth") -Encoding ascii -Value @(
    "python312.zip", "Lib", "Lib/site-packages", "DLLs", ".", "import site"
)
New-Item -ItemType Directory -Force -Path (Join-Path $libDir "site-packages") | Out-Null

# --- 3. pip и зависимости -----------------------------------------------------
if (-not (Test-Path (Join-Path $libDir "site-packages\pip"))) {
    Write-Host "  бутстраплю pip…"
    & $py $getPip --no-warn-script-location -q
    if ($LASTEXITCODE) { throw "get-pip завершился с $LASTEXITCODE" }
}
Copy-Item (Join-Path $root "requirements.txt") $pkg -Force
if ($SkipInstall) {
    if (-not (Test-Path (Join-Path $libDir "site-packages\websockets"))) {
        throw "-SkipInstall задан, а зависимостей в пакете нет - уберите флаг для первой сборки"
    }
    Write-Host "  установку пропускаю (-SkipInstall)" -ForegroundColor Yellow
} else {
    Write-Host "  ставлю requirements (самый долгий шаг)…"
    & $py -m pip install --no-cache-dir --retries 8 --timeout 180 -r (Join-Path $pkg "requirements.txt")
    if ($LASTEXITCODE) { throw "pip install завершился с $LASTEXITCODE" }
}

# --- 4. файлы приложения ------------------------------------------------------
Write-Host "  копирую приложение…"
foreach ($file in @("main.py", "selftest.py", "requirements.txt", "config.example.json", "LICENSE", "Govorok.cmd")) {
    $src = Join-Path $root $file
    if (Test-Path $src) { Copy-Item $src $pkg -Force }
}
foreach ($dirName in @("govorok", "assets", "tools")) {
    $srcDir = Join-Path $pkg $dirName
    if (Test-Path $srcDir) { Remove-Item -Recurse -Force $srcDir }
    Copy-Item (Join-Path $root $dirName) $srcDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path (Join-Path $pkg "Models") | Out-Null

# --- 5. самопроверка ВСТРОЕННЫМ python (до уборки: тест сам создаёт .selftest-tmp) ---
if (-not $SkipTest) {
    Write-Host "  прогоняю selftest внутри пакета…" -ForegroundColor Cyan
    Push-Location $pkg
    try {
        & $py selftest.py
        $code = $LASTEXITCODE
    } finally { Pop-Location }
    if ($code) {
        Get-ChildItem $pkg -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
        throw "selftest в пакете упал с $code - пакет неполноценный"
    }
}

# --- 6. уборка и записка получателю ------------------------------------------
Write-Host "  убираю временное…"
Get-ChildItem $pkg -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
foreach ($junk in @(".selftest-tmp", "t.cmd", "selftest-report.txt", "*.cmd~")) {
    Get-ChildItem -LiteralPath $pkg -Filter $junk -Force -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}
# exe-шимы из Scripts (pip.exe, flet.exe, ct2-*.exe …) содержат абсолютный путь до
# папки СБОРКИ: после переезда они молча работают не оттуда. Всё нужное доступно
# через python.exe -m … и Govorok.cmd, поэтому чистим
if (Test-Path (Join-Path $pkg "Scripts")) {
    Remove-Item (Join-Path $pkg "Scripts\*.exe") -Force -ErrorAction SilentlyContinue
}
# LICENSE.txt у embedded python свой, лицензию приложения переименовываем, чтобы в
# архиве не выглядело как два одинаковых файла
if (Test-Path (Join-Path $pkg "LICENSE")) {
    Move-Item (Join-Path $pkg "LICENSE") (Join-Path $pkg "LICENSE-Govorok.txt") -Force
}

@"
Govorok $version - перевод чата Twitch и субтитры из микрофона для OBS
=======================================================================

Ничего устанавливать не нужно: Python уже внутри папки.

  1. Распакуйте АРХИВ ЦЕЛИКОМ в обычную папку (например C:\Govorok).
     Не запускайте из архива «на лету» и выбирайте путь без прав администратора.
  2. Дважды щёлкните Govorok.cmd - откроется окно настроек: канал, ключ перевода,
     включение дока и субтитров.

  OBS: Источники -> + -> «Браузер» -> ссылка из окна настроек
  (для субтитров там отдельная ссылка «оверлей»).

Команды (Govorok.cmd <команда>):
  dock                 без окна: только бот и док (для автозапуска)
  test                 самопроверка, сеть и ключи не нужны
  check                диагностика микрофона и субтитров
  models               скачать веса для субтитров заранее
  pip <args>           pip внутри этого пакета

Настройки, ключи и веса моделей лежат в %APPDATA%\Govorok - папку с программой
можно удалять и заменять новой, настройки останутся.

Субтитры считаются локально. Если CUDA 12 в системе нет, модель работает на CPU:
для хорошей задержки берите small (или medium на быстром железе). Ваши слова никуда
не отправляются, кроме перевода второй строки, если он включён.
"@ | Set-Content -Path (Join-Path $pkg "README.txt") -Encoding UTF8

$sizeMB = (Get-ChildItem $pkg -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("готово: {0}  ({1:N0} МБ)" -f $pkg, $sizeMB) -ForegroundColor Green

# --- 7. архив -----------------------------------------------------------------
if ($Zip) {
    $zipPath = Join-Path $outRoot "$name.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    $bsdtar = Join-Path $env:SystemRoot "System32\tar.exe"
    Write-Host "  упаковываю в $zipPath…"
    if (Test-Path $bsdtar) {
        & $bsdtar -a -cf $zipPath -C $outRoot $name
    } else {
        Compress-Archive -Path $pkg -DestinationPath $zipPath -CompressionLevel Optimal
    }
    if ($LASTEXITCODE) { throw "упаковка завершилась с $LASTEXITCODE" }
    Write-Host ("архив: {0:N0} МБ" -f ((Get-Item $zipPath).Length / 1MB)) -ForegroundColor Green
} else {
    Write-Host "  архив не собран (добавьте -Zip)" -ForegroundColor Yellow
}
