# Model Providers

The core depends only on the `ModelProvider` protocol. Provider SDKs are optional adapters and may
not leak their response types into Runtime models.

## OpenAI-compatible Chat Completions

Install the optional dependency:

```bash
uv add 'base-agent[openai]'
```

Configure the official OpenAI endpoint through `OPENAI_API_KEY`:

```python
from base_agent import Agent, AgentProfile, OpenAIChatProvider

provider = OpenAIChatProvider(model="your-model-id")
agent = Agent(
    profile=AgentProfile(
        id="assistant",
        instructions="Answer clearly.",
    ),
    model=provider,
)
```

The official SDK reads `OPENAI_API_KEY` when `api_key` is omitted. Do not store keys in Python,
Skill files, TOML configuration, examples, or Git history.

## Attachments

`ModelRequest.attachments` contains structured references rather than binary payloads. Provider
adapters must resolve and map them using an application-authorized content mechanism or reject
them explicitly. `OpenAIChatProvider` currently raises `UnsupportedAttachmentError`; use an
attachment-capable Provider adapter or process the input through Tools.

`ModelRequest.memories` follows the same explicit capability rule. `OpenAIChatProvider` raises
`UnsupportedMemoryError`; a memory-capable Provider must deliberately map the structured matches.

For an OpenAI-compatible gateway:

```python
provider = OpenAIChatProvider(
    model="gateway-model",
    api_key=os.environ["GATEWAY_API_KEY"],
    base_url=os.environ["GATEWAY_BASE_URL"],
    timeout=60,
    max_retries=2,
)
```

The adapter supports:

- async Chat Completions;
- system, user, assistant, and Tool messages;
- multiple function ToolCalls in one response;
- `none`, `auto`, and `required` Tool choice;
- prompt, completion, and total token usage;
- request ID, response model, and system fingerprint metadata;
- refusal text normalization;
- configurable temperature and maximum completion tokens.

The first adapter is intentionally non-streaming. It does not forward arbitrary
`ModelRequest.metadata` to the remote endpoint.

Chat Completions was selected for this adapter because it remains supported by the official SDK
and is commonly implemented by OpenAI-compatible gateways. A future Responses API adapter should
be a separate Provider rather than adding endpoint-specific conditionals to this class.

Official references:

- [OpenAI Python SDK](https://github.com/openai/openai-python)
- [Chat Completions create parameters](https://github.com/openai/openai-python/blob/main/src/openai/types/chat/completion_create_params.py)
