import asyncio
import json
import logging
import os
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jose import JWTError, jwt
from pydantic import ValidationError

load_dotenv()

import auth
from auth import authenticate_user, create_access_token, get_current_active_user, get_financial_profile, new_session_id, register_user, save_financial_profile, save_session_data
from gemini_utils import ai_is_configured, get_home_recommendations, get_jewelry_recommendations, get_party_recommendations
from models import FinancialProfileInput, HomeBudgetInput, JewelryBudgetInput, PartyBudgetInput, RegisterInput, SessionDataInput

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pocketsmart")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = auth.DATA_DIR
HISTORY_FILE = DATA_DIR / "recommendations.json"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
SESSION_IDLE_SECONDS = 30 * 60
COOKIE_NAME = "access_token"
sessions: dict[str, dict[str, Any]] = {}
revoked_sessions: set[str] = set()
_history_lock = threading.RLock()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def get_session_user(session_id: str, username: str) -> dict[str, Any] | None:
    session = sessions.get(session_id)
    if not session or session_id in revoked_sessions or session["username"].casefold() != username.casefold():
        return None
    current = now_utc()
    if (current - session["last_activity"]).total_seconds() > SESSION_IDLE_SECONDS:
        sessions.pop(session_id, None)
        return None
    session["last_activity"] = current
    return {"username": session["username"], "full_name": session["full_name"], "email": session["email"]}


async def expire_idle_sessions() -> None:
    while True:
        await asyncio.sleep(300)
        current = now_utc()
        expired = [key for key, value in sessions.items() if (current - value["last_activity"]).total_seconds() > SESSION_IDLE_SECONDS]
        for key in expired:
            sessions.pop(key, None)


@asynccontextmanager
async def lifespan(_: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(expire_idle_sessions())
    if not ai_is_configured():
        logger.warning("Gemini is not configured. Add GOOGLE_API_KEY to .env to enable AI recommendations.")
    if not auth.valid_secret_key():
        logger.warning("SECRET_KEY is missing or too short. Set a random SECRET_KEY of at least 32 characters before deployment.")
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="PocketSmart AI", description="Your smart budget and recommendation assistant.", lifespan=lifespan)
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.exception_handler(HTTPException)
async def handle_http_exception(request: Request, error: HTTPException):
    if error.status_code == 401 and request.url.path not in {"/token", "/register"} and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)
    return JSONResponse(status_code=error.status_code, content={"detail": error.detail}, headers=error.headers)


def _render(request: Request, template: str, **context: Any) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name=template, context=context)


def _read_history() -> list[dict[str, Any]]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with HISTORY_FILE.open("r", encoding="utf-8") as file:
            entries = json.load(file)
            return entries if isinstance(entries, list) else []
    except FileNotFoundError:
        return []


