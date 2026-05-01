"""
Outcome API Client — авторизация (SIWE + cookie), запросы, ордера.
API: https://api-testnet-v2.outcome.xyz/api/v1/outcome
Auth: Cookie-based (outcome_token_{address})
Chain: Arbitrum Sepolia (421614)
"""
import asyncio
import random
from typing import Optional
from datetime import datetime, timezone

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from siwe import SiweMessage

import parameters as p
from core.account import Wallet
from core.logger import get_logger
from core.utils import random_int


class OutcomeClient:
    """HTTP-клиент для Outcome API с cookie-based SIWE-авторизацией."""

    def __init__(self, wallet: Wallet):
        self.wallet = wallet
        self.log = get_logger(wallet.address)
        self.account = Account.from_key(wallet.private_key)
        self._authenticated: bool = False
        self._contract_address: Optional[str] = None
        self._cookies: dict = {}

    # ------------------------------------------------------------------
    # Общий httpx-клиент (создаётся один раз, хранит куки)
    # ------------------------------------------------------------------

    def _get_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Origin": p.SIWE_URI,
            "Referer": f"{p.SIWE_URI}/",
            "X-Active-Address": self.wallet.address,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        }

    # ------------------------------------------------------------------
    # Авторизация (SIWE → cookie)
    # ------------------------------------------------------------------

    async def authenticate(self) -> bool:
        """Полный цикл SIWE-авторизации: nonce → sign → signin → cookie."""
        is_first_time = False
        try:
            # 1. Получаем nonce
            nonce_resp = await self._api_post("/auth/nonce", {
                "address": self.wallet.address,
            })
            # Ответ: {"success":true,"data":{"nonce":"hex32"}}
            if isinstance(nonce_resp, dict) and nonce_resp.get("success"):
                nonce = nonce_resp["data"]["nonce"]
            elif isinstance(nonce_resp, dict) and "nonce" in nonce_resp:
                nonce = nonce_resp["nonce"]
            elif isinstance(nonce_resp, dict) and "data" in nonce_resp:
                nonce = nonce_resp["data"].get("nonce", "")
            else:
                self.log.error(f"Outcome: не удалось получить nonce: {nonce_resp}")
                return False

            if not nonce:
                self.log.error(f"Outcome: пустой nonce: {nonce_resp}")
                return False

            # 2. Формируем SIWE-сообщение через библиотеку siwe
            issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

            siwe_msg = SiweMessage(
                domain=p.SIWE_DOMAIN,
                address=self.wallet.address,  # checksummed
                statement=p.SIWE_STATEMENT,
                uri=p.SIWE_URI,
                version="1",
                chain_id=p.CHAIN_ID,
                nonce=nonce,
                issued_at=issued_at,
            )

            message_text = siwe_msg.prepare_message()

            # 3. Подписываем
            msg = encode_defunct(text=message_text)
            signed = self.account.sign_message(msg)
            signature = "0x" + signed.signature.hex()

            # 4. Отправляем signin — сервер ставит куку outcome_token_{addr}
            signin_body = {
                "address":   self.wallet.address,
                "signature": signature,
                "issuedAt":  issued_at,
                "domain":    p.SIWE_DOMAIN,
                "uri":       p.SIWE_URI,
            }

            proxy = self.wallet.get_current_proxy()
            async with httpx.AsyncClient(proxy=proxy, timeout=30, follow_redirects=True) as client:
                resp = await client.post(
                    f"{p.OUTCOME_API_BASE}/auth/signin",
                    json=signin_body,
                    headers=self._get_headers(),
                )

            if resp.status_code == 200:
                # Сохраняем все куки из ответа
                for key, value in resp.cookies.items():
                    self._cookies[key] = value

                # Проверяем наличие нужной куки
                cookie_key = f"outcome_token_{self.wallet.address.lower()}"
                if cookie_key in self._cookies:
                    self._authenticated = True
                    self.log.success("Outcome: авторизация SIWE успешна (cookie set)")
                else:
                    # Иногда кука может прийти с другим именем — примем любую outcome_token_*
                    found_cookie = False
                    for k in self._cookies:
                        if k.startswith("outcome_token_"):
                            found_cookie = True
                            break
                    if found_cookie:
                        self._authenticated = True
                        self.log.success("Outcome: авторизация SIWE успешна (cookie set)")
                    else:
                        # Возможно JWT в теле ответа
                        try:
                            body = resp.json()
                            if body.get("success"):
                                self._authenticated = True
                                self.log.success("Outcome: авторизация SIWE успешна")
                            else:
                                self.log.error(f"Outcome: signin ответ без куки: {body}")
                                return False
                        except Exception:
                            self.log.error(f"Outcome: signin без куки, status={resp.status_code}")
                            return False
            else:
                self.log.error(f"Outcome: signin HTTP {resp.status_code}: {resp.text[:300]}")
                return False

            # Запоминаем is_firstTime если это новый кошелёк
            try:
                signin_body_resp = resp.json()
                is_first_time = signin_body_resp.get("data", {}).get("is_firstTime", False)
            except Exception:
                is_first_time = False

            # 5. Accept terms (на всякий случай)
            try:
                await self._api_post("/auth/accept-terms", {})
            except Exception:
                pass  # Не критично — terms могут быть уже приняты

            # 6. Получаем/ждём contract_address через /auth/me
            # Для новых кошельков (is_firstTime=True) сервер деплоит smart wallet — нужно поллить
            try:
                poll_attempts = 15 if is_first_time else 1
                for attempt in range(poll_attempts):
                    me = await self._api_get("/auth/me")
                    if isinstance(me, dict):
                        data = me.get("data", me)
                        self._contract_address = data.get("contract_address")
                    if self._contract_address:
                        self.log.info(f"Outcome: contract_address = {self._contract_address}")
                        break
                    if is_first_time and attempt < poll_attempts - 1:
                        self.log.info(f"Outcome: ожидаем деплой smart wallet... ({attempt + 1}/{poll_attempts})")
                        await asyncio.sleep(3)
                else:
                    if not self._contract_address:
                        self.log.warning("Outcome: contract_address не появился после ожидания")
            except Exception:
                pass

            # 7. Проверяем/включаем торговлю
            await self._ensure_trading_enabled()

            return True

        except Exception as e:
            self.log.error(f"Outcome: ошибка авторизации: {e}")
            return False

    async def _ensure_trading_enabled(self):
        """Проверяет allowance и включает торговлю если нужно."""
        try:
            # Проверяем allowance
            status = await self._api_post("/user/allowancestatus", {})
            approved = False
            if isinstance(status, dict):
                data = status.get("data", status)
                approved = data.get("approved", False)

            if approved:
                self.log.info("Outcome: торговля уже включена (approved)")
                return

            self.log.info("Outcome: включаем торговлю...")

            # Шаг 1: получаем messageHash от сервера
            sig_resp = await self._api_get("/enable-trading/signature")
            message_hash = None
            if isinstance(sig_resp, dict):
                inner = sig_resp.get("data", sig_resp)
                if isinstance(inner, dict):
                    message_hash = inner.get("messageHash") or inner.get("message_hash")

            if not message_hash:
                self.log.warning(f"Outcome: не удалось получить messageHash для enable-trading: {sig_resp}")
                return

            # Шаг 2: подписываем messageHash
            msg = encode_defunct(hexstr=message_hash)
            signed = self.account.sign_message(msg)
            signature = "0x" + signed.signature.hex()

            # Шаг 3: отправляем подпись
            result = await self._api_post("/enable-trading", {"signature": signature})
            if isinstance(result, dict) and result.get("success"):
                self.log.success("Outcome: торговля включена!")
            else:
                self.log.warning(f"Outcome: enable-trading ответ: {result}")

        except Exception as e:
            self.log.warning(f"Outcome: ошибка при enable-trading: {e}")

    # ------------------------------------------------------------------
    # User / Balance
    # ------------------------------------------------------------------

    async def get_balance(self) -> dict:
        """Получает баланс пользователя. Возвращает {available, locked, total} в raw units (6 decimals)."""
        try:
            data = await self._api_get("/user/balance")
            if isinstance(data, dict):
                bal = data.get("data", data).get("balance", data.get("balance", data))
                if isinstance(bal, dict):
                    return bal
            return {"available": "0", "locked": "0", "total": "0"}
        except Exception as e:
            self.log.debug(f"Outcome: ошибка получения баланса: {e}")
            return {"available": "0", "locked": "0", "total": "0"}

    def parse_balance(self, raw: str) -> float:
        """Конвертирует raw баланс (6 decimals) в человеко-читаемый формат."""
        try:
            return int(raw) / 1_000_000
        except (ValueError, TypeError):
            return 0.0

    # ------------------------------------------------------------------
    # Events / Markets
    # ------------------------------------------------------------------

    async def get_events(
        self,
        category: str = "",
        status: str = "OPEN",
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "volume",
    ) -> list:
        """Получает список событий."""
        params = {
            "status":   status,
            "page":     str(page),
            "pageSize": str(page_size),
            "sortBy":   sort_by,
        }
        if category:
            params["category"] = category
        try:
            data = await self._api_get("/events", params=params)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                inner = data.get("data", data)
                if isinstance(inner, list):
                    return inner
                if isinstance(inner, dict):
                    return inner.get("events", inner.get("items", []))
            return []
        except Exception as e:
            self.log.error(f"Outcome: ошибка получения событий: {e}")
            return []

    async def get_event(self, event_id: int) -> Optional[dict]:
        """Получает конкретное событие по ID."""
        try:
            data = await self._api_get(f"/events/{event_id}")
            if isinstance(data, dict):
                return data.get("data", data)
            return data
        except Exception as e:
            self.log.error(f"Outcome: ошибка получения события {event_id}: {e}")
            return None

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def create_order(
        self,
        market_id: int,
        side: str,        # "BUY" или "SELL"
        order_type: str,  # "MARKET" или "LIMIT"
        quantity: float,
        asset: str,       # "YES" или "NO"
        limit_price: str = "0.50",
        slippage: float = 0.05,
        event_nickname: str = "",
    ) -> Optional[dict]:
        """Создаёт ордер через POST /orders/create."""
        body = {
            "market_id":   market_id,
            "OrderSide":   side,
            "OrderType":   order_type,
            "quantity":    str(int(quantity)),
            "amount":      str(int(quantity)),
            "limit_price": str(limit_price),
            "asset":       asset,
            "slippage":    str(slippage),
        }
        if event_nickname:
            body["event_nickname"] = event_nickname
        result = await self._api_post("/orders/create", body)
        return result

    async def cancel_order(self, order_id: str) -> bool:
        """Отменяет ордер через DELETE /orders/cancel/{id}."""
        try:
            await self._api_delete(f"/orders/cancel/{order_id}")
            return True
        except Exception as e:
            self.log.error(f"Outcome: ошибка отмены ордера {order_id}: {e}")
            return False

    async def cancel_all_orders(self) -> bool:
        """Отменяет все открытые ордера через POST /orders/cancel-all."""
        try:
            await self._api_post("/orders/cancel-all", {})
            return True
        except Exception as e:
            self.log.error(f"Outcome: ошибка отмены всех ордеров: {e}")
            return False

    # ------------------------------------------------------------------
    # Positions / Open Orders
    # ------------------------------------------------------------------

    def _extract_list(self, data, fallback_keys: tuple) -> list:
        """
        Универсальный парсер ответов API.
        Обрабатывает структуры:
          - [...] — прямой список
          - {"data": [...]} — один уровень вложения
          - {"data": {"data": [...]}} — двойное вложение (реальный формат Outcome API)
          - {"data": {"data": {"orders": [...]}}} — тройное с именованным ключом
        """
        if isinstance(data, list):
            return data

        if not isinstance(data, dict):
            return []

        # Разворачиваем до двух уровней "data"
        candidate = data
        for _ in range(3):  # максимум 3 уровня распаковки
            if isinstance(candidate, list):
                return candidate
            if not isinstance(candidate, dict):
                return []
            # Если есть прямой список по одному из fallback_keys — возвращаем
            for key in fallback_keys:
                val = candidate.get(key)
                if isinstance(val, list):
                    return val
            # Иначе идём глубже по ключу "data"
            next_level = candidate.get("data")
            if next_level is None:
                return []
            candidate = next_level

        # Последняя попытка после трёх уровней
        if isinstance(candidate, list):
            return candidate
        if isinstance(candidate, dict):
            for key in fallback_keys:
                val = candidate.get(key)
                if isinstance(val, list):
                    return val
        return []

    async def get_positions(self) -> list:
        """Получает текущие позиции пользователя через GET /user/positions."""
        try:
            data = await self._api_get("/user/positions")
            return self._extract_list(data, ("positions", "items", "list", "result"))
        except Exception as e:
            self.log.debug(f"Outcome: ошибка получения позиций: {e}")
            return []

    async def get_open_orders(self) -> list:
        """Получает открытые ордера через GET /user/open-orders."""
        try:
            data = await self._api_get("/user/open-orders")
            return self._extract_list(data, ("orders", "items", "list", "result"))
        except Exception as e:
            self.log.debug(f"Outcome: ошибка получения открытых ордеров: {e}")
            return []

    async def get_trade_history(self, page: int = 1) -> list:
        """Получает историю сделок через GET /user/trade-history."""
        try:
            data = await self._api_get("/user/trade-history", params={"page": str(page)})
            return self._extract_list(data, ("trades", "orders", "history", "items", "list", "result"))
        except Exception as e:
            self.log.debug(f"Outcome: ошибка получения trade history: {e}")
            return []

    async def get_trade_count(self) -> int:
        """Возвращает общее количество сделок через pagination.total."""
        try:
            data = await self._api_get("/user/trade-history", params={"page": "1", "pageSize": "1"})
            # Разворачиваем двойное data-вложение
            if isinstance(data, dict):
                inner = data.get("data", {})
                if isinstance(inner, dict):
                    deep = inner.get("data", inner)
                    if isinstance(deep, dict):
                        pagination = deep.get("pagination", {})
                    else:
                        # inner сам содержит pagination
                        pagination = inner.get("pagination", {})
                    total = pagination.get("total", 0)
                    if total:
                        return int(total)
            return 0
        except Exception as e:
            self.log.debug(f"Outcome: ошибка получения trade count: {e}")
            return 0

    # ------------------------------------------------------------------
    # Orderbook (public — без авторизации)
    # ------------------------------------------------------------------

    async def get_orderbook(self, market_id: int) -> Optional[dict]:
        """Получает ордербук маркета (public, без авторизации)."""
        proxy = self.wallet.get_current_proxy()
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=20) as client:
                resp = await client.get(
                    f"{p.OUTCOME_API_BASE}/orderbook/{market_id}",
                    headers={"Content-Type": "application/json"},
                )
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception as e:
            self.log.debug(f"Outcome: ошибка получения ордербука {market_id}: {e}")
            return None

    # ------------------------------------------------------------------
    # Faucet
    # ------------------------------------------------------------------

    async def faucet_mint(self, amount: int) -> Optional[dict]:
        """Запрашивает тестовые токены через POST /faucet/mint.

        При 400 не поглощает ошибку — возвращает тело ответа как dict,
        чтобы вызывающий код мог обработать FAUCET_LIMIT_EXCEEDED.
        При сетевой ошибке возвращает None.
        """
        proxy = self.wallet.get_current_proxy()
        try:
            async with httpx.AsyncClient(
                proxy=proxy,
                timeout=30,
                follow_redirects=True,
                cookies=self._cookies,
            ) as client:
                resp = await client.post(
                    f"{p.OUTCOME_API_BASE}/faucet/mint",
                    json={"address": self.wallet.address, "amount": str(amount)},
                    headers=self._get_headers(),
                )
                for key, value in resp.cookies.items():
                    self._cookies[key] = value

                if resp.status_code >= 400:
                    self.log.error(f"Outcome: /faucet/mint [{resp.status_code}] body: {resp.text[:500]}")
                    try:
                        return resp.json()
                    except Exception:
                        return None

                return resp.json()
        except Exception as e:
            self.log.error(f"Outcome: ошибка faucet/mint: {e}")
            return None

    # ------------------------------------------------------------------
    # HTTP helpers (cookie-based auth)
    # ------------------------------------------------------------------

    async def _api_get(self, path: str, params: dict = None) -> dict:
        proxy = self.wallet.get_current_proxy()
        attempts = random_int(*p.RETRY_RANGE)
        last_exc = None

        for attempt in range(1, attempts + 1):
            try:
                async with httpx.AsyncClient(
                    proxy=proxy,
                    timeout=30,
                    follow_redirects=True,
                    cookies=self._cookies,
                ) as client:
                    resp = await client.get(
                        f"{p.OUTCOME_API_BASE}{path}",
                        params=params,
                        headers=self._get_headers(),
                    )
                    # Обновляем куки
                    for key, value in resp.cookies.items():
                        self._cookies[key] = value

                # Логируем тело при ошибке
                if resp.status_code == 404:
                    self.log.warning(f"Outcome: {path} [404] endpoint недоступен")
                elif resp.status_code >= 500:
                    self.log.warning(f"Outcome: {path} [{resp.status_code}] server error")
                elif resp.status_code >= 400:
                    self.log.error(f"Outcome: {path} [{resp.status_code}] body: {resp.text[:500]}")

                # 404/501 и 5xx — ретраить бессмысленно
                if resp.status_code in (404, 501):
                    last_exc = RuntimeError(f"Outcome API {path} недоступен ({resp.status_code})")
                    break

                if resp.status_code >= 500:
                    last_exc = RuntimeError(
                        f"Server error '{resp.status_code} {resp.reason_phrase}' for url '{resp.url}'"
                    )
                    break

                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                last_exc = e
                if attempt < attempts:
                    wait = random_int(*p.RETRY_DELAY_RANGE)
                    self.log.debug(f"Outcome: GET {path} retry {attempt}/{attempts} через {wait}s: {e}")
                    await asyncio.sleep(wait)
        raise last_exc

    async def _api_post(self, path: str, body: dict) -> dict:
        proxy = self.wallet.get_current_proxy()
        attempts = random_int(*p.RETRY_RANGE)
        last_exc = None

        for attempt in range(1, attempts + 1):
            try:
                async with httpx.AsyncClient(
                    proxy=proxy,
                    timeout=30,
                    follow_redirects=True,
                    cookies=self._cookies,
                ) as client:
                    resp = await client.post(
                        f"{p.OUTCOME_API_BASE}{path}",
                        json=body,
                        headers=self._get_headers(),
                    )
                    # Обновляем куки
                    for key, value in resp.cookies.items():
                        self._cookies[key] = value

                # Логируем тело при ошибке
                if resp.status_code == 404:
                    self.log.warning(f"Outcome: {path} [404] endpoint недоступен")
                elif resp.status_code >= 500:
                    self.log.warning(f"Outcome: {path} [{resp.status_code}] server error")
                elif resp.status_code >= 400:
                    body_text = resp.text[:500]
                    body_lower = body_text.lower()
                    if "no liquidity" in body_lower:
                        self.log.warning(f"Outcome: {path} [400] нет ликвидности в стакане")
                    elif "paused" in body_lower:
                        self.log.warning(f"Outcome: {path} [400] событие на паузе")
                    else:
                        self.log.error(f"Outcome: {path} [{resp.status_code}] body: {body_text}")

                # 404/501 и 5xx — ретраить бессмысленно
                if resp.status_code in (404, 501):
                    last_exc = RuntimeError(f"Outcome API {path} недоступен ({resp.status_code})")
                    break

                if resp.status_code >= 500:
                    last_exc = RuntimeError(
                        f"Server error '{resp.status_code} {resp.reason_phrase}' for url '{resp.url}'"
                    )
                    break

                # 400 с постоянными причинами — ретраить бессмысленно
                if resp.status_code == 400:
                    body_lower = resp.text.lower()
                    if "no liquidity" in body_lower:
                        last_exc = RuntimeError("No liquidity within slippage bounds")
                        break
                    if "paused" in body_lower:
                        last_exc = RuntimeError("Event is currently PAUSED")
                        break

                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                last_exc = e
                if attempt < attempts:
                    wait = random_int(*p.RETRY_DELAY_RANGE)
                    self.log.debug(f"Outcome: POST {path} retry {attempt}/{attempts} через {wait}s: {e}")
                    await asyncio.sleep(wait)
        raise last_exc

    async def _api_delete(self, path: str) -> dict:
        proxy = self.wallet.get_current_proxy()
        attempts = random_int(*p.RETRY_RANGE)
        last_exc = None

        for attempt in range(1, attempts + 1):
            try:
                async with httpx.AsyncClient(
                    proxy=proxy,
                    timeout=30,
                    follow_redirects=True,
                    cookies=self._cookies,
                ) as client:
                    resp = await client.delete(
                        f"{p.OUTCOME_API_BASE}{path}",
                        headers=self._get_headers(),
                    )
                    for key, value in resp.cookies.items():
                        self._cookies[key] = value

                # Логируем тело при ошибке
                if resp.status_code == 404:
                    self.log.warning(f"Outcome: {path} [404] endpoint недоступен")
                elif resp.status_code >= 500:
                    self.log.warning(f"Outcome: {path} [{resp.status_code}] server error")
                elif resp.status_code >= 400:
                    self.log.error(f"Outcome: {path} [{resp.status_code}] body: {resp.text[:500]}")

                # 404/501 и 5xx — ретраить бессмысленно
                if resp.status_code in (404, 501):
                    last_exc = RuntimeError(f"Outcome API {path} недоступен ({resp.status_code})")
                    break

                if resp.status_code >= 500:
                    last_exc = RuntimeError(
                        f"Server error '{resp.status_code} {resp.reason_phrase}' for url '{resp.url}'"
                    )
                    break

                resp.raise_for_status()
                try:
                    return resp.json()
                except Exception:
                    return {"success": True}
            except Exception as e:
                last_exc = e
                if attempt < attempts:
                    wait = random_int(*p.RETRY_DELAY_RANGE)
                    self.log.debug(f"Outcome: DELETE {path} retry {attempt}/{attempts} через {wait}s: {e}")
                    await asyncio.sleep(wait)
        raise last_exc
