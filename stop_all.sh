#!/bin/bash
# Stop SweRex containers and session server
#
# Usage:
#   ./stop_all.sh              # Stop everything
#   ./stop_all.sh --keep-containers   # Only stop server, keep containers

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

KEEP_CONTAINERS=false
if [ "$1" = "--keep-containers" ]; then
    KEEP_CONTAINERS=true
fi

echo "=== Stopping SweRex Environment ==="

# Stop server
echo ""
echo "Stopping server..."
pkill -f "swerex_server" 2>/dev/null && echo "  Server stopped" || echo "  No server running"
pkill -f "start_swerex_server" 2>/dev/null || true

# Stop containers
if [ "$KEEP_CONTAINERS" = false ]; then
    echo ""
    echo "Stopping containers..."
    
    # Try docker-compose first
    if [ -f "docker-compose.yaml" ]; then
        docker compose -f docker-compose.yaml down 2>/dev/null && echo "  Containers stopped" || true
    fi
    
    # Also stop any stray containers
    STRAY=$(docker ps --filter "name=swerex" -q 2>/dev/null | wc -l)
    if [ "$STRAY" -gt 0 ]; then
        echo "  Stopping $STRAY stray containers..."
        docker ps --filter "name=swerex" -q | xargs -r docker stop 2>/dev/null || true
        docker ps --filter "name=swerex" -q -a | xargs -r docker rm 2>/dev/null || true
    fi
else
    echo ""
    echo "Keeping containers running (--keep-containers)"
fi

# Show status
echo ""
echo "=== Status ==="
RUNNING=$(docker ps --filter "name=swerex" -q 2>/dev/null | wc -l)
echo "Running containers: $RUNNING"
echo "PTY usage: $(cat /proc/sys/kernel/pty/nr)/$(cat /proc/sys/kernel/pty/max)"

SERVER_RUNNING=$(pgrep -f "swerex_server" 2>/dev/null | wc -l)
if [ "$SERVER_RUNNING" -gt 0 ]; then
    echo "Server: RUNNING"
else
    echo "Server: STOPPED"
fi

echo ""
echo "Done."