def _write_history(entries: list[dict[str, Any]]) -> None:
    descriptor, temporary_path = tempfile.mkstemp(dir=DATA_DIR, prefix="history-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(entries, file, ensure_ascii=True, indent=2)
        os.replace(temporary_path, HISTORY_FILE)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _result_summary(result: dict[str, Any]) -> str:
    count = sum(len(category.get("items", [])) for category in result.get("budget_breakdown", []))
    count += len(result.get("jewelry_recommendations", []))
    return f"₹{result.get('total_budget', 0):,.0f} plan · {count} recommendations · ₹{result.get('remaining_budget', 0):,.0f} remaining"


def _save_recommendation(username: str, kind: str, inputs: dict[str, Any], result: dict[str, Any], image_filename: str | None = None) -> dict[str, Any]:
    entry = {"id": uuid.uuid4().hex, "username": username, "type": kind, "timestamp": now_utc().isoformat(), "input_summary": inputs, "result_summary": _result_summary(result), "result": result, "image_filename": image_filename}
    with _history_lock:
        entries = _read_history()
        entries.append(entry)
        _write_history(entries)
    return entry


def _user_history(username: str) -> list[dict[str, Any]]:
    with _history_lock:
        return sorted((item for item in _read_history() if item.get("username", "").casefold() == username.casefold()), key=lambda item: item.get("timestamp", ""), reverse=True)


def _available_budget(profile: dict[str, Any]) -> float | None:
    if not profile or "monthly_net_income" not in profile:
        return None
    available = (
        Decimal(str(profile["monthly_net_income"]))
        - Decimal(str(profile.get("monthly_essential_expenses", 0)))
        - Decimal(str(profile.get("monthly_savings_goal", 0)))
    )
    return float(max(Decimal("0"), available).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


async def _parsed_form(model: type, request: Request):
    try:
        if "application/json" in request.headers.get("content-type", ""):
            values = await request.json()
        else:
            form = await request.form()
            values = dict(form)
            if model is HomeBudgetInput:
                values["rooms"] = form.getlist("rooms")
        return model.model_validate(values)
    except (ValidationError, ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def _set_auth_cookie(response: JSONResponse, token: str) -> None:
    response.set_cookie(key=COOKIE_NAME, value=token, max_age=auth.ACCESS_TOKEN_EXPIRE_MINUTES * 60, httponly=True, secure=os.getenv("COOKIE_SECURE", "false").casefold() == "true", samesite="lax", path="/")


@app.get("/", response_class=HTMLResponse, name="landing")
async def landing(request: Request):
    return _render(request, "index.html", ai_ready=ai_is_configured())


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return _render(request, "register.html", next=request.query_params.get("next", ""))


@app.post("/register", status_code=201)
async def create_account(request: Request):
    payload = await _parsed_form(RegisterInput, request)
    user = register_user(payload.username, str(payload.email), payload.full_name, payload.password)
    return JSONResponse(status_code=201, content={"message": "Account created. You can now sign in.", "user": user})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return _render(request, "login.html", registered=request.query_params.get("registered") == "1", next=request.query_params.get("next", ""))


def _issue_session(username: str, user: dict[str, Any]) -> str:
    session_id = new_session_id()
    issued_at = now_utc()
    sessions[session_id] = {"username": username, "full_name": user.get("full_name", ""), "email": user.get("email", ""), "login_time": issued_at, "last_activity": issued_at, "session_data": {}}
    return create_access_token(username, session_id)


@app.post("/token")
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(status_code=401, detail="Incorrect username or password.", headers={"WWW-Authenticate": "Bearer"})
    token = _issue_session(user["username"], user)
    response = JSONResponse(content={"access_token": token, "token_type": "bearer", "username": user["username"], "expires_in": auth.ACCESS_TOKEN_EXPIRE_MINUTES * 60})
    _set_auth_cookie(response, token)
    return response


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            payload = jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
            session_id = payload.get("jti")
            if session_id:
                revoked_sessions.add(session_id)
                sessions.pop(session_id, None)
        except JWTError:
            pass
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/session-info")
async def session_info(request: Request, user: dict[str, Any] = Depends(get_current_active_user)):
    token = request.headers.get("authorization", "").removeprefix("Bearer ") or request.cookies.get(COOKIE_NAME, "")
    try:
        session_id = jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])["jti"]
        session = sessions[session_id]
    except (JWTError, KeyError):
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.") from None
    current = now_utc()
    session["last_activity"] = current
    return {"username": user["username"], "login_time": session["login_time"].isoformat(), "last_activity": current.isoformat(), "session_duration": max(0, int((current - session["login_time"]).total_seconds()))}


@app.post("/session-data")
async def session_data(payload: SessionDataInput, request: Request, user: dict[str, Any] = Depends(get_current_active_user)):
    save_session_data(user["username"], payload.data)
    token = request.headers.get("authorization", "").removeprefix("Bearer ") or request.cookies.get(COOKIE_NAME, "")
    try:
        session_id = jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])["jti"]
        sessions[session_id]["session_data"] = payload.data
    except (JWTError, KeyError):
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.") from None
    return {"message": "Personalization settings saved.", "data": payload.data}


@app.get("/financial-profile", response_class=HTMLResponse)
async def financial_profile_page(request: Request, user: dict[str, Any] = Depends(get_current_active_user)):
    profile = get_financial_profile(user["username"])
    return _render(request, "financial_profile.html", user=user, profile=profile, available_budget=_available_budget(profile))


@app.get("/api/financial-profile")
async def financial_profile_data(user: dict[str, Any] = Depends(get_current_active_user)):
    profile = get_financial_profile(user["username"])
    return {**profile, "available_budget": _available_budget(profile)}


@app.post("/financial-profile")
async def update_financial_profile(payload: FinancialProfileInput, user: dict[str, Any] = Depends(get_current_active_user)):
    profile = payload.model_dump()
    save_financial_profile(user["username"], profile)
    return {**profile, "available_budget": _available_budget(profile)}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user: dict[str, Any] = Depends(get_current_active_user)):
    profile = get_financial_profile(user["username"])
    return _render(request, "dashboard.html", user=user, recent=_user_history(user["username"])[:3], ai_ready=ai_is_configured(), available_budget=_available_budget(profile))


@app.get("/home-planner", response_class=HTMLResponse)
async def home_planner(request: Request, user: dict[str, Any] = Depends(get_current_active_user)):
    profile = get_financial_profile(user["username"])
    return _render(request, "home_planner.html", user=user, ai_ready=ai_is_configured(), profile=profile, available_budget=_available_budget(profile))


