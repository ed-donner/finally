# マーケットデータインターフェース設計

FinAlly のマーケットデータ用 Python 統一インターフェース。2 つの実装（シミュレーターと Massive API）を 1 つの抽象インターフェースの背後に配置。SSE ストリーミング、価格キャッシュ、ポートフォリオ評価など、すべての下流コードはデータソースに依存しません。

## コアデータモデル

```python
from dataclasses import dataclass

@dataclass
class PriceUpdate:
    """単一ティッカーの価格更新。"""
    ticker: str
    price: float
    previous_price: float
    timestamp: float          # Unix 秒
    change: float             # price - previous_price
    direction: str            # "up"、"down"、または "flat"
```

これはマーケットデータレイヤーから外に出る唯一のデータ構造です。すべての下流コードは `PriceUpdate` オブジェクトで動作します。

## 抽象インターフェース

```python
from abc import ABC, abstractmethod

class MarketDataSource(ABC):
    """マーケットデータプロバイダーの抽象インターフェース。"""

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """指定されたティッカーの価格更新の生成を開始する。"""

    @abstractmethod
    async def stop(self) -> None:
        """価格更新の生成を停止してクリーンアップを行う。"""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """アクティブセットにティッカーを追加する。"""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """アクティブセットからティッカーを削除する。"""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """現在アクティブなティッカーのリストを返す。"""
```

両実装は共有の `PriceCache` に書き込みます（以下参照）。インターフェースは価格を直接返しません — 独自のスケジュールでキャッシュに更新をプッシュします。

## 価格キャッシュ

両データソースが書き込み、SSE ストリーマーが読み取る共有インメモリストア。

```python
import time
from threading import Lock

class PriceCache:
    """ティッカーごとの最新価格のスレッドセーフキャッシュ。"""

    def __init__(self):
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """ティッカーの価格を更新する。PriceUpdate を返す。"""
        with self._lock:
            ts = timestamp or time.time()
            previous = self._prices.get(ticker)
            previous_price = previous.price if previous else price

            if price > previous_price:
                direction = "up"
            elif price < previous_price:
                direction = "down"
            else:
                direction = "flat"

            update = PriceUpdate(
                ticker=ticker,
                price=price,
                previous_price=previous_price,
                timestamp=ts,
                change=price - previous_price,
                direction=direction,
            )
            self._prices[ticker] = update
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        """ティッカーの最新価格を取得する。"""
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """すべての現在の価格を取得する。"""
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        """キャッシュからティッカーを削除する。"""
        with self._lock:
            self._prices.pop(ticker, None)
```

## ファクトリー関数

起動時に環境に基づいてデータソースを選択:

```python
import os

def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """環境に基づいて適切なマーケットデータソースを作成する。"""
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveDataSource
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        from .simulator import SimulatorDataSource
        return SimulatorDataSource(price_cache=price_cache)
```

## Massive 実装スケッチ

```python
import asyncio
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

class MassiveDataSource(MarketDataSource):
    def __init__(self, api_key: str, price_cache: PriceCache, poll_interval: float = 15.0):
        self._client = RESTClient(api_key=api_key)
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._tickers = list(tickers)
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def add_ticker(self, ticker: str) -> None:
        if ticker not in self._tickers:
            self._tickers.append(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _poll_loop(self) -> None:
        while True:
            await self._poll_once()
            await asyncio.sleep(self._interval)

    async def _poll_once(self) -> None:
        if not self._tickers:
            return
        # 同期的な Massive クライアントをスレッドプールで実行
        snapshots = await asyncio.to_thread(
            self._client.get_snapshot_all,
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
        for snap in snapshots:
            self._cache.update(
                ticker=snap.ticker,
                price=snap.last_trade.price,
                timestamp=snap.last_trade.timestamp / 1000,  # ms -> 秒
            )
```

## シミュレーター実装スケッチ

```python
import asyncio

class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5):
        self._cache = price_cache
        self._interval = update_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._sim: GBMSimulator | None = None  # MARKET_SIMULATOR.md を参照

    async def start(self, tickers: list[str]) -> None:
        self._tickers = list(tickers)
        self._sim = GBMSimulator(tickers=self._tickers)
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def add_ticker(self, ticker: str) -> None:
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            self._sim.add_ticker(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers = [t for t in self._tickers if t != ticker]
        self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _run_loop(self) -> None:
        while True:
            prices = self._sim.step()  # dict[str, float] を返す
            for ticker, price in prices.items():
                self._cache.update(ticker=ticker, price=price)
            await asyncio.sleep(self._interval)
```

## SSE との統合

SSE エンドポイントは `PriceCache` から読み取り、接続されたクライアントにプッシュします:

```python
async def price_stream(price_cache: PriceCache):
    """価格更新を yield する SSE ジェネレーター。"""
    while True:
        prices = price_cache.get_all()
        data = {
            ticker: {
                "ticker": p.ticker,
                "price": p.price,
                "previous_price": p.previous_price,
                "change": p.change,
                "direction": p.direction,
                "timestamp": p.timestamp,
            }
            for ticker, p in prices.items()
        }
        yield f"data: {json.dumps(data)}\n\n"
        await asyncio.sleep(0.5)
```

## ファイル構造

```
backend/
  app/
    market/
      __init__.py
      models.py             # PriceUpdate データクラス
      interface.py           # MarketDataSource ABC、PriceCache
      factory.py             # create_market_data_source()
      massive_client.py      # MassiveDataSource
      simulator.py           # SimulatorDataSource + GBMSimulator
      seed_prices.py         # デフォルトティッカーシード価格
```

## ライフサイクル

1. **アプリ起動**: `PriceCache` を作成し、`create_market_data_source(price_cache)` を呼び出し、次に `await source.start(initial_tickers)`
2. **ウォッチリスト変更**: `source.add_ticker()` または `source.remove_ticker()` を呼び出す
3. **SSE ストリーミング**: 500ms ごとに `PriceCache.get_all()` から読み取る
4. **取引実行**: `PriceCache.get(ticker)` から現在価格を読み取る
5. **アプリ終了**: `await source.stop()` を呼び出す
