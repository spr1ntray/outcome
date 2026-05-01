"""
Менеджер прокси с автоматической заменой нерабочих.
"""
import asyncio
import httpx
from typing import Optional
from loguru import logger as _base_logger

log = _base_logger.bind(wallet="PROXY-MGR")

PROXY_CHECK_URL = "https://httpbin.org/ip"
PROXY_CHECK_TIMEOUT = 10


class ProxyManager:
    def __init__(self, proxies: list[str]):
        self._all_proxies = list(proxies)
        self._working: list[str] = list(proxies)
        self._failed: set[str] = set()
        self._lock = asyncio.Lock()

    def get_proxy(self, preferred: Optional[str] = None) -> Optional[str]:
        """Возвращает рабочую прокси. Если preferred нерабочая — берёт следующую."""
        if preferred and preferred not in self._failed:
            return preferred
        working = [p for p in self._all_proxies if p not in self._failed]
        return working[0] if working else None

    async def mark_failed(self, proxy: str):
        """Помечает прокси как нерабочую."""
        async with self._lock:
            if proxy not in self._failed:
                self._failed.add(proxy)
                working_count = len(self._all_proxies) - len(self._failed)
                log.warning(
                    f"ProxyManager: прокси {proxy[:30]}... помечена нерабочей. "
                    f"Рабочих: {working_count}/{len(self._all_proxies)}"
                )

    async def get_next_working(self, current: Optional[str] = None) -> Optional[str]:
        """Возвращает следующую рабочую прокси (не текущую)."""
        candidates = [p for p in self._all_proxies if p not in self._failed and p != current]
        return candidates[0] if candidates else None

    async def check_proxy(self, proxy: str) -> bool:
        """Проверяет доступность прокси."""
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=PROXY_CHECK_TIMEOUT) as client:
                resp = await client.get(PROXY_CHECK_URL)
                return resp.status_code == 200
        except Exception:
            return False

    async def check_all(self):
        """Проверяет все прокси при старте."""
        log.info(f"ProxyManager: проверяем {len(self._all_proxies)} прокси...")
        tasks = [self._check_and_mark(p) for p in self._all_proxies]
        await asyncio.gather(*tasks)
        working = len(self._all_proxies) - len(self._failed)
        log.info(f"ProxyManager: рабочих {working}/{len(self._all_proxies)}")

    async def _check_and_mark(self, proxy: str):
        ok = await self.check_proxy(proxy)
        if not ok:
            await self.mark_failed(proxy)

    @property
    def has_working(self) -> bool:
        return len(self._failed) < len(self._all_proxies)

    @property
    def working_count(self) -> int:
        return len(self._all_proxies) - len(self._failed)


# Глобальный экземпляр (инициализируется в main)
proxy_manager: Optional[ProxyManager] = None
