"""GitHub integration, web part: receives GitHub's webhooks.

GitHub calls POST /api/github/webhooks on the gateway, which forwards it here.
That route is public: GitHub has no user token, it proves who it is with the
signature instead. This part only checks and queues; the slow work (fetching
the diff, the AI review, posting it) happens in the worker, app/worker.py.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import ValidationError

from .config import settings
from .db import create_schema, make_engine, make_sessions
from .webhooks import HANDLERS, Delivery, signature_is_valid

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = make_engine(settings.database_url)
    await create_schema(engine)
    app.state.sessions = make_sessions(engine)
    yield
    await engine.dispose()


app = FastAPI(title="GitHub integration", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/github/webhooks", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(
    request: Request,
    x_github_event: Annotated[str, Header(max_length=100)],
    x_hub_signature_256: Annotated[str | None, Header()] = None,
) -> Delivery:
    secret = settings.github_webhook_secret
    if not secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub webhooks are not set up")
    # The gateway has already refused bodies over MAX_BODY_BYTES (1 MiB).
    body = await request.body()
    if not signature_is_valid(secret, body, x_hub_signature_256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid signature")
    if x_github_event == "ping":  # sent once, when the webhook is created
        return Delivery(status="pong")
    handler = HANDLERS.get(x_github_event)
    if handler is None:
        return Delivery(status="ignored", reason="this event is not used")
    try:
        return await handler(request.app.state.sessions, body, datetime.now(UTC))
    except ValidationError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unexpected payload") from None
