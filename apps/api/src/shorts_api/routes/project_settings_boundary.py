from collections.abc import Callable, Coroutine

from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute
from starlette.types import Message


class ProjectSettingsRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        handler = super().get_route_handler()
        if self.path.endswith("/settings") and "PUT" in self.methods:
            async def bounded_handler(request: Request) -> Response:
                size = 0

                async def receive() -> Message:
                    nonlocal size
                    message = await request.receive()
                    size += len(message.get("body", b""))
                    if size > 4096:
                        raise HTTPException(status_code=422, detail=[{
                            "loc": ["body"], "type": "settings_size",
                            "msg": "Settings body must not exceed 4096 bytes",
                        }])
                    return message

                try:
                    return await handler(Request(request.scope, receive=receive))
                except RecursionError as error:
                    raise HTTPException(status_code=422, detail=[{
                        "loc": ["body"], "type": "settings_depth",
                        "msg": "Settings must be a flat JSON object",
                    }]) from error

            return bounded_handler
        return handler
