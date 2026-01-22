#!/bin/bash
# Start SweRex sandbox containers
#
# Usage:
#   ./start_containers.sh           # Start 128 containers (default)
#   ./start_containers.sh 64        # Start 64 containers

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

NUM_CONTAINERS="${1:-128}"
COMPOSE_FILE="docker-compose.yaml"

echo "=== Starting SweRex Containers ==="
echo "Containers: $NUM_CONTAINERS"
echo "Compose file: $COMPOSE_FILE"
echo ""

# Check if compose file exists
if [ ! -f "$COMPOSE_FILE" ]; then
    echo "ERROR: $COMPOSE_FILE not found"
    echo "Run: python generate_compose.py --containers $NUM_CONTAINERS"
    exit 1
fi

# Stop any existing containers
echo "Stopping existing containers..."
docker compose -f "$COMPOSE_FILE" down 2>/dev/null || true

# Start containers
echo "Starting containers..."
docker compose -f "$COMPOSE_FILE" up -d

# Wait for containers to be ready
echo ""
echo "Waiting for containers to start..."
sleep 10

# Check status
RUNNING=$(docker ps --filter "name=swerex" --format "{{.Names}}" | wc -l)
echo ""
echo "=== Status ==="
echo "Running containers: $RUNNING"
echo "PTY usage: $(cat /proc/sys/kernel/pty/nr)/$(cat /proc/sys/kernel/pty/max)"

# Test first container
if curl -s -H "X-API-Key: ${SWEREX_AUTH_TOKEN:-test-token}" "http://localhost:18000/is_alive" | grep -q "true"; then
    echo "Container health: OK"
else
    echo "Container health: WAITING (may need more time)"
fi

echo ""
echo "Containers ready on ports 18000-$((18000 + NUM_CONTAINERS - 1))"
