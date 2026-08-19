"""Shell completion scripts for the public boundver CLI.

The command and option tables in this module are the completion contract.  All
three scripts are rendered from those tables so adding an option cannot update
one shell while silently leaving another behind.
"""

from typing import Dict, List, Tuple

from ._utils import SOURCE_MODES


_SOURCE_CHOICES = " ".join(SOURCE_MODES)

_COMMAND_DESCRIPTIONS: Dict[str, str] = {
    "generate": "Generate or update the lockfile",
    "verify": "Check selected facets against the lockfile",
    "diff": "Diff two lockfiles",
    "slice": "Show a slice fingerprint",
    "validate-config": "Validate configuration",
    "check-config": "Alias for validate-config",
    "init": "Create a starter JSON config",
    "add": "Add a component",
    "remove": "Remove a component",
    "status": "Show status and drift",
    "explain": "Explain changed files",
    "why": "Explain component drift",
    "discover": "Discover tracked manifests",
    "migrate-lock": "Normalize a current lock or require regeneration",
    "completions": "Emit a completion script",
}

_COMMANDS: Tuple[str, ...] = tuple(_COMMAND_DESCRIPTIONS)

# argparse accepts these before the subcommand.  core.main() intentionally
# normalizes --quiet/--verbose when they occur after it as well.
_GLOBAL_OPTIONS: Tuple[str, ...] = (
    "-h",
    "--help",
    "--version",
    "--quiet",
    "--verbose",
)
_POST_COMMAND_GLOBAL_OPTIONS: Tuple[str, ...] = ("--quiet", "--verbose")

# Keep this table in parser order.  Tests compare it with the live argparse
# parser, including argparse's automatic -h/--help action.
_COMMAND_OPTIONS: Dict[str, Tuple[str, ...]] = {
    "generate": (
        "-h", "--help", "--config", "--out", "--source", "--allow-partial",
        "--dry-run", "--components", "--format", "--allow-custom-providers",
    ),
    "verify": (
        "-h", "--help", "--config", "--lock", "--source", "--components",
        "--changed-from", "--fail-fast", "--facets", "--transitive",
        "--update", "--format", "--allow-custom-providers",
    ),
    "diff": ("-h", "--help", "--format"),
    "slice": ("-h", "--help", "--lock", "--format"),
    "validate-config": ("-h", "--help", "--config", "--allow-custom-providers"),
    "check-config": ("-h", "--help", "--config", "--allow-custom-providers"),
    "init": ("-h", "--help", "--out", "--force", "--discover"),
    "add": ("-h", "--help", "--provider", "--paths", "--config"),
    "remove": ("-h", "--help", "--config"),
    "status": (
        "-h", "--help", "--config", "--lock", "--source", "--format",
        "--strict", "--allow-custom-providers",
    ),
    "explain": (
        "-h", "--help", "--config", "--base-ref", "--source",
        "--allow-custom-providers",
    ),
    "why": (
        "-h", "--help", "--config", "--lock", "--source", "--format",
        "--transitive", "--allow-custom-providers",
    ),
    "discover": ("-h", "--help", "--format"),
    "migrate-lock": ("-h", "--help", "--lock", "--dry-run"),
    "completions": ("-h", "--help", "--shell"),
}

_OPTION_DESCRIPTIONS: Dict[str, str] = {
    "-h": "Show help",
    "--help": "Show help",
    "--version": "Show version",
    "--quiet": "Reduce output",
    "--verbose": "Extra diagnostics",
    "--config": "Config file",
    "--out": "Lockfile output",
    "--source": "Snapshot source",
    "--allow-partial": "Allow partial boundary or compatibility digests",
    "--dry-run": "Preview without writing",
    "--components": "Comma-separated component names",
    "--format": "Output format",
    "--allow-custom-providers": "Allow external provider modules",
    "--lock": "Lockfile",
    "--changed-from": "Git base ref",
    "--fail-fast": "Report only the highest-severity mismatch",
    "--facets": "Comma-separated gate facets",
    "--transitive": "Include transitive downstream consumers",
    "--update": "Regenerate after reporting drift",
    "--force": "Overwrite an existing file",
    "--discover": "Auto-discover components",
    "--provider": "Boundary provider",
    "--paths": "Comma-separated boundary paths",
    "--strict": "Exit non-zero on warnings or drift",
    "--base-ref": "Git base ref",
    "--shell": "Target shell",
}

