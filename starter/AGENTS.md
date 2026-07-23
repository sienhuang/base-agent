# Agent application conventions

- Keep business behavior in `agent_app`, not in `base_agent` internals.
- Compose Agents from profiles, Tools, Skills, stores, and Resources; do not subclass Runtime.
- Every Tool must have typed arguments, a narrow purpose, and explicit permissions for side effects.
- Never place secrets in prompts, Skills, examples, logs, Run metadata, or committed files.
- Use the offline Provider and deterministic tests before enabling a network Provider.
- Keep infrastructure construction in `build_agent()` or an application lifecycle module.
- Run `pytest`, `ruff check .`, and `mypy src` before handing off changes.
