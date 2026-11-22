$ErrorActionPreference = "Continue"
$script:AuditFailed = $false
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

function Write-Check {
    param(
        [string]$Name,
        [bool]$Passed,
        [string]$Detail
    )

    if ($Passed) {
        Write-Host ("PASS {0}: {1}" -f $Name, $Detail) -ForegroundColor Green
    }
    else {
        Write-Host ("FAIL {0}: {1}" -f $Name, $Detail) -ForegroundColor Red
        $script:AuditFailed = $true
    }
}

function Get-FieldValue {
    param(
        [object]$Record,
        [string]$Path
    )

    $current = $Record
    foreach ($part in $Path.Split(".")) {
        if ($null -eq $current -or $null -eq $current.PSObject.Properties[$part]) {
            return $null
        }
        $current = $current.PSObject.Properties[$part].Value
    }
    return $current
}

function Read-JsonLines {
    param([string]$Path)

    $records = [System.Collections.Generic.List[object]]::new()
    $errors = [System.Collections.Generic.List[string]]::new()
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [pscustomobject]@{ Records = $records; Errors = @("missing file: $Path") }
    }

    $lineNumber = 0
    foreach ($line in Get-Content -LiteralPath $Path) {
        $lineNumber++
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        try {
            $records.Add(($line | ConvertFrom-Json -ErrorAction Stop))
        }
        catch {
            $errors.Add("line ${lineNumber}: $($_.Exception.Message)")
        }
    }
    return [pscustomobject]@{ Records = $records; Errors = $errors }
}

function Test-Duplicates {
    param(
        [object[]]$Records,
        [string]$Field
    )

    $seen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $duplicates = [System.Collections.Generic.List[string]]::new()
    foreach ($record in $Records) {
        $value = Get-FieldValue $record $Field
        if ($null -eq $value -or [string]::IsNullOrWhiteSpace([string]$value)) {
            continue
        }
        if (-not $seen.Add([string]$value)) {
            $duplicates.Add([string]$value)
        }
    }
    return $duplicates
}

function Get-JsonLinesCount {
    param([string]$Path)
    $loaded = Read-JsonLines $Path
    return $loaded.Records.Count
}

$jsonFiles = [ordered]@{
    "Startups" = "output\startups.jsonl"
    "Products" = "output\products.jsonl"
    "Research Papers" = "output\research_papers.jsonl"
    "Jobs" = "output\jobs.jsonl"
    "News" = "output\news.jsonl"
}
$loaded = @{}
foreach ($name in $jsonFiles.Keys) {
    $loaded[$name] = Read-JsonLines $jsonFiles[$name]
}

# 1. Required row counts.
$minimums = @{
    "Startups" = 1000
    "Products" = 1000
    "Research Papers" = 1000
    "Jobs" = 1
    "News" = 1
}
foreach ($name in $minimums.Keys) {
    $count = $loaded[$name].Records.Count
    Write-Check "1 Row count: $name" ($count -ge $minimums[$name]) "$count rows; required >= $($minimums[$name])"
}

$entityLogPath = "output\entity_mapping_log.csv"
$entityRows = @()
$entityLogExists = Test-Path -LiteralPath $entityLogPath -PathType Leaf
if ($entityLogExists) {
    $entityRows = @(Import-Csv -LiteralPath $entityLogPath)
}
Write-Check "1 Row count: Entity Mapping Log" ($entityRows.Count -ge 1000) "$($entityRows.Count) rows; required >= 1000"

