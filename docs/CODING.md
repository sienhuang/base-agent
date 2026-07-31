# Coding Bundle

`CodingBundle` packages the existing Sandbox Tools and one execution-scoped Sandbox Resource into a
concrete coding composition. It does not add a generic Capability abstraction and it does not grant
permissions implicitly.

## Docker-backed coding

Install the optional adapter and preload an image:

```bash
uv add 'base-agent[sandbox]'
docker pull python:3.12
```

Use an immutable image digest in production; the tag above is only a concise local example.

Compose the bundle:

```python
from base_agent import Agent, AgentProfile, docker_coding_bundle
from base_agent.sandbox.docker import DockerSandboxConfig

coding = docker_coding_bundle(
    DockerSandboxConfig(
        image="python:3.12",
        network_enabled=False,
        command_timeout_seconds=60,
    )
)

agent = Agent(
    profile=AgentProfile(
        id="coding-agent",
        instructions="Write and test code only in the isolated workspace.",
        tools=coding.tool_names,
        permissions=coding.required_permissions,
    ),
    model=model,
    tools=coding.tools,
    resources=coding.resources,
)
```

The model can now write a file with `sandbox_write_text`, execute an argv array such as
`["python", "analysis.py"]`, inspect bounded stdout/stderr, and read generated text files. A shell is
not implied; `["/bin/sh", "-c", "..."]` is an explicit, auditable use of the broad execute
permission.

Use `coding_bundle(resource, ...)` with another `SandboxSession` implementation. Disable actions
that an Agent does not need:

```python
coding = coding_bundle(
    sandbox_resource,
    allow_read=True,
    allow_write=False,
    allow_execute=False,
)
```

`required_permissions` describes the permissions used by the selected Tools. The application still
chooses whether to place those permissions in `AgentProfile`.

## Security and current boundary

- Docker execution is opt-in and never opens a host Shell.
- The default Docker adapter has no network access, no host bind mount, and a disposable workspace.
- The configured image must already exist; the Runtime does not pull images.
- Files persist across model/Tool iterations in one uninterrupted execution segment.
- Entering `WAITING` releases the default Sandbox; resume starts a new workspace.
- `workspace_tools()` operates on an explicitly configured host directory, while the Docker
  CodingBundle operates on its own isolated filesystem. Safe project snapshot, Patch export, and
  approved Patch application remain separate future work.

See [Sandbox Sessions and Docker Adapter](SANDBOX.md) for the complete container boundary.
