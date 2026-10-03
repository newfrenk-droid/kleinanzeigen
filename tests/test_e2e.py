"""Сквозной тест воркера на реальном Chromium против локальной имитации Kleinanzeigen и Vision.

Пропускается, если Chromium для Playwright не установлен.
"""
import asyncio
import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import db
from app.worker import AccountWorker

pytest.importorskip("playwright")

INBOX_HTML = """<!doctype html><html><body><h1>Nachrichten</h1><script>
fetch('/gateway/messagebox/api/users/U1/conversations?page=0&size=10',
      {headers: {authorization: 'Bearer TEST'}}).then(r => r.json()).then(d => document.title = 'ok');
</script></body></html>"""

CONV_HTML = """<!doctype html><html><body>
<form onsubmit="event.preventDefault(); const t=document.querySelector('textarea');
  fetch('/sent', {method:'POST', body: t.value}).then(() => t.value='');">
<textarea></textarea><button type="submit">Senden</button></form></body></html>"""

CONVERSATIONS = {"conversations": [
    {"id": "c1", "role": "Seller", "unread": True, "adId": "42", "adTitle": "iPhone 13",
     "buyerName": "Max", "boundness": "INBOUND", "textShortTrimmed": "Noch da?"},
    {"id": "c2", "role": "Seller", "unread": False, "adId": "43", "adTitle": "Sofa",
     "buyerName": "Eva", "boundness": "OUTBOUND"},
    {"id": "c3", "role": "Buyer", "unread": True, "adId": "99", "adTitle": "Fahrrad",
     "sellerName": "Tom", "boundness": "INBOUND"},
]}

DETAIL = {"id": "c1", "role": "Seller", "adId": "42", "adTitle": "iPhone 13", "buyerName": "Max",
          "adPriceInEuroCent": 45000,
          "messages": [{"messageId": "m1", "text": "Hallo, noch da?", "boundness": "INBOUND", "type": "MESSAGE"}]}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Site:
    def __init__(self):
        self.sent: list[str] = []
        self.auth_seen: list[str] = []
        self.cdp_port = free_port()
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, body, ctype="application/json"):
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/m-nachrichten.html":
                    self.reply(CONV_HTML if "conversationId" in self.path else INBOX_HTML, "text/html")
                elif path.endswith("/messagebox/api/users/U1/conversations"):
                    self.reply(CONVERSATIONS)
                elif path.endswith("/messagebox/api/users/U1/conversations/c1"):
                    site.auth_seen.append(self.headers.get("authorization", ""))
                    self.reply(DETAIL)
                elif path == "/my-ads.json":
                    page = int(self.path.split("page=")[1])
                    self.reply({"ads": [{"id": 42, "title": "iPhone 13", "price": {"amount": 450}}] if page == 1 else []})
                elif path.startswith("/start/"):
                    self.reply({"port": site.cdp_port})
                elif path.startswith("/stop/"):
                    self.reply({})
                else:
                    self.send_error(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                site.sent.append(body)
                self.reply({})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture()
def chromium(tmp_path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        exe = p.chromium.executable_path
    if not os.path.exists(exe):
        exe = os.environ.get("CHROMIUM_PATH", "/opt/pw-browsers/chromium")
    site = Site()
    try:
        proc = subprocess.Popen([exe, "--headless=new", f"--remote-debugging-port={site.cdp_port}",
                                 f"--user-data-dir={tmp_path / 'profile'}", "--no-sandbox", "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pytest.skip("Chromium не установлен")
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", site.cdp_port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.2)
    yield site
    proc.terminate()
    site.server.shutdown()


def test_worker_replies_only_to_incoming_on_our_ads(chromium, tmp_path):
    site = chromium
    db.connect(tmp_path / "e2e.db")
    db.save_settings({
        "vision_token": "t", "vision_folder_id": "F", "vision_local_url": site.url,
        "ka_inbox_url": f"{site.url}/m-nachrichten.html",
        "ka_conversation_url": f"{site.url}/m-nachrichten.html?conversationId={{conversation_id}}",
        "ka_gateway_url": f"{site.url}/gateway/messagebox/api",
        "ka_my_ads_url": f"{site.url}/my-ads.json?page={{page}}",
        "reply_delay_min": "0", "reply_delay_max": "0", "typing_delay_ms": "5",
        "poll_interval": "3600", "poll_jitter": "0",
    })
    db.save_template(None, name="iPhone", text="Hallo {buyer_name}!\n{ad_title} für {ad_price} ist da.",
                     account_id=None, ad_id="", keywords="iphone", priority=0, enabled=1)
    account_id = db.create_account("A", [{"name": "x", "value": "1", "domain": "127.0.0.1", "path": "/",
                                          "expires": -1, "httpOnly": False, "secure": False, "sameSite": "Lax"}], "")
    # профиль Vision уже «создан» — облачный API в тесте не нужен
    db.update_account(account_id, vision_profile_id="P")

    async def scenario():
        worker = AccountWorker(account_id)
        task = asyncio.create_task(worker.run())
        for _ in range(100):
            await asyncio.sleep(0.2)
            if site.sent:
                break
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    logs = [l["message"] for l in db.list_log(account_id=account_id)]
    assert site.sent == ["Hallo Max!\niPhone 13 für 450 € ist da."], logs
    assert site.auth_seen == ["Bearer TEST"]
    conv = db.get_conversation(account_id, "c1")
    assert conv["reply_count"] == 1 and conv["ad_title"] == "iPhone 13" and conv["buyer_name"] == "Max"
    # на диалог, где последнее сообщение наше, и на диалог, где мы покупатель — не отвечали
    assert db.get_conversation(account_id, "c2") is None and db.get_conversation(account_id, "c3") is None
    assert db.get_ad(account_id, "42")["price"] == "450 €"
    assert db.get_account(account_id)["ka_user_id"] == "U1"
