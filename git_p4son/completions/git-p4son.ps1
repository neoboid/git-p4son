# PowerShell completion for git-p4son
# Also handles "git p4son" subcommand completion.
#
# Delegates to "git-p4son complete" for candidate generation.
#
# Installation (pip install):
#   Dot-source in your PowerShell profile ($PROFILE):
#     . $(git-p4son completion powershell)
#
# Installation (from repo):
#   Dot-source in your PowerShell profile ($PROFILE):
#     . /path/to/git-p4son/git_p4son/completions/git-p4son.ps1

function _GitP4sonCompleter {
    param($wordToComplete, $commandAst, $cursorPosition, $p4sonArgs)

    # Ensure the word being completed is the last element
    if ($p4sonArgs.Count -eq 0 -or $p4sonArgs[-1] -ne $wordToComplete) {
        $p4sonArgs += $wordToComplete
    }

    # Call git-p4son complete (-- prevents words from being parsed as flags)
    try {
        $output = & git-p4son complete -- @p4sonArgs 2>$null
    } catch {
        return @()
    }
    if (-not $output) { return @() }

    $completions = @()
    foreach ($line in $output) {
        # Handle special directives
        if ($line -eq '__branch__') {
            try {
                $branches = & git branch --format='%(refname:short)' 2>$null
                foreach ($b in $branches) {
                    $b = $b.Trim()
                    if ($b -like "$wordToComplete*") {
                        $completions += [System.Management.Automation.CompletionResult]::new(
                            $b, $b, 'ParameterValue', "Branch: $b")
                    }
                }
            } catch {}
            continue
        }

        $parts = $line -split "`t", 2
        $name = $parts[0]
        $desc = if ($parts.Count -gt 1) { $parts[1] } else { $name }
        $type = if ($name -match '^-') { 'ParameterName' } else { 'ParameterValue' }
        $completions += [System.Management.Automation.CompletionResult]::new(
            $name, $name, $type, $desc)
    }

    return $completions
}

# Register completer for "git-p4son" (standalone invocation)
Register-ArgumentCompleter -CommandName git-p4son -Native -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)

    # Use CommandElements for proper token parsing (handles quoted strings)
    $elements = @($commandAst.CommandElements | ForEach-Object { $_.ToString() })
    # Skip the first element ("git-p4son"), rest are the p4son args
    $p4sonArgs = @($elements | Select-Object -Skip 1)

    _GitP4sonCompleter $wordToComplete $commandAst $cursorPosition $p4sonArgs
}

# Register completer for "git" to handle "git p4son ..." subcommand.
# For non-p4son commands, delegate to posh-git's Expand-GitCommand if available.
Register-ArgumentCompleter -CommandName git -Native -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)

    # Use CommandElements for proper token parsing (handles quoted strings)
    $elements = @($commandAst.CommandElements | ForEach-Object { $_.ToString() })
    # Check if this is a "git p4son" invocation
    if ($elements.Count -ge 2 -and $elements[1] -eq 'p4son') {
        # Skip "git" and "p4son", rest are the p4son args
        $p4sonArgs = @($elements | Select-Object -Skip 2)
        return _GitP4sonCompleter $wordToComplete $commandAst $cursorPosition $p4sonArgs
    }

    # Not a p4son subcommand — delegate to posh-git if available
    if (Get-Command Expand-GitCommand -ErrorAction SilentlyContinue) {
        $padLength = $cursorPosition - $commandAst.Extent.StartOffset
        $textToComplete = $commandAst.ToString().PadRight($padLength, ' ').Substring(0, $padLength)
        return Expand-GitCommand $textToComplete
    }
}
