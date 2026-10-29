param([Parameter(Mandatory=$true)][string]$Installer)
$ErrorActionPreference = 'Stop'
$installerPath = (Resolve-Path -LiteralPath $Installer).Path
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('img-switch-smoke-' + [guid]::NewGuid().ToString('N'))
$appDir = Join-Path $testRoot 'Installed App'
$homeDir = Join-Path $testRoot 'Test Home'
$appDataDir = Join-Path $testRoot 'AppData'
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$mockProcess = $null
$previous = @{}
function Invoke-CheckedProcess([string]$FilePath, [string[]]$Arguments) {
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -PassThru
    if (!$process.WaitForExit(120000)) {
        $process.Kill()
        throw "Process timed out: $FilePath"
    }
    if ($process.ExitCode -ne 0) { throw "Process failed with exit code $($process.ExitCode): $FilePath" }
}
foreach ($name in @('APPDATA','FAILOVER_IMAGEGEN_APP_HOME','FAILOVER_IMAGEGEN_TOOL_CONFIG','FAILOVER_IMAGEGEN_APP_SMOKE','FAILOVER_IMAGEGEN_CHILD_OUT','PYTHONHOME','PYTHONPATH','PATH')) {
    $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
New-Item -ItemType Directory -Force -Path $testRoot, $homeDir, $appDataDir | Out-Null
try {
    foreach ($hive in @('HKCU:', 'HKLM:')) {
        if (Test-Path "$hive\Software\Microsoft\Windows\CurrentVersion\Uninstall\{AD6F616E-EE64-467A-8DD7-935BCB23A218}_is1") {
            throw 'An IMG Switch installation already exists; run this smoke test on an isolated machine.'
        }
    }
    $env:APPDATA = $appDataDir
    $env:FAILOVER_IMAGEGEN_APP_HOME = $homeDir
    $portFile = Join-Path $testRoot 'mock-port.txt'
    $mockScript = Join-Path $PSScriptRoot 'mock_server.py'
    $mockProcess = Start-Process -FilePath $pythonPath -ArgumentList @('-B', ('"' + $mockScript + '"'), ('"' + $portFile + '"')) -PassThru -WindowStyle Hidden
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    while (!(Test-Path -LiteralPath $portFile) -and [DateTime]::UtcNow -lt $deadline) {
        if ($mockProcess.HasExited) { throw 'Local mock server exited before becoming ready' }
        Start-Sleep -Milliseconds 100
    }
    if (!(Test-Path -LiteralPath $portFile)) { throw 'Local mock server startup timed out' }
    $port = (Get-Content -LiteralPath $portFile -Raw).Trim()
    if ($port -notmatch '^\d+$') { throw 'Local mock server returned invalid port' }
    $env:PATH = Join-Path $env:SystemRoot 'System32'
    Remove-Item Env:PYTHONHOME, Env:PYTHONPATH -ErrorAction SilentlyContinue
    $env:FAILOVER_IMAGEGEN_TOOL_CONFIG = Join-Path $testRoot 'config.json'
    $config = @{ version = 1; sources = @(@{ name = 'test-source'; base_url = "http://127.0.0.1:$port/v1"; api_key = 'test-key'; model = 'mock-image-1'; enabled = $true }) } | ConvertTo-Json -Compress -Depth 4
    [IO.File]::WriteAllText($env:FAILOVER_IMAGEGEN_TOOL_CONFIG, $config, [Text.UTF8Encoding]::new($false))
    Invoke-CheckedProcess $installerPath @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',('/DIR="' + $appDir + '"'))
    $app = Join-Path $appDir 'IMG-Switch.exe'
    if (!(Test-Path -LiteralPath $app)) { throw 'Installed app missing' }
    $env:FAILOVER_IMAGEGEN_APP_SMOKE = '1'
    Invoke-CheckedProcess $app @('gui')
    Remove-Item Env:FAILOVER_IMAGEGEN_APP_SMOKE
    $env:FAILOVER_IMAGEGEN_CHILD_OUT = Join-Path $testRoot 'gui-child-output.txt'
    Invoke-CheckedProcess $app @('__tool', '--list-sources')
    if (!(Test-Path -LiteralPath $env:FAILOVER_IMAGEGEN_CHILD_OUT) -or
        (Get-Content -LiteralPath $env:FAILOVER_IMAGEGEN_CHILD_OUT -Raw) -notmatch 'test-source') {
        throw 'Bundled GUI engine invocation did not return expected output'
    }
    Remove-Item Env:FAILOVER_IMAGEGEN_CHILD_OUT
    Invoke-CheckedProcess $app @('on')
    $skill = Join-Path $homeDir '.agents\skills\failover-imagegen-tool'
    $engine = Join-Path $skill 'bin\img-switch-engine.exe'
    if (!(Test-Path -LiteralPath $engine)) { throw 'Standalone engine was not installed' }
    $output = & $engine --list-sources 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0 -or $output -notmatch 'test-source') { throw "Standalone engine failed, exit $LASTEXITCODE; output: $output" }
    $outDir = Join-Path $testRoot 'generated'
    $output = & $engine generate --prompt 'test' --size 64x64 --name smoke --outdir $outDir 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "Standalone generation failed, exit $LASTEXITCODE; output: $output" }
    $png = @(Get-ChildItem -LiteralPath $outDir -Filter '*.png')
    $meta = @(Get-ChildItem -LiteralPath $outDir -Filter '*.meta.json')
    if ($png.Count -ne 1 -or $meta.Count -ne 1) { throw 'Standalone generation did not produce one PNG and one metadata file' }
    $signature = [IO.File]::ReadAllBytes($png[0].FullName)
    if ($signature.Length -lt 8 -or [BitConverter]::ToString($signature[0..7]) -ne '89-50-4E-47-0D-0A-1A-0A') { throw 'Generated file is not a PNG' }
    $metadata = Get-Content -LiteralPath $meta[0].FullName -Raw | ConvertFrom-Json
    if ($metadata.source.name -ne 'test-source') { throw 'Metadata does not identify the test source' }
    Invoke-CheckedProcess $app @('off')
    if (Test-Path -LiteralPath $skill) { throw 'Agent removal failed' }
    $uninstaller = Join-Path $appDir 'unins000.exe'
    Invoke-CheckedProcess $uninstaller @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART')
    if (!(Test-Path -LiteralPath $env:FAILOVER_IMAGEGEN_TOOL_CONFIG)) { throw 'Uninstall deleted user config' }
    Write-Output 'Windows installed GUI and standalone engine smoke passed'
} finally {
    if ($null -ne $mockProcess -and !$mockProcess.HasExited) { Stop-Process -Id $mockProcess.Id -Force -ErrorAction SilentlyContinue }
    $uninstaller = Join-Path $appDir 'unins000.exe'
    if (Test-Path -LiteralPath $uninstaller) {
        try { Invoke-CheckedProcess $uninstaller @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART') } catch { Write-Warning "Uninstaller cleanup failed: $_" }
    }
    foreach ($name in $previous.Keys) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}
