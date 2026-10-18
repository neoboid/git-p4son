# Zsh completion for "jj p4son", the jj alias that runs git-p4son.
#
# Completes the words after "p4son" through _git-p4son, and hands every other jj completion to jj's own completer,
# so load it after compinit and jj's completion setup.
#
# Installation (pip install):
#   Add to ~/.zshrc, after compinit and jj's completion:
#     source $(git-p4son completion zsh --jj)
#
# Installation (from repo):
#   Add to ~/.zshrc, after compinit and jj's completion:
#     source /path/to/git-p4son/git_p4son/completions/git-p4son-jj.zsh

fpath=(${0:A:h} $fpath)
autoload -Uz _git-p4son

_git_p4son_jj() {
    local i=2
    # Skip jj's global options; zsh keeps "--config=x" one word.
    while (( i < CURRENT )); do
        case ${words[i]} in
            -R|--repository|--at-operation|--at-op|--color|--config|--config-file) (( i += 2 )) ;;
            -*) (( i += 1 )) ;;
            *) break ;;
        esac
    done
    if (( i < CURRENT )) && [[ ${words[i]} == ${GIT_P4SON_JJ_ALIAS:-p4son} ]]; then
        words=(git-p4son "${(@)words[i+1,-1]}")
        (( CURRENT -= i - 1 ))
        _git-p4son
    elif [[ -n $_git_p4son_jj_original ]]; then
        $_git_p4son_jj_original "$@"
    fi
}

# Sourced again, the hook is already in place.
if [[ $_comps[jj] != _git_p4son_jj ]]; then
    _git_p4son_jj_original=$_comps[jj]
    compdef _git_p4son_jj jj
fi
