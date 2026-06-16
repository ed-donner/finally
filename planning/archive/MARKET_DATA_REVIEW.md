# マーケットデータバックエンド — コードレビュー

**日付:** 2026-02-10
**スコープ:** `backend/app/market/`（ソースファイル 8 本）および `backend/tests/market/`（テストファイル 6 本）

---

## 1. テスト結果サマリー

**テスト 73 件収集、68 件合格、5 件失敗。**

すべての失敗は `test_massive.py` にあり、同一の根本原因によるものです: `massive` パッケージがテスト環境にインストールされていないため、`patch("app.market.massive_client.RESTClient")` が `AttributeError` で失敗します。これはモジュールレベルの名前 `RESTClient` がインポートされていないためです（メソッド内で遅延インポートされています）。これは環境の問題であり、ロジックのバグではありません — テスト自体は正しく構成されていますが、`massive` パッケージが利用可能（またはパッチに `create=True` が指定）でモックターゲットが存在する必要があります。

失敗したテスト:
- `test_poll_updates_cache` — `massive` が存在しない場合 `_fetch_snapshots` が適切にモックされないため `asyncio.to_thread` が失敗
- `test_malformed_snapshot_skipped` — 同じ原因
- `test_timestamp_conversion` — 同じ原因
- `test_stop_cancels_task` — `patch("app.market.massive_client.RESTClient")` が失敗（モジュールレベルに名前が存在しない）
- `test_start_immediate_poll` — 同上

基盤となる `_poll_once()` ロジック自体は正しいです。`source._fetch_snapshots` を直接モックする 3 つのテストは、`asyncio.to_thread(self._fetch_snapshots)` が `massive` をインポートしようとする実際のメソッドを呼び出すため失敗します。`patch("app.market.massive_client.RESTClient")` を使用する 2 つのテストは、遅延インポートのためモジュールの名前空間にその名前が存在しないため失敗します。どちらの問題も `massive` パッケージがインストールされれば解決します。

**リント（ruff）:** ソースコードはクリーンに通過。テストには 5 件の未使用インポート警告（一部のテストファイルで `pytest`、`math`、`asyncio` がインポートされているが使用されていない）。

**カバレッジ:** 全体 84%。
| モジュール | カバレッジ | 備考 |
|---|---|---|
| models.py | 100% | |
| cache.py | 100% | |
| interface.py | 100% | |
| seed_prices.py | 100% | |
| factory.py | 100% | |
| simulator.py | 98% | 未カバー: `_add_ticker_internal` の重複ガード（L145）、`_run_loop` の例外ログ（L264-265） |
| massive_client.py | 56% | 想定内 — massive パッケージなしでは実際の API メソッドを実行できない |
| stream.py | 31% | 想定内 — SSE ジェネレーターのテストには実行中の ASGI サーバーが必要 |

---

## 2. アーキテクチャ評価

マーケットデータサブシステムはよく設計されています。クリーンなストラテジーパターンに従っています:

```
MarketDataSource (ABC)
├── SimulatorDataSource  （GBM シミュレーター）
└── MassiveDataSource    （Polygon.io REST ポーラー）
        │
        ▼
   PriceCache（共有、スレッドセーフ）
        │
        ▼
   SSE ストリーム → フロントエンド
```

**強み:**
- 8 つの焦点を絞ったモジュールにわたる明確な関心の分離
- 遅延インポートを用いたファクトリーパターン — `massive` パッケージは `MASSIVE_API_KEY` が設定されている場合にのみ必要
- 唯一の真実の源泉としての PriceCache がプロデューサーとコンシューマーを分離
- `frozen=True, slots=True` を持つイミュータブルな `PriceUpdate` データクラスは正確で効率的
- GBM の数学は適切: `exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)` による対数正規価格パス
- コレスキー分解による相関移動はリアリズムのための優れたタッチ
- すべてのバックグラウンドタスクは適切にキャンセル可能で `stop()` で冪等

---

## 3. 発見された問題

### 3.1 ビルド設定バグ（重要度: 高）

