"""Shell completion scripts for boundver."""

from typing import Dict

_BASH_COMPLETION = """\
# boundver bash completion
# Source this file or add to ~/.bash_completion.d/
_boundver_completions() {
    local cur prev words cword
    _init_completion 2>/dev/null || {
        COMP_WORDS=("${COMP_WORDS[@]}")
        cword=$COMP_CWORD
        cur="${COMP_WORDS[$cword]}"
        prev="${COMP_WORDS[$cword-1]}"
    }

    local commands="generate verify diff slice validate-config init status explain why discover completions"
    local global_opts="--quiet --verbose --help"

    case "${COMP_WORDS[1]}" in
        generate)
            case "$prev" in
                --source)   COMPREPLY=($(compgen -W "head index working-tree" -- "$cur")); return ;;
                --format)   COMPREPLY=($(compgen -W "json text" -- "$cur")); return ;;
                --config|--out) COMPREPLY=($(compgen -f -- "$cur")); return ;;
            esac
            COMPREPLY=($(compgen -W "--config --out --source --allow-partial --dry-run --components --format --allow-custom-providers --quiet --verbose" -- "$cur")) ;;
        verify)
            case "$prev" in
                --source)       COMPREPLY=($(compgen -W "head index working-tree" -- "$cur")); return ;;
                --format)       COMPREPLY=($(compgen -W "json text" -- "$cur")); return ;;
                --config|--lock) COMPREPLY=($(compgen -f -- "$cur")); return ;;
            esac
            COMPREPLY=($(compgen -W "--config --lock --source --components --changed-from --format --allow-custom-providers --quiet --verbose" -- "$cur")) ;;
        diff)
            case "$prev" in
                --format) COMPREPLY=($(compgen -W "json text" -- "$cur")); return ;;
            esac
            COMPREPLY=($(compgen -W "--format" -- "$cur")) ;;
        slice)
            case "$prev" in
                --lock) COMPREPLY=($(compgen -f -- "$cur")); return ;;
            esac
            COMPREPLY=($(compgen -W "--lock" -- "$cur")) ;;
        validate-config)
            COMPREPLY=($(compgen -W "--config --allow-custom-providers" -- "$cur")) ;;
        init)
            COMPREPLY=($(compgen -W "--out --force --discover" -- "$cur")) ;;
        status)
            case "$prev" in
                --source) COMPREPLY=($(compgen -W "head index working-tree" -- "$cur")); return ;;
                --format) COMPREPLY=($(compgen -W "json text" -- "$cur")); return ;;
            esac
            COMPREPLY=($(compgen -W "--config --lock --source --format --quiet" -- "$cur")) ;;
        explain)
            case "$prev" in
                --config) COMPREPLY=($(compgen -f -- "$cur")); return ;;
            esac
            COMPREPLY=($(compgen -W "--config --base-ref" -- "$cur")) ;;
        why)
            case "$prev" in
                --source) COMPREPLY=($(compgen -W "head index working-tree" -- "$cur")); return ;;
                --config|--lock) COMPREPLY=($(compgen -f -- "$cur")); return ;;
            esac
            COMPREPLY=($(compgen -W "--config --lock --source" -- "$cur")) ;;
        discover)
            case "$prev" in
                --format) COMPREPLY=($(compgen -W "json text" -- "$cur")); return ;;
            esac
            COMPREPLY=($(compgen -W "--format" -- "$cur")) ;;
        completions)
            case "$prev" in
                --shell) COMPREPLY=($(compgen -W "bash zsh fish" -- "$cur")); return ;;
            esac
            COMPREPLY=($(compgen -W "--shell" -- "$cur")) ;;
        *)
            if [[ $cword -eq 1 ]]; then
                COMPREPLY=($(compgen -W "$commands $global_opts" -- "$cur"))
            fi ;;
    esac
}
complete -F _boundver_completions boundver
"""

