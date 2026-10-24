<#
.SYNOPSIS
  Deploy the committed code to the Unraid server and restart the container.

.DESCRIPTION
  Uploads `git archive HEAD` (so uncommitted changes are NOT deployed) plus your .env, then runs
  unraid/install.sh on the server, which builds the image there and recreates the container.

.EXAMPLE
  ./deploy.ps1                 # deploy
  ./deploy.ps1 -CopyDb         # also replace the server's database with data/watcher.db
  ./deploy.ps1 -Logs           # just follow the container's logs
#>
param(
    [string]$Server = "root@192.168.0.2",
    [switch]$CopyDb,
    [switch]$Logs
)
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
Set-Location $PSScriptRoot

$Name = "website-watcher"
$AppData = "/mnt/user/appdata/$Name"

if ($Logs) { ssh $Server "docker logs -f --tail 50 $Name"; return }

if (git status --porcelain) {
    Write-Warning "You have uncommitted changes; only the last commit ($(git rev-parse --short HEAD)) is deployed."
}

$tar = Join-Path ([IO.Path]::GetTempPath()) "$Name.tar"
git archive --format=tar -o $tar HEAD
try {
    Write-Host "== Uploading $(git log -1 --format='%h %s')"
    ssh $Server "mkdir -p $AppData/data && rm -rf $AppData/src && mkdir $AppData/src"
    scp -q $tar "${Server}:$AppData/src.tar"
    ssh $Server "tar -xf $AppData/src.tar -C $AppData/src && rm $AppData/src.tar"
} finally {
    Remove-Item $tar -ErrorAction SilentlyContinue
}

if (Test-Path .env) { scp -q .env "${Server}:$AppData/.env" }
else { Write-Warning "No .env here; Messenger stays off unless $AppData/.env already exists on the server." }

if ($CopyDb) {
    Write-Host "== Replacing the server's database with data/watcher.db"
    ssh $Server "docker stop $Name 2>/dev/null; true"
    scp -q data/watcher.db "${Server}:$AppData/data/watcher.db"
}

ssh $Server "bash $AppData/src/unraid/install.sh"
Write-Host "== Done: http://$($Server.Split('@')[-1]):8000"
