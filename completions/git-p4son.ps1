# PowerShell completion for git-p4son
# Also handles "git p4son" subcommand completion.
#
# Installation:
#   Dot-source this file in your PowerShell profile ($PROFILE):
#     . /path/to/git-p4son/completions/git-p4son.ps1

function _GitP4sonGetAliases {
    try {
        $root = git rev-parse --show-toplevel 2>$null
        if (-not $root) { return @() }
        $aliasDir = Join-Path $root ".git-p4son" "changelists"
        if (Test-Path $aliasDir) {
            return @(Get-ChildItem -Path $aliasDir -File | ForEach-Object { $_.Name })
        }
    } catch {}
    return @()
}

function _GitP4sonCompleter {
    param($wordToComplete, $commandAst, $cursorPosition, $p4sonArgs)

    # $p4sonArgs contains the arguments after "p4son" (or after "git-p4son")
    $command = $null
    $subcommand = $null
    $positionalIndex = 0

    # Parse out the command and track positional args
    $argsAfterCommand = @()
    foreach ($arg in $p4sonArgs) {
        if (-not $command -and $arg -notmatch '^-') {
            $command = $arg
        } elseif ($command) {
            $argsAfterCommand += $arg
        }
    }

    # For alias subcommand, find the subcommand
    if ($command -eq 'alias') {
        foreach ($arg in $argsAfterCommand) {
            if ($arg -notmatch '^-') {
                $subcommand = $arg
                break
            }
        }
    }

    # Count positional args after command (excluding the word being completed and flags)
    $positionals = @()
    foreach ($arg in $argsAfterCommand) {
        if ($arg -ne $wordToComplete -and $arg -notmatch '^-') {
            $positionals += $arg
        }
    }

    $completions = @()

    if (-not $command -or ($command -eq $wordToComplete -and $positionals.Count -eq 0)) {
        # Complete command names
        $commands = @(
            @{ Name = 'sync';         Desc = 'Sync git repo with Perforce workspace' }
            @{ Name = 'new';          Desc = 'Create a new changelist' }
            @{ Name = 'update';       Desc = 'Update an existing changelist' }
            @{ Name = 'list-changes'; Desc = 'List commit subjects since base branch' }
            @{ Name = 'alias';        Desc = 'Manage changelist aliases' }
        )
        foreach ($cmd in $commands) {
            if ($cmd.Name -like "$wordToComplete*") {
                $completions += [System.Management.Automation.CompletionResult]::new(
                    $cmd.Name, $cmd.Name, 'ParameterValue', $cmd.Desc)
            }
        }
        # Global flags
        if ($wordToComplete -match '^-') {
            $globalFlags = @('--version')
            foreach ($f in $globalFlags) {
                if ($f -like "$wordToComplete*") {
                    $completions += [System.Management.Automation.CompletionResult]::new(
                        $f, $f, 'ParameterName', $f)
                }
            }
        }
        return $completions
    }

    # Complete flags and positional args per command
    switch ($command) {
        'sync' {
            if ($wordToComplete -match '^-') {
                $flags = @('-f', '--force')
                foreach ($f in $flags) {
                    if ($f -like "$wordToComplete*") {
                        $completions += [System.Management.Automation.CompletionResult]::new(
                            $f, $f, 'ParameterName', $f)
                    }
                }
            } else {
                # Changelist: special values + aliases
                $specials = @(
                    @{ Name = 'latest';      Desc = 'Sync to latest changelist' }
                    @{ Name = 'last-synced'; Desc = 'Re-sync last synced changelist' }
                )
                foreach ($s in $specials) {
                    if ($s.Name -like "$wordToComplete*") {
                        $completions += [System.Management.Automation.CompletionResult]::new(
                            $s.Name, $s.Name, 'ParameterValue', $s.Desc)
                    }
                }
                foreach ($a in (_GitP4sonGetAliases)) {
                    if ($a -like "$wordToComplete*") {
                        $completions += [System.Management.Automation.CompletionResult]::new(
                            $a, $a, 'ParameterValue', "Alias: $a")
                    }
                }
            }
        }
        'new' {
            if ($wordToComplete -match '^-') {
                $flags = @('-m', '--message', '-b', '--base-branch', '-f', '--force',
                           '-n', '--dry-run', '--no-edit', '--shelve', '--review',
                           '-s', '--sleep')
                foreach ($f in $flags) {
                    if ($f -like "$wordToComplete*") {
                        $completions += [System.Management.Automation.CompletionResult]::new(
                            $f, $f, 'ParameterName', $f)
                    }
                }
            } else {
                # Optional alias name for the new changelist
                foreach ($a in (_GitP4sonGetAliases)) {
                    if ($a -like "$wordToComplete*") {
                        $completions += [System.Management.Automation.CompletionResult]::new(
                            $a, $a, 'ParameterValue', "Alias: $a")
                    }
                }
            }
        }
        'update' {
            if ($wordToComplete -match '^-') {
                $flags = @('-b', '--base-branch', '-n', '--dry-run',
                           '--no-edit', '--shelve', '-s', '--sleep')
                foreach ($f in $flags) {
                    if ($f -like "$wordToComplete*") {
                        $completions += [System.Management.Automation.CompletionResult]::new(
                            $f, $f, 'ParameterName', $f)
                    }
                }
            } else {
                foreach ($a in (_GitP4sonGetAliases)) {
                    if ($a -like "$wordToComplete*") {
                        $completions += [System.Management.Automation.CompletionResult]::new(
                            $a, $a, 'ParameterValue', "Alias: $a")
                    }
                }
            }
        }
        'list-changes' {
            if ($wordToComplete -match '^-') {
                $flags = @('-b', '--base-branch')
                foreach ($f in $flags) {
                    if ($f -like "$wordToComplete*") {
                        $completions += [System.Management.Automation.CompletionResult]::new(
                            $f, $f, 'ParameterName', $f)
                    }
                }
            }
        }
        'alias' {
            if (-not $subcommand -or ($subcommand -eq $wordToComplete -and $positionals.Count -le 1)) {
                # Complete alias subcommands
                $subcmds = @(
                    @{ Name = 'list';   Desc = 'List all aliases' }
                    @{ Name = 'set';    Desc = 'Save a changelist under an alias' }
                    @{ Name = 'delete'; Desc = 'Delete an alias' }
                    @{ Name = 'clean';  Desc = 'Interactive cleanup of aliases' }
                )
                foreach ($sc in $subcmds) {
                    if ($sc.Name -like "$wordToComplete*") {
                        $completions += [System.Management.Automation.CompletionResult]::new(
                            $sc.Name, $sc.Name, 'ParameterValue', $sc.Desc)
                    }
                }
            } else {
                switch ($subcommand) {
                    'set' {
                        if ($wordToComplete -match '^-') {
                            $flags = @('-f', '--force')
                            foreach ($f in $flags) {
                                if ($f -like "$wordToComplete*") {
                                    $completions += [System.Management.Automation.CompletionResult]::new(
                                        $f, $f, 'ParameterName', $f)
                                }
                            }
                        }
                    }
                    'delete' {
                        if ($wordToComplete -notmatch '^-') {
                            foreach ($a in (_GitP4sonGetAliases)) {
                                if ($a -like "$wordToComplete*") {
                                    $completions += [System.Management.Automation.CompletionResult]::new(
                                        $a, $a, 'ParameterValue', "Alias: $a")
                                }
                            }
                        }
                    }
                    'list' {
                    }
                    'clean' {
                    }
                }
            }
        }
    }

    return $completions
}

# Register completer for "git-p4son" (standalone invocation)
Register-ArgumentCompleter -CommandName git-p4son -Native -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)

    $tokens = $commandAst.ToString() -split '\s+'
    # Skip the first token ("git-p4son"), rest are the p4son args
    $p4sonArgs = @($tokens | Select-Object -Skip 1)

    _GitP4sonCompleter $wordToComplete $commandAst $cursorPosition $p4sonArgs
}

# Register completer for "git" to handle "git p4son ..." subcommand
Register-ArgumentCompleter -CommandName git -Native -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)

    $tokens = $commandAst.ToString() -split '\s+'
    # Check if this is a "git p4son" invocation
    if ($tokens.Count -ge 2 -and $tokens[1] -eq 'p4son') {
        # Skip "git" and "p4son", rest are the p4son args
        $p4sonArgs = @($tokens | Select-Object -Skip 2)
        return _GitP4sonCompleter $wordToComplete $commandAst $cursorPosition $p4sonArgs
    }

    # Not a p4son subcommand, don't interfere with other git completions
}
