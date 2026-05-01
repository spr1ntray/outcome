"""
Модуль: Faucet — получение тестовых USDT0 через Outcome API
Endpoint: POST /faucet/mint
Макс: 10000 USDT0 за раз
"""
import asyncio
from typing import Optional

from core.account import Wallet
from core.outcome_client import OutcomeClient
from core.logger import get_logger
from core.utils import random_delay, random_int

import parameters as p


class FaucetModule:
    def __init__(self, wallet: Wallet, outcome_client: OutcomeClient):
        self.wallet = wallet
        self.log = get_logger(wallet.address)
        self.client = outcome_client

    def _parse_remaining_from_error(self, error_msg: str) -> int:
        """Парсит оставшееся количество из сообщения FAUCET_LIMIT_EXCEEDED."""
        import re
        for pattern in (r'up to ([0-9,]+)', r'remaining[:\s]+([0-9,]+)', r'can claim[:\s]+([0-9,]+)'):
            match = re.search(pattern, error_msg, re.IGNORECASE)
            if match:
                try:
                    return int(match.group(1).replace(',', ''))
                except ValueError:
                    pass
        return 0

    async def _try_mint(self, amount: int) -> bool:
        """Пытается получить amount USDT0. Возвращает True при успехе."""
        result = await self.client.faucet_mint(amount)

        if result is None:
            # Сетевая ошибка — сигнализируем наружу через исключение
            raise RuntimeError("сетевая ошибка (None)")

        if not isinstance(result, dict):
            raise RuntimeError(f"неожиданный тип ответа: {type(result)}")

        # Успех
        if result.get("success"):
            data = result.get("data", {})
            tx_hash = (
                data.get("txHash")
                or data.get("tx_hash")
                or data.get("transactionHash", "")
            )
            self.log.success(f"Faucet: {amount} USDT0 получено! TX: {tx_hash}")
            await random_delay(5, 10)
            new_balance = await self.client.get_balance()
            new_available = self.client.parse_balance(new_balance.get("available", "0"))
            self.log.info(f"Faucet: новый баланс = {new_available:.2f} USDT0")
            return True

        # Ошибка API
        error = result.get("error", {})
        error_code = error.get("code", "")
        error_msg = error.get("message", "")

        if error_code == "FAUCET_LIMIT_EXCEEDED":
            remaining = self._parse_remaining_from_error(error_msg)
            if remaining > 100:
                self.log.info(f"Faucet: лимит превышен, пробуем получить остаток {remaining} USDT0")
                result2 = await self.client.faucet_mint(remaining)
                if result2 and result2.get("success"):
                    data2 = result2.get("data", {})
                    tx_hash2 = (
                        data2.get("txHash")
                        or data2.get("tx_hash")
                        or data2.get("transactionHash", "")
                    )
                    self.log.success(f"Faucet: {remaining} USDT0 получено (остаток лимита)! TX: {tx_hash2}")
                    await random_delay(5, 10)
                    new_balance = await self.client.get_balance()
                    new_available = self.client.parse_balance(new_balance.get("available", "0"))
                    self.log.info(f"Faucet: новый баланс = {new_available:.2f} USDT0")
                else:
                    self.log.warning(f"Faucet: не удалось получить остаток {remaining} USDT0")
            else:
                self.log.info(f"Faucet: дневной лимит исчерпан (остаток: {remaining} USDT0), пропускаем")
            # В обоих случаях — не ошибка, выходим
            raise _FaucetLimitExceeded()

        # Прочие ошибки API
        # Проверяем легаси-поля (success=False, error в data)
        data = result.get("data", result)
        legacy_msg = data.get("error") or data.get("message") or error_msg or str(data)
        if "already" in str(legacy_msg).lower() or "cooldown" in str(legacy_msg).lower():
            self.log.info("Faucet: похоже, уже получали токены недавно")
            raise _FaucetLimitExceeded()

        raise RuntimeError(f"API ошибка: {legacy_msg}")

    async def run(self) -> bool:
        """Запрашивает тестовые токены через /faucet/mint и проверяет баланс."""
        self.log.info("Faucet: начинаем получение тестовых токенов...")

        # 1. Проверяем текущий баланс
        balance_data = await self.client.get_balance()
        available_raw = balance_data.get("available", "0")
        available = self.client.parse_balance(available_raw)
        self.log.info(f"Faucet: текущий баланс {available:.2f} USDT0")

        # 2. Определяем сумму
        amount = random_int(*p.FAUCET_AMOUNT_RANGE)
        self.log.info(f"Faucet: запрашиваем {amount} USDT0...")

        # 3. Вызываем /faucet/mint с ретраями
        attempts = random_int(*p.RETRY_RANGE)
        last_exc = None

        for attempt in range(1, attempts + 1):
            try:
                return await self._try_mint(amount)

            except _FaucetLimitExceeded:
                # Лимит исчерпан или cooldown — не ошибка
                return True

            except Exception as e:
                last_exc = e
                self.log.warning(f"Faucet: attempt {attempt}/{attempts} — {e}")

            if attempt < attempts:
                wait = random_int(*p.RETRY_DELAY_RANGE)
                self.log.debug(f"Faucet: ждём {wait}s перед retry...")
                await asyncio.sleep(wait)

        self.log.error(f"Faucet: не удалось получить токены после {attempts} попыток: {last_exc}")
        return False


class _FaucetLimitExceeded(Exception):
    """Внутреннее исключение: лимит фаусета исчерпан, выходим без ошибки."""
