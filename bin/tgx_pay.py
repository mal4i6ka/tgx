#!/usr/bin/env python3
"""Платежи: звёзды, TON и обычные инвойсы.

Три разные валюты живут в одних и тех же вызовах и различаются флагом. Баланс
звёзд и баланс TON читает `payments.getStarsStatus` — с `ton=True` он отвечает
про криптовалюту; то же и у истории операций. Обычные деньги (карты через
провайдера) идут отдельным путём: инвойс со списком цен в минимальных единицах
валюты, форма оплаты и чек.

Оплата есть, но не сама по себе: `pay_stars` списывает звёзды, и вызывать её
полагается только после того, как человек нажал «Разрешить» в Telegram — этим
занимается `tgx_confirm`. Форма перед списанием запрашивается заново: между
показом суммы и согласием проходит время, а платить надо ровно за то, что
человек видел.
"""
from __future__ import annotations

from typing import Any, Sequence

# Звезда делится на 10^9 «нанозвёзд» — суммы приходят парой (amount, nanos).
NANOS = 1_000_000_000

# Валюты без дробной части: суммы в них передаются как есть, а не в сотых.
# XTR — звёзды — тоже целые: 50 в счёте означает пятьдесят звёзд, а не полста
# сотых. Без этого счёт на 50 выписывается на 5000.
ZERO_DECIMAL = {"XTR", "JPY", "KRW", "VND", "CLP", "ISK", "UGX", "XAF", "XOF",
                "PYG", "RWF", "VUV"}


class PayError(RuntimeError):
    """Платёжная операция, которую не удалось выполнить или разобрать."""


def amount_of(value: Any) -> float:
    """StarsAmount или StarsTonAmount → число. У звёзд есть дробная часть."""
    if value is None:
        return 0.0
    whole = getattr(value, "amount", 0) or 0
    nanos = getattr(value, "nanos", 0) or 0
    return round(whole + nanos / NANOS, 9)


def minor_units(currency: str, amount: float) -> int:
    """`12.5` в USD → 1250. Telegram принимает цену только в минимальных единицах."""
    if (currency or "").upper() in ZERO_DECIMAL:
        return int(round(amount))
    return int(round(amount * 100))


def describe_transaction(row: Any) -> dict[str, Any]:
    """Одна операция — плоской записью, без разбора всех видов партнёров."""
    partner = getattr(row, "peer", None)
    kind = type(partner).__name__.replace("StarsTransactionPeer", "") or "?"
    # Поле называется amount, а не stars: на stars приходили сплошные нули.
    stars = amount_of(getattr(row, "amount", None))
    date = getattr(row, "date", None)
    return {
        "id": getattr(row, "id", None),
        "дата": date.isoformat(timespec="seconds") if date else None,
        "сумма": stars,
        "направление": "приход" if stars > 0 else "расход",
        "кому": kind,
        "за что": getattr(row, "title", None) or getattr(row, "description", None) or "",
        "возврат": bool(getattr(row, "refund", False)),
        "не завершена": bool(getattr(row, "pending", False)),
    }


