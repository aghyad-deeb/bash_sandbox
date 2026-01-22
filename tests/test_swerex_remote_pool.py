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
Comprehensive reliability tests for SweRexRemoteSessionPool.

Tests cover:
1. High-concurrency (1000 workers)
2. Stress testing with timing
3. State isolation
4. Error handling
5. Edge cases

Run with: python3.12 -m pytest tests/experimental/agent_loop/test_swerex_remote_pool.py -v -s
"""

import asyncio
import importlib.util
import os
import time
from typing import Any

import pytest

# Import the module directly to avoid verl package dependencies
module_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "verl", "experimental", "agent_loop", "swerex_session.py"
)
spec = importlib.util.spec_from_file_location("swerex_session", module_path)
swerex_session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(swerex_session)

SweRexRemoteSessionPool = swerex_session.SweRexRemoteSessionPool
CommandResult = swerex_session.CommandResult

# Configure pytest-asyncio
pytest_plugins = ('pytest_asyncio',)

# Test configuration
TEST_ENDPOINT = os.getenv("TEST_SWEREX_ENDPOINT", "http://127.0.0.1:18000")
TEST_AUTH_TOKEN = os.getenv("TEST_SWEREX_AUTH_TOKEN", "test-token")

# Skip all tests if container is not available
def is_container_available():
    import socket
    try:
        host = TEST_ENDPOINT.replace("http://", "").replace("https://", "")
        if ":" in host:
            host, port = host.rsplit(":", 1)
            port = int(port)
        else:
            port = 8000
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False

pytestmark = [
    pytest.mark.skipif(
        not is_container_available(),
        reason=f"Sandbox container not available at {TEST_ENDPOINT}"
    ),
    pytest.mark.asyncio
]


def percentile(data: list[float], p: float) -> float:
    """Calculate percentile of sorted data."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = int(len(sorted_data) * p)
    idx = min(idx, len(sorted_data) - 1)
    return sorted_data[idx]


def print_timing_report(name: str, timings: dict[str, list[float]], total_time: float, 
                        num_ops: int, errors: list[Any]):
    """Print a formatted timing report."""
    print(f"\n{'='*70}")
    print(f"{name}")
    print(f"{'='*70}")
    print(f"Total time:    {total_time:.2f}s")
    print(f"Operations:    {num_ops}")
    print(f"Throughput:    {num_ops/total_time:.1f} ops/sec")
    print(f"Errors:        {len(errors)}")
    
    for op, data in timings.items():
        if not data:
            continue
        print(f"\n{op.upper()} latency (ms):")
        print(f"  min:  {min(data)*1000:.1f}")
        print(f"  p50:  {percentile(data, 0.50)*1000:.1f}")
        print(f"  p95:  {percentile(data, 0.95)*1000:.1f}")
        print(f"  p99:  {percentile(data, 0.99)*1000:.1f}")
        print(f"  max:  {max(data)*1000:.1f}")
    
    if errors:
        print(f"\nFirst 5 errors:")
        for err in errors[:5]:
            print(f"  {err}")
    
    print(f"{'='*70}")


