import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from moto import mock_aws

from app import aws
from tests.aws_fixtures import make_queues, make_table

# Never let a test reach a real account.
os.environ.pop("AWS_PROFILE", None)
os.environ.update(
    AWS_DEFAULT_REGION="us-east-2",
    AWS_ACCESS_KEY_ID="testing",
    AWS_SECRET_ACCESS_KEY="testing",
    TABLE_NAME="tailhook-events",
    INBOUND_SECRET="whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw",
    OUTBOUND_SECRET="whsec_c2VjcmV0LWZvci1kZWxpdmVyaWVzLXRvLXRoZS1hcHA=",
)


@pytest.fixture(autouse=True)
def aws_mock():
    with mock_aws():
        aws.reset()
        make_table(os.environ["TABLE_NAME"])
        os.environ["QUEUE_URL"], os.environ["DLQ_URL"] = make_queues()
        yield
    aws.reset()


@pytest.fixture
def destination(monkeypatch):
    """A local stand-in for the merchant's app. It records every request and answers 200,
    unless a status code has been pushed onto `replies`."""
    received, replies = [], []

    class App(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["content-length"])).decode()
            received.append(({k.lower(): v for k, v in self.headers.items()}, body))
            status = replies.pop(0) if replies else 200
            self.send_response(status)
            if 300 <= status < 400:
                self.send_header("location", "/somewhere-else")
            self.end_headers()
            self.wfile.write(b"ok" if status < 400 else b"boom")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), App)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    monkeypatch.setenv("DESTINATION_URL", url)
    yield SimpleNamespace(url=url, received=received, replies=replies)
    server.shutdown()
