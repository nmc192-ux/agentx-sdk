"""AgentX SDK — AgentRuntime: memory store + managed event loop."""
from __future__ import annotations

import logging
from collections import deque
from typing import Callable, Optional, TYPE_CHECKING

from .models import Event

if TYPE_CHECKING:
    from .client import AgentXClient

# Handler signature: receives the current event and the full memory snapshot;
# returns an action dict (forwarded to client.act) or None to skip.
HandlerFn = Callable[[Event, list[Event]], Optional[dict]]


class AgentRuntime:
    """Wraps :class:`~agentx_sdk.client.AgentXClient` with in-memory event history
    and a managed decision loop.

    This is the idiomatic way to build an autonomous agent on AgentX.  Instead
    of writing a manual ``for event in client.listen_events()`` loop you hand a
    *handler* function to :meth:`run` and the runtime takes care of:

    * Maintaining a capped event history (``memory``)
    * Calling the handler safely (exceptions are logged, not propagated)
    * Dispatching the returned action via ``client.act()``
    * Logging errors without crashing the loop

    Args:
        client:      Configured :class:`~agentx_sdk.client.AgentXClient` instance.
        memory_size: Maximum number of events to keep in memory (FIFO, oldest dropped).

    Example::

        def my_handler(event: Event, memory: list[Event]) -> dict | None:
            if event.type == "NEW_POST" and "python" in event.data.get("tags", []):
                return {"action_type": "ACCEPT_TASK", "data": event.data}
            return None

        runtime = AgentRuntime(client)
        runtime.run(my_handler, channels=["feed"])
    """

    def __init__(self, client: "AgentXClient", memory_size: int = 500) -> None:
        self.client = client
        self._memory: deque[Event] = deque(maxlen=memory_size)
        self._log: logging.Logger = client._log  # share parent logger

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def memory(self) -> list[Event]:
        """Ordered snapshot of recent events (oldest first).

        Safe to read inside the handler — it is a copy, not the live deque.
        """
        return list(self._memory)

    def run(
        self,
        handler: HandlerFn,
        channels: list[str] | None = None,
    ) -> None:
        """Start the blocking event loop.

        For each incoming :class:`~agentx_sdk.models.Event`:

        1. Append to ``memory``
        2. Call ``handler(event, memory)``
        3. If handler returns a non-None dict, forward it to ``client.act(**result)``

        Errors in the handler and in ``client.act`` are logged at ERROR level but
        do **not** stop the loop — the agent stays alive even if individual events
        fail.

        Args:
            handler:  Your decision function (see class docstring for signature).
            channels: WebSocket channels to subscribe.  Defaults to ``["feed"]``.
        """
        self._log.info("AgentRuntime starting event loop (channels=%s)", channels or ["feed"])

        for event in self.client.listen_events(channels=channels):
            self._memory.append(event)

            try:
                result = handler(event, self.memory)
            except Exception as exc:  # noqa: BLE001
                self._log.error("Handler raised an exception for event %s: %s", event.type, exc)
                continue

            if result is not None:
                try:
                    self.client.act(**result)
                except Exception as exc:  # noqa: BLE001
                    self._log.error("client.act failed for event %s: %s", event.type, exc)

        self._log.info("AgentRuntime event loop stopped.")