@app.post("/home-budget")
async def home_budget(payload: HomeBudgetInput, user: dict[str, Any] = Depends(get_current_active_user)):
    result = await asyncio.to_thread(get_home_recommendations, payload)
    entry = _save_recommendation(user["username"], "Home interior", payload.model_dump(), result)
    result.update({"recommendation_id": entry["id"], "timestamp": entry["timestamp"]})
    return result


@app.get("/party-planner", response_class=HTMLResponse)
async def party_planner(request: Request, user: dict[str, Any] = Depends(get_current_active_user)):
    profile = get_financial_profile(user["username"])
    return _render(request, "party_planner.html", user=user, ai_ready=ai_is_configured(), profile=profile, available_budget=_available_budget(profile))


@app.post("/party-budget")
async def party_budget(payload: PartyBudgetInput, user: dict[str, Any] = Depends(get_current_active_user)):
    result = await asyncio.to_thread(get_party_recommendations, payload)
    entry = _save_recommendation(user["username"], "Party & event", payload.model_dump(), result)
    result.update({"recommendation_id": entry["id"], "timestamp": entry["timestamp"]})
    return result


@app.get("/jewelry-planner", response_class=HTMLResponse)
async def jewelry_planner(request: Request, user: dict[str, Any] = Depends(get_current_active_user)):
    profile = get_financial_profile(user["username"])
    return _render(request, "jewelry_planner.html", user=user, ai_ready=ai_is_configured(), profile=profile, available_budget=_available_budget(profile))


@app.post("/jewelry-budget")
async def jewelry_budget(request: Request, user: dict[str, Any] = Depends(get_current_active_user), outfit_image: UploadFile | None = File(default=None)):
    form = await request.form()
    try:
        values = {key: form.get(key) for key in ("total_budget", "occasion", "preferences")}
        payload = JewelryBudgetInput.model_validate(values)
    except ValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    image_filename = None
    image_path = None
    if outfit_image and outfit_image.filename:
        if not (outfit_image.content_type or "").startswith("image/"):
            raise HTTPException(status_code=400, detail="Upload a valid outfit image.")
        content = await outfit_image.read(8 * 1024 * 1024 + 1)
        if len(content) > 8 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Outfit images must be 8 MB or smaller.")
        try:
            from PIL import Image
            import io

            with Image.open(io.BytesIO(content)) as image:
                image.verify()
                extension = "." + (image.format or "JPEG").lower().replace("jpeg", "jpg")
            if extension not in {".jpg", ".png", ".webp", ".gif", ".bmp"}:
                raise ValueError("unsupported image format")
        except Exception as error:
            raise HTTPException(status_code=400, detail="The uploaded file is not a supported image.") from error
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        image_filename = f"{now_utc().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}{extension}"
        image_path = UPLOAD_DIR / image_filename
        image_path.write_bytes(content)
    try:
        result = await asyncio.to_thread(get_jewelry_recommendations, payload, image_path)
    except Exception:
        if image_path:
            image_path.unlink(missing_ok=True)
        raise
    entry = _save_recommendation(user["username"], "Jewelry", payload.model_dump(), result, image_filename)
    result.update({"recommendation_id": entry["id"], "timestamp": entry["timestamp"], "image_filename": image_filename, "image_url": f"/static/uploads/{image_filename}" if image_filename else None})
    return result


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request, user: dict[str, Any] = Depends(get_current_active_user)):
    return _render(request, "history.html", user=user, history=_user_history(user["username"]))


@app.get("/recommendation-history")
async def recommendation_history(user: dict[str, Any] = Depends(get_current_active_user)):
    return [{key: value for key, value in entry.items() if key != "username"} for entry in _user_history(user["username"])]


@app.get("/recommendation-details/{recommendation_id}")
async def recommendation_details(recommendation_id: str, user: dict[str, Any] = Depends(get_current_active_user)):
    entry = next((item for item in _user_history(user["username"]) if item["id"] == recommendation_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Recommendation not found.")
    return {key: value for key, value in entry.items() if key != "username"}


@app.get("/static/uploads/{filename}")
async def private_outfit_image(filename: str, user: dict[str, Any] = Depends(get_current_active_user)):
    if Path(filename).name != filename or filename.startswith("."):
        raise HTTPException(status_code=404, detail="Image not found.")
    owned = any(entry.get("image_filename") == filename for entry in _user_history(user["username"]))
    image_path = UPLOAD_DIR / filename
    if not owned or not image_path.is_file():
        raise HTTPException(status_code=404, detail="Image not found.")
    return FileResponse(image_path)


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
