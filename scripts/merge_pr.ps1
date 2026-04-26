param(
    [Parameter(Mandatory = $true, Position = 0)]
    [int]$Pr
)

$ErrorActionPreference = "Stop"

function Invoke-GhJson {
    param([string[]]$Args)
    $raw = & gh @Args
    if ($LASTEXITCODE -ne 0) {
        throw "gh $($Args -join ' ') failed"
    }
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $null
    }
    return $raw | ConvertFrom-Json
}

$repo = (& gh repo view --json nameWithOwner -q .nameWithOwner).Trim()
$repoParts = $repo -split "/", 2
$owner = $repoParts[0]
$name = $repoParts[1]
$headBranch = (& gh pr view $Pr --json headRefName -q .headRefName).Trim()

Write-Host ">> fetching review threads on PR #$Pr"
$threadQuery = @"
query(`$owner: String!, `$name: String!, `$pr: Int!) {
  repository(owner: `$owner, name: `$name) {
    pullRequest(number: `$pr) {
      reviewThreads(first: 100) {
        nodes { id isResolved }
      }
    }
  }
}
"@
$threads = & gh api graphql -f "query=$threadQuery" -F "owner=$owner" -F "name=$name" -F "pr=$Pr" --jq ".data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false) | .id"
if ([string]::IsNullOrWhiteSpace($threads)) {
    Write-Host "   no unresolved threads"
} else {
    $threadList = $threads -split "`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    Write-Host ">> resolving $($threadList.Count) thread(s)"
    $resolveMutation = @"
mutation(`$id: ID!) {
  resolveReviewThread(input: {threadId: `$id}) {
    thread { id }
  }
}
"@
    foreach ($threadId in $threadList) {
        & gh api graphql -f "query=$resolveMutation" -F "id=$threadId" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "failed to resolve review thread $threadId"
        }
    }
}

Write-Host ">> waiting for CI on PR #$Pr"
& gh pr checks $Pr --watch --fail-fast
if ($LASTEXITCODE -ne 0) {
    throw "PR checks failed"
}

Write-Host ">> squash-merging PR #$Pr"
& gh pr merge $Pr --squash --delete-branch
if ($LASTEXITCODE -ne 0) {
    Write-Host "   direct merge blocked; enabling/queueing auto-merge"
    $canAdmin = (& gh repo view $repo --json viewerCanAdminister -q .viewerCanAdminister).Trim()
    if ($canAdmin -eq "true") {
        & gh repo edit $repo --enable-auto-merge | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "failed to enable repo auto-merge"
        }
    }

    & gh pr merge $Pr --squash --delete-branch --auto
    if ($LASTEXITCODE -ne 0) {
        throw "failed to queue auto-merge"
    }

    Write-Host ">> waiting for auto-merge to land"
    $merged = $false
    for ($i = 0; $i -lt 60; $i++) {
        $state = (& gh pr view $Pr --json state -q .state).Trim()
        if ($state -eq "MERGED") {
            $merged = $true
            break
        }
        Start-Sleep -Seconds 10
    }
    if (-not $merged) {
        & gh pr view $Pr --json mergeStateStatus,autoMergeRequest,url
        throw "PR #$Pr is queued for auto-merge but has not merged yet"
    }
}

if (-not [string]::IsNullOrWhiteSpace($headBranch)) {
    & git ls-remote --exit-code --heads origin $headBranch | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host ">> deleting merged remote branch $headBranch"
        & git push origin --delete $headBranch | Out-Null
    }
}

& git checkout main
if ($LASTEXITCODE -ne 0) {
    throw "failed to checkout main"
}
& git pull --ff-only origin main
if ($LASTEXITCODE -ne 0) {
    throw "failed to fast-forward local main"
}

Write-Host "done: PR #$Pr merged, local main synced"
