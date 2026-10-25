"""Administrator triage mutations for feedback groups."""

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kebivo.admin_feedback import _iso_utc
from kebivo.admin_group_transactions import begin_group_write
from kebivo.admin_groups import _group, _id, _reject_bearer
from kebivo.auth import AdminAuth
from kebivo.errors import error_response
from kebivo.feedback.tags import display_tag_v1, normalize_tag_v1
from kebivo.models import (
    AuditEvent,
    FeedbackEventTag,
    FeedbackGroup,
    FeedbackGroupHistory,
    FeedbackGroupMember,
    FeedbackGroupNote,
    Tag,
    now_utc,
)


class GroupPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["open", "in_progress", "resolved", "dismissed"] | None = None
    representative_event_id: str | None = None


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=4000)


class TagPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


# Tag edits load a group's tag links in one transaction. Bound that work after merges.
MAX_TAG_EDIT_MEMBERS = 10_000


def _error(request: Request, status: int, code: str, message: str) -> Any:
    return error_response(status, code, message, request.state.request_id)


def _trail(
    db: Session,
    group: FeedbackGroup,
    admin_id: str,
    action: str,
    details: dict[str, Any],
    *,
    from_status: str | None = None,
    to_status: str | None = None,
) -> None:
    safe_details = json.dumps(details, separators=(",", ":"), sort_keys=True)
    db.add(
        FeedbackGroupHistory(
            group_id=group.id,
            action=action,
            actor_type="admin",
            actor_admin_id=admin_id,
            from_status=from_status,
            to_status=to_status,
            details_json=safe_details,
        )
    )
    db.add(
        AuditEvent(
            actor_type="admin",
            actor_ref=admin_id,
            action=f"feedback_group.{action}",
            subject_type="feedback_group",
            subject_id=group.id,
            details_json=safe_details,
        )
    )


def _group_response(group: FeedbackGroup) -> dict[str, Any]:
    return {
        "id": group.id,
        "status": group.status,
        "representative_event_id": group.representative_event_id,
        "status_changed_at": _iso_utc(group.status_changed_at),
        "resolved_at": _iso_utc(group.resolved_at) if group.resolved_at else None,
    }