# option -> (argument label, zsh completion action).  An empty action still
# marks the option as taking a value.  Fish uses the same table to add -r.
_OPTION_ARGUMENTS: Dict[str, Tuple[str, str]] = {
    "--config": ("file", "_files"),
    "--out": ("file", "_files"),
    "--source": ("source", f"({_SOURCE_CHOICES})"),
    "--components": ("components", ""),
    "--format": ("format", "(text json)"),
    "--lock": ("file", "_files"),
    "--changed-from": ("Git ref", ""),
    "--facets": ("facets", ""),
    "--provider": ("provider", ""),
    "--paths": ("paths", ""),
    "--base-ref": ("Git ref", ""),
    "--shell": ("shell", "(bash zsh fish)"),
}

_OPTION_CHOICES: Dict[str, Tuple[str, ...]] = {
    "--source": SOURCE_MODES,
    "--format": ("json", "text"),
    "--shell": ("bash", "zsh", "fish"),
}

_FILE_OPTIONS = ("--config", "--lock", "--out")

# zsh can describe positional arguments without custom parsing functions.
_COMMAND_POSITIONALS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "diff": (("old lock", "_files"), ("new lock", "_files")),
    "slice": (("slice", ""),),
    "add": (("name", ""), ("path", "_files")),
    "remove": (("name", ""),),
    "explain": (("component", ""),),
    "why": (("component", ""),),
}


def _options_after_command(command: str) -> Tuple[str, ...]:
    """Return every option accepted in post-command position."""
    return _COMMAND_OPTIONS[command] + _POST_COMMAND_GLOBAL_OPTIONS


