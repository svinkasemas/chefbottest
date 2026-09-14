<#
.SYNOPSIS
    Точечная проверка ChefBot внутри настоящего Telegram во время разработки.

.DESCRIPTION
    Автоматизирует то, что иначе пришлось бы делать руками при каждой проверке:
      1) запускает backend (uvicorn), если он ещё не запущен;
      2) поднимает туннель (localtunnel по умолчанию, либо CloudPub);
      3) вытаскивает из вывода туннеля свежий https-адрес;
      4) прописывает его как WEBAPP_URL в .env;
      5) перезапускает bot.py — при старте бот сам обновляет кнопку меню
         в Telegram через Bot API, поэтому руками лезть в @BotFather не нужно.

    Сведения о запущенных процессах хранятся в файле .dev-tunnel-state.json
    в корне проекта — чтобы повторный запуск не плодил дубликаты и чтобы
    -Stop знал, что останавливать.

.PARAMETER Tool
    Какой туннель использовать: 'localtunnel' (по умолчанию, требует Node.js —
    достаточно установить один раз: winget install OpenJS.NodeJS.LTS) или
    'cloudpub' (требует отдельно скачанный clo.exe, см. -CloudPubPath).

.PARAMETER Port
    Порт, на котором работает backend. По умолчанию 8000.

.PARAMETER CloudPubPath
    Путь к clo.exe (только при -Tool cloudpub). По умолчанию
    tools\cloudpub\clo.exe в корне проекта.

.PARAMETER Stop
    Останавливает всё, что было запущено этим скриптом (backend, туннель, бота).

.EXAMPLE
    .\scripts\dev-tunnel.ps1
    Запускает всё через localtunnel и печатает свежий https-адрес приложения.

.EXAMPLE
    .\scripts\dev-tunnel.ps1 -Tool cloudpub
    То же самое, но через CloudPub вместо localtunnel.

.EXAMPLE
    .\scripts\dev-tunnel.ps1 -Stop
    Останавливает backend, туннель и бота, запущенные предыдущим вызовом.
#>

