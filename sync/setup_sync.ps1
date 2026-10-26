<#
노트북 → 메인 PC 단방향 자동 동기화 (Tailscale 망 위에서 Syncthing)

순서
  1) 메인 PC : .\setup_sync.ps1 -Role Main             → 출력되는 "내 기기 ID" 복사
  2) 노트북  : .\setup_sync.ps1 -Role Laptop -PeerId <메인ID> -PeerHost <메인 Tailscale 이름 또는 100.x IP>
               → 출력되는 "내 기기 ID" 복사
  3) 메인 PC : .\setup_sync.ps1 -Role Main   -PeerId <노트북ID> -PeerHost <노트북 Tailscale 이름 또는 IP>

- 폴더 목록·메인 저장 위치는 sync_config.json 에서 지정 (laptop_path 가 빈 폴더는 건너뜀)
- 노트북 폴더 = 보내기 전용, 메인 폴더 = 받기 전용 (+ 노트북에서 지워도 메인 사본 유지)
- 공용 릴레이·글로벌 탐색을 끄고 Tailscale 주소로만 연결 → 파일이 tailnet 밖으로 나가지 않음
- 로그온 시 Syncthing 자동 시작(작업 스케줄러). 다시 실행해도 안전(설정을 덮어씀)
#>
param(
    [Parameter(Mandatory)][ValidateSet("Laptop", "Main")][string]$Role,
    [string]$PeerId = "",
    [string]$PeerHost = ""
)
$ErrorActionPreference = "Stop"
$cfg = Get-Content (Join-Path $PSScriptRoot "sync_config.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$Gui = "http://127.0.0.1:8384"

function Find-Exe($name, $extra) {
    $c = Get-Command $name -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($p in $extra) { if ($p -and (Test-Path $p)) { return $p } }
    return $null
}

# ── 1. Tailscale ─────────────────────────────────────────
$ts = Find-Exe "tailscale" @("$env:ProgramFiles\Tailscale\tailscale.exe")
if (-not $ts) {
    Write-Host "Tailscale 설치 (관리자 승인 창이 뜹니다)..."
    winget install --id Tailscale.Tailscale -e --accept-source-agreements --accept-package-agreements
    $ts = "$env:ProgramFiles\Tailscale\tailscale.exe"
}
$ErrorActionPreference = "Continue"   # 로그아웃 상태면 tailscale 이 stderr 를 내므로 일시 완화
$tsStatus = & $ts status 2>&1 | Out-String
$ErrorActionPreference = "Stop"
if ($LASTEXITCODE -ne 0 -or $tsStatus -match "Logged out|NeedsLogin|not running") {
    Write-Host "Tailscale 로그인이 필요합니다. 브라우저에서 메인 PC와 같은 계정으로 로그인하세요."
    & $ts up
}
$myTsIp = (& $ts ip -4 | Select-Object -First 1).Trim()
Write-Host "이 PC의 Tailscale IP: $myTsIp"

# ── 2. Syncthing 설치 (사용자 권한) ─────────────────────
$st = Find-Exe "syncthing" @("$env:LOCALAPPDATA\Microsoft\WinGet\Links\syncthing.exe")
if (-not $st) {
    Write-Host "Syncthing 설치..."
    winget install --id Syncthing.Syncthing -e --accept-source-agreements --accept-package-agreements
    $st = Find-Exe "syncthing" @("$env:LOCALAPPDATA\Microsoft\WinGet\Links\syncthing.exe")
    if (-not $st) { $st = (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Syncthing.Syncthing*" -Recurse -Filter syncthing.exe | Select-Object -First 1).FullName }
}
if (-not $st) { throw "syncthing.exe 를 찾지 못했습니다." }

# ── 3. 로그온 시 자동 시작 + 지금 실행 ───────────────────
$taskName = "Syncthing (강의 동기화)"
$action = New-ScheduledTaskAction -Execute $st -Argument "serve --no-browser --no-console"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
if (-not (Get-Process syncthing -ErrorAction SilentlyContinue)) { Start-ScheduledTask -TaskName $taskName }

# REST API 준비 대기
$cfgXml = Join-Path $env:LOCALAPPDATA "Syncthing\config.xml"
for ($i = 0; $i -lt 60 -and -not (Test-Path $cfgXml); $i++) { Start-Sleep 1 }
$apiKey = ([xml](Get-Content $cfgXml -Raw)).configuration.gui.apikey
$H = @{ "X-API-Key" = $apiKey }
function Api($method, $path, $body) {
    $req = @{ Method = $method; Uri = "$Gui$path"; Headers = $H; ContentType = "application/json; charset=utf-8" }
    if ($null -ne $body) { $req.Body = [Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Depth 10)) }
    Invoke-RestMethod @req
}
for ($i = 0; $i -lt 30; $i++) { try { $myId = (Api GET "/rest/system/status" $null).myID; break } catch { Start-Sleep 1 } }
if (-not $myId) { throw "Syncthing API에 연결하지 못했습니다." }

# ── 4. 공용 인터넷 경로 차단: Tailscale 주소로만 통신 ────
Api PATCH "/rest/config/options" @{
    globalAnnounceEnabled = $false; relaysEnabled = $false; natEnabled = $false; localAnnounceEnabled = $false
    listenAddresses = @("tcp://${myTsIp}:22000"); urAccepted = -1
} | Out-Null

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " 내 기기 ID ($Role): $myId" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

if (-not $PeerId) {
    Write-Host "상대 PC에서 위 ID를 -PeerId 로 넣어 실행한 뒤, 상대 ID를 받아 이 스크립트를 -PeerId 와 함께 다시 실행하세요."
    exit 0
}
if (-not $PeerHost) { throw "-PeerHost (상대 PC의 Tailscale 이름 또는 100.x IP) 가 필요합니다." }
$peerIp = if ($PeerHost -match '^\d+\.\d+\.\d+\.\d+$') { $PeerHost } else { (& $ts ip -4 $PeerHost | Select-Object -First 1).Trim() }

# ── 5. 상대 기기 등록 ────────────────────────────────────
$peerName = if ($Role -eq "Laptop") { "메인 PC" } else { "노트북" }
Api PUT "/rest/config/devices/$PeerId" @{
    deviceID = $PeerId; name = $peerName; addresses = @("tcp://${peerIp}:22000"); autoAcceptFolders = $false
} | Out-Null

# ── 6. 폴더 공유 ─────────────────────────────────────────
foreach ($f in $cfg.folders) {
    if (-not $f.laptop_path) { Write-Host "건너뜀(경로 미지정): $($f.label)"; continue }
    if ($Role -eq "Laptop") {
        if (-not (Test-Path $f.laptop_path)) { throw "폴더 없음: $($f.laptop_path)" }
        $path = $f.laptop_path; $type = "sendonly"; $ignoreDelete = $false
    } else {
        $path = Join-Path $cfg.main_base_dir $f.label; $type = "receiveonly"; $ignoreDelete = [bool]$cfg.keep_deleted_on_main
        New-Item -ItemType Directory -Force $path | Out-Null
    }
    Api PUT "/rest/config/folders/$($f.id)" @{
        id = $f.id; label = $f.label; path = $path; type = $type
        devices = @(@{ deviceID = $myId }, @{ deviceID = $PeerId })
        ignoreDelete = $ignoreDelete
        fsWatcherEnabled = $true; fsWatcherDelayS = 60   # 녹음 중 계속 바뀌는 파일은 1분 모아서 전송
        rescanIntervalS = 3600
    } | Out-Null
    Write-Host "공유 설정: $($f.label) [$type] $path"
}
Write-Host ""
Write-Host "완료. 상태 확인: 브라우저에서 $Gui"
