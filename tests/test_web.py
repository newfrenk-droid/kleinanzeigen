import pytest
from fastapi.testclient import TestClient

from app import db


@pytest.fixture()
def client(tmp_path):
    db.connect(tmp_path / "test.db")
    from app.web import app
    with TestClient(app) as c:
        yield c


def test_account_and_template_flow(client):
    assert client.get("/").status_code == 200

    r = client.post("/accounts/save", data={"name": "Shop 1", "cookies": "a=1; b=2",
                                            "proxy": "1.2.3.4:8080:u:p", "enabled": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    [acc] = db.list_accounts()
    assert acc["proxy"] == "http://u:p@1.2.3.4:8080"

    # некорректный прокси — ошибка в форме, аккаунт не создаётся
    r = client.post("/accounts/save", data={"name": "Bad", "cookies": "a=1", "proxy": "ftp://x:1"})
    assert "Неподдерживаемый" in r.text and len(db.list_accounts()) == 1

    # редактирование без куки сохраняет старые
    client.post("/accounts/save", data={"account_id": acc["id"], "name": "Shop X", "cookies": "", "proxy": ""})
    acc2 = db.get_account(acc["id"])
    assert acc2["name"] == "Shop X" and acc2["cookies"] == acc["cookies"] and acc2["proxy"] == ""

    client.post("/templates/save", data={"name": "iPhone", "text": "Hi {buyer_name}", "keywords": "iphone",
                                         "account_id": str(acc["id"]), "priority": "1", "enabled": "1"})
    assert any(t["name"] == "iPhone" and t["account_id"] == acc["id"] for t in db.list_templates())
    for path in ("/templates", "/templates/0", "/dialogs", "/logs", "/settings", f"/accounts/{acc['id']}"):
        assert client.get(path).status_code == 200, path

    client.post("/settings", data={"poll_interval": "30", "vision_token": "t"})
    s = db.get_settings()
    assert s["poll_interval"] == "30" and s["vision_token"] == "t" and s["only_as_seller"] == "0"
