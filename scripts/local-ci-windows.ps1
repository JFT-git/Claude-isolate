# Local replay of the GitHub Windows CI job.
# Requires the prepared guest manifest at the repo root and the qcow2 in dist/guest.
param(
    [string]$GitCommit = ''
)
$ErrorActionPreference = 'Stop'
$Root = if ($env:CLAUDE_ISOLATE_ROOT) { $env:CLAUDE_ISOLATE_ROOT } else { Split-Path $PSScriptRoot -Parent }
Set-Location $Root
New-Item -ItemType Directory -Force -Path 'C:\ClaudeIsolateTest\agent' | Out-Null
Start-Transcript -Path 'C:\ClaudeIsolateTest\agent\local-ci.log' -Force | Out-Null
$env:PYTHONUTF8 = '1'
$env:CLAUDE_DIST = 'C:\ClaudeIsolateTest\build\ci-out'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
if ($GitCommit) { $env:CLAUDE_GIT_COMMIT = $GitCommit }
$Summary = New-Object System.Collections.Generic.List[string]
$Tools = 'C:\ClaudeIsolateTest\tools'

function Step($Name, [scriptblock]$Action) {
    Write-Host "== $Name =="
    try {
        $global:LASTEXITCODE = 0
        & $Action
        if ($global:LASTEXITCODE -ne 0) { throw "exit code $global:LASTEXITCODE" }
        $script:Summary.Add("PASS $Name")
        Write-Host "PASS $Name"
    } catch {
        $script:Summary.Add("FAIL $Name $($_.Exception.Message)")
        Write-Host "FAIL $Name $($_.Exception.Message)"
        Write-Host ''
        Write-Host '=== LOCAL CI SUMMARY ==='
        $script:Summary | ForEach-Object { Write-Host $_ }
        Write-Host 'LOCAL CI FAIL'
        if ($script:RestartSearch) { Start-Service WSearch -ErrorAction SilentlyContinue }
        try { Stop-Transcript | Out-Null } catch { }
        exit 1
    }
}

function Free-Memory {
    $os = Get-CimInstance Win32_OperatingSystem
    $free = [int]($os.FreePhysicalMemory / 1024)
    Write-Host ("Free RAM: {0} MB" -f $free)
    return $free
}

function Ensure-BuildMemory {
    # Never close Chrome, Firefox, Edge, or other user applications.
    # Only our own QEMU/Python/build processes may be stopped, and only by name below.
    Free-Memory | Out-Null
}

Step 'Python 3.12' {
    $python = @(
        (Join-Path $Tools 'Python312\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:ProgramFiles 'Python312\python.exe')
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $python) {
        $python = Join-Path $Tools 'Python312\python.exe'
        New-Item -ItemType Directory -Force -Path $Tools | Out-Null
        $installer = Join-Path $Tools 'python-3.12-amd64.exe'
        $versions = @('3.12.12', '3.12.11', '3.12.10', '3.12.9')
        $downloaded = $false
        foreach ($version in $versions) {
            $url = "https://www.python.org/ftp/python/$version/python-$version-amd64.exe"
            Write-Host "Trying $url"
            & curl.exe -fsSL -o $installer $url
            if ($LASTEXITCODE -eq 0 -and (Test-Path $installer) -and (Get-Item $installer).Length -gt 1000000) {
                $downloaded = $true
                break
            }
            Remove-Item $installer -ErrorAction SilentlyContinue
        }
        if (-not $downloaded) { throw 'Could not download Python 3.12' }
        $target = Join-Path $Tools 'Python312'
        $proc = Start-Process -FilePath $installer -ArgumentList @(
            '/quiet', 'InstallAllUsers=0', 'PrependPath=0', 'Include_pip=1',
            'Include_test=0', 'Include_launcher=0', 'Shortcuts=0', "TargetDir=$target"
        ) -Wait -PassThru
        if ($proc.ExitCode -ne 0) { throw "Python installer exit $($proc.ExitCode)" }
    }
    if (-not (Test-Path $python)) {
        # An existing per-user 3.12 install makes the installer ignore TargetDir.
        $fallback = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
        if (Test-Path $fallback) { $python = $fallback }
    }
    if (-not (Test-Path $python)) { throw "Python not found at $python" }
    $script:ResolvedPython = $python
    $env:PATH = "$(Split-Path $python);$(Split-Path $python)\Scripts;" + $env:PATH
    & $python -c "import sys; assert sys.version_info[:2]==(3,12), sys.version"
}

Step '7-Zip' {
    $seven = @(
        (Join-Path $Tools '7zip\7z.exe'),
        "${env:ProgramFiles}\7-Zip\7z.exe"
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $seven) {
        $seven = Join-Path $Tools '7zip\7z.exe'
        New-Item -ItemType Directory -Force -Path (Join-Path $Tools '7zip') | Out-Null
        $extraNames = @('7z2600-extra.7z', '7z2501-extra.7z', '7z2409-extra.7z')
        $zr = Join-Path $Tools '7zr.exe'
        & curl.exe -fsSL -o $zr 'https://www.7-zip.org/a/7zr.exe'
        if ($LASTEXITCODE -ne 0) { throw 'Could not download 7zr.exe' }
        $got = $false
        foreach ($name in $extraNames) {
            $archive = Join-Path $Tools $name
            & curl.exe -fsSL -o $archive "https://www.7-zip.org/a/$name"
            if ($LASTEXITCODE -eq 0 -and (Test-Path $archive) -and (Get-Item $archive).Length -gt 100000) {
                & $zr x "-o$(Join-Path $Tools '7zip')" -y $archive | Out-Null
                $found = Get-ChildItem (Join-Path $Tools '7zip') -Recurse -Filter '7za.exe' | Select-Object -First 1
                if ($found) {
                    Copy-Item $found.FullName $seven -Force
                    $got = $true
                    break
                }
            }
        }
        if (-not $got) { throw 'Could not install portable 7-Zip' }
    }
    $env:PATH = "$(Split-Path $seven);" + $env:PATH
}

Step 'Inno Setup' {
    $installed = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $installed)) { $installed = "$env:ProgramFiles\Inno Setup 6\ISCC.exe" }
    $iscc = if (Test-Path $installed) { Get-Item $installed } else { Get-ChildItem $Tools -Recurse -Filter 'ISCC.exe' -ErrorAction SilentlyContinue | Select-Object -First 1 }
    if (-not $iscc) {
        $setup = Join-Path $Tools 'innosetup.exe'
        & curl.exe -fsSL -o $setup 'https://jrsoftware.org/download.php/is.exe'
        if ($LASTEXITCODE -ne 0) { throw 'Could not download Inno Setup' }
        $dest = Join-Path $Tools 'InnoSetup'
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        & 7z.exe x "-o$dest" -y $setup | Out-Null
        $iscc = Get-ChildItem $dest -Recurse -Filter 'ISCC.exe' | Select-Object -First 1
    }
    if (-not $iscc) { throw 'ISCC.exe not found after extracting Inno Setup' }
    $env:PATH = "$($iscc.DirectoryName);" + $env:PATH
    Write-Host "ISCC $($iscc.FullName)"
}

