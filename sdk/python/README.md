# Kebivo Python SDK

A synchronous, standard-library client for the [Kebivo REST feedback API](../../docs/rest-api.md). It is not published to PyPI.

Install from GitHub:

```sh
pip install "git+https://github.com/OCNAI/kebivo#subdirectory=sdk/python"
```

Use a project token with `feedback:gap` scope. Corrections additionally need `feedback:correction` scope and an enabled corrections policy.

```python
import os
from kebivo import Kebivo, KebivoError

client = Kebivo(os.environ["KEBIVO_URL"], os.environ["KEBIVO_TOKEN"], timeout=10)
try:
    accepted = client.report_gap("How do I reset my VPN?", tags=["it"], agent_id="support-bot")
    print(accepted["event_id"])
except KebivoError as error:
    print(f"Could not report feedback: {error}")
```

`submit_correction(question, correction, *, tags=..., source_reference=..., ...)` submits a proposed correction. Both methods accept the REST API's optional fields: `previous_answer`, `context`, `agent_id`, `conversation_id`, `source_event_id`, `user_reference`, and `metadata`. They return the accepted response dictionary (`event_id`, `status`, `processing_status`). They also accept `idempotency_key` when you need to retry the same report across separate method calls.

Each call generates an idempotency key unless you provide one. The key and body stay the same across up to four attempts. The client retries HTTP 429, HTTP 5xx, and connection failures with 1, 2, and 4 second backoff, waiting at least the server's `Retry-After` value (capped at 60 seconds) when present. Other HTTP 4xx errors fail immediately. Redirects are not followed, so the token is never sent to another URL.

`KebivoHTTPError` provides `status_code`, `code`, `message`, `details`, and `request_id`. `KebivoConnectionError` means no response arrived after retries. Both inherit `KebivoError`. Catch these errors at your application boundary so feedback failure does not interrupt the user's answer.

For LangChain or LangGraph retrieval callbacks, install the optional extra and see the [integration examples](../../docs/rest-api.md#langchain-and-langgraph).
