$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

python -m markdown -x tables -x fenced_code docs\architecture.md -f docs\architecture_body.html
if ($LASTEXITCODE -ne 0) {
    throw "Markdown conversion failed."
}

$body = Get-Content -LiteralPath docs\architecture_body.html -Raw
$template = @'
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>FrontierAtlas Architecture</title>
<style>
@page {
  size: A4;
  margin: 1.8cm;
  @bottom-right {
    content: "Page " counter(page) " of " counter(pages);
    font: 8pt Arial, sans-serif;
  }
}
html { font: 11pt Arial, sans-serif; color: #17202a; }
body { margin: 0; line-height: 1.25; }
h1 { font-size: 17pt; margin: 0 0 10pt; }
h2 { font-size: 14pt; margin: 0 0 8pt; }
h3 { font-size: 11pt; margin: 8pt 0 4pt; }
p, ul { margin: 4pt 0; }
ul { padding-left: 18pt; }
li { margin: 2pt 0; }
pre { white-space: pre-wrap; font: 8pt Consolas, monospace; }
code { font: 9pt Consolas, monospace; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #999; padding: 3pt; }
.page-break { break-before: page; page-break-before: always; }
</style>
</head>
<body>
__ARCHITECTURE_BODY__
</body>
</html>
'@
$html = $template.Replace("__ARCHITECTURE_BODY__", $body)
Set-Content -LiteralPath docs\architecture.html -Value $html -Encoding UTF8

$chromeCandidates = @(
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe")
)
$browser = $chromeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $browser) {
    throw "Chrome or Edge was not found. Open docs\architecture.html and print it to PDF."
}

$pdfPath = Join-Path $Root "docs\architecture.pdf"
$profile = Join-Path $env:TEMP ("frontieratlas-pdf-" + [guid]::NewGuid().ToString("N"))
$fileUrl = ([System.Uri](Join-Path $Root "docs\architecture.html")).AbsoluteUri
$arguments = @(
    "--headless=new",
    "--disable-gpu",
    "--no-pdf-header-footer",
    "--user-data-dir=`"$profile`"",
    "--print-to-pdf=`"$pdfPath`"",
    "`"$fileUrl`""
)
try {
    $process = Start-Process -FilePath $browser -ArgumentList $arguments -Wait -PassThru
    if (-not (Test-Path -LiteralPath $pdfPath -PathType Leaf)) {
        throw "Browser PDF printing failed (exit code $($process.ExitCode))."
    }

    $pageCount = python -c "from pypdf import PdfReader; import sys; print(len(PdfReader(sys.argv[1]).pages))" $pdfPath
    if ($LASTEXITCODE -ne 0 -or [int]$pageCount -gt 3) {
        throw "Expected at most 3 PDF pages; got $pageCount."
    }
    Write-Host "Created docs\architecture.pdf ($pageCount pages)."
}
finally {
    if (Test-Path -LiteralPath $profile) {
        Remove-Item -LiteralPath $profile -Recurse -Force
    }
    if (Test-Path -LiteralPath docs\architecture_body.html) {
        Remove-Item -LiteralPath docs\architecture_body.html -Force
    }
}