$Python = $script:ResolvedPython

Step 'Python deps' {
    & $Python -m pip install --require-hashes -r requirements-windows.txt
}

Step 'Unit tests' {
    & $Python -m unittest discover -s tests -v
}

Step 'Compileall' {
    & $Python -m compileall -q environment.py network_guard.py network_transport.py tls_trust.py route_guard.py ubuntu_image.py release_image.py session_lock.py scripts macos windows
}

Step 'Windows unit tests' {
    & $Python -m unittest discover -s tests -p test_windows.py -v
}

Step 'PyInstaller build and smoke' {
    if (-not (Test-Path 'guest-image-x86_64.json')) {
        throw 'Missing guest-image-x86_64.json in the repo root'
    }
    Ensure-BuildMemory
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'qemu-system-x86_64.exe' -or $_.Name -eq 'Claude Isolate Core.exe' -or
        ($_.Name -eq 'python.exe' -and $_.CommandLine -like '*integration.py*')
    } | Where-Object { $_.CommandLine -like '*ClaudeIsolateTest*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $search = Get-Service WSearch -ErrorAction SilentlyContinue
    if ($search -and $search.Status -eq 'Running') {
        Stop-Service WSearch -Force -ErrorAction SilentlyContinue
        $script:RestartSearch = $true
        Write-Host 'Paused Windows Search so it does not lock the build directory'
    }
    if (Test-Path -LiteralPath $env:CLAUDE_DIST) {
        Remove-Item -LiteralPath $env:CLAUDE_DIST -Recurse -Force -ErrorAction SilentlyContinue
    }
    New-Item -ItemType Directory -Force -Path $env:CLAUDE_DIST | Out-Null
    & $Python windows/build.py
}

Step 'Windows integration' {
    if (-not (Test-Path 'dist\guest\Claude-isolate-guest-x86_64.qcow2')) {
        throw 'Missing dist/guest prepared image'
    }
    Ensure-BuildMemory
    # prepare_disk repairs the backup GPT and lets BOOT/UEFI mount late.
    # 2048 MB keeps two vCPUs, but only when the host has room left with
    # the user's browsers still open. Otherwise match the 1024 MB CI leg.
    # TCG on this laptop is slower than GitHub's 2100s budget.
    if (-not $env:CLAUDE_BOOT_TIMEOUT) { $env:CLAUDE_BOOT_TIMEOUT = '5400' }
    $free = Free-Memory
    $memory = if ($free -ge 4200) { 2048 } else { 1024 }
    Write-Host "Integration guest memory: $memory MB"
    # Tee everything (stdout+stderr) into the polled transcript.
    $ErrorActionPreference = 'Continue'
    & $Python windows/integration.py --desktop --memory-mb $memory 2>&1 |
        ForEach-Object { Write-Host ([string]$_) }
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        foreach ($name in 'boot-1','boot-2') {
            $dir = Join-Path 'reports\windows' $name
            foreach ($file in 'timings.json','network-state.json') {
                $path = Join-Path $dir $file
                if (Test-Path -LiteralPath $path) { Write-Host "--- $path"; Get-Content -LiteralPath $path }
            }
            $boot = Join-Path $dir 'boot.log'
            if (Test-Path -LiteralPath $boot) {
                Write-Host "--- $boot (WINDOWS-INTEGRATION/ISOLATION lines)"
                Select-String -LiteralPath $boot -Pattern 'WINDOWS-INTEGRATION|CLAUDE-ISOLATION|Traceback|Error' |
                    Select-Object -Last 40 | ForEach-Object { Write-Host $_.Line }
            }
        }
    }
    $global:LASTEXITCODE = $code
}

Write-Host ''
Write-Host '=== LOCAL CI SUMMARY ==='
$Summary | ForEach-Object { Write-Host $_ }
if ($script:RestartSearch) { Start-Service WSearch -ErrorAction SilentlyContinue }
if ($Summary | Where-Object { $_ -like 'FAIL *' }) {
    Write-Host 'LOCAL CI FAIL'
    Stop-Transcript | Out-Null
    exit 1
}
Write-Host 'LOCAL CI PASS'
Stop-Transcript | Out-Null
exit 0