class TestHighConcurrency:
    """Test high-concurrency scenarios with 1000 workers."""
    
    @pytest.mark.asyncio
    async def test_1000_concurrent_workers(self):
        """
        Simulate 1000 concurrent episode workers competing for pool sessions.
        Each worker: acquire -> execute commands -> release
        """
        NUM_WORKERS = 1000
        POOL_SIZE = 8  # High contention: 1000 workers, 8 sessions
        
        pool = SweRexRemoteSessionPool(
            endpoints=[TEST_ENDPOINT],
            auth_token=TEST_AUTH_TOKEN,
            sessions_per_endpoint=POOL_SIZE
        )
        
        try:
            await pool.initialize()
            assert pool.get_pool_size() == POOL_SIZE
            
            timings = {"acquire": [], "execute": [], "release": [], "total": []}
            errors = []
            completed = [0]  # Use list to allow modification in nested function
            
            async def worker(worker_id: int):
                start_total = time.perf_counter()
                try:
                    # Acquire session
                    t0 = time.perf_counter()
                    session_id = await pool.acquire_session(timeout=120.0)
                    timings["acquire"].append(time.perf_counter() - t0)
                    
                    # Execute command
                    t0 = time.perf_counter()
                    result = await pool.execute_command(session_id, f"echo worker_{worker_id}")
                    timings["execute"].append(time.perf_counter() - t0)
                    
                    if result.status != "Success":
                        errors.append((worker_id, f"Command failed: {result.stderr}"))
                    
                    # Release session
                    t0 = time.perf_counter()
                    await pool.release_session(session_id)
                    timings["release"].append(time.perf_counter() - t0)
                    
                    timings["total"].append(time.perf_counter() - start_total)
                    completed[0] += 1
                    
                except Exception as e:
                    errors.append((worker_id, str(e)))
            
            # Run all workers concurrently
            start = time.perf_counter()
            await asyncio.gather(*[worker(i) for i in range(NUM_WORKERS)])
            total_time = time.perf_counter() - start
            
            # Report results
            print_timing_report(
                f"1000 WORKERS / {POOL_SIZE} SESSIONS",
                timings, total_time, NUM_WORKERS, errors
            )
            
            # Assertions
            assert len(errors) == 0, f"Got {len(errors)} errors: {errors[:5]}"
            assert completed[0] == NUM_WORKERS, f"Only {completed[0]}/{NUM_WORKERS} completed"
            assert pool.get_available_count() == POOL_SIZE, "Pool should be fully available after test"
            
        finally:
            await pool.shutdown()
    
    @pytest.mark.asyncio
    async def test_concurrent_with_multiple_commands(self):
        """Each worker executes multiple commands per session."""
        NUM_WORKERS = 100
        COMMANDS_PER_WORKER = 10
        POOL_SIZE = 4
        
        pool = SweRexRemoteSessionPool(
            endpoints=[TEST_ENDPOINT],
            auth_token=TEST_AUTH_TOKEN,
            sessions_per_endpoint=POOL_SIZE
        )
        
        try:
            await pool.initialize()
            
            timings = {"acquire": [], "execute": [], "release": [], "total": []}
            errors = []
            total_commands = [0]
            
            async def worker(worker_id: int):
                start_total = time.perf_counter()
                try:
                    t0 = time.perf_counter()
                    session_id = await pool.acquire_session(timeout=120.0)
                    timings["acquire"].append(time.perf_counter() - t0)
                    
                    # Execute multiple commands
                    for cmd_id in range(COMMANDS_PER_WORKER):
                        t0 = time.perf_counter()
                        result = await pool.execute_command(
                            session_id, 
                            f"echo worker_{worker_id}_cmd_{cmd_id}"
                        )
                        timings["execute"].append(time.perf_counter() - t0)
                        
                        if result.status == "Success":
                            total_commands[0] += 1
                        else:
                            errors.append((worker_id, cmd_id, result.stderr))
                    
                    t0 = time.perf_counter()
                    await pool.release_session(session_id)
                    timings["release"].append(time.perf_counter() - t0)
                    
                    timings["total"].append(time.perf_counter() - start_total)
                    
                except Exception as e:
                    errors.append((worker_id, -1, str(e)))
            
            start = time.perf_counter()
            await asyncio.gather(*[worker(i) for i in range(NUM_WORKERS)])
            total_time = time.perf_counter() - start
            
            expected_commands = NUM_WORKERS * COMMANDS_PER_WORKER
            print_timing_report(
                f"{NUM_WORKERS} WORKERS x {COMMANDS_PER_WORKER} COMMANDS",
                timings, total_time, expected_commands, errors
            )
            
            assert len(errors) == 0, f"Got {len(errors)} errors"
            assert total_commands[0] == expected_commands
            
        finally:
            await pool.shutdown()


