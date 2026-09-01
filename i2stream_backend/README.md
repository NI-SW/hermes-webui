# i2Stream Compatibility Backend

Hermes WebUI bundled FastAPI compatibility service for the i2Stream assistant.
It preserves the existing port-50091 browser extension, node report, storage,
and Gateway Bridge contracts while Hermes WebUI provides the visible interface
and browser chat on port 8787.

## Purpose

- Route browser requests to a connected Hermes Gateway platform plugin.
- Keep HMAC and Gateway bridge credentials off the browser.
- Provide a communication endpoint compatible with workspace agent messaging.
- Provide file upload and token-based download endpoints for future artifact support.

## Configuration

Copy `.env.example` to `.env` and configure the required bridge secrets.

```bash
cp .env.example .env
```

Important variables:

- `HERMES_BASE_URL`: Hermes API server URL, usually `http://127.0.0.1:8642`.
- `PORT`: backend port, default `50091`.
- `PUBLIC_BASE_URL`: public base URL used in returned file links.
- `FILE_STORE_DIR`: generated-file storage directory. Defaults to `/app/data/agent-console/files`.
- `INBOX_FILE_STORE_DIR`: directory for files uploaded by external clients and shown in the Tampermonkey Files tab. Defaults to `/app/data/agent-console/inbox-files`.
- `VECTOR_SEARCH_HOST`: RAG/vector service root URL. It must be a full `http://host:port` or `https://host:port` value, for example `http://192.168.34.65:8900`.
- `PROXY_API_KEY`: optional backend API key. If set, callers must send `Authorization: Bearer <key>`.
- `SESSION_HMAC_SECRET`: required server-only secret of at least 32 UTF-8 bytes. It derives an opaque Gateway session ID from browser `client_id` and `conversation`.
- `GATEWAY_BRIDGE_TOKEN`: required bridge credential of at least 32 UTF-8 bytes. The Gateway plugin sends it as a Bearer token when connecting to `/internal/gateway`.
- Chat history SQLite file is stored at `/app/data/agent-console/chat.db`. `/app/data` is mounted to the host in container deployments.

The DataCop endpoint, target `agent` project, credentials, 30-second request timeout, 32-job queue, and 900-second job TTL are built into `config.py`. DataCop environment variables are intentionally not read.

Keep `SESSION_HMAC_SECRET` stable across restarts. Rotating it intentionally creates different Gateway session IDs, so existing Hermes conversation context is no longer selected.

## Run

```bash
uv run python main.py
```

The default public URL is:

```text
http://192.168.34.65:50091
```

## Endpoints

### `GET /health`

Returns backend status and configured Hermes base URL.

### `GET /dashboard`

Backend-hosted Dashboard page. It provides a left navigation with knowledge upload, knowledge file listing, Report management, and chat history lookup views. The knowledge view supports single and batch deletion of indexed files. The Report view lists files from `/api/reports` and supports open/download, single delete, and batch delete. It does not preview file content.

### `GET /`

Redirects to `/dashboard`.

### `POST /api/heartbeat`

Records the reporting IPv4 or IPv6 address. The JSON body is `{"ip":"10.1.1.10"}`.
The first heartbeat time is retained, each later heartbeat updates the latest
time, and a node is offline only after more than 90 seconds without a heartbeat.

### `GET /api/nodes`

Lists all retained node records with their first heartbeat, latest heartbeat,
and current online state.

### `DELETE /api/nodes/{ip}`

Deletes one retained node record. The operation is idempotent; a later heartbeat
from the same IP creates the record again.

### `POST /api/agent/requests`

SSE endpoint for the Tampermonkey assistant.

The frontend keeps its existing request shape. The backend derives this internal session ID without forwarding either public identity value:

```text
HMAC-SHA256(SESSION_HMAC_SECRET, canonical_json(["v1", client_id, conversation]))
```

It then sends `request.start` to the single authenticated Hermes Gateway plugin connected at `/internal/gateway`. If no Gateway is connected the endpoint returns HTTP 503; if the same internal session already has an in-flight request it returns HTTP 409. It never falls back to `/v1/responses`.

When the browser sends `X-I2H-Client-Id`, the backend stores the user message and final assistant response in SQLite for that browser identity.

After the assistant response is stored, the stream emits `proxy.message.persisted` with the assistant `message_id` before `response.completed`. The browser uses this ID to associate feedback with the exact stored message instead of guessing from the latest history row.

Report processing requests may include an optional `report_token` alongside a text `input`. The backend validates the token, resolves the matching file inside `INBOX_FILE_STORE_DIR`, removes `report_token` from the payload forwarded to Hermes, and appends the resolved path to the Agent-only input. SQLite stores the browser-visible input and `{"report_token": "..."}` in `payload_json`; it does not store the resolved path.

