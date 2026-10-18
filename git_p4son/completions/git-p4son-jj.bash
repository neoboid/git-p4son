# Bash completion for "jj p4son", the jj alias that runs git-p4son.
#
# Completes the words after "p4son" through "git-p4son complete", and hands every other jj completion to jj's own
# completer, so load it after jj's completion setup.
#
# Installation (pip install):
#   Add to ~/.bashrc, after jj's completion:
#     source $(git-p4son completion bash --jj)
#
# Installation (from repo):
#   Add to ~/.bashrc, after jj's completion:
#     source /path/to/git-p4son/git_p4son/completions/git-p4son-jj.bash

source "$(dirname "${BASH_SOURCE[0]}")/git-p4son.bash"

_git_p4son_jj() {
    local i=1
    # Skip jj's global options; bash splits "--config=x" into "--config", "=", "x".
    while (( i < COMP_CWORD )); do
        case "${COMP_WORDS[i]}" in
            -R|--repository|--at-operation|--at-op|--color|--config|--config-file)
                if [[ "${COMP_WORDS[i+1]}" == "=" ]]; then (( i += 3 )); else (( i += 2 )); fi ;;
            -*) (( i += 1 )) ;;
            *) break ;;
        esac
    done
    if (( i < COMP_CWORD )) && [[ "${COMP_WORDS[i]}" == "${GIT_P4SON_JJ_ALIAS:-p4son}" ]]; then
        # jj registers with nospace; git-p4son's candidates are whole words. bash 3.2 has no compopt.
        compopt +o nospace 2>/dev/null
        __git_p4son_complete_words "${COMP_WORDS[@]:i+1}"
    elif [[ -n "$__git_p4son_jj_original" ]]; then
        "$__git_p4son_jj_original" "$@"
    fi
}

__git_p4son_jj_register() {
    local spec
    spec=$(complete -p jj 2>/dev/null)
    if [[ -z "$spec" ]]; then
        # bash-completion loads a packaged jj completion on first use; load it now, to have one to hand over to.
        __load_completion jj 2>/dev/null || _comp_load jj 2>/dev/null || _completion_loader jj 2>/dev/null
        spec=$(complete -p jj 2>/dev/null)
    fi
    if [[ "$spec" =~ -F\ ([^ ]+) ]]; then
        # Sourced again, the hook is already in place.
        [[ "${BASH_REMATCH[1]}" == _git_p4son_jj ]] && return
        __git_p4son_jj_original=${BASH_REMATCH[1]}
        eval "${spec/-F ${BASH_REMATCH[1]}/-F _git_p4son_jj}"
    else
        complete -o bashdefault -o default -F _git_p4son_jj jj
    fi
}
__git_p4son_jj_register
