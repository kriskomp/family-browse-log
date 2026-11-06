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

$agentVersion = '2026.11.06.8'

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
# in the same folder, so each account gets its own log, queue and
# temp copy of History. There are NO cursor files any more: where to resume reading
# history is decided by the server (GET /config -> since) and kept only in memory. The ONLY shared file is settings.dat (read-only config).
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
    history_overlap_sec = 900    # czytaj od (punkt wznowienia - overlap); serwer odrzuca duplikaty
    backfill_days     = 7        # nowy profil / polecenie 'backfill': tyle dni wstecz
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

# WebKit microseconds (since 1601) <-> unix epoch seconds
function New-Pos([int64]$epochSec) {
    # id = MaxValue switches the "id > pos.id" part of the query off until a batch sets it
    return @{ vt = (($epochSec + 11644473600L) * 1000000L); id = [int64]::MaxValue }
}

function New-ReadResult {
    return [PSCustomObject]@{
        rows = [System.Collections.Generic.List[object]]::new()
        maxId = [int64]0; maxVt = [int64]0          # of the rows returned (WebKit us)
        dbMaxId = [int64]0; dbMaxVt = [int64]0      # of the whole History file (diagnostics)
        error = $null
    }
}

# Reads visits from a COPY of the History db (avoids the lock held by a running Edge):
#   rows with visit_time > $pos.vt  OR  id > $pos.id, oldest first, at most $limit.
# $cap > 0 (new profile / backfill): start no further back than the $cap newest visits.
# Edge's visits.id is NOT trusted as a resume point (it restarts when Edge rebuilds History):
# the position is time based, the server de-duplicates what is read twice.
function Read-EdgeHistory($historyPath, $pos, $limit, $cap) {
    $res = New-ReadResult
    $tmp = Join-Path $stateDir ("tmp_hist_" + $userKey + ".db")
    try {
        # Remove the previous copy (and its -wal/-shm) first: a stale WAL left over
        # from another Edge profile must never be applied to this profile's copy.
        foreach ($t in @($tmp, ($tmp + "-wal"), ($tmp + "-shm"))) {
            Remove-Item $t -Force -ErrorAction SilentlyContinue
        }
        Copy-Item $historyPath $tmp -Force -ErrorAction Stop
        foreach ($ext in @("-wal","-shm")) {
            if (Test-Path ($historyPath + $ext)) { Copy-Item ($historyPath + $ext) ($tmp + $ext) -Force }
        }
    }
    catch { $res.error = "copy failed: $($_.Exception.Message)"; return $res }

    $db = [IntPtr]::Zero
    if ($SQLite::sqlite3_open_v2($tmp, [ref]$db, 0x02, [IntPtr]::Zero) -ne 0) {   # 0x02 = READWRITE (copy)
        $res.error = "open_v2 failed"; return $res
    }

    # diagnostics: newest id / visit_time in the whole file
    $st0 = [IntPtr]::Zero
    if ($SQLite::sqlite3_prepare_v2($db, "SELECT CAST(MAX(id) AS TEXT), CAST(MAX(visit_time) AS TEXT) FROM visits;", -1, [ref]$st0, [ref][IntPtr]::Zero) -eq 0) {
        if ($SQLite::sqlite3_step($st0) -eq 100) {
            $x = [int64]0; [int64]::TryParse((PtrToUtf8 ($SQLite::sqlite3_column_text($st0,0))), [ref]$x) | Out-Null; $res.dbMaxId = $x
            $x = [int64]0; [int64]::TryParse((PtrToUtf8 ($SQLite::sqlite3_column_text($st0,1))), [ref]$x) | Out-Null; $res.dbMaxVt = $x
        }
        $null = $SQLite::sqlite3_finalize($st0)
    }

    $vt = [int64]$pos.vt
    $id = [int64]$pos.id
    if ($cap -gt 0) {
        $st2 = [IntPtr]::Zero
        $capQ = "SELECT CAST(visit_time AS TEXT) FROM visits ORDER BY visit_time DESC LIMIT 1 OFFSET " + ([int]$cap - 1) + ";"
        if ($SQLite::sqlite3_prepare_v2($db, $capQ, -1, [ref]$st2, [ref][IntPtr]::Zero) -eq 0) {
            if ($SQLite::sqlite3_step($st2) -eq 100) {
                $c = [int64]0; [int64]::TryParse((PtrToUtf8 ($SQLite::sqlite3_column_text($st2,0))), [ref]$c) | Out-Null
                if (($c - 1) -gt $vt) { $vt = $c - 1 }
            }
            $null = $SQLite::sqlite3_finalize($st2)
        }
        $id = [int64]::MaxValue
    }

    # columns: id, url, title, visit_time as unix seconds, raw visit_time (WebKit us)
    $q = "SELECT CAST(v.id AS TEXT), u.url, u.title, CAST((v.visit_time/1000000 - 11644473600) AS TEXT), CAST(v.visit_time AS TEXT) " +
         "FROM visits v JOIN urls u ON u.id = v.url " +
         "WHERE (v.visit_time > $vt OR v.id > $id) ORDER BY v.visit_time, v.id LIMIT $limit;"

    $stmt = [IntPtr]::Zero
    if ($SQLite::sqlite3_prepare_v2($db, $q, -1, [ref]$stmt, [ref][IntPtr]::Zero) -ne 0) {
        $null = $SQLite::sqlite3_close($db); $res.error = "prepare_v2 failed"; return $res
    }
    while ($SQLite::sqlite3_step($stmt) -eq 100) {
        $vid = PtrToUtf8 ($SQLite::sqlite3_column_text($stmt,0))
        $url = PtrToUtf8 ($SQLite::sqlite3_column_text($stmt,1))
        $ttl = PtrToUtf8 ($SQLite::sqlite3_column_text($stmt,2))
        $ts  = PtrToUtf8 ($SQLite::sqlite3_column_text($stmt,3))
        $raw = PtrToUtf8 ($SQLite::sqlite3_column_text($stmt,4))
        $vv = [int64]0; [int64]::TryParse($vid, [ref]$vv) | Out-Null
        $tt = [int64]0; [int64]::TryParse($ts,  [ref]$tt) | Out-Null
        $rr = [int64]0; [int64]::TryParse($raw, [ref]$rr) | Out-Null
        $res.rows.Add([PSCustomObject]@{ visit_id = $vv; url = $url; title = $ttl; ts = $tt }) | Out-Null
        if ($vv -gt $res.maxId) { $res.maxId = $vv }
        if ($rr -gt $res.maxVt) { $res.maxVt = $rr }
    }
    $null = $SQLite::sqlite3_finalize($stmt)
    $null = $SQLite::sqlite3_close($db)
    return $res
}

