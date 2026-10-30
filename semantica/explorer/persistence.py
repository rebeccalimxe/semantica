"""Optional file persistence for the Explorer's in-memory graph.

Set ``SEMANTICA_GRAPH_PATH`` to a JSON file path and the Explorer loads that
file at start-up, saves it every ``SEMANTICA_GRAPH_SAVE_INTERVAL`` seconds when
the graph has changed, and saves it once more on shutdown. On Cloud Run the path
can sit on a mounted Cloud Storage volume so the graph survives instance
restarts. Only the graph (nodes and edges) is persisted; annotations and
provenance are not (provenance has its own ``SEMANTICA_PROVENANCE_DB``).

Run a single instance when this is enabled: each instance holds its own copy of
the graph, so two of them would overwrite each other's saves.
"""

import asyncio
import contextlib
import logging
import os
from typing import Optional

from .session import GraphSession

logger = logging.getLogger(__name__)

DEFAULT_SAVE_INTERVAL = 30.0


def graph_path_from_env() -> Optional[str]:
    """Return the graph file path named by the environment, if any."""
    raw = os.environ.get("SEMANTICA_GRAPH_PATH", "").strip()
    return raw or None


def save_interval_from_env() -> float:
    """Return the autosave interval in seconds (``<= 0`` disables the timer)."""
    raw = os.environ.get("SEMANTICA_GRAPH_SAVE_INTERVAL", "").strip()
    if not raw:
        return DEFAULT_SAVE_INTERVAL
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "Ignoring invalid SEMANTICA_GRAPH_SAVE_INTERVAL=%r; using %ss",
            raw,
            DEFAULT_SAVE_INTERVAL,
        )
        return DEFAULT_SAVE_INTERVAL


class GraphPersistence:
    """Load a graph file at start-up and keep it saved while the app runs."""

    def __init__(
        self,
        session: GraphSession,
        path: str,
        interval: float = DEFAULT_SAVE_INTERVAL,
    ) -> None:
        self.session = session
        self.path = path
        self.interval = interval
        self._saved_revision: Optional[int] = None
        self._task: Optional["asyncio.Task[None]"] = None

    def load(self) -> None:
        """Replace the session graph with the saved file, if it exists.

        A file that exists but cannot be parsed raises instead of starting
        empty: the autosave would otherwise overwrite it with an empty graph.
        """
        if os.path.exists(self.path):
            try:
                self.session.graph.load_from_file(self.path)
            except Exception as exc:
                raise RuntimeError(
                    f"Could not load graph from SEMANTICA_GRAPH_PATH={self.path!r}; "
                    "fix or remove the file to start the Explorer"
                ) from exc
            self.session.rebuild_search_index()
            logger.info(
                "Loaded graph from %s (%d nodes)",
                self.path,
                len(self.session.graph.nodes),
            )
        else:
            logger.info("No graph file at %s yet; starting empty", self.path)
        self._saved_revision = self.session.graph_revision

    def save(self, force: bool = False) -> bool:
        """Write the graph to disk if it changed since the last save."""
        revision = self.session.graph_revision
        if not force and revision == self._saved_revision:
            return False
        self.session.graph.save_to_file(self.path)
        self._saved_revision = revision
        return True

    def start(self) -> None:
        """Begin the periodic autosave (needs a running event loop)."""
        if self.interval > 0 and self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        """Stop the autosave and write a final save."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        try:
            await asyncio.to_thread(self.save)
        except Exception:
            logger.exception("Final graph save to %s failed", self.path)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await asyncio.to_thread(self.save)
            except Exception:
                # Keep the loop alive; the next tick retries, since a failed
                # save leaves _saved_revision unchanged.
                logger.exception("Autosave of graph to %s failed", self.path)
