"""
Парсинг кошельков Outcome — баланс и активность.

Показывает для каждого кошелька:
  Баланс USDT0, открытые позиции, количество сделок, последняя активность
"""
import asyncio
from dataclasses import dataclass
from typing import Optional

import parameters as p
from core.account import Wallet
from core.outcome_client import OutcomeClient


@dataclass
class WalletStats:
    address:      str
    balance:      float = 0.0
    positions:    int   = 0
    open_orders:  int   = 0
    trades:       int   = 0
    last_activity: str  = "—"
    error:        Optional[str] = None


async def _fetch(wallet: Wallet, sem: asyncio.Semaphore) -> WalletStats:
    stats = WalletStats(address=wallet.address)
    async with sem:
        try:
            client = OutcomeClient(wallet)
            auth_ok = await client.authenticate()
            if not auth_ok:
                stats.error = "auth failed"
                return stats

            # Баланс
            bal_data = await client.get_balance()
            stats.balance = client.parse_balance(bal_data.get("available", "0"))

            # Позиции
            positions = await client.get_positions()
            stats.positions = len(positions) if positions else 0

            # Открытые ордера
            open_orders = await client.get_open_orders()
            stats.open_orders = len(open_orders) if open_orders else 0

            # История сделок — берём total из pagination, не len() страницы
            stats.trades = await client.get_trade_count()

            # Последняя активность — берём из первой записи истории
            history = await client.get_trade_history(page=1)

            # Последняя активность
            if history:
                first = history[0]
                ts = (first.get("time") or first.get("created_at") or
                      first.get("timestamp") or first.get("createdAt") or "")
                if ts:
                    stats.last_activity = str(ts)[:10]  # только дата

        except Exception as e:
            stats.error = str(e)[:60]

    return stats


async def run_stats(wallets: list[Wallet]) -> None:
    sem     = asyncio.Semaphore(3)
    tasks   = [_fetch(w, sem) for w in wallets]
    results: list[WalletStats] = await asyncio.gather(*tasks)

    col = {
        "num":       4,
        "addr":      16,
        "balance":   12,
        "positions": 10,
        "open_ord":  9,
        "trades":    8,
        "last":      12,
    }
    sep = "─" * (sum(col.values()) + len(col) * 3 + 1)

    def row(*cells):
        parts = []
        for val, width in zip(cells, col.values()):
            parts.append(str(val).rjust(width))
        return "  " + "   ".join(parts)

    print()
    print("=" * len(sep))
    print("  СТАТИСТИКА КОШЕЛЬКОВ OUTCOME")
    print("=" * len(sep))
    print(row("#", "Адрес", "USDT0", "Позиции", "Ордера", "Сделки", "Посл. акт."))
    print("  " + sep)

    total_balance     = 0.0
    total_positions   = 0
    total_open_orders = 0
    total_trades      = 0

    for i, s in enumerate(results, 1):
        short = f"{s.address[:6]}...{s.address[-4:]}"
        if s.error:
            print(row(i, short, "ERR", "ERR", "ERR", "ERR", "ERR"))
            print(f"     └─ {s.error}")
        else:
            bal_str = f"{s.balance:.2f}"
            print(row(i, short, bal_str, s.positions, s.open_orders, s.trades, s.last_activity))
            total_balance     += s.balance
            total_positions   += s.positions
            total_open_orders += s.open_orders
            total_trades      += s.trades

    print("  " + sep)
    print(row("∑", f"{len(results)} кош.", f"{total_balance:.2f}",
              total_positions, total_open_orders, total_trades, ""))
    print("=" * len(sep))
    print()
