"""Provider-neutral Tool invocation errors."""


class ToolInvalidArgumentsError(ValueError):
    """A non-function Tool rejected arguments before invoking its backend."""