`pyproject.toml` に hatchling パッケージ探索設定が欠けています。`uv sync` の実行が失敗します:

```
ValueError: Unable to determine which files to ship inside the wheel
```

**修正:** `pyproject.toml` に以下を追加:
```toml
[tool.hatch.build.targets.wheel]
packages = ["app"]
```

これが修正されるまで Docker ビルドと新規 `uv sync` がブロックされます。

### 3.2 Massive テストの脆弱性（重要度: 中）

`massive` パッケージがインストールされていない場合、`test_massive.py` の 5 つのテストが失敗します。根本原因は 2 つあります:

1. **`_poll_once` が `asyncio.to_thread(self._fetch_snapshots)` を使用** — `_fetch_snapshots` がインスタンスにパッチされていても、`to_thread` はスレッドエグゼキューターで実行します。3 つのテストが `_fetch_snapshots` を `MagicMock`（同期）としてモックしていますが、`asyncio.to_thread` が `loop.run_in_executor` でラップします... ただし `_fetch_snapshots` がパッチされていない場合、実際のメソッドが `from massive.rest.models import SnapshotMarketType` を試みて失敗します。

2. **`patch("app.market.massive_client.RESTClient")`** が `start()` 内の遅延インポートのためモジュールレベルに存在しない名前をターゲットにしています。パッチには `create=True` が必要か、インポートを `TYPE_CHECKING` ガードの背後のモジュールレベルに配置する必要があります。

これらのテストは `massive>=1.0.0` がインストールされていれば合格します（`pyproject.toml` がコア依存関係として宣言しているため）。これは技術的にはテスト環境の問題であり、コードのバグではありません。しかし、遅延インポートの目的がシミュレーターのみの使用で `massive` をオプションにすることであるため、テストもそれなしで動作する必要があります。

### 3.3 `_generate_events` 戻り値型アノテーション（重要度: 低）

`stream.py:54` が戻り値型を `-> None` と宣言していますが、この関数は非同期ジェネレーター（`yield` を使用）です。正しいアノテーションは `-> AsyncGenerator[str, None]` またはアノテーションを削除することです。これはランタイムの問題を引き起こしませんが、型チェッカーと開発者に誤解を与えます。

### 3.4 `version` プロパティがロックなし（重要度: 低）

`PriceCache.version` が `self._lock` を取得せずに `self._version` を読み取ります:

```python
@property
def version(self) -> int:
    return self._version
```

GIL のある CPython では単一の `int` の読み取りはアトミックであるため、破損は発生しません。しかし、クラスの残りと一貫性がなく、プロジェクトが GIL なし Python ビルド（PEP 703、Python 3.13t+）で実行される場合、競合状態になる可能性があります。現在のコンテキストでは軽微な懸念です。

### 3.5 `SimulatorDataSource.get_tickers` がプライベート状態にアクセス（重要度: 低）

`simulator.py:254`:
```python
def get_tickers(self) -> list[str]:
    return list(self._sim._tickers) if self._sim else []
```

これは `GBMSimulator._tickers`（プライベート属性）にアクセスしています。`GBMSimulator` は境界をクリーンに保つために `get_tickers()` メソッドまたは `tickers` プロパティを公開すべきです。

### 3.6 モジュールレベルのルーターインスタンス（重要度: 低）

`stream.py:16` はモジュールレベルの `router` オブジェクトを作成し、`create_stream_router()` がクロージャを介してルートを登録します。`create_stream_router` が 2 回呼び出された場合（例: テストで）、同じルーターに `/prices` ルートが 2 回登録されます。実際にはアプリ起動時に 1 回しか呼び出されないため問題は発生しませんが、テストのための潜在的な地雷です。

### 3.7 テストの未使用インポート（重要度: 軽微）

`ruff` からの 5 件のリント警告:
- `test_cache.py`: 未使用の `pytest`
- `test_factory.py`: 未使用の `pytest`
- `test_massive.py`: 未使用の `asyncio`
- `test_simulator.py`: 未使用の `math`、未使用の `pytest`

---

## 4. 設計上の観察