Example browser payload:

```json
{
  "model": "hermes-agent",
  "input": "请读取选中的报告，并根据报告中明确给出的解决方案处理报告所描述的问题。",
  "report_token": "...",
  "conversation": "i2stream-dashboard-10.1.131.91:58086",
  "store": true,
  "stream": true
}
```

### `POST /api/chat-clients`

Allocates a new browser identity token and records it in SQLite with a unique primary key. Tampermonkey calls this only when it has no stored `client_id`.

Response:

```json
{
  "code": 0,
  "status": "success",
  "client_id": "..."
}
```

### `GET /api/conversations/{conversation_id}/messages`

Returns visible chat history for the current browser identity.

Required header:

```text
X-I2H-Client-Id: <browser-client-id>
```

### `GET /api/chat-clients/{client_id}/conversations`

Returns all stored conversations for a supplied browser client ID, ordered by most recently updated conversation first.

### `POST /api/conversations/{conversation_id}/messages/{message_id}/feedback`

Accepts `{"feedback":"like"}` or `{"feedback":"dislike"}` for an assistant message owned by the current browser identity and conversation. It requires `X-I2H-Client-Id` and the same optional proxy authentication as the other conversation APIs.

`dislike` is acknowledged immediately and does not start a background task. `like` snapshots the visible conversation through the selected assistant message and queues an in-memory job. The job uses an isolated Gateway session to produce one strictly validated DataCop problem object, then sends one upload request to the existing DataCop problem API.

The first accepted feedback for a message is immutable. Its type, job ID, processing status, DataCop problem ID, and sanitized error are stored in chat SQLite, so chat history can restore the selected button and a repeated request cannot start another Agent task. The internal prompt, conversation snapshot, Agent response, and generated DataCop fields are not persisted. Summary and upload are one-shot operations and failures are terminal. A process restart marks unfinished jobs as failed instead of retrying them. The built-in 900-second TTL only releases finished in-memory snapshots; it does not delete the persisted feedback status. The original user conversation remains governed by the normal chat-history persistence rules.

### `GET /api/dialog-interactions/{job_id}`

Returns the persisted like-job state for the same `X-I2H-Client-Id`: `queued`, `summarizing`, `uploading`, `succeeded`, or `failed`. Successful responses include `datacop_problem_id`; failures include a sanitized `error`. Unknown or another client's jobs return HTTP 404.

### `DELETE /api/conversations/{conversation_id}/messages`

Marks existing messages as hidden for the current browser identity and conversation. It does not delete rows.

Required header:

```text
X-I2H-Client-Id: <browser-client-id>
```

### `GET /api/agent/requests/{request_id}/progress`

Returns progress messages collected while a streaming agent request is running.

Required header:

```text
X-I2H-Client-Id: <browser-client-id>
```

Progress is keyed by both browser client ID and request ID, so another browser cannot read a request's reasoning or completion state by reusing its request ID.

Query:

```text
after=<last-seen-seq>
```

Reasoning and tool lifecycle events received from Gateway are added to this progress stream in real time. Assistant snapshots may replace earlier text, so the Console keeps only the latest snapshot outside the bounded event queue and emits only the final completed text as one `response.output_text.delta` SSE event.

Gateway text is bounded by UTF-8 byte size: one reasoning event may contain up to 64 KiB and the aggregated assistant response may contain up to 256 KiB. The server also limits an incoming WebSocket message to 2 MiB. The progress cache keeps at most 16 KiB per displayed event and 256 KiB per request; oversized reasoning is marked as truncated, while an oversized assistant response fails the request explicitly instead of returning partial text as a successful result.

Progress is process-local and active requests are never expired. After a request first reaches a terminal state, its progress remains available for 15 minutes so the browser can perform a final read. A lifespan-managed cleanup task scans once per minute and removes expired records; repeated cleanup paths do not extend the retention period.

On successful completion, `MEDIA:/path` markers are removed from assistant text and converted into `proxy.file` or `proxy.file_error` events. Successfully registered files are saved with the assistant history payload. A path produced on another host can legitimately result in `proxy.file_error` when it is not visible to Agent Console.

Gateway failures end with a generic `response.failed` event followed by `[DONE]`. Remote error details are logged server-side and are neither returned to the browser nor persisted as assistant history.

### `WS /internal/gateway`

Internal WebSocket endpoint for one Hermes Gateway platform plugin connection. It requires:

```text
Authorization: Bearer <GATEWAY_BRIDGE_TOKEN>
```

Console sends `request.start` and `request.cancel`. Gateway sends `assistant.snapshot`, `reasoning`, `tool.started`, `tool.completed`, `request.completed`, and `request.failed`; every frame carries the opaque `request_id` and HMAC-derived `session_id`. A disconnect fails all affected browser requests explicitly.

