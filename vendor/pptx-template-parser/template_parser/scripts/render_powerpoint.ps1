param(
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][string]$Destination,
    [Parameter(Mandatory=$true)][int]$Width,
    [Parameter(Mandatory=$true)][int]$Height
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$existingPowerPoint = @(Get-Process -Name POWERPNT -ErrorAction SilentlyContinue).Count -gt 0
$application = $null
$presentation = $null
$presentations = $null
$slides = $null
$slide = $null
$originalSecurity = $null
try {
    $application = New-Object -ComObject PowerPoint.Application
    $originalSecurity = $application.AutomationSecurity
    $presentations = $application.Presentations
    try {
        $application.AutomationSecurity = 3
        # ReadOnly=true, Untitled=false, WithWindow=false.
        $presentation = $presentations.Open($Source, -1, 0, 0)
    } finally {
        $application.AutomationSecurity = $originalSecurity
    }
    $slides = $presentation.Slides
    $count = $slides.Count
    for ($index = 1; $index -le $count; $index++) {
        try {
            $slide = $slides.Item($index)
            $slide.Export((Join-Path $Destination "s$index.png"), 'PNG', $Width, $Height)
        } finally {
            if ($null -ne $slide) {
                [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($slide)
                $slide = $null
            }
        }
    }
    $metadata = @{name='Microsoft PowerPoint'; version=[string]$application.Version; slide_count=$count}
    [System.IO.File]::WriteAllText((Join-Path $Destination 'engine.json'), ($metadata | ConvertTo-Json), [System.Text.UTF8Encoding]::new($false))
} finally {
    if ($null -ne $slides) { [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($slides) }
    if ($null -ne $presentation) {
        $presentation.Close()
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($presentation)
    }
    # Never quit a pre-existing user session or one containing other documents.
    if ($null -ne $application) {
        if (-not $existingPowerPoint -and $null -ne $presentations -and $presentations.Count -eq 0) { $application.Quit() }
        if ($null -ne $presentations) { [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($presentations) }
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($application)
    }
}