def _render_bash() -> str:
    command_pattern = "|".join(_COMMANDS)
    root_words = " ".join(_COMMANDS + _GLOBAL_OPTIONS)
    value_pattern = "|".join(_OPTION_ARGUMENTS)
    file_options = tuple(
        option
        for option, (_label, action) in _OPTION_ARGUMENTS.items()
        if action == "_files"
    )
    file_pattern = "|".join(file_options)
    file_equals_pattern = "|".join(
        "{0}=*".format(option) for option in file_options
    )
    plain_value_pattern = "|".join(
        option
        for option in _OPTION_ARGUMENTS
        if option not in _OPTION_CHOICES and option not in file_options
    )
    plain_value_equals_pattern = "|".join(
        "{0}=*".format(option)
        for option in _OPTION_ARGUMENTS
        if option not in _OPTION_CHOICES and option not in file_options
    )
    choice_cases = "\n".join(
        '        {0}) options="{1}" ;;'.format(
            option, " ".join(choices)
        )
        for option, choices in _OPTION_CHOICES.items()
    )
    choice_equals_cases = "\n".join(
        """        {option}=*)
            option_prefix="${{cur%%=*}}="
            value="${{cur#*=}}"
            COMPREPLY=()
            while IFS= read -r reply; do
                COMPREPLY+=("${{option_prefix}}${{reply}}")
            done < <(compgen -W "{choices}" -- "$value")
            return
            ;;""".format(option=option, choices=" ".join(choices))
        for option, choices in _OPTION_CHOICES.items()
    )
    file_position_pattern = "|".join(
        "{0}:{1}".format(command, index)
        for command, positionals in _COMMAND_POSITIONALS.items()
        for index, (_label, action) in enumerate(positionals, start=1)
        if action == "_files"
    )
    plain_position_pattern = "|".join(
        "{0}:{1}".format(command, index)
        for command, positionals in _COMMAND_POSITIONALS.items()
        for index, (_label, action) in enumerate(positionals, start=1)
        if action != "_files"
    )
    cases = "\n".join(
        '        {0}) options="{1}" ;;'.format(
            command, " ".join(_options_after_command(command))
        )
        for command in _COMMANDS
    )
    return """# boundver bash completion
_boundver_completions() {{
    local cur prev command command_index options word reply i
    local option_prefix value
    local expect_value=0 after_double_dash=0 positional_index=0
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"
    command=""
    command_index=0

    # Global flags may precede the command, so do not assume COMP_WORDS[1].
    for ((i=1; i<COMP_CWORD; i++)); do
        word="${{COMP_WORDS[i]}}"
        case "$word" in
            {command_pattern}) command="$word"; command_index=$i; break ;;
        esac
    done

    # Count completed positional arguments while skipping option values.  This
    # keeps value-taking options from falling back to command-option results.
    if [[ -n "$command" ]]; then
        for ((i=command_index+1; i<COMP_CWORD; i++)); do
            word="${{COMP_WORDS[i]}}"
            if (( after_double_dash )); then
                positional_index=$((positional_index + 1))
                continue
            fi
            if (( expect_value )); then
                expect_value=0
                continue
            fi
            case "$word" in
                --) after_double_dash=1 ;;
                --*=*) ;;
                {value_pattern}) expect_value=1 ;;
                -*) ;;
                *) positional_index=$((positional_index + 1)) ;;
            esac
        done
    fi

    # Complete --option=value without losing the option prefix.
    case "$cur" in
{choice_equals_cases}
        {file_equals_pattern})
            option_prefix="${{cur%%=*}}="
            value="${{cur#*=}}"
            COMPREPLY=()
            while IFS= read -r reply; do
                COMPREPLY+=("${{option_prefix}}${{reply}}")
            done < <(compgen -f -- "$value")
            return
            ;;
        {plain_value_equals_pattern}) COMPREPLY=(); return ;;
    esac

    if (( ! after_double_dash )); then
        case "$prev" in
{choice_cases}
            {file_pattern})
                COMPREPLY=()
                while IFS= read -r reply; do
                    COMPREPLY+=("$reply")
                done < <(compgen -f -- "$cur")
                return
                ;;
            {plain_value_pattern}) COMPREPLY=(); return ;;
        esac
        if [[ -n "${{options:-}}" ]]; then
            COMPREPLY=()
            while IFS= read -r reply; do
                COMPREPLY+=("$reply")
            done < <(compgen -W "$options" -- "$cur")
            return
        fi
    fi

    # Positional grammar comes from the same table as zsh completion.
    if [[ -n "$command" && ( $after_double_dash -eq 1 || "$cur" != -* ) ]]; then
        case "$command:$((positional_index + 1))" in
            {file_position_pattern})
                COMPREPLY=()
                while IFS= read -r reply; do
                    COMPREPLY+=("$reply")
                done < <(compgen -f -- "$cur")
                return
                ;;
            {plain_position_pattern}) COMPREPLY=(); return ;;
            *)
                if (( after_double_dash )) || [[ -n "$cur" ]]; then
                    COMPREPLY=()
                    return
                fi
                ;;
        esac
    fi

    if [[ -z "$command" ]]; then
        options="{root_words}"
    else
        case "$command" in
{cases}
            *) options="" ;;
        esac
    fi
    COMPREPLY=()
    while IFS= read -r reply; do
        COMPREPLY+=("$reply")
    done < <(compgen -W "$options" -- "$cur")
}}
complete -F _boundver_completions boundver
""".format(
        command_pattern=command_pattern,
        root_words=root_words,
        cases=cases,
        value_pattern=value_pattern,
        file_pattern=file_pattern,
        file_equals_pattern=file_equals_pattern,
        plain_value_pattern=plain_value_pattern,
        plain_value_equals_pattern=plain_value_equals_pattern,
        choice_cases=choice_cases,
        choice_equals_cases=choice_equals_cases,
        file_position_pattern=file_position_pattern,
        plain_position_pattern=plain_position_pattern,
    )


