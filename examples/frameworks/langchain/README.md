# LangChain: report questions with no matching source

`agent.py` keeps two sample policy notes as LangChain `Document` objects. A `RunnableLambda` retrieves notes with shared keywords. When none match, Python's standard HTTP client sends a `knowledge_gap` event to Kebivo's REST API. No model or embedding service is needed.

From the repository root, run `ghcr.io/ocnai/kebivo:0.2.0` (the [quickstart](../../../docs/quickstart.md) explains setup with a source build), create a project token with `feedback:gap`, and run:

```sh
export KEBIVO_URL=http://127.0.0.1:8000
export KEBIVO_TOKEN='<PROJECT_TOKEN>'
uv run examples/frameworks/langchain/agent.py 'What is the hotel reimbursement limit?'
uv run examples/frameworks/langchain/agent.py 'How do I reset my corporate VPN?'
```

The first command returns the expense policy and `"reported": false`. The second returns `"reported": true` and an `event_id`; find the question in Kebivo's Inbox. Replace `NOTES` and the simple overlap rule with your own retrieval pipeline and a tested confidence rule. A keyword match is not proof that a document answers the question. The script submits each unmatched invocation as a separate event; add stable idempotency keys across retries in production.

## Tested

Against `ghcr.io/ocnai/kebivo:0.1.1` on 2026-10-26: the known question returned the expense note without reporting; the VPN question returned `reported: true` with an event ID. Hosted LLMs and vector search were not used. API pattern: [LangChain retrievers](https://docs.langchain.com/oss/python/langchain/knowledge-base).
