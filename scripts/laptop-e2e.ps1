# Laptop B end-to-end check for the Windows build.
# Run later on the test machine (repo at C:\ClaudeIsolateTest). Do not run it
# until dist actually contains the installer and Claude Isolate.exe.
#
# Each check prints one PASS, FAIL, or SKIP line. If the build output is
# missing, the script records that and exits immediately instead of waiting
# on prepare, SSH, or the guest. It never stops Chrome, Firefox, Edge, or
# any other user browser, and it never uses virtfs (Windows QEMU has it
# disabled). Shared files are checked through the app's SFTP sync on
# 127.0.0.1:<ssh_port> (2222 unless the launcher had to choose another port;
# it saves the port as ssh_port in environment.json).
#
# Paths match the app, but this script never uses the existing profile:
#   data     C:\ClaudeIsolateTest\e2e-data   (not %LOCALAPPDATA%\Claude Isolate)
#   key      <data>\tools\ssh_host_key          (OpenSSH ed25519)
#   shared   <data>\shared                      <-> /home/claude/Shared
#   snapshot <data>\state.json and qcow2 tag claude-fast-start
#   pipes    \\.\pipe\<qmp_pipe>                (claude-isolate-<32 hex>)
#            \\.\pipe\<qmp_pipe>-gateway
#            \\.\pipe\<qmp_pipe>-clipboard
param(
    [string]$Dist = '',
    [string]$Data = '',
    [int]$PrepareTimeoutSec = 3600,
    [int]$BootTimeoutSec = 1200,
    [int]$SyncTimeoutSec = 120,
    [int]$ClipboardTimeoutSec = 45
)

$ErrorActionPreference = 'Stop'

if ([Threading.Thread]::CurrentThread.GetApartmentState() -ne 'STA') {
    $hostExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $bound = @()
    foreach ($name in $PSBoundParameters.Keys) {
        $bound += "-$name"
        $bound += [string]$PSBoundParameters[$name]
    }
    & $hostExe -NoProfile -STA -File $PSCommandPath @bound
    exit $LASTEXITCODE
}