### 4.1 うまくできていること

- **GBM パラメーターのチューニングは考慮されています。** TSLA の sigma=0.50 対 V の 0.17 は実世界のボラティリティの違いを反映しています。ショックイベントシステム（ティックあたり約 0.1%、約 50 秒ごとに目に見える動きを生成）は価格を不安定にすることなく視覚的ドラマを加えます。
- **相関移動のためのコレスキー分解**は数学的に正しいアプローチです。セクターベースの相関構造（テクノロジー 0.6、金融 0.5、セクター間 0.3）は合理的です。
- **両データソースの防御的なエラー処理。** `_run_loop`（シミュレーター）と `_poll_once`/`_poll_loop`（Massive）の両方が例外をキャッチして継続します。これは長時間実行のバックグラウンドサービスにとって不可欠です。
- **SSE 実装はクリーンです。** バージョンベースの変更検知により冗長なペイロードの送信を回避します。`retry: 1000\n\n` ディレクティブによりブラウザが自動再接続します。nginx バッファリングが事前に無効化されています。
- **起動時のキャッシュへのシード**により、フロントエンドは最初の SSE ポーリングでデータを取得でき、表示上の遅延がありません。
- **Lock を使ったスレッドセーフキャッシュ**は、Massive クライアントが `asyncio.to_thread` 経由で API 呼び出しを実行するため正しい選択です。

### 4.2 不足しているテスト

- **SSE ストリーミング（`stream.py`）**はカバレッジ 31% で専用テストがありません。SSE のテストには ASGI テストクライアント（例: `httpx.AsyncClient` と `app`）が必要です。PriceCache の主要なコンシューマーであることを考えると、基本的な統合テストでも信頼性が向上します。
- **PriceCache の並行/スレッドセーフテストがありません。** ロックの使用はコードレビューからは正しく見えますが、複数のスレッドが同時に書き込むテストで実証的に検証できます。
- **デフォルトの 10 ティッカーすべてを使った `GBMSimulator` のテストがありません。** テストは 1-2 ティッカーを使用しています。完全なデフォルト 10 ティッカーセットでコレスキー分解が成功することを確認するテストにより、相関行列の問題を検出できます。

### 4.3 将来の考慮事項

- `PriceCache` は履歴に上限を設けず、ティッカーごとの最新価格のみを保存するため、メモリは O(ティッカー数) に制限されています。良好。
- `DEFAULT_CORR` 定数（0.3、`seed_prices.py:48`）は定義されていますが `_pairwise_correlation` では参照されていません。スタティックメソッドはフォールバックとして `CROSS_GROUP_CORR`（同様に 0.3）を返します。これは意味的に混乱を招きます — `DEFAULT_CORR` はどのグループにも属さないティッカーを意図しているようですが、コードはマッチしないすべてのペアに対して `CROSS_GROUP_CORR` を返します。どちらも 0.3 であるため動作は正しいですが、命名が誤解を招きます。

---

## 5. 評価

マーケットデータバックエンドはしっかりとよく構成されています。GBM シミュレーター、価格キャッシュ、抽象インターフェース、ファクトリーパターン、SSE ストリーミングはすべて正しく動作し、良い実践に従っています。このアーキテクチャはアプリケーションの残りと問題なく統合されます。

**続行前に必ず修正すること:**
1. `[tool.hatch.build.targets.wheel] packages = ["app"]` を `pyproject.toml` に追加する — これがなければ `uv sync` と Docker ビルドが失敗します。

**修正すべきこと:**
2. `massive` パッケージが存在しない場合でも Massive テストが動作するようにする（パッチに `create=True` を使用するか、モックを再構成する）。
3. `_generate_events` の戻り値型アノテーションを修正する。
4. テストファイルの未使用インポートを削除する。

**あれば良いこと:**
5. `GBMSimulator` にパブリックな `get_tickers()` メソッドを追加する。
6. 少なくとも 1 つの SSE 統合テストを追加する。
7. `DEFAULT_CORR` と `CROSS_GROUP_CORR` の命名を明確にする。
