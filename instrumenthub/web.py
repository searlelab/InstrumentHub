from __future__ import annotations

import json
import logging
import uuid
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import Settings
from .db import Database
from .models import (
    Allotment,
    AppSetting,
    AuditEvent,
    Booking,
    CalendarEntry,
    Grant,
    Hold,
    Instrument,
    InstrumentTraining,
    Maintenance,
    PI,
    Project,
    ProjectGrant,
    ReservedSlot,
    ScheduleChangeDraft,
    SessionRecord,
    User,
    UserProject,
)
from .security import cleanup_login_attempts, cleanup_sessions, create_session, hash_token, parse_utc, resolve_session, revoke_user_sessions, utc_text, utcnow, verify_password
from .services import audit, complete_setup, create_user, replace_block_definitions, reset_user_password, setup_complete
from .services import get_setting
from .scheduling import calendar_data, create_maintenance, month_days, prune_expired_holds
from .booking import acquire_hold, clear_wizard_state, release_hold, release_hold_as_admin, resolve_hold, save_wizard_state, slot_values, submit_booking
from .rules import RuleResult, allotment_balances, evaluate_booking, estimate_duration_minutes
from .management import apply_schedule_draft, cancel_booking, decide_booking, edit_booking, owner_can_manage_booking, preview_outage, preview_resume, recover_unknown_outage, transition_booking
from .logging_config import configure_logging

LOGGER = logging.getLogger("instrumenthub")
PACKAGE_DIR = Path(__file__).parent


@dataclass(frozen=True)
class RequestUser:
    id: int
    role: str
    active: bool
    display_name: str
    initials: str
    email: str
    phone: str
    accent_color: str


def _request_user(user: User) -> RequestUser:
    return RequestUser(
        id=user.id,
        role=user.role,
        active=user.active,
        display_name=user.display_name,
        initials=user.initials,
        email=user.email,
        phone=user.phone,
        accent_color=user.accent_color,
    )


def _context(request: Request, **values: Any) -> dict[str, Any]:
    return {"request": request, "user": getattr(request.state, "user", None), "csrf_token": getattr(request.state, "csrf_token", ""), **values}


