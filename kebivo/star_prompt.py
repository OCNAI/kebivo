"""One-time administrator GitHub star prompt."""

from typing import Any

from fastapi import APIRouter, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from kebivo.auth import AdminAuth
from kebivo.models import FeedbackEvent, Project, now_utc


def install_star_prompt_routes(app: Any) -> None:
    router = APIRouter(prefix="/api/v1/admin/star-prompt", tags=["admin"])

    @router.get("")
    def status(request: Request, auth: AdminAuth) -> dict[str, bool]:
        admin, _, _ = auth
        db: Session = request.state.db
        real_report = (
            select(FeedbackEvent.id).join(Project).where(Project.is_sample.is_(False)).limit(1)
        )
        return {
            "show": admin.star_prompt_dismissed_at is None and db.scalar(real_report) is not None
        }

    @router.post("/dismiss", status_code=204)
    def dismiss(request: Request, auth: AdminAuth) -> Response:
        admin, _, _ = auth
        if admin.star_prompt_dismissed_at is None:
            admin.star_prompt_dismissed_at = now_utc()
            db: Session = request.state.db
            db.commit()
        return Response(status_code=204)

    app.include_router(router)
