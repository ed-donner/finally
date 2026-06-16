---
name: cerebras-inference
description: LiteLLM と OpenRouter を使って Cerebras 推論プロバイダー経由で LLM を呼び出すコードを書く際に使用する
---

# Cerebras 経由での LLM 呼び出し

これらの手順は、Cerebras を推論プロバイダーとして指定した LLM 呼び出しコードを作成するためのものです。
LiteLLM と OpenRouter を使用します。

## セットアップ

OPENROUTER_API_KEY を .env ファイルに設定し、環境変数として読み込む必要があります。

uv プロジェクトには litellm と pydantic を含める必要があります。
`uv add litellm pydantic`

## コードスニペット

Cerebras を使用するには、以下のサンプルのようなコードを使用してください。

### インポートと定数

```python
from litellm import completion
MODEL = "openrouter/openai/gpt-oss-120b"
EXTRA_BODY = {"provider": {"order": ["cerebras"]}}
```

### Cerebras 経由でテキスト応答を取得するコード

```python
response = completion(model=MODEL, messages=messages, reasoning_effort="low", extra_body=EXTRA_BODY)
result = response.choices[0].message.content
```

### Cerebras 経由で構造化出力応答を取得するコード

```python
response = completion(model=MODEL, messages=messages, response_format=MyBaseModelSubclass, reasoning_effort="low", extra_body=EXTRA_BODY)
result = response.choices[0].message.content
result_as_object = MyBaseModelSubclass.model_validate_json(result)
```
