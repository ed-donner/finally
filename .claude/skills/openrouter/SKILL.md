---
name: openrouter-inference
description: Use this to write code to call an LLM using LiteLLM and OpenRouter with a free model
---

# Calling an LLM via OpenRouter

These instructions allow you to write code to call an LLM through OpenRouter using LiteLLM.
The model is a free one, so no inference provider is pinned and no request costs money.

## Setup

The OPENROUTER_API_KEY must be set in the .env file and loaded in as an environment variable.

The uv project must include litellm and pydantic.
`uv add litellm pydantic`

## Code snippets

### Imports and constants

```python
from litellm import completion
MODEL = "openrouter/nvidia/nemotron-3.5-lightning:free"
```

The `openrouter/` prefix is what routes the call through OpenRouter — without it LiteLLM
cannot resolve the provider. Do not set `extra_body={"provider": {...}}`: pinning a provider
is what makes a request land on a paid one.

### Code to call for a text response

```python
response = completion(model=MODEL, messages=messages)
result = response.choices[0].message.content
```

### Code to call for a structured response

Free models do not support `response_format`, so Structured Outputs are unavailable. Use
forced tool calling instead: the schema is enforced server-side and the arguments come back
as JSON.

```python
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "submit_response",
            "description": "Return the reply to the user.",
            "parameters": MyBaseModelSubclass.model_json_schema(),
        },
    }
]

response = completion(
    model=MODEL,
    messages=messages,
    tools=TOOLS,
    tool_choice={"type": "function", "function": {"name": "submit_response"}},
)
raw = response.choices[0].message.tool_calls[0].function.arguments
result_as_object = MyBaseModelSubclass.model_validate_json(raw)
```

Always validate with Pydantic. A forced tool call is reliable but not guaranteed — check for
an empty `tool_calls` before indexing into it.

## Expect slow responses

Free endpoints are heavily shared and have no latency guarantee. Measured response times for
the model above ranged from 7 to 58 seconds across 18 calls, with a median around 20-30
seconds. Any UI calling this needs a progress indicator that sets an honest expectation, not
a bare spinner.

## Rate limits

Models with the `:free` suffix are limited to 20 requests per minute and 50 per day, rising
to 1000 per day once at least 10 USD of credit has been purchased on the account. Tests
should mock the LLM rather than spend this budget.