param(
    [ValidateSet('localtunnel', 'cloudpub')]
    [string]$Tool = 'localtunnel',
    [int]$Port = 8000,
    [string]$CloudPubPath = "$PSScriptRoot\..\tools\cloudpub\clo.exe",
    [switch]$Stop
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$EnvPath     = Join-Path $ProjectRoot ".env"
$StatePath   = Join-Path $ProjectRoot ".dev-tunnel-state.json"
$PythonExe   = Join-Path $ProjectRoot "venv\Scripts\python.exe"
$TunnelLog   = Join-Path $ProjectRoot ".dev-tunnel.log"

function Write-Step($text) {
    Write-Host ""
    Write-Host "==> $text" -ForegroundColor Cyan
}

function Load-State {
    if (Test-Path $StatePath) {
        try { return Get-Content $StatePath -Raw | ConvertFrom-Json } catch { return $null }
    }
    return $null
}

function Save-State($state) {
    $state | ConvertTo-Json | Set-Content -Path $StatePath -Encoding UTF8
}

# Останавливает процесс и всех его потомков (важно для localtunnel: реальная
# команда — это cmd.exe -> npx.cmd -> node.exe, и убить нужно всю цепочку,
# иначе node.exe останется висеть в фоне после Stop-Process по одному PID).
function Stop-ProcessTree($rootId, $label) {
    if (-not $rootId) { return }
    $root = Get-Process -Id $rootId -ErrorAction SilentlyContinue
    if (-not $root) { return }

    Write-Host "  Останавливаю $label (PID $rootId) и дочерние процессы..."
    $queue = [System.Collections.Generic.Queue[int]]::new()
    $queue.Enqueue($rootId)
    $allIds = New-Object System.Collections.Generic.List[int]

    while ($queue.Count -gt 0) {
        $currentId = $queue.Dequeue()
        $allIds.Add($currentId)
        Get-CimInstance Win32_Process -Filter "ParentProcessId=$currentId" -ErrorAction SilentlyContinue |
            ForEach-Object { $queue.Enqueue($_.ProcessId) }
    }

    foreach ($id in $allIds) {
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Режим остановки
# ---------------------------------------------------------------------------
if ($Stop) {
    Write-Step "Останавливаю всё, что было запущено скриптом"

    # Гасим осиротевшие вручную запущенные экземпляры бота в любом случае —
    # даже если файла состояния нет вовсе (например, бота запускали только
    # руками, ни разу не через этот скрипт).
    $strayBotProcesses = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'bot\.bot' }
    foreach ($proc in $strayBotProcesses) {
        Write-Host "  Останавливаю процесс бота (PID $($proc.ProcessId))..."
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }

    $state = Load-State
    if (-not $state) {
        Write-Host "Файл состояния скрипта не найден — backend/туннель, запущенные" -ForegroundColor Yellow
        Write-Host "именно этим скриптом, останавливать нечего." -ForegroundColor Yellow
        exit 0
    }
    Stop-ProcessTree $state.BotPid "бота"
    Stop-ProcessTree $state.TunnelPid "туннель"
    Stop-ProcessTree $state.BackendPid "backend"
    Remove-Item $StatePath -ErrorAction SilentlyContinue
    Write-Host "Готово." -ForegroundColor Green
    exit 0
}

# ---------------------------------------------------------------------------
# Проверки перед стартом
# ---------------------------------------------------------------------------
if (-not (Test-Path $PythonExe)) {
    Write-Host "Не найден venv: $PythonExe" -ForegroundColor Red
    Write-Host "Создайте окружение и установите зависимости — см. INSTALL.md, шаг 2." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $EnvPath)) {
    Write-Host "Не найден .env в корне проекта. Скопируйте .env.example в .env и заполните его (см. INSTALL.md, шаг 3)." -ForegroundColor Red
    exit 1
}

if ($Tool -eq 'localtunnel') {
    if (-not (Get-Command npx.cmd -ErrorAction SilentlyContinue)) {
        Write-Host "Не найден npx (значит, не установлен Node.js)." -ForegroundColor Red
        Write-Host "Установите: winget install OpenJS.NodeJS.LTS, затем откройте новое окно PowerShell." -ForegroundColor Red
        exit 1
    }
} else {
    if (-not (Test-Path $CloudPubPath)) {
        Write-Host "Не найден clo.exe по пути: $CloudPubPath" -ForegroundColor Red
        Write-Host "Скачайте клиент с https://cloudpub.ru/docs и положите его туда," -ForegroundColor Red
        Write-Host "либо укажите свой путь параметром -CloudPubPath." -ForegroundColor Red
        exit 1
    }
}

$state = Load-State
if (-not $state) { $state = [PSCustomObject]@{ BackendPid = $null; TunnelPid = $null; BotPid = $null } }

# ---------------------------------------------------------------------------
# 1. Backend
# ---------------------------------------------------------------------------
$portBusy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue

if ($portBusy) {
    Write-Step "Backend уже слушает порт $Port — не трогаю"
} else {
    Write-Step "Запускаю backend (uvicorn) на порту $Port"
    $backendProc = Start-Process -FilePath $PythonExe `
        -ArgumentList "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "$Port" `
        -WorkingDirectory $ProjectRoot `
        -PassThru
    $state.BackendPid = $backendProc.Id
    Start-Sleep -Seconds 2
}

# ---------------------------------------------------------------------------
# 2. Туннель
# ---------------------------------------------------------------------------
Stop-ProcessTree $state.TunnelPid "предыдущий туннель"
Remove-Item $TunnelLog -ErrorAction SilentlyContinue
Remove-Item "$TunnelLog.err" -ErrorAction SilentlyContinue

if ($Tool -eq 'localtunnel') {
    Write-Step "Поднимаю туннель через localtunnel"
    # Если localtunnel установлен глобально (npm install -g localtunnel),
    # используем команду lt напрямую — она стартует мгновенно, без обращения
    # к реестру npm за границей. Иначе — через npx (медленнее и зависит от
    # скорости сети до registry.npmjs.org, поэтому даём больше времени на ожидание).
    $globalLt = Get-Command lt.cmd -ErrorAction SilentlyContinue
    if ($globalLt) {
        $cmdArgs = @("/c", "lt", "--port", "$Port")
    } else {
        # -y перед именем пакета — чтобы npx не ждал интерактивного
        # подтверждения "Ok to proceed?", ведь ввод для этого окна никто не вводит.
        $cmdArgs = @("/c", "npx", "-y", "localtunnel", "--port", "$Port")
        Write-Host "  Совет: 'npm install -g localtunnel' один раз — тогда туннель" -ForegroundColor DarkGray
        Write-Host "  будет подниматься мгновенно, без обращения к npm registry." -ForegroundColor DarkGray
    }
    # Запускаем через cmd.exe /c, чтобы Windows корректно нашла .cmd-обёртки по PATH
    # (Start-Process не всегда сам разрешает их по PATHEXT).
    $tunnelProc = Start-Process -FilePath "cmd.exe" `
        -ArgumentList $cmdArgs `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $TunnelLog `
        -RedirectStandardError "$TunnelLog.err" `
        -WindowStyle Hidden `
        -PassThru
    $urlPattern = "https://\S+\.loca\.lt"
} else {
    Write-Step "Поднимаю туннель через CloudPub"
    $tunnelProc = Start-Process -FilePath $CloudPubPath `
        -ArgumentList "publish", "http", "$Port" `
        -WorkingDirectory (Split-Path $CloudPubPath) `
        -RedirectStandardOutput $TunnelLog `
        -RedirectStandardError "$TunnelLog.err" `
        -WindowStyle Hidden `
        -PassThru
    $urlPattern = "https://\S+\.cloudpub\.ru"
}
$state.TunnelPid = $tunnelProc.Id

Write-Host "  Жду, пока туннель выдаст адрес (это может занять до пары минут, если сеть до npm registry медленная)..."
$publicUrl = $null
$maxWaitSeconds = 120
for ($i = 0; $i -lt $maxWaitSeconds; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Path $TunnelLog) {
        $line = Select-String -Path $TunnelLog -Pattern $urlPattern -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($line) {
            $publicUrl = $line.Matches[0].Value
            break
        }
    }
    if ($i -gt 0 -and $i % 20 -eq 0) {
        Write-Host "  ...жду ещё ($i из $maxWaitSeconds секунд)"
    }
}

