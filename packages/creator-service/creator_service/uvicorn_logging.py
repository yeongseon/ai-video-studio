"""Access-target sanitization without a dependency on Uvicorn."""

import logging


class UvicornAccessFilter(logging.Filter):
    """Remove queries from Uvicorn's five-argument access records before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if type(args) is tuple and len(args) == 5 and type(args[2]) is str:
            record.args = (*args[:2], args[2].partition("?")[0], *args[3:])
        return True
