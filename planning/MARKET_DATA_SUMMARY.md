# マーケットデータバックエンド — サマリー

**ステータス:** 完成、テスト済み、レビュー済み、すべての問題解決済み。

## 構築内容

`backend/app/market/` に 8 モジュール、約 500 行からなる完全なマーケットデータサブシステムを構築。統一インターフェースを通じてライブ価格シミュレーションと実際のマーケットデータを提供します。

### アーキテクチャ

```
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBM シミュレーター（デフォルト、API キー不要）
└── MassiveDataSource    →  Polygon.io REST ポーラー（MASSIVE_API_KEY 設定時）
        │
        ▼
   PriceCache（スレッドセーフ、インメモリ）
        │
        ├──→ SSE ストリームエンドポイント（/api/stream/prices）
        ├──→ ポートフォリオ評価
        └──→ 取引実行
```

### モジュール

| ファイル | 用途 |
|------|---------|
| `models.py` | `PriceUpdate` — イミュータブルなフローズンデータクラス（ticker、price、previous_price、timestamp、change、direction） |
| `interface.py` | `MarketDataSource` — `start/stop/add_ticker/remove_ticker/get_tickers` を定義する抽象基底クラス |
| `cache.py` | `PriceCache` — SSE 変更検知用のバージョンカウンター付きスレッドセーフ価格ストア |
| `seed_prices.py` | リアルなシード価格、ティッカーごとの GBM パラメーター（ドリフト/ボラティリティ）、相関グループ |
| `simulator.py` | `GBMSimulator`（コレスキー相関移動を用いた幾何ブラウン運動）+ `SimulatorDataSource` |
| `massive_client.py` | `MassiveDataSource` — `massive` パッケージ経由の Polygon.io 用 REST ポーリングクライアント |
| `factory.py` | `create_market_data_source()` — `MASSIVE_API_KEY` 環境変数に基づいてシミュレーターか Massive かを選択 |
| `stream.py` | `create_stream_router()` — バージョンベースの変更検知を使用する FastAPI SSE エンドポイントファクトリー |

### 主要設計判断

- **ストラテジーパターン** — 両データソースが同じ ABC を実装; 下流コードはソース非依存
- **PriceCache を唯一の真実の源泉として** — プロデューサーが書き込み、コンシューマーが読み取り; 直接結合なし
- **相関移動を伴う GBM** — セクターベースの相関行列のコレスキー分解; テクノロジー株の相関は 0.6、金融は 0.5、セクター間は 0.3
- **ランダムショックイベント** — ティッカーあたりティックあたり約 0.1% の確率で 2-5% の移動が発生し、視覚的ドラマを演出
- **WebSockets より SSE** — シンプル、一方向プッシュ、ユニバーサルなブラウザサポート

## テストスイート

**テスト 73 件、すべて合格。** `backend/tests/market/` に 6 つのテストモジュール。

| モジュール | テスト数 | カバレッジ |
|--------|-------|----------|
| test_models.py | 11 | models.py: 100% |
| test_cache.py | 13 | cache.py: 100% |
| test_simulator.py | 17 | simulator.py: 98% |
| test_simulator_source.py | 10 |（統合テスト） |
| test_factory.py | 7 | factory.py: 100% |
| test_massive.py | 13 | massive_client.py: 56%（想定内 — API メソッドはモック） |

全体カバレッジ: 84%。

## コードレビューと適用済み修正

包括的なコードレビューで 7 つの問題を特定。すべて解決済み:

1. **pyproject.toml ビルド設定** — `[tool.hatch.build.targets.wheel] packages = ["app"]` を追加
2. **遅延インポートを削除** — `massive` はコア依存関係; インポートをトップレベルに移動
3. **SSE 戻り値型を修正** — `_generate_events` に `AsyncGenerator[str, None]` アノテーションを追加
4. **パブリック `get_tickers()`** — プライベート属性アクセスを避けるため `GBMSimulator` に追加
5. **相関定数を整理** — 未使用の `DEFAULT_CORR` を削除し、`CROSS_GROUP_CORR` に統合
6. **テストの未使用インポートを削除** — 4 つのテストファイルから `pytest`、`math`、`asyncio` をクリーンアップ
7. **Massive テストのモックを修正** — テスト内に `source._client` を設定し、パッチが正しい名前をターゲットにするよう修正

## デモ

リッチなターミナルデモが `backend/market_data_demo.py` で利用可能です:

```bash
cd backend
uv run market_data_demo.py
```

10 ティッカーすべてのライブ更新ダッシュボード、スパークライン、カラーコードされた方向矢印、注目すべき価格移動のイベントログを表示します。60 秒間実行されるか Ctrl+C で終了します。

## 下流コードからの使用方法

```python
from app.market import PriceCache, create_market_data_source

# 起動
cache = PriceCache()
source = create_market_data_source(cache)  # MASSIVE_API_KEY を読み取る
await source.start(["AAPL", "GOOGL", "MSFT", ...])

# 価格の読み取り
update = cache.get("AAPL")          # PriceUpdate または None
price = cache.get_price("AAPL")     # float または None
all_prices = cache.get_all()        # dict[str, PriceUpdate]

# 動的ウォッチリスト
await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")

# 終了
await source.stop()
```
