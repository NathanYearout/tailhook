import os

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
