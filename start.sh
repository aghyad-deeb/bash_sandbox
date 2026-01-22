#!/bin/bash
# Start Bash Sandbox (containers + server)
#
# Usage:
#   ./start.sh                        # Start in tmux session "sandbox" (128 containers, port 8180)
#   ./start.sh 64                     # Start 64 containers
#   ./start.sh 128 8 8180             # 128 containers, 8 sessions each, port 8180
#   ./start.sh --no-tmux              # Start without tmux (background mode)
#   ./start.sh --attach               # Start and attach to tmux session
#
# Environment variables:
#   SWEREX_AUTH_TOKEN       - Auth token for containers (default: default-token)
#   SWEREX_CLEANUP_WORKERS  - Number of cleanup workers (default: 128)
#   SWEREX_ACQUIRE_TIMEOUT  - Max wait time for session (default: 120)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Parse arguments
USE_TMUX=true
ATTACH_TMUX=false
POSITIONAL_ARGS=()

for arg in "$@"; do
    case $arg in
        --no-tmux)
            USE_TMUX=false
            ;;
        --attach)
            ATTACH_TMUX=true
            ;;
        *)
            POSITIONAL_ARGS+=("$arg")
            ;;
    esac
done

NUM_CONTAINERS="${POSITIONAL_ARGS[0]:-128}"
SESSIONS_PER_CONTAINER="${POSITIONAL_ARGS[1]:-8}"
SERVER_PORT="${POSITIONAL_ARGS[2]:-8180}"

# PID and state files
PID_FILE="/tmp/swerex_server.pid"
STATE_FILE="/tmp/swerex_state"
LOG_FILE="/tmp/swerex_server.log"
STARTUP_SCRIPT="/tmp/start_swerex_server.py"
TMUX_SESSION="sandbox"

# =============================================================================
# TMUX MODE: Start in tmux session
# =============================================================================

if [ "$USE_TMUX" = true ] && [ -z "$TMUX" ] && [ -z "$SWEREX_IN_TMUX" ]; then
    echo "============================================================"
    echo "                    BASH SANDBOX                            "
    echo "============================================================"
    echo ""
    echo "Starting in tmux session: $TMUX_SESSION"
    echo ""
    
    # Kill existing tmux session if it exists
    tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
    
    # Create new tmux session
    tmux new-session -d -s "$TMUX_SESSION" -x 180 -y 50
    
    # Set environment for the tmux session
    tmux send-keys -t "$TMUX_SESSION" "cd '$SCRIPT_DIR' && SWEREX_IN_TMUX=1 ./start.sh --no-tmux $NUM_CONTAINERS $SESSIONS_PER_CONTAINER $SERVER_PORT" Enter
    
    # Wait for server to start
    echo "Waiting for server to initialize..."
    for i in {1..180}; do
        if curl -s "http://localhost:$SERVER_PORT/health" 2>/dev/null | grep -q "total_sessions"; then
            echo ""
            echo "============================================================"
            echo "          SERVER STARTED SUCCESSFULLY!                      "
            echo "============================================================"
            echo ""
            
            # Parse health info
            RESP=$(curl -s "http://localhost:$SERVER_PORT/health")
            SESSIONS=$(echo "$RESP" | python3.12 -c "import sys,json; print(json.load(sys.stdin).get('total_sessions', 0))" 2>/dev/null || echo "?")
            HEALTHY=$(echo "$RESP" | python3.12 -c "import sys,json; print(json.load(sys.stdin).get('healthy_containers', 0))" 2>/dev/null || echo "?")
            
            echo "  Server URL:    http://localhost:$SERVER_PORT"
            echo "  Sessions:      $SESSIONS total"
            echo "  Containers:    $HEALTHY healthy"
            echo ""
            echo "  Tmux session:  $TMUX_SESSION"
            echo "  Attach:        tmux attach -t $TMUX_SESSION"
            echo "  Stop:          ./stop.sh"
            echo ""
            echo "============================================================"
            
            # Split tmux window and start health monitor
            tmux split-window -t "$TMUX_SESSION" -v -l 15
            tmux send-keys -t "$TMUX_SESSION" "cd '$SCRIPT_DIR' && watch -n 5 -c './check_health.py --brief 2>/dev/null || echo \"Checking...\"'" Enter
            
            # Select the main pane (server logs)
            tmux select-pane -t "$TMUX_SESSION:0.0"
            
            if [ "$ATTACH_TMUX" = true ]; then
                echo "Attaching to tmux session..."
                exec tmux attach -t "$TMUX_SESSION"
            fi
            
            exit 0
        fi
        if [ $((i % 15)) -eq 0 ]; then
            echo "  Still initializing... (${i}s)"
        fi
        sleep 1
    done
    
    echo ""
    echo "ERROR: Server did not start within 180 seconds"
    echo "Check tmux session: tmux attach -t $TMUX_SESSION"
    exit 1
