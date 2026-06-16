# バックエンド — 開発者ガイド

## プロジェクトセットアップ

```bash
cd backend
uv sync --extra dev   # テスト/リントツールを含むすべての依存関係をインストール
```

## マーケットデータ API

マーケットデータサブシステムは `app/market/` にあります。以下のインポートを使用してください:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source
```

### コアタイプ

- **`PriceUpdate`** — イミュータブルなデータクラス: `ticker`、`price`、`previous_price`、`timestamp`、加えてプロパティ `change`、`change_percent`、`direction`（"up"/"down"/"flat"）、および JSON シリアライゼーション用の `to_dict()`。

- **`PriceCache`** — スレッドセーフなインメモリストア。主なメソッド:
  - `update(ticker, price, timestamp=None) -> PriceUpdate`
  - `get(ticker) -> PriceUpdate | None`
  - `get_price(ticker) -> float | None`
  - `get_all() -> dict[str, PriceUpdate]`
  - `remove(ticker)`
  - `version` プロパティ — 更新のたびにインクリメントされる単調増加カウンター（SSE 変更検知用）

- **`MarketDataSource`** — `SimulatorDataSource` と `MassiveDataSource` が実装する抽象インターフェース。ライフサイクル: `start(tickers)` → `add_ticker()` / `remove_ticker()` → `stop()`。

- **`create_market_data_source(cache)`** — ファクトリー。`MASSIVE_API_KEY` が設定されている場合は `MassiveDataSource` を、そうでなければ `SimulatorDataSource` を返す。

### SSE ストリーミング

```python
from app.market import create_stream_router

router = create_stream_router(price_cache)  # FastAPI APIRouter を返す
# エンドポイント: GET /api/stream/prices (text/event-stream)
```

### シードデータ

デフォルトティッカー: AAPL、GOOGL、MSFT、AMZN、TSLA、NVDA、META、JPM、V、NFLX。シード価格とティッカーごとのボラティリティ/ドリフトパラメーターは `app/market/seed_prices.py` にあります。

## テストの実行

```bash
uv run --extra dev pytest -v              # 全テスト
uv run --extra dev pytest --cov=app       # カバレッジ付き
uv run --extra dev ruff check app/ tests/ # リント
```

## デモ

```bash
uv run market_data_demo.py   # シミュレート価格を使ったライブターミナルダッシュボード
```
