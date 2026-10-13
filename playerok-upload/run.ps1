# ==========================================================
#  Playerok Deals - launcher
#
#  1. podnimaet HTTPS-tunnel na port 8080
#     (snachala cloudflared, potom serveo kak zapasnoy)
#  2. pishet poluchennyy adres v .env
#  3. zapuskaet bota
#
#  Stop: Ctrl+C v etom okne
# ==========================================================

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

# Refresh PATH for python / cloudflared
$env:Path = [Environment]::GetEnvironmentVariable('Path','User') + ';' + [Environment]::GetEnvironmentVariable('Path','Machine')

Set-Location $PSScriptRoot
Write-Host ''
Write-Host '  Playerok Deals - zapusk' -ForegroundColor Cyan
Write-Host '  ------------------------' -ForegroundColor Cyan
Write-Host ''

# --- 1. check .env ---
if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Host '  Sozdan .env. Vpishi BOT_TOKEN.' -ForegroundColor Yellow
    notepad '.env'
    Read-Host '  Enter, kogda sohranil'
}

$tokenLine = (Get-Content '.env' | Where-Object { $_ -match '^BOT_TOKEN=' }) -replace '^BOT_TOKEN=',''
if (-not $tokenLine -or $tokenLine -notmatch ':' -or $tokenLine -like '*ABC-DEF*') {
    Write-Host '  V .env net BOT_TOKEN.' -ForegroundColor Red
    notepad '.env'
    Read-Host '  Enter, kogda sohranil'
}

# --- helper: zhdem URL v loge ---
function Wait-TunnelUrl {
    param([string]$LogPath, [string]$Pattern, [int]$Seconds, [scriptblock]$Alive)
    for ($i = 0; $i -lt $Seconds; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Path $LogPath) {
            $hit = Select-String -Path $LogPath -Pattern $Pattern -AllMatches -ErrorAction SilentlyContinue |
                   Select-Object -First 1
            if ($hit) {
                foreach ($m in $hit.Matches) {
                    if ($m.Value -notlike 'https://api.*') { return $m.Value }
                }
            }
        }
        if ($Alive -and (& $Alive)) { return $null }
    }
    return $null
}

$url = $null
$tunnel = $null
$tunnelKind = $null

