#!/bin/bash
# Start Bash Sandbox (containers + server)
#
# Usage:
#   ./start.sh                        # Start with defaults (128 containers, port 8180)
#   ./start.sh 64                     # Start 64 containers
#   ./start.sh 128 8 8180             # 128 containers, 8 sessions each, port 8180
#
# Environment variables:
#   SWEREX_AUTH_TOKEN       - Auth token for containers (default: test-token)
#   SWEREX_CLEANUP_WORKERS  - Number of cleanup workers (default: 128)
#   SWEREX_ACQUIRE_TIMEOUT  - Max wait time for session (default: 120)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

NUM_CONTAINERS="${1:-128}"
SESSIONS_PER_CONTAINER="${2:-8}"
SERVER_PORT="${3:-8180}"

echo "============================================================"
echo "                    BASH SANDBOX                            "
echo "============================================================"
echo ""
echo "Configuration:"
echo "  Containers: $NUM_CONTAINERS"
echo "  Sessions per container: $SESSIONS_PER_CONTAINER"
echo "  Total sessions: $((NUM_CONTAINERS * SESSIONS_PER_CONTAINER))"
echo "  Server port: $SERVER_PORT"
echo ""

# =============================================================================
# STEP 1: START CONTAINERS
# =============================================================================

echo "============================================================"
echo "Step 1: Starting Docker Containers"
echo "============================================================"

COMPOSE_FILE="docker-compose.yaml"

if [ ! -f "$COMPOSE_FILE" ]; then
    echo "ERROR: $COMPOSE_FILE not found"
    echo "Run: python generate_compose.py --containers $NUM_CONTAINERS"
    exit 1
fi

# Stop any existing containers
echo "Stopping existing containers..."
docker compose -f "$COMPOSE_FILE" down 2>/dev/null || true

# Start containers
echo "Starting $NUM_CONTAINERS containers..."
docker compose -f "$COMPOSE_FILE" up -d

# Wait for containers
echo "Waiting for containers to start..."
sleep 10

# Check container status
RUNNING=$(docker ps --filter "name=swerex" --format "{{.Names}}" | wc -l)
echo ""
echo "Containers running: $RUNNING"

# Test first container
if curl -s -H "X-API-Key: ${SWEREX_AUTH_TOKEN:-test-token}" "http://localhost:18000/is_alive" 2>/dev/null | grep -q "true"; then
    echo "Container health: OK"
else
    echo "Container health: WAITING (may need more time)"
fi

# =============================================================================
# STEP 2: START SERVER
# =============================================================================

echo ""
echo "============================================================"
echo "Step 2: Starting Session Server"
echo "============================================================"

# Configuration
export SWEREX_AUTH_TOKEN="${SWEREX_AUTH_TOKEN:-test-token}"
export SWEREX_CLEANUP_WORKERS="${SWEREX_CLEANUP_WORKERS:-128}"
export SWEREX_ACQUIRE_TIMEOUT="${SWEREX_ACQUIRE_TIMEOUT:-120}"
export SWEREX_SESSIONS_PER_CONTAINER="$SESSIONS_PER_CONTAINER"

# Generate endpoints
ENDPOINTS=$(python3 -c "print(','.join([f'http://localhost:{18000+i}' for i in range($NUM_CONTAINERS)]))")
export SWEREX_ENDPOINTS="$ENDPOINTS"

# Kill any existing server
pkill -f "swerex_server" 2>/dev/null || true
sleep 1

# Create startup script
cat > /tmp/start_swerex_server.py << SCRIPT
import os
import sys

script_dir = os.environ.get('SCRIPT_DIR', '.')
sys.path.insert(0, script_dir)

import importlib.util
spec = importlib.util.spec_from_file_location('swerex_server', 
    os.path.join(script_dir, 'swerex_server.py'))
swerex_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(swerex_server)

import uvicorn
port = int(os.environ.get('SERVER_PORT', 8180))
uvicorn.run(swerex_server.app, host='127.0.0.1', port=port, log_level='info')
SCRIPT

# Start server
export SCRIPT_DIR="$SCRIPT_DIR"
export SERVER_PORT="$SERVER_PORT"

LOG_FILE="/tmp/swerex_server.log"
echo "Starting server on port $SERVER_PORT..."
echo "Log file: $LOG_FILE"
python3 /tmp/start_swerex_server.py > "$LOG_FILE" 2>&1 &
SERVER_PID=$!

# Wait for server to be ready
echo "Waiting for server to initialize..."
for i in {1..120}; do
    RESP=$(curl -s "http://localhost:$SERVER_PORT/health" 2>&1)
    if echo "$RESP" | grep -q "total_sessions"; then
        SESSIONS=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['total_sessions'])")
        HEALTHY=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['healthy_containers'])")
        
        echo ""
        echo "============================================================"
        echo "                    READY!                                  "
        echo "============================================================"
        echo ""
        echo "Server URL:    http://localhost:$SERVER_PORT"
        echo "Health check:  http://localhost:$SERVER_PORT/health"
        echo "Server PID:    $SERVER_PID"
        echo ""
        echo "Sessions:      $SESSIONS"
        echo "Containers:    $HEALTHY healthy"
        echo ""
        echo "To stop: ./stop.sh"
        exit 0
    fi
    if [ $((i % 15)) -eq 0 ]; then
        echo "  Still initializing... (${i}s)"
    fi
    sleep 1
done

echo ""
echo "ERROR: Server failed to start within 120s"
echo "Check log: $LOG_FILE"
tail -20 "$LOG_FILE"
exit 1
