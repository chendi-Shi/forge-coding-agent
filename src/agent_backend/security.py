from starlette.responses import JSONResponse

from agent_backend.settings import identity_from_header


class BearerGuard:
    """An outer HTTP guard in addition to Aegra's identity-aware authorization."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        public = {"/", "/studio", "/health", "/ok", "/docs", "/openapi.json", "/docs/oauth2-redirect"}
        if scope["type"] == "http" and scope["path"] not in public and not scope["path"].startswith("/studio/static/"):
            headers = dict(scope.get("headers", []))
            header = headers.get(b"authorization", b"").decode("latin1")
            if identity_from_header(header) is None:
                response = JSONResponse(
                    {"detail": "Valid bearer token required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
