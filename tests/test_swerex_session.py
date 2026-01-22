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
Comprehensive tests for SweRexSessionManager.

These tests verify:
1. Basic functionality - session creation, command execution, destruction
2. Intra-episode statefulness - cd, export, variables persist within session
3. Inter-episode isolation - shell state does NOT persist between sessions
4. Performance - first session is slow (~14s), subsequent sessions are fast (~100ms)
5. Edge cases - timeouts, invalid commands, file handling

Run with: pytest tests/experimental/agent_loop/test_swerex_session.py -v

Architecture note:
- ONE Docker deployment is shared across all sessions (started lazily on first use)
- Each session is a separate bash session with isolated shell state
- Sessions share the filesystem, but each has a unique working directory
"""

import asyncio
import base64
import importlib.util
import os
import sys
import time

import pytest

# Import the module directly to avoid verl package dependencies
module_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "verl", "experimental", "agent_loop", "swerex_session.py"
)
spec = importlib.util.spec_from_file_location("swerex_session", module_path)
swerex_session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(swerex_session)

CommandResult = swerex_session.CommandResult
SweRexSessionManager = swerex_session.SweRexSessionManager
run_stateful_bash = swerex_session.run_stateful_bash

# Configure pytest-asyncio
pytest_plugins = ('pytest_asyncio',)

# Skip all tests if Docker is not available
pytestmark = [
    pytest.mark.skipif(
        os.system("docker ps > /dev/null 2>&1") != 0,
        reason="Docker not available"
    ),
    pytest.mark.asyncio
]


class TestBasicFunctionality:
    """Test basic session lifecycle and command execution."""
    
    @pytest.mark.asyncio
    async def test_session_creation_and_destruction(self):
        """Test that sessions can be created and destroyed."""
        manager = SweRexSessionManager()
        
        try:
            # Create session
            session_id = await manager.create_session()
            assert session_id is not None
            assert len(session_id) == 32  # UUID hex
            assert manager.get_active_session_count() == 1
            assert manager.is_deployment_running() is True
            
            # Destroy session
            result = await manager.destroy_session(session_id)
            assert result is True
            assert manager.get_active_session_count() == 0
            # Deployment should still be running after destroying session
            assert manager.is_deployment_running() is True
        finally:
            await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_basic_command_execution(self):
        """Test basic command execution returns correct output."""
        manager = SweRexSessionManager()
        
        try:
            session_id = await manager.create_session()
            
            # Test echo
            result = await manager.execute_command(session_id, "echo 'Hello World'")
            assert result.status == "Success"
            assert "Hello World" in result.stdout
            assert result.return_code == 0
            
            # Test command with arguments
            result = await manager.execute_command(session_id, "ls -la /")
            assert result.status == "Success"
            assert result.return_code == 0
            
        finally:
            await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_command_with_exit_code(self):
        """Test that exit codes are properly returned."""
        manager = SweRexSessionManager()
        
        try:
            session_id = await manager.create_session()
            
            # Test successful command
            result = await manager.execute_command(session_id, "true")
            assert result.return_code == 0
            assert result.status == "Success"
            
            # Test failing command
            result = await manager.execute_command(session_id, "false")
            assert result.return_code == 1
            assert result.status == "Failed"
            
            # Test specific exit code using subshell
            # Note: Can't use "exit 42" directly as it would terminate the session
            result = await manager.execute_command(session_id, "sh -c 'exit 42'")
            assert result.return_code == 42
            
        finally:
            await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_invalid_session(self):
        """Test that invalid session returns error."""
        manager = SweRexSessionManager()
        
        result = await manager.execute_command("nonexistent_session", "echo test")
        assert result.status == "Failed"
        assert "not found" in result.stderr.lower()
    
    @pytest.mark.asyncio
    async def test_destroy_nonexistent_session(self):
        """Test that destroying nonexistent session returns False."""
        manager = SweRexSessionManager()
        
        result = await manager.destroy_session("nonexistent_session")
        assert result is False


class TestIntraEpisodeStatefulness:
    """Test that state persists within a single session (episode)."""
    
    @pytest.mark.asyncio
    async def test_cd_persistence(self):
        """Test that working directory changes persist."""
        manager = SweRexSessionManager()
        
        try:
            session_id = await manager.create_session()
            
            # Change directory
            await manager.execute_command(session_id, "cd /tmp")
            
            # Verify pwd shows /tmp
            result = await manager.execute_command(session_id, "pwd")
            assert result.stdout.strip() == "/tmp"
            
            # Change to another directory
            await manager.execute_command(session_id, "cd /var")
            
            # Verify pwd shows /var
            result = await manager.execute_command(session_id, "pwd")
            assert result.stdout.strip() == "/var"
            
        finally:
            await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_export_persistence(self):
        """Test that exported environment variables persist."""
        manager = SweRexSessionManager()
        
        try:
            session_id = await manager.create_session()
            
            # Export variable
            await manager.execute_command(session_id, "export MY_VAR='test_value'")
            
            # Verify variable is set
            result = await manager.execute_command(session_id, "echo $MY_VAR")
            assert result.stdout.strip() == "test_value"
            
            # Export another variable
            await manager.execute_command(session_id, "export ANOTHER_VAR=12345")
            
            # Verify both variables
            result = await manager.execute_command(session_id, "echo $MY_VAR $ANOTHER_VAR")
            assert "test_value" in result.stdout
            assert "12345" in result.stdout
            
        finally:
            await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_shell_variable_persistence(self):
        """Test that shell variables (not exported) persist."""
        manager = SweRexSessionManager()
        
        try:
            session_id = await manager.create_session()
            
            # Set shell variable (not exported)
            await manager.execute_command(session_id, "LOCAL_VAR=local_value")
            
            # Verify variable is set
            result = await manager.execute_command(session_id, "echo $LOCAL_VAR")
            assert result.stdout.strip() == "local_value"
            
        finally:
            await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_file_persistence(self):
        """Test that files created persist within session."""
        manager = SweRexSessionManager()
        
        try:
            session_id = await manager.create_session()
            
            # Create a file (using relative path in session's work_dir)
            await manager.execute_command(session_id, "echo 'file content' > test_file.txt")
            
            # Verify file exists and has content
            result = await manager.execute_command(session_id, "cat test_file.txt")
            assert result.stdout.strip() == "file content"
            
            # Append to file
            await manager.execute_command(session_id, "echo 'more content' >> test_file.txt")
            
            # Verify append worked
            result = await manager.execute_command(session_id, "cat test_file.txt")
            assert "file content" in result.stdout
            assert "more content" in result.stdout
            
        finally:
            await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_combined_state_persistence(self):
        """Test that multiple types of state persist together."""
        manager = SweRexSessionManager()
        
        try:
            session_id = await manager.create_session()
            
            # Get the initial work_dir
            result = await manager.execute_command(session_id, "pwd")
            initial_dir = result.stdout.strip()
            
            # Set up various state
            await manager.execute_command(session_id, "export ENV_VAR=env_value")
            await manager.execute_command(session_id, "SHELL_VAR=shell_value")
            await manager.execute_command(session_id, "echo 'data' > state_test.txt")
            
            # Verify all state in a single command
            result = await manager.execute_command(
                session_id,
                "echo pwd=$(pwd) env=$ENV_VAR shell=$SHELL_VAR file=$(cat state_test.txt)"
            )
            
            assert f"pwd={initial_dir}" in result.stdout
            assert "env=env_value" in result.stdout
            assert "shell=shell_value" in result.stdout
            assert "file=data" in result.stdout
            
        finally:
            await manager.shutdown()


class TestInterEpisodeIsolation:
    """Test that state does NOT persist between different sessions (episodes).
    
    Note: Sessions share the same Docker container but have:
    - Isolated shell state (working directory, environment variables)
    - Isolated working directories (/tmp/session_<id>/)
    - SHARED filesystem (files outside work_dir can be seen by other sessions)
    """
    
    @pytest.mark.asyncio
    async def test_cd_isolation(self):
        """Test that working directory does not leak between sessions."""
        manager = SweRexSessionManager()
        
        try:
            # Session 1: Change directory
            session1 = await manager.create_session()
            await manager.execute_command(session1, "cd /tmp")
            result = await manager.execute_command(session1, "pwd")
            assert result.stdout.strip() == "/tmp"
            await manager.destroy_session(session1)
            
            # Session 2: Should be in its own unique work_dir, not /tmp
            session2 = await manager.create_session()
            result = await manager.execute_command(session2, "pwd")
            assert result.stdout.strip() != "/tmp"
            assert result.stdout.strip().startswith("/tmp/session_")  # In unique work_dir
            await manager.destroy_session(session2)
            
        finally:
            await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_env_var_isolation(self):
        """Test that environment variables do not leak between sessions."""
        manager = SweRexSessionManager()
        
        try:
            # Session 1: Set env var
            session1 = await manager.create_session()
            await manager.execute_command(session1, "export ISOLATED_VAR=session1_value")
            result = await manager.execute_command(session1, "echo $ISOLATED_VAR")
            assert result.stdout.strip() == "session1_value"
            await manager.destroy_session(session1)
            
            # Session 2: Variable should NOT exist
            session2 = await manager.create_session()
            result = await manager.execute_command(
                session2, 
                "echo \"VAR=${ISOLATED_VAR:-NOT_SET}\""
            )
            assert "NOT_SET" in result.stdout
            await manager.destroy_session(session2)
            
        finally:
            await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_work_dir_isolation(self):
        """Test that files in session work_dir are isolated between sessions."""
        manager = SweRexSessionManager()
        
        try:
            # Session 1: Create file in work_dir (relative path)
            session1 = await manager.create_session()
            await manager.execute_command(session1, "echo 'secret' > isolated_file.txt")
            result = await manager.execute_command(session1, "cat isolated_file.txt")
            assert result.stdout.strip() == "secret"
            work_dir1 = (await manager.execute_command(session1, "pwd")).stdout.strip()
            await manager.destroy_session(session1)
            
            # Session 2: File should NOT exist (different work_dir)
            session2 = await manager.create_session()
            result = await manager.execute_command(session2, "cat isolated_file.txt 2>&1")
            assert result.return_code != 0 or "No such file" in result.stdout
            
            # Verify session2 has a different work_dir
            work_dir2 = (await manager.execute_command(session2, "pwd")).stdout.strip()
            assert work_dir1 != work_dir2
            await manager.destroy_session(session2)
            
        finally:
            await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_shared_filesystem_outside_workdir(self):
        """Test that files created with absolute paths ARE visible to other sessions.
        
        This is expected behavior - sessions share the same container filesystem.
        Only shell state and the starting work_dir are isolated.
        """
        manager = SweRexSessionManager()
        
        try:
            # Session 1: Create file with absolute path
            session1 = await manager.create_session()
            await manager.execute_command(session1, "echo 'shared_data' > /tmp/shared_file.txt")
            await manager.destroy_session(session1)
            
            # Session 2: File SHOULD exist (shared filesystem)
            session2 = await manager.create_session()
            result = await manager.execute_command(session2, "cat /tmp/shared_file.txt")
            assert result.stdout.strip() == "shared_data"
            
            # Cleanup
            await manager.execute_command(session2, "rm /tmp/shared_file.txt")
            await manager.destroy_session(session2)
            
        finally:
            await manager.shutdown()


class TestFileHandling:
    """Test file upload functionality."""
    
    @pytest.mark.asyncio
    async def test_file_upload_at_creation(self):
        """Test that files can be uploaded when creating session."""
        manager = SweRexSessionManager()
        
        content = "Hello from uploaded file!"
        content_b64 = base64.b64encode(content.encode()).decode()
        
        try:
            session_id = await manager.create_session(
                files={"uploaded.txt": content_b64}
            )
            
            result = await manager.execute_command(session_id, "cat uploaded.txt")
            assert content in result.stdout
            
        finally:
            await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_file_upload_with_directory(self):
        """Test that files in subdirectories are handled."""
        manager = SweRexSessionManager()
        
        content = "Nested file content"
        content_b64 = base64.b64encode(content.encode()).decode()
        
        try:
            session_id = await manager.create_session(
                files={"subdir/nested/file.txt": content_b64}
            )
            
            result = await manager.execute_command(
                session_id, 
                "cat subdir/nested/file.txt"
            )
            assert content in result.stdout
            
        finally:
            await manager.shutdown()


class TestStartupCommands:
    """Test startup commands functionality."""
    
    @pytest.mark.asyncio
    async def test_startup_commands(self):
        """Test that startup commands are executed."""
        manager = SweRexSessionManager()
        
        try:
            session_id = await manager.create_session(
                startup_commands=[
                    "cd /tmp",
                    "export STARTUP_VAR=initialized",
                    "echo 'setup complete' > setup.log"
                ]
            )
            
            # Verify startup state
            result = await manager.execute_command(session_id, "pwd")
            assert result.stdout.strip() == "/tmp"
            
            result = await manager.execute_command(session_id, "echo $STARTUP_VAR")
            assert result.stdout.strip() == "initialized"
            
            result = await manager.execute_command(session_id, "cat setup.log")
            assert "setup complete" in result.stdout
            
        finally:
            await manager.shutdown()


class TestEdgeCases:
    """Test edge cases and error handling."""
    
    @pytest.mark.asyncio
    async def test_timeout_handling(self):
        """Test that command timeout is handled."""
        manager = SweRexSessionManager(default_timeout=2)
        
        try:
            session_id = await manager.create_session()
            
            # Command that should timeout
            result = await manager.execute_command(
                session_id, 
                "sleep 10",
                timeout=1
            )
            
            assert result.status == "Failed"
            assert "timed out" in result.stderr.lower()
            
        finally:
            await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_invalid_command(self):
        """Test that invalid commands return error."""
        manager = SweRexSessionManager()
        
        try:
            session_id = await manager.create_session()
            
            result = await manager.execute_command(
                session_id, 
                "nonexistent_command_xyz"
            )
            
            assert result.return_code != 0
            
        finally:
            await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_large_output(self):
        """Test handling of large command output."""
        manager = SweRexSessionManager()
        
        try:
            session_id = await manager.create_session()
            
            # Generate large output
            result = await manager.execute_command(
                session_id,
                "for i in $(seq 1 1000); do echo \"Line $i\"; done"
            )
            
            assert result.status == "Success"
            assert "Line 1" in result.stdout
            assert "Line 1000" in result.stdout
            
        finally:
            await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_special_characters_in_command(self):
        """Test handling of special characters in commands."""
        manager = SweRexSessionManager()
        
        try:
            session_id = await manager.create_session()
            
            # Test various special characters
            result = await manager.execute_command(
                session_id,
                "echo 'single quotes' \"double quotes\" `backticks` $HOME"
            )
            assert result.status == "Success"
            
            # Test pipes
            result = await manager.execute_command(
                session_id,
                "echo 'hello world' | grep hello"
            )
            assert result.status == "Success"
            assert "hello" in result.stdout
            
        finally:
            await manager.shutdown()


class TestConvenienceFunction:
    """Test the run_stateful_bash convenience function."""
    
    @pytest.mark.asyncio
    async def test_run_stateful_bash(self):
        """Test the convenience function."""
        results = await run_stateful_bash([
            "cd /tmp",
            "pwd",
            "export X=test",
            "echo $X"
        ])
        
        assert len(results) == 4
        assert results[1].stdout.strip() == "/tmp"
        assert results[3].stdout.strip() == "test"


class TestPerformance:
    """Test performance characteristics of the persistent deployment approach.
    
    Key expectations:
    - First session creation: ~14 seconds (Docker container startup)
    - Subsequent session creation: ~100ms (just bash session creation)
    """
    
    @pytest.mark.asyncio
    async def test_first_session_is_slow_subsequent_fast(self):
        """Test that first session is slow but subsequent sessions are fast."""
        manager = SweRexSessionManager()
        timing_info = {}
        
        try:
            # First session - should be slow (Docker startup)
            start = time.time()
            session1 = await manager.create_session()
            first_session_time = time.time() - start
            timing_info["first_session_time"] = first_session_time
            
            # Verify it works
            result = await manager.execute_command(session1, "echo 'session1'")
            assert "session1" in result.stdout
            await manager.destroy_session(session1)
            
            # Second session - should be fast (deployment already running)
            start = time.time()
            session2 = await manager.create_session()
            second_session_time = time.time() - start
            timing_info["second_session_time"] = second_session_time
            
            # Verify it works
            result = await manager.execute_command(session2, "echo 'session2'")
            assert "session2" in result.stdout
            await manager.destroy_session(session2)
            
            # Third session - should also be fast
            start = time.time()
            session3 = await manager.create_session()
            third_session_time = time.time() - start
            timing_info["third_session_time"] = third_session_time
            
            # Verify it works
            result = await manager.execute_command(session3, "echo 'session3'")
            assert "session3" in result.stdout
            await manager.destroy_session(session3)
            
            # Print timing info (will show in pytest -v output)
            print(f"\n{'='*60}")
            print("TIMING RESULTS (Persistent Deployment Approach):")
            print(f"  First session (includes Docker startup): {first_session_time:.2f}s")
            print(f"  Second session (bash session only):      {second_session_time:.2f}s")
            print(f"  Third session (bash session only):       {third_session_time:.2f}s")
            print(f"  Speedup (first vs second):               {first_session_time/second_session_time:.1f}x")
            print(f"{'='*60}")
            
            # Assertions on timing
            # First session should be significantly slower than subsequent ones
            # We expect first to be > 5 seconds, subsequent < 2 seconds
            assert second_session_time < first_session_time, \
                f"Second session ({second_session_time:.2f}s) should be faster than first ({first_session_time:.2f}s)"
            assert third_session_time < first_session_time, \
                f"Third session ({third_session_time:.2f}s) should be faster than first ({first_session_time:.2f}s)"
            
            # More specific: subsequent sessions should be < 2 seconds
            assert second_session_time < 2.0, \
                f"Second session took {second_session_time:.2f}s, expected < 2.0s"
            assert third_session_time < 2.0, \
                f"Third session took {third_session_time:.2f}s, expected < 2.0s"
            
        finally:
            await manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_multiple_concurrent_sessions(self):
        """Test that multiple sessions can be active simultaneously."""
        manager = SweRexSessionManager()
        
        try:
            # Create multiple sessions
            start = time.time()
            session1 = await manager.create_session()  # This will be slow (first)
            session2 = await manager.create_session()  # This should be fast
            session3 = await manager.create_session()  # This should be fast
            total_time = time.time() - start
            
            assert manager.get_active_session_count() == 3
            
            # Each session should have isolated state
            await manager.execute_command(session1, "export VAR=s1")
            await manager.execute_command(session2, "export VAR=s2")
            await manager.execute_command(session3, "export VAR=s3")
            
            r1 = await manager.execute_command(session1, "echo $VAR")
            r2 = await manager.execute_command(session2, "echo $VAR")
            r3 = await manager.execute_command(session3, "echo $VAR")
            
            assert r1.stdout.strip() == "s1"
            assert r2.stdout.strip() == "s2"
            assert r3.stdout.strip() == "s3"
            
            print(f"\n3 concurrent sessions created in {total_time:.2f}s")
            
        finally:
            await manager.shutdown()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
