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
Tests for SweRex Session Server.

These tests verify:
1. Server endpoints work correctly
2. Session lifecycle (acquire/execute/release)
3. Multiple concurrent sessions
4. State persistence within sessions
5. Health monitoring

Requirements:
    - At least one swerex-sandbox container running
    - SWEREX_TEST_ENDPOINT env var pointing to it

Usage:
    # Start a test container
    docker run -d --name swerex-test -p 18000:8000 \
        --read-only --tmpfs /tmp:size=500M,mode=1777 --tmpfs /root:size=100M \
        -e AUTH_TOKEN=test-token swerex-sandbox:latest
    
    # Run tests
    SWEREX_TEST_ENDPOINT=http://localhost:18000 pytest tests/experimental/agent_loop/test_swerex_server.py -v -s
"""

import asyncio
import base64
import os
import socket
import time

import aiohttp
import pytest
import pytest_asyncio

# Test configuration
SWEREX_TEST_ENDPOINT = os.getenv("SWEREX_TEST_ENDPOINT", "http://localhost:18000")
SWEREX_AUTH_TOKEN = os.getenv("SWEREX_AUTH_TOKEN", "test-token")

# Configure pytest-asyncio
pytest_plugins = ('pytest_asyncio',)


def is_container_available() -> bool:
    """Check if test container is available."""
    try:
        host, port = SWEREX_TEST_ENDPOINT.replace("http://", "").split(":")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((host, int(port)))
        sock.close()
        return result == 0
    except Exception:
        return False


@pytest.fixture
def check_container():
    """Skip if container not available."""
    if not is_container_available():
        pytest.skip(f"Container not available at {SWEREX_TEST_ENDPOINT}")


@pytest_asyncio.fixture
async def session_manager():
    """Create and initialize a SessionManager for testing."""
    # Import directly to avoid verl package dependencies
    import importlib.util
    
    module_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
        "verl", "experimental", "agent_loop", "swerex_server.py"
    )
    spec = importlib.util.spec_from_file_location("swerex_server", module_path)
    swerex_server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(swerex_server)
    
    SessionManager = swerex_server.SessionManager
    
    manager = SessionManager(
        endpoints=[SWEREX_TEST_ENDPOINT],
        sessions_per_container=4,
        auth_token=SWEREX_AUTH_TOKEN,
    )
    
    await manager.initialize()
    yield manager
    await manager.shutdown()


@pytest.mark.asyncio
class TestSessionManager:
    """Test SessionManager directly."""
    
    async def test_initialize(self, check_container, session_manager):
        """Test manager initialization."""
        health = session_manager.get_health()
        
        assert health.total_sessions == 4
        assert health.available_sessions == 4
        assert health.in_use_sessions == 0
        assert health.healthy_containers == 1
        
        print(f"\nInitialized: {health.total_sessions} sessions, "
              f"{health.healthy_containers} healthy containers")
    
    async def test_acquire_release(self, check_container, session_manager):
        """Test basic acquire/release cycle."""
        # Acquire
        session_id = await session_manager.acquire()
        assert session_id is not None
        
        health = session_manager.get_health()
        assert health.in_use_sessions == 1
        assert health.available_sessions == 3
        
        # Release
        await session_manager.release(session_id)
        
        # Wait for cleanup
        await asyncio.sleep(0.5)
        
        health = session_manager.get_health()
        assert health.in_use_sessions == 0
        
        print("\nAcquire/release cycle passed!")
    
    async def test_execute_command(self, check_container, session_manager):
        """Test command execution."""
        session_id = await session_manager.acquire()
        
        try:
            # Simple echo
            result = await session_manager.execute(session_id, "echo hello")
            assert result.status == "Success"
            assert "hello" in result.stdout
            
            # Working directory
            result = await session_manager.execute(session_id, "pwd")
            assert result.status == "Success"
            assert "/tmp/s/" in result.stdout
            
            print(f"\nExecuted commands, working dir: {result.stdout.strip()}")
            
        finally:
            await session_manager.release(session_id)
    
    async def test_state_persistence(self, check_container, session_manager):
        """Test that state persists across commands."""
        session_id = await session_manager.acquire()
        
        try:
            # Set variable
            result = await session_manager.execute(session_id, "export MY_VAR=test123")
            assert result.status == "Success"
            
            # Read variable
            result = await session_manager.execute(session_id, "echo $MY_VAR")
            assert result.status == "Success"
            assert "test123" in result.stdout
            
            # Change directory
            result = await session_manager.execute(session_id, "mkdir -p subdir && cd subdir")
            assert result.status == "Success"
            
            # Verify directory change persists
            result = await session_manager.execute(session_id, "pwd")
            assert result.status == "Success"
            assert "subdir" in result.stdout
            
            print("\nState persistence verified!")
            
        finally:
            await session_manager.release(session_id)
    
    async def test_multiple_sessions(self, check_container, session_manager):
        """Test multiple concurrent sessions."""
        # Acquire all 4 sessions
        session_ids = []
        for i in range(4):
            session_id = await session_manager.acquire()
            session_ids.append(session_id)
        
        health = session_manager.get_health()
        assert health.in_use_sessions == 4
        assert health.available_sessions == 0
        
        try:
            # Set different variables in each session
            for i, session_id in enumerate(session_ids):
                result = await session_manager.execute(
                    session_id, f"export SESSION_NUM={i}"
                )
                assert result.status == "Success"
            
            # Verify isolation - each session has its own variable
            for i, session_id in enumerate(session_ids):
                result = await session_manager.execute(
                    session_id, "echo $SESSION_NUM"
                )
                assert result.status == "Success"
                assert str(i) in result.stdout
            
            print("\nMultiple sessions isolated correctly!")
            
        finally:
            for session_id in session_ids:
                await session_manager.release(session_id)
    
    async def test_acquire_with_files(self, check_container, session_manager):
        """Test acquiring session with files."""
        test_content = "Hello from test file!"
        files = {
            "test.txt": base64.b64encode(test_content.encode()).decode(),
        }
        
        session_id = await session_manager.acquire(files=files)
        
        try:
            result = await session_manager.execute(session_id, "cat test.txt")
            assert result.status == "Success"
            assert test_content in result.stdout
            
            print("\nFile setup verified!")
            
        finally:
            await session_manager.release(session_id)
    
    async def test_acquire_with_startup_commands(self, check_container, session_manager):
        """Test acquiring session with startup commands."""
        startup_commands = [
            "export STARTUP_VAR=initialized",
            "mkdir -p startup_dir",
        ]
        
        session_id = await session_manager.acquire(startup_commands=startup_commands)
        
        try:
            # Verify variable was set
            result = await session_manager.execute(session_id, "echo $STARTUP_VAR")
            assert result.status == "Success"
            assert "initialized" in result.stdout
            
            # Verify directory was created
            result = await session_manager.execute(session_id, "ls -d startup_dir")
            assert result.status == "Success"
            
            print("\nStartup commands executed!")
            
        finally:
            await session_manager.release(session_id)
    
    async def test_cleanup_on_release(self, check_container, session_manager):
        """Test that cleanup happens after release."""
        # Acquire and create a file
        session_id = await session_manager.acquire()
        
        result = await session_manager.execute(session_id, "echo 'test' > cleanup_test.txt")
        assert result.status == "Success"
        
        result = await session_manager.execute(session_id, "ls cleanup_test.txt")
        assert result.status == "Success"
        
        # Release
        await session_manager.release(session_id)
        
        # Wait for cleanup
        await asyncio.sleep(1)
        
        # Acquire again - should be clean
        new_session_id = await session_manager.acquire()
        
        try:
            result = await session_manager.execute(new_session_id, "ls cleanup_test.txt 2>&1")
            # File should not exist
            assert "No such file" in result.stdout or result.status == "Failed"
            
            print("\nCleanup verified!")
            
        finally:
            await session_manager.release(new_session_id)


@pytest.mark.asyncio
class TestServerEndpoints:
    """Test FastAPI server endpoints via HTTP."""
    
    @pytest_asyncio.fixture
    async def server_url(self, check_container):
        """Start server and return URL."""
        import subprocess
        import time
        
        # Start server in background
        env = os.environ.copy()
        env["SWEREX_ENDPOINTS"] = SWEREX_TEST_ENDPOINT
        env["SWEREX_AUTH_TOKEN"] = SWEREX_AUTH_TOKEN
        env["SWEREX_SESSIONS_PER_CONTAINER"] = "2"
        env["SWEREX_SERVER_PORT"] = "18080"
        
        proc = subprocess.Popen(
            ["python", "-m", "verl.experimental.agent_loop.swerex_server"],
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        
        # Wait for server to start
        server_url = "http://localhost:18080"
        for _ in range(30):
            try:
                async with aiohttp.ClientSession() as client:
                    async with client.get(f"{server_url}/health", timeout=aiohttp.ClientTimeout(total=2)) as resp:
                        if resp.status == 200:
                            break
            except Exception:
                pass
            await asyncio.sleep(1)
        else:
            proc.kill()
            pytest.fail("Server failed to start")
        
        yield server_url
        
        # Cleanup
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    
    async def test_health_endpoint(self, server_url):
        """Test /health endpoint."""
        async with aiohttp.ClientSession() as client:
            async with client.get(f"{server_url}/health") as resp:
                assert resp.status == 200
                data = await resp.json()
                
                assert "status" in data
                assert "total_sessions" in data
                assert data["total_sessions"] == 2
                
                print(f"\nHealth: {data}")
    
    async def test_acquire_execute_release(self, server_url):
        """Test full session lifecycle via HTTP."""
        async with aiohttp.ClientSession() as client:
            # Acquire
            async with client.post(f"{server_url}/session/acquire", json={}) as resp:
                assert resp.status == 200
                data = await resp.json()
                session_id = data["session_id"]
            
            # Execute
            async with client.post(
                f"{server_url}/session/{session_id}/execute",
                json={"command": "echo hello"},
            ) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "Success"
                assert "hello" in data["stdout"]
            
            # Release
            async with client.post(f"{server_url}/session/{session_id}/release") as resp:
                assert resp.status == 200
            
            print("\nHTTP lifecycle test passed!")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
