# Bash completion for git-p4son
# Also works for "git p4son" subcommand completion.
#
# Delegates to "git-p4son complete" for candidate generation.
#
# Installation (pip install):
#   Add to ~/.bashrc:
#     source $(git-p4son completion bash)
#
# Installation (from repo):
#   Add to ~/.bashrc:
#     source /path/to/git-p4son/git_p4son/completions/git-p4son.bash

_git_p4son_complete() {
    local words=("${COMP_WORDS[@]:1}")

    local output
    output=$(git-p4son complete -- "${words[@]}" 2>/dev/null) || return

    local candidates=()
    local name
    while IFS=$'\t' read -r name _; do
        [[ -z "$name" ]] && continue
        if [[ "$name" == "__branch__" ]]; then
            local branches
            branches=$(git branch --format='%(refname:short)' 2>/dev/null) || continue
            while IFS= read -r branch; do
                candidates+=("$branch")
            done <<< "$branches"
        else
            candidates+=("$name")
        fi
    done <<< "$output"

    local IFS=$'\n'
    COMPREPLY=($(compgen -W "${candidates[*]}" -- "${COMP_WORDS[COMP_CWORD]}"))
}

# Register for standalone "git-p4son" invocation
complete -F _git_p4son_complete git-p4son

# Register for "git p4son" subcommand completion.
# Git's bash completion looks for _git_p4son when completing "git p4son <TAB>".
_git_p4son() {
    _git_p4son_complete
}
