"""Read-only observer contract; observers cannot change execution."""

from collections.abc import Awaitable, Callable

from base_agent.models import Event

Observer = Callable[[Event], None | Awaitable[None]]
