# Kebivo Python SDK

A synchronous, standard-library client for the [Kebivo REST feedback API](../../docs/rest-api.md). The distribution is `kebivo-client` and the import package is `kebivo_client`. It is not published to PyPI.

Install from GitHub:

```sh
pip install "git+https://github.com/OCNAI/kebivo#subdirectory=sdk/python"
```

Use a `feedback:gap`-only token for gap reporting, with one token per service. Keep tokens server side, use HTTPS, and revoke a token immediately if it leaks. Corrections additionally need `feedback:correction` scope and an enabled corrections policy.

```python
import os
from kebivo_client import Kebivo, KebivoError

client = Kebivo(os.environ["KEBIVO_URL"], os.environ["KEBIVO_TOKEN"], timeout=10)
try:
    accepted = client.report_gap("How do I reset my VPN?", tags=["it"], agent_id="support-bot")
    print(accepted["event_id"])
except KebivoError as error:
    print(f"Could not report feedback: {error}")
```

`submit_correction(question, correction, *, tags=..., source_reference=..., ...)` submits a proposed correction. Both methods accept the REST API's optional fields: `previous_answer`, `context`, `agent_id`, `conversation_id`, `source_event_id`, `user_reference`, and `metadata`. They return the accepted response dictionary (`event_id`, `status`, `processing_status`). They also accept `idempotency_key` when you need to retry the same report across separate method calls.

Each call generates an idempotency key unless you provide one. The key and body stay the same across up to four attempts. The client retries HTTP 429, HTTP 5xx, and connection failures with 1, 2, and 4 second backoff, waiting at least the server's `Retry-After` value (capped at 60 seconds) when present. Other HTTP 4xx errors fail immediately. Redirects are not followed, so the token is never sent to another URL. `timeout` is a total deadline for each attempt, including connection, response headers, and body. A synchronous caller may block for up to four attempts × `timeout` + three capped `Retry-After` waits (up to 60 seconds each); the usual backoffs are 1, 2, and 4 seconds. DNS lookup performed by the operating system can exceed the connection deadline.

`KebivoHTTPError` provides `status_code`, `code`, `message`, `details`, and `request_id`. `KebivoConnectionError` means no response arrived; deadline expiry stops immediately, while other connection failures are retried. Both inherit `KebivoError`. Invalid input raises `KebivoValidationError` before a request. Catch these errors at your application boundary so feedback failure does not interrupt the user's answer.

For LangChain or LangGraph retrieval callbacks, install the `kebivo-client[langchain]` optional extra and see the [integration examples](../../docs/rest-api.md#langchain-and-langgraph).

The LangChain callback buffers at most 100 pending reports and sends them with one daemon worker. When the queue is full it drops new reports with a warning. Call `handler.flush(timeout=1)` before shutdown when delivery matters; it returns whether all accepted reports finished. Process exit also makes a best-effort one-second flush. Create one handler and reuse it; each handler starts its own worker.
