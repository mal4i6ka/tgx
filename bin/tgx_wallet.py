#!/usr/bin/env python3
"""Встроенный кошелёк TON (слой 233): всё, что делается без приватного ключа.

Кошелёк в Telegram самохранимый: ключ живёт у владельца, сервер видит только
публичный ключ и адрес. Отсюда граница этого модуля.

* **Чтение — целиком.** Баланс, операции, NFT, адреса людей, лимит переводов без
  комиссии, оформление карточки, старый кошелёк Wallet, сессии TonConnect.
* **Без подписи — тоже.** Закрыть сессию TonConnect, отклонить запрос
  приложения, узнать котировку и открыть страницу покупки криптовалюты: деньги
  при этом не двигаются, а покупку человек завершает сам, на странице провайдера.
* **С подписью — нет.** `wallet.sendTransfer` принимает уже подписанное внешнее
  сообщение блокчейна, а выгрузка сид-фразы, резервная копия и замена кошелька —
  ключевой материал. Описаний этих методов Telegram не публиковал, проверить их
  можно только настоящими деньгами, и ошибка в сериализации здесь — потерянные
  средства. Поэтому они не собираются вслепую, а отказывают словами.

Суммы в TON приходят в нанотонах (10⁻⁹), как в самом блокчейне.
"""
from __future__ import annotations

import json
from typing import Any

NANO = 10 ** 9
NEEDS_KEY = ("перевод, сид-фраза, резервная копия и замена кошелька подписываются "
             "приватным ключом, которого у tgx нет и быть не должно; Telegram не "
             "опубликовал описание этих методов, и собирать подпись вслепую — значит "
             "рисковать деньгами. Сделайте это в приложении Telegram")


class WalletError(RuntimeError):
    """Не вышло."""


HINTS = {
    "WALLET_NOT_FOUND": "кошелька у аккаунта нет — его создают в приложении Telegram",
    "WALLET_NOT_CREATED": "кошелёк ещё не создан — его создают в приложении Telegram",
    "TONCONNECT_SESSION_INVALID": "такой сессии TonConnect нет — список: tgx wallet sessions",
    "ONRAMP_PROVIDER_INVALID": "такого провайдера нет — список: tgx wallet onramp providers",
    "ONRAMP_UNAVAILABLE": "покупка у этого провайдера в вашей стране недоступна",
    "ADDRESS_INVALID": "адрес TON не распознан",
}


def _explain(exc: Exception) -> Exception:
    import tgx_net
    return tgx_net.explain(exc, HINTS, WalletError)


def ton(nanos: Any) -> str:
    """Нанотоны → «1.5 TON». Без округления до копеек: в блокчейне каждая единица на счету."""
    if nanos is None:
        return ""
    value = int(getattr(nanos, "amount", nanos) or 0)
    sign = "-" if value < 0 else ""
    whole, frac = divmod(abs(value), NANO)
    tail = f"{frac:09d}".rstrip("0")
    return f"{sign}{whole}{'.' + tail if tail else ''} TON"


def _layer233(lookup: Any) -> Any:
    try:
        return lookup()
    except AttributeError:
        raise WalletError("кошелёк появился в слое 233 — нужен Telethon 1.46: "
                          ".venv/bin/pip install -U -r requirements.txt") from None


def _date(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else "")


def _peer(peer: Any, users: dict[int, Any]) -> str:
    """С кем была операция — человеком, каналом, адресом или сервисом."""
    kind = type(peer).__name__.replace("WalletTransactionPeer", "")
    if kind == "User":
        user = users.get(getattr(peer, "user_id", 0))
        who = getattr(user, "username", None) or getattr(user, "first_name", None)
        return f"@{who}" if getattr(user, "username", None) else (who or getattr(peer, "address", ""))
    if kind == "Channel":
        return getattr(peer, "domain", None) or getattr(peer, "address", "") or "канал"
    if kind == "Onramp":
        return f"покупка: {getattr(peer, 'provider_name', '')}".strip()
    if kind == "TelegramGift":
        return "подарок Telegram"
    if kind == "Address":
        return getattr(peer, "domain", None) or getattr(peer, "address", "")
    return "неизвестно"


