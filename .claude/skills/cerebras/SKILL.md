---
name: Cerebras Inference
description: Use this to write code to call an LLM using LiteLLM with the direct Cerebras API
---

# Calling an LLM via Cerebras

These instructions allow you to write code to call an LLM using the direct Cerebras API via LiteLLM.

## Setup

The `CEREBRAS_API_KEY` must be set in the `.env` file and loaded as an environment variable.

The uv project must include litellm and pydantic.
`uv add litellm pydantic`

## Code snippets

### Imports and constants

```python
from litellm import completion
MODEL = "cerebras/qwen-3-235b-a22b-instruct-2507"
```

### Code to call Cerebras for a text response

```python
response = completion(model=MODEL, messages=messages)
result = response.choices[0].message.content
```

### Code to call Cerebras for a Structured Outputs response

```python
response = completion(model=MODEL, messages=messages, response_format=MyBaseModelSubclass)
result = response.choices[0].message.content
result_as_object = MyBaseModelSubclass.model_validate_json(result)
```