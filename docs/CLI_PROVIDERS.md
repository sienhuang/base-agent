# Local CLI Model Providers

`CodexCLIProvider` and `ClaudeCLIProvider` adapt trusted local model command-line programs to the
same `ModelProvider` contract used by HTTP/SDK providers.

```python
from base_agent import Agent, AgentProfile, CodexCLIProvider

agent = Agent(
    profile=AgentProfile(
        id="local-cli-agent",
        instructions="Answer clearly and use declared application Tools when needed.",
        tools=("lookup",),
    ),
    model=CodexCLIProvider(),
    tools=(lookup,),
)
```

The adapters serialize the complete provider-neutral `ModelRequest`, including conversation
messages, application Tool definitions, and `tool_choice`. The local CLI must return a small
structured envelope:

```json
{
  "content": null,
  "tool_calls": [
    {
      "name": "lookup",
      "arguments_json": "{\"query\":\"DAU\"}"
    }
  ]
}
```

Application Tool calls are returned to the normal base-agent Model → Tool → Model loop. The local
CLI is not allowed to execute those application Tools itself.

## Codex

`CodexCLIProvider` invokes `codex exec` through an argv array, never through a shell. Every request
runs:

- in a new empty temporary directory;
- with `--sandbox read-only`;
- with `--ephemeral`;
- with `--ignore-user-config`;
- with `--skip-git-repo-check`;
- with model-generated shell commands inheriting no parent environment variables;
- with JSONL output and an explicit final-response JSON Schema.

Authentication still uses the normal Codex home. A model is optional; when omitted, the Codex CLI
uses its own default.

## Claude

`ClaudeCLIProvider` invokes `claude --print` with:

- JSON output and an explicit JSON Schema;
- built-in Tools disabled through `--tools ""`;
- slash commands disabled;
- session persistence disabled;
- an empty temporary working directory.

Authentication uses the installed Claude CLI configuration. A model is optional and otherwise
uses the CLI default.

## Process safety

Both adapters provide:

- argv-only execution without a shell;
- a default five-minute timeout;
- separate 4 MiB limits for stdout and stderr;
- termination and forced-kill cleanup;
- typed missing-executable, timeout, output-limit, non-zero-exit, and invalid-response errors;
- no stdout, stderr, prompts, Tool arguments, or response bodies in file logs.

The child process inherits the parent environment so the installed CLI can authenticate. Only use
trusted executables. `environment=` can add or replace selected environment values but does not
remove inherited variables.

Attachments and automatically retrieved Memory are rejected at the adapter boundary. Process
attachments through application Tools and use the explicit Memory Tool instead.

## my-agent / Starter CLI

Select the backend without changing application code:

```bash
uv run agent-app --provider codex-cli --no-skill "获取DAU"
uv run agent-app --provider claude-cli --model sonnet --no-skill "获取DAU"
```

Equivalent environment configuration:

```bash
export AGENT_PROVIDER=codex-cli
export AGENT_CLI_TIMEOUT_SECONDS=300
export AGENT_CLI_MAX_OUTPUT_BYTES=4194304
uv run agent-app --no-skill "获取DAU"
```

Use `--cli-executable /absolute/path/to/codex` or `AGENT_CLI_EXECUTABLE` when the executable is not
on `PATH`. Do not set `AGENT_MODEL` if the local CLI should select its configured default.

Each Model request starts a fresh CLI process. Conversation history and Tool observations are
provided again through `ModelRequest`, so correctness does not depend on CLI session persistence.