### `POST /api/agent/local-requests`

Workspace agent communication endpoint.

Request:

```json
{
  "agent_port": 8642,
  "conversation_id": "my-project",
  "messages": [
    { "role": "user", "content": "Hello" }
  ],
  "api_key": "agent-api-key",
  "stream": true
}
```

When `stream` is `true`, the backend returns an SSE stream from:

```text
http://127.0.0.1:{agent_port}/v1/responses
```

When `stream` is `false`, the backend returns:

```json
{
  "code": 0,
  "status": "success",
  "conversation_id": "my-project",
  "output": [
    { "role": "assistant", "content": "..." }
  ]
}
```

### `POST /api/generated-files`

Multipart upload endpoint.

Field name:

```text
file
```

Returns a token and download URL under `/api/generated-files/{token}/content`.

### `POST /api/knowledge/files`

Multipart upload endpoint for knowledge documents. The backend validates the filename and extension, then forwards the uploaded file directly to vector service:

```text
{VECTOR_SEARCH_HOST}/api/v1/upload_file
```

The backend does not save the source file, does not convert it to text, does not write a knowledge file row into SQLite, and does not add `metadata`.

Field name:

```text
file
```

Supported v1 formats:

```text
txt, md, pdf, docx, xls, xlsx
```

Response:

```json
{
  "code": 0,
  "status": "success",
  "file": {
    "filename": "guide.pdf",
    "file_id": "vector-generated-file-id",
    "task_id": "vector-task-id"
  }
}
```

### `GET /api/knowledge/files`

Lists files currently present in the vector database. The backend derives Qdrant HTTP URL from `VECTOR_SEARCH_HOST` by using the same scheme and host with port `6335`, then scrolls the `documents` collection and deduplicates by `file_id`.

Response:

```json
{
  "code": 0,
  "status": "success",
  "total": 1,
  "files": [
    {
      "file_id": "1783409846_manual.docx",
      "display_name": "manual.docx",
      "file_type": "docx",
      "file_size": 120,
      "upload_time": "2026-07-07T10:00:00",
      "total_chunks": 3
    }
  ]
}
```

### `DELETE /api/knowledge/files/{file_id}`

Deletes all Qdrant points in the `documents` collection whose payload `file_id` exactly matches `{file_id}`. The backend derives the Qdrant HTTP URL from `VECTOR_SEARCH_HOST` by using the same scheme and host with port `6335`.

Response:

```json
{
  "code": 0,
  "status": "success",
  "file": {
    "file_id": "1783409846_manual.docx",
    "operation_id": 42
  }
}
```

### `GET /api/knowledge/tasks/{task_id}`

Proxies a single vector service task status lookup. The upload page uses this endpoint only for the task returned by the current upload.

Response:

```json
{
  "code": 0,
  "status": "success",
  "task": {
    "task_id": "vector-task-id",
    "status": "completed",
    "file_id": "vector-generated-file-id",
    "file_name": "guide.pdf",
    "chunks_count": 12,
    "message": "成功处理文件",
    "error": null,
    "terminal": true
  }
}
```

### `POST /api/reports`

External-client report endpoint. The client sends a `MEDIA:/path/to/file` string for a file that already exists on the backend host, plus a required `description`. The backend copies that local file into the inbox store and shows it in the Tampermonkey assistant's Report tab.

Plain text example:

```bash
curl -H "Content-Type: text/plain" \
  -H "X-Description: Daily sync report" \
  --data 'MEDIA:/home/reports/hello.html' \
  http://192.168.34.65:50091/api/reports
```

JSON example:

```bash
curl -H "Content-Type: application/json" \
  -d '{"media":"MEDIA:/home/reports/hello.html","description":"Daily sync report"}' \
  http://192.168.34.65:50091/api/reports
```

Returns a token and download URL:

```json
{
  "code": 0,
  "status": "success",
  "file": {
    "token": "...",
    "name": "hello.txt",
    "url": "http://192.168.34.65:50091/api/reports/.../content",
    "media_type": "text/plain",
    "size": 12,
    "created_at": 1781000000.0,
    "description": "Daily sync report"
  }
}
```

### `GET /api/reports`

Lists external-client uploads for the Tampermonkey Files tab.

### `GET /api/generated-files/{token}/content`

Downloads a previously uploaded or registered file.

The implementation only serves paths inside `FILE_STORE_DIR`.

### `GET /api/reports/{token}/content`

Downloads a file uploaded through `POST /api/reports`.

HTML files are served inline for browser viewing. Other files are served as downloads.

### `DELETE /api/reports/{token}`

Deletes a Report file and its metadata. The endpoint is idempotent: if another user already deleted the same token, the response is still successful with `deleted: false`.
