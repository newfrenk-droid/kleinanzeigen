"""Работа с сообщениями Kleinanzeigen внутри уже открытого (залогиненного) браузера.

Как это устроено:
  * Бот открывает страницу «Nachrichten». Веб-интерфейс сам запрашивает список
    диалогов у messagebox API — бот перехватывает этот ответ, а заодно токен
    авторизации и userId из запроса. Так не нужно ничего «угадывать» про логин.
  * Детали диалога (все сообщения, ID и название объявления) бот запрашивает
    тем же API через fetch() ВНУТРИ страницы — трафик идёт через браузер Vision
    и его прокси (а не напрямую с вашего IP).
  * Ответ отправляется через интерфейс: открывается диалог, текст набирается
    в поле ввода и нажимается «Senden» (send_mode=ui), либо POST-запросом (send_mode=api).

Поля JSON у Kleinanzeigen не документированы, поэтому нормализация ниже
проверяет несколько вариантов названий.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass, field


class NotLoggedIn(RuntimeError):
    pass


class KleinanzeigenError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Нормализация JSON
# ---------------------------------------------------------------------------

def _get(obj: dict, *paths, default=None):
    """Первое непустое значение по одному из путей вида "ad.title"."""
    for path in paths:
        cur = obj
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if cur not in (None, ""):
            return cur
    return default


@dataclass
class Message:
    id: str
    text: str
    inbound: bool | None   # True — от собеседника, False — наше, None — неизвестно
    date: str = ""
    type: str = ""


@dataclass
class Conversation:
    id: str
    ad_id: str = ""
    ad_title: str = ""
    ad_price: str = ""
    role: str = ""          # "Seller" — мы продавец (пишут по нашему объявлению)
    buyer_name: str = ""
    seller_name: str = ""
    unread: bool = False
    last_text: str = ""
    last_inbound: bool | None = None
    messages: list[Message] = field(default_factory=list)

    @property
    def is_ours_as_seller(self) -> bool:
        return self.role.lower() in ("seller", "verkäufer", "verkaeufer")

    @property
    def partner_name(self) -> str:
        if self.role.lower() == "buyer":
            return self.seller_name
        return self.buyer_name

    def last_message(self) -> Message | None:
        msgs = [m for m in self.messages if m.text.strip()]
        return msgs[-1] if msgs else None


def _inbound(value) -> bool | None:
    if value is None:
        return None
    v = str(value).upper()
    if v in ("INBOUND", "IN", "RECEIVED"):
        return True
    if v in ("OUTBOUND", "OUT", "SENT"):
        return False
    return None


def _price(raw) -> str:
    if isinstance(raw, dict):
        amount = _get(raw, "amount", "amountInEuro", "value")
        if amount is None:
            cents = _get(raw, "amountInEuroCent", "cents")
            amount = cents / 100 if isinstance(cents, (int, float)) else None
        price_type = str(_get(raw, "priceType", "type", default="")).upper()
        if amount is None:
            return "VB" if "NEGOTIABLE" in price_type else ""
        amount = int(amount) if float(amount).is_integer() else amount
        return f"{amount} €" + (" VB" if "NEGOTIABLE" in price_type else "")
    if isinstance(raw, (int, float)):
        return f"{raw} €"
    return str(raw or "")


def parse_message(raw: dict) -> Message:
    inbound = _inbound(_get(raw, "boundness", "direction"))
    if inbound is None and "senderIsMe" in raw:
        inbound = not raw["senderIsMe"]
    return Message(
        id=str(_get(raw, "messageId", "id", default="")),
        text=str(_get(raw, "text", "textShort", "message", default="")),
        inbound=inbound,
        date=str(_get(raw, "receivedDate", "sentDate", "date", default="")),
        type=str(_get(raw, "type", default="")),
    )


def parse_conversation(raw: dict) -> Conversation:
    price = _get(raw, "adPriceInEuroCent")
    price = _price({"amountInEuroCent": price}) if isinstance(price, (int, float)) else _price(_get(raw, "adPrice", "ad.price"))
    messages = [parse_message(m) for m in (_get(raw, "messages", default=[]) or []) if isinstance(m, dict)]
    unread_count = _get(raw, "unreadMessagesCount", "numUnreadMessages", default=0)
    conv = Conversation(
        id=str(_get(raw, "id", "conversationId", default="")),
        ad_id=str(_get(raw, "adId", "ad.id", default="")),
        ad_title=str(_get(raw, "adTitle", "ad.title", "subject", default="")),
        ad_price=price,
        role=str(_get(raw, "role", "userRole", default="")),
        buyer_name=str(_get(raw, "buyerName", "buyer.name", "userNameBuyer", default="")),
        seller_name=str(_get(raw, "sellerName", "seller.name", "userNameSeller", default="")),
        unread=bool(_get(raw, "unread", default=False)) or (isinstance(unread_count, int) and unread_count > 0),
        last_text=str(_get(raw, "textShortTrimmed", "lastMessage.text", "textShort", default="")),
        last_inbound=_inbound(_get(raw, "boundness", "lastMessage.boundness")),
        messages=messages,
    )
    if messages and conv.last_inbound is None:
        last = conv.last_message()
        conv.last_inbound = last.inbound if last else None
    return conv


def parse_conversation_list(payload) -> list[Conversation]:
    if isinstance(payload, list):
        items = payload
    else:
        items = _get(payload or {}, "conversations", "_embedded.conversations", "data", "content", default=[])
    return [parse_conversation(c) for c in items if isinstance(c, dict) and _get(c, "id", "conversationId")]


def parse_my_ads(payload) -> list[dict]:
    items = _get(payload or {}, "ads", "_embedded.ads", "data", default=[]) if not isinstance(payload, list) else payload
    ads = []
    for a in items or []:
        if not isinstance(a, dict) or not _get(a, "id", "adId"):
            continue
        ads.append({
            "ad_id": str(_get(a, "id", "adId")),
            "title": str(_get(a, "title", "adTitle", default="")),
            "price": _price(_get(a, "price", "priceAmount", default="")),
            "url": str(_get(a, "link", "url", "seoUrl", default="")),
        })
    return ads


def needs_reply(conv: Conversation, state: dict | None, reply_mode: str, only_as_seller: bool) -> Message | None:
    """Возвращает входящее сообщение, на которое нужно ответить, иначе None.

    Отвечаем только если ПОСЛЕДНЕЕ сообщение в диалоге — от другого пользователя
    (т.е. мы ещё не отвечали вручную или ботом).
    """
    if only_as_seller and conv.role and not conv.is_ours_as_seller:
        return None
    last = conv.last_message()
    if last is None:
        return None
    if last.inbound is not True:
        return None
    if last.type and last.type.upper() not in ("MESSAGE", "TEXT", "OFFER", "MAKE_OFFER", "PAYMENT_OFFER"):
        # системные уведомления и т.п.
        return None
    if state:
        if reply_mode == "once" and state.get("reply_count", 0) > 0:
            return None
        if last.id and state.get("replied_msg_id") == last.id:
            return None
    return last


# ---------------------------------------------------------------------------
# Работа через браузер
# ---------------------------------------------------------------------------

_FETCH_JS = """
async ({method, url, body, auth}) => {
  const headers = {'accept': 'application/json'};
  if (auth) headers['authorization'] = auth;
  if (body !== null) headers['content-type'] = 'application/json';
  const r = await fetch(url, {method, headers, credentials: 'include',
                              body: body === null ? undefined : JSON.stringify(body)});
  return {status: r.status, text: await r.text()};
}
"""

_USER_RE = re.compile(r"/users/([^/?#]+)/")


class KleinanzeigenSession:
    def __init__(self, page, settings: dict, log=None):
        self.page = page
        self.s = settings
        self.log = log or (lambda level, msg: None)
        self.auth: str = ""
        self.user_id: str = ""

    # ---------- низкоуровневое ----------

    async def fetch(self, method: str, url: str, body=None, auth: bool = True):
        res = await self.page.evaluate(_FETCH_JS, {
            "method": method, "url": url, "body": body, "auth": self.auth if auth else "",
        })
        status, text = res["status"], res["text"]
        if status in (401, 403):
            raise NotLoggedIn(f"{method} {url} -> {status}")
        if status >= 400:
            raise KleinanzeigenError(f"{method} {url} -> {status}: {text[:200]}")
        try:
            return json.loads(text) if text else {}
        except ValueError:
            return {}

    def _check_login(self) -> None:
        url = self.page.url
        if "m-einloggen" in url or "login" in url.split("?")[0].lower():
            raise NotLoggedIn("Kleinanzeigen перекинул на страницу входа — куки недействительны")

    def _on_request(self, request) -> None:
        if "/messagebox/" not in request.url:
            return
        auth = request.headers.get("authorization")
        if auth:
            self.auth = auth
        m = _USER_RE.search(request.url)
        if m:
            self.user_id = m.group(1)

    # ---------- диалоги ----------

    async def load_inbox(self, timeout: float = 25) -> list[Conversation]:
        """Открывает «Nachrichten» и возвращает список диалогов."""
        captured: list = []

        def on_response(response):
            url = response.url.split("?")[0]
            if "/messagebox/" in url and url.rstrip("/").endswith("/conversations") \
                    and response.request.method == "GET":
                captured.append(response)

        self.page.on("request", self._on_request)
        self.page.on("response", on_response)
        try:
            await self.page.goto(self.s["ka_inbox_url"], wait_until="domcontentloaded")
            self._check_login()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while not captured and loop.time() < deadline:
                await asyncio.sleep(0.5)
            self._check_login()
        finally:
            self.page.remove_listener("response", on_response)
            self.page.remove_listener("request", self._on_request)

        if captured:
            try:
                return parse_conversation_list(await captured[-1].json())
            except Exception as e:  # тело ответа может быть уже недоступно
                self.log("warning", f"Не удалось прочитать перехваченный список диалогов: {e}")
        if self.user_id:
            return parse_conversation_list(await self.fetch(
                "GET", f"{self.s['ka_gateway_url']}/users/{self.user_id}/conversations?page=0&size=50"))
        raise KleinanzeigenError(
            "Не удалось получить список диалогов: страница сообщений не сделала запрос к messagebox API. "
            "Проверьте вручную, открывается ли «Nachrichten» в этом профиле Vision.")

    async def load_conversation(self, conversation_id: str) -> Conversation:
        if not self.user_id:
            raise KleinanzeigenError("Неизвестен userId — сначала нужно открыть список диалогов")
        data = await self.fetch(
            "GET", f"{self.s['ka_gateway_url']}/users/{self.user_id}/conversations/{conversation_id}?contentWarnings=true")
        conv = parse_conversation(data)
        conv.id = conv.id or conversation_id
        return conv

    async def load_my_ads(self, max_pages: int = 20) -> list[dict]:
        ads: list[dict] = []
        for page in range(1, max_pages + 1):
            data = await self.fetch("GET", self.s["ka_my_ads_url"].format(page=page), auth=False)
            chunk = parse_my_ads(data)
            new = [a for a in chunk if a["ad_id"] not in {x["ad_id"] for x in ads}]
            if not new:
                break
            ads.extend(new)
            paging = _get(data, "paging", default={}) or {}
            if isinstance(paging, dict) and paging.get("last") and page >= int(paging["last"]):
                break
        return ads

    # ---------- отправка ----------

    async def send_reply(self, conversation_id: str, text: str) -> None:
        if self.s.get("send_mode") == "api":
            await self._send_api(conversation_id, text)
        else:
            await self._send_ui(conversation_id, text)

    async def _send_api(self, conversation_id: str, text: str) -> None:
        await self.fetch(
            "POST", f"{self.s['ka_gateway_url']}/users/{self.user_id}/conversations/{conversation_id}",
            {"message": text})

    async def _send_ui(self, conversation_id: str, text: str) -> None:
        page = self.page
        await page.goto(self.s["ka_conversation_url"].format(conversation_id=conversation_id),
                        wait_until="domcontentloaded")
        self._check_login()
        box = page.locator(self.s["ka_reply_textarea_selector"]).last
        await box.wait_for(state="visible", timeout=30000)
        await box.click()
        await box.fill("")
        delay = int(self.s.get("typing_delay_ms") or 0)
        # Абзацы набираем через Shift+Enter, чтобы Enter не отправил сообщение раньше времени
        for i, line in enumerate(text.split("\n")):
            if i:
                await page.keyboard.press("Shift+Enter")
            if line and delay:
                await box.press_sequentially(line, delay=delay + random.randint(0, delay // 2))
            elif line:
                await page.keyboard.insert_text(line)
        await asyncio.sleep(random.uniform(0.5, 1.5))
        button = page.locator(self.s["ka_send_button_selector"]).last
        if await button.count():
            await button.click()
        else:
            await box.press("Enter")
        # ждём, что поле очистилось — признак отправки
        for _ in range(20):
            await asyncio.sleep(0.5)
            try:
                if not (await box.input_value()).strip():
                    return
            except Exception:
                return
        raise KleinanzeigenError("Сообщение набрано, но поле не очистилось — возможно, не отправлено "
                                 "(проверьте селектор кнопки «Senden» в настройках)")