def _normalized(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        slug = normalize_tag_v1(value)
        result.setdefault(slug, display_tag_v1(value))
    return result


def _tag_trails(
    db: Session,
    group: FeedbackGroup,
    admin_id: str,
    added: set[str],
    removed: dict[str, dict[str, int]],
) -> None:
    # A history details row is capped at 4 KiB. A full replacement of 30
    # long slugs can exceed that even though each event remains within 30 tags.
    details: dict[str, Any] = {"group_id": group.id, "added": [], "removed": {}}
    changes = [("added", slug, None) for slug in sorted(added)] + [
        ("removed", slug, removed[slug]) for slug in sorted(removed)
    ]
    for kind, slug, provenance in changes:
        current = details["added"] if kind == "added" else details["removed"]
        if kind == "added":
            current.append(slug)
        else:
            current[slug] = provenance
        if len(json.dumps(details, separators=(",", ":"), sort_keys=True).encode()) > 4096:
            if kind == "added":
                current.pop()
            else:
                del current[slug]
            _trail(db, group, admin_id, "tags_changed", details)
            details = {"group_id": group.id, "added": [], "removed": {}}
            if kind == "added":
                details["added"].append(slug)
            else:
                details["removed"][slug] = provenance
    if details["added"] or details["removed"]:
        _trail(db, group, admin_id, "tags_changed", details)


def install_admin_group_mutation_routes(app: Any) -> None:
    router = APIRouter(
        prefix="/api/v1/admin/groups", tags=["admin-groups"], dependencies=[Depends(_reject_bearer)]
    )

    @router.patch("/{group_id}")
    def patch_group(group_id: str, payload: GroupPatch, request: Request, auth: AdminAuth) -> Any:
        db: Session = request.state.db
        begin_group_write(db)
        group = _group(db, group_id)
        if group is None:
            return _error(request, 404, "not_found", "Group not found")
        if payload.status is None and payload.representative_event_id is None:
            return _error(request, 422, "validation_error", "No group change supplied")
        representative = (
            _id(payload.representative_event_id)
            if payload.representative_event_id is not None
            else None
        )
        if payload.representative_event_id is not None:
            member = db.get(FeedbackGroupMember, representative) if representative else None
            if member is None or member.group_id != group.id:
                return _error(request, 422, "validation_error", "Representative must be a member")
        admin_id = auth[0].id
        changed = False
        if payload.status is not None and payload.status != group.status:
            previous = group.status
            group.status = payload.status
            group.status_changed_at = now_utc()
            group.resolved_at = group.status_changed_at if payload.status == "resolved" else None
            _trail(
                db,
                group,
                admin_id,
                "status_changed",
                {"group_id": group.id, "from_status": previous, "to_status": group.status},
                from_status=previous,
                to_status=group.status,
            )
            changed = True
        if representative is not None and representative != group.representative_event_id:
            previous_rep = group.representative_event_id
            group.representative_event_id = representative
            _trail(
                db,
                group,
                admin_id,
                "representative_changed",
                {
                    "group_id": group.id,
                    "from_event_id": previous_rep,
                    "to_event_id": representative,
                },
            )
            changed = True
        if changed:
            group.updated_at = now_utc()
            db.commit()
        return _group_response(group)

    @router.post("/{group_id}/notes", status_code=201)
    def add_note(group_id: str, payload: NoteCreate, request: Request, auth: AdminAuth) -> Any:
        db: Session = request.state.db
        begin_group_write(db)
        group = _group(db, group_id)
        if group is None:
            return _error(request, 404, "not_found", "Group not found")
        count = db.scalar(
            select(func.count())
            .select_from(FeedbackGroupNote)
            .where(FeedbackGroupNote.group_id == group.id)
        )
        if count is not None and count >= 500:
            return _error(request, 422, "note_limit", "Group has reached the note limit")
        note = FeedbackGroupNote(group_id=group.id, author_admin_id=auth[0].id, body=payload.body)
        db.add(note)
        db.flush()
        _trail(db, group, auth[0].id, "note_added", {"group_id": group.id, "note_id": note.id})
        db.commit()
        return {
            "id": note.id,
            "body": note.body,
            "author_admin_id": note.author_admin_id,
            "created_at": _iso_utc(note.created_at),
        }

    @router.delete("/{group_id}/notes/{note_id}")
    def delete_note(group_id: str, note_id: str, request: Request, auth: AdminAuth) -> Any:
        db: Session = request.state.db
        begin_group_write(db)
        group = _group(db, group_id)
        if group is None:
            return _error(request, 404, "not_found", "Group not found")
        note = db.get(FeedbackGroupNote, _id(note_id)) if _id(note_id) else None
        if note is None or note.group_id != group.id:
            return _error(request, 404, "not_found", "Note not found")
        db.delete(note)
        _trail(db, group, auth[0].id, "note_deleted", {"group_id": group.id, "note_id": note.id})
        db.commit()
        return {"id": note_id, "deleted": True}

    @router.patch("/{group_id}/tags")
    def patch_tags(group_id: str, payload: TagPatch, request: Request, auth: AdminAuth) -> Any:
        db: Session = request.state.db
        begin_group_write(db)
        group = _group(db, group_id)
        if group is None:
            return _error(request, 404, "not_found", "Group not found")
        try:
            add = _normalized(payload.add)
            remove = _normalized(payload.remove)
        except ValueError:
            return _error(request, 422, "validation_error", "Invalid tag")
        if set(add) & set(remove):
            return _error(
                request, 422, "validation_error", "Tag cannot be added and removed together"
            )
        if not add and not remove:
            return {"id": group.id, "added": [], "removed": []}
        member_count = db.scalar(
            select(func.count()).select_from(FeedbackGroupMember).where(
                FeedbackGroupMember.group_id == group.id
            )
        )
        if member_count is not None and member_count > MAX_TAG_EDIT_MEMBERS:
            return _error(request, 422, "tag_edit_limit", "Group is too large for a tag edit")
        event_ids = db.scalars(
            select(FeedbackGroupMember.event_id).where(FeedbackGroupMember.group_id == group.id)
        ).all()
        tags = {
            tag.slug: tag
            for tag in db.scalars(select(Tag).where(Tag.project_id == group.project_id)).all()
        }
        links = db.scalars(
            select(FeedbackEventTag)
            .join(FeedbackGroupMember, FeedbackGroupMember.event_id == FeedbackEventTag.event_id)
            .where(FeedbackGroupMember.group_id == group.id)
        ).all()
        by_event = {(link.event_id, link.tag_id): link for link in links}
        removed_ids = {tags[slug].id for slug in remove if slug in tags}
        existing_by_event: dict[str, int] = {}
        for existing_link in links:
            if existing_link.tag_id not in removed_ids:
                existing_by_event[existing_link.event_id] = (
                    existing_by_event.get(existing_link.event_id, 0) + 1
                )
        for event_id in event_ids:
            existing = existing_by_event.get(event_id, 0)
            incoming = sum(
                1 for slug in add if slug not in tags or (event_id, tags[slug].id) not in by_event
            )
            if existing + incoming > 30:
                return _error(request, 422, "tag_limit", "An event would exceed 30 tags")
        changed_add: set[str] = set()
        changed_remove: dict[str, dict[str, int]] = {}
        for slug in remove:
            tag = tags.get(slug)
            if tag is None:
                continue
            for event_id in event_ids:
                link = by_event.get((event_id, tag.id))
                if link is None:
                    continue
                provenance = changed_remove.setdefault(
                    slug, {"from_default": 0, "from_submission": 0, "from_admin": 0}
                )
                for source in provenance:
                    provenance[source] += int(getattr(link, source))
                db.delete(link)
        for slug, name in add.items():
            tag = tags.get(slug)
            if tag is None:
                tag = Tag(project_id=group.project_id, slug=slug, name=name)
                db.add(tag)
                db.flush()
                tags[slug] = tag
            for event_id in event_ids:
                link = by_event.get((event_id, tag.id))
                if link is None:
                    db.add(FeedbackEventTag(event_id=event_id, tag_id=tag.id, from_admin=True))
                    changed_add.add(slug)
                elif not link.from_admin:
                    link.from_admin = True
                    changed_add.add(slug)
        if changed_add or changed_remove:
            _tag_trails(db, group, auth[0].id, changed_add, changed_remove)
            db.commit()
        return {"id": group.id, "added": sorted(changed_add), "removed": sorted(changed_remove)}

    app.include_router(router)
