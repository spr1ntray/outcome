"""
OUTCOME Testnet Bot
Запуск: python main.py
Настройки: parameters.py
API: https://api-testnet-v2.outcome.xyz/api/v1/outcome
Chain: Arbitrum Sepolia (421614)
"""
import asyncio
import os
import random
import sys

import parameters as p
from core.account import Wallet, load_wallets
from core.database import db_exists, save_db, unlock
from core.capsolver import CapSolverClient
from core.logger import setup_logger, get_logger
from core.outcome_client import OutcomeClient
from core.utils import random_delay

from modules.faucet         import FaucetModule
from modules.betting        import BettingModule
from modules.sell_positions import SellPositionsModule
from modules.wallet_stats   import run_stats

CAT_ART = r"""
      _.---.._             _.---...__
   .-'   /\   \          .'  /\     /
   `.   (  )   \        /   (  )   /
     `.  \/   .'\      /`.   \/  .'
       ``---''   )    (   ``---''
               .';.--.;`.
             .' /_...._\ `.
           .'   `.a  a.'   `.
          (        \/        )
           `.___..-'`-..___.`
              \          /
BY SPRINTRAY   `-.____.-'   ESPECIALLY FOR 🤍 MXW CHAT 福 USERS
"""

# ---------------------------------------------------------------------------
# Меню со стрелками
# ---------------------------------------------------------------------------

def _read_key() -> str:
    """Читает одно нажатие клавиши."""
    if sys.platform == "win32":
        import msvcrt
        ch = msvcrt.getch()
        if ch == b'\xe0' or ch == b'\x00':
            ch2 = msvcrt.getch()
            if ch2 == b'H': return 'up'
            if ch2 == b'P': return 'down'
            return 'esc'
        if ch in (b'\r', b'\n'): return 'enter'
        if ch == b'\x03': raise KeyboardInterrupt
        if ch == b'q':    return 'q'
        return ch.decode('utf-8', errors='ignore')
    else:
        import tty
        import termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.buffer.read(1)
            if ch == b'\x1b':
                ch2 = sys.stdin.buffer.read(1)
                ch3 = sys.stdin.buffer.read(1)
                if ch2 == b'[':
                    if ch3 == b'A': return 'up'
                    if ch3 == b'B': return 'down'
                return 'esc'
            if ch in (b'\r', b'\n'): return 'enter'
            if ch == b'\x03': raise KeyboardInterrupt
            if ch == b'q':    return 'q'
            return ch.decode('utf-8', errors='ignore')
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _confirm(question: str) -> bool:
    """Подтверждение стрелками ↑↓ + Enter. Возвращает True если выбрано «Да»."""
    options  = ["Да", "Нет"]
    selected = 0

    print(f"\n  {question}")
    print()
    for i, opt in enumerate(options):
        if i == selected:
            print(f"\033[36m  ▶  {opt}\033[0m")
        else:
            print(f"     {opt}")
    print()

    def _render():
        print(f"\033[{len(options) + 1}A", end="")
        for i, opt in enumerate(options):
            if i == selected:
                print(f"\033[36m  ▶  {opt}\033[0m\033[K")
            else:
                print(f"     {opt}\033[K")
        print()

    try:
        while True:
            key = _read_key()
            if key == 'up':
                selected = (selected - 1) % len(options)
                _render()
            elif key == 'down':
                selected = (selected + 1) % len(options)
                _render()
            elif key == 'enter':
                print(f"\n  Выбрано: {options[selected]}\n")
                return selected == 0
    except Exception:
        ans = input("  (y/n): ").strip().lower()
        return ans == "y"


def show_menu() -> dict:
    """Интерактивное меню со стрелками. Возвращает словарь флагов модулей."""
    options = [
        (
            "Создать базу данных",
            {"action": "create_db"},
        ),
        (
            "Полный цикл",
            {"faucet": True, "betting": True, "sell": True},
        ),
        (
            "Только активности (без пополнения)",
            {"faucet": False, "betting": True, "sell": True},
        ),
        (
            "Только пополнение",
            {"faucet": True, "betting": False, "sell": False},
        ),
        (
            "Продать все позиции (принудительно)",
            {"action": "force_sell"},
        ),
        (
            "Парсинг кошельков",
            {"action": "stats"},
        ),
    ]

    labels  = [o[0] for o in options]
    presets = [o[1] for o in options]
    n       = len(options)
    selected = 0

    print(CAT_ART)
    print("=" * 55)
    print("       OUTCOME TESTNET BOT")
    print("=" * 55)
    print("  Стрелки ↑↓ для выбора, Enter для запуска\n")

    for i, label in enumerate(labels):
        if i == selected:
            print(f"\033[36m  ▶  {label}\033[0m")
        else:
            print(f"     {label}")
    print()

    def _render():
        print(f"\033[{n + 1}A", end="")
        for i, label in enumerate(labels):
            if i == selected:
                print(f"\033[36m  ▶  {label}\033[0m\033[K")
            else:
                print(f"     {label}\033[K")
        print()

    try:
        while True:
            key = _read_key()
            if key == 'up':
                selected = (selected - 1) % n
                _render()
            elif key == 'down':
                selected = (selected + 1) % n
                _render()
            elif key == 'enter':
                print(f"\n  Выбрано: {labels[selected]}\n")
                return presets[selected]
    except Exception:
        choice = input("Введи номер (1-6): ").strip()
        idx = int(choice) - 1 if choice.isdigit() and 1 <= int(choice) <= n else 0
        return presets[idx]


