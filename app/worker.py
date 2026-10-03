"""Цикл работы одного аккаунта и менеджер всех аккаунтов."""
from __future__ import annotations

import asyncio
import json
import random
import time

from . import db
from .kleinanzeigen import Conversation, KleinanzeigenSession, Message, NotLoggedIn, needs_reply
from .proxy import parse_proxy
from .replies import choose_template, safe_render
from .vision import VisionClient


def _int(s: dict, key: str, default: int) -> int:
    try:
        return int(float(s.get(key, default)))
    except (TypeError, ValueError):
        return default


async def ensure_vision_profile(vision: VisionClient, account: dict, log) -> dict:
    """Создаёт в Vision прокси и профиль для аккаунта (если ещё нет) и импортирует куки."""
    if account["vision_profile_id"]:
        return account
    proxy_id = None
    proxy = parse_proxy(account["proxy"])
    if proxy:
        proxy_id = await vision.create_proxy(f"KA {account['name']}", proxy)
        log("info", f"Vision: создан прокси {proxy.host}:{proxy.port}")
    profile_id = await vision.create_profile(f"KA {account['name']}", proxy_id, notes="Kleinanzeigen autoresponder")
    log("info", f"Vision: создан профиль {profile_id}")
    fields = {"vision_profile_id": profile_id, "vision_proxy_id": proxy_id or ""}
    try:
        await vision.import_cookies(profile_id, json.loads(account["cookies"]))
        fields["cookies_applied"] = 1
        log("info", "Vision: куки импортированы в профиль")
    except Exception as e:
        log("warning", f"Vision: импорт куки через API не удался ({e}), подставлю их через браузер")
    db.update_account(account["id"], **fields)
    return db.get_account(account["id"])


