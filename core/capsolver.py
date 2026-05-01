import asyncio
import httpx
from typing import Optional
from core.logger import get_logger

CAPSOLVER_URL = "https://api.capsolver.com"

log = get_logger("CAPSOLVER")


class CapSolverClient:
    def __init__(self, api_key: str, proxy: Optional[str] = None):
        self.api_key = api_key
        self.proxy = proxy

    async def solve_turnstile(self, website_url: str, website_key: str) -> str:
        """Решает Cloudflare Turnstile. Возвращает cf-token."""
        task = {
            "type": "AntiTurnstileTask",
            "websiteURL": website_url,
            "websiteKey": website_key,
        }
        if self.proxy:
            task["proxy"] = self.proxy

        task_id = await self._create_task(task)
        return await self._poll_result(task_id)

    async def solve_turnstile_proxyless(self, website_url: str, website_key: str) -> str:
        """Решает Cloudflare Turnstile без прокси."""
        task = {
            "type": "AntiTurnstileTaskProxyLess",
            "websiteURL": website_url,
            "websiteKey": website_key,
        }
        task_id = await self._create_task(task)
        return await self._poll_result(task_id)

    async def solve_hcaptcha(self, website_url: str, website_key: str) -> str:
        """Решает hCaptcha."""
        task = {
            "type": "HCaptchaTaskProxyLess",
            "websiteURL": website_url,
            "websiteKey": website_key,
        }
        task_id = await self._create_task(task)
        return await self._poll_result(task_id)

    async def _create_task(self, task: dict) -> str:
        payload = {"clientKey": self.api_key, "task": task}
        async with httpx.AsyncClient(timeout=30, proxy=self.proxy) as client:
            resp = await client.post(f"{CAPSOLVER_URL}/createTask", json=payload)
            resp.raise_for_status()
            data = resp.json()

        if data.get("errorId", 0) != 0:
            raise RuntimeError(f"CapSolver createTask error: {data.get('errorDescription')}")

        return data["taskId"]

    async def _poll_result(self, task_id: str, max_wait: int = 120) -> str:
        payload = {"clientKey": self.api_key, "taskId": task_id}
        elapsed = 0
        async with httpx.AsyncClient(timeout=30, proxy=self.proxy) as client:
            while elapsed < max_wait:
                await asyncio.sleep(5)
                elapsed += 5

                resp = await client.post(f"{CAPSOLVER_URL}/getTaskResult", json=payload)
                resp.raise_for_status()
                data = resp.json()

                if data.get("errorId", 0) != 0:
                    raise RuntimeError(f"CapSolver getTaskResult error: {data.get('errorDescription')}")

                status = data.get("status")
                if status == "ready":
                    solution = data.get("solution", {})
                    token = solution.get("token") or solution.get("userAgent")
                    if token:
                        return token
                    raise RuntimeError(f"CapSolver: no token in solution: {solution}")

                if status == "failed":
                    raise RuntimeError("CapSolver: task failed")

        raise TimeoutError(f"CapSolver: task {task_id} not completed in {max_wait}s")

    async def get_balance(self) -> float:
        """Проверяет баланс CapSolver аккаунта."""
        async with httpx.AsyncClient(timeout=15, proxy=self.proxy) as client:
            resp = await client.post(
                f"{CAPSOLVER_URL}/getBalance",
                json={"clientKey": self.api_key},
            )
            resp.raise_for_status()
            data = resp.json()
        return data.get("balance", 0.0)
