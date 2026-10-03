"""Выбор шаблона автоответа под конкретный товар и подстановка переменных.

Порядок выбора (побеждает первый подходящий, при равенстве — больший priority):
  1. шаблон, привязанный к конкретному объявлению (ad_id)
  2. шаблон с ключевыми словами, найденными в названии объявления
  3. общий шаблон (без ad_id и ключевых слов)
Шаблоны, привязанные к аккаунту, рассматриваются только для этого аккаунта
и при прочих равных важнее шаблонов «для всех».

Если в тексте шаблона есть строки `---`, текст делится на варианты,
и для каждого ответа случайно берётся один из них.
"""
from __future__ import annotations

import random
import re
import string

PLACEHOLDERS = {
    "buyer_name": "имя написавшего",
    "ad_title": "название объявления",
    "ad_price": "цена объявления",
    "ad_id": "ID объявления",
    "ad_url": "ссылка на объявление",
    "account_name": "название аккаунта в панели",
    "message": "текст входящего сообщения",
}

_VARIANT_SPLIT = re.compile(r"^\s*---\s*$", re.MULTILINE)


def _keywords(template: dict) -> list[str]:
    return [k.strip().lower() for k in (template.get("keywords") or "").split(",") if k.strip()]


def match_level(template: dict, account_id: int, ad_id: str, ad_title: str) -> int | None:
    """Насколько специфично шаблон подходит к диалогу; None — не подходит."""
    if not template.get("enabled", 1):
        return None
    if template.get("account_id") not in (None, "", account_id):
        return None
    t_ad = str(template.get("ad_id") or "").strip()
    words = _keywords(template)
    if t_ad:
        if t_ad != str(ad_id):
            return None
        level = 30
    elif words:
        title = (ad_title or "").lower()
        if not any(w in title for w in words):
            return None
        level = 20
    else:
        level = 10
    if template.get("account_id") == account_id:
        level += 5
    return level


def choose_template(templates: list[dict], account_id: int, ad_id: str, ad_title: str) -> dict | None:
    best, best_key = None, None
    for t in templates:
        level = match_level(t, account_id, ad_id, ad_title)
        if level is None:
            continue
        key = (level, int(t.get("priority") or 0), -int(t.get("id") or 0))
        if best_key is None or key > best_key:
            best, best_key = t, key
    return best


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(text: str, variables: dict, rng: random.Random | None = None) -> str:
    variants = [v.strip() for v in _VARIANT_SPLIT.split(text) if v.strip()]
    if not variants:
        return ""
    chosen = (rng or random).choice(variants)
    values = _SafeDict({k: ("" if v is None else str(v)) for k, v in variables.items()})
    # string.Formatter, чтобы не падать на "{" в тексте и неизвестных переменных
    out = []
    for literal, field, spec, conv in string.Formatter().parse(chosen.replace("{{", "\0").replace("}}", "\1")):
        out.append(literal)
        if field is not None:
            out.append(values[field] if field.isidentifier() else "{" + field + "}")
    return "".join(out).replace("\0", "{").replace("\1", "}").strip()


def safe_render(text: str, variables: dict, rng: random.Random | None = None) -> str:
    try:
        return render(text, variables, rng)
    except ValueError:
        # непарные скобки и т.п. — отдаём текст как есть, только подставив известные переменные
        out = text
        for k, v in variables.items():
            out = out.replace("{" + k + "}", "" if v is None else str(v))
        return out.strip()
