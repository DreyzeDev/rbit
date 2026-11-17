"""Allow `python -m rbit` execution and a fast `--version` path."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["--version"], ["-v"]):
        from . import __version__

        print(f"rbit v{__version__}")
        return 0
    if args and args[0] == "--generate-completion":
        from .completion import COMPLETION_SCRIPTS, completion_script

        shell = args[1].lower() if len(args) > 1 else "bash"
        if shell not in COMPLETION_SCRIPTS:
            print(f"Unsupported shell '{shell}'. Use bash, zsh, or powershell.", file=sys.stderr)
            return 2
        print(completion_script(shell), end="")
        return 0
    from .cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
