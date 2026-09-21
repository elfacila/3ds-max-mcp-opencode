# Protocol

TCP `127.0.0.1:8080`, UTF-8, **one JSON object per line**. One request per connection; server closes after replying.

## Request

```json
{"id": "<uuid>", "code": "<python source>"}
```

## Response

```json
{
  "id": "<echoed id>",
  "status": "ok | error",
  "output": "<captured stdout+stderr>",
  "result": "<value of __mcp_result__ (JSON-safe, else repr)> | null",
  "error": "<traceback or null>",
  "version": "<LISTENER_VERSION>"
}
```

## Conventions

- Assign `__mcp_result__` in Max-side code for structured data; otherwise parse `output`.
- The exec namespace persists across calls — imports and variables survive.
- Limits: 1 MB code per request, 30 s client read timeout, 120 s main-thread execution wait (then a timeout error, not a hang).
