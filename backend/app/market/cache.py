import asyncio
from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory store for the latest price of every tracked ticker.

    The background market data task writes here; the SSE endpoint and API
    routes read here. Reads are plain dict lookups — no locking needed because
    CPython's GIL makes dict reads atomic. Writes use a lock to prevent
    torn writes during bulk updates.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = asyncio.Lock()
        self._subscribers: list[asyncio.Queue] = []

    async def update(self, updates: list[PriceUpdate]) -> None:
        async with self._lock:
            for u in updates:
                self._prices[u.ticker] = u
        for queue in self._subscribers:
            for u in updates:
                try:
                    queue.put_nowait(u)
                except asyncio.QueueFull:
                    pass  # slow consumer: drop oldest by draining one then re-putting
                    try:
                        queue.get_nowait()
                        queue.put_nowait(u)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass

    def get(self, ticker: str) -> PriceUpdate | None:
        return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        return dict(self._prices)

    def get_tickers(self) -> list[str]:
        return list(self._prices.keys())

    def subscribe(self) -> asyncio.Queue:
        """Return a queue that receives every PriceUpdate as it arrives.

        Used by the SSE endpoint to fan out updates to connected clients.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass
