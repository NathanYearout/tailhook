import os
import socket
import threading
from datetime import UTC, datetime

from app import delivery, signing, store
from app.simulator import event_body

T0 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)


def stored_event():
    item, _ = store.record_receipt("msg_1", event_body("payment.succeeded", "pay_1", T0), "r1")
    return item


def test_a_delivery_is_re_signed_and_recorded(destination):
    item = stored_event()

    assert delivery.deliver(item, failed_status="retrying")

    headers, sent = destination.received[0]
    assert sent == item["payload"]
    assert headers["webhook-id"] == "msg_1"
    signing.verify(os.environ["OUTBOUND_SECRET"], headers, sent)
    saved = store.get_event("msg_1")
    assert saved["status"] == "delivered"
    assert saved["attempts"][0]["status_code"] == 200
    assert saved["attempts"][0]["body"] == "ok"
    assert saved["attempts"][0]["replay"] is False


def test_an_error_response_is_a_failed_attempt(destination):
    destination.replies.append(500)

    assert not delivery.deliver(stored_event(), failed_status="retrying", replay=True)

    saved = store.get_event("msg_1")
    assert saved["status"] == "retrying"
    assert saved["attempts"][0]["status_code"] == 500
    assert saved["attempts"][0]["body"] == "boom"
    assert saved["attempts"][0]["replay"] is True


def test_an_unreachable_app_is_a_failed_attempt(monkeypatch):
    monkeypatch.setenv("DESTINATION_URL", "http://127.0.0.1:9/")

    assert not delivery.deliver(stored_event(), failed_status="dead")

    saved = store.get_event("msg_1")
    assert saved["status"] == "dead"
    assert saved["attempts"][0]["status_code"] == 0


def test_a_redirect_is_a_failed_attempt_and_is_not_followed(destination):
    destination.replies.append(302)

    assert not delivery.deliver(stored_event(), failed_status="retrying")

    assert len(destination.received) == 1
    assert store.get_event("msg_1")["attempts"][0]["status_code"] == 302


def test_a_reply_that_isnt_http_is_a_failed_attempt(monkeypatch):
    server = socket.create_server(("127.0.0.1", 0))

    def answer():
        connection, _ = server.accept()
        connection.recv(65536)
        connection.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
        connection.close()

    threading.Thread(target=answer, daemon=True).start()
    monkeypatch.setenv("DESTINATION_URL", f"http://127.0.0.1:{server.getsockname()[1]}/")

    assert not delivery.deliver(stored_event(), failed_status="retrying")
    assert store.get_event("msg_1")["attempts"][-1]["status_code"] == 0
    server.close()
