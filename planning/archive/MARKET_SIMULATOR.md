# マーケットシミュレーター設計

Massive API キーが設定されていない場合にリアルな株価をシミュレートするためのアプローチとコード構造。

## 概要

シミュレーターは**幾何ブラウン運動（GBM）**を使ってリアルな株価パスを生成します。GBM は Black-Scholes オプション価格モデルの基礎となっている標準モデルです — 価格は連続的にランダムノイズとともに変動し、マイナスにはならず、実際の市場で観測される対数正規分布を示します。

更新は約 500ms 間隔で実行され、生き生きとした継続的な価格変化のストリームを生成します。

## GBM の数学

各タイムステップにおいて、株価は次のように変化します:

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

ここで:
- `S(t)` = 現在の価格
- `mu` = 年率ドリフト（期待収益率）、例: 0.05（5%）
- `sigma` = 年率ボラティリティ、例: 0.20（20%）
- `dt` = 取引年の端数としてのタイムステップ
- `Z` = 標準正規確率変数（N(0,1) から抽出）

約 252 取引日、1 日約 6.5 時間の 500ms 更新の場合:
```
dt = 0.5 / (252 * 6.5 * 3600) = ~8.5e-8
```

この微小な `dt` が小さく自然なティックあたりの値動きを生成します。

## 相関移動

実際の株は独立して動くわけではありません — テクノロジー株は連動して動く傾向があります。相関ランダム抽出を生成するために、相関行列の**コレスキー分解**を使用します。

相関行列 `C` が与えられたとき、`L = cholesky(C)` を計算します。独立した標準正規分布 `Z_independent` に対して:
```
Z_correlated = L @ Z_independent
```

デフォルト相関グループ:
- **テクノロジー**: AAPL、GOOGL、MSFT、AMZN、META、NVDA、NFLX — グループ内相関 ~0.6
- **金融**: JPM、V — グループ内相関 ~0.5
- **セクター間**: ~0.3 のベースライン相関
- **TSLA**: すべてとの相関が低い（~0.3）— 独自の動きをする

## ランダムイベント

各ステップで、各ティッカーには小さな確率（~0.001）でランダムイベントが発生します — 突然の 2-5% の移動。これによりドラマが加わり、ダッシュボードが視覚的に面白くなります。

```python
if random.random() < event_probability:
    shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
    price *= (1 + shock)
```

## シード価格

デフォルトウォッチリストのリアルな開始価格:

```python
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.0,
    "GOOGL": 175.0,
    "MSFT": 420.0,
    "AMZN": 185.0,
    "TSLA": 250.0,
    "NVDA": 800.0,
    "META": 500.0,
    "JPM": 195.0,
    "V": 280.0,
    "NFLX": 600.0,
}
```

動的に追加されたティッカー（リストにないもの）は $50-$300 のランダムな価格から開始します。

## ティッカーごとのパラメーター

各ティッカーには実世界の動作を反映した固有のボラティリティがあります:

```python
TICKER_PARAMS: dict[str, dict] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # 高ボラ
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # 高ボラ、強いドリフト
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # 低ボラ（銀行）
    "V":     {"sigma": 0.17, "mu": 0.04},   # 低ボラ（決済）
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# 未知のティッカーのデフォルト
DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}
```

## 実装

```python
import math
import random
import time
import numpy as np

class GBMSimulator:
    """複数ティッカーの相関 GBM 価格パスを生成する。"""

    def __init__(
        self,
        tickers: list[str],
        dt: float = 8.5e-8,
        event_probability: float = 0.001,
    ):
        self._dt = dt
        self._event_prob = event_probability
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict] = {}
        self._tickers: list[str] = []
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self.add_ticker(ticker)

    def add_ticker(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50, 300))
        self._params[ticker] = TICKER_PARAMS.get(ticker, DEFAULT_PARAMS)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """1 タイムステップ進める。{ticker: new_price} を返す。"""
        n = len(self._tickers)
        if n == 0:
            return {}

        # 相関正規乱数を生成
        z_independent = np.random.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z_independent
        else:
            z = z_independent

        result = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu = params["mu"]
            sigma = params["sigma"]

            # GBM ステップ
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # ランダムイベント
            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= (1 + shock)

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker)

    def _rebuild_cholesky(self) -> None:
        """相関行列のコレスキー分解を再構築する。"""
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._get_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)

    def _get_correlation(self, t1: str, t2: str) -> float:
        """2 つのティッカー間のペアワイズ相関を返す。"""
        tech = {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"}
        finance = {"JPM", "V"}

        t1_tech = t1 in tech
        t2_tech = t2 in tech
        t1_fin = t1 in finance
        t2_fin = t2 in finance

        # 同一セクター: 高い相関
        if t1_tech and t2_tech:
            return 0.6
        if t1_fin and t2_fin:
            return 0.5

        # TSLA は一匹狼
        if t1 == "TSLA" or t2 == "TSLA":
            return 0.3

        # セクター間または未知
        if (t1_tech and t2_fin) or (t1_fin and t2_tech):
            return 0.3

        # デフォルト
        return 0.3
```

## ファイル構造

すべてのシミュレーターコードは単一のモジュールに配置されます:

```
backend/
  app/
    market/
      simulator.py       # GBMSimulator クラス + シードデータ + SimulatorDataSource
      seed_prices.py      # SEED_PRICES、TICKER_PARAMS、DEFAULT_PARAMS（定数）
```

`seed_prices.py` には定数辞書のみが含まれます。`simulator.py` には `GBMSimulator` クラスと `SimulatorDataSource`（`GBMSimulator` を非同期ループでラップする `MarketDataSource` 実装）が含まれます。

## 動作に関する注意

- 価格がマイナスになることはない（GBM は乗法的 — `exp()` は常に正）
- 微小な `dt` がティックあたりのサブセント移動を生成し、時間とともに自然に蓄積する
- `sigma=0.50`（TSLA）の場合、シミュレートされた 1 日の取引でほぼ適切な日中レンジが生成される
- 相関行列は正半定値でなければならない — コレスキー分解は有効な相関行列に対してこれを保証する
- ランダムイベントはステップの約 0.1% で発生 = ティッカーあたり約 500 秒に 1 回。10 ティッカーであれば、約 50 秒ごとにどこかでイベントが発生する — 興味深さを保つのに十分
- セッション途中で新しいティッカーが追加されると、コレスキー行列が再構築される。これは O(n^2) だが n は小さい（< 50 ティッカー）
