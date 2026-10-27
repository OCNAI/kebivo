"""The optional callback works with a retriever and no model or network."""

import threading
from typing import Any, cast
from unittest.mock import Mock

from kebivo_client import Kebivo
from kebivo_client import langchain as callback_module
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import RunnableConfig


class FakeRetriever(BaseRetriever):
    documents: list[Document]

    def _get_relevant_documents(self, query: str, *, run_manager: Any) -> list[Document]:
        return self.documents


class FakeClient:
    def __init__(self, fails: bool = False) -> None:
        self.calls: list[tuple[str, list[str] | None]] = []
        self.fails = fails

    def report_gap(self, question: str, *, tags: list[str] | None = None) -> None:
        self.calls.append((question, tags))
        if self.fails:
            raise OSError("Kebivo unavailable")


def test_reports_only_empty_retrieval_and_uses_query() -> None:
    client = FakeClient()
    handler = callback_module.KebivoCallbackHandler(cast(Kebivo, client), project_tags=["support"])
    config: RunnableConfig = {"callbacks": [handler]}
    assert FakeRetriever(documents=[]).invoke("Where is the VPN guide?", config=config) == []
    assert FakeRetriever(documents=[Document(page_content="Guide")]).invoke(
        "Where is the handbook?", config=config
    ) == [Document(page_content="Guide")]
    assert handler.flush(5)
    assert client.calls == [("Where is the VPN guide?", ["support"])]
    assert handler._queries == {}


def test_reporting_failure_does_not_break_retriever(monkeypatch: Any) -> None:
    client = FakeClient(fails=True)
    handler = callback_module.KebivoCallbackHandler(cast(Kebivo, client))
    logged = Mock()
    monkeypatch.setattr(callback_module.logger, "exception", logged)
    result = FakeRetriever(documents=[]).invoke("Missing answer", config={"callbacks": [handler]})
    assert result == []
    assert handler.flush(5)
    assert client.calls == [("Missing answer", None)]
    logged.assert_called_once_with("Could not report Kebivo retrieval gap")


def test_slow_reporting_does_not_delay_retriever() -> None:
    release = threading.Event()
    client = Mock()
    client.report_gap.side_effect = lambda *args, **kwargs: release.wait(5)
    handler = callback_module.KebivoCallbackHandler(cast(Kebivo, client))
    assert FakeRetriever(documents=[]).invoke("Slow", config={"callbacks": [handler]}) == []
    release.set()
    assert handler.flush(5)
    client.report_gap.assert_called_once_with("Slow", tags=None)


def test_queue_drops_without_blocking(monkeypatch: Any) -> None:
    from uuid import uuid4

    release = threading.Event()
    started = threading.Event()
    client = Mock()

    def block(*_args: Any, **_kwargs: Any) -> None:
        started.set()
        release.wait(5)

    client.report_gap.side_effect = block
    handler = callback_module.KebivoCallbackHandler(cast(Kebivo, client))
    warning = Mock()
    monkeypatch.setattr(callback_module.logger, "warning", warning)
    try:
        first = uuid4()
        handler.on_retriever_start({}, "first", run_id=first)
        handler.on_retriever_end([], run_id=first)
        assert started.wait(1)
        for index in range(101):
            run_id = uuid4()
            handler.on_retriever_start({}, str(index), run_id=run_id)
            handler.on_retriever_end([], run_id=run_id)
        warning.assert_called_once_with("Kebivo report queue full; dropping retrieval gap")
        assert not handler.flush(0.01)
    finally:
        release.set()
        assert handler.flush(5)
    assert client.report_gap.call_count == 101
