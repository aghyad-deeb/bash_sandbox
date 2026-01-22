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
SWE-ReX Session Manager for Stateful Bash Execution.

This module provides wrappers around SWE-ReX for stateful bash execution
in the FusionAgentLoop. It supports two deployment modes:

1. DockerDeployment (SweRexSessionManager):
   - Starts Docker containers directly from Python
   - Requires Docker socket access
   - Good for local development

2. RemoteDeployment (SweRexRemoteSessionPool):
   - Connects to externally running swerex-remote servers
   - No Docker-in-Docker required
   - Good for production with pre-configured sandbox containers
   - Supports session pooling for fast episode startup

Key features:
- State (cd, export, variables) persists across commands within an episode
- Each episode gets a fresh bash session (no state leakage between episodes)
- Simple async interface compatible with FusionAgentLoop

Usage (RemoteDeployment - recommended for production):
    # Start external sandbox containers first, then:
    pool = SweRexRemoteSessionPool(
        endpoints=["http://sandbox1:8000", "http://sandbox2:8000"],
        auth_token="<YOUR_AUTH_TOKEN>"
    )
    await pool.initialize()
    
    # Per-episode usage (fast - just acquires from pool)
    session_id = await pool.acquire_session()
    result = await pool.execute_command(session_id, "echo hello")
    await pool.release_session(session_id)
    
    await pool.shutdown()

Usage (DockerDeployment - for local development):
    manager = SweRexSessionManager()
    session_id = await manager.create_session(files={"test.py": b64_content})
    result = await manager.execute_command(session_id, "cd /tmp && pwd")
    await manager.destroy_session(session_id)
    await manager.shutdown()
