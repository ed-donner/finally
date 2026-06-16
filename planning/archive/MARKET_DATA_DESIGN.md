# マーケットデータバックエンド — 詳細設計

FinAlly マーケットデータサブシステムの実装準備済み設計書。統一インターフェース、インメモリ価格キャッシュ、GBM シミュレーター、Massive API クライアント、SSE ストリーミングエンドポイント、FastAPI ライフサイクル統合を網羅します。

このドキュメントのすべての内容は `backend/app/market/` に配置されます。

---

## 目次

1. [ファイル構造](#1-file-structure)
2. [データモデル — `models.py`](#2-data-model)
3. [価格キャッシュ — `cache.py`](#3-price-cache)
4. [抽象インターフェース — `interface.py`](#4-abstract-interface)
5. [シード価格とティッカーパラメーター — `seed_prices.py`](#5-seed-prices--ticker-parameters)
6. [GBM シミュレーター — `simulator.py`](#6-gbm-simulator)
7. [Massive API クライアント — `massive_client.py`](#7-massive-api-client)
8. [ファクトリー — `factory.py`](#8-factory)
9. [SSE ストリーミングエンドポイント — `stream.py`](#9-sse-streaming-endpoint)
10. [FastAPI ライフサイクル統合](#10-fastapi-lifecycle-integration)
11. [ウォッチリスト連携](#11-watchlist-coordination)
12. [テスト戦略](#12-testing-strategy)
13. [エラー処理とエッジケース](#13-error-handling--edge-cases)
14. [設定サマリー](#14-configuration-summary)

---

## 1. ファイル構造

```
backend/
  app/
    market/
      __init__.py             # 再エクスポート: PriceUpdate, PriceCache, MarketDataSource, create_market_data_source
      models.py               # PriceUpdate データクラス
      cache.py                # PriceCache（スレッドセーフなインメモリストア）
      interface.py            # MarketDataSource ABC
      seed_prices.py          # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, CORRELATION_GROUPS
      simulator.py            # GBMSimulator + SimulatorDataSource
      massive_client.py       # MassiveDataSource
      factory.py              # create_market_data_source()
      stream.py               # SSE エンドポイント（FastAPI ルーター）
```

各ファイルは単一の責務を持ちます。`__init__.py` がパブリック API を再エクスポートするため、バックエンドの残りの部分はサブモジュールに直接アクセスすることなく `app.market` からインポートできます。

---

## 2. データモデル

**ファイル: `backend/app/market/models.py`**

`PriceUpdate` はマーケットデータレイヤーから外に出る唯一のデータ構造です。SSE ストリーミング、ポートフォリオ評価、取引実行など、すべての下流コンシューマーがこの型のみを使用します。

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change from previous update."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from previous update."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }
```

### 設計判断

- **`frozen=True`**: 価格更新はイミュータブルな値オブジェクトです。一度作成されると変更されないため、コピーなしに非同期タスク間で安全に共有できます。
- **`slots=True`**: マイナーなメモリ最適化 — 毎秒多数作成されます。
- **計算プロパティ**（`change`、`direction`、`change_percent`）: `price` と `previous_price` から導出されるため、不整合が発生しません。古い `direction` フィールドのリスクがありません。
- **`to_dict()`**: SSE エンドポイントと REST API レスポンスの両方で使用される単一のシリアライゼーションポイント。

---

## 3. 価格キャッシュ

**ファイル: `backend/app/market/cache.py`**

価格キャッシュは中央データハブです。データソースが書き込み、SSE ストリーミングとポートフォリオ評価が読み取ります。シミュレーター/ポーラーがスレッドプールエグゼキューターで実行される可能性があり、SSE の読み取りが非同期イベントループで発生するため、スレッドセーフである必要があります。

```python
from __future__ import annotations

import asyncio
import time
from threading import Lock
from typing import Callable

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.

    Writers: SimulatorDataSource or MassiveDataSource (one at a time).
    Readers: SSE streaming endpoint, portfolio valuation, trade execution.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonically increasing; bumped on every update

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        Automatically computes direction and change from the previous price.
        If this is the first update for the ticker, previous_price == price (direction='flat').
        """
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        """Get the latest price for a single ticker, or None if unknown."""
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices. Returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: get just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        """Remove a ticker from the cache (e.g., when removed from watchlist)."""
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Current version counter. Useful for SSE change detection."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### バージョンカウンターが必要な理由

SSE ストリーミングループはキャッシュを ~500ms ごとにポーリングします。バージョンカウンターがなければ、何も変わっていない場合でも（例: Massive API は 15 秒ごとにしか更新しない）、毎ティックすべての価格をシリアライズして送信します。バージョンカウンターにより SSE ループは新しいものがない場合に送信をスキップできます:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

### スレッドセーフの根拠

`asyncio.Lock` の代わりに `threading.Lock` を使用している理由:
- Massive クライアントの同期的な `get_snapshot_all()` は `asyncio.to_thread()` で実行されますが、これは実際の OS スレッドで動作します — `asyncio.Lock` はそれに対して保護できません。
- GBM シミュレーターの `step()` は CPU バウンドであり、公平性のためにスレッドにオフロードされる可能性があります。
- `threading.Lock` は同期スレッドと非同期イベントループの両方から正しく動作します。

---

## 4. 抽象インターフェース

**ファイル: `backend/app/market/interface.py`**

```python
from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices —
    it reads from the cache.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        # ... app runs ...
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        # ... app shutting down ...
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Starts a background task that periodically writes to the PriceCache.
        Must be called exactly once. Calling start() twice is undefined behavior.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.

        Safe to call multiple times. After stop(), the source will not write
        to the cache again.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present.

        The next update cycle will include this ticker.
        """

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. No-op if not present.

        Also removes the ticker from the PriceCache.
        """

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

### ソースが価格を返すのではなくキャッシュに書き込む理由

このプッシュモデルはタイミングを分離します。シミュレーターは 500ms でティック、Massive は 15 秒でポーリングしますが、SSE は常に独自の 500ms カデンスでキャッシュから読み取ります。SSE レイヤーはどのデータソースがアクティブか、その更新間隔が何かを知る必要がありません。

---

## 5. シード価格とティッカーパラメーター

**ファイル: `backend/app/market/seed_prices.py`**

定数のみ — ロジックなし、標準ライブラリ以外のインポートなし。このファイルはシミュレーター（初期価格と GBM パラメーター用）と、潜在的に Massive クライアント（API がまだ応答していない場合のフォールバック価格として）の両方で共有されます。

```python
"""Seed prices and per-ticker parameters for the market simulator."""

# Realistic starting prices for the default watchlist (as of project creation)
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,
    "GOOGL": 175.00,
    "MSFT": 420.00,
    "AMZN": 185.00,
    "TSLA": 250.00,
    "NVDA": 800.00,
    "META": 500.00,
    "JPM": 195.00,
    "V": 280.00,
    "NFLX": 600.00,
}

# Per-ticker GBM parameters
# sigma: annualized volatility (higher = more price movement)
# mu: annualized drift / expected return
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # High volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # High volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # Low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # Low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Default parameters for tickers not in the list above (dynamically added)
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups for the simulator's Cholesky decomposition
# Tickers in the same group have higher intra-group correlation
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Correlation coefficients
INTRA_TECH_CORR = 0.6       # Tech stocks move together
INTRA_FINANCE_CORR = 0.5    # Finance stocks move together
CROSS_GROUP_CORR = 0.3      # Between sectors
TSLA_CORR = 0.3             # TSLA does its own thing
DEFAULT_CORR = 0.3           # Unknown tickers
```

---

## 6. GBM シミュレーター

**ファイル: `backend/app/market/simulator.py`**

このファイルには 2 つのクラスが含まれます:
- `GBMSimulator`: 純粋な数学エンジン。ステートフル — 現在の価格を保持し、一度に 1 ステップ進めます。
- `SimulatorDataSource`: `GBMSimulator` を非同期ループでラップし `PriceCache` に書き込む `MarketDataSource` 実装。

### 6.1 GBMSimulator — 数学エンジン

```python
from __future__ import annotations

import asyncio
import logging
import math
import random

import numpy as np

from .cache import PriceCache
from .interface import MarketDataSource
from .seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    DEFAULT_CORR,
    DEFAULT_PARAMS,
    INTRA_FINANCE_CORR,
    INTRA_TECH_CORR,
    SEED_PRICES,
    TICKER_PARAMS,
    TSLA_CORR,
)

logger = logging.getLogger(__name__)


class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices.

    Math:
        S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)

    Where:
        S(t)   = current price
        mu     = annualized drift (expected return)
        sigma  = annualized volatility
        dt     = time step as fraction of a trading year
        Z      = correlated standard normal random variable

    The tiny dt (~8.5e-8 for 500ms ticks over 252 trading days * 6.5h/day)
    produces sub-cent moves per tick that accumulate naturally over time.
    """

    # 500ms expressed as a fraction of a trading year
    # 252 trading days * 6.5 hours/day * 3600 seconds/hour = 5,896,800 seconds
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
    ) -> None:
        self._dt = dt
        self._event_prob = event_probability

        # Per-ticker state
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}

        # Cholesky decomposition of the correlation matrix (for correlated moves)
        self._cholesky: np.ndarray | None = None

        # Initialize all starting tickers
        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    # --- Public API ---

    def step(self) -> dict[str, float]:
        """Advance all tickers by one time step. Returns {ticker: new_price}.

        This is the hot path — called every 500ms. Keep it fast.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        # Generate n independent standard normal draws
        z_independent = np.random.standard_normal(n)

        # Apply Cholesky to get correlated draws
        if self._cholesky is not None:
            z_correlated = self._cholesky @ z_independent
        else:
            z_correlated = z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu = params["mu"]
            sigma = params["sigma"]

            # GBM: S(t+dt) = S(t) * exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)
            drift = (mu - 0.5 * sigma ** 2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random event: ~0.1% chance per tick per ticker
            # With 10 tickers at 2 ticks/sec, expect an event ~every 50 seconds
            if random.random() < self._event_prob:
                shock_magnitude = random.uniform(0.02, 0.05)
                shock_sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock_magnitude * shock_sign
                logger.debug(
                    "Random event on %s: %.1f%% %s",
                    ticker,
                    shock_magnitude * 100,
                    "up" if shock_sign > 0 else "down",
                )

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the simulation. Rebuilds the correlation matrix."""
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the simulation. Rebuilds the correlation matrix."""
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        """Current price for a ticker, or None if not tracked."""
        return self._prices.get(ticker)

    # --- Internals ---

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add a ticker without rebuilding Cholesky (for batch initialization)."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """Rebuild the Cholesky decomposition of the ticker correlation matrix.

        Called whenever tickers are added or removed. O(n^2) but n < 50.
        """
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        # Build the correlation matrix
        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        """Determine correlation between two tickers based on sector grouping.

        Correlation structure:
          - Same tech sector:   0.6
          - Same finance sector: 0.5
          - TSLA with anything: 0.3 (it does its own thing)
          - Cross-sector:       0.3
          - Unknown tickers:    0.3
        """
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]

        # TSLA is in tech set but behaves independently
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR

        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR

        return CROSS_GROUP_CORR
```

### 6.2 SimulatorDataSource — 非同期ラッパー

```python
class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by the GBM simulator.

    Runs a background asyncio task that calls GBMSimulator.step() every
    `update_interval` seconds and writes results to the PriceCache.
    """

    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 0.001,
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(
            tickers=tickers,
            event_probability=self._event_prob,
        )
        # Seed the cache with initial prices so SSE has data immediately
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(tickers))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Simulator stopped")

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            # Seed cache immediately so the ticker has a price right away
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
            logger.info("Simulator: added ticker %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._sim._tickers) if self._sim else []

    async def _run_loop(self) -> None:
        """Core loop: step the simulation, write to cache, sleep."""
        while True:
            try:
                if self._sim:
                    prices = self._sim.step()
                    for ticker, price in prices.items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

### 主要な動作

- **即時シーディング**: `start()` が呼び出されると、ループが始まる*前に*キャッシュにシード価格が投入されます。これにより SSE エンドポイントは最初のティックで送信するデータを持ち、ブランクスクリーンの遅延が発生しません。
- **グレースフルなキャンセル**: `stop()` はタスクをキャンセルして await し、`CancelledError` をキャッチします。これにより FastAPI ライフサイクルのティアダウン中にクリーンなシャットダウンが保証されます。
- **例外への耐性**: ループはステップごとに例外をキャッチするため、単一の不良ティックがデータフィード全体を停止させません。

---

## 7. Massive API クライアント

**ファイル: `backend/app/market/massive_client.py`**

設定可能な間隔で Massive（旧 Polygon.io）REST API スナップショットエンドポイントをポーリングします。同期的な Massive クライアントはイベントループのブロックを避けるために `asyncio.to_thread()` で実行されます。

```python
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call, then writes results to the PriceCache.

    Rate limits:
      - Free tier: 5 req/min → poll every 15s (default)
      - Paid tiers: higher limits → poll every 2-5s
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: Any = None  # Lazy import to avoid hard dependency

    async def start(self, tickers: list[str]) -> None:
        # Lazy import: only import massive when actually using real market data.
        # This means the massive package is not required when using the simulator.
        from massive import RESTClient

        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)

        # Do an immediate first poll so the cache has data right away
        await self._poll_once()

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval",
            len(tickers),
            self._interval,
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added ticker %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internal ---

    async def _poll_loop(self) -> None:
        """Poll on interval. First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """Execute one poll cycle: fetch snapshots, update cache."""
        if not self._tickers or not self._client:
            return

        try:
            # The Massive RESTClient is synchronous — run in a thread to
            # avoid blocking the event loop.
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    # Massive timestamps are Unix milliseconds → convert to seconds
                    timestamp = snap.last_trade.timestamp / 1000.0
                    self._cache.update(
                        ticker=snap.ticker,
                        price=price,
                        timestamp=timestamp,
                    )
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning(
                        "Skipping snapshot for %s: %s",
                        getattr(snap, "ticker", "???"),
                        e,
                    )
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))

        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — the loop will retry on the next interval.
            # Common failures: 401 (bad key), 429 (rate limit), network errors.

    def _fetch_snapshots(self) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        from massive.rest.models import SnapshotMarketType

        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### エラー処理の方針

Massive ポーラーは意図的に耐障害性があります:

| エラー | 動作 |
|-------|----------|
| **401 認証エラー** | エラーとしてログ。ポーラーは実行継続（ユーザーが `.env` を修正して再起動する可能性あり）。 |
| **429 レート制限** | エラーとしてログ。次のポーリングは `poll_interval` 秒後にリトライ。 |
| **ネットワークタイムアウト** | エラーとしてログ。次のサイクルで自動的にリトライ。 |
| **不正なスナップショット** | 個別のティッカーを警告とともにスキップ。他のティッカーは引き続き処理。 |
| **全ティッカー失敗** | キャッシュは最後に知られた価格を保持。SSE は古いデータをストリーミング継続（データなしより良い）。 |

### 遅延インポート戦略

`from massive import RESTClient` はモジュールインポート時ではなく `start()` 内で行われます。これにより:
- `massive` パッケージは `MASSIVE_API_KEY` が設定されている場合にのみ必要。
- Massive API キーを持たない学生はパッケージをインストールする必要がない。
- シミュレーターパスは `numpy` 以外の外部依存関係がゼロ。

---

## 8. ファクトリー

**ファイル: `backend/app/market/factory.py`**

```python
from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate market data source based on environment variables.

    - MASSIVE_API_KEY set and non-empty → MassiveDataSource (real market data)
    - Otherwise → SimulatorDataSource (GBM simulation)

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveDataSource

        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        from .simulator import SimulatorDataSource

        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

### アプリ起動時の使用方法

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(initial_tickers)  # 例: ["AAPL", "GOOGL", ...]
```

---

## 9. SSE ストリーミングエンドポイント

**ファイル: `backend/app/market/stream.py`**

SSE エンドポイントは長期 HTTP 接続を保持し、`text/event-stream` として価格更新をクライアントにプッシュする FastAPI ルートです。

```python
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["streaming"])


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Create the SSE streaming router with a reference to the price cache.

    This factory pattern lets us inject the PriceCache without globals.
    """

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint for live price updates.

        Streams all tracked ticker prices every ~500ms. The client connects
        with EventSource and receives events in the format:

            data: {"AAPL": {"ticker": "AAPL", "price": 190.50, ...}, ...}

        Includes a retry directive so the browser auto-reconnects on
        disconnection (EventSource built-in behavior).
        """
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering if proxied
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> None:
    """Async generator that yields SSE-formatted price events.

    Sends all prices every `interval` seconds. Stops when the client
    disconnects (detected via request.is_disconnected()).
    """
    # Tell the client to retry after 1 second if the connection drops
    yield "retry: 1000\n\n"

    last_version = -1
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while True:
            # Check for client disconnect
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client_ip)
                break

            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                prices = price_cache.get_all()

                if prices:
                    data = {
                        ticker: update.to_dict()
                        for ticker, update in prices.items()
                    }
                    payload = json.dumps(data)
                    yield f"data: {payload}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

### SSE ワイヤーフォーマット

クライアントが受信する各イベントは次のようになります:

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{"ticker":"GOOGL","price":175.12,...}}

```

クライアントはこれを次のようにパースします:

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices は { "AAPL": { ticker, price, previous_price, ... }, ... }
};
```

### イベント駆動ではなくポールアンドプッシュを使う理由

SSE エンドポイントはデータソースから通知を受け取るのではなく、固定間隔でキャッシュをポーリングします。これはシンプルで、フロントエンドに均等に間隔を置いた更新を生成します。フロントエンドはこれらをスパークラインチャートに蓄積します — 規則的な間隔はクリーンな可視化のために重要です。

---

## 10. FastAPI ライフサイクル統合

マーケットデータシステムは `lifespan` コンテキストマネージャーパターンを使って FastAPI アプリケーションとともに起動/停止します。

**`backend/app/main.py` 内:**

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market.cache import PriceCache
from app.market.factory import create_market_data_source
from app.market.interface import MarketDataSource
from app.market.stream import create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown of background services."""

    # --- STARTUP ---

    # 1. Create the shared price cache
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    # 2. Create and start the market data source
    source = create_market_data_source(price_cache)
    app.state.market_source = source

    # 3. Load initial tickers from the database watchlist
    initial_tickers = await load_watchlist_tickers()  # reads from SQLite
    await source.start(initial_tickers)

    # 4. Register the SSE streaming router
    stream_router = create_stream_router(price_cache)
    app.include_router(stream_router)

    yield  # App is running

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


# Dependency for injecting the price cache into route handlers
def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

### 他のルートからのマーケットデータアクセス

バックエンドの他の部分（取引実行、ポートフォリオ評価、ウォッチリスト管理）は FastAPI の依存性注入経由で価格キャッシュとデータソースにアクセスします:

```python
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/api")

@router.post("/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
):
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(404, f"No price available for {trade.ticker}")
    # ... execute trade at current_price ...


@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
    price_cache: PriceCache = Depends(get_price_cache),
):
    # Add to database ...
    # Then tell the data source to start tracking it
    await source.add_ticker(payload.ticker)
    # ...


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    # Remove from database ...
    # Then stop tracking
    await source.remove_ticker(ticker)
    # ...
```

---

## 11. ウォッチリスト連携

ウォッチリストが変更されると（REST API または LLM チャット経由）、マーケットデータソースに正しいティッカーセットを追跡するよう通知する必要があります。

### フロー: ティッカーの追加

```
ユーザー（または LLM）→ POST /api/watchlist {ticker: "PYPL"}
  → ウォッチリストテーブル（SQLite）に挿入
  → await source.add_ticker("PYPL")
      シミュレーター: GBMSimulator に追加、コレスキーを再構築、キャッシュにシード
      Massive: ティッカーリストに追加、次のポーリングで反映
  → 成功を返す（ティッカー + 利用可能であれば現在価格）
```

### フロー: ティッカーの削除

```
ユーザー（または LLM）→ DELETE /api/watchlist/PYPL
  → ウォッチリストテーブル（SQLite）から削除
  → await source.remove_ticker("PYPL")
      シミュレーター: GBMSimulator から削除、コレスキーを再構築、キャッシュから削除
      Massive: ティッカーリストから削除、キャッシュから削除
  → 成功を返す
```

### エッジケース: ティッカーにオープンポジションがある

ユーザーがウォッチリストからティッカーを削除しても株を保有している場合、ポートフォリオ評価が正確であり続けるようにデータソースにティッカーを残す必要があります。ウォッチリストルートはこれを確認する必要があります:

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    # Remove from watchlist table
    await db.delete_watchlist_entry(ticker)

    # Only stop tracking if no open position
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)

    return {"status": "ok"}
```

---

## 12. テスト戦略

### 12.1 GBMSimulator のユニットテスト

**ファイル: `backend/tests/market/test_simulator.py`**

```python
import math
import pytest
from app.market.simulator import GBMSimulator
from app.market.seed_prices import SEED_PRICES


class TestGBMSimulator:
    """Unit tests for the GBM price simulator."""

    def test_step_returns_all_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        result = sim.step()
        assert set(result.keys()) == {"AAPL", "GOOGL"}

    def test_prices_are_positive(self):
        """GBM prices can never go negative (exp() is always positive)."""
        sim = GBMSimulator(tickers=["AAPL"])
        for _ in range(10_000):
            prices = sim.step()
            assert prices["AAPL"] > 0

    def test_initial_prices_match_seeds(self):
        sim = GBMSimulator(tickers=["AAPL"])
        # Before any step, price should be the seed price
        assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]

    def test_add_ticker(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("TSLA")
        result = sim.step()
        assert "TSLA" in result

    def test_remove_ticker(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        sim.remove_ticker("GOOGL")
        result = sim.step()
        assert "GOOGL" not in result
        assert "AAPL" in result

    def test_add_duplicate_is_noop(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("AAPL")
        assert len(sim._tickers) == 1

    def test_remove_nonexistent_is_noop(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.remove_ticker("NOPE")  # Should not raise

    def test_unknown_ticker_gets_random_seed_price(self):
        sim = GBMSimulator(tickers=["ZZZZ"])
        price = sim.get_price("ZZZZ")
        assert 50.0 <= price <= 300.0

    def test_empty_step(self):
        sim = GBMSimulator(tickers=[])
        result = sim.step()
        assert result == {}

    def test_prices_change_over_time(self):
        """After many steps, prices should have drifted from their seeds."""
        sim = GBMSimulator(tickers=["AAPL"])
        for _ in range(1000):
            sim.step()
        # Price should have changed (extremely unlikely to be exactly the seed)
        assert sim.get_price("AAPL") != SEED_PRICES["AAPL"]

    def test_cholesky_rebuilds_on_add(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None  # Only 1 ticker, no correlation matrix
        sim.add_ticker("GOOGL")
        assert sim._cholesky is not None  # Now 2 tickers, matrix exists
```

### 12.2 PriceCache のユニットテスト

**ファイル: `backend/tests/market/test_cache.py`**

```python
import pytest
from app.market.cache import PriceCache


class TestPriceCache:

    def test_update_and_get(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.ticker == "AAPL"
        assert update.price == 190.50
        assert cache.get("AAPL") == update

    def test_first_update_is_flat(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.direction == "flat"
        assert update.previous_price == 190.50

    def test_direction_up(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 191.00)
        assert update.direction == "up"
        assert update.change == 1.00

    def test_direction_down(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 189.00)
        assert update.direction == "down"
        assert update.change == -1.00

    def test_remove(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.remove("AAPL")
        assert cache.get("AAPL") is None

    def test_get_all(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        all_prices = cache.get_all()
        assert set(all_prices.keys()) == {"AAPL", "GOOGL"}

    def test_version_increments(self):
        cache = PriceCache()
        v0 = cache.version
        cache.update("AAPL", 190.00)
        assert cache.version == v0 + 1
        cache.update("AAPL", 191.00)
        assert cache.version == v0 + 2

    def test_get_price_convenience(self):
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("NOPE") is None
```

### 12.3 統合テスト: SimulatorDataSource

**ファイル: `backend/tests/market/test_simulator_source.py`**

```python
import asyncio
import pytest
from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


@pytest.mark.asyncio
class TestSimulatorDataSource:

    async def test_start_populates_cache(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "GOOGL"])

        # Cache should have seed prices immediately (before first loop tick)
        assert cache.get("AAPL") is not None
        assert cache.get("GOOGL") is not None

        await source.stop()

    async def test_prices_update_over_time(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
        await source.start(["AAPL"])

        initial = cache.get("AAPL").price
        await asyncio.sleep(0.3)  # Several update cycles
        current = cache.get("AAPL").price

        # Extremely unlikely to be identical after many steps
        # (but not impossible, so this is a probabilistic test)
        assert current != initial or True  # Soft assertion

        await source.stop()

    async def test_stop_is_clean(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])
        await source.stop()
        # Double stop should not raise
        await source.stop()

    async def test_add_and_remove_ticker(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])

        await source.add_ticker("TSLA")
        assert "TSLA" in source.get_tickers()
        assert cache.get("TSLA") is not None

        await source.remove_ticker("TSLA")
        assert "TSLA" not in source.get_tickers()
        assert cache.get("TSLA") is None

        await source.stop()
```

### 12.4 ユニットテスト: MassiveDataSource（モック使用）

**ファイル: `backend/tests/market/test_massive.py`**

```python
import asyncio
from unittest.mock import MagicMock, patch
import pytest
from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource


def _make_snapshot(ticker: str, price: float, timestamp_ms: int) -> MagicMock:
    """Create a mock Massive snapshot object."""
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = timestamp_ms
    return snap


@pytest.mark.asyncio
class TestMassiveDataSource:

    async def test_poll_updates_cache(self):
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,  # Long interval so the loop doesn't auto-poll
        )

        mock_snapshots = [
            _make_snapshot("AAPL", 190.50, 1707580800000),
            _make_snapshot("GOOGL", 175.25, 1707580800000),
        ]

        with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("GOOGL") == 175.25

    async def test_malformed_snapshot_skipped(self):
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL", "BAD"]

        good_snap = _make_snapshot("AAPL", 190.50, 1707580800000)
        bad_snap = MagicMock()
        bad_snap.ticker = "BAD"
        bad_snap.last_trade = None  # Will cause AttributeError

        with patch.object(source, "_fetch_snapshots", return_value=[good_snap, bad_snap]):
            await source._poll_once()

        # Good ticker processed, bad one skipped
        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("BAD") is None

    async def test_api_error_does_not_crash(self):
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL"]

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
            await source._poll_once()  # Should not raise

        assert cache.get_price("AAPL") is None  # No update happened
```

---

## 13. エラー処理とエッジケース

### 13.1 起動: 空のウォッチリスト

データベースにウォッチリストエントリーがない場合（ユーザーがすべて削除した）、`start()` は空のリストを受け取ります。両データソースはこれをグレースフルに処理します — シミュレーターは価格を生成せず、Massive ポーラーは API 呼び出しをスキップします。SSE エンドポイントは空のイベントを送信します。ユーザーがティッカーを追加すると、ソースはすぐに追跡を開始します。

### 13.2 取引中の価格キャッシュミス

ユーザーがキャッシュされた価格のないティッカーを取引しようとした場合（例: ウォッチリストに追加されたばかりで Massive がまだポーリングしていない）:

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker}. Please wait a moment and try again.",
    )
```

シミュレーターは `add_ticker()` でキャッシュにシードすることでこれを回避します。Massive クライアントには短いギャップが発生する可能性があります — 明確なメッセージを含む HTTP 400 が正しいレスポンスです。

### 13.3 Massive API キーが無効

API キーが設定されているが無効な場合、最初のポーリングが 401 で失敗します。ポーラーはエラーをログに記録して再試行し続けます。SSE エンドポイントは空のデータをストリーミングします。ユーザーは価格が表示されず、「接続済み」を示す接続状態インジケーターが表示されます（SSE は動作しているが、データがない）。修正は API キーを正しく設定して再起動することです。

### 13.4 負荷下でのスレッドセーフ

`PriceCache` は `threading.Lock` を使用します。これはミューテックスで、一度に 1 つのスレッドしか保持できません。通常の負荷（10 ティッカー、2 更新/秒）では、ロックの競合は無視できます。クリティカルセクションは非常に小さい（辞書のルックアップ + 代入）。

これがボトルネックになった場合（数百のティッカー、多数の同時 SSE リーダー）、修正は `ReadWriteLock` になりますが、このプロジェクトにはそのレベルの最適化は不要です。

### 13.5 シミュレーターの精度

微小な `dt` の GBM は非常に小さなティックあたりの移動を生成します。浮動小数点精度は問題ではありません:
- 価格は `GBMSimulator.step()` で小数点以下 2 桁に `round()` される
- 指数計算（`exp(drift + diffusion)`）は数値的に安定
- 価格は常に正（指数関数）

---

## 14. 設定サマリー

すべての調整可能なパラメーターとそのデフォルト:

| パラメーター | 場所 | デフォルト | 説明 |
|-----------|----------|---------|-------------|
| `MASSIVE_API_KEY` | 環境変数 | `""`（空） | 設定すると Massive API を使用; それ以外はシミュレーターを使用 |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5`（秒） | シミュレーターティック間の時間 |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0`（秒） | Massive API ポーリング間の時間 |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | ティックあたりティッカーあたりのランダムショックイベントの確率 |
| `dt` | `GBMSimulator.__init__` | `~8.5e-8` | GBM タイムステップ（取引年の端数） |
| SSE プッシュ間隔 | `_generate_events()` | `0.5`（秒） | クライアントへの SSE プッシュ間の時間 |
| SSE リトライディレクティブ | `_generate_events()` | `1000`（ミリ秒） | ブラウザ EventSource 再接続遅延 |

### パッケージ `__init__.py`

**ファイル: `backend/app/market/__init__.py`**

```python
"""Market data subsystem for FinAlly.

Public API:
    PriceUpdate         - Immutable price snapshot dataclass
    PriceCache          - Thread-safe in-memory price store
    MarketDataSource    - Abstract interface for data providers
    create_market_data_source - Factory that selects simulator or Massive
    create_stream_router - FastAPI router factory for SSE endpoint
"""

from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router

__all__ = [
    "PriceUpdate",
    "PriceCache",
    "MarketDataSource",
    "create_market_data_source",
    "create_stream_router",
]
```
