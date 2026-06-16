# FinAlly バックエンド

FinAlly AI トレーディングワークステーションの FastAPI バックエンド。

## 構造

- `app/` - アプリケーションコード
  - `market/` - マーケットデータサブシステム
    - `models.py` - PriceUpdate データクラス
    - `cache.py` - スレッドセーフな価格キャッシュ
    - `interface.py` - MarketDataSource 抽象インターフェース
    - `simulator.py` - GBM ベースのマーケットシミュレーター
    - `massive_client.py` - Massive/Polygon.io API クライアント
    - `factory.py` - データソースファクトリー
    - `stream.py` - SSE ストリーミングエンドポイント
    - `seed_prices.py` - デフォルトティッカー価格とパラメーター

- `tests/` - ユニットテストと統合テスト
  - `market/` - マーケットデータテスト

## テストの実行

```bash
# 依存関係のインストール
uv sync --dev

# 全テストの実行
uv run pytest

# カバレッジ付きで実行
uv run pytest --cov=app --cov-report=html

# 特定のテストファイルを実行
uv run pytest tests/market/test_simulator.py

# 詳細出力で実行
uv run pytest -v
```

## 環境変数

- `MASSIVE_API_KEY` - 任意。設定すると Massive API から実際のマーケットデータを使用。未設定の場合は組み込みシミュレーターを使用。

## 開発

```bash
# 依存関係のインストール
uv sync --dev

# リンターの実行
uv run ruff check .

# コードのフォーマット
uv run ruff format .
```