# ---------------------------------------------------------------------------
# Создание базы данных
# ---------------------------------------------------------------------------

def _read_lines_from_file(filepath: str) -> list[str]:
    from pathlib import Path
    path = Path(filepath)
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text("utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")]


def create_database():
    """Читает txt-файлы, создаёт зашифрованную базу данных, очищает файлы."""
    from getpass import getpass
    from pathlib import Path

    print("=" * 55)
    print("  СОЗДАНИЕ БАЗЫ ДАННЫХ")
    print("=" * 55)
    print()

    private_keys = _read_lines_from_file("input/private_keys.txt")
    proxies      = _read_lines_from_file("input/proxies.txt")

    if not private_keys:
        print("  Ошибка: input/private_keys.txt пуст или не найден.")
        print("  Добавь приватные ключи в файл и повтори.\n")
        return

    print(f"  Найдено ключей: {len(private_keys)}")
    print(f"  Найдено прокси: {len(proxies)}")
    print()

    if db_exists():
        if not _confirm("  База уже существует. Перезаписать?"):
            print("  Отмена.\n")
            return
        print()

    while True:
        pwd  = getpass("  Придумай пароль: ")
        pwd2 = getpass("  Повтори пароль:  ")
        if not pwd:
            print("  Пароль не может быть пустым.\n")
            continue
        if pwd != pwd2:
            print("  Пароли не совпадают. Попробуй снова.\n")
            continue
        break

    save_db(pwd, private_keys, proxies)

    # Очищаем txt-файлы
    Path("input/private_keys.txt").write_text("")
    Path("input/proxies.txt").write_text("")

    print()
    print("  База данных создана: input/database.enc")
    print("  Файлы private_keys.txt и proxies.txt очищены.\n")


# ---------------------------------------------------------------------------
# Обработка одного кошелька
# ---------------------------------------------------------------------------

