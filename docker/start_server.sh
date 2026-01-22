#!/bin/bash
# Start SweRex Session Server
#
# Usage:
#   ./start_server.sh                    # Start with defaults
#   ./start_server.sh 128 8 8180         # 128 containers, 8 sessions each, port 8180
#
# Environment variables:
#   SWEREX_AUTH_TOKEN       - Auth token for containers (default: test-token)
#   SWEREX_CLEANUP_WORKERS  - Number of cleanup workers (default: 128)
#   SWEREX_ACQUIRE_TIMEOUT  - Max wait time for session (default: 120)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

NUM_CONTAINERS="${1:-128}"
SESSIONS_PER_CONTAINER="${2:-8}"
SERVER_PORT="${3:-8180}"

# Configuration
export SWEREX_AUTH_TOKEN="${SWEREX_AUTH_TOKEN:-test-token}"
export SWEREX_CLEANUP_WORKERS="${SWEREX_CLEANUP_WORKERS:-128}"
export SWEREX_ACQUIRE_TIMEOUT="${SWEREX_ACQUIRE_TIMEOUT:-120}"
export SWEREX_SESSIONS_PER_CONTAINER="$SESSIONS_PER_CONTAINER"

echo "=== Starting SweRex Session Server ==="
echo "Containers: $NUM_CONTAINERS"
echo "Sessions per container: $SESSIONS_PER_CONTAINER"
echo "Total sessions: $((NUM_CONTAINERS * SESSIONS_PER_CONTAINER))"
echo "Server port: $SERVER_PORT"
echo "Cleanup workers: $SWEREX_CLEANUP_WORKERS"
echo ""

# Generate endpoints
ENDPOINTS=$(python3 -c "print(','.join([f'http://localhost:{18000+i}' for i in range($NUM_CONTAINERS)]))")
export SWEREX_ENDPOINTS="$ENDPOINTS"

# Kill any existing server
pkill -f "swerex_server" 2>/dev/null || true
sleep 1

# Create startup script
cat > /tmp/start_swerex_server.py << 'SCRIPT'
import os
import sys

project_root = os.environ.get('PROJECT_ROOT', '.')
sys.path.insert(0, project_root)

import importlib.util
spec = importlib.util.spec_from_file_location('swerex_server', 
    os.path.join(project_root, 'verl/experimental/agent_loop/swerex_server.py'))
swerex_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(swerex_server)

import uvicorn
port = int(os.environ.get('SERVER_PORT', 8180))
uvicorn.run(swerex_server.app, host='127.0.0.1', port=port, log_level='info')
SCRIPT

# Start server
export PROJECT_ROOT="$PROJECT_ROOT"
export SERVER_PORT="$SERVER_PORT"

LOG_FILE="/tmp/swerex_server.log"
echo "Starting server (log: $LOG_FILE)..."
python3 /tmp/start_swerex_server.py > "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "Server PID: $SERVER_PID"

# Wait for server to be ready
echo ""
echo "Waiting for server to initialize..."
for i in {1..120}; do
    RESP=$(curl -s "http://localhost:$SERVER_PORT/health" 2>&1)
    if echo "$RESP" | grep -q "total_sessions"; then
        SESSIONS=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['total_sessions'])")
        CONTAINERS=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['healthy_containers'])")
        echo ""
        echo "=== Server Ready ==="
        echo "Sessions: $SESSIONS"
        echo "Healthy containers: $CONTAINERS"
        echo "URL: http://localhost:$SERVER_PORT"
        echo "Health: http://localhost:$SERVER_PORT/health"
        echo "PID: $SERVER_PID"
        exit 0
    fi
    if [ $((i % 15)) -eq 0 ]; then
        echo "  Still initializing... (${i}s)"
    fi
    sleep 1
done

echo "ERROR: Server failed to start within 120s"
echo "Check log: $LOG_FILE"
tail -20 "$LOG_FILE"
exit 1
