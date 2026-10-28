"""Container-local administration commands."""

import argparse
import json
import re
import sys
from getpass import getpass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kebivo import version
from kebivo.auth import create_admin
from kebivo.db import make_engine
from kebivo.feedback.groups import check_integrity
from kebivo.models import AdminUser, AuditEvent, Destination, LlmProviderConfig, Project
from kebivo.projects import ProjectCreate
from kebivo.retention import preview as retention_preview
from kebivo.retention import run as retention_run
from kebivo.security.secretbox import SecretBox, SecretError
from kebivo.settings import Settings
from kebivo.tokens import GAP_SCOPE, mint_project_token


def _password_pair(from_stdin: bool, *, confirm_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
        if not password:
            raise SystemExit("Password input must contain a non-empty line")
        confirmation = sys.stdin.readline().rstrip("\r\n") if confirm_stdin else password
    else:
        password = getpass("New administrator password: ")
        confirmation = getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    return password


def main() -> None:
    parser = argparse.ArgumentParser(prog="kebivo-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap", help="create the first local administrator")
    bootstrap.add_argument("--email", required=True)
    bootstrap.add_argument("--password-stdin", action="store_true", help="read one password line")
    reset = commands.add_parser("reset-password", help="reset an administrator password")
    reset.add_argument("--email", required=True)
    reset.add_argument("--password-stdin", action="store_true", help=argparse.SUPPRESS)
    project = commands.add_parser("project", help="manage projects")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_create = project_commands.add_parser("create", help="create a project and gap token")
    project_create.add_argument("--name", required=True)
    project_create.add_argument("--slug")
    project_create.add_argument("--json", action="store_true")
    commands.add_parser(
        "reencrypt-secrets", help="rewrite encrypted credentials with the current key"
    )
    commands.add_parser("check-groups", help="check feedback group membership invariants")
    commands.add_parser("version", help="print the installed version")
    retention = commands.add_parser("retention", help="preview or run retention now")
    retention_mode = retention.add_mutually_exclusive_group(required=True)
    retention_mode.add_argument("--dry-run", action="store_true")
    retention_mode.add_argument("--run", action="store_true")
    evaluation = commands.add_parser(
        "dedup-eval", help="score labelled dedup pairs against a project embedding provider"
    )
    evaluation.add_argument("--project", required=True)
    evaluation.add_argument(
        "--corpus", action="store_true", help="use the bundled synthetic corpus"
    )
    args = parser.parse_args()

    if args.command == "version":
        print(version())
        return

    configured = Settings.from_env()
    database_url = configured.database_url or (
        "sqlite://"
        if configured.database_path == ":memory:"
        else f"sqlite:///{configured.database_path}"
    )
    engine = make_engine(database_url)
    try:
        if args.command == "dedup-eval":
            from kebivo.dedup.evaluation import evaluate_project

            with Session(engine) as db:
                try:
                    print(evaluate_project(db, configured, args.project, args.corpus))
                except ValueError as exc:
                    raise SystemExit(str(exc)) from None
            return
        if args.command == "project":
            slug = (
                args.slug
                if args.slug is not None
                else re.sub(r"[^a-z0-9]+", "-", args.name.strip().lower()).strip("-")
            )
            if args.slug is None and args.name.strip() and not slug:
                raise SystemExit("Could not derive a slug from the name; pass --slug")
            try:
                body = ProjectCreate(name=args.name, slug=slug)
            except ValueError as exc:
                raise SystemExit(f"Invalid project: {exc}") from None
            with Session(engine) as db:
                admin_id = db.scalar(select(AdminUser.id).limit(1))
                if admin_id is None:
                    raise SystemExit("Bootstrap an administrator before creating a project")
                row = Project(
                    name=body.name,
                    slug=body.slug,
                    settings_json=json.dumps(body.settings, separators=(",", ":")),
                    created_by_admin_id=admin_id,
                )
                db.add(row)
                try:
                    db.flush()
                    db.add(
                        AuditEvent(
                            actor_type="admin",
                            actor_ref=admin_id,
                            action="project.created",
                            subject_type="project",
                            subject_id=row.id,
                            details_json='{"source":"cli"}',
                        )
                    )
                    _, token = mint_project_token(
                        db, configured, row, admin_id, name="Quickstart", scopes=[GAP_SCOPE]
                    )
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    raise SystemExit("A project with this slug already exists") from None
                result = {"id": row.id, "name": row.name, "slug": row.slug, "token": token}
            print(
                json.dumps(result)
                if args.json
                else f"Project {body.name} ({body.slug}) created.\nToken: {token}"
            )
            return
        if args.command == "retention":
            factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
            counts = (
                retention_preview(factory, configured)
                if args.dry_run
                else retention_run(factory, configured)
            )
            print(json.dumps(counts, sort_keys=True))
            return
        if args.command == "check-groups":
            with Session(engine) as db:
                violations = check_integrity(db)
            if violations:
                for violation in violations:
                    print(violation, file=sys.stderr)
                raise SystemExit(1)
            print("Feedback groups are consistent.")
            return
        if args.command == "reencrypt-secrets":
            box = SecretBox.from_settings(configured)
            rewritten = 0
            providers_rewritten = 0
            with Session(engine) as db:
                with db.begin():
                    destinations = db.scalars(
                        select(Destination).where(Destination.secrets_ciphertext.is_not(None))
                    ).all()
                    for destination in destinations:
                        assert destination.secrets_ciphertext is not None
                        plaintext = box.decrypt(destination.id, destination.secrets_ciphertext)
                        destination.secrets_ciphertext = box.encrypt(destination.id, plaintext)
                        destination.secrets_key_id = box.current_key_id
                        rewritten += 1
                    providers = db.scalars(
                        select(LlmProviderConfig).where(
                            LlmProviderConfig.secrets_ciphertext.is_not(None)
                        )
                    ).all()
                    for provider in providers:
                        assert provider.secrets_ciphertext is not None
                        plaintext = box.decrypt(
                            provider.id, provider.secrets_ciphertext, purpose="llm-provider-secrets"
                        )
                        provider.secrets_ciphertext = box.encrypt(
                            provider.id, plaintext, purpose="llm-provider-secrets"
                        )
                        provider.secrets_key_id = box.current_key_id
                        providers_rewritten += 1
            print(
                f"Re-encrypted {rewritten} destination secrets and "
                f"{providers_rewritten} LLM provider secrets."
            )
            return
        with Session(engine) as db:
            admin = create_admin(
                db,
                args.email,
                _password_pair(args.password_stdin, confirm_stdin=args.command == "reset-password"),
                reset=args.command == "reset-password",
            )
            email = admin.email
        print(f"Administrator {email} is ready.")
    except SecretError as exc:
        raise SystemExit(str(exc)) from None
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