async def process_wallet(
    wallet: Wallet,
    sem: asyncio.Semaphore,
    capsolver: CapSolverClient,
    *,
    run_faucet: bool,
    run_betting: bool,
    run_sell: bool,
):
    """Обрабатывает один кошелёк — последовательно запускает выбранные модули."""
    log = get_logger(wallet.address)

    async with sem:
        # Рандомная задержка перед стартом (анти-бан, randomization)
        await random_delay(*p.WALLET_DELAY_RANGE)
        log.info(f"Начинаем обработку | proxy: {wallet.proxy or 'нет'}")

        # --- Авторизация (SIWE → cookie) ---
        client = OutcomeClient(wallet)

        # Авторизация нужна для всех модулей (фаусет тоже через API)
        auth_ok = await client.authenticate()
        if not auth_ok:
            log.error("Авторизация не удалась, пропускаем кошелёк")
            return
        await random_delay(2, 5)

        # --- Фаусет ---
        if run_faucet:
            try:
                faucet = FaucetModule(wallet, outcome_client=client)
                await faucet.run()
            except Exception as e:
                log.error(f"Faucet: критическая ошибка: {e}")
            await random_delay(*p.POST_FAUCET_DELAY)

        # --- Ставки ---
        placed_orders = []
        if run_betting:
            try:
                betting = BettingModule(wallet, client)
                placed_orders = await betting.run()
            except Exception as e:
                log.error(f"Betting: критическая ошибка: {e}")
            await random_delay(*p.POST_BET_DELAY)

        # --- Продажа позиций ---
        if run_sell:
            try:
                sell = SellPositionsModule(wallet, client)
                await sell.run(placed_orders)
            except Exception as e:
                log.error(f"Sell: критическая ошибка: {e}")

    log.success("Кошелёк обработан полностью")


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main():
    os.makedirs("logs",  exist_ok=True)
    os.makedirs("input", exist_ok=True)
    setup_logger()
    log = get_logger("MAIN")

    selected = show_menu()

    # --- Создание базы данных ---
    if selected.get("action") == "create_db":
        create_database()
        return

    # --- Остальные режимы требуют базу данных ---
    if not db_exists():
        print("  База данных не найдена.")
        print("  Выбери 'Создать базу данных' в меню.\n")
        return

    private_keys, proxies = unlock()
    wallets = load_wallets(private_keys, proxies)

    if not wallets:
        log.error("Нет кошельков в базе данных.")
        return

    # --- Парсинг кошельков ---
    if selected.get("action") == "stats":
        log.info(f"Парсим {len(wallets)} кошельков...")
        await run_stats(wallets)
        return

    # --- Принудительная продажа всех позиций ---
    if selected.get("action") == "force_sell":
        log.info("=" * 60)
        log.info("OUTCOME: Принудительная продажа позиций")
        log.info("=" * 60)
        log.info(f"Загружено кошельков: {len(wallets)}")

        from core import proxy_manager as pm_module
        if proxies:
            pm_module.proxy_manager = pm_module.ProxyManager(proxies)
            log.info(f"ProxyManager: {len(proxies)} прокси загружено")

        wallet_sem = asyncio.Semaphore(p.THREADS)

        async def _force_sell_wallet(wallet: Wallet):
            wlog = get_logger(wallet.address)
            async with wallet_sem:
                await random_delay(*p.WALLET_DELAY_RANGE)
                wlog.info(f"ForceSell | proxy: {wallet.proxy or 'нет'}")
                client = OutcomeClient(wallet)
                auth_ok = await client.authenticate()
                if not auth_ok:
                    wlog.error("Авторизация не удалась")
                    return
                sell = SellPositionsModule(wallet, client)
                await sell.force_sell_all()
            wlog.success("Кошелёк обработан")

        tasks = [_force_sell_wallet(w) for w in wallets]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        success = sum(1 for r in results if not isinstance(r, Exception))
        log.info("=" * 60)
        log.success(f"ForceSell завершён | успешно: {success}/{len(wallets)}")
        log.info("=" * 60)
        return

    run_faucet  = selected.get("faucet",  p.ENABLE_FAUCET)
    run_betting = selected.get("betting", p.ENABLE_BETTING)
    run_sell    = selected.get("sell",    p.ENABLE_SELL)

    log.info("=" * 60)
    log.info("OUTCOME Testnet Bot запущен")
    log.info(
        f"Модули: faucet={run_faucet} betting={run_betting} sell={run_sell}"
    )
    log.info(f"API: {p.OUTCOME_API_BASE}")
    log.info(f"Chain: Arbitrum Sepolia (ID {p.CHAIN_ID})")
    log.info("=" * 60)

    log.info(f"Загружено: {len(wallets)} кошельков, {len(proxies)} прокси")

    from core import proxy_manager as pm_module
    if proxies:
        pm_module.proxy_manager = pm_module.ProxyManager(proxies)
        if getattr(p, "CHECK_PROXIES_ON_START", False):
            await pm_module.proxy_manager.check_all()
        else:
            log.info(f"ProxyManager: {len(proxies)} прокси загружено")
    else:
        log.info("Прокси не заданы, работаем без прокси")

    if not wallets:
        log.error("Нет кошельков для обработки!")
        return

    if p.SHUFFLE_WALLETS:
        random.shuffle(wallets)
        log.info("Порядок кошельков перемешан")

    capsolver = CapSolverClient(
        api_key=p.CAPSOLVER_API_KEY,
        proxy=wallets[0].proxy if wallets else None,
    )

    if p.CAPSOLVER_API_KEY:
        try:
            balance = await capsolver.get_balance()
            log.info(f"CapSolver баланс: ${balance:.4f}")
        except Exception as e:
            log.warning(f"Не удалось проверить баланс CapSolver: {e}")

    wallet_sem = asyncio.Semaphore(p.THREADS)

    total = len(wallets)
    log.info(f"Запускаем обработку {total} кошельков | потоки: {p.THREADS}")

    tasks = [
        process_wallet(
            wallet, wallet_sem, capsolver,
            run_faucet=run_faucet,
            run_betting=run_betting,
            run_sell=run_sell,
        )
        for wallet in wallets
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    success = sum(1 for r in results if not isinstance(r, Exception))
    failed  = total - success

    log.info("=" * 60)
    log.success(f"Обработка завершена | успешно: {success}/{total} | ошибок: {failed}/{total}")
    log.info("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nПрервано пользователем (Ctrl+C)")
