# Contributing to rbit

Thanks for helping improve rbit.

## Setup

```bash
git clone https://github.com/DreyzeDev/rbit.git
cd rbit
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e ".[dev]"
pre-commit install             # optional but recommended
```

## Development workflow

- Run the test suite: `pytest -q`
- Lint the CI subset: `ruff check rbit tests --select E9,F63,F7,F82`
- Keep the TUI usable at 90x30 (compact mode) and follow the existing code style.
- Add or update tests for every behavioral change.

## Commits and pull requests

- Write short imperative commit messages (`feat: ...`, `fix: ...`, `docs: ...`).
- Describe the user-visible change and list the tests you ran in the PR body.
- Do not commit API keys, `.env` files, or `.rbit/` state.
- Update `docs/IMPROVEMENTS.md` and `README.md` when a backlog item is completed.

## Reporting bugs

Open an issue using the bug template and include the `rbit doctor` output, OS, Python
version, and the model/provider you used.
