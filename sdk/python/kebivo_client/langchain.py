"""Report empty LangChain retrievals as Kebivo knowledge gaps."""

from __future__ import annotations

import atexit
import logging
from collections.abc import Sequence
from queue import Full, Queue
from threading import Condition, Lock, Thread
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.documents import Document

from . import Kebivo

logger = logging.getLogger(__name__)


class KebivoCallbackHandler(BaseCallbackHandler):
    """Report a gap when a retriever returns no documents.

    Reports are sent by one background worker so client retries never delay retrieval.
    """

    def __init__(self, client: Kebivo, project_tags: list[str] | None = None) -> None:
        self.client = client
        self.project_tags = project_tags
        self._queries: dict[UUID, str] = {}
        self._lock = Lock()
        self._queue: Queue[str] = Queue(maxsize=100)
        self._idle = Condition()
        self._pending = 0
        Thread(target=self._run, name="kebivo-report", daemon=True).start()
        atexit.register(self.flush, 1.0)

    def on_retriever_start(
        self, serialized: dict[str, Any], query: str, *, run_id: UUID, **kwargs: Any
    ) -> None:
        try:
            with self._lock:
                self._queries[run_id] = query
        except Exception:
            logger.exception("Could not track Kebivo retriever query")

    def on_retriever_end(
        self, documents: Sequence[Document], *, run_id: UUID, **kwargs: Any
    ) -> None:
        try:
            with self._lock:
                query = self._queries.pop(run_id, None)
            if not documents and query:
                with self._idle:
                    try:
                        self._queue.put_nowait(query)
                    except Full:
                        logger.warning("Kebivo report queue full; dropping retrieval gap")
                    else:
                        self._pending += 1
        except Exception:
            logger.exception("Could not report Kebivo retrieval gap")

    def _report(self, query: str) -> None:
        try:
            self.client.report_gap(query, tags=self.project_tags)
        except Exception:
            logger.exception("Could not report Kebivo retrieval gap")

    def _run(self) -> None:
        while True:
            query = self._queue.get()
            try:
                self._report(query)
            finally:
                with self._idle:
                    self._pending -= 1
                    self._idle.notify_all()

    def flush(self, timeout: float = 1.0) -> bool:
        """Wait up to timeout seconds for accepted reports to finish."""
        with self._idle:
            return self._idle.wait_for(lambda: self._pending == 0, timeout)

    def on_retriever_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            with self._lock:
                self._queries.pop(run_id, None)
        except Exception:
            logger.exception("Could not clear Kebivo retriever query")
