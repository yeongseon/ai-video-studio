"""Suppress SDK diagnostics only inside adapter operations, at record creation sites."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator

_private_operation: ContextVar[bool] = ContextVar("private_storage_operation", default=False)


class StorageSDKFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _private_operation.get()


_filter = StorageSDKFilter()


@contextmanager
def private_sdk_operation() -> Iterator[None]:
    # Logger filters run before ANY handler, unlike root-logger filters which
    # do not see propagated child records. ContextVars isolate concurrent callers.
    for name in tuple(logging.Logger.manager.loggerDict):
        if name.startswith(("azure.", "botocore.", "boto3.", "urllib3.")):
            logger = logging.getLogger(name)
            if _filter not in logger.filters:
                logger.addFilter(_filter)
    token = _private_operation.set(True)
    try:
        yield
    finally:
        _private_operation.reset(token)