if (-not $publicUrl) {
    Write-Host "Не удалось получить адрес за $maxWaitSeconds секунд." -ForegroundColor Red
    Write-Host "Проверьте лог: $TunnelLog (и $TunnelLog.err)" -ForegroundColor Red
    Write-Host "Если в логе адрес всё же есть — значит, туннель поднялся, но" -ForegroundColor Red
    Write-Host "дольше отведённого времени. Запустите скрипт ещё раз, либо" -ForegroundColor Red
    Write-Host "поставьте localtunnel глобально (см. совет выше) для надёжности." -ForegroundColor Red
    if ($Tool -eq 'cloudpub') {
        Write-Host "Частая причина — не выполнен вход: $CloudPubPath login" -ForegroundColor Red
    }
    Save-State $state
    exit 1
}
if (-not $publicUrl.EndsWith("/")) { $publicUrl += "/" }
Write-Host "  Адрес получен: $publicUrl" -ForegroundColor Green

if ($Tool -eq 'localtunnel') {
    Write-Host ""
    Write-Host "  Примечание: при первом открытии этого адреса с телефона" -ForegroundColor Yellow
    Write-Host "  localtunnel может показать межстраничную заглушку 'Friendly" -ForegroundColor Yellow
    Write-Host "  Reminder' — это нормально, просто нажмите 'Click to Continue'." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# 3. Обновляем .env
# ---------------------------------------------------------------------------
Write-Step "Обновляю WEBAPP_URL в .env"
$envLines = Get-Content $EnvPath -Encoding UTF8
$found = $false
$newLines = $envLines | ForEach-Object {
    if ($_ -match "^\s*WEBAPP_URL\s*=") {
        $found = $true
        "WEBAPP_URL=$publicUrl"
    } else {
        $_
    }
}
if (-not $found) { $newLines += "WEBAPP_URL=$publicUrl" }
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllLines($EnvPath, $newLines, $utf8NoBom)

# ---------------------------------------------------------------------------
# 4. Перезапускаем бота
# ---------------------------------------------------------------------------
Write-Step "Перезапускаю бота"
Stop-ProcessTree $state.BotPid "бот (отслеженный скриптом)"

# Telegram разрешает только один активный getUpdates-опрос на бота. Если бот
# когда-либо запускался вручную (не через этот скрипт), его PID не попадает
# в .dev-tunnel-state.json — Stop-ProcessTree выше его не увидит, и в
# результате окажется два одновременно работающих экземпляра, что Telegram
# воспримет как конфликт (TelegramConflictError: Conflict: terminated by
# other getUpdates request). Поэтому дополнительно ищем и глушим ЛЮБЫЕ
# процессы python.exe, в командной строке которых есть "bot.bot", независимо
# от того, как и когда они были запущены.
$strayBotProcesses = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'bot\.bot' }
foreach ($proc in $strayBotProcesses) {
    Write-Host "  Останавливаю ранее запущенный вручную процесс бота (PID $($proc.ProcessId))..."
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 2

$botProc = Start-Process -FilePath $PythonExe `
    -ArgumentList "-m", "bot.bot" `
    -WorkingDirectory $ProjectRoot `
    -PassThru
$state.BotPid = $botProc.Id

Save-State $state

Write-Host ""
Write-Host "Готово! Приложение доступно по адресу:" -ForegroundColor Green
Write-Host "  $publicUrl" -ForegroundColor Green
Write-Host ""
Write-Host "Бот сам обновил кнопку меню в Telegram при старте — в @BotFather" -ForegroundColor Green
Write-Host "ничего вручную менять не нужно. Откройте бота и нажмите /start." -ForegroundColor Green
Write-Host ""
Write-Host "Если снова увидите ошибку 'Conflict: terminated by other getUpdates" -ForegroundColor DarkGray
Write-Host "request' — значит, где-то ещё вручную запущен python -m bot.bot" -ForegroundColor DarkGray
Write-Host "в отдельном окне. Закройте все такие окна и запустите скрипт заново." -ForegroundColor DarkGray
Write-Host ""
Write-Host "Когда закончите проверку, остановите всё командой:" -ForegroundColor DarkGray
Write-Host "  .\scripts\dev-tunnel.ps1 -Stop" -ForegroundColor DarkGray
