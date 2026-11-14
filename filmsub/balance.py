"""Чтение баланса: ключи отправляются только своему сервису, без запросов к моделям."""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .api import USER_AGENT

ENDPOINTS = {
    'DeepSeek': 'https://api.deepseek.com/user/balance',
    'OpenRouter': 'https://openrouter.ai/api/v1/credits',
}


class BalanceError(Exception):
    pass


@dataclass(frozen=True)
class Balance:
    amounts: tuple[tuple[str, Decimal], ...]

    def label(self):
        return ' / '.join(f'{amount:.2f} {currency}' for currency, amount in self.amounts)


def _number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('invalid amount')
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('non-finite amount')
    return number


def parse_balance(provider, data):
    try:
        if provider == 'DeepSeek':
            rows = data['balance_infos']
            if not isinstance(rows, list) or not rows:
                raise ValueError('missing balances')
            amounts = []
            for row in rows:
                currency = row['currency']
                if currency not in {'USD', 'CNY'}:
                    raise ValueError('unknown currency')
                amounts.append((currency, _number(row['total_balance'])))
            return Balance(tuple(amounts))
        if provider == 'OpenRouter':
            credits = data['data']
            return Balance((('USD', _number(credits['total_credits']) - _number(credits['total_usage'])),))
        raise BalanceError('Неизвестный сервис')
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise BalanceError('Сервис вернул непонятные данные баланса') from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Не переносим Authorization на адрес из перенаправления.
        return None


def fetch_balance(provider, key, timeout=15):
    if provider not in ENDPOINTS:
        raise BalanceError('Неизвестный сервис')
    if not key.strip():
        raise BalanceError('Не указан ключ')
    request = urllib.request.Request(ENDPOINTS[provider], method='GET', headers={
        'Authorization': f'Bearer {key.strip()}', 'User-Agent': USER_AGENT,
        'Accept': 'application/json',
    })
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(
        context=ssl.create_default_context()))
    try:
        with opener.open(request, timeout=timeout) as response:
            # Не выводим тело ошибки: сервис может включить в него ключ или другие данные.
            data = json.loads(response.read(1_000_001), parse_float=Decimal)
        return parse_balance(provider, data)
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()
        if code == 401:
            raise BalanceError('Ключ не принят (401) — проверьте настройки') from None
        if code == 403 and provider == 'OpenRouter':
            raise BalanceError('Доступ запрещён (403) — нужен действующий management key') from None
        if code == 429:
            raise BalanceError('Лимит запросов (429) — обновите позже') from None
        raise BalanceError(f'Сервис не вернул баланс (HTTP {code})') from None
    except (TimeoutError, socket.timeout):
        raise BalanceError('Сервис не ответил вовремя') from None
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        raise BalanceError('Нет связи с сервисом') from None
    except (ValueError, UnicodeError):
        raise BalanceError('Сервис вернул непонятные данные баланса') from None