# --- 2. ngrok (nastoyachivyy variant, nuzhen besplatnyy akkaunt) ---
if (Get-Command ngrok -ErrorAction SilentlyContinue) {
    Write-Host '  Probuyu ngrok...' -ForegroundColor Gray
    $ngLog = Join-Path $PSScriptRoot 'tunnel.ngrok.log'
    $ngErr = Join-Path $PSScriptRoot 'tunnel.ngrok.err.log'
    Remove-Item $ngLog, $ngErr -Force -ErrorAction SilentlyContinue
    # NGROK_DOMAIN in .env removes the ngrok warning page (ERR_NGROK_6024).
    # Free static domain: dashboard.ngrok.com -> Domains -> Create domain.
    $ngDomain = ((Get-Content '.env' | Where-Object { $_ -match '^NGROK_DOMAIN=' }) -replace '^NGROK_DOMAIN=','').Trim()
    $ngArgs = @('http', '8080', '--log', 'stdout', '--log-format', 'logfmt')
    if ($ngDomain) { $ngArgs += @('--domain', $ngDomain) }
    $ng = Start-Process -FilePath 'ngrok' -ArgumentList $ngArgs `
        -RedirectStandardOutput $ngLog -RedirectStandardError $ngErr -PassThru -NoNewWindow
    $url = Wait-TunnelUrl -LogPath $ngLog -Pattern 'https://[-a-z0-9]+\.ngrok[a-z-]*\.(app|io|dev)' -Seconds 30 `
                         -Alive { $ng.HasExited }
    if (-not $url) {
        $url = Wait-TunnelUrl -LogPath $ngErr -Pattern 'https://[-a-z0-9]+\.ngrok[a-z-]*\.(app|io|dev)' -Seconds 10 `
                             -Alive { $ng.HasExited }
    }
    if ($url) {
        $tunnel = $ng; $tunnelKind = 'ngrok'
        Write-Host '  ngrok gotov.' -ForegroundColor Green
    } else {
        if (-not $ng.HasExited) { Stop-Process -Id $ng.Id -Force -ErrorAction SilentlyContinue }
        Write-Host '  ngrok ne zapuskayetsya (net authtoken).' -ForegroundColor Yellow
    }
}

# --- 3. cloudflared ---
if (-not $url) {
  Write-Host '  Probuyu cloudflared...' -ForegroundColor Gray
$cfOut = Join-Path $PSScriptRoot 'tunnel.log'
$cfErr = Join-Path $PSScriptRoot 'tunnel.err.log'
Remove-Item $cfOut, $cfErr -Force -ErrorAction SilentlyContinue

if (Get-Command cloudflared -ErrorAction SilentlyContinue) {
    $cf = Start-Process -FilePath 'cloudflared' `
        -ArgumentList 'tunnel', '--url', 'http://localhost:8080', '--no-autoupdate' `
        -RedirectStandardOutput $cfOut -RedirectStandardError $cfErr -PassThru -NoNewWindow
    $url = Wait-TunnelUrl -LogPath $cfErr -Pattern 'https://[-a-z0-9]+\.trycloudflare\.com' -Seconds 20 `
                         -Alive { $cf.HasExited }
    if (-not $url) { $url = Wait-TunnelUrl -LogPath $cfOut -Pattern 'https://[-a-z0-9]+\.trycloudflare\.com' -Seconds 8 `
                         -Alive { $cf.HasExited } }
    if ($url) {
        $tunnel = $cf; $tunnelKind = 'cloudflared'
        Write-Host '  cloudflared gotov.' -ForegroundColor Green
    } else {
        if (-not $cf.HasExited) { Stop-Process -Id $cf.Id -Force -ErrorAction SilentlyContinue }
        Write-Host '  cloudflared nedostupen na etoy seti, pereklyuchayus na serveo...' -ForegroundColor Yellow
    }
}
}

# --- 4. serveo (zapasnoy, bez akkaunta; na etoy seti mozhet otdavat 502) ---
if (-not $url) {
    Write-Host '  Probuyu serveo...' -ForegroundColor Gray
    $svOut = Join-Path $PSScriptRoot 'tunnel.serveo.log'
    $svErr = Join-Path $PSScriptRoot 'tunnel.serveo.err.log'
    Remove-Item $svOut, $svErr -Force -ErrorAction SilentlyContinue
    $sv = Start-Process -FilePath 'ssh' `
        -ArgumentList '-T','-o','StrictHostKeyChecking=no','-o','ExitOnForwardFailure=yes','-R','80:localhost:8080','serveo.net' `
        -RedirectStandardOutput $svOut -RedirectStandardError $svErr -PassThru -NoNewWindow
    $url = Wait-TunnelUrl -LogPath $svOut -Pattern 'https://[-a-z0-9.]+\.serveousercontent\.com' -Seconds 15 `
                         -Alive { $sv.HasExited }
    if (-not $url) {
        $url = Wait-TunnelUrl -LogPath $svErr -Pattern 'https://[-a-z0-9.]+\.serveousercontent\.com' -Seconds 25 `
                             -Alive { $sv.HasExited }
    }
    if ($url) {
        $tunnel = $sv; $tunnelKind = 'serveo'
        Write-Host '  serveo gotov.' -ForegroundColor Green
    } else {
        if (-not $sv.HasExited) { Stop-Process -Id $sv.Id -Force -ErrorAction SilentlyContinue }
        Write-Host '  Oba tunnela ne podnyalis.' -ForegroundColor Red
        Write-Host '  Otkroy tunnel.log i tunnel.serveo.log - tam prichina.' -ForegroundColor Yellow
        Read-Host '  Enter dlya vyhoda'
        exit 1
    }
}

Write-Host ''
Write-Host "  Adres Mini App: $url" -ForegroundColor Green
Write-Host "  (cherez $tunnelKind)" -ForegroundColor DarkGray

# --- 4. write into .env ---
$content = Get-Content '.env'
$content = $content -replace '^WEBAPP_URL=.*$', "WEBAPP_URL=$url"
Set-Content '.env' $content -Encoding UTF8
Write-Host '  Adres vpisan v .env' -ForegroundColor Green

# --- 5. check that the tunnel serves the app, and not a placeholder ---
# ngrok on the free plan shows a warning page (ERR_NGROK_6024) to the browser,
# and the Telegram webview shows it too, so the Mini App will not open.
$ngDomainConfigured = $false
$domainLine = (Get-Content '.env' | Where-Object { $_ -match '^NGROK_DOMAIN=' }) -replace '^NGROK_DOMAIN=',''
if ($domainLine.Trim()) { $ngDomainConfigured = $true }

try {
    $probe = Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 25 -UserAgent 'Mozilla/5.0'
    if ($probe.Content -match 'ngrok' -and $probe.Content -notmatch 'Playerok Deals') {
        Write-Host ''
        Write-Host '  ВНИМАНИЕ: ngrok pokazyvaet zaglushku, a ne prilozhenie.' -ForegroundColor Yellow
        Write-Host '  Telegram ukazhet na yeyo vmesto Mini App.' -ForegroundColor Yellow
        if (-not $ngDomainConfigured) {
            Write-Host ''
            Write-Host '  KAK POCHINIT:' -ForegroundColor Yellow
            Write-Host '   1. dashboard.ngrok.com -> Domains -> Create domain' -ForegroundColor Gray
            Write-Host '   2. v .env propishi: NGROK_DOMAIN=tvoydomen.ngrok-free.app' -ForegroundColor Gray
            Write-Host '   3. zapusti run.bat zanovo' -ForegroundColor Gray
        }
    } else {
        Write-Host '  Tunnel otdayot prilozhenie - vse v poryadke' -ForegroundColor Green
    }
} catch {
    Write-Host ''
    Write-Host "  Tunnel ne otdayot stranicu: $($_.Exception.Message)" -ForegroundColor Yellow
}

# --- 6. start bot ---
Write-Host ''
Write-Host '  Zapuskayu bota. Ne zakryvay eto okno.' -ForegroundColor Cyan
Write-Host '  Otkroy v Telegram: @playerokofficialdeal_bot -> /start' -ForegroundColor Cyan
Write-Host '  Ostanovit: Ctrl+C' -ForegroundColor DarkGray
Write-Host ''

try {
    python -u -m bot
}
finally {
    Write-Host ''
    Write-Host '  Bot ostoplen, ostanavlivayu tunnel...' -ForegroundColor Yellow
    if ($tunnel -and -not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue }
}