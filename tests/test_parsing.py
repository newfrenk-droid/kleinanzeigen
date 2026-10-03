import json
import random

import pytest

from app.cookies import CookieParseError, parse_cookies
from app.kleinanzeigen import needs_reply, parse_conversation, parse_conversation_list, parse_my_ads
from app.proxy import ProxyParseError, parse_proxy
from app.replies import choose_template, safe_render


# ---------- cookies ----------

def test_cookies_json_export():
    raw = json.dumps([{"name": "access_token", "value": "abc", "domain": ".kleinanzeigen.de",
                       "expirationDate": 1893456000.5, "sameSite": "no_restriction", "secure": False,
                       "httpOnly": True}])
    [c] = parse_cookies(raw)
    assert c["expires"] == 1893456000
    assert c["sameSite"] == "None" and c["secure"] is True
    assert c["httpOnly"] is True and c["path"] == "/"


def test_cookies_netscape():
    text = "# Netscape HTTP Cookie File\n#HttpOnly_.kleinanzeigen.de\tTRUE\t/\tTRUE\t1893456000\tsid\txyz\n"
    [c] = parse_cookies(text)
    assert (c["name"], c["value"], c["httpOnly"], c["secure"]) == ("sid", "xyz", True, True)


def test_cookies_header_string():
    cookies = parse_cookies("Cookie: a=1; b=2=3")
    assert [(c["name"], c["value"], c["domain"]) for c in cookies] == [
        ("a", "1", ".kleinanzeigen.de"), ("b", "2=3", ".kleinanzeigen.de")]


def test_cookies_errors():
    with pytest.raises(CookieParseError):
        parse_cookies("")
    with pytest.raises(CookieParseError):
        parse_cookies("[not json")


# ---------- proxy ----------

@pytest.mark.parametrize("text,expected", [
    ("http://u:p@1.2.3.4:8080", ("http", "1.2.3.4", 8080, "u", "p")),
    ("socks5://1.2.3.4:1080", ("socks5", "1.2.3.4", 1080, "", "")),
    ("1.2.3.4:8080:user:pa%ss", ("http", "1.2.3.4", 8080, "user", "pa%ss")),
    ("user:pass@host.example:3128", ("http", "host.example", 3128, "user", "pass")),
    ("1.2.3.4:8080", ("http", "1.2.3.4", 8080, "", "")),
])
def test_proxy_formats(text, expected):
    p = parse_proxy(text)
    assert (p.type, p.host, p.port, p.username, p.password) == expected


def test_proxy_empty_and_bad():
    assert parse_proxy("  ") is None
    with pytest.raises(ProxyParseError):
        parse_proxy("ftp://1.2.3.4:21")
    with pytest.raises(ProxyParseError):
        parse_proxy("1.2.3.4:notaport:u:p")


# ---------- conversations ----------

LIST_PAYLOAD = {
    "conversations": [
        {"id": "c1", "role": "Seller", "unread": True, "adId": "2900000001", "adTitle": "iPhone 13 128GB",
         "buyerName": "Max", "sellerName": "Ich", "textShortTrimmed": "Ist das noch da?", "boundness": "INBOUND"},
        {"id": "c2", "role": "Buyer", "unread": True, "adId": "777", "adTitle": "Fahrrad",
         "buyerName": "Ich", "sellerName": "Anna", "boundness": "INBOUND"},
    ]
}

DETAIL_PAYLOAD = {
    "id": "c1", "role": "Seller", "adId": "2900000001", "adTitle": "iPhone 13 128GB",
    "adPriceInEuroCent": 45000, "buyerName": "Max",
    "messages": [
        {"messageId": "m1", "text": "Hallo, ist das noch da?", "boundness": "INBOUND", "type": "MESSAGE"},
    ],
}


