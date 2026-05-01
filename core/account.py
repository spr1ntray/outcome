from dataclasses import dataclass, field
from typing import Optional
from eth_account import Account


@dataclass
class Wallet:
    private_key: str
    proxy: Optional[str] = None
    address: str = field(init=False)

    def __post_init__(self):
        acct = Account.from_key(self.private_key)
        self.address = acct.address

    @property
    def short_address(self) -> str:
        return f"{self.address[:6]}...{self.address[-4:]}"

    def get_current_proxy(self) -> Optional[str]:
        """Возвращает актуальную рабочую прокси через ProxyManager."""
        from core.proxy_manager import proxy_manager
        if proxy_manager is None:
            return self.proxy
        return proxy_manager.get_proxy(self.proxy)


def _normalize_proxy(proxy: str) -> str:
    """Добавляет http:// если протокол не указан."""
    if proxy and "://" not in proxy:
        return "http://" + proxy
    return proxy


def load_keys_from_file(filepath: str) -> list[str]:
    """Читает непустые строки из файла, игнорирует комментарии (#)."""
    import os
    if not os.path.exists(filepath):
        return []
    result = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                result.append(line)
    return result


def load_proxies_from_file(filepath: str) -> list[str]:
    """Читает прокси из файла, автоматически добавляет http:// если не указан."""
    lines = load_keys_from_file(filepath)
    return [_normalize_proxy(line) for line in lines]


def load_wallets(private_keys: list[str], proxies: list[str]) -> list[Wallet]:
    proxies = [_normalize_proxy(p) for p in proxies] if proxies else []
    wallets = []
    for i, pk in enumerate(private_keys):
        pk = pk.strip()
        if not pk:
            continue
        try:
            proxy = proxies[i % len(proxies)] if proxies else None
            wallets.append(Wallet(private_key=pk, proxy=proxy))
        except Exception as e:
            from loguru import logger
            logger.warning(f"Невалидный приватный ключ #{i+1}: {e}")
            continue
    return wallets
