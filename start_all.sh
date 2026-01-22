#!/bin/bash
# Start both SweRex containers and session server
#
# Usage:
#   ./start_all.sh                       # Start with defaults (128 containers)
#   ./start_all.sh 64                    # Start 64 containers
#   ./start_all.sh 128 8 8180            # 128 containers, 8 sessions each, port 8180

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NUM_CONTAINERS="${1:-128}"
SESSIONS_PER_CONTAINER="${2:-8}"
SERVER_PORT="${3:-8180}"

echo "============================================================"
echo "Starting SweRex Environment"
echo "============================================================"
echo ""
echo "Configuration:"
echo "  Containers: $NUM_CONTAINERS"
echo "  Sessions per container: $SESSIONS_PER_CONTAINER"
echo "  Total sessions: $((NUM_CONTAINERS * SESSIONS_PER_CONTAINER))"
echo "  Server port: $SERVER_PORT"
echo ""

# Step 1: Start containers
echo "============================================================"
echo "Step 1: Starting containers"
echo "============================================================"
"$SCRIPT_DIR/start_containers.sh" "$NUM_CONTAINERS"

echo ""
echo "============================================================"
echo "Step 2: Starting server"
echo "============================================================"
"$SCRIPT_DIR/start_server.sh" "$NUM_CONTAINERS" "$SESSIONS_PER_CONTAINER" "$SERVER_PORT"

echo ""
echo "============================================================"
echo "SweRex Environment Ready!"
echo "============================================================"
echo ""
echo "Server URL: http://localhost:$SERVER_PORT"
echo "Health check: curl http://localhost:$SERVER_PORT/health"
echo ""
echo "To stop: ./stop_all.sh"