# Where to read from, per Edge profile. Lives in memory only; after a restart it is rebuilt
# from the server (GET /config -> since). Nothing about it is stored on this PC.
$script:readPos      = @{}      # profile -> @{ vt = WebKit us; id = visits.id }
$script:since        = @{}      # profile -> unix seconds of the newest visit the server has
$script:cfgOk        = $false   # GET /config answered with a resume point (new server)
$script:cfgWarned    = $false
$script:backfillDays = 0        # > 0 = a 'backfill' command is being executed
$script:handledCmds  = @{}      # command ids already acted on (the server resends until acked)
$script:cmdToAck     = @()      # ids received, acknowledged after the backfill was read
$script:ackIds       = @()      # ids to confirm with the next block
$script:diag         = @()      # per-profile diagnostics for the next block
$script:lastDiagSent = [DateTime]::MinValue

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
    # piggy-back diagnostics and command confirmations on this block
    $payloadObj | Add-Member -NotePropertyName agent_version -NotePropertyValue $agentVersion -Force
    if (@($script:diag).Count -gt 0) {
        $payloadObj | Add-Member -NotePropertyName diag -NotePropertyValue @($script:diag) -Force
        $script:diag = @()
        $script:lastDiagSent = Get-Date
    }
    if (@($script:ackIds).Count -gt 0) {
        $payloadObj | Add-Member -NotePropertyName acked -NotePropertyValue @($script:ackIds) -Force
        $script:ackIds = @()
    }
    $script:seq++
    # UTC, so the name order never goes backwards when the local clock is turned back
    # (end of DST) - blocks must reach the server oldest first (window sessions rely on it)
    $name  = "{0}_{1:000}.json" -f ((Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmssfff")), $script:seq
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
            # Only process settings when enabled. An excluded user gets
            # enabled:false with empty settings, whose .PSObject.Properties.Name is $null.
            if ($enabled) {
                if ($rc.settings) {
                    $sNames = @($rc.settings.PSObject.Properties.Name) | Where-Object { $_ }
                    foreach ($k in $sNames) { $settings[$k] = [int]$rc.settings.$k }
                }
                # resume point per profile (unix seconds). An old server has no `since`:
                # history reading then waits (visit ids are no longer used) - update the server.
                if ($rc.PSObject.Properties.Name -contains 'since') {
                    $script:cfgOk = $true
                    $script:since = @{}
                    if ($rc.since) {
                        foreach ($p in (@($rc.since.PSObject.Properties.Name) | Where-Object { $_ })) {
                            $script:since[$p] = [int64]$rc.since.$p
                        }
                    }
                    # commands queued on the server (e.g. backfill); the server resends them
                    # until we confirm, so remember which ones we already acted on
                    foreach ($c in @($rc.commands)) {
                        if ($null -eq $c -or $script:handledCmds.ContainsKey([int64]$c.id)) { continue }
                        $script:handledCmds[[int64]$c.id] = $true
                        if ($c.command -eq 'backfill') {
                            $days = [int]$settings.backfill_days
                            if ($c.arg) { $days = [int]$c.arg }
                            if ($days -lt 1) { $days = 1 }
                            $script:backfillDays = $days
                            $script:cmdToAck += [int64]$c.id
                            LogInfo "Polecenie z serwera: backfill $days dni (id=$($c.id))"
                        }
                        else {
                            $script:ackIds += [int64]$c.id      # unknown command: confirm, do nothing
                            LogInfo "Nieznane polecenie '$($c.command)' (id=$($c.id)) pominiete"
                        }
                    }
                }
                elseif (-not $script:cfgWarned) {
                    $script:cfgWarned = $true
                    LogError "Serwer nie zwraca 'since' (stara wersja) - czytanie historii wstrzymane do aktualizacji serwera"
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
        #    Drains in consecutive blocks until caught up (block < block_size).
        #    Waits until GET /config gave a resume point: never guess where to start.
        if ($script:cfgOk -and (($now - $lastHistory).TotalSeconds -ge [int]$settings.history_read_sec)) {
            $lastHistory = $now
            $attachedWindows = $false
            $bs        = [int]$settings.block_size
            $overlap   = [int64]$settings.history_overlap_sec
            $nowEpoch  = [int64][Math]::Floor(([DateTimeOffset](Get-Date)).ToUnixTimeSeconds())
            $profiles  = @(Get-EdgeProfiles)
            $diagList  = @()
            $hadError  = $false
            $queuedAny = $false
            if ($profiles.Count -eq 0) {
                $diagList += [PSCustomObject]@{ profile = "(none)"; error = "no Edge profile with a History file found" }
                $hadError = $true
            }
            foreach ($p in $profiles) {
                $name = $p.FolderName
                $cap  = 0
                if ($script:backfillDays -gt 0) {
                    # command from the server: read the last N days again (server skips what it has)
                    $script:readPos[$name] = New-Pos ($nowEpoch - [int64]$script:backfillDays * 86400)
                    $cap = [int]$settings.max_backfill
                }
                elseif (-not $script:readPos.ContainsKey($name)) {
                    if ($script:since.ContainsKey($name)) {
                        $script:readPos[$name] = New-Pos ($script:since[$name] - $overlap)
                    }
                    else {   # profile the server has never seen
                        $script:readPos[$name] = New-Pos ($nowEpoch - [int64]$settings.backfill_days * 86400)
                        $cap = [int]$settings.max_backfill
                    }
                }
                $total = 0; $err = $null; $info = $null
                for ($iter = 0; $iter -lt 20; $iter++) {
                    $pos = $script:readPos[$name]
                    $r = Read-EdgeHistory $p.HistoryPath $pos $bs $cap
                    $cap = 0
                    $info = $r
                    if ($r.error) { $err = $r.error; break }
                    $n = $r.rows.Count
                    if ($n -eq 0) { break }
                    $wins = @()
                    if (-not $attachedWindows) { $wins = @($windowBuffer); $attachedWindows = $true }
                    Enqueue ([PSCustomObject]@{
                        computer = $komputer; user = $user; profile = $name
                        visits = @($r.rows); windows = $wins
                    })
                    $queuedAny = $true
                    # advance: newest time / id seen. A FULL block may have cut a group of
                    # visits with the same time, so step back 1 us (the server skips repeats).
                    $newVt = $r.maxVt
                    if ($n -ge $bs -and ($newVt - 1) -gt [int64]$pos.vt) { $newVt = $newVt - 1 }
                    if ([int64]$pos.vt -gt $newVt) { $newVt = [int64]$pos.vt }
                    $newId = $r.maxId
                    if ([int64]$pos.id -ne [int64]::MaxValue -and [int64]$pos.id -gt $newId) { $newId = [int64]$pos.id }
                    $script:readPos[$name] = @{ vt = $newVt; id = $newId }
                    $total += $n
                    LogInfo "Zakolejkowano $n wizyt [$name]"
                    if ($n -lt $bs) { break }   # caught up
                }
                if ($err) { $hadError = $true; LogError "Historia [$name]: $err" }
                $mtime = $null
                try { $mtime = [int64][Math]::Floor(([DateTimeOffset](Get-Item $p.HistoryPath).LastWriteTimeUtc).ToUnixTimeSeconds()) } catch { }
                $dbVt = $null
                if ($info -and $info.dbMaxVt -gt 0) { $dbVt = [int64][Math]::Floor($info.dbMaxVt / 1000000) - 11644473600L }
                $diagList += [PSCustomObject]@{
                    profile = $name; history_mtime = $mtime
                    max_visit_id = $(if ($info) { $info.dbMaxId } else { $null })
                    max_visit_ts = $dbVt; rows_read = $total; error = $err
                }
            }
            if ($attachedWindows) { $windowBuffer.Clear() }
            if ($script:backfillDays -gt 0) {
                $script:backfillDays = 0
                $script:ackIds += @($script:cmdToAck); $script:cmdToAck = @()
            }
            $script:diag = $diagList
            # diagnostics ride on the next block; send a block of their own when there is
            # something to say (error / command confirmation) or every 10 minutes
            if ($hadError -or @($script:ackIds).Count -gt 0 -or
                    (($now - $script:lastDiagSent).TotalSeconds -ge 600)) {
                $fp = ($profiles | Select-Object -First 1).FolderName
                if (-not $fp) { $fp = "Default" }
                Enqueue ([PSCustomObject]@{
                    computer = $komputer; user = $user; profile = $fp
                    visits = @(); windows = @()
                })
            }
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