class AccountWorker:
    def __init__(self, account_id: int):
        self.account_id = account_id
        self.retry_ids: set[str] = set()   # диалоги, где отправка сорвалась — повторим
        self.ads_synced_at = 0.0

    def log(self, level: str, message: str) -> None:
        db.add_log(self.account_id, level, message)

    def status(self, status: str, error: str = "") -> None:
        db.update_account(self.account_id, status=status, last_error=error)

    async def run(self) -> None:
        backoff = 30
        while True:
            try:
                await self._session()
                return
            except asyncio.CancelledError:
                self.status("stopped")
                raise
            except NotLoggedIn as e:
                self.log("error", f"Аккаунт не залогинен: {e}. Обновите куки и запустите снова.")
                self.status("cookies_invalid", str(e))
                return
            except Exception as e:
                self.log("error", f"Ошибка: {type(e).__name__}: {e}. Перезапуск через {backoff} с")
                self.status("error", f"{type(e).__name__}: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 900)

    async def _session(self) -> None:
        from playwright.async_api import async_playwright

        s = db.get_settings()
        account = db.get_account(self.account_id)
        if not account:
            return
        self.status("starting")
        vision = VisionClient.from_settings(s)
        account = await ensure_vision_profile(vision, account, self.log)
        profile_id = account["vision_profile_id"]

        port = await vision.start_profile(profile_id)
        self.log("info", f"Профиль Vision запущен (CDP порт {port})")
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                if not account["cookies_applied"]:
                    await context.add_cookies(json.loads(account["cookies"]))
                    db.update_account(self.account_id, cookies_applied=1)
                    self.log("info", "Куки добавлены в профиль через браузер")
                page = await context.new_page()
                try:
                    ka = KleinanzeigenSession(page, s, self.log)
                    self.status("running")
                    while True:
                        ka.s = s = db.get_settings()   # изменения настроек применяются на лету
                        await self.poll(ka, s)
                        await asyncio.sleep(_int(s, "poll_interval", 60) + random.uniform(0, _int(s, "poll_jitter", 0)))
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
        finally:
            if s.get("vision_stop_on_exit", "1") == "1":
                try:
                    await vision.stop_profile(profile_id)
                except Exception:
                    pass

    async def sync_ads(self, ka: KleinanzeigenSession) -> None:
        try:
            ads = await ka.load_my_ads()
        except NotLoggedIn:
            raise
        except Exception as e:
            self.log("warning", f"Не удалось загрузить список своих объявлений: {e}")
            return
        for ad in ads:
            db.upsert_ad(self.account_id, ad["ad_id"], ad["title"], ad["price"], ad["url"])
        self.log("info", f"Синхронизировано объявлений: {len(ads)}")

    async def poll(self, ka: KleinanzeigenSession, s: dict) -> None:
        conversations = await ka.load_inbox()
        if ka.user_id:
            db.update_account(self.account_id, ka_user_id=ka.user_id)

        if time.time() - self.ads_synced_at > _int(s, "ads_sync_minutes", 30) * 60:
            self.ads_synced_at = time.time()
            await self.sync_ads(ka)

        reply_mode = s.get("reply_mode", "once")
        only_as_seller = s.get("only_as_seller", "1") == "1"
        for conv in conversations:
            if only_as_seller and conv.role and not conv.is_ours_as_seller:
                continue                       # это мы писали кому-то — не наш товар
            if conv.last_inbound is False and conv.id not in self.retry_ids:
                continue                       # последнее сообщение наше
            if not conv.unread and conv.last_inbound is not True and conv.id not in self.retry_ids:
                continue
            state = db.get_conversation(self.account_id, conv.id)
            if reply_mode == "once" and state and state["reply_count"] > 0:
                continue
            await self.handle(ka, conv, s, reply_mode, only_as_seller)

    async def handle(self, ka: KleinanzeigenSession, short: Conversation, s: dict,
                     reply_mode: str, only_as_seller: bool) -> None:
        conv = await ka.load_conversation(short.id)
        # дополняем детали полями из списка, если в деталях их нет
        for attr in ("ad_id", "ad_title", "ad_price", "role", "buyer_name", "seller_name"):
            if not getattr(conv, attr):
                setattr(conv, attr, getattr(short, attr))
        if not conv.messages and short.last_text:
            # API деталей не вернул сообщений — опираемся на список
            conv.messages = [Message(id="", text=short.last_text, inbound=short.last_inbound)]

        ad = db.get_ad(self.account_id, conv.ad_id) if conv.ad_id else None
        ad_title = conv.ad_title or (ad or {}).get("title", "")
        last = conv.last_message()
        state = db.get_conversation(self.account_id, conv.id)
        record = {
            "ad_id": conv.ad_id, "ad_title": ad_title, "buyer_name": conv.partner_name,
        }
        if last and last.inbound:
            record.update(last_inbound_text=last.text[:2000], last_inbound_id=last.id, last_inbound_at=last.date)
        db.upsert_conversation(self.account_id, conv.id, **record)

        msg = needs_reply(conv, state, reply_mode, only_as_seller)
        if msg is None:
            self.retry_ids.discard(conv.id)
            return

        template = choose_template(db.list_templates(), self.account_id, conv.ad_id, ad_title)
        if not template:
            self.log("warning", f"Нет подходящего шаблона для «{ad_title}» (диалог {conv.id}) — пропускаю")
            return

        account = db.get_account(self.account_id) or {}
        text = safe_render(template["text"], {
            "buyer_name": conv.partner_name,
            "ad_title": ad_title,
            "ad_price": conv.ad_price or (ad or {}).get("price", ""),
            "ad_id": conv.ad_id,
            "ad_url": (ad or {}).get("url") or (s["ka_ad_url"].format(ad_id=conv.ad_id) if conv.ad_id else ""),
            "account_name": account.get("name", ""),
            "message": msg.text,
        })
        if not text:
            self.log("warning", f"Шаблон «{template['name']}» дал пустой текст — пропускаю")
            return

        self.log("info", f"Новое сообщение от «{conv.partner_name}» по товару «{ad_title}» (ID {conv.ad_id}): "
                         f"{msg.text[:200]!r}")
        lo, hi = sorted((_int(s, "reply_delay_min", 0), _int(s, "reply_delay_max", 0)))
        await asyncio.sleep(random.uniform(lo, hi))
        try:
            await ka.send_reply(conv.id, text)
        except NotLoggedIn:
            raise
        except Exception as e:
            self.retry_ids.add(conv.id)
            self.log("error", f"Не удалось отправить ответ в диалог {conv.id}: {e}")
            return
        self.retry_ids.discard(conv.id)
        db.upsert_conversation(
            self.account_id, conv.id,
            replied_msg_id=msg.id, reply_count=(state or {}).get("reply_count", 0) + 1,
            last_reply_text=text, last_reply_at=time.time(), template_id=template["id"],
        )
        self.log("info", f"Ответил «{conv.partner_name}» по «{ad_title}» шаблоном «{template['name']}»")


class BotManager:
    def __init__(self):
        self.tasks: dict[int, asyncio.Task] = {}

    def is_running(self, account_id: int) -> bool:
        task = self.tasks.get(account_id)
        return bool(task and not task.done())

    def start(self, account_id: int) -> None:
        if self.is_running(account_id):
            return
        worker = AccountWorker(account_id)
        self.tasks[account_id] = asyncio.create_task(worker.run(), name=f"account-{account_id}")

    async def stop(self, account_id: int) -> None:
        task = self.tasks.pop(account_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if db.get_account(account_id):
            db.update_account(account_id, status="stopped")

    def start_all(self) -> None:
        for acc in db.list_accounts():
            if acc["enabled"]:
                self.start(acc["id"])

    async def stop_all(self) -> None:
        for account_id in list(self.tasks):
            await self.stop(account_id)
