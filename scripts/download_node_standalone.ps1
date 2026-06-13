# Downloads the official Node.js LTS runtime for Windows (x64), verifies it
# against the published SHASUMS256, and prunes it to just node.exe.
# Run from repo root: powershell -ExecutionPolicy Bypass -File scripts/download_node_standalone.ps1

$ErrorActionPreference = "Stop"

$NodeVersion = "v24.16.0"   # Node.js LTS ("Krypton"); keep current with nodejs.org LTS
$Dest = "node-standalone"
$Name = "node-${NodeVersion}-win-x64"
$Filename = "${Name}.zip"
$BaseUrl = "https://nodejs.org/dist/${NodeVersion}"
$Url = "${BaseUrl}/${Filename}"
$ShasumsUrl = "${BaseUrl}/SHASUMS256.txt"

Write-Host "=== Downloading Node.js ${NodeVersion} for win-x64 ==="
Write-Host "URL: ${Url}"

if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
if (Test-Path "_node_tmp") { Remove-Item -Recurse -Force "_node_tmp" }
New-Item -ItemType Directory -Path "_node_tmp" | Out-Null

$ArchivePath = "_node_tmp\${Filename}"
Write-Host "Downloading..."
Invoke-WebRequest -Uri $Url -OutFile $ArchivePath -UseBasicParsing

Write-Host "Verifying SHASUMS256..."
$Shasums = (Invoke-WebRequest -Uri $ShasumsUrl -UseBasicParsing).Content
$Expected = ($Shasums -split "`n" | Where-Object { $_ -match [Regex]::Escape($Filename) } | Select-Object -First 1).Split(" ")[0]
if (-not $Expected) { throw "Could not find ${Filename} in ${ShasumsUrl}" }
$Actual = (Get-FileHash -Algorithm SHA256 $ArchivePath).Hash.ToLower()
if ($Expected.ToLower() -ne $Actual) {
    throw "SHASUMS256 mismatch for ${Filename}: expected ${Expected}, got ${Actual}"
}
Write-Host "Checksum OK: ${Actual}"

Write-Host "Extracting..."
tar -xf $ArchivePath -C "_node_tmp"

# Keep only node.exe — drop npm/npx/corepack and node_modules.
New-Item -ItemType Directory -Path $Dest | Out-Null
Copy-Item "_node_tmp\${Name}\node.exe" "${Dest}\node.exe"
if (Test-Path "_node_tmp\${Name}\LICENSE") { Copy-Item "_node_tmp\${Name}\LICENSE" "${Dest}\LICENSE" }
Remove-Item -Recurse -Force "_node_tmp"

echo ""
Write-Host "=== Done ==="
Write-Host "Node: ${Dest}\node.exe"
& "${Dest}\node.exe" --version