class TestStressWithTiming:
    """Stress tests with detailed timing measurements."""
    
    @pytest.mark.asyncio
    async def test_1000_sequential_cycles(self):
        """Sequential acquire/execute/release cycles for baseline timing."""
        NUM_CYCLES = 100  # Sequential is slow, so fewer cycles
        POOL_SIZE = 2
        
        pool = SweRexRemoteSessionPool(
            endpoints=[TEST_ENDPOINT],
            auth_token=TEST_AUTH_TOKEN,
            sessions_per_endpoint=POOL_SIZE
        )
        
        try:
            await pool.initialize()
            
            timings = {"acquire": [], "execute": [], "release": [], "total": []}
            errors = []
            
            start = time.perf_counter()
            for i in range(NUM_CYCLES):
                cycle_start = time.perf_counter()
                try:
                    t0 = time.perf_counter()
                    session_id = await pool.acquire_session()
                    timings["acquire"].append(time.perf_counter() - t0)
                    
                    t0 = time.perf_counter()
                    result = await pool.execute_command(session_id, f"echo cycle_{i}")
                    timings["execute"].append(time.perf_counter() - t0)
                    
                    t0 = time.perf_counter()
                    await pool.release_session(session_id)
                    timings["release"].append(time.perf_counter() - t0)
                    
                    timings["total"].append(time.perf_counter() - cycle_start)
                    
                except Exception as e:
                    errors.append((i, str(e)))
            
            total_time = time.perf_counter() - start
            
            print_timing_report(
                f"{NUM_CYCLES} SEQUENTIAL CYCLES (baseline)",
                timings, total_time, NUM_CYCLES, errors
            )
            
            assert len(errors) == 0
            # Sequential should be fast - each cycle under 1 second
            assert percentile(timings["total"], 0.99) < 1.0, "p99 total latency should be < 1s"
            
        finally:
            await pool.shutdown()
    
    @pytest.mark.asyncio
    async def test_rapid_acquire_release(self):
        """Test rapid acquire/release without command execution."""
        NUM_CYCLES = 500
        POOL_SIZE = 4
        
        pool = SweRexRemoteSessionPool(
            endpoints=[TEST_ENDPOINT],
            auth_token=TEST_AUTH_TOKEN,
            sessions_per_endpoint=POOL_SIZE
        )
        
        try:
            await pool.initialize()
            
            timings = {"acquire": [], "release": [], "cycle": []}
            errors = []
            
            async def rapid_cycle(cycle_id: int):
                try:
                    cycle_start = time.perf_counter()
                    
                    t0 = time.perf_counter()
                    session_id = await pool.acquire_session(timeout=60.0)
                    timings["acquire"].append(time.perf_counter() - t0)
                    
                    # Minimal work - just verify we have the session
                    await asyncio.sleep(0.001)  # 1ms simulated work
                    
                    t0 = time.perf_counter()
                    await pool.release_session(session_id)
                    timings["release"].append(time.perf_counter() - t0)
                    
                    timings["cycle"].append(time.perf_counter() - cycle_start)
                    
                except Exception as e:
                    errors.append((cycle_id, str(e)))
            
            start = time.perf_counter()
            await asyncio.gather(*[rapid_cycle(i) for i in range(NUM_CYCLES)])
            total_time = time.perf_counter() - start
            
            print_timing_report(
                f"{NUM_CYCLES} RAPID ACQUIRE/RELEASE CYCLES",
                timings, total_time, NUM_CYCLES, errors
            )
            
            assert len(errors) == 0
            assert pool.get_available_count() == POOL_SIZE
            
        finally:
            await pool.shutdown()