# 2. Uniqueness for JSONL identifiers.
$duplicateChecks = @(
    @{ Name = "Startups source.url"; Records = $loaded["Startups"].Records; Field = "source.url" },
    @{ Name = "Products source.url"; Records = $loaded["Products"].Records; Field = "source.url" },
    @{ Name = "Research Papers source.url"; Records = $loaded["Research Papers"].Records; Field = "source.url" },
    @{ Name = "Jobs source.url"; Records = $loaded["Jobs"].Records; Field = "source.url" },
    @{ Name = "News source.url"; Records = $loaded["News"].Records; Field = "source.url" },
    @{ Name = "Products productName"; Records = $loaded["Products"].Records; Field = "content.productName" },
    @{ Name = "Research Papers paper_url"; Records = $loaded["Research Papers"].Records; Field = "content.paper_url" }
)
foreach ($check in $duplicateChecks) {
    $duplicates = @(Test-Duplicates $check.Records $check.Field)
    $examples = ($duplicates | Select-Object -First 5) -join ", "
    Write-Check "2 Uniqueness: $($check.Name)" ($duplicates.Count -eq 0) "$($duplicates.Count) duplicates; examples: $examples"
}

# 3. Research-paper GitHub fields and suspicious zero-star count.
$papers = $loaded["Research Papers"].Records
$invalidGithub = 0
$zeroStars = 0
foreach ($paper in $papers) {
    $githubUrl = Get-FieldValue $paper "content.github_url"
    $stars = Get-FieldValue $paper "content.github_stars"
    if ([string]::IsNullOrWhiteSpace([string]$githubUrl) -or
        ($stars -isnot [int] -and $stars -isnot [long])) {
        $invalidGithub++
    }
    if (($stars -is [int] -or $stars -is [long]) -and $stars -eq 0) {
        $zeroStars++
    }
}
$paperCount = $papers.Count
$zeroPercent = if ($paperCount) { [math]::Round(100.0 * $zeroStars / $paperCount, 2) } else { 0 }
Write-Check "3 Research Papers: GitHub URL and integer stars" ($paperCount -gt 0 -and $invalidGithub -eq 0) "$invalidGithub invalid rows of $paperCount"
Write-Host ("Research Papers with github_stars == 0: {0} ({1}%)" -f $zeroStars, $zeroPercent)
foreach ($paper in $papers) {
    if ((Get-FieldValue $paper "content.github_stars") -eq 0) {
        Write-Host ("  paper_url={0} | github_url={1}" -f
            (Get-FieldValue $paper "content.paper_url"),
            (Get-FieldValue $paper "content.github_url"))
    }
}
if ($zeroPercent -gt 5) {
    Write-Host "WARNING Research Papers zero-star rate exceeds 5%; review the printed rows." -ForegroundColor Yellow
}
Write-Check "3 Research Papers: zero-star rate" $true "$zeroPercent%; review warning threshold is > 5%"

# 4. Freshness of Jobs and News content.date, measured against current UTC.
$nowUtc = [DateTimeOffset]::UtcNow
foreach ($name in @("Jobs", "News")) {
    $dates = [System.Collections.Generic.List[DateTimeOffset]]::new()
    foreach ($record in $loaded[$name].Records) {
        $rawDate = Get-FieldValue $record "content.date"
        if ($null -eq $rawDate) {
            continue
        }
        $parsedDate = [DateTimeOffset]::MinValue
        $validDate = [DateTimeOffset]::TryParse(
            [string]$rawDate,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::AssumeUniversal,
            [ref]$parsedDate
        )
        if ($validDate) {
            $dates.Add($parsedDate.ToUniversalTime())
        }
    }

    if ($dates.Count -eq 0) {
        Write-Check "4 Freshness: $name" $false "no parseable content.date values"
    }
    else {
        $oldest = ($dates | Sort-Object)[0]
        $ageHours = [math]::Round(($nowUtc - $oldest).TotalHours, 2)
        Write-Check "4 Freshness: $name" ($ageHours -ge 0 -and $ageHours -le 24) "oldest=$($oldest.ToString('o')); age=${ageHours}h"
    }
}