_ZSH_COMPLETION = """\
#compdef boundver
# boundver zsh completion — place in a $fpath directory as _boundver

_boundver() {
    local -a commands
    commands=(
        'generate:Generate or update the lockfile'
        'verify:Check lockfile matches current repo state'
        'diff:Diff two lockfiles'
        'slice:Show fingerprint for a specific slice'
        'validate-config:Validate config for strict boundary rules'
        'init:Create a starter boundary.config.json'
        'status:Show lockfile summary and warnings'
        'explain:Explain changed files for a component'
        'why:Explain why a component lockfile is out of date'
        'discover:Print discovered components as JSON'
        'completions:Emit shell completion scripts'
    )
    case "$words[2]" in
        generate)
            _arguments \\
                '--config[Config file path]:file:_files' \\
                '--out[Output lockfile path]:file:_files' \\
                '--source[Fingerprint source]:source:(head index working-tree)' \\
                '--allow-partial[Allow missing digests]' \\

                '--dry-run[Do not write output]' \\
                '--components[Component names (comma-separated)]:components:' \\
                '--format[Output format]:format:(json text)' \\
                '--allow-custom-providers[Allow loading external provider modules]' \\
                '--quiet[Suppress output]' \\
                '--verbose[Extra diagnostics]' ;;
        verify)
            _arguments \\
                '--config[Config file path]:file:_files' \\
                '--lock[Lockfile path]:file:_files' \\
                '--source[Fingerprint source]:source:(head index working-tree)' \\
                '--components[Component names (comma-separated)]:components:' \\
                '--changed-from[Auto-select changed components since ref]:ref:' \\
                '--format[Output format]:format:(json text)' \\
                '--allow-custom-providers[Allow loading external provider modules]' \\
                '--quiet[Suppress output]' \\
                '--verbose[Extra diagnostics]' ;;
        diff)
            _arguments \\
                ':old lockfile:_files' \\
                ':new lockfile:_files' \\
                '--format[Output format]:format:(json text)' ;;
        slice)
            _arguments \\
                ':slice name:' \\
                '--lock[Lockfile path]:file:_files' ;;
        validate-config)
            _arguments '--config[Config file path]:file:_files' \\
                        '--allow-custom-providers[Allow loading external provider modules]' ;;
        init)
            _arguments \\
                '--out[Output config path]:file:_files' \\
                '--force[Overwrite existing]' \\
                '--discover[Auto-discover components]' ;;
        status)
            _arguments \\
                '--config[Config file path]:file:_files' \\
                '--lock[Lockfile path]:file:_files' \\
                '--source[Fingerprint source]:source:(head index working-tree)' \\
                '--format[Output format]:format:(json text)' \\
                '--quiet[Suppress output]' ;;
        explain)
            _arguments \\
                ':component name:' \\
                '--config[Config file path]:file:_files' \\
                '--base-ref[Git ref to diff against]:ref:' ;;
        why)
            _arguments \\
                ':component name:' \\
                '--config[Config file path]:file:_files' \\
                '--lock[Lockfile path]:file:_files' \\
                '--source[Fingerprint source]:source:(head index working-tree)' ;;
        discover)
            _arguments '--format[Output format]:format:(json text)' ;;
        completions)
            _arguments '--shell[Shell type]:shell:(bash zsh fish)' ;;
        *)
            _describe 'command' commands ;;
    esac
}
_boundver
"""

