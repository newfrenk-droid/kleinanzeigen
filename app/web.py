"""Веб-панель: аккаунты, шаблоны автоответа, диалоги, лог, настройки."""
from __future__ import annotations

import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import db
from .cookies import CookieParseError, parse_cookies
from .proxy import ProxyParseError, parse_proxy
from .replies import PLACEHOLDERS
from .vision import VisionClient
from .worker import BotManager

manager = BotManager()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["dt"] = lambda ts: datetime.fromtimestamp(ts).strftime("%d.%m %H:%M:%S") if ts else ""

STATUS_LABELS = {
    "stopped": "остановлен", "starting": "запускается", "running": "работает",
    "error": "ошибка", "cookies_invalid": "куки недействительны",
}
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    for acc in db.list_accounts():
        db.update_account(acc["id"], status="stopped")
    if db.get_settings().get("autostart") == "1":
        manager.start_all()
    yield
    await manager.stop_all()


_security = HTTPBasic(auto_error=False)


def auth(credentials: HTTPBasicCredentials | None = Depends(_security)) -> None:
    password = os.environ.get("PANEL_PASSWORD")
    if not password:
        return
    user = os.environ.get("PANEL_USER", "admin")
    if not credentials or not (
        secrets.compare_digest(credentials.username, user) and secrets.compare_digest(credentials.password, password)
    ):
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


app = FastAPI(title="Kleinanzeigen Autoresponder", lifespan=lifespan, dependencies=[Depends(auth)])


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ---------------- аккаунты ----------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    accounts = db.list_accounts()
    for a in accounts:
        a["running"] = manager.is_running(a["id"])
        a["ads_count"] = len(db.list_ads(a["id"]))
    return page(request, "index.html", accounts=accounts, settings=db.get_settings())


@app.get("/accounts/new", response_class=HTMLResponse)
async def account_new(request: Request):
    return page(request, "account.html", account=None, error="")


@app.get("/accounts/{account_id}", response_class=HTMLResponse)
async def account_edit(request: Request, account_id: int):
    account = db.get_account(account_id) or {}
    if not account:
        raise HTTPException(404)
    return page(request, "account.html", account=account, error="", ads=db.list_ads(account_id))


@app.post("/accounts/save", response_class=HTMLResponse)
async def account_save(request: Request, account_id: int = Form(0), name: str = Form(...),
                       cookies: str = Form(""), proxy: str = Form(""), enabled: str = Form("")):
    existing = db.get_account(account_id) if account_id else None
    form = {"id": account_id, "name": name, "proxy": proxy, "cookies_raw": cookies, "enabled": bool(enabled)}
    try:
        parsed_proxy = parse_proxy(proxy)
        parsed_cookies = parse_cookies(cookies) if (cookies.strip() or not existing) else None
    except (CookieParseError, ProxyParseError) as e:
        return page(request, "account.html", account=existing or form, form=form, error=str(e))

    proxy_str = str(parsed_proxy) if parsed_proxy else ""
    if existing:
        fields = {"name": name, "enabled": 1 if enabled else 0}
        if parsed_cookies is not None:
            fields.update(cookies=json.dumps(parsed_cookies), cookies_applied=0)
        if proxy_str != existing["proxy"]:
            fields["proxy"] = proxy_str
            if existing["vision_profile_id"]:
                try:
                    vision = VisionClient.from_settings(db.get_settings())
                    new_proxy_id = await vision.create_proxy(f"KA {name}", parsed_proxy) if parsed_proxy else None
                    await vision.update_profile_proxy(existing["vision_profile_id"], new_proxy_id)
                    fields["vision_proxy_id"] = new_proxy_id or ""
                    db.add_log(account_id, "info", "Прокси профиля Vision обновлён")
                except Exception as e:
                    db.add_log(account_id, "error", f"Не удалось обновить прокси в Vision: {e}")
        db.update_account(account_id, **fields)
        if manager.is_running(account_id):
            await manager.stop(account_id)
            manager.start(account_id)
    else:
        account_id = db.create_account(name, parsed_cookies, proxy_str)
        if not enabled:
            db.update_account(account_id, enabled=0)
        db.add_log(account_id, "info", f"Аккаунт добавлен, куки: {len(parsed_cookies)} шт.")
    return back("/")


