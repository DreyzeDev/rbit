"""Shell completion scripts (kept lightweight for fast CLI startup)."""

from __future__ import annotations

COMMANDS = (
    "run mini doctor stats sessions session export import map lint init commit update upgrade uninstall "
    "mcp acp serve service api debug plugin auth config permissions completion logs models agent pr web "
    "watch voice backup describe schedule risk replay reload pair"
)

COMPLETION_SCRIPTS: dict[str, str] = {
    "bash": (
        "_rbit_completion() {\n"
        f"  COMPREPLY=( $(compgen -W \"{COMMANDS} --help --version --generate-completion\" "
        "-- \"${COMP_WORDS[COMP_CWORD]}\") )\n"
        "}\n"
        "complete -F _rbit_completion rbit\n"
    ),
    "zsh": (f"#compdef rbit\n_arguments '1:command:({COMMANDS})' '*::arg:->args'\n"),
    "powershell": (
        "Register-ArgumentCompleter -Native -CommandName rbit -ScriptBlock {\n"
        "    param($wordToComplete, $commandAst, $cursorPosition)\n"
        f"    '{COMMANDS}'.Split(' ') | Where-Object {{ $_ -like \"$wordToComplete*\" }} | "
        "ForEach-Object { [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_) }\n"
        "}\n"
    ),
}


def completion_script(shell: str) -> str:
    return COMPLETION_SCRIPTS.get(shell, COMPLETION_SCRIPTS["bash"])
