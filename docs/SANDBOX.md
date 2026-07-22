# Sandbox Sessions and Docker Adapter

The provider-neutral `SandboxSession` exposes three capabilities: argv execution, bounded text
reading, and bounded text writing. `sandbox_tools()` maps those capabilities onto Resource-aware
Tools without coupling Agent Runtime to Docker or any remote Sandbox product.

## Docker installation

```bash
uv add 'base-agent[sandbox]'
```

The host must provide a reachable Docker daemon and a preloaded image. The adapter never mounts the
Docker socket into a Sandbox and does not automatically pull an absent image. The image must contain
`/bin/sh`, `sleep`, `cat`, `mkdir`, and `base64`.

## Configure an Agent

```python
from base_agent import Agent, AgentProfile
from base_agent.sandbox import sandbox_tools
from base_agent.sandbox.docker import DockerSandboxConfig, docker_sandbox_resource

config = DockerSandboxConfig(
    image="my-sandbox@sha256:...",
    network_enabled=False,
    command_timeout_seconds=60,
)
tools = sandbox_tools(execute_timeout_seconds=65)

agent = Agent(
    profile=AgentProfile(
        id="coder",
        instructions="Work only inside the isolated workspace.",
        tools=("sandbox_execute", "sandbox_read_text", "sandbox_write_text"),
        permissions=frozenset(
            {"sandbox:execute", "sandbox:read", "sandbox:write"}
        ),
    ),
    model=model,
    tools=tools,
    resources=(docker_sandbox_resource(config),),
)
```

`sandbox_execute` accepts an argv array such as `["python", "script.py"]`; it never implies a
shell. A model can request `['/bin/sh', '-c', '...']` only when the application grants the broad
`sandbox:execute` permission, making that escalation visible in Tool arguments and policy.

## Default Docker boundary

Each acquired Resource creates one disposable container with:

- no network namespace connectivity unless explicitly enabled;
- a read-only root filesystem;
- writable, size-limited, `noexec/nosuid/nodev` tmpfs mounts for `/workspace` and `/tmp`;
- all Linux capabilities dropped and `no-new-privileges` enabled;
- configurable non-root user, memory, CPU, PID, command-time, output, and file limits;
- no host bind mounts, privileged mode, port publication, Docker socket, or inherited host secrets;
- fixed `/bin/sh` entrypoint instead of an image-provided entrypoint;
- forced container removal on timeout, cancellation, normal release, or failure.

Docker shares the host kernel and access to the Docker daemon is itself highly privileged. This
adapter is a reference boundary, not a substitute for host hardening, image review, seccomp/AppArmor,
rootless containers, egress controls, or a stronger VM/microVM Sandbox where required.

## Lifecycle and persistence

The default Docker Resource exists for one execution segment. A Run entering `WAITING` releases and
destroys it; resume receives a new workspace. Applications needing continuity must provide their own
`SandboxSession` Resource backed by durable storage or an externally managed Sandbox. Live container
objects are never checkpointed.

## Integration test

```bash
BASE_AGENT_TEST_SANDBOX_IMAGE='postgres:17-alpine' \
  uv run pytest tests/test_sandbox.py
```