def test_parse_list_and_detail():
    convs = parse_conversation_list(LIST_PAYLOAD)
    assert [c.id for c in convs] == ["c1", "c2"]
    assert convs[0].is_ours_as_seller and not convs[1].is_ours_as_seller
    assert convs[1].partner_name == "Anna"

    conv = parse_conversation(DETAIL_PAYLOAD)
    assert conv.ad_price == "450 €"
    assert conv.last_message().id == "m1" and conv.last_inbound is True


def test_needs_reply_rules():
    conv = parse_conversation(DETAIL_PAYLOAD)
    assert needs_reply(conv, None, "once", True).id == "m1"
    # уже ответили один раз
    assert needs_reply(conv, {"reply_count": 1, "replied_msg_id": "m1"}, "once", True) is None
    # every: новое входящее после нашего ответа
    assert needs_reply(conv, {"reply_count": 1, "replied_msg_id": "m0"}, "every", True).id == "m1"
    assert needs_reply(conv, {"reply_count": 1, "replied_msg_id": "m1"}, "every", True) is None

    # последнее сообщение наше — не отвечаем
    ours = dict(DETAIL_PAYLOAD, messages=DETAIL_PAYLOAD["messages"] + [
        {"messageId": "m2", "text": "Ja", "boundness": "OUTBOUND", "type": "MESSAGE"}])
    assert needs_reply(parse_conversation(ours), None, "every", True) is None

    # мы покупатель — не наш товар
    buyer = dict(DETAIL_PAYLOAD, role="Buyer")
    assert needs_reply(parse_conversation(buyer), None, "once", True) is None
    assert needs_reply(parse_conversation(buyer), None, "once", False) is not None


def test_parse_my_ads():
    ads = parse_my_ads({"ads": [{"id": 1, "title": "Sofa", "price": {"amount": 120, "priceType": "NEGOTIABLE"},
                                 "link": "https://x/1"}, {"foo": 1}]})
    assert ads == [{"ad_id": "1", "title": "Sofa", "price": "120 € VB", "url": "https://x/1"}]


# ---------- templates ----------

TEMPLATES = [
    {"id": 1, "name": "general", "text": "G", "account_id": None, "ad_id": "", "keywords": "", "priority": 0, "enabled": 1},
    {"id": 2, "name": "iphone", "text": "I", "account_id": None, "ad_id": "", "keywords": "iPhone, airpods", "priority": 0, "enabled": 1},
    {"id": 3, "name": "exact", "text": "E", "account_id": None, "ad_id": "42", "keywords": "", "priority": 0, "enabled": 1},
    {"id": 4, "name": "acc2", "text": "A", "account_id": 2, "ad_id": "", "keywords": "", "priority": 0, "enabled": 1},
    {"id": 5, "name": "off", "text": "O", "account_id": None, "ad_id": "", "keywords": "sofa", "priority": 9, "enabled": 0},
]


@pytest.mark.parametrize("account_id,ad_id,title,expected", [
    (1, "1", "Sofa grau", "general"),          # выключенный шаблон не берётся
    (1, "1", "Apple iPhone 13", "iphone"),     # ключевые слова
    (1, "42", "Apple iPhone 13", "exact"),     # конкретное объявление важнее слов
    (2, "1", "Stuhl", "acc2"),                 # шаблон аккаунта важнее общего
])
def test_choose_template(account_id, ad_id, title, expected):
    assert choose_template(TEMPLATES, account_id, ad_id, title)["name"] == expected


def test_render_variables_and_variants():
    text = "Hallo {buyer_name}, {ad_title} kostet {ad_price}. {unknown} {{x}}"
    out = safe_render(text, {"buyer_name": "Max", "ad_title": "Sofa", "ad_price": "10 €"})
    assert out == "Hallo Max, Sofa kostet 10 €. {unknown} {x}"

    variants = "A {buyer_name}\n---\nB {buyer_name}"
    seen = {safe_render(variants, {"buyer_name": "M"}, random.Random(i)) for i in range(20)}
    assert seen == {"A M", "B M"}

    assert safe_render("broken { brace {buyer_name}", {"buyer_name": "M"}) == "broken { brace M"