def transaction(tx: Any, users: dict[int, Any] | None = None) -> dict[str, Any]:
    incoming = bool(getattr(tx, "incoming", False))
    amount = int(getattr(tx, "amount", 0) or 0)
    row = {
        "id": getattr(tx, "id", ""),
        "когда": _date(getattr(tx, "date", None)),
        "сумма": ("+" if incoming else "−") + ton(abs(amount)),
        "комиссия": ton(getattr(tx, "fee", 0)) if getattr(tx, "fee", None) else "",
        "с кем": _peer(getattr(tx, "peer", None), users or {}),
        "комментарий": getattr(tx, "comment", None)
                       or ("(зашифрован)" if getattr(tx, "comment_encrypted", None) else ""),
    }
    flags = [label for attr, label in (("failed", "не прошла"), ("gasless", "без комиссии в TON"),
                                       ("key_change", "смена ключа")) if getattr(tx, attr, False)]
    if getattr(tx, "nft", None) is not None:
        flags.append("NFT")
    row["отметки"] = ", ".join(flags)
    return row


def onramp_payload(provider: str, crypto: str, address: str, *, base: str = "",
                   base_amount: str = "", crypto_amount: str = "", method: str = "") -> dict[str, Any]:
    """Поля `payments.createOnrampSession`, проверенные до сети."""
    if not provider or not crypto or not address:
        raise WalletError("нужны провайдер, криптовалюта и адрес получателя")
    if base_amount and crypto_amount:
        raise WalletError("укажите сумму либо в валюте оплаты, либо в криптовалюте — не обе")
    for name, value in (("сумма", base_amount), ("сумма", crypto_amount)):
        if value:
            try:
                if float(value) <= 0:
                    raise ValueError
            except ValueError:
                raise WalletError(f"{name} должна быть положительным числом, а не «{value}»") from None
    fields: dict[str, Any] = {"provider": provider, "crypto_currency": crypto.upper(),
                              "address": address}
    if base:
        fields["base_currency"] = base.upper()
    if base_amount:
        fields["base_amount"] = str(base_amount)
    if crypto_amount:
        fields["crypto_amount"] = str(crypto_amount)
    if method:
        fields["payment_method"] = method
    return fields


