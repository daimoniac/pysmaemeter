import asyncio
import logging
from typing import Dict, Optional

from sma_emeter.config import CONFIG

class SpeedwireCollector:
    """Speedwire fetcher with a reused asyncio event loop."""

    def __init__(self) -> None:
        self._timeout = CONFIG['speedwire']['timeout']
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    async def _fetch_async(self) -> Dict[str, int]:
        from lib.speedwire_multigate_asyncio import fetch_speedwire_data

        return await asyncio.wait_for(fetch_speedwire_data(), timeout=self._timeout)

    def fetch(self) -> Optional[Dict[str, int]]:
        loop = self._ensure_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self._fetch_async())
        except asyncio.TimeoutError:
            logging.error("Timeout fetching Speedwire data")
            return None
        except Exception:
            logging.exception("Error fetching Speedwire data")
            return None

    def close(self) -> None:
        if self._loop and not self._loop.is_closed():
            self._loop.close()
        self._loop = None
