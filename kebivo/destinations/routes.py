"""Admin destination configuration API."""

import base64
import json
import secrets as random_secrets
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kebivo.auth import AdminAuth
from kebivo.destinations.validation import ConfigError, validate_config
from kebivo.errors import error_response
from kebivo.models import (
    AuditEvent,
    Delivery,
    Destination,
    Project,
    RoutingRuleDestination,
    now_utc,
)
from kebivo.security.secretbox import SecretBox, SecretError


class DestinationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    type: str
    enabled: bool = True
    delivery_mode: str = "new_groups_only"
    allow_private_network: bool = False
    config: dict[str, Any]
    secrets: dict[str, Any] = Field(default_factory=dict)


class DestinationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    enabled: bool | None = None
    delivery_mode: str | None = None
    allow_private_network: bool | None = None
    config: dict[str, Any] | None = None
    secrets: dict[str, Any] | None = None


def _fail(
    request: Request, status: int, code: str, message: str, field: str | None = None
) -> Response:
    details = {"fields": [{"field": field, "code": code, "message": message}]} if field else None
    return error_response(status, code, message, request.state.request_id, details)


def _id(value: str) -> str | None:
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _box(request: Request) -> SecretBox:
    return SecretBox.from_settings(request.app.state.settings)


def _plain(row: Destination, request: Request) -> dict[str, Any]:
    return _box(request).decrypt(row.id, row.secrets_ciphertext) if row.secrets_ciphertext else {}