# 5. Required fields and ISO-8601 collectedAt across all JSONL records.
$requiredFields = @("schemaVersion", "recordType", "source.name", "source.url", "collectedAt")
$schemaFailures = 0
$schemaErrors = [System.Collections.Generic.List[string]]::new()
foreach ($name in $jsonFiles.Keys) {
    foreach ($errorMessage in $loaded[$name].Errors) {
        $schemaFailures++
        if ($schemaErrors.Count -lt 10) {
            $schemaErrors.Add("$name parse error: $errorMessage")
        }
    }
    $recordIndex = 0
    foreach ($record in $loaded[$name].Records) {
        $recordIndex++
        foreach ($field in $requiredFields) {
            $value = Get-FieldValue $record $field
            if ($null -eq $value -or [string]::IsNullOrWhiteSpace([string]$value)) {
                $schemaFailures++
                if ($schemaErrors.Count -lt 10) {
                    $schemaErrors.Add("$name record $recordIndex missing $field")
                }
            }
        }

        $collectedAt = Get-FieldValue $record "collectedAt"
        if ($null -ne $collectedAt) {
            $parsedAt = [DateTimeOffset]::MinValue
            $isoShape = [string]$collectedAt -match '^\d{4}-\d{2}-\d{2}T.+(Z|[+-]\d{2}:\d{2})$'
            $validAt = [DateTimeOffset]::TryParse(
                [string]$collectedAt,
                [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::RoundtripKind,
                [ref]$parsedAt
            )
            if (-not ($isoShape -and $validAt)) {
                $schemaFailures++
                if ($schemaErrors.Count -lt 10) {
                    $schemaErrors.Add("$name record $recordIndex has non-ISO collectedAt")
                }
            }
        }
    }
}
Write-Check "5 Required schema fields and ISO collectedAt" ($schemaFailures -eq 0) "$schemaFailures schema/parse failures"
foreach ($message in $schemaErrors) {
    Write-Host "  $message"
}

# 6. Product pricing coverage and manual-review samples.
$products = $loaded["Products"].Records
$pricingNonNull = 0
$pricingNull = 0
$pricedProducts = [System.Collections.Generic.List[object]]::new()
foreach ($product in $products) {
    $pricing = Get-FieldValue $product "content.pricingModel"
    if ($null -eq $pricing -or [string]::IsNullOrWhiteSpace([string]$pricing)) {
        $pricingNull++
    }
    else {
        $pricingNonNull++
        $pricedProducts.Add($product)
    }
}
Write-Host ("Products pricingModel: non-null={0}; null={1}" -f $pricingNonNull, $pricingNull)
Write-Host "Pricing samples (up to 10 random non-null rows):"
$sampleCount = [math]::Min(10, $pricedProducts.Count)
$missingPricingSampleFields = 0
if ($sampleCount -gt 0) {
    Get-Random -InputObject $pricedProducts.ToArray() -Count $sampleCount | ForEach-Object {
        $pricingUrl = Get-FieldValue $_ "content.pricingSourceUrl"
        if ($null -eq $pricingUrl) { $pricingUrl = Get-FieldValue $_ "pricingSourceUrl" }
        $evidence = Get-FieldValue $_ "content.pricingEvidence"
        if ($null -eq $evidence) { $evidence = Get-FieldValue $_ "pricingEvidence" }
        if ($evidence -is [array]) { $evidence = $evidence | Select-Object -First 1 }
        Write-Host ("  pricingSourceUrl={0} | firstEvidence={1}" -f $pricingUrl, $evidence)
        if ([string]::IsNullOrWhiteSpace([string]$pricingUrl) -or
            [string]::IsNullOrWhiteSpace([string]$evidence)) {
            $missingPricingSampleFields++
        }
    }
}
else {
    Write-Host "  no non-null pricingModel rows available to sample"
}
$pricingSamplesPassed = $products.Count -gt 0 -and $missingPricingSampleFields -eq 0
$pricingSampleDetail = if ($sampleCount -eq 0) {
    "$pricingNonNull non-null, $pricingNull null; no verified non-null rows to sample"
} else {
    "$pricingNonNull non-null, $pricingNull null; printed $sampleCount samples; $missingPricingSampleFields missing URL/evidence"
}
Write-Check "6 Product pricing fields audited" $pricingSamplesPassed $pricingSampleDetail

# 7. Entity mapping method counts and every ambiguous-review row.
$ambiguousRows = @()
if ($entityLogExists) {
    $entityRows | Group-Object -Property method | Sort-Object Name | ForEach-Object {
        Write-Host ("Entity method {0}: {1}" -f $_.Name, $_.Count)
    }
    $ambiguousRows = @($entityRows | Where-Object { $_.method -eq "ambiguous_review" })
}
Write-Host ("Ambiguous review rows: {0}" -f $ambiguousRows.Count)
foreach ($row in $ambiguousRows) {
    Write-Host ("  raw={0} | canonical={1} | score={2} | source_url={3}" -f $row.raw, $row.canonical, $row.score, $row.source_url)
}
Write-Check "7 Entity mapping log audited" $entityLogExists "methods counted; listed $($ambiguousRows.Count) ambiguous_review rows"

# 8. Tracked secret/local artifact checks. Matched file names only are printed.
$gitFiles = @(& git ls-files 2>$null)
$gitLsFilesExit = $LASTEXITCODE
$trackedForbidden = @($gitFiles | Where-Object {
    $path = ([string]$_).Replace("\", "/")
    $leaf = $path.Split("/")[-1]
    $path -eq ".env" -or
    $leaf -eq "service_account.json" -or
    $leaf -eq "seen.db" -or
    $path.StartsWith("output/", [StringComparison]::OrdinalIgnoreCase)
})
$secretMarkers = @("AI" + "za", "g" + "sk_", "g" + "hp_", "s" + "k" + "-")
$secretPattern = "(" + (($secretMarkers | ForEach-Object { [regex]::Escape($_) }) -join "|") + ")"
$gitGrepFiles = @(& git grep -I -l -E $secretPattern -- 2>$null)
$gitGrepExit = $LASTEXITCODE
$secretSearchPassed = ($gitGrepExit -eq 1 -or ($gitGrepExit -eq 0 -and $gitGrepFiles.Count -eq 0))
$forbiddenPassed = ($gitLsFilesExit -eq 0 -and $trackedForbidden.Count -eq 0)
Write-Check "8 Tracked private artifacts" $forbiddenPassed ("{0} forbidden tracked path(s): {1}" -f $trackedForbidden.Count, ($trackedForbidden -join ", "))
Write-Check "8 Tracked secret-pattern scan" $secretSearchPassed ("matched file paths only: {0}" -f ($gitGrepFiles -join ", "))

# 9. Execute the test suite with the active Python environment.
Write-Host "Running python -m pytest -q ..."
& python -m pytest -q
$pytestExit = $LASTEXITCODE
Write-Check "9 Pytest" ($pytestExit -eq 0) "python -m pytest -q exit code $pytestExit"

# 10. Submission deliverables and PDF page limit.
$readmeExists = Test-Path -LiteralPath "README.md" -PathType Leaf
$sourceExists = Test-Path -LiteralPath "src" -PathType Container
$testsExists = Test-Path -LiteralPath "tests" -PathType Container
$pdfExists = Test-Path -LiteralPath "docs\architecture.pdf" -PathType Leaf
$pdfPages = 0
$pdfValid = $false
if ($pdfExists) {
    $pageOutput = & python -c "from pypdf import PdfReader; import sys; print(len(PdfReader(sys.argv[1]).pages))" "docs\architecture.pdf" 2>$null
    if ($LASTEXITCODE -eq 0 -and "$pageOutput" -match '^\d+$') {
        $pdfPages = [int]$pageOutput
        $pdfValid = $pdfPages -le 3
    }
}
Write-Check "10 Deliverables: README.md" $readmeExists "README.md present"
Write-Check "10 Deliverables: src/" $sourceExists "src/ present"
Write-Check "10 Deliverables: tests/" $testsExists "tests/ present"
Write-Check "10 Deliverables: architecture.pdf" ($pdfExists -and $pdfValid) ("present={0}; pages={1}; required <= 3" -f $pdfExists, $pdfPages)

if ($script:AuditFailed) {
    Write-Host "AUDIT FAILED: one or more submission gates need attention." -ForegroundColor Red
    exit 1
}

Write-Host "AUDIT PASSED: all submission gates passed." -ForegroundColor Green
exit 0
