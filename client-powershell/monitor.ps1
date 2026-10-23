# =====================================================================
# family-browse-log - agent v2 (client)
# Reads Microsoft Edge history + active-window activity and pushes it to
# the server-python endpoint (see ../docs/api.md). Holds NO database
# password - only the server URL and an API token.
#
# Self-locating: all paths are relative to this script's own folder
# ($PSScriptRoot), so the agent can live anywhere - no hard-coded C:\boot.
#
# settings.dat (next to this script, git-ignored) is a positional array:
#     @('http://netguard.local:8080','<API_TOKEN>')
#      [0] = base URL of the endpoint   [1] = API token
# =====================================================================

Start-Sleep -Seconds 20

$ErrorActionPreference = "SilentlyContinue"
while (-not (Get-Process -Name "explorer" -ErrorAction SilentlyContinue)) {
    Start-Sleep -Seconds 1
}

# --- SELF-LOCATING BASE ---
$base     = $PSScriptRoot
$stateDir = Join-Path $base "state"
$queueDir = Join-Path $stateDir "queue"
$logFile  = Join-Path $base "monitor_log.txt"
foreach ($d in @($stateDir, $queueDir)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

function LogError($msg) { Add-Content -Path $logFile -Value "$(Get-Date) ERROR: $msg" }
function LogInfo($msg)  { Add-Content -Path $logFile -Value "$(Get-Date) INFO: $msg" }

# --- CONFIG (server URL + token) from settings.dat next to the script ---
$settingsPath = Join-Path $base "settings.dat"
try {
    $cfg     = Invoke-Expression ((Get-Content -Raw -Path $settingsPath))
    $baseUrl = $cfg[0].TrimEnd('/')
    $token   = $cfg[1]
}
catch {
    LogError "Nie mozna wczytac settings.dat: $($_.Exception.Message)"
    return
}

$komputer = $env:COMPUTERNAME
$user     = "$env:USERDOMAIN\$env:USERNAME"
$headers  = @{ Authorization = "Bearer $token" }

# defaults - overwritten by GET /config when the server is reachable
$settings = @{
    push_interval_sec = 30
    window_sample_sec = 30
    history_read_sec  = 120
    block_size        = 500
    max_backfill      = 3000
    idle_limit_sec    = 300
}

$edgeUserData = Join-Path $env:LOCALAPPDATA "Microsoft\Edge\User Data"

# --- IDLE ---
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class IdleTime {
    [StructLayout(LayoutKind.Sequential)]
    public struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
    [DllImport("user32.dll")]
    public static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
    public static uint GetIdleTime() {
        LASTINPUTINFO info = new LASTINPUTINFO();
        info.cbSize = (uint)Marshal.SizeOf(info);
        GetLastInputInfo(ref info);
        return ((uint)Environment.TickCount - info.dwTime);
    }
}
"@
function Get-IdleSeconds {
    try { return [IdleTime]::GetIdleTime() / 1000 }
    catch { LogError "IdleTime error: $($_.Exception.Message)"; return 0 }
}

# =====================================================================
# EDGE HISTORY READING (winsqlite3.dll - built into Windows, nothing to install)
# =====================================================================
$sig = @"
[DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
public static extern int sqlite3_open_v2(string filename, out IntPtr ppDb, int flags, IntPtr zVfs);
[DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
public static extern int sqlite3_prepare_v2(IntPtr db, string zSql, int nByte, out IntPtr ppStmt, out IntPtr pzTail);
[DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
public static extern int sqlite3_step(IntPtr pStmt);
[DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
public static extern IntPtr sqlite3_column_text(IntPtr pStmt, int iCol);
[DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
public static extern int sqlite3_finalize(IntPtr pStmt);
[DllImport("winsqlite3.dll", CallingConvention = CallingConvention.Cdecl)]
public static extern int sqlite3_close(IntPtr db);
"@
$SQLite = Add-Type -MemberDefinition $sig -Name "WinSQLiteMon" -Namespace "Win32" -PassThru

function PtrToUtf8($ptr) {
    if ($ptr -eq [IntPtr]::Zero) { return "" }
    $bytes = [System.Collections.Generic.List[byte]]::new()
    $i = 0
    while ($true) {
        $b = [System.Runtime.InteropServices.Marshal]::ReadByte($ptr, $i)
        if ($b -eq 0) { break }
        $bytes.Add($b); $i++
    }
    return [System.Text.Encoding]::UTF8.GetString($bytes.ToArray())
}

function Get-EdgeProfiles {
    $res = @()
    if (-not (Test-Path $edgeUserData)) { return $res }
    Get-ChildItem -Path $edgeUserData -Directory -ErrorAction SilentlyContinue | ForEach-Object {
        $h = Join-Path $_.FullName "History"
        if ((Test-Path $h) -and ($_.Name -eq "Default" -or $_.Name -like "Profile *")) {
            $res += [PSCustomObject]@{ FolderName = $_.Name; HistoryPath = $h }
        }
    }
    return $res
}

# Reads new visits (v.id > cursor) from a COPY of the History db (avoids the
# lock held by a running Edge). Returns up to $limit rows ordered by id.
function Read-EdgeHistory($historyPath, $cursor, $limit) {
    $rows = [System.Collections.Generic.List[object]]::new()
    $tmp  = Join-Path $stateDir "tmp_hist.db"
    try {
        Copy-Item $historyPath $tmp -Force
        foreach ($ext in @("-wal","-shm")) {
            if (Test-Path ($historyPath + $ext)) { Copy-Item ($historyPath + $ext) ($tmp + $ext) -Force }
        }
    }
    catch { LogError "Kopia historii nieudana: $($_.Exception.Message)"; return $rows }

    $db = [IntPtr]::Zero
    if ($SQLite::sqlite3_open_v2($tmp, [ref]$db, 0x02, [IntPtr]::Zero) -ne 0) {   # 0x02 = READWRITE (copy)
        LogError "Nie mozna otworzyc kopii historii (open_v2)"; return $rows
    }

    # first run (cursor 0): limit initial backfill to max_backfill newest visits
    $effCursor = $cursor
    $maxBackfill = [int]$settings.max_backfill
    if ($cursor -eq 0 -and $maxBackfill -gt 0) {
        $st2 = [IntPtr]::Zero
        if ($SQLite::sqlite3_prepare_v2($db, "SELECT CAST(MAX(id) AS TEXT) FROM visits;", -1, [ref]$st2, [ref][IntPtr]::Zero) -eq 0) {
            if ($SQLite::sqlite3_step($st2) -eq 100) {
                $mxTxt = PtrToUtf8 ($SQLite::sqlite3_column_text($st2,0))
                $mx = [int64]0; [int64]::TryParse($mxTxt, [ref]$mx) | Out-Null
                if ($mx -gt $maxBackfill) { $effCursor = $mx - $maxBackfill }
            }
            $null = $SQLite::sqlite3_finalize($st2)
        }
    }

    # visit_time: WebKit microseconds since 1601 -> unix epoch seconds
    $q = "SELECT CAST(v.id AS TEXT), u.url, u.title, CAST((v.visit_time/1000000 - 11644473600) AS TEXT) " +
         "FROM visits v JOIN urls u ON u.id = v.url " +
         "WHERE v.id > $effCursor ORDER BY v.id LIMIT $limit;"

    $stmt = [IntPtr]::Zero
    if ($SQLite::sqlite3_prepare_v2($db, $q, -1, [ref]$stmt, [ref][IntPtr]::Zero) -ne 0) {
        $null = $SQLite::sqlite3_close($db); LogError "Blad zapytania historii (prepare_v2)"; return $rows
    }
    while ($SQLite::sqlite3_step($stmt) -eq 100) {
        $vid = PtrToUtf8 ($SQLite::sqlite3_column_text($stmt,0))
        $url = PtrToUtf8 ($SQLite::sqlite3_column_text($stmt,1))
        $ttl = PtrToUtf8 ($SQLite::sqlite3_column_text($stmt,2))
        $ts  = PtrToUtf8 ($SQLite::sqlite3_column_text($stmt,3))
        $vv = [int64]0; [int64]::TryParse($vid, [ref]$vv) | Out-Null
        $tt = [int64]0; [int64]::TryParse($ts,  [ref]$tt) | Out-Null
        $rows.Add([PSCustomObject]@{ visit_id = $vv; url = $url; title = $ttl; ts = $tt }) | Out-Null
    }
    $null = $SQLite::sqlite3_finalize($stmt)
    $null = $SQLite::sqlite3_close($db)
    return $rows
}

# --- local cursor (max visit_id already queued) per profile ---
function Get-Cursor($profile) {
    $f = Join-Path $stateDir ("cursor_" + $profile + ".txt")
    $v = [int64]0
    if (Test-Path $f) { [int64]::TryParse((Get-Content $f -Raw).Trim(), [ref]$v) | Out-Null }
    return $v
}
function Set-Cursor($profile, $value) {
    Set-Content -Path (Join-Path $stateDir ("cursor_" + $profile + ".txt")) -Value $value
}

# =====================================================================
# HTTP
# =====================================================================
function Get-RemoteConfig {
    $u = "$baseUrl/config?computer=$([uri]::EscapeDataString($komputer))&user=$([uri]::EscapeDataString($user))"
    return Invoke-RestMethod -Uri $u -Headers $headers -Method Get -TimeoutSec 15
}

function Post-Ingest($payloadObj) {
    $json  = $payloadObj | ConvertTo-Json -Depth 6 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    return Invoke-RestMethod -Uri "$baseUrl/ingest" -Headers $headers -Method Post `
                             -ContentType "application/json; charset=utf-8" -Body $bytes -TimeoutSec 30
}

# --- offline queue: each pending block is a JSON file in state\queue ---
$script:seq = 0
function Enqueue($payloadObj) {
    $script:seq++
    $name = "{0}_{1:000}.json" -f (Get-Date -Format "yyyyMMddHHmmssfff"), $script:seq
    $payloadObj | ConvertTo-Json -Depth 6 | Set-Content -Path (Join-Path $queueDir $name) -Encoding UTF8
}

# Try to send everything queued, oldest first. Server is idempotent, so a
# re-sent block is harmless. On any failure we stop and keep the rest queued.
function Flush-Queue {
    $files = Get-ChildItem -Path $queueDir -Filter *.json -ErrorAction SilentlyContinue | Sort-Object Name
    foreach ($f in $files) {
        try {
            $obj = Get-Content $f.FullName -Raw | ConvertFrom-Json
            $ack = Post-Ingest $obj
            Remove-Item $f.FullName -Force
            LogInfo "Wyslano $($f.Name): przyjeto=$($ack.accepted_visits) pominieto=$($ack.skipped_visits) okna=$($ack.accepted_windows)"
        }
        catch {
            LogError "Wysylka $($f.Name) nieudana (zostaje w kolejce): $($_.Exception.Message)"
            return $false   # endpoint unreachable - stop, retry next cycle
        }
    }
    return $true
}

# =====================================================================
# MAIN LOOP
# =====================================================================
$windowBuffer  = [System.Collections.Generic.List[object]]::new()
$lastHistory   = [DateTime]::MinValue
$lastPush      = [DateTime]::MinValue
$enabled       = $true

while ($true) {
    try {
        # 1) refresh config from server (settings + authoritative resume point)
        try {
            $rc = Get-RemoteConfig
            $enabled = [bool]$rc.enabled
            if ($rc.settings) {
                foreach ($k in @($rc.settings.PSObject.Properties.Name)) { $settings[$k] = [int]$rc.settings.$k }
            }
            # align local cursor to server's last_visit_id (server is source of truth)
            if ($rc.last_visit_id) {
                foreach ($p in @($rc.last_visit_id.PSObject.Properties.Name)) {
                    $srv = [int64]$rc.last_visit_id.$p
                    if ($srv -gt (Get-Cursor $p)) { Set-Cursor $p $srv }
                }
            }
        }
        catch {
            LogError "GET /config nieudane (pracuje offline z ostatnimi ustawieniami): $($_.Exception.Message)"
        }

        if (-not $enabled) {
            Start-Sleep -Seconds ([int]$settings.push_interval_sec)
            continue
        }

        $now = Get-Date

        # 2) window sample (only when user is active)
        if (((Get-IdleSeconds) -lt [int]$settings.idle_limit_sec) -and
            (($now - $lastPush).TotalSeconds -ge [int]$settings.window_sample_sec -or $windowBuffer.Count -eq 0)) {
            Get-Process | Where-Object { $_.MainWindowTitle -ne "" } |
                Select-Object ProcessName, MainWindowTitle | ForEach-Object {
                    $w = $_.MainWindowTitle; if ($w.Length -gt 2000) { $w = $w.Substring(0,2000) }
                    $windowBuffer.Add([PSCustomObject]@{
                        ts = [int64][Math]::Floor(([DateTimeOffset](Get-Date)).ToUnixTimeSeconds())
                        session_min = 0; program = $_.ProcessName; window = $w
                    }) | Out-Null
                }
        }

        # 3) read new Edge history into queue (every history_read_sec)
        if (($now - $lastHistory).TotalSeconds -ge [int]$settings.history_read_sec) {
            $lastHistory = $now
            $attachedWindows = $false
            foreach ($p in (Get-EdgeProfiles)) {
                $cursor = Get-Cursor $p.FolderName
                $visits = Read-EdgeHistory $p.HistoryPath $cursor ([int]$settings.block_size)
                if ($visits.Count -gt 0) {
                    $wins = @()
                    if (-not $attachedWindows) { $wins = @($windowBuffer); $attachedWindows = $true }
                    Enqueue ([PSCustomObject]@{
                        computer = $komputer; user = $user; profile = $p.FolderName
                        visits = @($visits); windows = $wins
                    })
                    Set-Cursor $p.FolderName ([int64]($visits[$visits.Count-1].visit_id))
                    LogInfo "Zakolejkowano $($visits.Count) wizyt [$($p.FolderName)]"
                }
            }
            if ($attachedWindows) { $windowBuffer.Clear() }
        }

        # 4) push: flush queue; if only windows pending, send a windows-only block
        if (($now - $lastPush).TotalSeconds -ge [int]$settings.push_interval_sec) {
            $lastPush = $now
            if ($windowBuffer.Count -gt 0) {
                $firstProfile = (Get-EdgeProfiles | Select-Object -First 1).FolderName
                if (-not $firstProfile) { $firstProfile = "Default" }
                Enqueue ([PSCustomObject]@{
                    computer = $komputer; user = $user; profile = $firstProfile
                    visits = @(); windows = @($windowBuffer)
                })
                $windowBuffer.Clear()
            }
            Flush-Queue | Out-Null
        }
    }
    catch {
        LogError "Loop error: $($_.Exception.Message)"
    }

    Start-Sleep -Seconds ([int]$settings.push_interval_sec)
}
