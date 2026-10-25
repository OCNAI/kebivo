"""AC-15: the stack runs with no internet and makes no outbound call.

`scripts/e2e.sh --offline` runs the whole suite on an `internal: true` Docker
network, so every acceptance test above doubles as AC-15 evidence there. This
file adds the secrets that only exist to be swept, and checks the network
state; the final DNS and isolation checks are in test_zz_sweep.py.
"""

import os
import socket

import pytest
from conftest import MODE, Admin, record_secret, unique


def test_ac_15_stores_destination_secrets_for_the_sweep(admin: Admin) -> None:
    record_secret("admin_password", os.environ["E2E_ADMIN_PASSWORD"])
    bearer, header, path = unique("E2E-WEBHOOK-BEARER"), unique("E2E-WEBHOOK-HEADER"), unique("p")
    smtp_password = unique("E2E-SMTP-PASSWORD")
    for kind, value in (("webhook_bearer", bearer), ("webhook_header", header),
                        ("smtp_password", smtp_password)):
        record_secret(kind, value)
    admin.webhook(path, config={"auth": {"type": "bearer", "token": bearer},
                                "custom_headers": {"X-E2E-Canary": header}})
    admin.smtp(tls_mode="starttls", username="e2e-user", password=smtp_password)


@pytest.mark.skipif(MODE != "offline", reason="only the offline run removes the network")
def test_ac_15_runner_has_no_internet() -> None:
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