class TestStateIsolation:
    """Test that sessions are properly isolated."""
    
    @pytest.mark.asyncio
    async def test_env_var_isolation(self):
        """Environment variables should not leak between sessions."""
        POOL_SIZE = 4
        
        pool = SweRexRemoteSessionPool(
            endpoints=[TEST_ENDPOINT],
            auth_token=TEST_AUTH_TOKEN,
            sessions_per_endpoint=POOL_SIZE
        )
        
        try:
            await pool.initialize()
            
            # Session 1: Set env var
            session1 = await pool.acquire_session()
            await pool.execute_command(session1, "export SECRET_VAR='session1_secret'")
            result = await pool.execute_command(session1, "echo $SECRET_VAR")
            assert "session1_secret" in result.stdout
            await pool.release_session(session1)
            
            # Session 2: Should NOT see the env var
            session2 = await pool.acquire_session()
            result = await pool.execute_command(session2, "echo \"VAR=${SECRET_VAR:-NOT_SET}\"")
            assert "NOT_SET" in result.stdout, f"Env var leaked! Got: {result.stdout}"
            await pool.release_session(session2)
            
            print("\nENV VAR ISOLATION: PASSED")
            
        finally:
            await pool.shutdown()
    
    @pytest.mark.asyncio
    async def test_working_dir_isolation(self):
        """Working directories should be isolated between sessions."""
        POOL_SIZE = 4
        
        pool = SweRexRemoteSessionPool(
            endpoints=[TEST_ENDPOINT],
            auth_token=TEST_AUTH_TOKEN,
            sessions_per_endpoint=POOL_SIZE
        )
        
        try:
            await pool.initialize()
            
            # Acquire two sessions simultaneously
            session1 = await pool.acquire_session()
            session2 = await pool.acquire_session()
            
            # Check working directories are different
            result1 = await pool.execute_command(session1, "pwd")
            result2 = await pool.execute_command(session2, "pwd")
            
            wd1 = result1.stdout.strip()
            wd2 = result2.stdout.strip()
            
            assert wd1 != wd2, f"Working dirs should be different: {wd1} vs {wd2}"
            assert "/tmp/sessions/" in wd1
            assert "/tmp/sessions/" in wd2
            
            await pool.release_session(session1)
            await pool.release_session(session2)
            
            print(f"\nWORKING DIR ISOLATION: PASSED ({wd1} vs {wd2})")
            
        finally:
            await pool.shutdown()
    
    @pytest.mark.asyncio
    async def test_file_isolation(self):
        """Files created in one session should not appear in another."""
        POOL_SIZE = 4
        
        pool = SweRexRemoteSessionPool(
            endpoints=[TEST_ENDPOINT],
            auth_token=TEST_AUTH_TOKEN,
            sessions_per_endpoint=POOL_SIZE
        )
        
        try:
            await pool.initialize()
            
            # Session 1: Create a file
            session1 = await pool.acquire_session()
            await pool.execute_command(session1, "echo 'secret_data' > secret.txt")
            result = await pool.execute_command(session1, "cat secret.txt")
            assert "secret_data" in result.stdout
            await pool.release_session(session1)
            
            # Session 2: File should NOT exist
            session2 = await pool.acquire_session()
            result = await pool.execute_command(session2, "cat secret.txt 2>&1 || echo 'FILE_NOT_FOUND'")
            assert "FILE_NOT_FOUND" in result.stdout or "No such file" in result.stdout
            await pool.release_session(session2)
            
            print("\nFILE ISOLATION: PASSED")
            
        finally:
            await pool.shutdown()
    
    @pytest.mark.asyncio
    async def test_cleanup_on_reacquire(self):
        """Working directory should be cleaned when session is reacquired."""
        POOL_SIZE = 1  # Force reuse of same session
        
        pool = SweRexRemoteSessionPool(
            endpoints=[TEST_ENDPOINT],
            auth_token=TEST_AUTH_TOKEN,
            sessions_per_endpoint=POOL_SIZE
        )
        
        try:
            await pool.initialize()
            
            # First acquisition: Create files
            session1 = await pool.acquire_session()
            await pool.execute_command(session1, "echo 'data1' > file1.txt")
            await pool.execute_command(session1, "echo 'data2' > file2.txt")
            await pool.execute_command(session1, "mkdir subdir && echo 'data3' > subdir/file3.txt")
            
            result = await pool.execute_command(session1, "find . -type f | wc -l")
            files_before = int(result.stdout.strip())
            assert files_before == 3, f"Expected 3 files, got {files_before}"
            
            await pool.release_session(session1)
            
            # Second acquisition: Files should be cleaned
            session2 = await pool.acquire_session()
            result = await pool.execute_command(session2, "find . -type f 2>/dev/null | wc -l")
            files_after = int(result.stdout.strip())
            
            assert files_after == 0, f"Expected 0 files after reacquire, got {files_after}"
            await pool.release_session(session2)
            
            print(f"\nCLEANUP ON REACQUIRE: PASSED ({files_before} -> {files_after} files)")
            
        finally:
            await pool.shutdown()


class TestErrorHandling:
    """Test error handling scenarios."""
    
    @pytest.mark.asyncio
    async def test_command_timeout(self):
        """Commands that exceed timeout should fail gracefully."""
        pool = SweRexRemoteSessionPool(
            endpoints=[TEST_ENDPOINT],
            auth_token=TEST_AUTH_TOKEN,
            sessions_per_endpoint=2,
            default_timeout=2.0  # 2 second timeout
        )
        
        try:
            await pool.initialize()
            
            session_id = await pool.acquire_session()
            
            # Command that exceeds timeout
            result = await pool.execute_command(session_id, "sleep 10", timeout=1.0)
            
            assert result.status == "Failed"
            assert "timed out" in result.stderr.lower()
            
            await pool.release_session(session_id)
            
            print("\nCOMMAND TIMEOUT: PASSED")
            
        finally:
            await pool.shutdown()
    
    @pytest.mark.asyncio
    async def test_pool_exhaustion_timeout(self):
        """Acquiring when pool is full should timeout."""
        POOL_SIZE = 2
        
        pool = SweRexRemoteSessionPool(
            endpoints=[TEST_ENDPOINT],
            auth_token=TEST_AUTH_TOKEN,
            sessions_per_endpoint=POOL_SIZE
        )
        
        try:
            await pool.initialize()
            
            # Acquire all sessions
            sessions = []
            for _ in range(POOL_SIZE):
                session_id = await pool.acquire_session()
                sessions.append(session_id)
            
            assert pool.get_available_count() == 0
            
            # Try to acquire another - should timeout
            with pytest.raises(TimeoutError):
                await pool.acquire_session(timeout=1.0)
            
            # Release and verify recovery
            for session_id in sessions:
                await pool.release_session(session_id)
            
            assert pool.get_available_count() == POOL_SIZE
            
            print("\nPOOL EXHAUSTION TIMEOUT: PASSED")
            
        finally:
            await pool.shutdown()
    
    @pytest.mark.asyncio
    async def test_invalid_command(self):
        """Invalid commands should return proper error codes."""
        pool = SweRexRemoteSessionPool(
            endpoints=[TEST_ENDPOINT],
            auth_token=TEST_AUTH_TOKEN,
            sessions_per_endpoint=2
        )
        
        try:
            await pool.initialize()
            
            session_id = await pool.acquire_session()
            
            # Non-existent command
            result = await pool.execute_command(session_id, "nonexistent_command_xyz")
            assert result.status == "Failed"
            assert result.return_code != 0
            
            # Command with non-zero exit
            result = await pool.execute_command(session_id, "sh -c 'exit 42'")
            assert result.return_code == 42
            
            await pool.release_session(session_id)
            
            print("\nINVALID COMMAND HANDLING: PASSED")
            
        finally:
            await pool.shutdown()
    
    @pytest.mark.asyncio
    async def test_uninitialized_pool(self):
        """Operations on uninitialized pool should raise errors."""
        pool = SweRexRemoteSessionPool(
            endpoints=[TEST_ENDPOINT],
            auth_token=TEST_AUTH_TOKEN,
            sessions_per_endpoint=2
        )
        
        # Don't call initialize()
        
        with pytest.raises(RuntimeError, match="not initialized"):
            await pool.acquire_session()
        
        print("\nUNINITIALIZED POOL: PASSED")


