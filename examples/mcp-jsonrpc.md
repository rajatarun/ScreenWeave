# MCP JSON-RPC examples

Replace `MCP_ENDPOINT` with your stack output `McpEndpoint`. In `audit` mode, no `Authorization` header is required. In `enforce` mode, send a valid access token for the `screenweave` audience.

## 1. List tools

```bash
export MCP_ENDPOINT="https://YOUR_API.execute-api.us-east-1.amazonaws.com/mcp"

curl -sS -X POST "$MCP_ENDPOINT" \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

## 2. Start a crawl

```bash
curl -sS -X POST "$MCP_ENDPOINT" \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "crawl_url",
      "arguments": {
        "url": "https://example.com",
        "max_depth": 1,
        "max_links": 5
      }
    }
  }'
```

Save `session_id` from the response text content.

## 3. Poll session status

```bash
SESSION_ID="sess-REPLACE_ME"

curl -sS -X POST "$MCP_ENDPOINT" \
  -H 'content-type: application/json' \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": 3,
    \"method\": \"tools/call\",
    \"params\": {
      \"name\": \"get_session_status\",
      \"arguments\": { \"session_id\": \"$SESSION_ID\" }
    }
  }"
```

Repeat until `status` is `COMPLETED` or `FAILED`.

## 4. Fetch full session (screenshots + JSON + trace)

```bash
curl -sS -X POST "$MCP_ENDPOINT" \
  -H 'content-type: application/json' \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": 4,
    \"method\": \"tools/call\",
    \"params\": {
      \"name\": \"get_full_session\",
      \"arguments\": { \"session_id\": \"$SESSION_ID\" }
    }
  }"
```

Pre-signed HTTPS URLs expire per `SignedUrlExpiresSeconds` (default one hour).

## Initialize (optional health check)

```bash
curl -sS -X POST "$MCP_ENDPOINT" \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-06-18",
      "capabilities": {},
      "clientInfo": { "name": "curl", "version": "1.0" }
    }
  }'
```

See also `examples/response.json` for illustrative artifact shapes (field names may differ slightly from live MCP envelopes).
