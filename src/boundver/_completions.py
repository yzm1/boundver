"""Shell completion scripts for the public boundver CLI."""

from typing import Dict


_COMMANDS = (
    "generate verify diff slice validate-config check-config init add remove "
    "status explain why discover migrate-lock completions"
)

_BASH_COMPLETION = r"""# boundver bash completion
_boundver_completions() {
    local cur prev command options
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    command="${COMP_WORDS[1]}"
    case "$prev" in
        --source) COMPREPLY=($(compgen -W "head index working-tree" -- "$cur")); return ;;
        --format) COMPREPLY=($(compgen -W "text json" -- "$cur")); return ;;
        --shell) COMPREPLY=($(compgen -W "bash zsh fish" -- "$cur")); return ;;
        --config|--lock|--out) COMPREPLY=($(compgen -f -- "$cur")); return ;;
    esac
    case "$command" in
        generate) options="--config --out --source --allow-partial --dry-run --components --format --allow-custom-providers --quiet --verbose" ;;
        verify) options="--config --lock --source --components --changed-from --facets --fail-fast --update --format --allow-custom-providers --quiet --verbose" ;;
        diff) options="--format" ;;
        slice) options="--lock" ;;
        validate-config|check-config) options="--config --allow-custom-providers" ;;
        init) options="--out --force --discover" ;;
        add) options="--provider --paths --config" ;;
        remove) options="--config" ;;
        status) options="--config --lock --source --format --strict --allow-custom-providers --quiet --verbose" ;;
        explain) options="--config --base-ref --source" ;;
        why) options="--config --lock --source --allow-custom-providers" ;;
        discover) options="--format" ;;
        migrate-lock) options="--lock --dry-run" ;;
        completions) options="--shell" ;;
        *) options="" ;;
    esac
    if [[ $COMP_CWORD -eq 1 ]]; then
        COMPREPLY=($(compgen -W "generate verify diff slice validate-config check-config init add remove status explain why discover migrate-lock completions --quiet --verbose --version --help" -- "$cur"))
    else
        COMPREPLY=($(compgen -W "$options" -- "$cur"))
    fi
}
complete -F _boundver_completions boundver
"""

_ZSH_COMPLETION = r"""#compdef boundver
_boundver() {
    local -a commands
    commands=(
      'generate:Generate or update the lockfile'
      'verify:Check selected facets against the lockfile'
      'diff:Diff two lockfiles'
      'slice:Show a slice fingerprint'
      'validate-config:Validate configuration'
      'check-config:Alias for validate-config'
      'init:Create a starter JSON config'
      'add:Add a component'
      'remove:Remove a component'
      'status:Show status and drift'
      'explain:Explain changed files'
      'why:Explain component drift'
      'discover:Discover tracked manifests'
      'migrate-lock:Check or migrate a lock schema'
      'completions:Emit a completion script'
    )
    _arguments -C '--quiet[Reduce output]' '--verbose[Extra diagnostics]' '--version[Show version]' '1:command:->command' '*::arg:->args'
    case $state in
      command) _describe command commands ;;
      args)
        case $words[2] in
          generate) _arguments '--config[Config]:file:_files' '--out[Lockfile]:file:_files' '--source[Source]:(head index working-tree)' '--allow-partial' '--dry-run' '--components[Names]' '--format[Format]:(text json)' '--allow-custom-providers' '--quiet' '--verbose' ;;
          verify) _arguments '--config[Config]:file:_files' '--lock[Lockfile]:file:_files' '--source[Source]:(head index working-tree)' '--components[Names]' '--changed-from[Git ref]' '--facets[Gate facets]' '--fail-fast' '--update' '--format[Format]:(text json)' '--allow-custom-providers' '--quiet' '--verbose' ;;
          diff) _arguments '1:old lock:_files' '2:new lock:_files' '--format[Format]:(text json)' ;;
          slice) _arguments '1:slice' '--lock[Lockfile]:file:_files' ;;
          validate-config|check-config) _arguments '--config[Config]:file:_files' '--allow-custom-providers' ;;
          init) _arguments '--out[Config]:file:_files' '--force' '--discover' ;;
          add) _arguments '1:name' '2:path:_files' '--provider[Provider]' '--paths[Boundary paths]' '--config[Config]:file:_files' ;;
          remove) _arguments '1:name' '--config[Config]:file:_files' ;;
          status) _arguments '--config[Config]:file:_files' '--lock[Lockfile]:file:_files' '--source[Source]:(head index working-tree)' '--format[Format]:(text json)' '--strict' '--allow-custom-providers' '--quiet' '--verbose' ;;
          explain) _arguments '1:component' '--config[Config]:file:_files' '--base-ref[Git ref]' '--source[Source]:(head index working-tree)' ;;
          why) _arguments '1:component' '--config[Config]:file:_files' '--lock[Lockfile]:file:_files' '--source[Source]:(head index working-tree)' '--allow-custom-providers' ;;
          discover) _arguments '--format[Format]:(text json)' ;;
          migrate-lock) _arguments '--lock[Lockfile]:file:_files' '--dry-run' ;;
          completions) _arguments '--shell[Shell]:(bash zsh fish)' ;;
        esac ;;
    esac
}
_boundver
"""

_FISH_COMPLETION = """# boundver fish completion
set -l __boundver_cmds generate verify diff slice validate-config check-config init add remove status explain why discover migrate-lock completions
complete -c boundver -f -n "not __fish_seen_subcommand_from $__boundver_cmds" -a "$__boundver_cmds"
complete -c boundver -l quiet -d 'Reduce output'
complete -c boundver -l verbose -d 'Extra diagnostics'
complete -c boundver -n '__fish_seen_subcommand_from generate' -l source -a 'head index working-tree'
complete -c boundver -n '__fish_seen_subcommand_from generate' -l config -F
complete -c boundver -n '__fish_seen_subcommand_from generate' -l out -F
complete -c boundver -n '__fish_seen_subcommand_from generate' -l allow-partial
complete -c boundver -n '__fish_seen_subcommand_from generate' -l dry-run
complete -c boundver -n '__fish_seen_subcommand_from generate' -l components
complete -c boundver -n '__fish_seen_subcommand_from generate' -l format -a 'text json'
complete -c boundver -n '__fish_seen_subcommand_from verify' -l source -a 'head index working-tree'
complete -c boundver -n '__fish_seen_subcommand_from verify' -l config -F
complete -c boundver -n '__fish_seen_subcommand_from verify' -l lock -F
complete -c boundver -n '__fish_seen_subcommand_from verify' -l components
complete -c boundver -n '__fish_seen_subcommand_from verify' -l changed-from
complete -c boundver -n '__fish_seen_subcommand_from verify' -l facets
complete -c boundver -n '__fish_seen_subcommand_from verify' -l fail-fast
complete -c boundver -n '__fish_seen_subcommand_from verify' -l update
complete -c boundver -n '__fish_seen_subcommand_from verify' -l format -a 'text json'
complete -c boundver -n '__fish_seen_subcommand_from status' -l strict
complete -c boundver -n '__fish_seen_subcommand_from status' -l format -a 'text json'
complete -c boundver -n '__fish_seen_subcommand_from explain why status' -l source -a 'head index working-tree'
complete -c boundver -n '__fish_seen_subcommand_from completions' -l shell -a 'bash zsh fish'
"""

_COMPLETION_SCRIPTS: Dict[str, str] = {
    "bash": _BASH_COMPLETION,
    "zsh": _ZSH_COMPLETION,
    "fish": _FISH_COMPLETION,
}
