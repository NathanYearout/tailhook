"""AWS handles, created on first use so tests can start moto before anything connects."""

import os
from functools import cache

import boto3


@cache
def table():
    return boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


@cache
def sqs():
    return boto3.client("sqs")


def reset() -> None:
    table.cache_clear()
    sqs.cache_clear()
