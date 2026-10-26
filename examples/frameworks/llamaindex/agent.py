# /// script
# requires-python = ">=3.10"
# dependencies = ["llama-index-core>=0.14"]
# ///
"""Retrieve a policy note with LlamaIndex; report an unmatched question to Kebivo."""

import argparse
import json
import os
import re
import urllib.request
import uuid

from llama_index.core import Document, SummaryIndex

NOTES = [
    Document(
        text="The expense policy allows hotel reimbursement up to $180 per night.",
        metadata={"source": "expense-policy"},
    ),
    Document(
        text="The travel policy requires manager approval before booking flights.",
        metadata={"source": "travel-policy"},
    ),
]


STOP = {
    "a",
    "an",
    "and",
    "are",
    "can",
    "do",
    "does",
    "for",
    "how",
    "i",
    "is",
    "my",
    "of",
    "the",
    "to",
    "what",
}


def terms(text):
    return set(re.findall(r"[a-z0-9]+", text.lower())) - STOP


def report(question):
    token = os.environ.get("KEBIVO_TOKEN")
    if not token:
        raise SystemExit("Set KEBIVO_TOKEN to a project token with feedback:gap")
    body = json.dumps(
        {
            "event_type": "knowledge_gap",
            "question": question,
            "context": "No matching policy note in the LlamaIndex example.",
            "tags": ["llamaindex"],
        }
    ).encode()
    request = urllib.request.Request(
        os.environ.get("KEBIVO_URL", "http://127.0.0.1:8000").rstrip("/") + "/api/v1/feedback",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question")
    args = parser.parse_args()
    candidates = SummaryIndex.from_documents(NOTES).as_retriever().retrieve(args.question)
    matches = [item for item in candidates if terms(args.question) & terms(item.node.get_content())]
    if matches:
        note = matches[0].node
        print(
            json.dumps(
                {"source": note.metadata["source"], "answer": note.get_content(), "reported": False}
            )
        )
    else:
        result = report(args.question)
        print(
            json.dumps(
                {
                    "answer": "I couldn't find a matching policy note.",
                    "reported": result["status"] == "accepted",
                    "event_id": result["event_id"],
                }
            )
        )


if __name__ == "__main__":
    main()
