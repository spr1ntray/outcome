"""
Модуль: Sell Positions — продажа (закрытие) позиций после холда
- Ожидание заданного времени (HOLD_TIME_RANGE)
- Продажа всех открытых позиций через SELL ордера (POST /orders/create)
- Отмена оставшихся ордеров (POST /orders/cancel-all)
- Slippage: 0.0-1.0 (НЕ проценты)
"""
import asyncio
from datetime import datetime, timezone
from typing import Optional

from core.account import Wallet
from core.outcome_client import OutcomeClient
from core.logger import get_logger
from core.utils import random_delay, random_int

import parameters as p


class SellPositionsModule:
    def __init__(self, wallet: Wallet, client: OutcomeClient):
        self.wallet = wallet
        self.client = client
        self.log = get_logger(wallet.address)

    async def run(self, placed_orders: list[dict] = None) -> bool:
        """
        Холдит позиции и затем продаёт их.
        Каждая позиция получает свой рандомный hold_time и закрывается независимо.
        placed_orders — список из BettingModule.run() (опционально).
        """
        self.log.info("Sell: начинаем процесс продажи позиций...")

        # 1. Получаем текущие позиции
        positions = await self.client.get_positions()
        if not positions:
            self.log.info("Sell: нет открытых позиций для продажи")
            await self._cancel_remaining_orders()
            return True

        # Фильтруем позиции с истёкшими маркетами (они дают 500 при продаже)
        def _is_market_open(pos: dict) -> bool:
            now = datetime.now(timezone.utc)
            for field in ("endDate", "end_date", "expiryDate", "expiry_date", "closeDate", "close_date",
                          "resolutionDate", "market_end_date"):
                val = pos.get(field) or (pos.get("event") or {}).get(field) or (pos.get("market") or {}).get(field)
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
                        return end_dt > now
                    except (ValueError, TypeError, OSError):
                        pass
            return True  # если дату не определить — пробуем продать

        open_positions = [p for p in positions if _is_market_open(p)]
        skipped = len(positions) - len(open_positions)
        if skipped:
            self.log.info(f"Sell: пропускаем {skipped} истёкших позиций")
        positions = open_positions

        if not positions:
            self.log.info("Sell: все позиции в истёкших маркетах, пропускаем")
            await self._cancel_remaining_orders()
            return True

        self.log.info(f"Sell: найдено {len(positions)} позиций, закрываем каждую в рандомное время...")

        # 2. Для каждой позиции — своя async задача с индивидуальным холдом
        async def _hold_and_sell(pos: dict) -> bool:
            hold_time = random_int(*p.HOLD_TIME_RANGE)
            title = (
                pos.get("title")
                or pos.get("event_nickname")
                or f"event_{pos.get('event_id') or pos.get('eventId', '?')}"
            )[:40]
            self.log.info(f"Sell: «{title}» — закроем через {hold_time} сек")
            await asyncio.sleep(hold_time)
            return await self._sell_position(pos, placed_orders)

        results = await asyncio.gather(*[_hold_and_sell(pos) for pos in positions])
        sold_count = sum(1 for r in results if r)

        # 3. Отменяем оставшиеся открытые ордера
        await self._cancel_remaining_orders()

        # 4. Финальный баланс
        balance = await self.client.get_balance()
        available = self.client.parse_balance(balance.get("available", "0"))
        self.log.success(f"Sell: продано {sold_count}/{len(positions)} позиций | Баланс: {available:.2f} USDT0")

        return sold_count > 0

    async def _sell_position(self, position: dict, placed_orders: list[dict] = None) -> bool:
        """Продаёт одну позицию."""
        # Извлекаем данные позиции (формат может варьироваться)
        event_id = (
            position.get("event_id")
            or position.get("eventId")
            or position.get("event", {}).get("id")
        )
        market_id = (
            position.get("market_id")
            or position.get("marketId")
        )
        asset = (
            position.get("asset")
            or position.get("side")
            or position.get("outcome")
            or "YES"
        ).upper()

        # Размер позиции
        size_raw = (
            position.get("size")
            or position.get("quantity")
            or position.get("shares")
            or position.get("amount")
            or "0"
        )

        try:
            quantity = int(float(str(size_raw)))
        except (ValueError, TypeError):
            quantity = 0

        if quantity <= 0:
            self.log.debug(f"Sell: пропускаем нулевую позицию: {position}")
            return False

        # Если market_id нет, вычисляем из event_id
        if not market_id and event_id:
            if asset == "YES":
                market_id = int(event_id) * 2
            else:
                market_id = int(event_id) * 2 + 1

        if not market_id:
            self.log.debug(f"Sell: пропускаем позицию без market_id/event_id: {position}")
            return False

        # Определяем цену
        sell_price = self._get_sell_price(position)

        # Название
        title = (
            position.get("title")
            or position.get("contract")
            or position.get("event", {}).get("title")
            or position.get("event_nickname")
            or f"event_{event_id}"
        )[:40]

        self.log.info(
            f"Sell: закрываем {asset} «{title}» | "
            f"market={market_id} qty={quantity} price={sell_price:.2f}"
        )

        # Ретраи
        attempts = random_int(*p.RETRY_RANGE)
        last_exc = None
        server_error = False

        for attempt in range(1, attempts + 1):
            try:
                result = await self.client.create_order(
                    market_id=int(market_id),
                    side="SELL",
                    order_type="MARKET",
                    quantity=quantity,
                    asset=asset,
                    limit_price=f"{sell_price:.2f}",
                    slippage=p.SELL_SLIPPAGE,
                )

                if result:
                    success = True
                    if isinstance(result, dict) and result.get("success") is False:
                        success = False

                    if success:
                        self.log.success(f"Sell: позиция «{title}» продана!")
                        return True
                    else:
                        self.log.warning(f"Sell: ордер не исполнен: {result}")
                else:
                    self.log.warning(f"Sell: пустой ответ при продаже {title}, retry {attempt}/{attempts}")

            except Exception as e:
                last_exc = e
                err_str = str(e)
                if "Server error" in err_str or "500" in err_str:
                    self.log.warning(f"Sell: «{title}» — рынок недоступен (500), пропускаем")
                    server_error = True
                    break
                if "no liquidity" in err_str.lower():
                    self.log.warning(f"Sell: «{title}» — нет ликвидности при продаже, пропускаем")
                    server_error = True
                    break
                if "paused" in err_str.lower():
                    self.log.warning(f"Sell: «{title}» — событие на паузе, пропускаем")
                    server_error = True
                    break
                self.log.debug(f"Sell: ошибка attempt {attempt}/{attempts}: {e}")

            if attempt < attempts:
                wait = random_int(*p.RETRY_DELAY_RANGE)
                await asyncio.sleep(wait)

        if not server_error:
            self.log.error(f"Sell: не удалось продать «{title}» после {attempts} попыток: {last_exc}")
        return False

    def _get_sell_price(self, position: dict) -> float:
        """Определяет цену для продажи."""
        for field in ["mark_price", "markPrice", "current_price", "lastPrice", "price"]:
            val = position.get(field)
            if val is not None:
                try:
                    p_val = float(str(val))
                    if 0.01 <= p_val <= 0.99:
                        return p_val
                except (ValueError, TypeError):
                    pass

        # Фолбэк — entry_price
        for field in ["entry_price", "entryPrice", "avgPrice", "avg_price"]:
            val = position.get(field)
            if val is not None:
                try:
                    p_val = float(str(val))
                    if 0.01 <= p_val <= 0.99:
                        return p_val
                except (ValueError, TypeError):
                    pass

        return 0.50

    async def _cancel_remaining_orders(self):
        """Отменяет все оставшиеся открытые ордера."""
        try:
            open_orders = await self.client.get_open_orders()
            if open_orders:
                self.log.info(f"Sell: отменяем {len(open_orders)} открытых ордеров...")
                await self.client.cancel_all_orders()
                self.log.info("Sell: все ордера отменены")
            else:
                self.log.debug("Sell: нет открытых ордеров для отмены")
        except Exception as e:
            self.log.debug(f"Sell: ошибка отмены ордеров: {e}")

    async def force_sell_all(self) -> bool:
        """
        Усиленная продажа ВСЕХ позиций. Без холда, максимум попыток.
        Используется когда обычная продажа не сработала.
        """
        self.log.info("ForceSell: начинаем принудительную продажу всех позиций...")

        # 1. Сначала отменяем ВСЕ открытые ордера — освобождаем баланс
        self.log.info("ForceSell: отменяем все открытые ордера...")
        await self._cancel_remaining_orders()
        await asyncio.sleep(3)

        total_sold = 0

        # 2. До 3 полных циклов продажи
        for cycle in range(1, 4):
            positions = await self.client.get_positions()
            if not positions:
                self.log.success(f"ForceSell: все позиции закрыты! (цикл {cycle})")
                break

            self.log.info(f"ForceSell: цикл {cycle}/3 — найдено {len(positions)} позиций")

            for pos in positions:
                success = await self._force_sell_one(pos)
                if success:
                    total_sold += 1
                await asyncio.sleep(2)

            # Небольшая пауза между циклами
            if cycle < 3:
                await asyncio.sleep(5)

        # 3. Итоговый баланс
        balance = await self.client.get_balance()
        available = self.client.parse_balance(balance.get("available", "0"))
        self.log.success(f"ForceSell: продано позиций: {total_sold} | Баланс: {available:.2f} USDT0")

        return total_sold > 0

    async def _force_sell_one(self, position: dict) -> bool:
        """Усиленная продажа одной позиции — 5 попыток, повышенный slippage."""
        # Извлекаем данные (та же логика что в _sell_position)
        event_id = (
            position.get("event_id") or position.get("eventId")
            or position.get("event", {}).get("id")
        )
        market_id = position.get("market_id") or position.get("marketId")
        asset = (
            position.get("asset") or position.get("side")
            or position.get("outcome") or "YES"
        ).upper()

        size_raw = (
            position.get("size") or position.get("quantity")
            or position.get("shares") or position.get("amount") or "0"
        )
        try:
            quantity = int(float(str(size_raw)))
        except (ValueError, TypeError):
            quantity = 0

        if quantity <= 0:
            return False

        if not market_id and event_id:
            market_id = int(event_id) * 2 if asset == "YES" else int(event_id) * 2 + 1

        if not market_id:
            return False

        sell_price = self._get_sell_price(position)
        title = (
            position.get("title") or position.get("event_nickname")
            or f"event_{event_id}"
        )[:40]

        self.log.info(f"ForceSell: закрываем {asset} «{title}» | market={market_id} qty={quantity}")

        # 5 попыток с повышенным slippage
        for attempt in range(1, 6):
            try:
                result = await self.client.create_order(
                    market_id=int(market_id),
                    side="SELL",
                    order_type="MARKET",
                    quantity=quantity,
                    asset=asset,
                    limit_price=f"{sell_price:.2f}",
                    slippage="0.20",  # повышенный slippage
                )
                if result and not (isinstance(result, dict) and result.get("success") is False):
                    self.log.success(f"ForceSell: позиция «{title}» закрыта!")
                    return True
                else:
                    self.log.warning(f"ForceSell: попытка {attempt}/5 не удалась: {result}")
            except Exception as e:
                err_str = str(e)
                if "Server error" in err_str or "500" in err_str:
                    self.log.warning(f"ForceSell: «{title}» — рынок недоступен (500), пропускаем")
                    return False
                if "no liquidity" in err_str.lower() or "paused" in err_str.lower():
                    self.log.warning(f"ForceSell: «{title}» — нет ликвидности или событие на паузе, пропускаем")
                    return False
                self.log.debug(f"ForceSell: попытка {attempt}/5 ошибка: {e}")

            if attempt < 5:
                await asyncio.sleep(3)

        self.log.error(f"ForceSell: не удалось закрыть «{title}» после 5 попыток")
        return False