$Root = if ($env:CLAUDE_ISOLATE_ROOT) { $env:CLAUDE_ISOLATE_ROOT } else { Split-Path $PSScriptRoot -Parent }
if (-not $Data) { $Data = 'C:\ClaudeIsolateTest\e2e-data' }
$protected = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Claude Isolate')).TrimEnd('\')
if ([IO.Path]::GetFullPath($Data).TrimEnd('\') -eq $protected) {
    Write-Host 'FAIL data refusing to use the existing %LOCALAPPDATA%\Claude Isolate profile'
    exit 1
}
$Results = New-Object System.Collections.Generic.List[string]
$PngBase64 = 'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAEUlEQVR42mP4z+DwH4QZYAwAUEoI+ankQjgAAAAASUVORK5CYII='

function Write-Result([string]$Status, [string]$Name, [string]$Detail) {
    $line = if ($Detail) { "$Status $Name $Detail" } else { "$Status $Name" }
    $script:Results.Add($line)
    Write-Host $line
}

function Exit-Summary {
    Write-Host ''
    Write-Host '=== LAPTOP E2E SUMMARY ==='
    $script:Results | ForEach-Object { Write-Host $_ }
    $failed = @($script:Results | Where-Object { $_ -like 'FAIL *' })
    if ($failed.Count -gt 0) {
        Write-Host 'LAPTOP E2E FAIL'
        exit 1
    }
    Write-Host 'LAPTOP E2E PASS'
    exit 0
}

function Skip-Unchecked([string]$Reason, [string[]]$Names) {
    foreach ($name in $Names) {
        Write-Result 'SKIP' $name $Reason
    }
}

function Quote-Cmd([string]$Value) {
    if ($Value -match '[\s"]') { return '"' + ($Value -replace '"', '\"') + '"' }
    return $Value
}

function Get-LogTail([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    $text = @(Get-Content -LiteralPath $Path -Tail 12 -ErrorAction SilentlyContinue) -join ' '
    $text = ($text -replace '\s+', ' ').Trim()
    if ($text.Length -gt 360) { return $text.Substring($text.Length - 360) }
    return $text
}

function Read-GuestConfig {
    $path = Join-Path $Data 'environment.json'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    return [System.IO.File]::ReadAllText($path) | ConvertFrom-Json
}

function Get-DiskPath($Config) {
    if ($Config -and $Config.disk) { return [string]$Config.disk }
    return Join-Path $Data 'desktop.qcow2'
}

function Get-SshPort {
    $config = Read-GuestConfig
    if ($config -and $config.ssh_port) { return [int]$config.ssh_port }
    return 2222
}

function Get-SharedDir($Config) {
    return Join-Path (Split-Path (Get-DiskPath $Config) -Parent) 'shared'
}

function Stop-OurProcess($Process) {
    if ($Process -and -not $Process.HasExited) {
        & taskkill.exe /PID $Process.Id /T /F 2>$null | Out-Null
    }
}

function New-HiddenProcess([string]$File, [string]$Arguments, [switch]$StandardInput) {
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $File
    $start.Arguments = $Arguments
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $utf8 = New-Object System.Text.UTF8Encoding $false
    $start.StandardOutputEncoding = $utf8
    $start.StandardErrorEncoding = $utf8
    if ($StandardInput) { $start.RedirectStandardInput = $true }
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    return $process
}

function Invoke-Logged([string]$File, [string]$Arguments, [string]$LogPath, [int]$TimeoutSec) {
    $process = New-HiddenProcess $File $Arguments
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    $finished = $process.WaitForExit($TimeoutSec * 1000)
    if (-not $finished) { Stop-OurProcess $process }
    try { [void]$process.WaitForExit(10000) } catch { }
    $text = ''
    try { $text = [string]$stdout.Result } catch { }
    try { $text += [string]$stderr.Result } catch { }
    [System.IO.File]::WriteAllText($LogPath, $text)
    $code = 124
    if ($finished) {
        try { $code = $process.ExitCode } catch { $code = 124 }
    }
    return @{ Code = $code; Text = $text }
}

function Start-Logged([string]$File, [string]$Arguments, [string]$LogPath) {
    $process = New-HiddenProcess $File $Arguments -StandardInput
    $process.EnableRaisingEvents = $true
    [void]$process.Start()
    Register-ObjectEvent -InputObject $process -EventName OutputDataReceived -MessageData $LogPath -Action {
        if ($EventArgs.Data) {
            [System.IO.File]::AppendAllText([string]$Event.MessageData, $EventArgs.Data + [Environment]::NewLine)
        }
    } | Out-Null
    Register-ObjectEvent -InputObject $process -EventName ErrorDataReceived -MessageData $LogPath -Action {
        if ($EventArgs.Data) {
            [System.IO.File]::AppendAllText([string]$Event.MessageData, $EventArgs.Data + [Environment]::NewLine)
        }
    } | Out-Null
    $process.BeginOutputReadLine()
    $process.BeginErrorReadLine()
    return $process
}

function Test-OutputLine([string]$Text, [string]$Expected) {
    foreach ($line in ([string]$Text -split '\r?\n')) {
        if ($line.Trim() -eq $Expected) { return $true }
    }
    return $false
}

function Test-PngPrefix($Got, [byte[]]$Expect) {
    if (-not $Got -or -not $Expect -or $Got.Length -lt $Expect.Length) { return $false }
    for ($i = 0; $i -lt $Expect.Length; $i++) {
        if ($Got[$i] -ne $Expect[$i]) { return $false }
    }
    return $true
}

function Test-FastStartSnapshot([string]$QemuImg, $Config) {
    $marker = Join-Path (Split-Path (Get-DiskPath $Config) -Parent) 'state.json'
    if (Test-Path -LiteralPath $marker) { return $true }
    $disk = Get-DiskPath $Config
    if (-not (Test-Path -LiteralPath $disk) -or -not $QemuImg -or -not (Test-Path -LiteralPath $QemuImg)) {
        return $false
    }
    $infoLog = Join-Path $Data 'e2e-qemu-img.log'
    $info = Invoke-Logged $QemuImg ('info --output=json ' + (Quote-Cmd $disk)) $infoLog 30
    if ($info.Code -ne 0) { return $false }
    return [bool]($info.Text -match '"name"\s*:\s*"claude-fast-start"')
}

function Wait-LocalPort([int]$TimeoutSec, $Process) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if ($Process -and $Process.HasExited) { return 'exited' }
        # The launcher may move SSH off a busy port when it starts.
        $Port = Get-SshPort
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $wait = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
            if ($wait.AsyncWaitHandle.WaitOne(1500) -and $client.Connected) { return 'open' }
        } catch {
        } finally {
            $client.Close()
        }
        Start-Sleep -Seconds 2
    }
    return 'timeout'
}