class TestEdgeCases:
    """Test edge cases and unusual inputs."""
    
    @pytest.mark.asyncio
    async def test_large_output(self):
        """Test handling of large command outputs."""
        pool = SweRexRemoteSessionPool(
            endpoints=[TEST_ENDPOINT],
            auth_token=TEST_AUTH_TOKEN,
            sessions_per_endpoint=2
        )
        
        try:
            await pool.initialize()
            
            session_id = await pool.acquire_session()
            
            # Generate 10KB of output
            result = await pool.execute_command(
                session_id,
                "for i in $(seq 1 1000); do echo \"Line $i: $(head -c 10 /dev/urandom | base64)\"; done"
            )
            
            assert result.status == "Success"
            assert len(result.stdout) > 10000, f"Expected > 10KB output, got {len(result.stdout)} bytes"
            assert "Line 1:" in result.stdout
            assert "Line 1000:" in result.stdout
            
            await pool.release_session(session_id)
            
            print(f"\nLARGE OUTPUT: PASSED ({len(result.stdout)} bytes)")
            
        finally:
            await pool.shutdown()
    
    @pytest.mark.asyncio
    async def test_special_characters(self):
        """Test handling of special characters in commands."""
        pool = SweRexRemoteSessionPool(
            endpoints=[TEST_ENDPOINT],
            auth_token=TEST_AUTH_TOKEN,
            sessions_per_endpoint=2
        )
        
        try:
            await pool.initialize()
            
            session_id = await pool.acquire_session()
            
            # Single quotes
            result = await pool.execute_command(session_id, "echo 'single quotes'")
            assert "single quotes" in result.stdout
            
            # Double quotes
            result = await pool.execute_command(session_id, 'echo "double quotes"')
            assert "double quotes" in result.stdout
            
            # Pipes
            result = await pool.execute_command(session_id, "echo 'hello world' | grep hello")
            assert "hello" in result.stdout
            
            # Redirections
            result = await pool.execute_command(
                session_id,
                "echo 'test' > testfile.txt && cat testfile.txt"
            )
            assert "test" in result.stdout
            
            # Command substitution
            result = await pool.execute_command(session_id, "echo $(echo nested)")
            assert "nested" in result.stdout
            
            await pool.release_session(session_id)
            
            print("\nSPECIAL CHARACTERS: PASSED")
            
        finally:
            await pool.shutdown()
    
    @pytest.mark.asyncio
    async def test_empty_command(self):
        """Test handling of empty or whitespace commands."""
        pool = SweRexRemoteSessionPool(
            endpoints=[TEST_ENDPOINT],
            auth_token=TEST_AUTH_TOKEN,
            sessions_per_endpoint=2
        )
        
        try:
            await pool.initialize()
            
            session_id = await pool.acquire_session()
            
            # Empty string - should succeed (bash does nothing)
            result = await pool.execute_command(session_id, "")
            # Empty command might succeed or fail depending on shell
            
            # Whitespace only
            result = await pool.execute_command(session_id, "   ")
            
            # Comment only
            result = await pool.execute_command(session_id, "# this is a comment")
            assert result.return_code == 0
            
            await pool.release_session(session_id)
            
            print("\nEMPTY COMMAND: PASSED")
            
        finally:
            await pool.shutdown()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