"""

import asyncio
import base64
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional, Union
from uuid import uuid4

from swerex.deployment.docker import DockerDeployment
from swerex.deployment.remote import RemoteDeployment
from swerex.exceptions import NonZeroExitCodeError
from swerex.runtime.abstract import (
    BashAction,
    CloseSessionRequest,
    CreateBashSessionRequest,
)

logger = logging.getLogger(__name__)

# Configuration from environment
SWEREX_DOCKER_IMAGE = os.getenv("SWEREX_DOCKER_IMAGE", "python:3.11-slim")
SWEREX_TIMEOUT = float(os.getenv("SWEREX_TIMEOUT", "30"))

# Remote deployment configuration
SWEREX_ENDPOINTS = os.getenv("SWEREX_ENDPOINTS", "")  # Comma-separated: "http://host1:8000,http://host2:8000"
SWEREX_AUTH_TOKEN = os.getenv("SWEREX_AUTH_TOKEN", "default-token")


@dataclass
class CommandResult:
    """Result of a bash command execution."""
    
    status: str  # "Success" or "Failed"
    stdout: str
    stderr: str
    return_code: int
    
    def to_dict(self) -> dict:
        """Convert to dictionary for compatibility with existing code."""
        return {
            "status": self.status,
            "run_result": {
                "stdout": self.stdout,
                "stderr": self.stderr,
                "return_code": self.return_code,
            }
        }


@dataclass 
class SessionInfo:
    """Information about an active session."""
    
    session_id: str
    bash_session_name: str
    work_dir: str  # Unique working directory for this session


class SweRexSessionManager:
    """
    Manages SWE-ReX deployments and sessions for stateful bash execution.
    
    Architecture:
    - ONE Docker deployment (container) is shared across all sessions
    - Each "session" is a bash session within that container
    - Bash sessions have isolated shell state (cwd, env vars, shell vars)
    - Sessions use unique working directories for filesystem isolation
    
    State (working directory, environment variables, shell variables) persists
    across commands within a session but NOT between different sessions.
    
    This matches RL training semantics where each episode should be independent.
    
    Performance:
    - First create_session(): ~14 seconds (starts Docker container)
    - Subsequent create_session(): ~100ms (creates bash session only)
    """
    
    def __init__(
        self, 
        docker_image: str = SWEREX_DOCKER_IMAGE,
        default_timeout: float = SWEREX_TIMEOUT
    ):
        """
        Initialize the session manager.
        
        Args:
            docker_image: Docker image to use for the shared deployment
            default_timeout: Default timeout for command execution (seconds)
        """
        self.docker_image = docker_image
        self.default_timeout = default_timeout
        self._sessions: dict[str, SessionInfo] = {}
        self._lock = asyncio.Lock()
        
        # Shared deployment (lazily initialized)
        self._deployment: Optional[DockerDeployment] = None
        self._deployment_started: bool = False
        self._deployment_lock = asyncio.Lock()
    
    async def _ensure_deployment_started(self) -> None:
        """
        Ensure the shared Docker deployment is started (lazy initialization).
        
        This is called on the first create_session() and takes ~14 seconds.
        Subsequent calls return immediately.
        """
        async with self._deployment_lock:
            if self._deployment_started:
                return
            
            logger.info(f"Starting shared Docker deployment with image {self.docker_image}")
            self._deployment = DockerDeployment(image=self.docker_image)
            await self._deployment.start()
            self._deployment_started = True
            logger.info("Shared Docker deployment started successfully")
    
    async def create_session(
        self,
        files: Optional[dict[str, str]] = None,
        startup_commands: Optional[list[str]] = None,
    ) -> str:
        """
        Create a new session with a fresh bash environment.
        
        The first call starts the Docker deployment (~14 seconds).
        Subsequent calls only create a new bash session (~100ms).
        
        Args:
            files: Dict of filename -> base64-encoded content to write
            startup_commands: List of bash commands to run at session start
            
        Returns:
            session_id: Unique identifier for this session
        """
        # Ensure shared deployment is running
        await self._ensure_deployment_started()
        
        session_id = uuid4().hex
        bash_session_name = f"bash_{session_id[:8]}"
        work_dir = f"/tmp/session_{session_id[:8]}"
        
        logger.info(f"Creating session {session_id}")
        
        try:
            assert self._deployment is not None, "Deployment should be started"
            runtime = self._deployment.runtime
            
            # Create bash session
            await runtime.create_session(
                CreateBashSessionRequest(session=bash_session_name)
            )
            
            # Create unique working directory for this session
            await runtime.run_in_session(
                BashAction(session=bash_session_name, command=f"mkdir -p '{work_dir}'")
            )
            await runtime.run_in_session(
                BashAction(session=bash_session_name, command=f"cd '{work_dir}'")
            )
            
            # Write files if provided (relative to work_dir)
            if files:
                await self._write_files(runtime, bash_session_name, files)
            
            # Run startup commands if provided
            if startup_commands:
                for cmd in startup_commands:
                    await runtime.run_in_session(
                        BashAction(session=bash_session_name, command=cmd)
                    )
            
            # Store session info
            async with self._lock:
                self._sessions[session_id] = SessionInfo(
                    session_id=session_id,
                    bash_session_name=bash_session_name,
                    work_dir=work_dir,
                )
            
            logger.info(f"Session {session_id} created successfully")
            return session_id
            
        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            raise
    
    async def _write_files(
        self, 
        runtime, 
        bash_session_name: str, 
        files: dict[str, str]
    ) -> None:
        """Write base64-encoded files to the container."""
        for filename, content_b64 in files.items():
            # Decode content
            try:
                content = base64.b64decode(content_b64).decode('utf-8')
            except Exception:
                # Assume it's already decoded
                content = content_b64
            
            # Create directory if needed
            dirname = os.path.dirname(filename)
            if dirname:
                await runtime.run_in_session(
                    BashAction(
                        session=bash_session_name, 
                        command=f"mkdir -p '{dirname}'"
                    )
                )
            
            # Write file - use runtime.write_file if available, else bash
            # Escape content for bash
            escaped_content = content.replace("'", "'\\''")
            cmd = f"printf '%s' '{escaped_content}' > '{filename}'"
            await runtime.run_in_session(
                BashAction(session=bash_session_name, command=cmd)
            )
    
    async def execute_command(
        self,
        session_id: str,
        command: str,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        """
        Execute a bash command in an existing session.
        
        State (working directory, environment variables, etc.) persists
        across commands within the same session.
        
        Args:
            session_id: Session identifier from create_session()
            command: Bash command to execute
            timeout: Command timeout in seconds (uses default if not specified)
            
        Returns:
            CommandResult with status, stdout, stderr, return_code
        """
        timeout = timeout or self.default_timeout
        
        async with self._lock:
            session_info = self._sessions.get(session_id)
        
        if not session_info:
            return CommandResult(
                status="Failed",
                stdout="",
                stderr=f"Session {session_id} not found",
                return_code=-1,
            )
        
        if not self._deployment or not self._deployment_started:
            return CommandResult(
                status="Failed",
                stdout="",
                stderr="Deployment not started",
                return_code=-1,
            )
        
        try:
            runtime = self._deployment.runtime
            
            # Execute command with timeout
            result = await asyncio.wait_for(
                runtime.run_in_session(
                    BashAction(
                        session=session_info.bash_session_name,
                        command=command,
                    )
                ),
                timeout=timeout
            )
            
            # Map exit code to status
            status = "Success" if result.exit_code == 0 else "Failed"
            
            return CommandResult(
                status=status,
                stdout=result.output,
                stderr="",  # SWE-ReX combines stdout/stderr in output
                return_code=result.exit_code,
            )
            
        except asyncio.TimeoutError:
            return CommandResult(
                status="Failed",
                stdout="",
                stderr=f"Command timed out after {timeout} seconds",
                return_code=-1,
            )
        except NonZeroExitCodeError as e:
            # SWE-ReX raises this for non-zero exit codes
            # Extract exit code from exception message: "Command 'X' failed with exit code N."
            import re
            match = re.search(r"exit code (\d+)", str(e))
            exit_code = int(match.group(1)) if match else 1
            
            # Extract output from the exception message if present
            output = ""
            output_match = re.search(r"Here is the output:\n'(.*)'", str(e), re.DOTALL)
            if output_match:
                output = output_match.group(1)
            
            return CommandResult(
                status="Failed",
                stdout=output,
                stderr="",
                return_code=exit_code,
            )
        except Exception as e:
            logger.error(f"Error executing command in session {session_id}: {e}")
            return CommandResult(
                status="Failed",
                stdout="",
                stderr=str(e),
                return_code=-1,
            )
    
    async def destroy_session(self, session_id: str) -> bool:
        """
        Destroy a session and cleanup its resources.
        
        This closes the bash session and removes its working directory.
        The shared Docker deployment remains running for other sessions.
        
        Args:
            session_id: Session identifier from create_session()
            
        Returns:
            True if session was destroyed, False if not found
        """
        async with self._lock:
            session_info = self._sessions.pop(session_id, None)
        
        if not session_info:
            logger.warning(f"Session {session_id} not found for destruction")
            return False
        
        if not self._deployment or not self._deployment_started:
            logger.warning(f"Deployment not running when destroying session {session_id}")
            return True
        
        try:
            runtime = self._deployment.runtime
            
            # Close bash session
            await runtime.close_session(
                CloseSessionRequest(session=session_info.bash_session_name)
            )
            
            # Note: We don't stop the deployment - it's shared across sessions
            # The working directory cleanup is optional (container will be destroyed eventually)
            
            logger.info(f"Session {session_id} destroyed successfully")
            return True
            
        except Exception as e:
            logger.error(f"Error destroying session {session_id}: {e}")
            return True  # Session is gone either way
    
    async def cleanup_all(self) -> None:
        """Cleanup all active sessions (keeps deployment running)."""
        async with self._lock:
            session_ids = list(self._sessions.keys())
        
        for session_id in session_ids:
            await self.destroy_session(session_id)
    
    async def shutdown(self) -> None:
        """
        Completely shutdown the manager, including the Docker deployment.
        
        Call this when you're completely done with the manager.
        After calling shutdown(), the manager cannot be used again.
        """
        # First cleanup all sessions
        await self.cleanup_all()
        
        # Then stop the deployment
        async with self._deployment_lock:
            if self._deployment and self._deployment_started:
                try:
                    logger.info("Stopping shared Docker deployment")
                    await self._deployment.stop()
                    logger.info("Shared Docker deployment stopped")
                except Exception as e:
                    logger.error(f"Error stopping deployment: {e}")
                finally:
                    self._deployment = None
                    self._deployment_started = False
    
    def get_active_session_count(self) -> int:
        """Get the number of active sessions."""
        return len(self._sessions)
    
    def is_deployment_running(self) -> bool:
        """Check if the shared deployment is running."""
        return self._deployment_started


# =============================================================================
# Remote Session Pool - For external sandbox containers
# =============================================================================


@dataclass
class PooledSession:
    """A session in the pool with its associated deployment."""
    
    session_id: str
    deployment: RemoteDeployment
    bash_session_name: str
    work_dir: str
    in_use: bool = False
    endpoint: str = ""


class SweRexRemoteSessionPool:
    """
    Session pool for external SWE-ReX sandbox containers.
    
    This class connects to externally running swerex-remote servers and manages
    a pool of bash sessions. It's designed for production use where sandbox
    containers are deployed independently (e.g., via docker-compose or Kubernetes).
    
    Architecture:
    - Connects to N external sandbox servers via RemoteDeployment
    - Pre-creates bash sessions in each sandbox (session pool)
    - Episodes acquire/release sessions from the pool (fast - no container startup)
    - Each session has its own working directory for isolation
    
    Benefits over DockerDeployment:
    - No Docker-in-Docker required
    - Sandbox containers can be pre-configured (read-only filesystem, etc.)
    - Faster episode startup (session reuse vs creation)
    - Better resource management in production
    
    Usage:
        # External sandbox containers must be running first
        pool = SweRexRemoteSessionPool(
            endpoints=["http://sandbox1:8000", "http://sandbox2:8000"],
            auth_token="<YOUR_AUTH_TOKEN>",
            sessions_per_endpoint=4  # Total pool size = 2 * 4 = 8
        )
        await pool.initialize()
        
        # Per-episode (fast - just pool acquisition)
        session_id = await pool.acquire_session()
        result = await pool.execute_command(session_id, "echo hello")
        await pool.release_session(session_id)  # Returns to pool, resets state
        
        await pool.shutdown()
    """
    
    def __init__(
        self,
        endpoints: Optional[list[str]] = None,
        auth_token: str = SWEREX_AUTH_TOKEN,
        sessions_per_endpoint: int = 1,
        default_timeout: float = SWEREX_TIMEOUT,
    ):
        """
        Initialize the remote session pool.
        
        Args:
            endpoints: List of swerex-remote server URLs (e.g., ["http://host:8000"])
                      If None, reads from SWEREX_ENDPOINTS environment variable
            auth_token: Authentication token for swerex-remote servers
            sessions_per_endpoint: Number of sessions to create per endpoint
            default_timeout: Default timeout for command execution (seconds)
        """
        # Parse endpoints from env var if not provided
        if endpoints is None:
            if SWEREX_ENDPOINTS:
                endpoints = [e.strip() for e in SWEREX_ENDPOINTS.split(",") if e.strip()]
            else:
                endpoints = []
        
        if not endpoints:
            raise ValueError(
                "No endpoints provided. Either pass endpoints parameter or set "
                "SWEREX_ENDPOINTS environment variable (comma-separated URLs)"
            )
        
        self.endpoints = endpoints
        self.auth_token = auth_token
        self.sessions_per_endpoint = sessions_per_endpoint
        self.default_timeout = default_timeout
        
        # Pool state
        self._pool: dict[str, PooledSession] = {}
        self._deployments: list[RemoteDeployment] = []
        self._initialized = False
        self._lock = asyncio.Lock()
        self._pool_semaphore: Optional[asyncio.Semaphore] = None
    
    async def initialize(self) -> None:
        """
        Initialize the pool by connecting to all endpoints and creating sessions.
        
        This should be called once at startup. It connects to all sandbox servers
        and pre-creates bash sessions for the pool. All endpoints are initialized
        in parallel for fast startup.
        """
        if self._initialized:
            return
        
        logger.info(f"Initializing session pool with {len(self.endpoints)} endpoints, "
                   f"{self.sessions_per_endpoint} sessions each (parallel initialization)")
        
        async def init_endpoint(endpoint: str) -> list[PooledSession]:
            """Initialize a single endpoint and return its sessions."""
            # Parse host and port from endpoint URL
            # Format: "http://host:port" or "http://host" (default port 8000)
            if "://" in endpoint:
                url_part = endpoint.split("://")[1]
            else:
                url_part = endpoint
            
            if ":" in url_part:
                host_part, port_str = url_part.rsplit(":", 1)
                port = int(port_str)
            else:
                host_part = url_part
                port = 8000
            
            # Reconstruct host with protocol
            if "://" in endpoint:
                protocol = endpoint.split("://")[0]
                host = f"{protocol}://{host_part}"
            else:
                host = f"http://{host_part}"
            
            # Create deployment connection
            deployment = RemoteDeployment(
                host=host,
                port=port,
                auth_token=self.auth_token,
            )
            
            await deployment.start()
            logger.debug(f"Connected to {endpoint}")
            
            # Create sessions for this endpoint
            runtime = deployment.runtime
            sessions = []
            for i in range(self.sessions_per_endpoint):
                session_id = uuid4().hex
                bash_session_name = f"pool_{session_id[:8]}"
                work_dir = f"/tmp/sessions/{session_id[:8]}"
                
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
                
                sessions.append(PooledSession(
                    session_id=session_id,
                    deployment=deployment,
                    bash_session_name=bash_session_name,
                    work_dir=work_dir,
                    in_use=False,
                    endpoint=endpoint,
                ))
                logger.debug(f"Created pooled session {session_id} on {endpoint}")
            
            return deployment, sessions
        
        # Initialize endpoints in parallel batches to avoid overwhelming the system
        batch_size = 32  # Initialize 32 endpoints at a time
        try:
            for batch_start in range(0, len(self.endpoints), batch_size):
                batch_end = min(batch_start + batch_size, len(self.endpoints))
                batch_endpoints = self.endpoints[batch_start:batch_end]
                
                logger.info(f"Initializing endpoints {batch_start}-{batch_end-1} "
                           f"({len(batch_endpoints)} endpoints)...")
                
                results = await asyncio.gather(
                    *[init_endpoint(ep) for ep in batch_endpoints],
                    return_exceptions=True
                )
                
                # Process results
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        endpoint_idx = batch_start + i
                        logger.error(f"Failed to connect to {self.endpoints[endpoint_idx]}: {result}")
                        raise result
                    
                    deployment, sessions = result
                    self._deployments.append(deployment)
                    for session in sessions:
                        self._pool[session.session_id] = session
            
        except Exception as e:
            # Cleanup any successful connections on failure
            for deployment in self._deployments:
                try:
                    await deployment.stop()
                except Exception:
                    pass
            self._deployments.clear()
            self._pool.clear()
            raise
        
        # Create semaphore for pool size limiting
        self._pool_semaphore = asyncio.Semaphore(len(self._pool))
        self._initialized = True
        logger.info(f"Session pool initialized with {len(self._pool)} sessions")
    
    async def acquire_session(
        self,
        files: Optional[dict[str, str]] = None,
        startup_commands: Optional[list[str]] = None,
        timeout: float = 30.0,
    ) -> str:
        """
        Acquire a session from the pool.
        
        This is fast (~10-50ms) as it just acquires an existing session
        and optionally resets its state.
        
        Args:
            files: Dict of filename -> base64-encoded content to write
            startup_commands: List of bash commands to run after acquiring
            timeout: Timeout for acquiring a session from the pool
            
        Returns:
            session_id: Identifier for the acquired session
            
        Raises:
            TimeoutError: If no session is available within timeout
            RuntimeError: If pool is not initialized
        """
        if not self._initialized:
            raise RuntimeError("Session pool not initialized. Call initialize() first.")
        
        assert self._pool_semaphore is not None
        
        # Wait for an available session
        try:
            await asyncio.wait_for(self._pool_semaphore.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"No session available in pool within {timeout} seconds")
        
        # Find and acquire an available session
        async with self._lock:
            for session_id, session in self._pool.items():
                if not session.in_use:
                    session.in_use = True
                    break
            else:
                # This shouldn't happen if semaphore is working correctly
                self._pool_semaphore.release()
                raise RuntimeError("No available session found despite semaphore")
        
        try:
            # Reset session state (clean working directory, reset env vars)
            runtime = session.deployment.runtime
            
            # Clean working directory
            await runtime.run_in_session(
                BashAction(
                    session=session.bash_session_name,
                    command=f"rm -rf '{session.work_dir}'/* 2>/dev/null || true"
                )
            )
            
            # Reset to working directory
            await runtime.run_in_session(
                BashAction(
                    session=session.bash_session_name,
                    command=f"cd '{session.work_dir}'"
                )
            )
            
            # Write files if provided
            if files:
                await self._write_files(runtime, session.bash_session_name, files)
            
            # Run startup commands if provided
            if startup_commands:
                for cmd in startup_commands:
                    await runtime.run_in_session(
                        BashAction(session=session.bash_session_name, command=cmd)
                    )
            
            logger.debug(f"Acquired session {session_id} from pool")
            return session_id
            
        except Exception as e:
            # Release session back to pool on error
            async with self._lock:
                session.in_use = False
            self._pool_semaphore.release()
            raise
    
    async def _write_files(
        self,
        runtime,
        bash_session_name: str,
        files: dict[str, str]
    ) -> None:
        """Write base64-encoded files to the session's working directory."""
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
                    BashAction(session=bash_session_name, command=f"mkdir -p '{dirname}'")
                )
            
            # Write file
            escaped_content = content.replace("'", "'\\''")
            cmd = f"printf '%s' '{escaped_content}' > '{filename}'"
            await runtime.run_in_session(
                BashAction(session=bash_session_name, command=cmd)
            )
    
    async def execute_command(
        self,
        session_id: str,
        command: str,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        """
        Execute a bash command in an acquired session.
        
        Args:
            session_id: Session identifier from acquire_session()
            command: Bash command to execute
            timeout: Command timeout in seconds
            
        Returns:
            CommandResult with status, stdout, stderr, return_code
        """
        timeout = timeout or self.default_timeout
        
        session = self._pool.get(session_id)
        if not session:
            return CommandResult(
                status="Failed",
                stdout="",
                stderr=f"Session {session_id} not found in pool",
                return_code=-1,
            )
        
        if not session.in_use:
            return CommandResult(
                status="Failed",
                stdout="",
                stderr=f"Session {session_id} not acquired",
                return_code=-1,
            )
        
        try:
            runtime = session.deployment.runtime
            
            result = await asyncio.wait_for(
                runtime.run_in_session(
                    BashAction(
                        session=session.bash_session_name,
                        command=command,
                    )
                ),
                timeout=timeout
            )
            
            status = "Success" if result.exit_code == 0 else "Failed"
            return CommandResult(
                status=status,
                stdout=result.output,
                stderr="",
                return_code=result.exit_code,
            )
            
        except asyncio.TimeoutError:
            return CommandResult(
                status="Failed",
                stdout="",
                stderr=f"Command timed out after {timeout} seconds",
                return_code=-1,
            )
        except NonZeroExitCodeError as e:
            match = re.search(r"exit code (\d+)", str(e))
            exit_code = int(match.group(1)) if match else 1
            
            output = ""
            output_match = re.search(r"Here is the output:\n'(.*)'", str(e), re.DOTALL)
            if output_match:
                output = output_match.group(1)
            
            return CommandResult(
                status="Failed",
                stdout=output,
                stderr="",
                return_code=exit_code,
            )
        except Exception as e:
            logger.error(f"Error executing command in session {session_id}: {e}")
            return CommandResult(
                status="Failed",
                stdout="",
                stderr=str(e),
                return_code=-1,
            )
    
    async def release_session(self, session_id: str) -> bool:
        """
        Release a session back to the pool.
        
        The session's working directory is NOT cleaned here - it will be
        cleaned when the session is next acquired. This makes release fast.
        
        Args:
            session_id: Session identifier from acquire_session()
            
        Returns:
            True if session was released, False if not found
        """
        session = self._pool.get(session_id)
        if not session:
            logger.warning(f"Session {session_id} not found for release")
            return False
        
        async with self._lock:
            if not session.in_use:
                logger.warning(f"Session {session_id} already released")
                return False
            session.in_use = False
        
        assert self._pool_semaphore is not None
        self._pool_semaphore.release()
        logger.debug(f"Released session {session_id} back to pool")
        return True
    
    async def shutdown(self) -> None:
        """
        Shutdown the pool and close all connections.
        
        This closes all bash sessions and disconnects from all endpoints.
        """
        logger.info("Shutting down session pool")
        
        # Close all bash sessions
        for session in self._pool.values():
            try:
                runtime = session.deployment.runtime
                await runtime.close_session(
                    CloseSessionRequest(session=session.bash_session_name)
                )
            except Exception as e:
                logger.debug(f"Error closing session {session.session_id}: {e}")
        
        # Stop all deployments
        for deployment in self._deployments:
            try:
                await deployment.stop()
            except Exception as e:
                logger.debug(f"Error stopping deployment: {e}")
        
        self._pool.clear()
        self._deployments.clear()
        self._initialized = False
        logger.info("Session pool shutdown complete")
    
    def get_pool_size(self) -> int:
        """Get total number of sessions in the pool."""
        return len(self._pool)
    
    def get_available_count(self) -> int:
        """Get number of available (not in use) sessions."""
        return sum(1 for s in self._pool.values() if not s.in_use)
    
    def get_in_use_count(self) -> int:
        """Get number of sessions currently in use."""
        return sum(1 for s in self._pool.values() if s.in_use)
    
    def is_initialized(self) -> bool:
        """Check if the pool has been initialized."""
        return self._initialized


# =============================================================================
# Convenience functions
# =============================================================================


# Convenience function for one-off usage
async def run_stateful_bash(
    commands: list[str],
    files: Optional[dict[str, str]] = None,
    startup_commands: Optional[list[str]] = None,
    docker_image: str = SWEREX_DOCKER_IMAGE,
) -> list[CommandResult]:
    """
    Run a sequence of bash commands with state persistence.
    
    Convenience function that creates a session, runs commands, and cleans up.
    
    Note: This starts a Docker container, so it's not efficient for repeated
    calls. For multiple episodes, use SweRexSessionManager directly and call
    shutdown() when completely done.
    
    Args:
        commands: List of bash commands to execute
        files: Dict of filename -> base64-encoded content to write
        startup_commands: Commands to run before main commands
        docker_image: Docker image to use
        
    Returns:
        List of CommandResult for each command
    """
    manager = SweRexSessionManager(docker_image=docker_image)
    
    try:
        session_id = await manager.create_session(
            files=files,
            startup_commands=startup_commands,
        )
        
        results = []
        for cmd in commands:
            result = await manager.execute_command(session_id, cmd)
            results.append(result)
        
        return results
        
    finally:
        await manager.shutdown()  # Full cleanup including Docker container