function Get-SshExe {
    $bundled = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
    if (Test-Path -LiteralPath $bundled) { return $bundled }
    $found = Get-Command ssh.exe -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    return $null
}

function Invoke-GuestSsh([string]$SshExe, [string]$Key, [string]$KnownHosts, [string]$Remote, [int]$TimeoutSec) {
    $args = @(
        '-p', [string](Get-SshPort),
        '-i', $Key,
        '-o', 'BatchMode=yes',
        '-o', 'IdentitiesOnly=yes',
        '-o', 'PreferredAuthentications=publickey',
        '-o', 'NumberOfPasswordPrompts=0',
        '-o', 'ConnectTimeout=15',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', ('UserKnownHostsFile=' + $KnownHosts),
        'claude@127.0.0.1',
        $Remote
    )
    $quoted = @()
    foreach ($item in $args) { $quoted += (Quote-Cmd $item) }
    $log = Join-Path $env:TEMP ('claude-isolate-e2e-ssh-' + [guid]::NewGuid().ToString('n') + '.log')
    $result = Invoke-Logged $SshExe ($quoted -join ' ') $log $TimeoutSec
    Remove-Item -LiteralPath $log -ErrorAction SilentlyContinue
    return @{ Code = $result.Code; Text = [string]$result.Text }
}

function Wait-GuestText([string]$SshExe, [string]$Key, [string]$KnownHosts, [string]$Remote, [string]$Expected, [int]$TimeoutSec) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $reply = Invoke-GuestSsh $SshExe $Key $KnownHosts $Remote 20
        if ($reply.Code -eq 0 -and (Test-OutputLine $reply.Text $Expected)) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

