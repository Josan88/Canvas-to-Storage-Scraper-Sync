# Agent Instructions

## Commands
- **Build/Install**: `pip install -r requirements.txt`
- **Executable**: `pyinstaller CanvasSync.spec`
- **Test**: No tests currently. Use `pytest` if adding new tests.
- **Lint**: Standard Python linting (e.g. `flake8`, `black`).

## Code Style
- **Structure**: Single-file `main.py` (1800+ lines). Do not split unless requested.
- **Formatting**: PEP 8. Use 4 spaces for indentation.
- **Types**: Use `typing` module (List, Dict, Optional) for function signatures.
- **Naming**: `snake_case` for functions/variables, `CamelCase` for classes.
- **Error Handling**: Use `try/except` for external calls. Always use `response.raise_for_status()`.
- **Imports**: Standard library -> Third-party -> Local. Group imports clearly.

## Rules & Context
- **CRITICAL**: Review `.github/copilot-instructions.md` before any changes.
- **Architecture**: `main.py` syncs Canvas -> Local/Drive.
- **Patterns**: Pass `session` explicitly. Use `SummaryCollector` for reporting.