def _redirect(path: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(path, status_code=status_code)


def _require_user(request: Request) -> RequestUser:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def _require_admin(request: Request) -> RequestUser:
    user = _require_user(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user


def _check_csrf(request: Request, submitted: str) -> None:
    expected = getattr(request.state, "csrf_token", None)
    if not expected or not submitted or not __import__("secrets").compare_digest(expected, submitted):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _hold_cookie(instrument_id: int) -> str:
    """Scope hold cookies per instrument so a hold on one never displaces another."""
    return f"instrumenthub_hold_{instrument_id}"


def _hold_token(request: Request, db: Session, hold_id: int) -> str:
    hold = db.get(Hold, hold_id)
    if not hold:
        return ""
    entry = db.get(CalendarEntry, hold.calendar_entry_id)
    return request.cookies.get(_hold_cookie(entry.instrument_id), "") if entry else ""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings)
    database = Database(settings)
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield

    app = FastAPI(title="InstrumentHub", lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        started = time.perf_counter()
        request.state.request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))[:128]
        request.state.user = None
        request.state.csrf_token = ""
        try:
            with database.session() as db:
                resolved = resolve_session(
                    db,
                    request.cookies.get("instrumenthub_session"),
                    settings,
                    refresh_idle_activity=request.url.path != "/api/calendar",
                )
                if resolved:
                    user, session_record = resolved
                    request.state.user = _request_user(user)
                    request.state.csrf_token = session_record.csrf_token
            response = await call_next(request)
        except Exception:
            request.state.user = None
            request.state.csrf_token = ""
            LOGGER.exception("Unhandled request error", extra={"request_id": request.state.request_id})
            response = templates.TemplateResponse(request, "error.html", _context(request, status=500, message="An unexpected error occurred. The request identifier is shown below."), status_code=500)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
        response.headers["Cache-Control"] = "no-store"
        LOGGER.info("request", extra={"request_id": request.state.request_id, "method": request.method, "path": request.url.path, "status": response.status_code, "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code == 401 and request.url.path != "/login":
            return _redirect(f"/login?next={request.url.path}", 303)
        return templates.TemplateResponse(request, "error.html", _context(request, status=exc.status_code, message=str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        LOGGER.exception("Unhandled request error", extra={"request_id": request.state.request_id})
        request.state.user = None
        request.state.csrf_token = ""
        return templates.TemplateResponse(request, "error.html", _context(request, status=500, message="An unexpected error occurred. The request identifier is shown below."), status_code=500)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        try:
            with database.session() as db:
                result = db.execute(text("SELECT 1")).scalar_one()
                migrated = db.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar_one()
            if result != 1 or not migrated:
                raise RuntimeError("Database is not ready")
            return JSONResponse({"status": "ready"})
        except Exception:
            return JSONResponse({"status": "not_ready"}, status_code=503)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, error: str = ""):
        if request.state.user:
            return _redirect("/")
        return templates.TemplateResponse(request, "login.html", _context(request, error=error))

    @app.post("/login")
    def login(request: Request, username: Annotated[str, Form()], password: Annotated[str, Form()]):
        remote = request.client.host if request.client else "unknown"
        with database.session() as db:
            cleanup_sessions(db)
            cleanup_login_attempts(db)
            cutoff = utc_text(utcnow() - timedelta(minutes=15))
            failures = db.execute(
                text("SELECT COUNT(*) FROM login_attempts WHERE lower(username)=lower(:username) AND remote_addr=:remote AND attempted_at>=:cutoff AND succeeded=0"),
                {"username": username, "remote": remote, "cutoff": cutoff},
            ).scalar_one()
            if failures >= 5:
                return templates.TemplateResponse(request, "login.html", _context(request, error="Too many failed attempts. Try again later."), status_code=429)
            user = db.scalar(select(User).where(func.lower(User.username) == username.strip().lower()))
            valid = bool(user and user.active and verify_password(user.password_hash, password))
            db.execute(text("INSERT INTO login_attempts(username,remote_addr,attempted_at,succeeded) VALUES (:u,:r,:t,:s)"), {"u": username[:64], "r": remote[:128], "t": utc_text(), "s": int(valid)})
            if not valid:
                return templates.TemplateResponse(request, "login.html", _context(request, error="Invalid username or password."), status_code=400)
            token, _record = create_session(db, user, settings)
            audit(db, "login", "session", None, actor_user_id=user.id, request_id=request.state.request_id)
        response = _redirect("/")
        response.set_cookie("instrumenthub_session", token, httponly=True, secure=settings.secure_cookies, samesite="lax", max_age=settings.session_absolute_hours * 3600, path="/")
        return response

    @app.post("/logout")
    def logout(request: Request, csrf_token: Annotated[str, Form()]):
        _check_csrf(request, csrf_token)
        token = request.cookies.get("instrumenthub_session")
        if token:
            with database.session() as db:
                record = db.scalar(select(SessionRecord).where(SessionRecord.token_hash == hash_token(token)))
                if record:
                    record.revoked_at = utc_text()
        response = _redirect("/login")
        response.delete_cookie("instrumenthub_session", path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        user = _require_user(request)
        with database.session() as db:
            configured = setup_complete(db)
        if not configured:
            return _redirect("/setup" if user.role == "admin" else "/setup-required")
        return _redirect("/calendar")

    @app.get("/setup-required", response_class=HTMLResponse)
    def setup_required(request: Request):
        _require_user(request)
        return templates.TemplateResponse(request, "error.html", _context(request, status=503, message="An administrator must complete initial scheduling setup."), status_code=503)

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request, error: str = ""):
        _require_admin(request)
        with database.session() as db:
            if setup_complete(db):
                return _redirect("/admin")
        return templates.TemplateResponse(request, "setup.html", _context(request, error=error))

    @app.post("/setup")
    def setup_submit(
        request: Request,
        csrf_token: Annotated[str, Form()], timezone: Annotated[str, Form()],
        instrument_1_name: Annotated[str, Form()], instrument_1_blocks: Annotated[str, Form()],
        instrument_2_name: Annotated[str, Form()], instrument_2_blocks: Annotated[str, Form()],
        hold_minutes: Annotated[int, Form()] = 15, tentative_expiration_days: Annotated[str, Form()] = "",
        instrument_1_training: Annotated[str | None, Form()] = None, instrument_2_training: Annotated[str | None, Form()] = None,
    ):
        admin = _require_admin(request)
        _check_csrf(request, csrf_token)
        try:
            expiration = int(tentative_expiration_days) if tentative_expiration_days.strip() else None
            with database.session() as db:
                complete_setup(
                    db, timezone=timezone, hold_minutes=hold_minutes, tentative_expiration_days=expiration,
                    instruments=[
                        {"name": instrument_1_name, "blocks": instrument_1_blocks, "training_required": bool(instrument_1_training)},
                        {"name": instrument_2_name, "blocks": instrument_2_blocks, "training_required": bool(instrument_2_training)},
                    ], actor_user_id=admin.id, request_id=request.state.request_id,
                )
            return _redirect("/admin")
        except ValueError as exc:
            return templates.TemplateResponse(request, "setup.html", _context(request, error=str(exc)), status_code=400)

    @app.get("/profile", response_class=HTMLResponse)
    def profile(request: Request, message: str = ""):
        user = _require_user(request)
        with database.session() as db:
            projects = db.scalars(select(Project).where(Project.active.is_(True)).order_by(Project.code)).all()
            selected_projects = set(db.scalars(select(UserProject.project_id).where(UserProject.user_id == user.id)).all())
        return templates.TemplateResponse(request, "profile.html", _context(request, profile_user=user, projects=projects, selected_projects=selected_projects, message=message))

    @app.post("/profile")
    def update_profile(request: Request, csrf_token: Annotated[str, Form()], email: Annotated[str, Form()] = "", phone: Annotated[str, Form()] = "", accent_color: Annotated[str, Form()] = "#2563eb"):
        user = _require_user(request)
        _check_csrf(request, csrf_token)
        if not __import__("re").fullmatch(r"#[0-9a-fA-F]{6}", accent_color):
            raise HTTPException(400, "Accent color must be a six-digit hex color.")
        with database.session() as db:
            stored = db.get(User, user.id)
            before = {"email": stored.email, "phone": stored.phone, "accent_color": stored.accent_color}
            stored.email, stored.phone, stored.accent_color = email.strip(), phone.strip(), accent_color.lower()
            audit(db, "updated", "user", user.id, actor_user_id=user.id, before=before, after={"email": stored.email, "phone": stored.phone, "accent_color": stored.accent_color}, request_id=request.state.request_id)
        return _redirect("/profile?message=Profile+updated")

    @app.post("/profile/password")
    def change_password(request: Request, csrf_token: Annotated[str, Form()], current_password: Annotated[str, Form()], new_password: Annotated[str, Form()]):
        user = _require_user(request)
        _check_csrf(request, csrf_token)
        from .security import hash_password
        try:
            with database.session() as db:
                stored = db.get(User, user.id)
                if not verify_password(stored.password_hash, current_password):
                    raise HTTPException(400, "Current password is incorrect.")
                stored.password_hash = hash_password(new_password)
                revoke_user_sessions(db, user.id)
                audit(db, "password_changed", "user", user.id, actor_user_id=user.id, request_id=request.state.request_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        response = _redirect("/login")
        response.delete_cookie("instrumenthub_session", path="/")
        return response

    @app.post("/profile/projects")
    def profile_project(request: Request, csrf_token: Annotated[str, Form()], project_id: Annotated[int, Form()], action: Annotated[str, Form()]):
        user = _require_user(request); _check_csrf(request, csrf_token)
        if action not in {"add", "remove"}:
            raise HTTPException(400, "Invalid project-association action.")
        with database.session() as db:
            project = db.get(Project, project_id)
            if not project or not project.active:
                raise HTTPException(404, "Project not found.")
            link = db.get(UserProject, {"user_id": user.id, "project_id": project_id})
            if action == "add" and not link:
                db.add(UserProject(user_id=user.id, project_id=project_id))
            elif action == "remove" and link:
                db.delete(link)
            audit(db, "project_added" if action == "add" else "project_removed", "user_project", f"{user.id}:{project_id}", actor_user_id=user.id, request_id=request.state.request_id)
        return _redirect("/profile?message=Project+associations+updated")

    @app.get("/admin", response_class=HTMLResponse)
    def admin_dashboard(request: Request, message: str = ""):
        _require_admin(request)
        with database.session() as db:
            data = {
                "users": db.scalars(select(User).order_by(User.display_name)).all(),
                "pis": db.scalars(select(PI).order_by(PI.name)).all(),
                "instruments": db.scalars(select(Instrument).order_by(Instrument.name)).all(),
                "projects": db.scalars(select(Project).order_by(Project.code)).all(),
                "grants": db.scalars(select(Grant).order_by(Grant.number)).all(),
                "project_grants": db.execute(select(ProjectGrant, Project, Grant).join(Project, Project.id == ProjectGrant.project_id).join(Grant, Grant.id == ProjectGrant.grant_id).order_by(Project.code, Grant.number)).all(),
                "allotments": db.scalars(select(Allotment).order_by(Allotment.period_start.desc())).all(),
                "training_records": db.execute(select(InstrumentTraining, User, Instrument).join(User, User.id == InstrumentTraining.user_id).join(Instrument, Instrument.id == InstrumentTraining.instrument_id).order_by(User.display_name, Instrument.name)).all(),
                "configured": setup_complete(db),
            }
        return templates.TemplateResponse(request, "admin.html", _context(request, message=message, **data))

    @app.post("/admin/users")
    def admin_create_user(request: Request, csrf_token: Annotated[str, Form()], username: Annotated[str, Form()], password: Annotated[str, Form()], display_name: Annotated[str, Form()], initials: Annotated[str, Form()], role: Annotated[str, Form()] = "user", email: Annotated[str, Form()] = "", phone: Annotated[str, Form()] = ""):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        try:
            with database.session() as db:
                create_user(db, username=username, password=password, display_name=display_name, initials=initials, role=role, email=email, phone=phone, actor_user_id=admin.id, request_id=request.state.request_id)
        except ValueError as exc:
            status = 409 if "already exists" in str(exc).lower() else 400
            raise HTTPException(status, str(exc)) from exc
        except IntegrityError as exc:
            raise HTTPException(409, "Username already exists.") from exc
        return _redirect("/admin?message=User+created")

    @app.post("/admin/users/{user_id}/status")
    def admin_user_status(request: Request, user_id: int, csrf_token: Annotated[str, Form()], active: Annotated[str, Form()], reason: Annotated[str, Form()]):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        enabled = active == "true"
        if user_id == admin.id and not enabled:
            raise HTTPException(400, "Administrators cannot deactivate their current account.")
        if not reason.strip():
            raise HTTPException(400, "A status-change reason is required.")
        with database.session() as db:
            target = db.get(User, user_id)
            if not target: raise HTTPException(404, "User not found.")
            before = target.active; target.active = enabled
            if not enabled: revoke_user_sessions(db, target.id)
            audit(db, "status_changed", "user", target.id, actor_user_id=admin.id, reason=reason, before={"active": before}, after={"active": enabled}, request_id=request.state.request_id)
        return _redirect("/admin?message=User+status+updated")

    @app.post("/admin/users/{user_id}/role")
    def admin_user_role(request: Request, user_id: int, csrf_token: Annotated[str, Form()], role: Annotated[str, Form()], reason: Annotated[str, Form()]):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        if role not in {"user", "admin"} or not reason.strip():
            raise HTTPException(400, "A valid role and reason are required.")
        if user_id == admin.id and role != "admin":
            raise HTTPException(400, "Administrators cannot remove their own administrator role.")
        with database.session() as db:
            target = db.get(User, user_id)
            if not target: raise HTTPException(404, "User not found.")
            before = target.role; target.role = role
            audit(db, "role_changed", "user", target.id, actor_user_id=admin.id, reason=reason, before={"role": before}, after={"role": role}, request_id=request.state.request_id)
        return _redirect("/admin?message=User+role+updated")

    @app.post("/admin/users/{user_id}/password")
    def admin_user_password(
        request: Request,
        user_id: int,
        csrf_token: Annotated[str, Form()],
        new_password: Annotated[str, Form()],
        new_password_confirmation: Annotated[str, Form()],
        reason: Annotated[str, Form()],
    ):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        if new_password != new_password_confirmation:
            raise HTTPException(400, "New passwords do not match.")
        try:
            with database.session() as db:
                reset_user_password(
                    db,
                    target_user_id=user_id,
                    new_password=new_password,
                    actor_user_id=admin.id,
                    reason=reason,
                    request_id=request.state.request_id,
                )
        except ValueError as exc:
            status_code = 404 if str(exc) == "User not found." else 400
            raise HTTPException(status_code, str(exc)) from exc
        if user_id == admin.id:
            response = _redirect("/login")
            response.delete_cookie("instrumenthub_session", path="/")
            return response
        return _redirect("/admin?message=Password+reset+and+sessions+revoked")

    @app.post("/admin/references/{reference_type}/{reference_id}/status")
    def admin_reference_status(request: Request, reference_type: str, reference_id: int, csrf_token: Annotated[str, Form()], active: Annotated[str, Form()], reason: Annotated[str, Form()]):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        model = {"pi": PI, "project": Project, "grant": Grant}.get(reference_type)
        if not model or not reason.strip():
            raise HTTPException(400, "A reference status-change reason is required.")
        with database.session() as db:
            target = db.get(model, reference_id)
            if not target: raise HTTPException(404, "Reference record not found.")
            before = target.active; target.active = active == "true"
            audit(db, "status_changed", reference_type, reference_id, actor_user_id=admin.id, reason=reason, before={"active": before}, after={"active": target.active}, request_id=request.state.request_id)
        return _redirect("/admin?message=Reference+status+updated")

    @app.post("/admin/instruments/{instrument_id}/policy")
    def admin_instrument_policy(request: Request, instrument_id: int, csrf_token: Annotated[str, Form()], active: Annotated[str, Form()], training_required: Annotated[str, Form()], reason: Annotated[str, Form()]):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        if not reason.strip(): raise HTTPException(400, "An instrument-policy reason is required.")
        with database.session() as db:
            instrument = db.get(Instrument, instrument_id)
            if not instrument: raise HTTPException(404, "Instrument not found.")
            before = {"active": instrument.active, "training_required": instrument.training_required}
            instrument.active, instrument.training_required = active == "true", training_required == "true"
            audit(db, "policy_changed", "instrument", instrument.id, actor_user_id=admin.id, reason=reason, before=before, after={"active": instrument.active, "training_required": instrument.training_required}, request_id=request.state.request_id)
        return _redirect("/admin?message=Instrument+policy+updated")

    @app.post("/admin/pis")
    def admin_create_pi(request: Request, csrf_token: Annotated[str, Form()], name: Annotated[str, Form()], email: Annotated[str, Form()] = ""):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        with database.session() as db:
            if not name.strip() or db.scalar(select(PI.id).where(func.lower(PI.name) == name.strip().lower())):
                raise HTTPException(400, "PI name is required and must be unique.")
            pi = PI(name=name.strip(), email=email.strip(), active=True); db.add(pi); db.flush()
            audit(db, "created", "pi", pi.id, actor_user_id=admin.id, after={"name": pi.name}, request_id=request.state.request_id)
        return _redirect("/admin?message=PI+created")

    @app.post("/admin/projects")
    def admin_create_project(request: Request, csrf_token: Annotated[str, Form()], pi_id: Annotated[int, Form()], code: Annotated[str, Form()], name: Annotated[str, Form()], max_consecutive_minutes: Annotated[str, Form()] = ""):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        with database.session() as db:
            try:
                limit = int(max_consecutive_minutes) if max_consecutive_minutes else None
            except ValueError as exc:
                raise HTTPException(400, "Consecutive-time limit must be a whole number of minutes.") from exc
            if not db.get(PI, pi_id) or not code.strip() or not name.strip() or (limit is not None and limit <= 0) or db.scalar(select(Project.id).where(func.lower(Project.code) == code.strip().lower())):
                raise HTTPException(400, "Project PI, code, name, or consecutive-time limit is invalid.")
            project = Project(pi_id=pi_id, code=code.strip(), name=name.strip(), max_consecutive_minutes=limit, active=True); db.add(project); db.flush()
            audit(db, "created", "project", project.id, actor_user_id=admin.id, after={"code": project.code}, request_id=request.state.request_id)
        return _redirect("/admin?message=Project+created")

    @app.post("/admin/grants")
    def admin_create_grant(request: Request, csrf_token: Annotated[str, Form()], pi_id: Annotated[int, Form()], number: Annotated[str, Form()], name: Annotated[str, Form()]):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        with database.session() as db:
            if not db.get(PI, pi_id) or not number.strip() or not name.strip() or db.scalar(select(Grant.id).where(func.lower(Grant.number) == number.strip().lower())):
                raise HTTPException(400, "Grant PI, number, or name is invalid.")
            grant = Grant(pi_id=pi_id, number=number.strip(), name=name.strip(), active=True); db.add(grant); db.flush()
            audit(db, "created", "grant", grant.id, actor_user_id=admin.id, after={"number": grant.number}, request_id=request.state.request_id)
        return _redirect("/admin?message=Grant+created")

    @app.post("/admin/project-grants")
    def admin_link_grant(request: Request, csrf_token: Annotated[str, Form()], project_id: Annotated[int, Form()], grant_id: Annotated[int, Form()]):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        with database.session() as db:
            project, grant = db.get(Project, project_id), db.get(Grant, grant_id)
            if not project or not grant or project.pi_id != grant.pi_id:
                raise HTTPException(400, "Project and grant must exist and belong to the same PI.")
            db.merge(ProjectGrant(project_id=project_id, grant_id=grant_id))
            audit(db, "linked", "project_grant", f"{project_id}:{grant_id}", actor_user_id=admin.id, request_id=request.state.request_id)
        return _redirect("/admin?message=Grant+linked")

    @app.post("/admin/project-grants/unlink")
    def admin_unlink_grant(request: Request, csrf_token: Annotated[str, Form()], project_id: Annotated[int, Form()], grant_id: Annotated[int, Form()], reason: Annotated[str, Form()]):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        if not reason.strip(): raise HTTPException(400, "An unlink reason is required.")
        with database.session() as db:
            link = db.get(ProjectGrant, {"project_id": project_id, "grant_id": grant_id})
            if not link: raise HTTPException(404, "Project-grant link not found.")
            db.delete(link)
            audit(db, "unlinked", "project_grant", f"{project_id}:{grant_id}", actor_user_id=admin.id, reason=reason, request_id=request.state.request_id)
        return _redirect("/admin?message=Grant+unlinked")

    @app.post("/admin/training")
    def admin_training(request: Request, csrf_token: Annotated[str, Form()], user_id: Annotated[int, Form()], instrument_id: Annotated[int, Form()]):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        with database.session() as db:
            if not db.get(User, user_id) or not db.get(Instrument, instrument_id):
                raise HTTPException(400, "User and instrument must exist.")
            db.merge(InstrumentTraining(user_id=user_id, instrument_id=instrument_id, trained_at=utc_text(), revoked_at=None))
            audit(db, "granted", "training", f"{user_id}:{instrument_id}", actor_user_id=admin.id, request_id=request.state.request_id)
        return _redirect("/admin?message=Training+recorded")

    @app.post("/admin/training/revoke")
    def admin_training_revoke(request: Request, csrf_token: Annotated[str, Form()], user_id: Annotated[int, Form()], instrument_id: Annotated[int, Form()], reason: Annotated[str, Form()]):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        if not reason.strip():
            raise HTTPException(400, "A training-revocation reason is required.")
        with database.session() as db:
            training = db.get(InstrumentTraining, {"user_id": user_id, "instrument_id": instrument_id})
            if not training or training.revoked_at:
                raise HTTPException(404, "Active training record not found.")
            training.revoked_at = utc_text()
            audit(db, "revoked", "training", f"{user_id}:{instrument_id}", actor_user_id=admin.id, reason=reason, before={"revoked_at": None}, after={"revoked_at": training.revoked_at}, request_id=request.state.request_id)
        return _redirect("/admin?message=Training+revoked")

    @app.post("/admin/allotments")
    def admin_allotment(request: Request, csrf_token: Annotated[str, Form()], scope_type: Annotated[str, Form()], scope_id: Annotated[int, Form()], period_start: Annotated[str, Form()], period_end: Annotated[str, Form()], minutes_allowed: Annotated[int, Form()], instrument_id: Annotated[str, Form()] = ""):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        if scope_type not in {"pi", "project"} or minutes_allowed <= 0 or period_end < period_start:
            raise HTTPException(400, "Invalid allotment.")
        with database.session() as db:
            target = db.get(PI if scope_type == "pi" else Project, scope_id)
            try:
                selected_instrument_id = int(instrument_id) if instrument_id else None
            except ValueError as exc:
                raise HTTPException(400, "Instrument selection is invalid.") from exc
            if not target or (selected_instrument_id and not db.get(Instrument, selected_instrument_id)):
                raise HTTPException(400, "Allotment scope or instrument does not exist.")
            item = Allotment(scope_type=scope_type, scope_id=scope_id, instrument_id=selected_instrument_id, period_start=period_start, period_end=period_end, minutes_allowed=minutes_allowed); db.add(item); db.flush()
            audit(db, "created", "allotment", item.id, actor_user_id=admin.id, after={"scope_type": scope_type, "scope_id": scope_id, "minutes_allowed": minutes_allowed}, request_id=request.state.request_id)
        return _redirect("/admin?message=Allotment+created")

    @app.post("/admin/allotments/{allotment_id}")
    def admin_update_allotment(request: Request, allotment_id: int, csrf_token: Annotated[str, Form()], period_start: Annotated[str, Form()], period_end: Annotated[str, Form()], minutes_allowed: Annotated[int, Form()], reason: Annotated[str, Form()], action: Annotated[str, Form()] = "update"):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        if not reason.strip() or action not in {"update", "end"} or (action == "update" and (minutes_allowed <= 0 or period_end < period_start)):
            raise HTTPException(400, "Allotment change is invalid.")
        with database.session() as db:
            allotment = db.get(Allotment, allotment_id)
            if not allotment: raise HTTPException(404, "Allotment not found.")
            before = {"period_start": allotment.period_start, "period_end": allotment.period_end, "minutes_allowed": allotment.minutes_allowed}
            if action == "end":
                if allotment.period_start > date.today().isoformat():
                    db.delete(allotment)
                    audit(db, "canceled", "allotment", allotment_id, actor_user_id=admin.id, reason=reason, before=before, after={"removed": True}, request_id=request.state.request_id)
                    return _redirect("/admin?message=Future+allotment+canceled")
                allotment.period_end = min(allotment.period_end, date.today().isoformat())
            else:
                allotment.period_start, allotment.period_end, allotment.minutes_allowed = period_start, period_end, minutes_allowed
            audit(db, "ended" if action == "end" else "updated", "allotment", allotment.id, actor_user_id=admin.id, reason=reason, before=before, after={"period_start": allotment.period_start, "period_end": allotment.period_end, "minutes_allowed": allotment.minutes_allowed}, request_id=request.state.request_id)
        return _redirect("/admin?message=Allotment+updated")

    @app.post("/admin/instruments/{instrument_id}/blocks")
    def admin_blocks(request: Request, instrument_id: int, csrf_token: Annotated[str, Form()], effective_from: Annotated[date, Form()], blocks: Annotated[str, Form()]):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        try:
            with database.session() as db:
                replace_block_definitions(db, instrument_id=instrument_id, effective_from=effective_from, blocks_text=blocks, actor_user_id=admin.id, request_id=request.state.request_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _redirect("/admin?message=Future+block+version+created")

    def selected_month(value: str) -> tuple[int, int]:
        try:
            parsed = datetime.strptime(value, "%Y-%m") if value else datetime.now()
            if not 2000 <= parsed.year <= 2100:
                raise ValueError
            return parsed.year, parsed.month
        except ValueError as exc:
            raise HTTPException(400, "Month must use YYYY-MM format.") from exc

    def adjacent_month(year: int, month: int, offset: int) -> str:
        index = year * 12 + month - 1 + offset
        return f"{index // 12:04d}-{index % 12 + 1:02d}"

    @app.get("/calendar", response_class=HTMLResponse)
    def calendar_page(request: Request, instrument_id: int | None = None, month: str = "", message: str = ""):
        _require_user(request)
        with database.session() as db:
            if not setup_complete(db):
                return _redirect("/setup-required")
            instruments = db.scalars(select(Instrument).where(Instrument.active.is_(True)).order_by(Instrument.name)).all()
            instrument = db.get(Instrument, instrument_id) if instrument_id else (instruments[0] if instruments else None)
            if not instrument or instrument not in instruments:
                raise HTTPException(404, "Instrument not found.")
            year, month_number = selected_month(month)
            month_value = f"{year:04d}-{month_number:02d}"
            days = month_days(year, month_number)
            slots = calendar_data(db, instrument.id, year, month_number, get_setting(db, "timezone", "UTC") or "UTC")
            slots_by_day: dict[str, list[dict[str, object]]] = {}
            for slot in slots:
                slots_by_day.setdefault(str(slot["date"]), []).append(slot)
            accent_index = list(instruments).index(instrument)
            values = dict(instruments=instruments, instrument=instrument, accent_index=accent_index, month=month_value, month_label=datetime(year, month_number, 1).strftime("%B %Y"), selected_month=month_number, days=days, slots_by_day=slots_by_day, timezone=get_setting(db, "timezone", "UTC"), previous_month=adjacent_month(year, month_number, -1), next_month=adjacent_month(year, month_number, 1))
        return templates.TemplateResponse(request, "calendar.html", _context(request, message=message, **values))

    @app.get("/api/calendar")
    def calendar_api(request: Request, instrument_id: int, month: str):
        _require_user(request)
        year, month_number = selected_month(month)
        with database.session() as db:
            if not setup_complete(db):
                return _redirect("/setup-required")
            instrument = db.get(Instrument, instrument_id)
            if not instrument or not instrument.active:
                raise HTTPException(404, "Instrument not found.")
            slots = calendar_data(db, instrument_id, year, month_number, get_setting(db, "timezone", "UTC") or "UTC")
        return {"instrument_id": instrument_id, "month": f"{year:04d}-{month_number:02d}", "slots": slots}

    @app.post("/admin/maintenance")
    def maintenance_create(request: Request, csrf_token: Annotated[str, Form()], instrument_id: Annotated[int, Form()], start_date: Annotated[date, Form()], end_date: Annotated[date, Form()], kind: Annotated[str, Form()], reason: Annotated[str, Form()]):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        try:
            with database.session() as db:
                create_maintenance(db, instrument_id=instrument_id, start_date=start_date, end_date=end_date, kind=kind, reason=reason, actor_user_id=admin.id, request_id=request.state.request_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return _redirect(f"/calendar?instrument_id={instrument_id}&month={start_date.strftime('%Y-%m')}")

    @app.get("/entries/{entry_id}", response_class=HTMLResponse)
    def entry_detail(request: Request, entry_id: int):
        _require_user(request)
        with database.session() as db:
            entry = db.get(CalendarEntry, entry_id)
            if not entry:
                raise HTTPException(404, "Calendar entry not found.")
            instrument = db.get(Instrument, entry.instrument_id)
            slots = db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry.id).order_by(ReservedSlot.slot_start)).all()
            booking = db.scalar(select(Booking).where(Booking.calendar_entry_id == entry.id))
            maintenance = db.scalar(select(Maintenance).where(Maintenance.calendar_entry_id == entry.id))
            handler = db.get(User, booking.user_id) if booking else None
            reserved_minutes = sum(slot.nominal_minutes for slot in slots)
        can_edit = bool(booking and (request.state.user.role == "admin" or (booking.user_id == request.state.user.id and entry.state in {"tentative", "confirmed"} and slots and parse_utc(slots[0].slot_start) > utcnow())))
        return templates.TemplateResponse(request, "entry_detail.html", _context(request, entry=entry, instrument=instrument, slots=slots, booking=booking, maintenance=maintenance, handler=handler, can_edit=can_edit, reserved_minutes=reserved_minutes))

    @app.get("/book/new", response_class=HTMLResponse)
    def booking_select(request: Request, instrument_id: int, slot_start: str, error: str = ""):
        _require_user(request)
        with database.session() as db:
            instrument = db.get(Instrument, instrument_id)
            if not instrument:
                raise HTTPException(404, "Instrument not found.")
            hold_minutes = get_setting(db, "hold_minutes", "15")
        return templates.TemplateResponse(request, "booking_select.html", _context(request, instrument_id=instrument_id, slot_start=slot_start, hold_minutes=hold_minutes, error=error))

    @app.post("/book/hold")
    def booking_hold(request: Request, csrf_token: Annotated[str, Form()], instrument_id: Annotated[int, Form()], slot_start: Annotated[str, Form()], count: Annotated[int, Form()] = 1):
        user = _require_user(request); _check_csrf(request, csrf_token)
        try:
            with database.session() as db:
                hold, token, _slots = acquire_hold(db, user=user, instrument_id=instrument_id, start_text=slot_start, count=count, request_id=request.state.request_id)
                hold_id = hold.id
        except ValueError as exc:
            with database.session() as db:
                hold_minutes = get_setting(db, "hold_minutes", "15")
            return templates.TemplateResponse(request, "booking_select.html", _context(request, instrument_id=instrument_id, slot_start=slot_start, hold_minutes=hold_minutes, error=str(exc)), status_code=409)
        response = _redirect(f"/book/{hold_id}")
        response.set_cookie(_hold_cookie(instrument_id), token, httponly=True, secure=settings.secure_cookies, samesite="lax", max_age=3600, path="/")
        return response

    @app.get("/book/{hold_id}", response_class=HTMLResponse)
    def booking_form(request: Request, hold_id: int, error: str = ""):
        user = _require_user(request)
        try:
            with database.session() as db:
                token = _hold_token(request, db, hold_id)
                hold, entry, slots = resolve_hold(db, hold_id=hold_id, token=token, user_id=user.id)
                pis = db.scalars(select(PI).where(PI.active.is_(True)).order_by(PI.name)).all()
                project_query = select(Project).where(Project.active.is_(True))
                if user.role != "admin":
                    project_query = project_query.join(UserProject, UserProject.project_id == Project.id).where(UserProject.user_id == user.id)
                projects = db.scalars(project_query.order_by(Project.code)).all()
                project_ids = [project.id for project in projects]
                grants = db.scalars(select(Grant).join(ProjectGrant, ProjectGrant.grant_id == Grant.id).where(Grant.active.is_(True), ProjectGrant.project_id.in_(project_ids)).distinct().order_by(Grant.number)).all() if project_ids else []
                session_record = db.scalar(select(SessionRecord).where(SessionRecord.token_hash == hash_token(request.cookies.get("instrumenthub_session", ""))))
                saved = json.loads(session_record.wizard_json) if session_record and session_record.wizard_json else {}
                values = dict(hold=hold, hold_token=token, instrument_id=entry.instrument_id, slots=slots, reserved_minutes=sum(slot.nominal_minutes for slot in slots), pis=pis, projects=projects, grants=grants, saved=saved, error=error)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return templates.TemplateResponse(request, "booking_form.html", _context(request, **values))

    @app.post("/book/submit")
    def booking_submit(
        request: Request, csrf_token: Annotated[str, Form()], hold_id: Annotated[int, Form()],
        pi_id: Annotated[int, Form()], project_id: Annotated[int, Form()], experiment_code: Annotated[str, Form()],
        sample_category: Annotated[str, Form()], instrument_id: Annotated[int | None, Form()] = None, grant_id: Annotated[str, Form()] = "",
        expected_minutes: Annotated[str, Form()] = "", injection_count: Annotated[str, Form()] = "",
        gradient_minutes: Annotated[str, Form()] = "", description: Annotated[str, Form()] = "",
        admin_notes: Annotated[str, Form()] = "", override_required: Annotated[str | None, Form()] = None,
    ):
        user = _require_user(request); _check_csrf(request, csrf_token)
        saved = {"pi_id": str(pi_id), "project_id": str(project_id), "instrument_id": str(instrument_id or ""), "grant_id": grant_id, "experiment_code": experiment_code, "expected_minutes": expected_minutes, "injection_count": injection_count, "gradient_minutes": gradient_minutes, "sample_category": sample_category, "description": description, "admin_notes": admin_notes, "override_required": bool(override_required)}
        selected_instrument_id = instrument_id or 0
        try:
            with database.session() as db:
                if not selected_instrument_id:
                    raise ValueError("The selected instrument is unavailable.")
                token = request.cookies.get(_hold_cookie(selected_instrument_id), "")
                booking, _results = submit_booking(db, hold_id=hold_id, token=token, user=user, pi_id=pi_id, project_id=project_id, grant_id=int(grant_id) if grant_id else None, experiment_code=experiment_code, expected_minutes=int(expected_minutes) if expected_minutes else None, injection_count=int(injection_count) if injection_count else None, gradient_minutes=int(gradient_minutes) if gradient_minutes else None, sample_category=sample_category, description=description, admin_notes=admin_notes, override_required=bool(override_required), request_id=request.state.request_id, expected_instrument_id=selected_instrument_id)
                clear_wizard_state(db, request.cookies.get("instrumenthub_session"))
                entry_id = booking.calendar_entry_id
        except (ValueError, TypeError, IndexError) as exc:
            with database.session() as db:
                save_wizard_state(db, request.cookies.get("instrumenthub_session"), saved)
            if selected_instrument_id and (isinstance(exc, IndexError) or any(word in str(exc).lower() for word in ("hold", "block", "available", "occup", "missing"))):
                return _redirect(f"/calendar?{urlencode({'instrument_id': selected_instrument_id, 'message': f'{exc} Choose available blocks to continue.'})}")
            return _redirect(f"/book/{hold_id}?{urlencode({'error': str(exc)})}")
        response = _redirect(f"/entries/{entry_id}")
        response.delete_cookie(_hold_cookie(selected_instrument_id), path="/")
        return response

    @app.post("/api/holds")
    async def holds_api(request: Request):
        user = _require_user(request); _check_csrf(request, request.headers.get("X-CSRF-Token", ""))
        payload = await request.json()
        try:
            instrument_id = int(payload["instrument_id"])
            with database.session() as db:
                hold, token, slots = acquire_hold(db, user=user, instrument_id=instrument_id, start_text=str(payload["slot_start"]), count=int(payload.get("count", 1)), request_id=request.state.request_id)
                result = {"id": hold.id, "instrument_id": instrument_id, "expires_at": hold.expires_at, "slots": [slot.start_text for slot in slots]}
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        response = JSONResponse(result, status_code=201)
        response.set_cookie(_hold_cookie(instrument_id), token, httponly=True, secure=settings.secure_cookies, samesite="lax", max_age=3600, path="/")
        return response

    @app.get("/api/holds/{hold_id}")
    def hold_status_api(request: Request, hold_id: int):
        user = _require_user(request)
        try:
            with database.session() as db:
                hold, _entry, slots = resolve_hold(db, hold_id=hold_id, token=_hold_token(request, db, hold_id), user_id=user.id)
                return {"id": hold.id, "expires_at": hold.expires_at, "slots": [slot.slot_start for slot in slots]}
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.delete("/api/holds/{hold_id}")
    def hold_release_api(request: Request, hold_id: int):
        user = _require_user(request); _check_csrf(request, request.headers.get("X-CSRF-Token", ""))
        try:
            with database.session() as db:
                tokens = [value for name, value in request.cookies.items() if name.startswith("instrumenthub_hold_")]
                instrument_id = release_hold(db, hold_id=hold_id, tokens=tokens, user_id=user.id, request_id=request.state.request_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        response = Response(status_code=204)
        response.delete_cookie(_hold_cookie(instrument_id), path="/")
        return response

    @app.post("/api/rules/preview")
    async def rules_preview_api(request: Request):
        user = _require_user(request); _check_csrf(request, request.headers.get("X-CSRF-Token", "")); payload = await request.json()
        try:
            with database.session() as db:
                hold, entry, rows = resolve_hold(db, hold_id=int(payload["hold_id"]), token=_hold_token(request, db, int(payload["hold_id"])), user_id=user.id)
                project = db.get(Project, int(payload["project_id"]))
                slots = slot_values(rows)
                instrument = db.get(Instrument, entry.instrument_id)
                pi = db.get(PI, int(payload["pi_id"]))
                results = evaluate_booking(db, user=db.get(User, user.id), instrument=instrument, pi=pi, project=project, slots=slots, override_required=bool(payload.get("override_required")), exclude_calendar_entry_id=entry.id, hold_valid=True)
                balances = allotment_balances(db, pi=pi, project=project, instrument=instrument, slots=slots, exclude_calendar_entry_id=entry.id) if pi and project and instrument else []
        except ValueError:
            results = [RuleResult("hold_lost", "blocked", "The temporary hold has expired or is no longer valid.")]
        return {"results": [result.as_dict() for result in results], "reserved_minutes": sum(slot.nominal_minutes for slot in slots) if 'slots' in locals() else 0, "project_consecutive_limit": project.max_consecutive_minutes if 'project' in locals() and project else None, "allotment_balances": balances if 'balances' in locals() else []}

    @app.get("/api/duration-estimate")
    def duration_estimate(request: Request, injection_count: int, gradient_minutes: int):
        _require_user(request)
        try:
            return {"minutes": estimate_duration_minutes(injection_count, gradient_minutes)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/bookings/{booking_id}/edit", response_class=HTMLResponse)
    def booking_edit_page(request: Request, booking_id: int):
        actor = _require_user(request)
        with database.session() as db:
            booking = db.get(Booking, booking_id)
            if not booking:
                raise HTTPException(404, "Booking not found.")
            entry = db.get(CalendarEntry, booking.calendar_entry_id)
            rows = db.scalars(select(ReservedSlot).where(ReservedSlot.calendar_entry_id == entry.id).order_by(ReservedSlot.slot_start)).all()
            if actor.role != "admin" and not owner_can_manage_booking(booking, entry, rows, actor.id):
                raise HTTPException(403, "This booking is not eligible for editing.")
            block_count = len(rows)
            schedule_editable = entry.state not in {"completed", "canceled", "rejected"}
        return templates.TemplateResponse(request, "booking_edit.html", _context(request, booking=booking, entry=entry, block_count=block_count, schedule_editable=schedule_editable))

    @app.post("/bookings/{booking_id}/edit")
    def booking_edit_submit(request: Request, booking_id: int, csrf_token: Annotated[str, Form()], experiment_code: Annotated[str, Form()], expected_minutes: Annotated[int, Form()], sample_category: Annotated[str, Form()], description: Annotated[str, Form()] = "", admin_notes: Annotated[str, Form()] = "", new_start: Annotated[str, Form()] = "", block_count: Annotated[int | None, Form()] = None, reason: Annotated[str, Form()] = ""):
        actor = _require_user(request); _check_csrf(request, csrf_token)
        try:
            with database.session() as db:
                edit_booking(db, booking_id=booking_id, actor=actor, experiment_code=experiment_code, expected_minutes=expected_minutes, sample_category=sample_category, description=description, admin_notes=admin_notes, new_start=new_start.strip() or None, block_count=block_count, reason=reason, request_id=request.state.request_id)
                booking = db.get(Booking, booking_id); entry_id = booking.calendar_entry_id
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return _redirect(f"/entries/{entry_id}")

    @app.post("/bookings/{booking_id}/cancel")
    def booking_cancel(request: Request, booking_id: int, csrf_token: Annotated[str, Form()], reason: Annotated[str, Form()] = ""):
        actor = _require_user(request); _check_csrf(request, csrf_token)
        try:
            with database.session() as db:
                cancellation_reason = reason.strip() if actor.role == "admin" else (reason.strip() or "User cancellation")
                cancel_booking(db, booking_id=booking_id, actor=actor, reason=cancellation_reason, request_id=request.state.request_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return _redirect("/calendar")

    @app.get("/admin/schedule", response_class=HTMLResponse)
    def schedule_admin_page(request: Request, message: str = ""):
        _require_admin(request)
        with database.session() as db:
            prune_expired_holds(db)
            tentative = [(booking, entry, db.get(User, booking.user_id)) for booking, entry in db.execute(select(Booking, CalendarEntry).join(CalendarEntry, CalendarEntry.id == Booking.calendar_entry_id).where(CalendarEntry.state == "tentative")).all()]
            live_holds = db.execute(
                select(Hold, CalendarEntry, User, Instrument)
                .join(CalendarEntry, CalendarEntry.id == Hold.calendar_entry_id)
                .join(User, User.id == Hold.user_id)
                .join(Instrument, Instrument.id == CalendarEntry.instrument_id)
                .order_by(Hold.expires_at, Hold.id)
            ).all()
            draft_views = []
            for draft in db.scalars(select(ScheduleChangeDraft).order_by(ScheduleChangeDraft.id.desc())):
                payload = json.loads(draft.payload_json)
                items = []
                for item in payload.get("items", []):
                    entry = db.get(CalendarEntry, item["entry_id"])
                    items.append({**item, "title": entry.title if entry else f"Entry {item['entry_id']}", "entry_type": entry.entry_type if entry else "unknown"})
                draft_views.append(SimpleNamespace(draft=draft, outage_slots=payload.get("outage_slots", []), restart_at=payload.get("restart_at"), items=items))
            values = dict(tentative=tentative, live_holds=live_holds, instruments=db.scalars(select(Instrument).where(Instrument.active.is_(True)).order_by(Instrument.name)).all(), draft_views=draft_views, audits=db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(100)).all(), message=message)
        return templates.TemplateResponse(request, "admin_schedule.html", _context(request, **values))

    @app.post("/admin/holds/{hold_id}/release")
    def admin_hold_release(request: Request, hold_id: int, csrf_token: Annotated[str, Form()], reason: Annotated[str, Form()]):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        try:
            with database.session() as db:
                release_hold_as_admin(db, hold_id=hold_id, admin_id=admin.id, reason=reason, request_id=request.state.request_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return _redirect("/admin/schedule?message=Hold+released")

    @app.post("/admin/bookings/{booking_id}/decision")
    def booking_decision(request: Request, booking_id: int, csrf_token: Annotated[str, Form()], decision: Annotated[str, Form()], reason: Annotated[str, Form()]):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        try:
            with database.session() as db: decide_booking(db, booking_id=booking_id, decision=decision, admin_id=admin.id, reason=reason, request_id=request.state.request_id)
        except ValueError as exc: raise HTTPException(409, str(exc)) from exc
        return _redirect("/admin/schedule")

    @app.post("/admin/bookings/{booking_id}/transition")
    def booking_transition(request: Request, booking_id: int, csrf_token: Annotated[str, Form()], new_state: Annotated[str, Form()], reason: Annotated[str, Form()]):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        try:
            with database.session() as db: entry_id = transition_booking(db, booking_id=booking_id, new_state=new_state, admin_id=admin.id, reason=reason, request_id=request.state.request_id)
        except ValueError as exc: raise HTTPException(409, str(exc)) from exc
        return _redirect(f"/entries/{entry_id}")

    @app.post("/admin/outages/preview")
    def outage_preview(request: Request, csrf_token: Annotated[str, Form()], instrument_id: Annotated[int, Form()], start_date: Annotated[date, Form()], reason: Annotated[str, Form()], end_date: Annotated[str, Form()] = ""):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        try:
            parsed_end = date.fromisoformat(end_date) if end_date else None
            with database.session() as db: preview_outage(db, instrument_id=instrument_id, start_date=start_date, end_date=parsed_end, reason=reason, admin_id=admin.id, request_id=request.state.request_id)
        except ValueError as exc: raise HTTPException(409, str(exc)) from exc
        return _redirect("/admin/schedule")

    @app.post("/admin/outages/resume-preview")
    def outage_resume_preview(request: Request, csrf_token: Annotated[str, Form()], instrument_id: Annotated[int, Form()], restart_date: Annotated[date, Form()], reason: Annotated[str, Form()]):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        try:
            with database.session() as db: preview_resume(db, instrument_id=instrument_id, restart_date=restart_date, admin_id=admin.id, reason=reason, request_id=request.state.request_id)
        except ValueError as exc: raise HTTPException(409, str(exc)) from exc
        return _redirect("/admin/schedule")

    @app.post("/admin/outages/recover")
    def outage_recover(request: Request, csrf_token: Annotated[str, Form()], instrument_id: Annotated[int, Form()], reason: Annotated[str, Form()]):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        try:
            with database.session() as db: recover_unknown_outage(db, instrument_id=instrument_id, admin_id=admin.id, reason=reason, request_id=request.state.request_id)
        except ValueError as exc: raise HTTPException(409, str(exc)) from exc
        return _redirect("/admin/schedule")

    @app.post("/admin/schedule-drafts/{draft_id}/apply")
    def schedule_apply(request: Request, draft_id: int, csrf_token: Annotated[str, Form()]):
        admin = _require_admin(request); _check_csrf(request, csrf_token)
        try:
            with database.session() as db: apply_schedule_draft(db, draft_id=draft_id, admin_id=admin.id, request_id=request.state.request_id)
        except ValueError as exc: raise HTTPException(409, str(exc)) from exc
        return _redirect("/admin/schedule")

    @app.get("/admin/operations", response_class=HTMLResponse)
    def operations_page(request: Request):
        _require_admin(request)
        with database.session() as db:
            integrity = db.execute(text("PRAGMA integrity_check")).scalar_one()
            migration_count = db.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar_one()
        backups = [path.name for path in sorted(settings.backup_dir.glob("*.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True)[:10]]
        return templates.TemplateResponse(request, "operations.html", _context(request, integrity=integrity, migration_count=migration_count, backups=backups))

    return app
