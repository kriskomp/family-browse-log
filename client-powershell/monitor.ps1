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

# Everything that belongs to one Windows account is suffixed with this key
# (e.g. LEGION_vvojc). Several accounts may run their agent on the same PC and
# in the same folder, so each account gets its own log, queue, cursors and
# temp copy of History. The ONLY shared file is settings.dat (read-only config).
$userKey  = ("$env:USERDOMAIN\$env:USERNAME") -replace '[^A-Za-z0-9]', '_'
$logFile  = Join-Path $base ("monitor_log_" + $userKey + ".txt")
$queueDir = Join-Path $stateDir ("queue_" + $userKey)

foreach ($d in @($stateDir, $queueDir)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

function LogError($msg) { Add-Content -Path $logFile -Value "$(Get-Date) ERROR: $msg" }
function LogInfo($msg)  { Add-Content -Path $logFile -Value "$(Get-Date) INFO: $msg" }

# =====================================================================
# SINGLE RECORDER PER ACCOUNT
# Only the agent running in the ACTIVE CONSOLE session records for this account.
# Ownership = an exclusive file handle on state\owner_<account>.lock. Windows
# releases that handle automatically when the process ends, so a crashed or
# logged-off agent never leaves the account locked. Works across sessions and
# needs no administrator rights.
# =====================================================================
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class ConsoleSession {
    [DllImport("kernel32.dll")] public static extern uint WTSGetActiveConsoleSessionId();
}
"@

$lockPath = Join-Path $stateDir ("owner_" + $userKey + ".lock")
$script:lockStream = $null
$script:owner      = $false

function Test-ActiveConsoleSession {
    $active = [ConsoleSession]::WTSGetActiveConsoleSessionId()
    $mine   = [uint32][System.Diagnostics.Process]::GetCurrentProcess().SessionId
    return ($active -eq $mine)
}
function Try-AcquireOwner {
    if ($script:lockStream) { return $true }
    try {
        $script:lockStream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate,
                                                    [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        return $true
    }
    catch { $script:lockStream = $null; return $false }   # another live agent holds it
}
function Release-Owner {
    if ($script:lockStream) { $script:lockStream.Dispose(); $script:lockStream = $null }
}

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
    window_all        = 0        # 0 = tylko aktywne okno (zalecane), 1 = wszystkie okna (jak v1)
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

# --- ACTIVE (foreground) WINDOW ---
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class FgWin {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr h);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint procId);
}
"@
function Get-ActiveWindow {
    try {
        $h = [FgWin]::GetForegroundWindow()
        if ($h -eq [IntPtr]::Zero) { return $null }
        $len = [FgWin]::GetWindowTextLength($h)
        $sb  = New-Object System.Text.StringBuilder ($len + 1)
        [void][FgWin]::GetWindowText($h, $sb, $sb.Capacity)
        $title = $sb.ToString()
        if ([string]::IsNullOrEmpty($title)) { return $null }
        if ($title.Length -gt 2000) { $title = $title.Substring(0,2000) }
        $procId = [uint32]0
        [void][FgWin]::GetWindowThreadProcessId($h, [ref]$procId)
        $proc = (Get-Process -Id $procId -ErrorAction SilentlyContinue).ProcessName
        return [PSCustomObject]@{
            ts = [int64][Math]::Floor(([DateTimeOffset](Get-Date)).ToUnixTimeSeconds())
            session_min = 0; program = $proc; window = $title
        }
    }
    catch { return $null }
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
    $tmp  = Join-Path $stateDir ("tmp_hist_" + $userKey + ".db")
    try {
        # Remove the previous copy (and its -wal/-shm) first: a stale WAL left over
        # from another Edge profile must never be applied to this profile's copy.
        foreach ($t in @($tmp, ($tmp + "-wal"), ($tmp + "-shm"))) {
            Remove-Item $t -Force -ErrorAction SilentlyContinue
        }
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

# --- local cursor (max visit_id already queued) per Windows account AND Edge profile ---
# The file name contains the account, so several children's accounts on one PC
# (same state\ folder) never share a cursor. Before this fix all accounts used
# cursor_<profile>.txt, so one account's cursor hid another account's visits.
function Get-CursorFile($profile) { Join-Path $stateDir ("cursor_" + $userKey + "_" + $profile + ".txt") }
function Get-Cursor($profile) {
    $f = Get-CursorFile $profile
    $v = [int64]0
    if (Test-Path $f) { [int64]::TryParse((Get-Content $f -Raw).Trim(), [ref]$v) | Out-Null }
    return $v
}
function Set-Cursor($profile, $value) {
    Set-Content -Path (Get-CursorFile $profile) -Value $value
}

# =====================================================================
# HTTP
# =====================================================================
function Get-RemoteConfig {
    $u = "$baseUrl/config?computer=$([uri]::EscapeDataString($komputer))&user=$([uri]::EscapeDataString($user))"
    return Invoke-RestMethod -Uri $u -Headers $headers -Method Get -TimeoutSec 15
}

function Post-Ingest($payloadObj) {
    if ($null -eq $payloadObj) { throw "pusty payload (null)" }
    $json  = $payloadObj | ConvertTo-Json -Depth 6 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    return Invoke-RestMethod -Uri "$baseUrl/ingest" -Headers $headers -Method Post `
                             -ContentType "application/json; charset=utf-8" -Body $bytes -TimeoutSec 30
}

# --- offline queue: each pending block is a JSON file in state\queue_<account> ---
$script:seq = 0
function Enqueue($payloadObj) {
    if ($null -eq $payloadObj) { return }   # never write an empty block to the queue
    $script:seq++
    $name  = "{0}_{1:000}.json" -f (Get-Date -Format "yyyyMMddHHmmssfff"), $script:seq
    $final = Join-Path $queueDir $name
    $tmpf  = $final + ".tmp"          # written first, renamed when complete (atomic)
    $payloadObj | ConvertTo-Json -Depth 6 | Set-Content -Path $tmpf -Encoding UTF8
    Move-Item -Path $tmpf -Destination $final -Force
}

# Try to send everything queued, oldest first. Server is idempotent, so a
# re-sent block is harmless. On any failure we stop and keep the rest queued.
function Flush-Queue {
    $files = Get-ChildItem -Path $queueDir -Filter *.json -ErrorAction SilentlyContinue | Sort-Object Name
    foreach ($f in $files) {
        # A corrupt/empty block can never be sent. Drop it, so it cannot block the queue.
        $obj = $null
        try { $raw = Get-Content $f.FullName -Raw; if ($raw) { $obj = $raw | ConvertFrom-Json } } catch { $obj = $null }
        if ($null -eq $obj) {
            Remove-Item $f.FullName -Force -ErrorAction SilentlyContinue
            LogError "Uszkodzony blok $($f.Name) usuniety z kolejki"
            continue
        }
        try {
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
$lastWindow    = [DateTime]::MinValue
$enabled       = $true

while ($true) {
    try {
        # 0) only the owner of this account records. Outside the active console
        #    session (fast user switching, logoff, lock of another user) we release
        #    the account and idle; the other session's agent takes over.
        if (-not (Test-ActiveConsoleSession)) {
            if ($script:owner) { LogInfo "Sesja nieaktywna - oddaje nagrywanie konta $userKey"; $script:owner = $false }
            Release-Owner
            Start-Sleep -Seconds ([int]$settings.push_interval_sec)
            continue
        }
        if (-not $script:owner) {
            if (Try-AcquireOwner) { $script:owner = $true; LogInfo "Przejmuje nagrywanie konta $userKey (sesja aktywna)" }
            else { Start-Sleep -Seconds ([int]$settings.push_interval_sec); continue }
        }

        # 1) refresh config from server (settings + authoritative resume point)
        try {
            $rc = Get-RemoteConfig
            $enabled = [bool]$rc.enabled
            # Only process settings/cursors when enabled. An excluded user gets
            # enabled:false with empty settings/last_visit_id objects, whose
            # .PSObject.Properties.Name is $null - skipping avoids touching them.
            if ($enabled) {
                if ($rc.settings) {
                    $sNames = @($rc.settings.PSObject.Properties.Name) | Where-Object { $_ }
                    foreach ($k in $sNames) { $settings[$k] = [int]$rc.settings.$k }
                }
                # align local cursor to server's last_visit_id (server is source of truth)
                if ($rc.last_visit_id) {
                    $pNames = @($rc.last_visit_id.PSObject.Properties.Name) | Where-Object { $_ }
                    foreach ($p in $pNames) {
                        $srv = [int64]$rc.last_visit_id.$p
                        if ($srv -gt (Get-Cursor $p)) { Set-Cursor $p $srv }
                    }
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

        # 2) window sample (only when user is active), every window_sample_sec
        if (((Get-IdleSeconds) -lt [int]$settings.idle_limit_sec) -and
            (($now - $lastWindow).TotalSeconds -ge [int]$settings.window_sample_sec)) {
            $lastWindow = $now
            if ([int]$settings.window_all -eq 1) {
                # wszystkie okna z tytulem (jak v1)
                Get-Process | Where-Object { $_.MainWindowTitle -ne "" } |
                    Select-Object ProcessName, MainWindowTitle | ForEach-Object {
                        $w = $_.MainWindowTitle; if ($w.Length -gt 2000) { $w = $w.Substring(0,2000) }
                        $windowBuffer.Add([PSCustomObject]@{
                            ts = [int64][Math]::Floor(([DateTimeOffset](Get-Date)).ToUnixTimeSeconds())
                            session_min = 0; program = $_.ProcessName; window = $w
                        }) | Out-Null
                    }
            }
            else {
                # tylko aktywne okno na pierwszym planie (zalecane)
                $aw = Get-ActiveWindow
                if ($aw) { $windowBuffer.Add($aw) | Out-Null }
            }
        }

        # 3) read new Edge history into queue (every history_read_sec).
        #    Drains backfill in consecutive blocks until caught up (block < block_size),
        #    instead of one block per cycle.
        if (($now - $lastHistory).TotalSeconds -ge [int]$settings.history_read_sec) {
            $lastHistory = $now
            $attachedWindows = $false
            $bs = [int]$settings.block_size
            foreach ($p in (Get-EdgeProfiles)) {
                while ($true) {
                    $cursor = Get-Cursor $p.FolderName
                    $visits = Read-EdgeHistory $p.HistoryPath $cursor $bs
                    if ($visits.Count -eq 0) { break }
                    $wins = @()
                    if (-not $attachedWindows) { $wins = @($windowBuffer); $attachedWindows = $true }
                    Enqueue ([PSCustomObject]@{
                        computer = $komputer; user = $user; profile = $p.FolderName
                        visits = @($visits); windows = $wins
                    })
                    Set-Cursor $p.FolderName ([int64]($visits[$visits.Count-1].visit_id))
                    LogInfo "Zakolejkowano $($visits.Count) wizyt [$($p.FolderName)]"
                    if ($visits.Count -lt $bs) { break }   # dogonilismy biezacy stan
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