class Pay:
    """Балансы, история, чеки и выписка счетов."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def balance(self, *, ton: bool = False) -> dict[str, Any]:
        """Баланс звёзд или TON — тот же вызов, разный флаг."""
        from telethon.tl import functions, types

        result = await self.client(functions.payments.GetStarsStatusRequest(
            peer=types.InputPeerSelf(), ton=ton or None))
        subs = getattr(result, "subscriptions", None) or []
        return {
            "валюта": "TON" if ton else "звёзды",
            "баланс": amount_of(getattr(result, "balance", None)),
            "подписок": len(subs),
            "подписки": [{"чат": getattr(getattr(s, "peer", None), "channel_id", None),
                          "сумма": amount_of(getattr(s, "pricing", None)),
                          "до": str(getattr(s, "until_date", "") or "")[:10]}
                         for s in subs[:10]],
        }

    async def transactions(self, *, limit: int = 30, inbound: bool = False,
                           outbound: bool = False, ton: bool = False) -> list[dict[str, Any]]:
        """История операций. Без флагов — и приход, и расход."""
        from telethon.tl import functions, types

        result = await self.client(functions.payments.GetStarsTransactionsRequest(
            peer=types.InputPeerSelf(), offset="", limit=int(limit),
            inbound=inbound or None, outbound=outbound or None, ton=ton or None,
            ascending=None, subscription_id=None))
        return [describe_transaction(row) for row in (getattr(result, "history", None) or [])]

    async def receipt(self, chat: Any, msg_id: int) -> dict[str, Any]:
        """Чек по оплаченному сообщению."""
        from telethon.tl import functions

        peer = await self.client.get_input_entity(chat)
        try:
            got = await self.client(functions.payments.GetPaymentReceiptRequest(
                peer=peer, msg_id=int(msg_id)))
        except Exception as exc:
            raise self._explain(exc) from exc
        invoice = getattr(got, "invoice", None)
        date = getattr(got, "date", None)
        return {
            "название": getattr(got, "title", None),
            "описание": getattr(got, "description", None),
            "валюта": getattr(invoice, "currency", None),
            "итого": sum(p.amount for p in (getattr(invoice, "prices", None) or [])),
            "оплачено": date.isoformat(timespec="seconds") if date else None,
            "провайдер": getattr(got, "provider_id", None),
        }

    async def form(self, slug_or_link: str) -> dict[str, Any]:
        """Что просит счёт: сумма, валюта, какие данные потребуются.

        Только чтение — форма не отправляется, платёж не совершается.
        """
        from telethon.tl import functions, types

        slug = slug_or_link.rstrip("/").split("/")[-1].lstrip("$")
        try:
            got = await self.client(functions.payments.GetPaymentFormRequest(
                invoice=types.InputInvoiceSlug(slug=slug), theme_params=None))
        except Exception as exc:
            raise self._explain(exc) from exc
        invoice = getattr(got, "invoice", None)
        prices = getattr(invoice, "prices", None) or []
        return {
            "название": getattr(got, "title", None),
            "описание": getattr(got, "description", None),
            "валюта": getattr(invoice, "currency", None),
            "итого": sum(p.amount for p in prices),
            "строки": [{"за что": p.label, "сумма": p.amount} for p in prices],
            "нужен адрес": bool(getattr(invoice, "shipping_address_requested", False)),
            "нужен телефон": bool(getattr(invoice, "phone_requested", False)),
            "нужна почта": bool(getattr(invoice, "email_requested", False)),
            "пробный": bool(getattr(invoice, "test", False)),
            "повторяющийся": bool(getattr(invoice, "recurring", False)),
        }

    @staticmethod
    def bot_invoice_link(token: str, *, title: str, description: str, currency: str,
                         prices: Sequence[tuple[str, float]], payload: str = "tgx",
                         provider: str = "", test: bool = False, needs_name: bool = False,
                         needs_phone: bool = False, needs_email: bool = False,
                         needs_address: bool = False, subscription_period: int | None = None,
                         ) -> str:
        """Ссылка-счёт через Bot API.

        Счета выписывает бот: `payments.exportInvoice` от лица пользователя
        отвечает USER_BOT_REQUIRED. В звёздах валюта пишется XTR и провайдер не
        нужен — платёжной системой выступает сам Telegram.
        """
        import json

        import tgx_net

        code = (currency or "").upper()
        if not prices:
            raise PayError("в счёте должна быть хотя бы одна строка с ценой")
        if code != "XTR" and not provider:
            raise PayError(f"для валюты {code} нужен токен платёжного провайдера "
                           f"(--provider); звёзды — валюта XTR — обходятся без него")
        if subscription_period and code != "XTR":
            raise PayError("подписку можно продавать только за звёзды (XTR)")

        payload_fields: dict[str, Any] = {
            "title": title.strip(), "description": description.strip(),
            "payload": payload, "currency": code,
            "prices": json.dumps([{"label": str(label),
                                   "amount": minor_units(code, value)}
                                  for label, value in prices], ensure_ascii=False),
        }
        if provider:
            payload_fields["provider_token"] = provider
        if subscription_period:
            payload_fields["subscription_period"] = int(subscription_period)
        for flag, name in ((needs_name, "need_name"), (needs_phone, "need_phone_number"),
                           (needs_email, "need_email"), (needs_address, "need_shipping_address")):
            if flag:
                payload_fields[name] = "true"
        try:
            answer = tgx_net.post_form(
                f"https://api.telegram.org/bot{token}/createInvoiceLink",
                payload_fields, "Bot API")
        except tgx_net.NetError as exc:
            raise PayError(str(exc)) from exc
        if not answer.get("ok"):
            raise PayError(f"Bot API отказал: {answer.get('description', 'без объяснений')}")
        return answer["result"]

    async def invoice_link(self, *, title: str, description: str, currency: str,
                           prices: Sequence[tuple[str, float]], payload: str = "tgx",
                           provider: str = "", provider_data: str = "{}",
                           test: bool = False, needs_name: bool = False,
                           needs_phone: bool = False, needs_email: bool = False,
                           needs_address: bool = False) -> str:
        """Выписать ссылку-счёт от лица пользователя — Telegram это запрещает.

        Оставлено, чтобы объяснить отказ: счета выписывает бот, см. bot_invoice_link.

        В звёздах валюта пишется как XTR, и провайдер не нужен: Telegram сам
        выступает платёжной системой. Для обычных денег провайдерский токен
        обязателен, иначе счёт выписать не выйдет.
        """
        from telethon.tl import functions, types

        code = (currency or "").upper()
        if not prices:
            raise PayError("в счёте должна быть хотя бы одна строка с ценой")
        if code != "XTR" and not provider:
            raise PayError(f"для валюты {code} нужен токен платёжного провайдера "
                           f"(--provider); без него Telegram счёт не выпишет. "
                           f"Звёзды — валюта XTR — провайдера не требуют")

        rows = [types.LabeledPrice(label=str(label), amount=minor_units(code, value))
                for label, value in prices]
        media = types.InputMediaInvoice(
            title=title.strip(), description=description.strip(),
            invoice=types.Invoice(currency=code, prices=rows, test=test or None,
                                  name_requested=needs_name or None,
                                  phone_requested=needs_phone or None,
                                  email_requested=needs_email or None,
                                  shipping_address_requested=needs_address or None),
            payload=payload.encode(), provider=provider or None,
            provider_data=types.DataJSON(data=provider_data))
        try:
            exported = await self.client(functions.payments.ExportInvoiceRequest(
                invoice_media=media))
        except Exception as exc:
            raise self._explain(exc) from exc
        return exported.url

    async def pay_stars(self, slug_or_link: str) -> dict[str, Any]:
        """Оплатить счёт звёздами. Списывает деньги — вызывать только после согласия.

        Форма запрашивается заново перед оплатой: между показом суммы и нажатием
        могло пройти время, а платить надо ровно за то, что человек увидел.
        """
        from telethon.tl import functions, types

        slug = slug_or_link.rstrip("/").split("/")[-1].lstrip("$")
        invoice = types.InputInvoiceSlug(slug=slug)
        try:
            form = await self.client(functions.payments.GetPaymentFormRequest(
                invoice=invoice, theme_params=None))
            result = await self.client(functions.payments.SendStarsFormRequest(
                form_id=form.form_id, invoice=invoice))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {"оплачено": True, "форма": form.form_id,
                "результат": type(result).__name__}

    async def gift_options(self, user: Any) -> list[dict[str, Any]]:
        """Во что обойдётся подарить звёзды этому человеку."""
        from telethon.tl import functions

        entity = await self.client.get_input_entity(user)
        result = await self.client(functions.payments.GetStarsGiftOptionsRequest(user_id=entity))
        return [{"звёзд": getattr(o, "stars", None),
                 "цена": getattr(o, "amount", None),
                 "валюта": getattr(o, "currency", None)} for o in (result or [])]

    # ── подарки ──────────────────────────────────────────────────────────────
    async def gift_catalogue(self, limit: int = 40) -> list[dict[str, Any]]:
        """Какие подарки продаёт Telegram и почём."""
        from telethon.tl import functions

        result = await self.client(functions.payments.GetStarGiftsRequest(hash=0))
        out = []
        for gift in (getattr(result, "gifts", None) or [])[:limit]:
            out.append({
                "id": getattr(gift, "id", None),
                "звёзд": getattr(gift, "stars", None),
                "осталось": getattr(gift, "availability_remains", None),
                "всего": getattr(gift, "availability_total", None),
                "за продажу": getattr(gift, "convert_stars", None),
                "улучшаемый": getattr(gift, "upgrade_stars", None) is not None,
            })
        return out

    async def my_gifts(self, peer: Any = None, limit: int = 30) -> list[dict[str, Any]]:
        """Подарки, полученные мной или каналом."""
        from telethon.tl import functions, types

        target = await self.client.get_input_entity(peer) if peer else types.InputPeerSelf()
        result = await self.client(functions.payments.GetSavedStarGiftsRequest(
            peer=target, offset="", limit=int(limit)))
        out = []
        for saved in getattr(result, "gifts", None) or []:
            gift = getattr(saved, "gift", None)
            unique = getattr(gift, "title", None)
            out.append({
                "id": getattr(saved, "msg_id", None) or getattr(saved, "saved_id", None),
                "что": unique or f"подарок {getattr(gift, 'id', '?')}",
                "звёзд": getattr(gift, "stars", None),
                "уникальный": unique is not None,
                "на виду": not bool(getattr(saved, "unsaved", False)),
                "можно продать за": getattr(saved, "convert_stars", None),
            })
        return out

    async def convert_gift(self, msg_id: int) -> dict[str, Any]:
        """Обменять подарок на звёзды. Подарок исчезает — назад не вернуть."""
        from telethon.tl import functions, types

        try:
            await self.client(functions.payments.ConvertStarGiftRequest(
                stargift=types.InputSavedStarGiftUser(msg_id=int(msg_id))))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {"обменян": int(msg_id)}

    async def transfer_gift(self, msg_id: int, to: Any) -> dict[str, Any]:
        """Передать подарок другому. Необратимо."""
        from telethon.tl import functions, types

        target = await self.client.get_input_entity(to)
        try:
            await self.client(functions.payments.TransferStarGiftRequest(
                stargift=types.InputSavedStarGiftUser(msg_id=int(msg_id)), to_id=target))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {"передан": int(msg_id)}

    # ── подписки, доходы, коды ───────────────────────────────────────────────
    async def subscriptions(self, peer: Any = None) -> list[dict[str, Any]]:
        from telethon.tl import functions, types

        target = await self.client.get_input_entity(peer) if peer else types.InputPeerSelf()
        result = await self.client(functions.payments.GetStarsSubscriptionsRequest(
            peer=target, offset="", missing_balance=None))
        out = []
        for sub in getattr(result, "subscriptions", None) or []:
            out.append({
                "id": getattr(sub, "id", None),
                "звёзд": amount_of(getattr(sub, "pricing", None))
                or getattr(getattr(sub, "pricing", None), "amount", None),
                "до": str(getattr(sub, "until_date", "") or "")[:10],
                "отменена": bool(getattr(sub, "canceled", False)),
                "просрочена": bool(getattr(sub, "missing_balance", False)),
            })
        return out

    async def cancel_subscription(self, peer: Any, subscription_id: str,
                                  canceled: bool = True) -> dict[str, Any]:
        from telethon.tl import functions

        target = await self.client.get_input_entity(peer)
        await self.client(functions.payments.ChangeStarsSubscriptionRequest(
            peer=target, subscription_id=str(subscription_id), canceled=canceled))
        return {"подписка": subscription_id, "отменена": canceled}

    async def revenue(self, peer: Any, *, ton: bool = False) -> dict[str, Any]:
        """Сколько заработал канал или бот."""
        from telethon.tl import functions

        target = await self.client.get_input_entity(peer)
        result = await self.client(functions.payments.GetStarsRevenueStatsRequest(
            peer=target, dark=None, ton=ton or None))
        status = getattr(result, "status", None)
        return {
            "валюта": "TON" if ton else "звёзды",
            "доступно": amount_of(getattr(status, "available_balance", None)),
            "всего": amount_of(getattr(status, "overall_revenue", None)),
            "на выводе": amount_of(getattr(status, "current_balance", None)),
            "вывод доступен": bool(getattr(status, "withdrawal_enabled", False)),
        }

    async def check_code(self, slug: str) -> dict[str, Any]:
        """Что даёт подарочный код — до того, как его применить."""
        from telethon.tl import functions

        clean = slug.rstrip("/").split("/")[-1]
        try:
            result = await self.client(functions.payments.CheckGiftCodeRequest(slug=clean))
        except Exception as exc:
            raise self._explain(exc) from exc
        used = getattr(result, "used_date", None)
        return {
            "месяцев Premium": getattr(result, "months", None),
            "уже использован": used.isoformat(timespec="seconds") if used else None,
            "от кого": getattr(getattr(result, "from_id", None), "user_id", None),
        }

    async def apply_code(self, slug: str) -> dict[str, Any]:
        """Применить подарочный код к своему аккаунту. Одноразово."""
        from telethon.tl import functions

        clean = slug.rstrip("/").split("/")[-1]
        try:
            await self.client(functions.payments.ApplyGiftCodeRequest(slug=clean))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {"код применён": clean}

    async def giveaway(self, chat: Any, msg_id: int) -> dict[str, Any]:
        from telethon.tl import functions

        peer = await self.client.get_input_entity(chat)
        result = await self.client(functions.payments.GetGiveawayInfoRequest(
            peer=peer, msg_id=int(msg_id)))
        return {"вид": type(result).__name__.replace("GiveawayInfo", "") or "идёт",
                "участвую": bool(getattr(result, "participating", False)),
                "закончится": str(getattr(result, "finish_date", "") or "")[:19],
                "почему нельзя": getattr(result, "disallowed_country", None)
                or ("подписан слишком недавно" if getattr(result, "joined_too_early_date", None) else None)}

    async def paid_reaction(self, chat: Any, msg_id: int, count: int,
                            anonymous: bool = False) -> dict[str, Any]:
        """Платная реакция: звёзды уходят автору поста. Списывает деньги."""
        from telethon import helpers
        from telethon.tl import functions

        peer = await self.client.get_input_entity(chat)
        try:
            await self.client(functions.messages.SendPaidReactionRequest(
                peer=peer, msg_id=int(msg_id), count=int(count),
                random_id=helpers.generate_random_long(), private=anonymous or None))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {"сообщение": int(msg_id), "звёзд": int(count), "анонимно": anonymous}

    async def saved_info(self) -> dict[str, Any]:
        """Что Telegram хранит из платёжных данных."""
        from telethon.tl import functions

        result = await self.client(functions.payments.GetSavedInfoRequest())
        info = getattr(result, "saved_info", None)
        return {
            "сохранён способ оплаты": bool(getattr(result, "has_saved_credentials", False)),
            "имя": getattr(info, "name", None),
            "телефон": getattr(info, "phone", None),
            "почта": getattr(info, "email", None),
            "адрес": bool(getattr(info, "shipping_address", None)),
        }

    async def clear_saved(self, *, credentials: bool = True, info: bool = True) -> dict[str, Any]:
        """Стереть сохранённые платёжные данные."""
        from telethon.tl import functions

        await self.client(functions.payments.ClearSavedInfoRequest(
            credentials=credentials or None, info=info or None))
        return {"стёрто": {"способ оплаты": credentials, "адрес и контакты": info}}

    # ── то, что требует секрета ──────────────────────────────────────────────
    async def withdrawal_url(self, peer: Any, amount: float, *, ton: bool = False,
                             secret: str = "") -> dict[str, Any]:
        """Ссылка на вывод средств.

        Пароль от аккаунта не уходит на сервер: Telegram проверяет его по SRP,
        то есть по доказательству знания. Сам пароль не попадает ни в аргументы
        команды, ни в историю оболочки — его вводят в скрытую строку.

        Деньги эта команда не переводит: она возвращает ссылку на страницу
        вывода, где подтверждение происходит уже у человека.
        """
        from telethon import password as srp
        from telethon.tl import functions

        if not secret:
            raise PayError("для вывода нужен пароль от аккаунта — введите его в приглашении")
        target = await self.client.get_input_entity(peer)
        state = await self.client(functions.account.GetPasswordRequest())
        if not getattr(state, "has_password", False):
            raise PayError("на аккаунте не включён пароль (двухэтапная проверка) — "
                           "вывод без него невозможен")
        check = srp.compute_check(state, secret)
        try:
            result = await self.client(functions.payments.GetStarsRevenueWithdrawalUrlRequest(
                peer=target, password=check, ton=ton or None,
                amount=int(amount) if not ton else None))
        except Exception as exc:
            raise self._explain(exc) from exc
        return {"ссылка": getattr(result, "url", None),
                "валюта": "TON" if ton else "звёзды", "сумма": amount}

    async def card_bank(self, number: str) -> dict[str, Any]:
        """Какой банк выпустил карту.

        Сервер требует **полный** номер, а не только первые цифры: на шести
        отвечает BANK_CARD_NUMBER_INVALID. Тем важнее скрытый ввод — номер не
        должен попадать ни в аргументы команды, ни в историю оболочки, ни в
        логи. Здесь он живёт только в памяти процесса.
        """
        from telethon.tl import functions

        digits = "".join(c for c in number if c.isdigit())
        if len(digits) < 12:
            raise PayError("нужен полный номер карты: на первых цифрах сервер "
                           "отвечает отказом")
        request = functions.payments.GetBankCardDataRequest(number=digits)
        try:
            result = await self.client(request)
        except Exception as exc:
            # Справочник банков лежит не в «домашнем» дата-центре, и сервер
            # отвечает FILE_MIGRATE_N. Повторяем запрос там, куда он указал.
            dc = getattr(exc, "new_dc", None)
            if dc is None:
                raise self._explain(exc) from exc
            sender = await self.client._borrow_exported_sender(dc)
            try:
                result = await self.client._call(sender, request)
            except Exception as inner:
                raise self._explain(inner) from inner
        return {"банк": getattr(result, "title", None),
                "ссылки": [{"название": u.text, "адрес": u.url}
                           for u in (getattr(result, "open_urls", None) or [])]}

    @staticmethod
    def _explain(exc: Exception) -> Exception:
        text = str(exc)
        hints = {
            "PAYMENT_PROVIDER_INVALID": "платёжный провайдер не принят: проверьте токен от @BotFather",
            "CURRENCY_TOTAL_AMOUNT_INVALID": "сумма не подходит под правила этой валюты "
                                             "(есть минимум и максимум)",
            "INVOICE_PAYLOAD_INVALID": "служебная метка счёта слишком длинная или пустая",
            "MESSAGE_ID_INVALID": "по этому сообщению чека нет",
            "SLUG_INVALID": "такой ссылки-счёта не существует",
            "BOT_INVALID": "счета выписывает бот — нужен его токен",
            "STARGIFT_INVALID": "этот подарок недоступен",
            "GIFT_SLUG_INVALID": "такого подарочного кода нет",
            "GIFT_SLUG_EXPIRED": "подарочный код уже использован или просрочен",
            "PREMIUM_ACCOUNT_REQUIRED": "нужен Telegram Premium",
            "BALANCE_TOO_LOW": "не хватает звёзд на балансе",
            "STARGIFT_TRANSFER_TOO_EARLY": "подарок ещё нельзя передавать — не вышел срок",
            "PASSWORD_HASH_INVALID": "пароль не подошёл",
            "PASSWORD_MISSING": "на аккаунте не включена двухэтапная проверка",
            "BALANCE_TOO_LOW": "на балансе меньше запрошенной суммы",
            "BANK_CARD_NUMBER_INVALID": "номер карты не принят — нужен полный номер",
            "BANK_CARD_NOT_FOUND": "по этому номеру банк не определился",
        }
        for code, message in hints.items():
            if code in text:
                return PayError(message)
        return exc