if (-not ('ClipE2E' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class ClipE2E {
  const uint CF_UNICODETEXT = 13;
  const uint GMEM = 0x0042;
  [DllImport("user32.dll", SetLastError=true)] static extern bool OpenClipboard(IntPtr h);
  [DllImport("user32.dll")] static extern bool CloseClipboard();
  [DllImport("user32.dll")] static extern bool EmptyClipboard();
  [DllImport("user32.dll")] static extern IntPtr GetClipboardData(uint format);
  [DllImport("user32.dll")] static extern IntPtr SetClipboardData(uint format, IntPtr mem);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern uint RegisterClipboardFormat(string name);
  [DllImport("user32.dll")] static extern bool IsClipboardFormatAvailable(uint format);
  [DllImport("kernel32.dll")] static extern IntPtr GlobalAlloc(uint flags, UIntPtr bytes);
  [DllImport("kernel32.dll")] static extern IntPtr GlobalLock(IntPtr mem);
  [DllImport("kernel32.dll")] static extern bool GlobalUnlock(IntPtr mem);
  [DllImport("kernel32.dll")] static extern UIntPtr GlobalSize(IntPtr mem);
  [DllImport("kernel32.dll")] static extern IntPtr GlobalFree(IntPtr mem);
  static void CopyTo(IntPtr mem, byte[] payload) {
    IntPtr locked = GlobalLock(mem);
    Marshal.Copy(payload, 0, locked, payload.Length);
    GlobalUnlock(mem);
  }
  public static void SetText(string text) {
    if (!OpenClipboard(IntPtr.Zero)) return;
    try {
      EmptyClipboard();
      byte[] payload = System.Text.Encoding.Unicode.GetBytes(text + "\0");
      IntPtr mem = GlobalAlloc(GMEM, (UIntPtr)payload.Length);
      CopyTo(mem, payload);
      SetClipboardData(CF_UNICODETEXT, mem);
    } finally { CloseClipboard(); }
  }
  public static string GetText() {
    if (!OpenClipboard(IntPtr.Zero)) return "";
    try {
      if (!IsClipboardFormatAvailable(CF_UNICODETEXT)) return "";
      IntPtr mem = GetClipboardData(CF_UNICODETEXT);
      if (mem == IntPtr.Zero) return "";
      IntPtr locked = GlobalLock(mem);
      try { return Marshal.PtrToStringUni(locked) ?? ""; }
      finally { GlobalUnlock(mem); }
    } finally { CloseClipboard(); }
  }
  public static void SetPng(byte[] png) {
    if (!OpenClipboard(IntPtr.Zero)) return;
    try {
      EmptyClipboard();
      uint format = RegisterClipboardFormat("PNG");
      IntPtr mem = GlobalAlloc(GMEM, (UIntPtr)png.Length);
      CopyTo(mem, png);
      SetClipboardData(format, mem);
    } finally { CloseClipboard(); }
  }
  public static byte[] GetPng() {
    if (!OpenClipboard(IntPtr.Zero)) return null;
    try {
      uint format = RegisterClipboardFormat("PNG");
      if (!IsClipboardFormatAvailable(format)) return null;
      IntPtr mem = GetClipboardData(format);
      if (mem == IntPtr.Zero) return null;
      int size = (int)GlobalSize(mem);
      IntPtr locked = GlobalLock(mem);
      try {
        byte[] payload = new byte[size];
        Marshal.Copy(locked, payload, 0, size);
        return payload;
      } finally { GlobalUnlock(mem); }
    } finally { CloseClipboard(); }
  }
}
'@ -Language CSharp
}

function Resolve-Dist {
    $candidates = @()
    if ($Dist) { $candidates += $Dist }
    if ($env:CLAUDE_DIST) { $candidates += $env:CLAUDE_DIST }
    $candidates += 'C:\ClaudeIsolateTest\build\ci-out'
    $candidates += (Join-Path $Root 'dist')
    $seen = @{}
    $ordered = @()
    foreach ($item in $candidates) {
        if (-not $item -or $seen.ContainsKey($item)) { continue }
        $seen[$item] = $true
        $ordered += $item
    }
    foreach ($dir in $ordered) {
        $installer = Join-Path $dir 'Claude-isolate-windows-x64-setup.exe'
        $app = Join-Path $dir 'windows\Claude Isolate\Claude Isolate.exe'
        if ((Test-Path -LiteralPath $installer) -or (Test-Path -LiteralPath $app)) {
            return @{ Dir = $dir; Tried = $ordered }
        }
    }
    return @{ Dir = $null; Tried = $ordered }
}

$resolved = Resolve-Dist
$distDir = $resolved.Dir
$installer = if ($distDir) { Join-Path $distDir 'Claude-isolate-windows-x64-setup.exe' } else { '' }
$app = if ($distDir) { Join-Path $distDir 'windows\Claude Isolate\Claude Isolate.exe' } else { '' }
$core = if ($distDir) { Join-Path $distDir 'windows\Claude Isolate\Claude Isolate Core.exe' } else { '' }
$qemuImg = if ($distDir) { Join-Path $distDir 'windows\Claude Isolate\runtime\qemu\qemu-img.exe' } else { '' }

$missing = @()
if (-not $distDir -or -not (Test-Path -LiteralPath $installer)) { $missing += 'Claude-isolate-windows-x64-setup.exe' }
if (-not $distDir -or -not (Test-Path -LiteralPath $app)) { $missing += 'windows\Claude Isolate\Claude Isolate.exe' }
if (-not $distDir -or -not (Test-Path -LiteralPath $core)) { $missing += 'windows\Claude Isolate\Claude Isolate Core.exe' }
if ($missing.Count -gt 0) {
    $looked = ($resolved.Tried -join '; ')
    Write-Result 'FAIL' 'dist' ("missing {0}. Looked in {1}." -f ($missing -join ', '), $looked)
    Skip-Unchecked 'Windows build output is not in dist' @('install', 'prepare', 'fast-start', 'qt-ui', 'ssh', 'shared', 'clipboard')
    Exit-Summary
}
Write-Result 'PASS' 'dist' ("installer {0}; app {1}" -f $installer, $app)

New-Item -ItemType Directory -Force -Path $Data | Out-Null
$installDir = 'C:\ClaudeIsolateTest\e2e-app'
$installLog = Join-Path $Data 'e2e-install.log'
$installArgs = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /TASKS= /DIR=' + (Quote-Cmd $installDir) + ' /GUESTDATA=' + (Quote-Cmd $Data)
$install = Invoke-Logged $installer $installArgs $installLog 180
if ($install.Code -ne 0) {
    Write-Result 'FAIL' 'install' ("exit {0}. {1}" -f $install.Code, (Get-LogTail $installLog))
    Skip-Unchecked 'clean install failed' @('prepare', 'fast-start', 'qt-ui', 'ssh', 'shared', 'clipboard')
    Exit-Summary
}
$app = Join-Path $installDir 'Claude Isolate.exe'
$core = Join-Path $installDir 'Claude Isolate Core.exe'
$qemuImg = Join-Path $installDir 'runtime\qemu\qemu-img.exe'
if (-not (Test-Path -LiteralPath $app) -or -not (Test-Path -LiteralPath $core)) {
    Write-Result 'FAIL' 'install' "installer finished but $app is missing"
    Skip-Unchecked 'clean install did not produce the app' @('prepare', 'fast-start', 'qt-ui', 'ssh', 'shared', 'clipboard')
    Exit-Summary
}
Write-Result 'PASS' 'install' $installDir
# Prepare reads the guest bridge from the installed bundle. Use the repo copy
# so a bridge fix can be tested before the installer is rebuilt.
$bundledGuest = Join-Path $installDir '_internal\guest\clipboard-bridge.py'
$sourceGuest = Join-Path $Root 'guest\clipboard-bridge.py'
if ((Test-Path -LiteralPath $sourceGuest) -and (Test-Path -LiteralPath $bundledGuest)) {
    Copy-Item -LiteralPath $sourceGuest -Destination $bundledGuest -Force
}

$manifestFile = Join-Path $Root 'guest-image-x86_64.json'
$artifact = $null
if (Test-Path -LiteralPath $manifestFile) {
    $manifest = Get-Content -LiteralPath $manifestFile -Raw | ConvertFrom-Json
    $candidate = Join-Path $Root ('dist\guest\' + $manifest.filename)
    if (Test-Path -LiteralPath $candidate) { $artifact = @{ Path = $candidate; Name = ($manifest.sha256 + '.qcow2') } }
}
if (-not $artifact) {
    Write-Result 'FAIL' 'prepare' 'guest image cache is missing; refusing to download a release that is not published'
    Skip-Unchecked 'guest image was not seeded' @('fast-start', 'qt-ui', 'ssh', 'shared', 'clipboard')
    Exit-Summary
}
$cache = Join-Path $Data 'downloads'
New-Item -ItemType Directory -Force -Path $cache | Out-Null
$seeded = Join-Path $cache $artifact.Name
if (-not (Test-Path -LiteralPath $seeded)) {
    Copy-Item -LiteralPath $artifact.Path -Destination $seeded
}

$config = Read-GuestConfig
if (Test-FastStartSnapshot $qemuImg $config) {
    Write-Result 'PASS' 'prepare' 'skipped; fast-start snapshot claude-fast-start already exists (state.json or qcow2 snapshot)'
} else {
    $prepareLog = Join-Path $Data 'e2e-prepare.log'
    $prepare = Invoke-Logged $core ('prepare --data ' + (Quote-Cmd $Data)) $prepareLog $PrepareTimeoutSec
    if ($prepare.Code -eq 124) {
        Write-Result 'FAIL' 'prepare' ("timed out after {0}s. {1}" -f $PrepareTimeoutSec, (Get-LogTail $prepareLog))
    } elseif ($prepare.Code -ne 0) {
        Write-Result 'FAIL' 'prepare' ("exit {0}. {1}" -f $prepare.Code, (Get-LogTail $prepareLog))
    } else {
        Write-Result 'PASS' 'prepare' 'completed'
    }
}
$config = Read-GuestConfig
$fastSnapshot = Test-FastStartSnapshot $qemuImg $config

$ui = Get-CimInstance Win32_Process -Filter "Name = 'Claude Isolate.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -like ($installDir + '\*') } | Select-Object -First 1
if ($ui) {
    Write-Result 'PASS' 'qt-ui' ("already running pid {0}; user browsers were left alone" -f $ui.ProcessId)
} else {
    Start-Process -FilePath $app -WorkingDirectory (Split-Path $app -Parent) -ArgumentList @('--data', $Data) | Out-Null
    $seen = $null
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        $seen = Get-CimInstance Win32_Process -Filter "Name = 'Claude Isolate.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -like ($installDir + '\*') } | Select-Object -First 1
        if ($seen) { break }
        Start-Sleep -Seconds 1
    }
    if ($seen) {
        Write-Result 'PASS' 'qt-ui' ("started pid {0}; user browsers were left alone" -f $seen.ProcessId)
    } else {
        Write-Result 'FAIL' 'qt-ui' 'Claude Isolate.exe exited without a running Qt process'
    }
}

$config = Read-GuestConfig
$disk = Get-DiskPath $config
$key = Join-Path $Data 'tools\ssh_host_key'
$knownHosts = Join-Path $Data 'e2e-known_hosts'
$ssh = Get-SshExe
$guest = $null
$sshReady = $false
$sshDetail = ''

if (-not (Test-Path -LiteralPath $disk)) {
    $sshDetail = "guest disk is not present at $disk"
} elseif (-not $ssh) {
    $sshDetail = 'OpenSSH ssh.exe is not installed'
} elseif (-not (Test-Path -LiteralPath $key)) {
    $sshDetail = "prepared ed25519 key is missing at $key"
} else {
    $pub = Join-Path $Data 'tools\ssh_host_key.pub'
    $pubLine = if (Test-Path -LiteralPath $pub) { (Get-Content -LiteralPath $pub -TotalCount 1) } else { '' }
    if ($pubLine -notlike 'ssh-ed25519 *') {
        $sshDetail = "public key at $pub is not ssh-ed25519"
    } else {
        $port = Wait-LocalPort 2 $null
        if ($port -ne 'open') {
            $startLog = Join-Path $Data 'e2e-start.log'
            [System.IO.File]::WriteAllText($startLog, '')
            $guest = Start-Logged $core ('start --data ' + (Quote-Cmd $Data) + ' --start-gate') $startLog
            try {
                $guest.StandardInput.WriteLine('GO')
                $guest.StandardInput.Close()
            } catch {
                $sshDetail = "could not release the start gate. $($_.Exception.Message)"
            }
            $port = Wait-LocalPort $BootTimeoutSec $guest
            if ($port -eq 'exited') {
                $sshDetail = "guest launcher exited {0}. {1}" -f $guest.ExitCode, (Get-LogTail $startLog)
            } elseif ($port -eq 'timeout') {
                $sshDetail = "127.0.0.1:$(Get-SshPort) did not open within ${BootTimeoutSec}s. $(Get-LogTail $startLog)"
            }
        }
        if (-not $sshDetail) {
            # QEMU opens the host forward long before the guest sshd answers (TCG boot
            # takes minutes), so keep probing until the boot timeout instead of once.
            $probeDeadline = (Get-Date).AddSeconds($BootTimeoutSec)
            do {
                $probe = Invoke-GuestSsh $ssh $key $knownHosts 'echo claude-isolate-e2e-ok' 30
                if ($probe.Code -eq 0 -and (Test-OutputLine $probe.Text 'claude-isolate-e2e-ok')) { break }
                if ($guest -and $guest.HasExited) { break }
                Start-Sleep -Seconds 10
            } while ((Get-Date) -lt $probeDeadline)
            if ($probe.Code -eq 0 -and (Test-OutputLine $probe.Text 'claude-isolate-e2e-ok')) {
                $sshReady = $true
                Write-Result 'PASS' 'ssh' "ssh -p $(Get-SshPort) -i $key claude@127.0.0.1"
            } else {
                $sshDetail = ("ssh -p {0} failed ({1}). {2}" -f (Get-SshPort), $probe.Code, ([string]$probe.Text).Trim())
            }
        }
    }
}
if (-not $sshReady) {
    Write-Result 'FAIL' 'ssh' $sshDetail
}

$startText = ''
$startLogPath = Join-Path $Data 'e2e-start.log'
if (Test-Path -LiteralPath $startLogPath) { $startText = [IO.File]::ReadAllText($startLogPath) }
if (-not $fastSnapshot) {
    # prepare never saves a snapshot; only 'suspend' does. The save/restore cycle and the
    # clock correction after restore are verified by windows/integration.py (run_fast_start).
    Write-Result 'SKIP' 'fast-start' 'no snapshot yet (created by suspend); save/restore is covered by windows/integration.py'
} elseif ($startText -match ('\u0411\u044b\u0441\u0442\u0440\u044b\u0439 \u0437\u0430\u043f\u0443\u0441\u043a \u043d\u0435 \u0443\u0434\u0430\u043b\u0441\u044f')) {
    Write-Result 'FAIL' 'fast-start' 'restore fell back to a full boot'
} elseif ($sshReady) {
    Write-Result 'PASS' 'fast-start' "snapshot restored and SSH answered on 127.0.0.1:$(Get-SshPort)"
} else {
    Write-Result 'FAIL' 'fast-start' 'snapshot exists but the restored guest did not accept SSH'
}

$shared = Get-SharedDir $config
if (-not $sshReady) {
    Write-Result 'SKIP' 'shared' "guest SSH on 127.0.0.1:$(Get-SshPort) is not up; SFTP sync was not checked"
} else {
    New-Item -ItemType Directory -Force -Path $shared | Out-Null
    $stamp = [guid]::NewGuid().ToString('n')
    $hostName = "e2e-host-$stamp.txt"
    $guestName = "e2e-guest-$stamp.txt"
    $hostToken = "host-$stamp"
    $guestToken = "guest-$stamp"
    $hostFile = Join-Path $shared $hostName
    [System.IO.File]::WriteAllText($hostFile, $hostToken)
    $remoteHost = "cat /home/claude/Shared/$hostName"
    $toGuest = Wait-GuestText $ssh $key $knownHosts $remoteHost $hostToken $SyncTimeoutSec
    $plant = "mkdir -p /home/claude/Shared && printf '%s' '$guestToken' > /home/claude/Shared/$guestName"
    $planted = Invoke-GuestSsh $ssh $key $knownHosts $plant 20
    $guestFile = Join-Path $shared $guestName
    $toHost = $false
    if ($planted.Code -eq 0) {
        $deadline = (Get-Date).AddSeconds($SyncTimeoutSec)
        while ((Get-Date) -lt $deadline) {
            if ((Test-Path -LiteralPath $guestFile) -and ((Get-Content -LiteralPath $guestFile -Raw).Trim() -eq $guestToken)) {
                $toHost = $true
                break
            }
            Start-Sleep -Seconds 2
        }
    }
    Remove-Item -LiteralPath $hostFile -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $guestFile -ErrorAction SilentlyContinue
    Invoke-GuestSsh $ssh $key $knownHosts "rm -f /home/claude/Shared/$hostName /home/claude/Shared/$guestName" 20 | Out-Null
    if ($toGuest -and $toHost) {
        Write-Result 'PASS' 'shared' "both ways $shared <-> /home/claude/Shared over SFTP 127.0.0.1:$(Get-SshPort)"
    } elseif (-not $toGuest) {
        Write-Result 'FAIL' 'shared' 'host file did not appear in /home/claude/Shared via SFTP sync'
    } else {
        Write-Result 'FAIL' 'shared' 'guest file did not appear in the host shared directory via SFTP sync'
    }
}

function Test-ClipboardBridge {
    $current = Read-GuestConfig
    $pipeName = if ($current) { [string]$current.qmp_pipe } else { '' }
    if ($pipeName -notmatch '^claude-isolate-[0-9a-f]{32}$') {
        return 'environment.json has no qmp_pipe'
    }
    $clipPipe = '\\.\pipe\' + $pipeName + '-clipboard'
    $deadline = (Get-Date).AddSeconds(60)
    $pipeUp = $false
    while ((Get-Date) -lt $deadline) {
        if ([IO.Directory]::GetFiles('\\.\pipe\') -contains $clipPipe) { $pipeUp = $true; break }
        Start-Sleep -Seconds 2
    }
    $remote = 'test -e /dev/virtio-ports/claude.clipboard && systemctl is-active claude-clipboard-bridge.service'
    $guestUp = Invoke-GuestSsh $ssh $key $knownHosts $remote 20
    if (-not $pipeUp -or $guestUp.Code -ne 0) {
        return "bridge is not up (pipe $clipPipe present=$pipeUp; guest port/service exit $($guestUp.Code))"
    }
    $saved = [ClipE2E]::GetText()
    $tokenOut = 'e2e-text-host-' + [guid]::NewGuid().ToString('n')
    $tokenIn = 'e2e-text-guest-' + [guid]::NewGuid().ToString('n')
    try {
        [ClipE2E]::SetText($tokenOut)
        $readGuest = "timeout 5 env DISPLAY=:0 XAUTHORITY=/home/claude/.Xauthority xclip -selection clipboard -o"
        if (-not (Wait-GuestText $ssh $key $knownHosts $readGuest $tokenOut $ClipboardTimeoutSec)) {
            return 'text host to guest did not arrive through the clipboard bridge'
        }
        # xclip stays in the foreground while it owns the selection. `timeout`
        # then returns 124 and drops the paste. Leave that process running.
        $writeGuest = "printf '%s' '$tokenIn' | nohup env DISPLAY=:0 XAUTHORITY=/home/claude/.Xauthority xclip -selection clipboard >/dev/null 2>&1 & sleep 1; timeout 5 env DISPLAY=:0 XAUTHORITY=/home/claude/.Xauthority xclip -selection clipboard -o"
        $wrote = Invoke-GuestSsh $ssh $key $knownHosts $writeGuest 20
        if ($wrote.Code -ne 0 -or -not (Test-OutputLine $wrote.Text $tokenIn)) {
            return ("guest xclip could not set text ({0}) {1}" -f $wrote.Code, ([string]$wrote.Text).Trim())
        }
        $got = ''
        $deadline = (Get-Date).AddSeconds($ClipboardTimeoutSec)
        while ((Get-Date) -lt $deadline) {
            $got = [ClipE2E]::GetText()
            if ($got.Trim() -eq $tokenIn) { break }
            Start-Sleep -Seconds 1
        }
        if ($got.Trim() -ne $tokenIn) { return ("text guest to host did not arrive through the clipboard bridge. host saw [{0}]" -f $got.Trim()) }
        $png = [Convert]::FromBase64String($PngBase64)
        [ClipE2E]::SetPng($png)
        $readPng = "timeout 5 env DISPLAY=:0 XAUTHORITY=/home/claude/.Xauthority xclip -selection clipboard -t image/png -o | base64 -w 0"
        $hostToGuest = $false
        $deadline = (Get-Date).AddSeconds($ClipboardTimeoutSec)
        while ((Get-Date) -lt $deadline) {
            $reply = Invoke-GuestSsh $ssh $key $knownHosts $readPng 20
            foreach ($line in ([string]$reply.Text -split '\r?\n')) {
                $payload = $line.Trim()
                if ($reply.Code -ne 0 -or $payload -notmatch '^[A-Za-z0-9+/=]+$') { continue }
                try {
                    if (Test-PngPrefix ([Convert]::FromBase64String($payload)) $png) { $hostToGuest = $true }
                } catch { }
            }
            if ($hostToGuest) { break }
            Start-Sleep -Seconds 2
        }
        if (-not $hostToGuest) { return 'PNG host to guest did not match' }
        $b64 = [Convert]::ToBase64String($png)
        $writePng = "printf '%s' '$b64' | base64 -d | nohup env DISPLAY=:0 XAUTHORITY=/home/claude/.Xauthority xclip -selection clipboard -t image/png >/dev/null 2>&1 & sleep 1; timeout 5 env DISPLAY=:0 XAUTHORITY=/home/claude/.Xauthority xclip -selection clipboard -t image/png -o | base64 -w 0"
        $sent = Invoke-GuestSsh $ssh $key $knownHosts $writePng 20
        if ($sent.Code -ne 0 -or -not (Test-OutputLine $sent.Text $b64)) {
            return ("guest xclip could not set a PNG ({0}) {1}" -f $sent.Code, ([string]$sent.Text).Trim())
        }
        $guestToHost = $false
        $deadline = (Get-Date).AddSeconds($ClipboardTimeoutSec)
        while ((Get-Date) -lt $deadline) {
            if (Test-PngPrefix ([ClipE2E]::GetPng()) $png) { $guestToHost = $true; break }
            Start-Sleep -Seconds 1
        }
        if (-not $guestToHost) { return 'PNG guest to host did not match' }
        return $null
    } finally {
        if ($saved) { [ClipE2E]::SetText($saved) }
    }
}

if (-not $sshReady) {
    Write-Result 'SKIP' 'clipboard' 'guest SSH is not up, so the clipboard bridge was not checked'
} else {
    $clipReason = Test-ClipboardBridge
    if ($clipReason) {
        if ($clipReason -like 'bridge is not up*' -or $clipReason -like 'environment.json*') {
            Write-Result 'SKIP' 'clipboard' $clipReason
        } else {
            Write-Result 'FAIL' 'clipboard' $clipReason
        }
    } else {
        $pipeNote = ''
        $current = Read-GuestConfig
        if ($current -and $current.qmp_pipe) { $pipeNote = ' pipe \\.\pipe\' + $current.qmp_pipe + '-clipboard' }
        Write-Result 'PASS' 'clipboard' ("text and PNG both ways.$pipeNote" )
    }
}

Exit-Summary