fi

# =============================================================================
# ACTUAL STARTUP (runs inside tmux or with --no-tmux)
# =============================================================================

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
# CLEANUP FUNCTION
# =============================================================================

cleanup_on_error() {
    echo ""
    echo "ERROR: Start failed. Cleaning up..."
    # Kill server if it started
    if [ -f "$PID_FILE" ]; then
        kill $(cat "$PID_FILE") 2>/dev/null || true
        rm -f "$PID_FILE"
    fi
    # Clean up temp files
    rm -f "$STARTUP_SCRIPT"
    exit 1
}

trap cleanup_on_error ERR

# =============================================================================
# STEP 0: STOP ANY EXISTING INSTANCES
# =============================================================================

# Stop any existing server using PID file
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping existing server (PID: $OLD_PID)..."
        kill "$OLD_PID" 2>/dev/null || true
        sleep 2
        # Force kill if still running
        if kill -0 "$OLD_PID" 2>/dev/null; then
            kill -9 "$OLD_PID" 2>/dev/null || true
        fi
    fi
    rm -f "$PID_FILE"
fi

# Also kill any stray processes
pkill -f "swerex_server" 2>/dev/null || true
pkill -f "start_swerex_server" 2>/dev/null || true
sleep 1

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
docker compose -f "$COMPOSE_FILE" down --remove-orphans 2>/dev/null || true

# Start containers
echo "Starting $NUM_CONTAINERS containers..."
if ! docker compose -f "$COMPOSE_FILE" up -d; then
    echo "ERROR: Failed to start containers"
    exit 1
fi

# Wait for containers to be ready
echo "Waiting for containers to start..."
sleep 10

# Check container status
RUNNING=$(docker ps --filter "name=swerex" --format "{{.Names}}" | wc -l)
echo ""
echo "Containers running: $RUNNING"

if [ "$RUNNING" -eq 0 ]; then
    echo "ERROR: No containers started!"
    exit 1
fi

# Test first container with retry
MAX_RETRIES=10
for i in $(seq 1 $MAX_RETRIES); do
    if curl -s -H "X-API-Key: ${SWEREX_AUTH_TOKEN:-default-token}" "http://localhost:18000/is_alive" 2>/dev/null | grep -q "true"; then
        echo "Container health: OK"
        break
    fi
    if [ $i -eq $MAX_RETRIES ]; then
        echo "Container health: WAITING (may need more time)"
    else
        sleep 2
    fi
done

# =============================================================================
# STEP 2: START SERVER
# =============================================================================

echo ""
echo "============================================================"
echo "Step 2: Starting Session Server"
echo "============================================================"

# Configuration
export SWEREX_AUTH_TOKEN="${SWEREX_AUTH_TOKEN:-default-token}"
export SWEREX_CLEANUP_WORKERS="${SWEREX_CLEANUP_WORKERS:-128}"
export SWEREX_ACQUIRE_TIMEOUT="${SWEREX_ACQUIRE_TIMEOUT:-120}"
export SWEREX_SESSIONS_PER_CONTAINER="$SESSIONS_PER_CONTAINER"

# Generate endpoints
ENDPOINTS=$(python3.12 -c "print(','.join([f'http://localhost:{18000+i}' for i in range($NUM_CONTAINERS)]))")
export SWEREX_ENDPOINTS="$ENDPOINTS"

# Create startup script
cat > "$STARTUP_SCRIPT" << 'SCRIPT'
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

echo "Starting server on port $SERVER_PORT..."
echo "Log file: $LOG_FILE"

