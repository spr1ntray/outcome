"""
Модуль: Betting — ставки на события Outcome
- Получение списка открытых событий
- Выбор рандомного или конкретного события
- Создание ордеров BUY YES/NO через POST /orders/create
- Slippage: 0.0-1.0 (НЕ проценты)
- Баланс: 6 decimals (делим на 1_000_000)
"""
import random
from datetime import datetime, timezone
from typing import Optional

from core.account import Wallet
from core.outcome_client import OutcomeClient
from core.logger import get_logger
from core.utils import random_delay, random_int, random_choice

import parameters as p


class BettingModule:
    def __init__(self, wallet: Wallet, client: OutcomeClient):
        self.wallet = wallet
        self.client = client
        self.log = get_logger(wallet.address)

    async def run(self) -> list[dict]:
        """
        Делает ставки. Возвращает список сделанных ордеров.
        Каждый элемент: {event_id, market_id, side, asset, quantity, order_result}
        """
        self.log.info("Betting: начинаем делать ставки...")

        # 1. Получаем баланс (6 decimals)
        balance_data = await self.client.get_balance()
        available_raw = balance_data.get("available", "0")
        available = self.client.parse_balance(available_raw)
        self.log.info(f"Betting: доступный баланс = {available:.2f} USDT0")

        if available < 1:
            self.log.warning("Betting: баланс слишком мал для ставок")
            return []

        # 2. Получаем события для ставок
        events = await self._get_target_events()
        if not events:
            self.log.warning("Betting: нет доступных событий для ставок")
            return []

        # 3. Определяем количество ставок и размер каждой части
        bet_count = random_int(*p.BET_COUNT_RANGE)
        bet_count = min(bet_count, len(events))

        # Делим бюджет на равные части заранее
        bet_amount = available / bet_count

        # Если часть слишком мала — уменьшаем количество ставок
        if bet_amount < p.BET_MIN_AMOUNT:
            bet_count = max(1, int(available / p.BET_MIN_AMOUNT))
            bet_amount = available / bet_count

        self.log.info(
            f"Betting: {bet_count} ставок по {bet_amount:.2f} USDT0 каждая "
            f"(баланс {available:.2f})"
        )

        if bet_amount < p.BET_MIN_AMOUNT:
            self.log.warning(f"Betting: баланс слишком мал для ставок (минимум {p.BET_MIN_AMOUNT} USDT0)")
            return []

        # 4. Делаем ставки
        placed_orders = []

        for i in range(bet_count):
            event = events[i % len(events)]
            order_info = await self._place_bet(event, bet_amount, i + 1)

            if order_info:
                placed_orders.append(order_info)

            if i < bet_count - 1:
                await random_delay(*p.BET_DELAY_RANGE)

        self.log.info(f"Betting: сделано {len(placed_orders)}/{bet_count} ставок")
        return placed_orders

    async def _get_target_events(self) -> list:
        """Получает список событий для ставок."""
        # Если указан конкретный event ID
        if p.TARGET_EVENT_ID > 0:
            event = await self.client.get_event(p.TARGET_EVENT_ID)
            if event:
                title = event.get("title") or event.get("name") or "?"
                self.log.info(f"Betting: целевое событие: {title}")
                return [event]
            self.log.warning(f"Betting: событие {p.TARGET_EVENT_ID} не найдено, берём рандомное")

        # Получаем открытые события
        events = await self.client.get_events(
            category=p.TARGET_CATEGORY,
            status="OPEN",
            page=1,
            page_size=50,
            sort_by="volume",
        )

        if not events:
            return []

        # Фильтруем активные
        active = []
        for ev in events:
            status = ev.get("status", "")
            if status == "OPEN" or not status:
                active.append(ev)

        if not active:
            active = events[:10]

        # Фильтруем события с истёкшей датой окончания или истекающие слишком скоро
        def is_not_expired(ev):
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            min_end = now + timedelta(hours=p.BET_EVENT_MIN_HOURS_TO_EXPIRY)
            for field in ("endDate", "end_date", "expiryDate", "expiry_date", "closeDate", "close_date", "resolutionDate"):
                val = ev.get(field)
                if val:
                    try:
                        if isinstance(val, (int, float)):
                            ts = val / 1000 if val > 1e10 else val
                            end_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                        else:
                            end_str = str(val).replace("Z", "+00:00")
                            end_dt = datetime.fromisoformat(end_str)
                        if end_dt.tzinfo is None:
                            end_dt = end_dt.replace(tzinfo=timezone.utc)
                        return end_dt > min_end
                    except (ValueError, TypeError, OSError):
                        pass
            return True  # если дату не определить — не фильтруем
        before = len(active)
        active = [ev for ev in active if is_not_expired(ev)]
        if len(active) < before:
            self.log.debug(f"Betting: отфильтровано {before - len(active)} истёкших событий")

        # Фильтруем события с очень низкой ценой на обеих сторонах (нет ликвидности)
        def has_liquidity(ev):
            try:
                yes_p = float(ev.get("yesMarketPrice", "0") or 0)
                no_p = float(ev.get("noMarketPrice", "0") or 0)
                # Хотя бы одна сторона должна иметь разумную цену
                return yes_p >= 0.05 or no_p >= 0.05
            except (ValueError, TypeError):
                return True  # если цену не определить — не фильтруем
        active = [ev for ev in active if has_liquidity(ev)]

        # Перемешиваем
        random.shuffle(active)
        return active

    async def _place_bet(self, event: dict, available_balance: float, bet_num: int) -> Optional[dict]:
        """Размещает одну ставку на событие."""
        event_id = event.get("id")
        title = (event.get("title") or event.get("name") or "Unknown")[:50]

        if not event_id:
            self.log.warning(f"Betting [{bet_num}]: у события нет ID, пропускаем")
            return None

        # Определяем сторону (YES / NO)
        if p.BET_SIDE == "RANDOM":
            asset = random_choice(["YES", "NO"])
        else:
            asset = p.BET_SIDE

        # Берём market_id из данных события (yesMarketId/noMarketId), иначе формула
        if asset == "YES":
            market_id = event.get("yesMarketId") or (event_id * 2)
        else:
            market_id = event.get("noMarketId") or (event_id * 2 + 1)

        market_id = int(market_id)

        # Определяем рыночную цену из события
        market_price = self._extract_market_price(event, asset)
        if market_price < 0.05:
            # Переключаемся на другую сторону
            other_asset = "NO" if asset == "YES" else "YES"
            other_price = self._extract_market_price(event, other_asset)
            if other_price >= 0.05:
                asset = other_asset
                market_price = other_price
                if asset == "YES":
                    market_id = event.get("yesMarketId") or (event_id * 2)
                else:
                    market_id = event.get("noMarketId") or (event_id * 2 + 1)
                market_id = int(market_id)
                self.log.debug(f"Betting [{bet_num}]: переключились на {asset} (цена {market_price:.2f})")
            else:
                self.log.debug(f"Betting [{bet_num}]: event {event_id} — нет ликвидности на обеих сторонах, пропускаем")
                return None

        # quantity = сумма ставки в USDC (передаётся готовой из run())
        quantity = int(available_balance)
        quantity = max(1, quantity)

        # Для лимитных ордеров — цена = текущая рыночная
        limit_price = f"{market_price:.2f}"

        self.log.info(
            f"Betting [{bet_num}]: {asset} на «{title}» | "
            f"event={event_id} market={market_id} | "
            f"qty={quantity} limit_price={limit_price} | "
            f"≈{available_balance:.2f} USDT0"
        )

        # Ретраи для создания ордера
        attempts = random_int(*p.RETRY_RANGE)
        last_exc = None
        server_error = False

        for attempt in range(1, attempts + 1):
            try:
                result = await self.client.create_order(
                    market_id=market_id,
                    side="BUY",
                    order_type=p.BET_ORDER_TYPE,
                    quantity=quantity,
                    asset=asset,
                    limit_price=limit_price,
                    slippage=p.BET_SLIPPAGE,
                )

                if result:
                    # Проверяем успешность
                    success = True
                    if isinstance(result, dict):
                        if result.get("success") is False:
                            success = False
                            error_msg = result.get("data", {}).get("error") or result.get("message", "")
                            self.log.warning(
                                f"Betting [{bet_num}]: ордер не создан: {error_msg}"
                            )

                    if success:
                        self.log.success(
                            f"Betting [{bet_num}]: ставка размещена! "
                            f"{asset} на «{title}» qty={quantity}"
                        )
                        return {
                            "event_id":     event_id,
                            "market_id":    market_id,
                            "title":        title,
                            "asset":        asset,
                            "side":         "BUY",
                            "quantity":     quantity,
                            "price":        market_price,
                            "order_result": result,
                        }
                else:
                    self.log.warning(f"Betting [{bet_num}]: пустой ответ, retry {attempt}/{attempts}")

            except Exception as e:
                last_exc = e
                err_str = str(e)
                if "Server error" in err_str or "500" in err_str:
                    self.log.warning(f"Betting [{bet_num}]: рынок event={event_id} недоступен (500), пропускаем")
                    server_error = True
                    break
                if "no liquidity" in err_str.lower():
                    self.log.warning(f"Betting [{bet_num}]: «{title}» — нет ликвидности, пропускаем")
                    server_error = True
                    break
                if "paused" in err_str.lower():
                    self.log.warning(f"Betting [{bet_num}]: «{title}» — событие на паузе, пропускаем")
                    server_error = True
                    break
                self.log.debug(f"Betting [{bet_num}]: ошибка attempt {attempt}/{attempts}: {e}")

            if attempt < attempts:
                wait = random_int(*p.RETRY_DELAY_RANGE)
                await random_delay(wait, wait + 3)

        if not server_error:
            self.log.error(f"Betting [{bet_num}]: не удалось разместить ставку после {attempts} попыток: {last_exc}")
        return None

    def _extract_market_price(self, event: dict, asset: str) -> float:
        """Извлекает рыночную цену из данных события."""
        # Пробуем разные поля
        if asset == "YES":
            price_fields = ["yesMarketPrice", "yes_price", "chance", "yesPrice"]
        else:
            price_fields = ["noMarketPrice", "no_price", "noPrice"]

        for field in price_fields:
            val = event.get(field)
            if val is not None:
                try:
                    p_val = float(val)
                    if 0.01 <= p_val <= 0.99:
                        return p_val
                except (ValueError, TypeError):
                    pass

        # Если YES цена есть, NO = 1 - YES
        for field in ["yesMarketPrice", "yes_price", "chance", "yesPrice"]:
            val = event.get(field)
            if val is not None:
                try:
                    yes_p = float(val)
                    if 0.01 <= yes_p <= 0.99:
                        return (1 - yes_p) if asset == "NO" else yes_p
                except (ValueError, TypeError):
                    pass

        # Дефолт
        return 0.50
