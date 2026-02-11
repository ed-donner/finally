"""SPA-aware static file serving for FinAlly.

Subclasses StaticFiles to return index.html for unknown paths,
enabling client-side routing in the Next.js static export.
"""

from starlette.staticfiles import StaticFiles


class SPAStaticFiles(StaticFiles):
    """Static file server with SPA fallback.

    Any path that doesn't match a real file returns index.html,
    so the frontend router can handle the URL.
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except Exception as exc:
            from starlette.exceptions import HTTPException

            if isinstance(exc, HTTPException) and exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise
