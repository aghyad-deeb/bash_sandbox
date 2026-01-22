# Copyright 2024 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
SweRex Session Server - Centralized session management for stateful bash execution.

This server manages a pool of SweRex containers and provides HTTP endpoints for:
- Acquiring sessions (with optional file setup and startup commands)
- Executing commands in sessions
- Releasing sessions back to the pool

Key features:
- Health-aware load balancing across containers
- Instant release with deferred cleanup
- Automatic session recreation on failure
- Background health monitoring

Usage:
    # Start with uvicorn
    uvicorn verl.experimental.agent_loop.swerex_server:app --host 127.0.0.1 --port 8080
    
    # Or run directly
    python -m verl.experimental.agent_loop.swerex_server

Environment variables:
    SWEREX_ENDPOINTS: Comma-separated container URLs (required)
    SWEREX_SESSIONS_PER_CONTAINER: Sessions per container (default: 8)
    SWEREX_AUTH_TOKEN: Auth token for containers (default: "default-token")
    SWEREX_SERVER_HOST: Server host (default: "127.0.0.1")
    SWEREX_SERVER_PORT: Server port (default: 8080)
"""

import asyncio
import base64
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from swerex.deployment.remote import RemoteDeployment
from swerex.runtime.abstract import BashAction, CreateBashSessionRequest, CloseSessionRequest

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =============================================================================
# Configuration
# =============================================================================

SWEREX_ENDPOINTS = os.getenv("SWEREX_ENDPOINTS", "")
SWEREX_SESSIONS_PER_CONTAINER = int(os.getenv("SWEREX_SESSIONS_PER_CONTAINER", "8"))
SWEREX_AUTH_TOKEN = os.getenv("SWEREX_AUTH_TOKEN", "default-token")
SWEREX_SERVER_HOST = os.getenv("SWEREX_SERVER_HOST", "127.0.0.1")
SWEREX_SERVER_PORT = int(os.getenv("SWEREX_SERVER_PORT", "8080"))
SWEREX_COMMAND_TIMEOUT = float(os.getenv("SWEREX_COMMAND_TIMEOUT", "30"))
SWEREX_HEALTH_CHECK_INTERVAL = float(os.getenv("SWEREX_HEALTH_CHECK_INTERVAL", "30"))
# Server-side rate limiting: max concurrent acquire requests being processed
# This is the key insight from SandboxFusion - don't try to handle 1000s of concurrent requests
SWEREX_MAX_CONCURRENT_ACQUIRE = int(os.getenv("SWEREX_MAX_CONCURRENT_ACQUIRE", "32"))

# =============================================================================
# Data Models
# =============================================================================


class SessionState(Enum):
    """State of a session in the pool."""
    AVAILABLE = "available"    # Ready to be acquired
    IN_USE = "in_use"          # Currently acquired by a client
    CLEANING = "cleaning"      # Released, cleanup in progress
    BROKEN = "broken"          # Failed, needs recreation


@dataclass
class Session:
    """A single bash session on a container."""
    id: str                    # Globally unique ID (UUID)
    container_id: str          # Which container this belongs to
    bash_session_name: str     # Name in swerex (e.g., "s_abc123")
    work_dir: str              # e.g., "/tmp/s/abc123"
    state: SessionState = SessionState.AVAILABLE
    acquired_at: Optional[float] = None


@dataclass
class Container:
    """A SweRex container with multiple sessions."""
    id: str                    # e.g., "c0"
    endpoint: str              # e.g., "http://localhost:18000"
    deployment: Optional[RemoteDeployment] = None
    sessions: dict = field(default_factory=dict)  # session_id -> Session
    healthy: bool = True
    consecutive_failures: int = 0


@dataclass
class CommandResult:
    """Result of a bash command execution."""
    status: str  # "Success" or "Failed"
    stdout: str
    stderr: str
    return_code: int
    files: Optional[dict[str, str]] = None  # filename -> base64 content


# =============================================================================
# Request/Response Schemas
# =============================================================================


class AcquireRequest(BaseModel):
    """Request to acquire a session."""
    files: Optional[dict[str, str]] = None  # filename -> base64 content
    startup_commands: Optional[list[str]] = None


class AcquireResponse(BaseModel):
    """Response with acquired session ID."""
    session_id: str


class ExecuteRequest(BaseModel):
    """Request to execute a command."""
    command: str
    timeout: float = 30.0
    fetch_files: Optional[list[str]] = None  # List of file paths to fetch after execution


class ExecuteResponse(BaseModel):
    """Response with command execution result."""
    status: str  # "Success" or "Failed"
    stdout: str
    stderr: str
    return_code: int
    files: Optional[dict[str, str]] = None  # filename -> base64 content


class HealthResponse(BaseModel):
    """Server health status."""
    status: str  # "healthy", "degraded", "unhealthy"
    total_sessions: int
    available_sessions: int
    in_use_sessions: int
    cleaning_sessions: int
    broken_sessions: int
    healthy_containers: int
    unhealthy_containers: int


# =============================================================================
# Session Manager
# =============================================================================


class SessionManager:
    """
    Manages containers and sessions. Designed for server use.
    
    Key design decisions:
    1. Instant release: cleanup is deferred to background queue
    2. Health-aware acquisition: skips unhealthy containers
    3. Load-balanced: prefers containers with more available sessions
    4. Self-healing: recreates broken sessions automatically
    """
    
    def __init__(
        self,
        endpoints: list[str],
        sessions_per_container: int = 8,
        auth_token: str = "default-token",
    ):
        """
        Initialize the session manager.
        
        Args:
            endpoints: List of container endpoint URLs
            sessions_per_container: Number of sessions to create per container
            auth_token: Auth token for connecting to containers
        """
        self.endpoints = endpoints
        self.sessions_per_container = sessions_per_container
        self.auth_token = auth_token
        
        # State
        self.containers: dict[str, Container] = {}  # container_id -> Container
        self.sessions: dict[str, Session] = {}      # session_id -> Session (flat index)
        
        # Synchronization
        self._lock = asyncio.Lock()
        self._session_available = asyncio.Condition(self._lock)  # Notified when sessions become available
        self._cleanup_queue: asyncio.Queue = asyncio.Queue()
        
        # Configuration
        self._acquire_timeout = float(os.getenv("SWEREX_ACQUIRE_TIMEOUT", "120"))  # Max wait time for session
        self._cleanup_workers = int(os.getenv("SWEREX_CLEANUP_WORKERS", "32"))  # Parallel cleanup workers
        self._max_concurrent_setup = int(os.getenv("SWEREX_MAX_CONCURRENT_SETUP", "32"))  # Limit concurrent session setups
        
        # Semaphore to limit concurrent session setup operations
        self._setup_semaphore = asyncio.Semaphore(self._max_concurrent_setup)
        
        # Background tasks
        self._background_tasks: list[asyncio.Task] = []
        self._shutdown = False
    
    async def initialize(self) -> None:
        """Connect to all containers and create initial sessions."""
        logger.info(f"Initializing SessionManager with {len(self.endpoints)} endpoints, "
                   f"{self.sessions_per_container} sessions each")
        
        # Limit concurrent container initialization to avoid overwhelming the system
        # Each container initialization makes multiple HTTP requests
        max_concurrent_init = int(os.getenv("SWEREX_MAX_CONCURRENT_INIT", "16"))
        semaphore = asyncio.Semaphore(max_concurrent_init)
        total_containers = len(self.endpoints)
        initialized_count = [0]  # Use list to allow mutation in nested function
        
        async def init_with_semaphore(container_id: str, endpoint: str):
            async with semaphore:
                result = await self._init_container(container_id, endpoint)
                initialized_count[0] += 1
                if initialized_count[0] % 10 == 0 or initialized_count[0] == total_containers:
                    logger.info(f"Container initialization progress: {initialized_count[0]}/{total_containers}")
                return result
        
        logger.info(f"Starting container initialization (max {max_concurrent_init} concurrent)")
        
        # Initialize containers with controlled concurrency
        init_tasks = []
        for i, endpoint in enumerate(self.endpoints):
            container_id = f"c{i}"
            init_tasks.append(init_with_semaphore(container_id, endpoint))
        
        results = await asyncio.gather(*init_tasks, return_exceptions=True)
        
        # Check results
        success_count = 0
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Failed to initialize container {i}: {result}")
            else:
                success_count += 1
        
        if success_count == 0:
            raise RuntimeError("Failed to initialize any containers")
        
        total_sessions = sum(len(c.sessions) for c in self.containers.values())
        logger.info(f"Initialized {success_count}/{len(self.endpoints)} containers "
                   f"with {total_sessions} total sessions")
        
        # Start background tasks
        self._background_tasks.append(asyncio.create_task(self._health_check_loop()))
        # Spawn multiple cleanup workers for parallel processing
        for i in range(self._cleanup_workers):
            self._background_tasks.append(asyncio.create_task(self._cleanup_loop()))
        logger.info(f"Started {self._cleanup_workers} cleanup workers")
        self._background_tasks.append(asyncio.create_task(self._recreation_loop()))
    
    async def _init_container(self, container_id: str, endpoint: str) -> None:
        """Initialize a single container and create its sessions."""
        logger.debug(f"Initializing container {container_id} at {endpoint}")
        # Parse endpoint
        if "://" in endpoint:
            url_part = endpoint.split("://")[1]
            protocol = endpoint.split("://")[0]
        else:
            url_part = endpoint
            protocol = "http"
        
        if ":" in url_part:
            host_part, port_str = url_part.rsplit(":", 1)
            port = int(port_str)
        else:
            host_part = url_part
            port = 8000
        
        host = f"{protocol}://{host_part}"
        
        # Create deployment
        deployment = RemoteDeployment(
            host=host,
            port=port,
            auth_token=self.auth_token,
        )
        
        await deployment.start()
        
        container = Container(
            id=container_id,
            endpoint=endpoint,
            deployment=deployment,
        )
        
        # Create sessions
        runtime = deployment.runtime
        for j in range(self.sessions_per_container):
            session_id = uuid4().hex[:12]
            bash_session_name = f"s_{session_id}"
            work_dir = f"/tmp/s/{session_id}"
            
            # Create bash session
            await runtime.create_session(
                CreateBashSessionRequest(session=bash_session_name)
            )
            
            # Create working directory
            await runtime.run_in_session(
                BashAction(session=bash_session_name, command=f"mkdir -p '{work_dir}'")
            )
            await runtime.run_in_session(
                BashAction(session=bash_session_name, command=f"cd '{work_dir}'")
            )
            
            session = Session(
                id=session_id,
                container_id=container_id,
                bash_session_name=bash_session_name,
                work_dir=work_dir,
                state=SessionState.AVAILABLE,
            )
            
            container.sessions[session_id] = session
            self.sessions[session_id] = session
        
        self.containers[container_id] = container
        logger.debug(f"Initialized container {container_id} at {endpoint} "
                    f"with {self.sessions_per_container} sessions")
    
    async def shutdown(self) -> None:
        """Shutdown the manager and close all connections."""
        logger.info("Shutting down SessionManager")
        self._shutdown = True
        
        # Cancel background tasks
        for task in self._background_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # Close all sessions and deployments
        for container in self.containers.values():
            if container.deployment:
                try:
                    # Close all bash sessions
                    runtime = container.deployment.runtime
                    for session in container.sessions.values():
                        try:
                            await runtime.close_session(
                                CloseSessionRequest(session=session.bash_session_name)
                            )
                        except Exception:
                            pass
                    
                    # Stop deployment
                    await container.deployment.stop()
                except Exception as e:
                    logger.debug(f"Error shutting down container {container.id}: {e}")
        
        self.containers.clear()
        self.sessions.clear()
        logger.info("SessionManager shutdown complete")
    
    # =========================================================================
    # Client Operations
    # =========================================================================
    
    async def acquire(
        self,
        files: Optional[dict[str, str]] = None,
        startup_commands: Optional[list[str]] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """
        Acquire an available session, waiting if necessary.
        
        Args:
            files: Dict of filename -> base64-encoded content to write
            startup_commands: Commands to run after acquiring
            timeout: Max seconds to wait for a session (default: SWEREX_ACQUIRE_TIMEOUT)
            
        Returns:
            Session ID
            
        Raises:
            HTTPException: If no session available within timeout
        """
        # #region agent log
        import json as _json; _acq_start = time.time(); _log_path = "/tmp/swerex_debug.log"; open(_log_path, "a").write(_json.dumps({"hypothesisId": "A_server", "location": "swerex_server.py:acquire:start", "message": "acquire_start", "data": {"num_files": len(files) if files else 0, "available_sessions": sum(1 for s in self.sessions.values() if s.state == SessionState.AVAILABLE), "in_use_sessions": sum(1 for s in self.sessions.values() if s.state == SessionState.IN_USE)}, "timestamp": int(time.time()*1000)}) + "\n")
        # #endregion
        if timeout is None:
            timeout = self._acquire_timeout
        
        session = None
        deadline = time.time() + timeout
        
        # #region agent log
        _lock_start = time.time(); open(_log_path, "a").write(_json.dumps({"hypothesisId": "A_server", "location": "swerex_server.py:acquire:lock_wait", "message": "waiting_for_lock", "data": {"time_before_lock_ms": int((_lock_start - _acq_start)*1000)}, "timestamp": int(time.time()*1000)}) + "\n")
        # #endregion
        async with self._session_available:
            # #region agent log
            _lock_acquired = time.time(); open(_log_path, "a").write(_json.dumps({"hypothesisId": "A_server", "location": "swerex_server.py:acquire:lock_acquired", "message": "lock_acquired", "data": {"lock_wait_ms": int((_lock_acquired - _lock_start)*1000)}, "timestamp": int(time.time()*1000)}) + "\n")
            # #endregion
            while session is None:
                # Try to find an available session
                # Sort containers: healthy first, then by available count (descending)
                candidates = sorted(
                    [c for c in self.containers.values() if c.healthy],
                    key=lambda c: -sum(1 for s in c.sessions.values() 
                                      if s.state == SessionState.AVAILABLE)
                )
                
                for container in candidates:
                    for s in container.sessions.values():
                        if s.state == SessionState.AVAILABLE:
                            s.state = SessionState.IN_USE
                            s.acquired_at = time.time()
                            session = s
                            break
                    if session:
                        break
                
                if session:
                    break
                
                # No session available - wait for one
                remaining = deadline - time.time()
                if remaining <= 0:
                    # #region agent log
                    open(_log_path, "a").write(_json.dumps({"hypothesisId": "C_server", "location": "swerex_server.py:acquire:timeout", "message": "acquire_timeout_no_session", "data": {"timeout": timeout, "total_wait_ms": int((time.time() - _acq_start)*1000)}, "timestamp": int(time.time()*1000)}) + "\n")
                    # #endregion
                    raise HTTPException(
                        status_code=503,
                        detail=f"No session available within {timeout}s timeout. "
                               f"All sessions are in use or unhealthy."
                    )
                
                # Wait for notification (with timeout)
                try:
                    await asyncio.wait_for(
                        self._session_available.wait(),
                        timeout=min(remaining, 5.0)  # Check every 5s max
                    )
                except asyncio.TimeoutError:
                    # Timeout on wait, loop will check deadline
                    pass
        
        # #region agent log
        _setup_start = time.time(); open(_log_path, "a").write(_json.dumps({"hypothesisId": "D_server", "location": "swerex_server.py:acquire:setup_start", "message": "starting_setup", "data": {"session_id": session.id, "has_files": bool(files), "has_commands": bool(startup_commands)}, "timestamp": int(time.time()*1000)}) + "\n")
        # #endregion
        # Setup files/commands outside lock (can take time)
        # Use semaphore to limit concurrent setups to avoid overwhelming containers
        try:
            if files or startup_commands:
                async with self._setup_semaphore:
                    await self._setup_session(session, files, startup_commands)
        except Exception as e:
            # Release session on setup failure
            async with self._lock:
                session.state = SessionState.BROKEN
            await self._cleanup_queue.put(session.id)
            # #region agent log
            open(_log_path, "a").write(_json.dumps({"hypothesisId": "D_server", "location": "swerex_server.py:acquire:setup_failed", "message": "setup_failed", "data": {"error": str(e)[:200], "setup_duration_ms": int((time.time() - _setup_start)*1000)}, "timestamp": int(time.time()*1000)}) + "\n")
            # #endregion
            raise HTTPException(status_code=500, detail=f"Session setup failed: {e}")
        
        # #region agent log
        open(_log_path, "a").write(_json.dumps({"hypothesisId": "A_server", "location": "swerex_server.py:acquire:success", "message": "acquire_complete", "data": {"session_id": session.id, "total_duration_ms": int((time.time() - _acq_start)*1000), "setup_duration_ms": int((time.time() - _setup_start)*1000)}, "timestamp": int(time.time()*1000)}) + "\n")
        # #endregion
        logger.debug(f"Acquired session {session.id}")
        return session.id
    
    async def _setup_session(
        self,
        session: Session,
        files: Optional[dict[str, str]],
        startup_commands: Optional[list[str]],
    ) -> None:
        """Setup session with files and startup commands."""
        container = self.containers[session.container_id]
        assert container.deployment is not None
        runtime = container.deployment.runtime
        
        # Write files
        if files:
            for filename, content_b64 in files.items():
                # Decode content
                try:
                    content = base64.b64decode(content_b64).decode('utf-8')
                except Exception:
                    content = content_b64
                
                # Create directory if needed
                dirname = os.path.dirname(filename)
                if dirname:
                    await runtime.run_in_session(
                        BashAction(session=session.bash_session_name, 
                                  command=f"mkdir -p '{dirname}'")
                    )
                
                # Write file
                escaped_content = content.replace("'", "'\\''")
                cmd = f"printf '%s' '{escaped_content}' > '{filename}'"
                await runtime.run_in_session(
                    BashAction(session=session.bash_session_name, command=cmd)
                )
        
        # Run startup commands
        if startup_commands:
            for cmd in startup_commands:
                await runtime.run_in_session(
                    BashAction(session=session.bash_session_name, command=cmd)
                )
    
    async def execute(
        self,
        session_id: str,
        command: str,
        timeout: float = SWEREX_COMMAND_TIMEOUT,
        fetch_files: Optional[list[str]] = None,
    ) -> CommandResult:
        """
        Execute a command in a session.
        
        Args:
            session_id: Session ID from acquire()
            command: Bash command to execute
            timeout: Command timeout in seconds
            fetch_files: Optional list of file paths to fetch after execution
            
        Returns:
            CommandResult with status, stdout, stderr, return_code, and optionally files
        """
        session = self.sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
        
        if session.state != SessionState.IN_USE:
            raise HTTPException(
                status_code=400, 
                detail=f"Session {session_id} not acquired (state: {session.state.value})"
            )
        
        container = self.containers.get(session.container_id)
        if not container or not container.deployment:
            raise HTTPException(status_code=500, detail="Container not available")
        
        files_result: Optional[dict[str, str]] = None
        
        try:
            runtime = container.deployment.runtime
            
            result = await asyncio.wait_for(
                runtime.run_in_session(
                    BashAction(session=session.bash_session_name, command=command)
                ),
                timeout=timeout
            )
            
            status = "Success" if result.exit_code == 0 else "Failed"
            
            # Fetch files if requested
            if fetch_files:
                files_result = await self._fetch_files(
                    runtime, session.bash_session_name, fetch_files, timeout
                )
            
            return CommandResult(
                status=status,
                stdout=result.output,
                stderr="",
                return_code=result.exit_code,
                files=files_result,
            )
            
        except asyncio.TimeoutError:
            return CommandResult(
                status="Failed",
                stdout="",
                stderr=f"Command timed out after {timeout} seconds",
                return_code=-1,
                files=None,
            )
        except Exception as e:
            # Check for non-zero exit code errors
            error_str = str(e)
            match = re.search(r"exit code (\d+)", error_str)
            exit_code = int(match.group(1)) if match else 1
            
            output = ""
            output_match = re.search(r"Here is the output:\n'(.*)'", error_str, re.DOTALL)
            if output_match:
                output = output_match.group(1)
                # Convert escaped newlines to actual newlines
                output = output.replace("\\n", "\n")
            
            return CommandResult(
                status="Failed",
                stdout=output,
                stderr=str(e) if not output else "",
                return_code=exit_code,
                files=None,
            )
    
    async def _fetch_files(
        self,
        runtime,
        bash_session_name: str,
        file_paths: list[str],
        timeout: float,
    ) -> dict[str, str]:
        """
        Fetch files from the session and return as base64-encoded content.
        
        Args:
            runtime: The container runtime
            bash_session_name: Name of the bash session
            file_paths: List of file paths to fetch
            timeout: Timeout for file operations
            
        Returns:
            Dict of filename -> base64-encoded content
        """
        files = {}
        for path in file_paths:
            try:
                # Use base64 to encode file content
                result = await asyncio.wait_for(
                    runtime.run_in_session(
                        BashAction(
                            session=bash_session_name,
                            command=f"base64 -w 0 '{path}' 2>/dev/null || echo 'FILE_NOT_FOUND'"
                        )
                    ),
                    timeout=timeout
                )
                
                output = result.output.strip()
                if output and output != "FILE_NOT_FOUND" and result.exit_code == 0:
                    files[path] = output
                else:
                    logger.debug(f"File not found or empty: {path}")
            except Exception as e:
                logger.warning(f"Failed to fetch file {path}: {e}")
        
        return files
    
    async def release(self, session_id: str) -> None:
        """
        Release a session back to the pool.
        
        This is instant - cleanup happens in background.
        
        Args:
            session_id: Session ID from acquire()
        """
        async with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                logger.warning(f"Session {session_id} not found for release")
                return
            
            if session.state != SessionState.IN_USE:
                logger.warning(f"Session {session_id} not in use (state: {session.state.value})")
                return
            
            session.state = SessionState.CLEANING
            session.acquired_at = None
        
        # Queue for background cleanup
        await self._cleanup_queue.put(session_id)
        logger.debug(f"Released session {session_id}")
    
    # =========================================================================
    # Background Tasks
    # =========================================================================
    
    async def _cleanup_loop(self) -> None:
        """Background worker that cleans released sessions."""
        while not self._shutdown:
            try:
                # Wait for session to clean
                session_id = await asyncio.wait_for(
                    self._cleanup_queue.get(),
                    timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            
            session = self.sessions.get(session_id)
            if not session or session.state != SessionState.CLEANING:
                continue
            
            try:
                container = self.containers.get(session.container_id)
                if not container or not container.deployment:
                    session.state = SessionState.BROKEN
                    continue
                
                runtime = container.deployment.runtime
                
                # Clean working directory and reset environment
                await asyncio.wait_for(
                    runtime.run_in_session(BashAction(
                        session=session.bash_session_name,
                        command=f"rm -rf {session.work_dir}/* 2>/dev/null; cd {session.work_dir}"
                    )),
                    timeout=10.0
                )
                
                session.state = SessionState.AVAILABLE
                logger.debug(f"Cleaned session {session_id}")
                
                # Notify waiting acquirers that a session is available
                async with self._session_available:
                    self._session_available.notify()
                
            except Exception as e:
                logger.error(f"Cleanup failed for {session_id}: {e}")
                session.state = SessionState.BROKEN
    
    async def _health_check_loop(self) -> None:
        """Check container health periodically."""
        while not self._shutdown:
            await asyncio.sleep(SWEREX_HEALTH_CHECK_INTERVAL)
            
            for container in list(self.containers.values()):
                if not container.deployment:
                    continue
                
                try:
                    # Use first session for health check
                    if not container.sessions:
                        continue
                    
                    first_session = next(iter(container.sessions.values()))
                    runtime = container.deployment.runtime
                    
                    await asyncio.wait_for(
                        runtime.run_in_session(BashAction(
                            session=first_session.bash_session_name,
                            command="echo ok"
                        )),
                        timeout=5
                    )
                    
                    if not container.healthy:
                        logger.info(f"Container {container.id} recovered")
                    container.healthy = True
                    container.consecutive_failures = 0
                    
                except Exception as e:
                    container.consecutive_failures += 1
                    if container.consecutive_failures >= 3 and container.healthy:
                        logger.warning(f"Container {container.id} marked unhealthy: {e}")
                        container.healthy = False
                        # Mark all available sessions as broken
                        for session in container.sessions.values():
                            if session.state == SessionState.AVAILABLE:
                                session.state = SessionState.BROKEN
    
    async def _recreation_loop(self) -> None:
        """Recreate broken sessions when their container is healthy."""
        while not self._shutdown:
            await asyncio.sleep(5)
            
            for session in list(self.sessions.values()):
                if session.state != SessionState.BROKEN:
                    continue
                
                container = self.containers.get(session.container_id)
                if not container or not container.healthy or not container.deployment:
                    continue
                
                try:
                    runtime = container.deployment.runtime
                    
                    # Try to close existing session first
                    try:
                        await runtime.close_session(
                            CloseSessionRequest(session=session.bash_session_name)
                        )
                    except Exception:
                        pass
                    
                    # Recreate session
                    await runtime.create_session(
                        CreateBashSessionRequest(session=session.bash_session_name)
                    )
                    
                    # Recreate working directory
                    await runtime.run_in_session(
                        BashAction(session=session.bash_session_name, 
                                  command=f"mkdir -p '{session.work_dir}'")
                    )
                    await runtime.run_in_session(
                        BashAction(session=session.bash_session_name, 
                                  command=f"cd '{session.work_dir}'")
                    )
                    
                    session.state = SessionState.AVAILABLE
                    logger.info(f"Recreated session {session.id}")
                    
                    # Notify waiting acquirers that a session is available
                    async with self._session_available:
                        self._session_available.notify()
                    
                except Exception as e:
                    logger.debug(f"Recreation failed for {session.id}: {e}")
    
    # =========================================================================
    # Status
    # =========================================================================
    
    def get_health(self) -> HealthResponse:
        """Get current health status."""
        total = len(self.sessions)
        available = sum(1 for s in self.sessions.values() if s.state == SessionState.AVAILABLE)
        in_use = sum(1 for s in self.sessions.values() if s.state == SessionState.IN_USE)
        cleaning = sum(1 for s in self.sessions.values() if s.state == SessionState.CLEANING)
        broken = sum(1 for s in self.sessions.values() if s.state == SessionState.BROKEN)
        
        healthy_containers = sum(1 for c in self.containers.values() if c.healthy)
        unhealthy_containers = len(self.containers) - healthy_containers
        
        # Determine overall status
        if healthy_containers == 0:
            status = "unhealthy"
        elif unhealthy_containers > 0 or broken > total * 0.1:
            status = "degraded"
        else:
            status = "healthy"
        
        return HealthResponse(
            status=status,
            total_sessions=total,
            available_sessions=available,
            in_use_sessions=in_use,
            cleaning_sessions=cleaning,
            broken_sessions=broken,
            healthy_containers=healthy_containers,
            unhealthy_containers=unhealthy_containers,
        )


# =============================================================================
# FastAPI Application
# =============================================================================

manager: Optional[SessionManager] = None
# Server-side rate limiter: controls how many /acquire requests are processed concurrently
# This prevents overwhelming the server when 1000s of clients connect simultaneously
_acquire_semaphore: Optional[asyncio.Semaphore] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global manager, _acquire_semaphore
    
    # Initialize server-side rate limiter
    _acquire_semaphore = asyncio.Semaphore(SWEREX_MAX_CONCURRENT_ACQUIRE)
    logger.info(f"Initialized acquire rate limiter: max {SWEREX_MAX_CONCURRENT_ACQUIRE} concurrent requests")
    
    # Parse endpoints
    endpoints = [e.strip() for e in SWEREX_ENDPOINTS.split(",") if e.strip()]
    if not endpoints:
        raise ValueError(
            "No endpoints configured. Set SWEREX_ENDPOINTS environment variable "
            "(comma-separated URLs like http://container1:8000,http://container2:8000)"
        )
    
    # Initialize manager
    manager = SessionManager(
        endpoints=endpoints,
        sessions_per_container=SWEREX_SESSIONS_PER_CONTAINER,
        auth_token=SWEREX_AUTH_TOKEN,
    )
    await manager.initialize()
    
    yield
    
    # Shutdown
    if manager:
        await manager.shutdown()


app = FastAPI(
    title="SweRex Session Server",
    description="Centralized session management for stateful bash execution",
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/session/acquire", response_model=AcquireResponse)
async def acquire_session(request: AcquireRequest) -> AcquireResponse:
    """Acquire a session from the pool.
    
    Server-side rate limiting ensures we only process SWEREX_MAX_CONCURRENT_ACQUIRE
    requests at a time. Additional requests queue on the semaphore, not at TCP level.
    """
    assert manager is not None
    assert _acquire_semaphore is not None
    
    # #region agent log
    import json as _json; _endpoint_start = time.time(); _log_path = "/tmp/swerex_debug.log"; open(_log_path, "a").write(_json.dumps({"hypothesisId": "E_ratelimit", "location": "swerex_server.py:acquire_endpoint:start", "message": "endpoint_received_request", "data": {"num_files": len(request.files) if request.files else 0}, "timestamp": int(time.time()*1000)}) + "\n")
    # #endregion
    
    # Rate limit: only process N acquire requests concurrently
    async with _acquire_semaphore:
        # #region agent log
        _after_semaphore = time.time(); open(_log_path, "a").write(_json.dumps({"hypothesisId": "E_ratelimit", "location": "swerex_server.py:acquire_endpoint:semaphore_acquired", "message": "semaphore_acquired", "data": {"semaphore_wait_ms": int((_after_semaphore - _endpoint_start)*1000)}, "timestamp": int(time.time()*1000)}) + "\n")
        # #endregion
        
        session_id = await manager.acquire(request.files, request.startup_commands)
        
        # #region agent log
        open(_log_path, "a").write(_json.dumps({"hypothesisId": "E_ratelimit", "location": "swerex_server.py:acquire_endpoint:complete", "message": "endpoint_complete", "data": {"session_id": session_id, "total_endpoint_ms": int((time.time() - _endpoint_start)*1000)}, "timestamp": int(time.time()*1000)}) + "\n")
        # #endregion
        
        return AcquireResponse(session_id=session_id)


@app.post("/session/{session_id}/execute", response_model=ExecuteResponse)
async def execute_command(session_id: str, request: ExecuteRequest) -> ExecuteResponse:
    """Execute a command in an acquired session."""
    assert manager is not None
    result = await manager.execute(
        session_id, 
        request.command, 
        request.timeout,
        fetch_files=request.fetch_files,
    )
    return ExecuteResponse(
        status=result.status,
        stdout=result.stdout,
        stderr=result.stderr,
        return_code=result.return_code,
        files=result.files,
    )


@app.post("/session/{session_id}/release")
async def release_session(session_id: str):
    """Release a session back to the pool."""
    assert manager is not None
    await manager.release(session_id)
    return {"status": "released"}


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Get server health status."""
    assert manager is not None
    return manager.get_health()


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "verl.experimental.agent_loop.swerex_server:app",
        host=SWEREX_SERVER_HOST,
        port=SWEREX_SERVER_PORT,
        log_level="info",
    )
