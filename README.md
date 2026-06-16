# FinAlly — AI トレーディングワークステーション

ライブマーケットデータをストリーミングし、ポートフォリオ取引をシミュレートし、ポジションを分析して自然言語で取引を実行できる LLM チャットアシスタントを統合した、視覚的に美しい AI 搭載トレーディングワークステーション。

エージェント AI コーディングコースのキャップストーンプロジェクトとして、コーディングエージェントのみによって構築されています。

## 機能

- **ライブ価格ストリーミング** — SSE による緑/赤フラッシュアニメーション
- **シミュレートポートフォリオ** — 仮想現金 $10k、成行注文、即時約定
- **ポートフォリオ可視化** — ヒートマップ（ツリーマップ）、損益チャート、ポジションテーブル
- **AI チャットアシスタント** — 保有株の分析、取引の提案と自動実行
- **ウォッチリスト管理** — 手動または AI 経由でティッカーを追跡
- **ダークターミナル外観** — Bloomberg 風のデータ密度の高いレイアウト

## アーキテクチャ

ポート 8000 ですべてを提供する単一の Docker コンテナ:

- **フロントエンド**: Next.js（静的エクスポート）、TypeScript、Tailwind CSS
- **バックエンド**: FastAPI（Python/uv）、SSE ストリーミング
- **データベース**: SQLite（遅延初期化）
- **AI**: LiteLLM → OpenRouter（Cerebras 推論）、構造化出力
- **マーケットデータ**: 組み込み GBM シミュレーター（デフォルト）または Massive API（オプション）

## クイックスタート

```bash
# クローンして設定
cp .env.example .env
# .env に OPENROUTER_API_KEY を追加

# Docker で実行
docker build -t finally .
docker run -v finally-data:/app/db -p 8000:8000 --env-file .env finally

# http://localhost:8000 を開く
```

## 環境変数

| 変数 | 必須 | 説明 |
|---|---|---|
| `OPENROUTER_API_KEY` | 必須 | AI チャット用 OpenRouter API キー |
| `MASSIVE_API_KEY` | 任意 | 実際のマーケットデータ用 Massive（Polygon.io）キー。省略時はシミュレーターを使用 |
| `LLM_MOCK` | 任意 | `true` に設定すると決定論的なモック LLM 応答を返す（テスト用） |

## プロジェクト構造

```
finally/
├── frontend/    # Next.js 静的エクスポート
├── backend/     # FastAPI uv プロジェクト
├── planning/    # プロジェクトドキュメントとエージェント契約
├── test/        # Playwright E2E テスト
├── db/          # SQLite ボリュームマウント（実行時）
└── scripts/     # 起動/停止ヘルパー
```

## ライセンス

[LICENSE](LICENSE) を参照してください。