@app.post("/accounts/{account_id}/start")
async def account_start(account_id: int):
    manager.start(account_id)
    return back("/")


@app.post("/accounts/{account_id}/stop")
async def account_stop(account_id: int):
    await manager.stop(account_id)
    return back("/")


@app.post("/accounts/{account_id}/reset-profile")
async def account_reset_profile(account_id: int):
    """Отвязать профиль Vision — при следующем запуске будет создан новый."""
    account = db.get_account(account_id)
    if account:
        await manager.stop(account_id)
        if account["vision_profile_id"]:
            try:
                await VisionClient.from_settings(db.get_settings()).delete_profile(account["vision_profile_id"])
            except Exception as e:
                db.add_log(account_id, "warning", f"Старый профиль Vision не удалён: {e}")
        db.update_account(account_id, vision_profile_id="", vision_proxy_id="", cookies_applied=0)
        db.add_log(account_id, "info", "Профиль Vision отвязан, при запуске будет создан новый")
    return back(f"/accounts/{account_id}")


@app.post("/accounts/{account_id}/delete")
async def account_delete(account_id: int):
    await manager.stop(account_id)
    db.delete_account(account_id)
    return back("/")


@app.post("/start-all")
async def start_all():
    manager.start_all()
    return back("/")


@app.post("/stop-all")
async def stop_all():
    await manager.stop_all()
    return back("/")


# ---------------- шаблоны ----------------

@app.get("/templates", response_class=HTMLResponse)
async def templates_list(request: Request):
    accounts = {a["id"]: a["name"] for a in db.list_accounts()}
    return page(request, "templates.html", items=db.list_templates(), accounts=accounts)


@app.get("/templates/{template_id}", response_class=HTMLResponse)
async def template_edit(request: Request, template_id: int):
    item = db.get_template(template_id) if template_id else None
    if template_id and not item:
        raise HTTPException(404)
    return page(request, "template.html", item=item, accounts=db.list_accounts(),
                ads=db.list_ads(), placeholders=PLACEHOLDERS)


@app.post("/templates/save")
async def template_save(template_id: int = Form(0), name: str = Form(...), text: str = Form(...),
                        account_id: str = Form(""), ad_id: str = Form(""), keywords: str = Form(""),
                        priority: int = Form(0), enabled: str = Form("")):
    db.save_template(
        template_id or None, name=name.strip(), text=text.replace("\r\n", "\n").strip(),
        account_id=int(account_id) if account_id else None, ad_id=ad_id.strip(),
        keywords=keywords.strip(), priority=priority, enabled=1 if enabled else 0,
    )
    return back("/templates")


@app.post("/templates/{template_id}/delete")
async def template_delete(template_id: int):
    db.delete_template(template_id)
    return back("/templates")


# ---------------- диалоги, лог, настройки ----------------

@app.get("/dialogs", response_class=HTMLResponse)
async def dialogs(request: Request):
    return page(request, "dialogs.html", items=db.list_conversations())


@app.get("/logs", response_class=HTMLResponse)
async def logs(request: Request, account_id: int | None = None):
    return page(request, "logs.html", items=db.list_log(account_id=account_id))


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: int = 0):
    return page(request, "settings.html", s=db.get_settings(), saved=saved)


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    values = {k: str(v).strip() for k, v in form.items() if k in db.DEFAULT_SETTINGS}
    # чекбоксы: если не отмечены, в форме их нет
    for key in ("only_as_seller", "autostart", "vision_stop_on_exit"):
        values[key] = "1" if form.get(key) else "0"
    db.save_settings(values)
    return back("/settings?saved=1")
