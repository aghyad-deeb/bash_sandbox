#!/bin/bash
# Stop SweRex containers and session server
#
# Usage:
#   ./stop.sh                    # Stop everything (server, containers, tmux)
#   ./stop.sh --keep-containers  # Only stop server, keep containers
#   ./stop.sh --keep-tmux        # Keep tmux session running
#   ./stop.sh --force            # Force kill everything immediately

# Don't use set -e - we want graceful handling of failures
# set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Files
PID_FILE="/tmp/swerex_server.pid"
STATE_FILE="/tmp/swerex_state"
LOG_FILE="/tmp/swerex_server.log"
STARTUP_SCRIPT="/tmp/start_swerex_server.py"
TMUX_SESSION="sandbox"

# Parse arguments
KEEP_CONTAINERS=false
FORCE_KILL=false
KEEP_TMUX=false
for arg in "$@"; do
    case $arg in
        --keep-containers)
            KEEP_CONTAINERS=true
            ;;
        --force)
            FORCE_KILL=true
            ;;
        --keep-tmux)
            KEEP_TMUX=true
            ;;
    esac
done

echo "=== Stopping SweRex Environment ==="

# =============================================================================
# STOP SERVER
# =============================================================================

echo ""
echo "Stopping server..."

stop_server() {
    local stopped=false
    
    # Method 1: Use PID file (most reliable)
    if [ -f "$PID_FILE" ]; then
        SERVER_PID=$(cat "$PID_FILE")
        if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
            echo "  Found server process (PID: $SERVER_PID)"
            
            if [ "$FORCE_KILL" = true ]; then
                kill -9 "$SERVER_PID" 2>/dev/null
                echo "  Force killed server"
            else
                # Graceful shutdown
                kill "$SERVER_PID" 2>/dev/null
                
                # Wait for process to stop (max 10 seconds)
                for i in {1..10}; do
                    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
                        echo "  Server stopped gracefully"
                        stopped=true
                        break
                    fi
                    sleep 1
                done
                
                # Force kill if still running
                if [ "$stopped" = false ] && kill -0 "$SERVER_PID" 2>/dev/null; then
                    echo "  Server didn't stop gracefully, force killing..."
                    kill -9 "$SERVER_PID" 2>/dev/null
                    sleep 1
                    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
                        echo "  Server stopped"
                        stopped=true
                    else
                        echo "  WARNING: Could not kill server process"
                    fi
                fi
            fi
        else
            echo "  PID file exists but process not running"
        fi
        rm -f "$PID_FILE"
    fi
    
    # Method 2: Use pkill as fallback
    if pgrep -f "swerex_server" > /dev/null 2>&1; then
        echo "  Found additional swerex_server processes"
        if [ "$FORCE_KILL" = true ]; then
            pkill -9 -f "swerex_server" 2>/dev/null || true
        else
            pkill -f "swerex_server" 2>/dev/null || true
            sleep 2
            # Force kill if still running
            if pgrep -f "swerex_server" > /dev/null 2>&1; then
                pkill -9 -f "swerex_server" 2>/dev/null || true
            fi
        fi
        echo "  Stopped additional processes"
        stopped=true
    fi
    
    # Also kill startup script process
    pkill -f "start_swerex_server" 2>/dev/null || true
    
    if [ "$stopped" = false ]; then
        if ! pgrep -f "swerex_server" > /dev/null 2>&1; then
            echo "  No server running"
        fi
    fi
    
    # Clean up files
    rm -f "$STARTUP_SCRIPT"
    rm -f "$STATE_FILE"
}

stop_server

# =============================================================================
# STOP CONTAINERS
# =============================================================================

if [ "$KEEP_CONTAINERS" = false ]; then
    echo ""
    echo "Stopping containers..."
    
    # Try docker-compose first (cleanest method)
    if [ -f "docker-compose.yaml" ]; then
        if [ "$FORCE_KILL" = true ]; then
            docker compose -f docker-compose.yaml down --remove-orphans -t 1 2>/dev/null && echo "  Containers stopped" || true
        else
            docker compose -f docker-compose.yaml down --remove-orphans 2>/dev/null && echo "  Containers stopped" || true
        fi
    fi
    
    # Also stop any stray containers not managed by compose
    STRAY=$(docker ps --filter "name=swerex" -q 2>/dev/null | wc -l)
    if [ "$STRAY" -gt 0 ]; then
        echo "  Stopping $STRAY additional containers..."
        if [ "$FORCE_KILL" = true ]; then
            docker ps --filter "name=swerex" -q | xargs -r docker kill 2>/dev/null || true
        else
            docker ps --filter "name=swerex" -q | xargs -r docker stop -t 5 2>/dev/null || true
        fi
        docker ps --filter "name=swerex" -q -a | xargs -r docker rm -f 2>/dev/null || true
        echo "  Cleaned up stray containers"
    fi
else
    echo ""
    echo "Keeping containers running (--keep-containers)"
fi

# =============================================================================
# STOP TMUX SESSION
# =============================================================================

if [ "$KEEP_TMUX" = false ]; then
    if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
        echo ""
        echo "Stopping tmux session '$TMUX_SESSION'..."
        tmux kill-session -t "$TMUX_SESSION" 2>/dev/null && echo "  Tmux session stopped" || true
    fi
fi

# =============================================================================
# SHOW STATUS
# =============================================================================

echo ""
echo "=== Status ==="

# Container count
RUNNING=$(docker ps --filter "name=swerex" -q 2>/dev/null | wc -l)
echo "Running containers: $RUNNING"

# PTY usage (if available)
if [ -f /proc/sys/kernel/pty/nr ] && [ -f /proc/sys/kernel/pty/max ]; then
    echo "PTY usage: $(cat /proc/sys/kernel/pty/nr)/$(cat /proc/sys/kernel/pty/max)"
fi

# Server status
if pgrep -f "swerex_server" > /dev/null 2>&1; then
    echo "Server: RUNNING (WARNING: server still running!)"
else
    echo "Server: STOPPED"
fi

# Tmux session status
if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    echo "Tmux session '$TMUX_SESSION': RUNNING"
else
    echo "Tmux session '$TMUX_SESSION': STOPPED"
fi

# Log file info
if [ -f "$LOG_FILE" ]; then
    echo "Last log: $LOG_FILE ($(wc -l < "$LOG_FILE") lines)"
fi

echo ""
echo "Done."
