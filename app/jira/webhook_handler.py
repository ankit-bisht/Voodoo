from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from app.config.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


# ---------------------------------------------------------------------------
# Trigger conditions
# ---------------------------------------------------------------------------

_TRIGGER_STATUS = "Ready for Dev"
_TRIGGER_LABEL = "auto-dev"


def _should_process(payload: dict[str, Any]) -> bool:
    """Return True when the event matches our trigger conditions."""
    issue = payload.get("issue", {})
    fields = issue.get("fields", {})
    status: str = fields.get("status", {}).get("name", "")
    labels: list[str] = fields.get("labels", [])
    event: str = payload.get("webhookEvent", "")

    # Only act on issue-related events
    if "issue" not in event:
        return False

    return status == _TRIGGER_STATUS or _TRIGGER_LABEL in labels


# ---------------------------------------------------------------------------
# Signature verification (optional – only active when secret is configured)
# ---------------------------------------------------------------------------

def _verify_signature(raw_body: bytes, signature_header: str | None) -> None:
    secret = settings.jira_webhook_secret
    if not secret:
        return  # verification disabled

    if not signature_header:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature header")

    expected = "sha256=" + hmac.new(
        secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.post("/jira")
async def jira_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature: str | None = Header(default=None),
) -> dict[str, str]:
    raw_body = await request.body()
    _verify_signature(raw_body, x_hub_signature)

    payload: dict[str, Any] = await request.json()
    issue_key: str = payload.get("issue", {}).get("key", "")

    if not issue_key:
        logger.warning("Received webhook with no issue key – ignoring")
        return {"status": "ignored", "reason": "no issue key"}

    if not _should_process(payload):
        logger.info("Issue %s does not meet trigger conditions – ignoring", issue_key)
        return {"status": "ignored", "reason": "trigger conditions not met"}

    logger.info("Queuing job for issue %s", issue_key)

    # Import here to avoid circular imports at module load time
    from app.workers.job_processor import process_issue  # noqa: PLC0415

    background_tasks.add_task(process_issue, issue_key)

    return {"status": "queued", "issue": issue_key}