def _serialize(row: Destination, request: Request) -> dict[str, Any]:
    public = json.loads(row.config_json)
    try:
        hidden = _plain(row, request)
    except SecretError:
        hidden = {}
        secrets_unavailable = True
    else:
        secrets_unavailable = False
    if "url" in hidden:
        parsed = urlsplit(hidden["url"])
        public["url_origin"] = f"{parsed.scheme}://{parsed.netloc}"
        public["url_has_path"] = bool(parsed.path and parsed.path != "/")
    for key in hidden:
        if key == "url":
            public["url"] = {"set": True}
        elif key.startswith("custom_headers."):
            public.setdefault("custom_header_secrets", {})[key.split(".", 1)[1]] = {"set": True}
        elif key in {"token", "header_value"}:
            public["auth"]["token" if key == "token" else "value"] = {"set": True}
        else:
            public[key] = {"set": True}
    return {
        "id": row.id,
        "name": row.name,
        "type": row.type,
        "enabled": row.enabled,
        "delivery_mode": row.delivery_mode,
        "allow_private_network": row.allow_private_network,
        "is_system": row.is_system,
        "secrets_unavailable": secrets_unavailable,
        "config": public,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _validate_name(name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= 120:
        raise ConfigError("name", "Name must contain 1 to 120 characters")
    return name


def _validate_mode(mode: str) -> None:
    if mode not in {"new_groups_only", "every_occurrence"}:
        raise ConfigError("delivery_mode", "Invalid delivery mode")


def _encrypt(row: Destination, request: Request, hidden: dict[str, str]) -> None:
    if hidden:
        box = _box(request)
        row.secrets_ciphertext = box.encrypt(row.id, hidden)
        row.secrets_key_id = box.current_key_id
    else:
        row.secrets_ciphertext = None
        row.secrets_key_id = None


def _audit(
    db: Session, admin_id: str, action: str, row: Destination, details: dict[str, Any] | None = None
) -> None:
    db.add(
        AuditEvent(
            actor_type="admin",
            actor_ref=admin_id,
            action=action,
            subject_type="destination",
            subject_id=row.id,
            details_json=json.dumps(details or {}, separators=(",", ":")),
        )
    )


def _config_for_validation(row: Destination, request: Request) -> dict[str, Any]:
    public = json.loads(row.config_json)
    hidden = _plain(row, request)
    config = {**public, "type": row.type, "allow_private_network": row.allow_private_network}
    config.update(hidden)
    if row.type == "webhook":
        config["custom_headers"] = {
            key: hidden.get(f"custom_headers.{key}") for key in public.get("custom_headers", [])
        }
        if "token" in hidden:
            config["auth"] = {**public["auth"], "token": hidden["token"]}
        if "header_value" in hidden:
            config["auth"] = {**public["auth"], "value": hidden["header_value"]}
    return config


def install_destination_routes(app: Any) -> None:
    router = APIRouter(prefix="/api/v1/destinations", tags=["destinations"])

    @router.get("")
    def list_destinations(
        request: Request,
        auth: AdminAuth,
        limit: int = Query(50, ge=1, le=100),
        cursor: str | None = None,
    ) -> Any:
        del auth
        db: Session = request.state.db
        statement = select(Destination)
        if cursor:
            try:
                raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
                created, identifier = raw.split("|", 1)
                created_at = datetime.fromisoformat(created)
                identifier = str(UUID(identifier))
            except (ValueError, UnicodeDecodeError):
                return _fail(request, 400, "invalid_cursor", "Cursor is invalid")
            statement = statement.where(
                or_(
                    Destination.created_at > created_at,
                    and_(Destination.created_at == created_at, Destination.id > identifier),
                )
            )
        rows = db.scalars(
            statement.order_by(Destination.created_at, Destination.id).limit(limit + 1)
        ).all()
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            value = f"{page[-1].created_at.isoformat()}|{page[-1].id}"
            next_cursor = base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
        return {"items": [_serialize(row, request) for row in page], "next_cursor": next_cursor}

    @router.post("", status_code=201)
    def create_destination(body: DestinationBody, request: Request, auth: AdminAuth) -> Any:
        admin, _, _ = auth
        db: Session = request.state.db
        try:
            name = _validate_name(body.name)
            _validate_mode(body.delivery_mode)
            config, hidden = validate_config(
                {
                    **body.config,
                    "type": body.type,
                    "allow_private_network": body.allow_private_network,
                },
                body.secrets,
            )
        except ConfigError as exc:
            return _fail(request, 422, "validation_error", "Invalid destination", exc.field)
        row = Destination(
            name=name,
            type=body.type,
            enabled=body.enabled,
            delivery_mode=body.delivery_mode,
            allow_private_network=body.allow_private_network,
            config_json=json.dumps(config, separators=(",", ":")),
            created_by_admin_id=admin.id,
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            return _fail(
                request, 409, "destination_name_conflict", "Destination name already exists", "name"
            )
        once = None
        if body.type == "webhook" and config["signing"]:
            once = "whsec_" + base64.b64encode(random_secrets.token_bytes(32)).decode()
            hidden["signing_secret"] = once
        _encrypt(row, request, hidden)
        _audit(
            db,
            admin.id,
            "destination.created",
            row,
            {"allow_private_network": row.allow_private_network},
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return _fail(
                request, 409, "destination_name_conflict", "Destination name already exists", "name"
            )
        result = _serialize(row, request)
        if once:
            result["signing_secret"] = once
        return result

    @router.get("/{destination_id}")
    def get_destination(destination_id: str, request: Request, auth: AdminAuth) -> Any:
        del auth
        row = request.state.db.get(Destination, _id(destination_id))
        return (
            _serialize(row, request)
            if row
            else _fail(request, 404, "destination_not_found", "Destination not found")
        )

    @router.patch("/{destination_id}")
    def patch_destination(
        destination_id: str, body: DestinationPatch, request: Request, auth: AdminAuth
    ) -> Any:
        admin, _, _ = auth
        db: Session = request.state.db
        row = db.get(Destination, _id(destination_id))
        if row is None:
            return _fail(request, 404, "destination_not_found", "Destination not found")
        changes = body.model_dump(exclude_unset=True)
        once = None
        if row.is_system:
            if set(changes) != {"enabled"} or changes["enabled"] is None:
                return _fail(
                    request, 422, "system_destination_immutable", "Only enabled may be changed"
                )
            row.enabled = changes["enabled"]
        else:
            try:
                if "name" in changes:
                    row.name = _validate_name(changes["name"] or "")
                if "enabled" in changes:
                    if changes["enabled"] is None:
                        raise ConfigError("enabled", "Cannot be null")
                    row.enabled = changes["enabled"]
                if "delivery_mode" in changes:
                    _validate_mode(changes["delivery_mode"] or "")
                    row.delivery_mode = changes["delivery_mode"]
                if "allow_private_network" in changes:
                    if row.type == "inbox" or changes["allow_private_network"] is None:
                        raise ConfigError("allow_private_network", "Invalid option")
                    row.allow_private_network = changes["allow_private_network"]
                if {"config", "secrets", "allow_private_network"} & changes.keys():
                    config = _config_for_validation(row, request)
                if changes.get("config"):
                    patch_config = changes["config"]
                    if "custom_headers" in patch_config and isinstance(
                        patch_config["custom_headers"], dict
                    ):
                        headers = dict(config.get("custom_headers", {}))
                        for name, value in patch_config["custom_headers"].items():
                            if value is None:
                                headers.pop(name, None)
                            else:
                                headers[name] = value
                        patch_config = {**patch_config, "custom_headers": headers}
                    config.update(patch_config)
                    if changes.get("secrets"):
                        config.update(changes["secrets"])
                    config["allow_private_network"] = row.allow_private_network
                    public, hidden = validate_config(config, {})
                    old_hidden = _plain(row, request)
                    if "signing_secret" in old_hidden and public.get("signing"):
                        hidden["signing_secret"] = old_hidden["signing_secret"]
                    elif public.get("signing"):
                        once = "whsec_" + base64.b64encode(random_secrets.token_bytes(32)).decode()
                        hidden["signing_secret"] = once
                    row.config_json = json.dumps(public, separators=(",", ":"))
                    _encrypt(row, request, hidden)
                elif "allow_private_network" in changes:
                    # Turning the option off must not leave a config that now breaks policy.
                    validate_config(config, {})
            except ConfigError as exc:
                db.rollback()
                return _fail(request, 422, "validation_error", "Invalid destination", exc.field)
            except SecretError:
                db.rollback()
                return _fail(request, 409, "secret_unavailable", "Destination secrets unavailable")
        row.updated_at = now_utc()
        _audit(db, admin.id, "destination.updated", row)
        if "allow_private_network" in changes:
            _audit(
                db,
                admin.id,
                "destination.private_network_changed",
                row,
                {"enabled": row.allow_private_network},
            )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return _fail(
                request, 409, "destination_name_conflict", "Destination name already exists", "name"
            )
        result = _serialize(row, request)
        if once:
            result["signing_secret"] = once
        return result

    @router.post("/{destination_id}/rotate-signing-secret")
    def rotate(destination_id: str, request: Request, auth: AdminAuth) -> Any:
        admin, _, _ = auth
        db: Session = request.state.db
        row = db.get(Destination, _id(destination_id))
        if row is None:
            return _fail(request, 404, "destination_not_found", "Destination not found")
        if row.type != "webhook" or not json.loads(row.config_json).get("signing"):
            return _fail(request, 422, "signing_not_enabled", "Signing is not enabled")
        try:
            hidden = _plain(row, request)
        except SecretError:
            return _fail(request, 409, "secret_unavailable", "Destination secrets unavailable")
        once = "whsec_" + base64.b64encode(random_secrets.token_bytes(32)).decode()
        hidden["signing_secret"] = once
        _encrypt(row, request, hidden)
        row.updated_at = now_utc()
        _audit(db, admin.id, "destination.signing_secret_rotated", row)
        db.commit()
        return {"signing_secret": once, "destination": _serialize(row, request)}

    @router.delete("/{destination_id}", status_code=204, response_class=Response)
    def delete_destination(destination_id: str, request: Request, auth: AdminAuth) -> Response:
        admin, _, _ = auth
        db: Session = request.state.db
        row = db.get(Destination, _id(destination_id))
        if row is None:
            return _fail(request, 404, "destination_not_found", "Destination not found")
        if row.is_system:
            return _fail(
                request, 422, "system_destination_immutable", "System inbox cannot be deleted"
            )
        used = (
            db.scalar(
                select(RoutingRuleDestination.rule_id).where(
                    RoutingRuleDestination.destination_id == row.id
                )
            )
            or db.scalar(select(Project.id).where(Project.fallback_destination_id == row.id))
            or db.scalar(select(Delivery.id).where(Delivery.destination_id == row.id))
        )
        if used:
            return _fail(request, 409, "destination_in_use", "Destination is in use")
        _audit(db, admin.id, "destination.deleted", row)
        db.delete(row)
        db.commit()
        return Response(status_code=204)

    app.include_router(router)
