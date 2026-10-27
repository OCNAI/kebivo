"""The optional callback works with a retriever and no model or network."""

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import RunnableConfig

SDK_DIR = Path(__file__).resolve().parents[2] / "sdk/python/kebivo"
package_spec = importlib.util.spec_from_file_location(
    "kebivo_sdk_test", SDK_DIR / "__init__.py", submodule_search_locations=[str(SDK_DIR)]
)
assert package_spec and package_spec.loader
package = importlib.util.module_from_spec(package_spec)
sys.modules[package_spec.name] = package
package_spec.loader.exec_module(package)
callback_spec = importlib.util.spec_from_file_location(
    "kebivo_sdk_test.langchain", SDK_DIR / "langchain.py"
)
assert callback_spec and callback_spec.loader
callback_module = importlib.util.module_from_spec(callback_spec)
callback_spec.loader.exec_module(callback_module)


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
    handler = callback_module.KebivoCallbackHandler(client, project_tags=["support"])
    config: RunnableConfig = {"callbacks": [handler]}
    assert FakeRetriever(documents=[]).invoke("Where is the VPN guide?", config=config) == []
    assert FakeRetriever(documents=[Document(page_content="Guide")]).invoke(
        "Where is the handbook?", config=config
    ) == [Document(page_content="Guide")]
    assert client.calls == [("Where is the VPN guide?", ["support"])]
    assert handler._queries == {}


def test_reporting_failure_does_not_break_retriever(monkeypatch: Any) -> None:
    client = FakeClient(fails=True)
    handler = callback_module.KebivoCallbackHandler(client)
    logged = Mock()
    monkeypatch.setattr(callback_module.logger, "exception", logged)
    result = FakeRetriever(documents=[]).invoke("Missing answer", config={"callbacks": [handler]})
    assert result == []
    assert client.calls == [("Missing answer", None)]
    logged.assert_called_once_with("Could not report Kebivo retrieval gap")