class Wallet:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def _call(self, request: Any) -> Any:
        try:
            return await self.client(request)
        except Exception as exc:
            raise _explain(exc) from exc

    # ── чтение ────────────────────────────────────────────────────────────────
    async def state(self) -> dict[str, Any]:
        from telethon.tl import functions

        got = await self._call(_layer233(lambda: functions.wallet.GetStateRequest)())
        if type(got).__name__ == "WalletStateEmpty":
            return {"кошелёк": "создаётся" if getattr(got, "creating", False) else "не создан"}
        return {"адрес": getattr(got, "address", ""), "баланс": ton(getattr(got, "balance", 0)),
                "резервная копия": bool(getattr(got, "backup_enabled", False)),
                "можно выгрузить фразу": bool(getattr(got, "can_export_phrase", False))}

    async def transactions(self, *, limit: int = 20, offset: str = "", inbound: bool = False,
                           outbound: bool = False) -> dict[str, Any]:
        from telethon.tl import functions

        request = _layer233(lambda: functions.wallet.GetTransactionsRequest)
        got = await self._call(request(offset=offset, limit=int(limit),
                                       inbound=inbound or None, outbound=outbound or None))
        return self._transactions(got)

    async def lookup(self, ids: list[str], *, by_hash: bool = False) -> dict[str, Any]:
        from telethon.tl import functions

        if by_hash:
            request = _layer233(lambda: functions.wallet.GetTransactionsByMsgHashRequest)
            got = await self._call(request(msg_hash=list(ids)))
        else:
            request = _layer233(lambda: functions.wallet.GetTransactionsByIDsRequest)
            got = await self._call(request(id=list(ids)))
        return self._transactions(got)

    @staticmethod
    def _transactions(got: Any) -> dict[str, Any]:
        users = {getattr(u, "id", 0): u for u in getattr(got, "users", None) or []}
        return {"баланс": ton(getattr(got, "balance", 0)),
                "операции": [transaction(t, users) for t in getattr(got, "transactions", None) or []],
                "дальше": getattr(got, "next_offset", None) or ""}

    async def nfts(self, *, limit: int = 50, offset: str = "") -> dict[str, Any]:
        from telethon.tl import functions

        got = await self._call(_layer233(lambda: functions.wallet.GetNftsRequest)(
            offset=offset, limit=int(limit)))
        items = [{"название": getattr(i, "name", "") or "", "адрес": getattr(i, "address", ""),
                  "коллекция": getattr(i, "collection_address", "") or "",
                  "подарок Telegram": getattr(i, "gift", None) is not None}
                 for i in getattr(got, "items", None) or []]
        return {"NFT": items, "дальше": getattr(got, "next_offset", None) or ""}

    async def addresses(self, users: list[Any], *, raw: list[str] | None = None,
                        force: bool = False) -> list[dict[str, Any]]:
        """Адреса кошельков людей — куда переводить, не спрашивая адрес вручную."""
        from telethon.tl import functions

        request = _layer233(lambda: functions.wallet.GetUserAddressesRequest)
        inputs = [await self.client.get_input_entity(u) for u in users]
        got = await self._call(request(id=inputs, addresses=list(raw or []), force=force or None))
        names = {getattr(u, "id", 0): getattr(u, "username", None) or getattr(u, "first_name", "")
                 for u in getattr(got, "users", None) or []}
        return [{"кто": names.get(getattr(a, "user_id", None), "") or "",
                 "адрес": getattr(a, "address", "")}
                for a in getattr(got, "addresses", None) or []]

    async def gasless(self) -> dict[str, Any]:
        """Сколько переводов можно сделать, расплачиваясь не TON, а самим токеном."""
        from telethon.tl import functions

        got = await self._call(_layer233(lambda: functions.wallet.GetGaslessInfoRequest)())
        for update in getattr(got, "updates", None) or ([getattr(got, "update", None)] if hasattr(got, "update") else []):
            if type(update).__name__ == "UpdateWalletGaslessInfo":
                return {"доступно": bool(getattr(update, "available", False)),
                        "осталось": getattr(update, "left", None),
                        "обновится": _date(getattr(update, "reset_at", None)),
                        "минимум": getattr(update, "min_amount", None)}
        return {"доступно": False}

    async def legacy_balance(self) -> dict[str, Any]:
        """Остался ли баланс в старом @wallet — и где его забрать."""
        from telethon.tl import functions

        got = await self._call(_layer233(lambda: functions.wallet.GetExistingWaltBalanceRequest)())
        return {"есть баланс": bool(getattr(got, "has_balance", False)),
                "ссылка": getattr(got, "url", "") or ""}

    async def toncenter(self, endpoint: str, query: dict[str, str] | None = None) -> Any:
        """Чтение из toncenter через Telegram — только GET, без отправки в сеть."""
        from urllib.parse import urlencode

        from telethon.tl import functions

        endpoint = endpoint.strip().lstrip("/")
        if not endpoint or "://" in endpoint:
            raise WalletError("нужен путь метода toncenter, например getAddressBalance")
        request = _layer233(lambda: functions.toncenter.PerformApiRequestRequest)
        got = await self._call(request(endpoint=endpoint, query=urlencode(query or {}) or None))
        body = getattr(got, "response", "")
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        try:
            return json.loads(body)
        except (TypeError, ValueError):
            return {"ответ": body}

    # ── TonConnect ───────────────────────────────────────────────────────────
    async def sessions(self) -> list[dict[str, Any]]:
        from telethon.tl import functions

        got = await self._call(_layer233(lambda: functions.wallet.TonConnectGetSessionsRequest)())
        rows = []
        for s in getattr(got, "sessions", None) or []:
            manifest = getattr(s, "manifest", None)
            state = ("закрыта" if getattr(s, "closed", False) else
                     "закрывается" if getattr(s, "closing", False) else
                     "ждёт подтверждения" if getattr(s, "pending", False) else "активна")
            rows.append({"id": getattr(s, "id", 0), "приложение": getattr(manifest, "name", "") or "",
                         "сайт": getattr(manifest, "url", "") or "", "с": _date(getattr(s, "date", None)),
                         "состояние": state})
        return rows

    async def pending(self, session_id: int | None = None) -> list[dict[str, Any]]:
        """Чего от кошелька хотят приложения: подписать, отправить, подключиться."""
        from telethon.tl import functions

        request = _layer233(lambda: functions.wallet.TonConnectGetPendingRequest)
        got = await self._call(request(session_id=session_id))
        rows = []
        for r in getattr(got, "requests", None) or []:
            body = getattr(r, "body", b"")
            if isinstance(body, bytes):
                body = body.decode("utf-8", "replace")
            rows.append({"сессия": getattr(r, "session_id", 0), "запрос": getattr(r, "msg_id", 0),
                         "тема": getattr(r, "topic", "") or "", "до": _date(getattr(r, "expires", None)),
                         "что просят": body[:200]})
        return rows

    async def close_session(self, session_id: int) -> dict[str, Any]:
        """Отключить приложение от кошелька. Деньги не трогаются."""
        from telethon.tl import functions

        request = _layer233(lambda: functions.wallet.TonConnectCloseSessionRequest)
        await self._call(request(session_id=int(session_id)))
        return {"сессия": int(session_id), "закрыта": True}

    async def decline(self, session_id: int, msg_id: int, app_request_id: str) -> dict[str, Any]:
        """Отклонить запрос приложения. Согласие требует подписи — его здесь нет."""
        from telethon.tl import functions

        request = _layer233(lambda: functions.wallet.TonConnectClaimRequestRequest)
        await self._call(request(session_id=int(session_id), msg_id=int(msg_id),
                                 app_request_id=str(app_request_id), declined=True))
        return {"сессия": int(session_id), "запрос": int(msg_id), "отклонён": True}

    # ── покупка криптовалюты ─────────────────────────────────────────────────
    async def onramp_providers(self, crypto: str = "") -> list[dict[str, Any]]:
        from telethon.tl import functions

        request = _layer233(lambda: functions.payments.GetOnrampProvidersRequest)
        got = await self._call(request(crypto_currency=crypto.upper() or None))
        return [{"id": getattr(p, "id", ""), "название": getattr(p, "name", ""),
                 "криптовалюты": ", ".join(getattr(p, "crypto_currencies", None) or []),
                 "котировки": bool(getattr(p, "supports_quote", False))}
                for p in (got if isinstance(got, list) else getattr(got, "providers", None) or [])]

    async def onramp_currencies(self, provider: str, crypto: str) -> list[str]:
        from telethon.tl import functions

        request = _layer233(lambda: functions.payments.GetOnrampBaseCurrenciesRequest)
        got = await self._call(request(provider=provider, crypto_currency=crypto.upper()))
        return [str(c) for c in (got if isinstance(got, list) else getattr(got, "currencies", None) or [])]

    async def onramp_check(self, provider: str, crypto: str, base: str = "",
                           method: str = "") -> dict[str, Any]:
        """Доступна ли покупка и в каких пределах."""
        from telethon.tl import functions

        avail = await self._call(_layer233(lambda: functions.payments.GetOnrampAvailabilityRequest)(
            provider=provider, crypto_currency=crypto.upper(), base_currency=base.upper() or None))
        result = {"страна": getattr(avail, "country_code", "") or "",
                  "можно": bool(getattr(avail, "allowed", False)),
                  "покупка": bool(getattr(avail, "buy_allowed", False)),
                  "способы": [getattr(m, "payment_method", "") for m in getattr(avail, "methods", None) or []
                              if getattr(m, "available", False)]}
        if base:
            limits = await self._call(functions.payments.GetOnrampLimitsRequest(
                provider=provider, crypto_currency=crypto.upper(), base_currency=base.upper(),
                payment_method=method or None))
            result["от"] = f"{getattr(limits, 'base_min_amount', '')} {base.upper()}"
            result["до"] = f"{getattr(limits, 'base_max_amount', '')} {base.upper()}"
        return result

    async def onramp_quote(self, provider: str, crypto: str, base: str, *, base_amount: str = "",
                           crypto_amount: str = "", method: str = "") -> dict[str, Any]:
        from telethon.tl import functions

        fields = onramp_payload(provider, crypto, "-", base=base, base_amount=base_amount,
                                crypto_amount=crypto_amount, method=method)
        fields.pop("address")
        q = await self._call(_layer233(lambda: functions.payments.GetOnrampQuoteRequest)(**fields))
        return {"платите": f"{getattr(q, 'total_amount', '')} {getattr(q, 'base_currency', '')}",
                "получите": f"{getattr(q, 'crypto_amount', '')} {getattr(q, 'crypto_currency', '')}",
                "цена": getattr(q, "crypto_price", ""), "комиссия": getattr(q, "fee_amount", ""),
                "комиссия сети": getattr(q, "network_fee_amount", ""),
                "котировка до": _date(getattr(q, "expires_date", None))}

    async def onramp_session(self, provider: str, crypto: str, address: str, **amounts: str) -> dict[str, Any]:
        """Страница провайдера, где человек сам оплатит покупку. tgx ничего не платит."""
        from telethon.tl import functions

        fields = onramp_payload(provider, crypto, address, **amounts)
        s = await self._call(_layer233(lambda: functions.payments.CreateOnrampSessionRequest)(**fields))
        return {"страница оплаты": getattr(s, "url", ""), "провайдер": getattr(s, "provider", provider),
                "действует до": _date(getattr(s, "expires_date", None)),
                "дальше": "откройте страницу и завершите покупку сами — tgx деньги не списывает"}

    @staticmethod
    def needs_key() -> None:
        raise WalletError(NEEDS_KEY)
