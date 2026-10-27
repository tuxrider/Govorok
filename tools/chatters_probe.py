r"""Диагностика вкладки «Зрители»: почему официальный список не грузится.

    .venv\\Scripts\\python.exe tools\chatters_probe.py

По порядку проверяет то, что требует Twitch, и печатает только коды/выводы,
без токенов: scope текущего токена, user id бота, id канала, и наконец сам
запрос /helix/chat/chatters. Это тот же путь, что проходит ChattersPoller,
но с внятными подписями вместо молчаливого отказа.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from govorok.config import AppConfig  # noqa: E402
from govorok.viewers import HELIX_CHATTERS, HELIX_USERS, ChattersPoller, ViewerRegistry  # noqa: E402

VERIFY_URL = "https://id.twitch.tv/oauth2/validate"


def main() -> int:
    cfg = AppConfig.load()
    twitch = cfg.twitch
    token = twitch.normalized_token().removeprefix("oauth:")

    print("1. настройки")
    print("   Client-Id задан       :", bool(twitch.client_id.strip()))
    print("   OAuth-токен задан     :", bool(twitch.oauth_token.strip()))
    print("   nickname бота задан   :", bool(twitch.nickname.strip()),
          "(без него Twitch не отдаёт список зрителей)")
    print("   fetch_chatters включён:", cfg.viewers.fetch_chatters)

    if not twitch.client_id.strip() or not token:
        print("\nнет Client-Id или токена — опрос не запускается вообще, "
              "вкладка живёт на активности чата.")
        return 0

    headers = {"Client-Id": twitch.client_id.strip(), "Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=12.0, headers={"User-Agent": "Govorok/0.1 (+local)"}) as client:
        print("\n2. что умеет токен (GET /oauth2/validate)")
        try:
            answer = client.get(VERIFY_URL, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            print("   сеть недоступна:", type(exc).__name__)
            return 1
        if answer.status_code != 200:
            print("   токен не принят:", answer.status_code)
            return 1
        body = answer.json()
        scopes = body.get("scopes") or []
        print("   логин токена      :", body.get("login"))
        print("   clientId совпадает:", body.get("client_id") == twitch.client_id.strip())
        needed = "moderator:read:chatters"
        print(f"   scope {needed}    :", "ЕСТЬ" if needed in scopes else "НЕТ")
        print("   все scope         :", ", ".join(scopes) or "(пусто)")

        print("\n3. user id бота (GET /helix/users)")
        users = client.get(HELIX_USERS, headers=headers)
        print("   ответ             :", users.status_code)
        data = (users.json() or {}).get("data") or []
        if not data:
            print("   Twitch не вернул пользователя — список зрителей недоступен")
            return 1
        bot_id = str(data[0].get("id"))
        print("   id                :", bot_id, "| логин:", data[0].get("login"))

        print("\n4. id канала (GET /helix/users?login=…)")
        login = (cfg.twitch.normalized_channels() or [""])[0]
        target = client.get(HELIX_USERS, params={"login": login}, headers=headers)
        print("   канал             :", login, "| ответ:", target.status_code)
        rows = (target.json() or {}).get("data") or []
        if not rows:
            print("   канал не найден — проверь написание лога в настройках")
            return 1
        room_id = str(rows[0].get("id"))
        print("   room id           :", room_id)

        print("\n5. список зрителей (GET /helix/chat/chatters)")
        poller = ChattersPoller(ViewerRegistry(), lambda: cfg)
        poller.bot_user_id = bot_id
        ok = __import__("asyncio").run(poller.fetch(room_id, login))
        print("   итог              :", "список получается ✔" if ok
              else f"не получилось: {poller.last_error or 'см. выше'}")
        if not ok:
            print("\n   чаще всего это 403: бот должен быть **модератором** канала "
                  "(или его авторизовал владелец),\n"
                  "   а в токене — scope moderator:read:chatters. Перевыпусти токен "
                  "с этим scope и выдай\n"
                  "   боту модераторские права, затем перезапусти Govorok.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