# Clear old log
> "$LOG_FILE"

# Run server in foreground if in tmux, otherwise background
if [ -n "$SWEREX_IN_TMUX" ]; then
    # Save PID file before running
    echo "$$" > "$PID_FILE"
    
    # Save state
    cat > "$STATE_FILE" << EOF
SERVER_PID=$$
SERVER_PORT=$SERVER_PORT
NUM_CONTAINERS=$NUM_CONTAINERS
LOG_FILE=$LOG_FILE
STARTED_AT=$(date -Iseconds)
EOF
    
    echo ""
    echo "Server starting... (logs will appear below)"
    echo "============================================================"
    echo ""
    
    # Run in foreground - uvicorn will handle signals
    exec python3.12 "$STARTUP_SCRIPT" 2>&1 | tee "$LOG_FILE"
else
    # Background mode
    python3.12 "$STARTUP_SCRIPT" > "$LOG_FILE" 2>&1 &
    SERVER_PID=$!
    
    # Save PID immediately
    echo "$SERVER_PID" > "$PID_FILE"
    
    # Verify process started
    sleep 1
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "ERROR: Server process died immediately!"
        echo "Check log: $LOG_FILE"
        echo ""
        echo "Last 20 lines of log:"
        tail -20 "$LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
    
    # Save state
    cat > "$STATE_FILE" << EOF
SERVER_PID=$SERVER_PID
SERVER_PORT=$SERVER_PORT
NUM_CONTAINERS=$NUM_CONTAINERS
LOG_FILE=$LOG_FILE
STARTED_AT=$(date -Iseconds)
EOF
    
    # Wait for server to be ready
    echo "Waiting for server to initialize..."
    set +e
    trap - ERR
    for i in {1..180}; do
        # Check if process is still running
        if ! kill -0 "$SERVER_PID" 2>/dev/null; then
            echo ""
            echo "ERROR: Server process died during initialization!"
            echo "Check log: $LOG_FILE"
            echo ""
            echo "Last 30 lines of log:"
            tail -30 "$LOG_FILE"
            rm -f "$PID_FILE"
            exit 1
        fi
        
        RESP=$(curl -s "http://localhost:$SERVER_PORT/health" 2>&1)
        if echo "$RESP" | grep -q "total_sessions"; then
            SESSIONS=$(echo "$RESP" | python3.12 -c "import sys,json; print(json.load(sys.stdin).get('total_sessions', 0))" 2>/dev/null || echo "?")
            HEALTHY=$(echo "$RESP" | python3.12 -c "import sys,json; print(json.load(sys.stdin).get('healthy_containers', 0))" 2>/dev/null || echo "?")
            STATUS=$(echo "$RESP" | python3.12 -c "import sys,json; print(json.load(sys.stdin).get('status', 'unknown'))" 2>/dev/null || echo "unknown")
            
            echo ""
            echo "============================================================"
            echo "          SERVER STARTED SUCCESSFULLY!                      "
            echo "============================================================"
            echo ""
            echo "  Status:        $STATUS"
            echo "  Server URL:    http://localhost:$SERVER_PORT"
            echo "  Health check:  http://localhost:$SERVER_PORT/health"
            echo "  Server PID:    $SERVER_PID"
            echo ""
            echo "  Sessions:      $SESSIONS total"
            echo "  Containers:    $HEALTHY healthy"
            echo ""
            echo "  To stop:       ./stop.sh"
            echo "  Health check:  ./check_health.py"
            echo ""
            echo "============================================================"
            
            rm -f "$STARTUP_SCRIPT"
            exit 0
        fi
        if [ $((i % 15)) -eq 0 ]; then
            echo "  Still initializing... (${i}s)"
        fi
        sleep 1
    done
    set -e
    
    echo ""
    echo "============================================================"
    echo "          ERROR: SERVER FAILED TO START                     "
    echo "============================================================"
    echo ""
    echo "Server did not respond within 180 seconds."
    echo "Check log: $LOG_FILE"
    echo ""
    echo "Last 30 lines of log:"
    tail -30 "$LOG_FILE"
    
    kill "$SERVER_PID" 2>/dev/null || true
    rm -f "$PID_FILE" "$STARTUP_SCRIPT"
    exit 1
fi
