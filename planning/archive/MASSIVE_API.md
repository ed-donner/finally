# Massive API リファレンス（旧 Polygon.io）

FinAlly で使用する Massive（旧 Polygon.io）REST API のリファレンスドキュメント。

## 概要

- **ベース URL**: `https://api.massive.com`（レガシーの `https://api.polygon.io` も引き続きサポート）
- **Python パッケージ**: `massive`（`pip install -U massive` / `uv add massive` でインストール）
- **最小 Python バージョン**: 3.9+
- **認証**: `MASSIVE_API_KEY` 環境変数または `RESTClient(api_key=...)` への引数で API キーを指定
- **認証ヘッダー**: `Authorization: Bearer <API_KEY>`（クライアントが自動的に処理）

## レート制限

| ティア | 制限 |
|------|-------|
| 無料 | 5 リクエスト/分 |
| 有料（全ティア） | 無制限（推奨: 100 req/s 未満に抑える） |

FinAlly ではタイマーでポーリングします。無料ティア: 15 秒ごとにポーリング。有料: 2-5 秒ごとにポーリング。

## クライアントの初期化

```python
from massive import RESTClient

# 環境から MASSIVE_API_KEY を自動的に読み取る
client = RESTClient()

# または明示的に渡す
client = RESTClient(api_key="your_key_here")
```

## FinAlly で使用するエンドポイント

### 1. スナップショット — 全ティッカー（主要エンドポイント）

**単一の API 呼び出し**で複数ティッカーの現在価格を取得します。ポーリングに使用するメインエンドポイントです。

**REST**: `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT`

**Python クライアント**:
```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient()

# 特定ティッカーのスナップショットを取得（1 回の API 呼び出し）
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"],
)

for snap in snapshots:
    print(f"{snap.ticker}: ${snap.last_trade.price}")
    print(f"  日次変化: {snap.day.change_percent}%")
    print(f"  日次 OHLC: O={snap.day.open} H={snap.day.high} L={snap.day.low} C={snap.day.close}")
    print(f"  出来高: {snap.day.volume}")
```

**レスポンス構造**（ティッカーごと）:
```json
{
  "ticker": "AAPL",
  "day": {
    "open": 129.61,
    "high": 130.15,
    "low": 125.07,
    "close": 125.07,
    "volume": 111237700,
    "volume_weighted_average_price": 127.35,
    "previous_close": 129.61,
    "change": -4.54,
    "change_percent": -3.50
  },
  "last_trade": {
    "price": 125.07,
    "size": 100,
    "exchange": "XNYS",
    "timestamp": 1675190399000
  },
  "last_quote": {
    "bid_price": 125.06,
    "ask_price": 125.08,
    "bid_size": 500,
    "ask_size": 1000,
    "spread": 0.02,
    "timestamp": 1675190399500
  },
  "prev_daily_bar": { "...": "前日の OHLCV" },
  "minute_volume": { "...": "分単位の出来高" }
}
```

**抽出する主要フィールド**:
- `last_trade.price` — 取引・表示用の現在価格
- `day.previous_close` — 日次変化率の計算用
- `day.change_percent` — 日次変化率
- `last_trade.timestamp` — 価格が記録された時刻

### 2. 単一ティッカースナップショット

1 つのティッカーの詳細データを取得（例: ユーザーがティッカーをクリックして詳細ビューを表示する場合）。

**Python クライアント**:
```python
snapshot = client.get_snapshot_ticker(
    market_type=SnapshotMarketType.STOCKS,
    ticker="AAPL",
)

print(f"価格: ${snapshot.last_trade.price}")
print(f"Bid/Ask: ${snapshot.last_quote.bid_price} / ${snapshot.last_quote.ask_price}")
print(f"日中レンジ: ${snapshot.day.low} - ${snapshot.day.high}")
```

### 3. 前日終値

ティッカーの前日の OHLC を取得します。シード価格として有用です。

