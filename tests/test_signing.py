import pytest

from app import signing

# The example from the Standard Webhooks spec.
SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
MSG_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
TIMESTAMP = 1614265330
BODY = '{"test": 2432232314}'
EXPECTED = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def headers(signature=EXPECTED, timestamp=TIMESTAMP):
    return {
        "webhook-id": MSG_ID,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": signature,
    }


def test_sign_spec_example():
    assert signing.sign(SECRET, MSG_ID, TIMESTAMP, BODY) == EXPECTED


def test_verify_valid():
    signing.verify(SECRET, headers(), BODY, now=TIMESTAMP)


def test_verify_tampered_body():
    with pytest.raises(signing.InvalidSignature):
        signing.verify(SECRET, headers(), BODY.replace("2", "3"), now=TIMESTAMP)


def test_verify_timestamp_window():
    for now in (TIMESTAMP + signing.TOLERANCE + 1, TIMESTAMP - signing.TOLERANCE - 1):
        with pytest.raises(signing.InvalidSignature):
            signing.verify(SECRET, headers(), BODY, now=now)


def test_verify_key_rotation():
    rotated = f"v1,bm90IHRoZSByaWdodCBvbmU= {EXPECTED}"
    signing.verify(SECRET, headers(rotated), BODY, now=TIMESTAMP)


def test_verify_bad_timestamp():
    with pytest.raises(signing.InvalidSignature):
        signing.verify(SECRET, headers(timestamp="soon"), BODY, now=TIMESTAMP)


def test_verify_missing_headers():
    with pytest.raises(signing.InvalidSignature):
        signing.verify(SECRET, {"webhook-id": MSG_ID}, BODY, now=TIMESTAMP)


def test_signed_headers_round_trip():
    signed = signing.signed_headers(SECRET, "msg_1", BODY)
    assert signed["webhook-id"] == "msg_1"
    assert signed["content-type"] == "application/json"
    signing.verify(SECRET, signed, BODY)
