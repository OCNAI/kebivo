"""Report empty LangChain retrievals as Kebivo knowledge gaps."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from threading import Lock, Thread
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.documents import Document

from . import Kebivo

logger = logging.getLogger(__name__)


class KebivoCallbackHandler(BaseCallbackHandler):
    """Report a gap when a retriever returns no documents.

    Reports are sent from a background thread so client retries never delay retrieval.
    """

    def __init__(self, client: Kebivo, project_tags: list[str] | None = None) -> None:
        self.client = client
        self.project_tags = project_tags
        self._queries: dict[UUID, str] = {}
        self._lock = Lock()

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
                Thread(
                    target=self._report, args=(query,), name="kebivo-report", daemon=True
                ).start()
        except Exception:
            logger.exception("Could not report Kebivo retrieval gap")

    def _report(self, query: str) -> None:
        try:
            self.client.report_gap(query, tags=self.project_tags)
        except Exception:
            logger.exception("Could not report Kebivo retrieval gap")

    def on_retriever_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            with self._lock:
                self._queries.pop(run_id, None)
        except Exception:
            logger.exception("Could not clear Kebivo retriever query")
