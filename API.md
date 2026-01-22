# Bash Sandbox API

This document explains how to use the Bash Sandbox server to execute commands in isolated, stateful bash sessions.

## Overview

The server manages a pool of bash sessions across multiple Docker containers. Each session:
- Maintains state (working directory, environment variables, shell variables) across commands
- Is isolated from other sessions
- Gets automatically cleaned up after release

## Quick Example

```python
import aiohttp
import asyncio

async def main():
    async with aiohttp.ClientSession() as session:
        # 1. Acquire a session
        async with session.post("http://localhost:8180/session/acquire", json={}) as resp:
            data = await resp.json()
            session_id = data["session_id"]
        
        # 2. Execute commands (state persists between commands)
        async with session.post(
            f"http://localhost:8180/session/{session_id}/execute",
            json={"command": "cd /tmp && export MY_VAR=hello"}
        ) as resp:
            result = await resp.json()
        
        async with session.post(
            f"http://localhost:8180/session/{session_id}/execute",
            json={"command": "pwd && echo $MY_VAR"}
        ) as resp:
            result = await resp.json()
            print(result["stdout"])  # "/tmp\nhello\n"
        
        # 3. Release the session
        await session.post(f"http://localhost:8180/session/{session_id}/release")

asyncio.run(main())
```

## Endpoints

### `POST /session/acquire`

Acquire a bash session from the pool. If no session is immediately available, the request waits until one becomes available (up to the configured timeout).

