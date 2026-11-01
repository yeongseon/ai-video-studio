from collections.abc import Callable, Coroutine

from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute


class JsonDepthValidator:
    def __init__(self) -> None:
        self.depth = 0
        self.quoted = False
        self.escaped = False

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
                budget = 900_000 if creation else 4096
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
