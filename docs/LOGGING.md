# File Logging

`Agent` automatically writes structured JSON Lines logs. No logging adapter or application setup is
required.

The default file is resolved from the process working directory:

```text
logs/base-agent.log
```

It rotates every day at local midnight and retains 30 days of backup files. Rotated files use a
date suffix such as `base-agent.log.2026-07-27`. If the working directory is not writable,
base-agent falls back to the operating system temporary directory under `base-agent/base-agent.log`.

Two optional environment variables change the destination and severity:

```bash
export BASE_AGENT_LOG_FILE=/var/log/my-agent/runtime.log
export BASE_AGENT_LOG_LEVEL=INFO
```

A relative `BASE_AGENT_LOG_FILE` is resolved from the working directory. The default level is
`INFO`; standard Python levels such as `DEBUG`, `WARNING`, and `ERROR` are accepted.

Each record contains correlation fields when available:

- `request_id`
- `conversation_id`
- `run_id`
- `turn_sequence`

Lifecycle records cover Agent initialization, Conversation Turn allocation, Run execution and
resume, model calls, Tool calls, cancellation, Runtime completion, HTTP requests, and infrastructure
warnings such as Redis notification fallback. Duration, status, token counts, and error types are
included where applicable. `model.request.completed` reports one Provider call, while
`runtime.execution.finished`, `run.finished`, and `run.resume_finished` report the actual token
usage accumulated across the whole Run together with `model_call_count`.

Prompts, model response bodies, Tool arguments, resume input, HTTP bodies, and Conversation metadata
values are intentionally excluded. The formatter also redacts common credential assignments,
Bearer tokens, AWS access-key IDs, and OpenAI-style secret keys if they occur in an exception
message.

The log directory is excluded by the base project and Starter `.gitignore` files. Do not commit
runtime logs because exception details and operational metadata may still be sensitive.