**Request Body:**
```json
{
  "files": {
    "script.py": "cHJpbnQoImhlbGxvIik=",
    "data/input.txt": "c29tZSBkYXRh"
  },
  "startup_commands": [
    "pip install numpy",
    "cd /tmp/work"
  ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `files` | object | No | Files to write before session starts. Keys are paths (relative to working dir), values are base64-encoded content. |
| `startup_commands` | array | No | Commands to run after acquiring session (e.g., setup commands). |

**Response:**
```json
{
  "session_id": "a1b2c3d4e5f6"
}
```

**Errors:**
- `503 Service Unavailable` - No session available within timeout

---

### `POST /session/{session_id}/execute`

Execute a bash command in an acquired session. State (working directory, environment variables, shell variables) persists between commands.

**URL Parameters:**
| Parameter | Description |
|-----------|-------------|
| `session_id` | Session ID returned from `/session/acquire` |

**Request Body:**
```json
{
  "command": "echo hello && pwd",
  "timeout": 30.0
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `command` | string | Yes | - | Bash command to execute |
| `timeout` | float | No | 30.0 | Command timeout in seconds |

**Response:**
```json
{
  "status": "Success",
  "stdout": "hello\n/tmp/work\n",
  "stderr": "",
  "return_code": 0
}
```

| Field | Description |
|-------|-------------|
| `status` | `"Success"` or `"Failed"` |
| `stdout` | Standard output from command |
| `stderr` | Standard error from command |
| `return_code` | Exit code (0 = success) |

**Errors:**
- `404 Not Found` - Session ID not found
- `400 Bad Request` - Session not in use (already released)

---

### `POST /session/{session_id}/release`

Release a session back to the pool. The session will be cleaned up in the background and made available for other requests.

**URL Parameters:**
| Parameter | Description |
|-----------|-------------|
| `session_id` | Session ID to release |

**Response:**
```json
{
  "status": "released"
}
```

**Notes:**
- Release is instant (cleanup happens asynchronously)
- Safe to call multiple times (idempotent)
- Always release sessions when done to avoid pool exhaustion

---

### `GET /health`

Get server health status.

**Response:**
```json
{
  "status": "healthy",
  "total_sessions": 1024,
  "available_sessions": 950,
  "in_use_sessions": 50,
  "cleaning_sessions": 24,
  "broken_sessions": 0,
  "healthy_containers": 128,
  "unhealthy_containers": 0
}
```

| Field | Description |
|-------|-------------|
| `status` | `"healthy"`, `"degraded"`, or `"unhealthy"` |
| `total_sessions` | Total sessions across all containers |
| `available_sessions` | Sessions ready to be acquired |
| `in_use_sessions` | Sessions currently acquired |
| `cleaning_sessions` | Sessions being cleaned after release |
| `broken_sessions` | Failed sessions awaiting recreation |
| `healthy_containers` | Number of responding containers |
| `unhealthy_containers` | Number of unresponsive containers |

---

## Session Lifecycle

```
   ┌─────────────────────────────────────────────────────────┐
   │                                                         │
   ▼                                                         │
AVAILABLE ──acquire──► IN_USE ──release──► CLEANING ────────┘
   ▲                      │                    │
   │                      │                    │
   │                      ▼                    ▼
   └─────────────────  BROKEN ◄───────── (on error)
         (recreated)
```

1. **AVAILABLE**: Session is ready to be acquired
2. **IN_USE**: Session is acquired by a client
3. **CLEANING**: Session released, cleanup in progress
4. **BROKEN**: Session failed, will be recreated

---

## State Persistence

Within a session, state persists across commands:

```bash
# Command 1
cd /tmp
export MY_VAR="hello"
MY_LOCAL="world"

# Command 2 (state persists)
echo $PWD        # /tmp
echo $MY_VAR     # hello
echo $MY_LOCAL   # world
```

State is **not** shared between sessions or between different `acquire`/`release` cycles.

---

## File Operations

### Writing Files on Acquire

```python
import base64

files = {
    "script.py": base64.b64encode(b'print("hello")').decode(),
    "data/input.txt": base64.b64encode(b'line1\nline2').decode(),
}

async with session.post(
    "http://localhost:8180/session/acquire",
    json={"files": files}
) as resp:
    data = await resp.json()
```

Files are written to the session's working directory (`/tmp/s/<session_id>/`).

### Reading Files via Commands

```python
# Execute command to read file
async with session.post(
    f"http://localhost:8180/session/{session_id}/execute",
    json={"command": "cat output.txt"}
) as resp:
    result = await resp.json()
    content = result["stdout"]
```

---

## Error Handling

### Command Failures

Non-zero exit codes return `status: "Failed"`:

```python
result = await execute(session_id, "exit 1")
# {
#   "status": "Failed",
#   "stdout": "",
#   "stderr": "",
#   "return_code": 1
# }
```

### Timeouts

Commands that exceed the timeout return an error:

```python
result = await execute(session_id, "sleep 60", timeout=5)
# {
#   "status": "Failed",
#   "stdout": "",
#   "stderr": "Command timed out after 5.0 seconds",
#   "return_code": -1
# }
```

### Session Not Found

```json
{
  "detail": "Session not found: abc123"
}
```

---

## Best Practices

1. **Always release sessions** - Use try/finally or context managers
2. **Set appropriate timeouts** - Prevent hanging commands
3. **Check return codes** - Don't assume commands succeed
4. **Use startup_commands for setup** - Install dependencies on acquire
5. **Batch related commands** - Reduce HTTP overhead

### Python Client Pattern

```python
class BashSession:
    def __init__(self, server_url="http://localhost:8180"):
        self.server_url = server_url
        self.session_id = None
        self.http = None
    
    async def __aenter__(self):
        self.http = aiohttp.ClientSession()
        async with self.http.post(f"{self.server_url}/session/acquire", json={}) as resp:
            data = await resp.json()
            self.session_id = data["session_id"]
        return self
    
    async def __aexit__(self, *args):
        if self.session_id:
            await self.http.post(f"{self.server_url}/session/{self.session_id}/release")
        await self.http.close()
    
    async def run(self, command: str, timeout: float = 30) -> dict:
        async with self.http.post(
            f"{self.server_url}/session/{self.session_id}/execute",
            json={"command": command, "timeout": timeout}
        ) as resp:
            return await resp.json()

# Usage
async with BashSession() as bash:
    result = await bash.run("echo hello")
    print(result["stdout"])
```

---

## Rate Limits & Capacity

| Resource | Default | Environment Variable |
|----------|---------|---------------------|
| Total sessions | 1024 | Containers × `SWEREX_SESSIONS_PER_CONTAINER` |
| Acquire timeout | 120s | `SWEREX_ACQUIRE_TIMEOUT` |
| Command timeout | 30s | Per-request `timeout` field |
| Cleanup workers | 128 | `SWEREX_CLEANUP_WORKERS` |

If all sessions are in use, new `acquire` requests wait in a queue until a session becomes available or the timeout expires.