**REST**: `GET /v2/aggs/ticker/{ticker}/prev`

**Python クライアント**:
```python
prev = client.get_previous_close_agg(ticker="AAPL")

for agg in prev:
    print(f"前日終値: ${agg.close}")
    print(f"OHLC: O={agg.open} H={agg.high} L={agg.low} C={agg.close}")
    print(f"出来高: {agg.volume}")
```

**レスポンス**:
```json
{
  "ticker": "AAPL",
  "results": [
    {
      "o": 150.0,
      "h": 155.0,
      "l": 149.0,
      "c": 154.5,
      "v": 1000000,
      "t": 1672531200000
    }
  ]
}
```

### 4. アグリゲート（バー）

日付範囲にわたる過去の OHLCV バー。ライブポーリングには不要ですが、過去チャートを追加する場合に有用です。

**REST**: `GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}`

**Python クライアント**:
```python
aggs = []
for a in client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="day",
    from_="2024-01-01",
    to="2024-01-31",
    limit=50000,
):
    aggs.append(a)

for a in aggs:
    print(f"日付: {a.timestamp}, O={a.open} H={a.high} L={a.low} C={a.close} V={a.volume}")
```

**レスポンス**（各バー）:
```json
{
  "o": 130.0,
  "h": 132.5,
  "l": 129.8,
  "c": 131.2,
  "v": 50000000,
  "t": 1672531200000
}
```

### 5. 最終約定 / 最終気配

最新の約定または NBBO 気配の個別エンドポイント。

```python
# 最終約定
trade = client.get_last_trade(ticker="AAPL")
print(f"最終約定: ${trade.price} x {trade.size}")

# 最終 NBBO 気配
quote = client.get_last_quote(ticker="AAPL")
print(f"Bid: ${quote.bid} x {quote.bid_size}")
print(f"Ask: ${quote.ask} x {quote.ask_size}")
```

## FinAlly での API 使用方法

Massive ポーラーはバックグラウンドタスクとして実行されます:

1. ウォッチリストからすべてのティッカーを収集
2. それらのティッカーで `get_snapshot_all()` を呼び出す（1 回の API 呼び出し）
3. 各スナップショットから `last_trade.price` と `day.previous_close` を抽出
4. 共有インメモリ価格キャッシュに書き込む
5. ポーリング間隔だけスリープして繰り返す

```python
import asyncio
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

async def poll_massive(api_key: str, get_tickers, price_cache, interval: float = 15.0):
    """Massive API をポーリングして価格キャッシュを更新する。"""
    client = RESTClient(api_key=api_key)

    while True:
        tickers = get_tickers()
        if tickers:
            snapshots = client.get_snapshot_all(
                market_type=SnapshotMarketType.STOCKS,
                tickers=tickers,
            )
            for snap in snapshots:
                price_cache.update(
                    ticker=snap.ticker,
                    price=snap.last_trade.price,
                    previous_close=snap.day.previous_close,
                    timestamp=snap.last_trade.timestamp,
                )

        await asyncio.sleep(interval)
```

## エラー処理

クライアントは HTTP エラーに対して例外を発生させます:
- **401**: 無効な API キー
- **403**: 権限不足（プランがエンドポイントを含んでいない）
- **429**: レート制限超過（無料ティア: 5 req/分）
- **5xx**: サーバーエラー（クライアントはデフォルトで 3 回リトライする）

## 注意事項

- スナップショットエンドポイントは**すべての要求されたティッカーを 1 回の呼び出しで**データを返します — これが無料ティアのレート制限内に収まるための重要なポイントです
- API からのタイムスタンプは Unix ミリ秒単位
- 市場が閉じている時間帯、`last_trade.price` は最後に取引された価格を反映します（時間外取引が含まれる場合あり）
- `day` オブジェクトは市場オープン時にリセットされます; プレマーケット中は前セッションの値になる場合があります
