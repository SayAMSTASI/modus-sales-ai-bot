param(
    [switch]$Foreground
)

$arguments = @{}
if ($Foreground) {
    $arguments.Foreground = $true
}

& (Join-Path $PSScriptRoot 'docker-up.ps1') @arguments
