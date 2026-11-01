from collections.abc import Callable, Coroutine
from typing import Final

from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute

MAX_JSON_WHITESPACE: Final = 65_536
# Escaped astral characters: 12*(5000+50000+200000+200+2000) + settings4096 + JSON1024 + whitespace65536.
MAX_CREATION_BYTES: Final = 12 * (5000 + 50000 + 200000 + 200 + 2000) + 4096 + 1024 + MAX_JSON_WHITESPACE


class JsonDepthValidator:
    def __init__(self) -> None:
        self.depth = 0
        self.quoted = False
        self.escaped = False
        self.whitespace = 0

    def feed(self, chunk: bytes) -> None:
        for byte in chunk:
            if self.quoted:
                if self.escaped:
                    self.escaped = False
                elif byte == 92:
                    self.escaped = True
                elif byte == 34:
                    self.quoted = False
            elif byte == 34:
                self.quoted = True
            elif byte in (9, 10, 13, 32):
                self.whitespace += 1
                if self.whitespace > MAX_JSON_WHITESPACE:
                    raise HTTPException(status_code=422, detail=[{
                        "loc": ["body"], "type": "settings_whitespace",
                        "msg": "Insignificant JSON whitespace must not exceed 65536 bytes",
                    }])
            elif byte in (123, 91):
                self.depth += 1
                if self.depth > 16:
                    raise HTTPException(status_code=422, detail=[{
                        "loc": ["body", "settings"], "type": "settings_depth",
                        "msg": "JSON nesting must not exceed 16 containers",
                    }])
            elif byte in (125, 93):
                self.depth -= 1


class ProjectSettingsRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        handler = super().get_route_handler()
        creation = self.path.endswith("/projects") and "POST" in self.methods
        if creation or (self.path.endswith("/settings") and "PUT" in self.methods):
            async def bounded_handler(request: Request) -> Response:
                budget = MAX_CREATION_BYTES if creation else 4096
                body = bytearray()
                depth = JsonDepthValidator()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > budget:
                        raise HTTPException(status_code=422, detail=[{
                            "loc": ["body"], "type": "settings_size",
                            "msg": f"Request body must not exceed {budget} bytes",
                        }])
                    depth.feed(chunk)
                    body.extend(chunk)
                request._body = bytes(body)
                return await handler(request)

            return bounded_handler
        return handler
