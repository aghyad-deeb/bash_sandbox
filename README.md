# Bash Sandbox

A scalable, stateful bash execution server for AI agents. Provides persistent bash sessions in isolated Docker containers with automatic cleanup and session pooling.

## Features

- **Stateful Sessions**: Bash sessions maintain state (working directory, environment variables, shell variables) across commands
- **Session Pooling**: Pre-initialized session pool for fast acquisition
- **Automatic Cleanup**: Background workers clean sessions after release
- **Health Monitoring**: Automatic health checks and session recreation
- **Horizontal Scaling**: Support for multiple containers with load balancing
- **HTTP API**: Simple REST API for session management

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Session Server                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │   Acquire   │  │   Execute   │  │   Release   │         │
│  └─────────────┘  └─────────────┘  └─────────────┘         │
│         │                │                │                 │
│         ▼                ▼                ▼                 │
│  ┌─────────────────────────────────────────────────┐       │
│  │              Session Pool (1024+)                │       │
│  └─────────────────────────────────────────────────┘       │
│         │                │                │                 │
└─────────┼────────────────┼────────────────┼─────────────────┘
          ▼                ▼                ▼
   ┌──────────┐     ┌──────────┐     ┌──────────┐
   │Container1│     │Container2│     │Container3│  ...
   │ 8 sessions│     │ 8 sessions│     │ 8 sessions│
   └──────────┘     └──────────┘     └──────────┘
```

## Quick Start

### 1. Build the sandbox container image

```bash
docker build -t swerex-sandbox:latest -f docker/Dockerfile.sandbox .
```

### 2. Start containers and server

```bash
cd docker

# Start everything (128 containers, 1024 sessions)
./start_all.sh

# Or with custom settings
./start_all.sh 64 8 8080  # 64 containers, 8 sessions each, port 8080
```

### 3. Use the API

```bash
# Check health
curl http://localhost:8180/health

# Acquire a session
curl -X POST http://localhost:8180/session/acquire -H "Content-Type: application/json" -d '{}'
# Returns: {"session_id": "abc123"}

# Execute a command
curl -X POST http://localhost:8180/session/abc123/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "echo hello", "timeout": 30}'
# Returns: {"status": "Success", "stdout": "hello\n", "stderr": "", "return_code": 0}

# Release the session
curl -X POST http://localhost:8180/session/abc123/release
```

### 4. Stop everything

```bash
./stop_all.sh
```

## API Reference

### `POST /session/acquire`

Acquire a session from the pool. Waits if no session is immediately available.

**Request Body:**
```json
{
  "files": {"filename.py": "base64_encoded_content"},  // optional
  "startup_commands": ["pip install numpy"]             // optional
}
```

**Response:**
```json
{"session_id": "abc123def456"}
```

### `POST /session/{session_id}/execute`

Execute a command in an acquired session.

**Request Body:**
```json
{
  "command": "echo hello",
  "timeout": 30.0
}
```

**Response:**
```json
{
  "status": "Success",
  "stdout": "hello\n",
  "stderr": "",
  "return_code": 0
}
```

### `POST /session/{session_id}/release`

Release a session back to the pool.

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

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `SWEREX_ENDPOINTS` | (required) | Comma-separated container URLs |
| `SWEREX_SESSIONS_PER_CONTAINER` | `8` | Sessions per container |
| `SWEREX_AUTH_TOKEN` | `default-token` | Auth token for containers |
| `SWEREX_SERVER_HOST` | `127.0.0.1` | Server bind host |
| `SWEREX_SERVER_PORT` | `8080` | Server port |
| `SWEREX_CLEANUP_WORKERS` | `128` | Parallel cleanup workers |
| `SWEREX_ACQUIRE_TIMEOUT` | `120` | Max wait time for session (seconds) |

## Performance

Tested with 10,000 workers, 128 containers, 1024 sessions:

| Metric | Value |
|--------|-------|
| Duration | 100s |
| Throughput | 500 commands/sec |
| Workers/sec | 100 |
| Success rate | 99.9% |
| Acquire time (median) | 75ms |
| Execute time (median) | 260ms |

## Development

### Run tests

```bash
# Unit tests
pytest tests/test_swerex_server.py

# Stress test (requires running containers)
python tests/stress_test_client.py --workers 1000 --commands 5
```

### Generate Docker Compose for N containers

```bash
python docker/generate_compose.py --containers 128
```

## License

Apache 2.0