def _zsh_option(option: str) -> str:
    spec = "{0}[{1}]".format(option, _OPTION_DESCRIPTIONS[option])
    argument = _OPTION_ARGUMENTS.get(option)
    if argument is not None:
        label, action = argument
        spec += ":{0}:{1}".format(label, action)
    return "'{0}'".format(spec)


def _render_zsh() -> str:
    command_lines = "\n".join(
        "      '{0}:{1}'".format(command, _COMMAND_DESCRIPTIONS[command])
        for command in _COMMANDS
    )
    separator = " " + "\\" + "\n      "
    global_arguments = separator.join(
        _zsh_option(option) for option in _GLOBAL_OPTIONS
    )

    command_cases: List[str] = []
    for command in _COMMANDS:
        specs = [_zsh_option(option) for option in _options_after_command(command)]
        for index, (label, action) in enumerate(
            _COMMAND_POSITIONALS.get(command, ()), start=1
        ):
            specs.append("'{0}:{1}:{2}'".format(index, label, action))
        arguments = (" " + "\\" + "\n              ").join(specs)
        command_cases.append(
            "          {0})\n"
            "            _arguments \\\n"
            "              {1}\n"
            "            ;;".format(command, arguments)
        )

    return """#compdef boundver
_boundver() {{
    local -a commands
    local command word
    commands=(
{command_lines}
    )
    _arguments -C \\
      {global_arguments} \\
      '1:command:->command' \\
      '*::arg:->args'
    case $state in
      command) _describe command commands ;;
      args)
        for word in $words[2,-1]; do
          case $word in
            {command_pattern}) command=$word; break ;;
          esac
        done
        case $command in
{command_cases}
        esac ;;
    esac
}}
_boundver
""".format(
        command_lines=command_lines,
        global_arguments=global_arguments,
        command_pattern="|".join(_COMMANDS),
        command_cases="\n".join(command_cases),
    )


def _fish_option(command: str, option: str) -> str:
    # -h is emitted together with --help as a global completion.
    if option in {"-h", "--help"}:
        return ""
    parts = [
        "complete -c boundver",
        "-n '__fish_seen_subcommand_from {0}'".format(command),
        "-l {0}".format(option[2:]),
    ]
    if option in _OPTION_ARGUMENTS:
        parts.append("-r")
    if option in _FILE_OPTIONS:
        parts.append("-F")
    choices = _OPTION_CHOICES.get(option)
    if choices:
        parts.append("-a '{0}'".format(" ".join(choices)))
    parts.append("-d '{0}'".format(_OPTION_DESCRIPTIONS[option]))
    return " ".join(parts)


def _render_fish() -> str:
    commands = " ".join(_COMMANDS)
    command_lines = [
        "complete -c boundver -f "
        "-n \"not __fish_seen_subcommand_from $__boundver_cmds\" "
        "-a '{0}' -d '{1}'".format(command, _COMMAND_DESCRIPTIONS[command])
        for command in _COMMANDS
    ]
    option_lines = [
        "complete -c boundver -s h -l help -d 'Show help'",
        "complete -c boundver -n \"not __fish_seen_subcommand_from $__boundver_cmds\" "
        "-l version -d 'Show version'",
        "complete -c boundver -l quiet -d 'Reduce output'",
        "complete -c boundver -l verbose -d 'Extra diagnostics'",
    ]
    for command in _COMMANDS:
        for option in _COMMAND_OPTIONS[command]:
            line = _fish_option(command, option)
            if line:
                option_lines.append(line)

    return "\n".join(
        [
            "# boundver fish completion",
            "set -l __boundver_cmds {0}".format(commands),
            *command_lines,
            *option_lines,
            "",
        ]
    )


_BASH_COMPLETION = _render_bash()
_ZSH_COMPLETION = _render_zsh()
_FISH_COMPLETION = _render_fish()

_COMPLETION_SCRIPTS: Dict[str, str] = {
    "bash": _BASH_COMPLETION,
    "zsh": _ZSH_COMPLETION,
    "fish": _FISH_COMPLETION,
}