_FISH_COMPLETION = """\
# boundver fish completion
# Place in ~/.config/fish/completions/boundver.fish

set -l __boundver_cmds generate verify diff slice validate-config init status explain why discover completions

# subcommands
complete -c boundver -f -n "not __fish_seen_subcommand_from $__boundver_cmds" -a generate        -d 'Generate or update the lockfile'
complete -c boundver -f -n "not __fish_seen_subcommand_from $__boundver_cmds" -a verify          -d 'Check lockfile matches current repo state'
complete -c boundver -f -n "not __fish_seen_subcommand_from $__boundver_cmds" -a diff            -d 'Diff two lockfiles'
complete -c boundver -f -n "not __fish_seen_subcommand_from $__boundver_cmds" -a slice           -d 'Show fingerprint for a specific slice'
complete -c boundver -f -n "not __fish_seen_subcommand_from $__boundver_cmds" -a validate-config -d 'Validate config'
complete -c boundver -f -n "not __fish_seen_subcommand_from $__boundver_cmds" -a init            -d 'Create a starter boundary.config.json'
complete -c boundver -f -n "not __fish_seen_subcommand_from $__boundver_cmds" -a status          -d 'Show lockfile summary and warnings'
complete -c boundver -f -n "not __fish_seen_subcommand_from $__boundver_cmds" -a explain         -d 'Explain changed files for a component'
complete -c boundver -f -n "not __fish_seen_subcommand_from $__boundver_cmds" -a why            -d 'Explain why a component lockfile is out of date'
complete -c boundver -f -n "not __fish_seen_subcommand_from $__boundver_cmds" -a discover        -d 'Print discovered components as JSON'
complete -c boundver -f -n "not __fish_seen_subcommand_from $__boundver_cmds" -a completions     -d 'Emit shell completion scripts'

# generate
complete -c boundver -n "__fish_seen_subcommand_from generate" -l config      -d 'Config file path'         -F
complete -c boundver -n "__fish_seen_subcommand_from generate" -l out         -d 'Output lockfile path'     -F
complete -c boundver -n "__fish_seen_subcommand_from generate" -l source      -d 'Fingerprint source'       -a 'head index working-tree'
complete -c boundver -n "__fish_seen_subcommand_from generate" -l format      -d 'Output format'            -a 'json text'
complete -c boundver -n "__fish_seen_subcommand_from generate" -l components  -d 'Component names'
complete -c boundver -n "__fish_seen_subcommand_from generate" -l allow-partial -d 'Allow missing digests'
complete -c boundver -n "__fish_seen_subcommand_from generate" -l dry-run     -d 'Do not write output'
complete -c boundver -n "__fish_seen_subcommand_from generate" -l allow-custom-providers -d 'Allow loading external provider modules'
complete -c boundver -n "__fish_seen_subcommand_from generate" -l quiet       -d 'Suppress output'
complete -c boundver -n "__fish_seen_subcommand_from generate" -l verbose     -d 'Extra diagnostics'

# verify
complete -c boundver -n "__fish_seen_subcommand_from verify" -l config       -d 'Config file path'             -F
complete -c boundver -n "__fish_seen_subcommand_from verify" -l lock         -d 'Lockfile path'                -F
complete -c boundver -n "__fish_seen_subcommand_from verify" -l source       -d 'Fingerprint source'           -a 'head index working-tree'
complete -c boundver -n "__fish_seen_subcommand_from verify" -l format       -d 'Output format'                -a 'json text'
complete -c boundver -n "__fish_seen_subcommand_from verify" -l components   -d 'Component names'
complete -c boundver -n "__fish_seen_subcommand_from verify" -l changed-from -d 'Auto-select changed since ref'
complete -c boundver -n "__fish_seen_subcommand_from verify" -l allow-custom-providers -d 'Allow loading external provider modules'
complete -c boundver -n "__fish_seen_subcommand_from verify" -l quiet        -d 'Suppress output'
complete -c boundver -n "__fish_seen_subcommand_from verify" -l verbose      -d 'Extra diagnostics'

# diff
complete -c boundver -n "__fish_seen_subcommand_from diff" -l format -d 'Output format' -a 'json text'

# slice
complete -c boundver -n "__fish_seen_subcommand_from slice" -l lock -d 'Lockfile path' -F

# validate-config
complete -c boundver -n "__fish_seen_subcommand_from validate-config" -l config              -d 'Config file path' -F
complete -c boundver -n "__fish_seen_subcommand_from validate-config" -l allow-custom-providers -d 'Allow loading external provider modules'

# init
complete -c boundver -n "__fish_seen_subcommand_from init" -l out      -d 'Output config path' -F
complete -c boundver -n "__fish_seen_subcommand_from init" -l force    -d 'Overwrite existing'
complete -c boundver -n "__fish_seen_subcommand_from init" -l discover -d 'Auto-discover components'

# status
complete -c boundver -n "__fish_seen_subcommand_from status" -l config -d 'Config file path'     -F
complete -c boundver -n "__fish_seen_subcommand_from status" -l lock   -d 'Lockfile path'        -F
complete -c boundver -n "__fish_seen_subcommand_from status" -l source -d 'Fingerprint source'   -a 'head index working-tree'
complete -c boundver -n "__fish_seen_subcommand_from status" -l format -d 'Output format'        -a 'json text'
complete -c boundver -n "__fish_seen_subcommand_from status" -l quiet  -d 'Suppress output'

# explain
complete -c boundver -n "__fish_seen_subcommand_from explain" -l config   -d 'Config file path'       -F
complete -c boundver -n "__fish_seen_subcommand_from explain" -l base-ref -d 'Git ref to diff against'

# why
complete -c boundver -n "__fish_seen_subcommand_from why" -l config -d 'Config file path'     -F
complete -c boundver -n "__fish_seen_subcommand_from why" -l lock   -d 'Lockfile path'        -F
complete -c boundver -n "__fish_seen_subcommand_from why" -l source -d 'Fingerprint source'   -a 'head index working-tree'

# discover
complete -c boundver -n "__fish_seen_subcommand_from discover" -l format -d 'Output format' -a 'json text'

# completions
complete -c boundver -n "__fish_seen_subcommand_from completions" -l shell -d 'Shell type' -a 'bash zsh fish'
"""

_COMPLETION_SCRIPTS: Dict[str, str] = {
    "bash": _BASH_COMPLETION,
    "zsh": _ZSH_COMPLETION,
    "fish": _FISH_COMPLETION,
}
