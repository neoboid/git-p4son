# PowerShell completion for "jj p4son", the jj alias that runs git-p4son.
#
# Completes the words after "p4son" through "git-p4son complete", and hands every other jj completion to jj's own
# completer, so load it after jj's completion setup.
#
# Installation (pip install):
#   Dot-source in your PowerShell profile ($PROFILE), after jj's completion:
#     . $(git-p4son completion powershell --jj)
#
# Installation (from repo):
#   Dot-source in your PowerShell profile ($PROFILE), after jj's completion:
#     . /path/to/git-p4son/git_p4son/completions/git-p4son-jj.ps1

. (Join-Path $PSScriptRoot 'git-p4son.ps1')

function _GitP4sonNativeCompleters {
    # PowerShell has no public way to read a registered native completer back, so read its internal table.
    try {
        $flags = [Reflection.BindingFlags]'NonPublic,Instance'
        $context = $ExecutionContext.GetType().GetField('_context', $flags).GetValue($ExecutionContext)
        $completers = $context.GetType().GetProperty('NativeArgumentCompleters', $flags).GetValue($context)
        # The comma keeps PowerShell from unrolling the table into its entries.
        if ($null -ne $completers) { return , $completers }
    } catch {}
    return $null
}

function _GitP4sonHookJj($Completers) {
    # Without the table, the hook can't hand jj's other completions back, so jj keeps its own completer.
    if ($null -eq $Completers) {
        Write-Warning 'git-p4son could not read the jj completer to hand back to, so jj p4son completion is not set up.'
        return
    }
    $original = if ($Completers.ContainsKey('jj')) { $Completers['jj'] } else { $null }
    # Dot-sourced again, the hook is already in place.
    if ($original -ne $global:_GitP4sonJjCompleter) {
        $global:_GitP4sonJjOriginal = $original
    }
    Register-ArgumentCompleter -Native -CommandName jj -ScriptBlock $global:_GitP4sonJjCompleter
}

# Defined once, so dot-sourcing again finds the hook already registered.
if (-not $global:_GitP4sonJjCompleter) {
    $global:_GitP4sonJjCompleter = {
        param($wordToComplete, $commandAst, $cursorPosition)

        $elements = @($commandAst.CommandElements | ForEach-Object { $_.ToString() })
        # Leave out the word under the cursor, as bash and zsh do: still being typed, the alias is jj's to complete.
        $count = if ($wordToComplete) { $elements.Count - 1 } else { $elements.Count }
        $alias = if ($env:GIT_P4SON_JJ_ALIAS) { $env:GIT_P4SON_JJ_ALIAS } else { 'p4son' }
        # Skip jj's global options; "--config=x" is one element.
        $i = 1
        while ($i -lt $count) {
            $word = $elements[$i]
            if ($word -in '-R', '--repository', '--at-operation', '--at-op', '--color', '--config', '--config-file') {
                $i += 2
            } elseif ($word.StartsWith('-')) {
                $i += 1
            } else {
                break
            }
        }
        if ($i -lt $count -and $elements[$i] -eq $alias) {
            $p4sonArgs = @($elements | Select-Object -Skip ($i + 1))
            return _GitP4sonCompleter $wordToComplete $commandAst $cursorPosition $p4sonArgs
        }
        if ($global:_GitP4sonJjOriginal) {
            return & $global:_GitP4sonJjOriginal $wordToComplete $commandAst $cursorPosition
        }
    }
}

_GitP4sonHookJj (_GitP4sonNativeCompleters)
