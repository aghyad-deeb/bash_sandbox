#!/usr/bin/env python3
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
Tests for file upload and fetching functionality.

These tests verify:
1. Files can be uploaded during session acquisition
2. Files can be fetched after command execution
3. Multiple files can be handled
4. Binary-safe base64 encoding/decoding
5. Non-existent file handling
6. Large file handling

Requirements:
    - SweRex server running at SWEREX_SERVER_URL (default: http://localhost:8080)
    - Or individual container at SWEREX_TEST_ENDPOINT

Usage:
    # Start server with ./start.sh, then:
    pytest tests/test_file_operations.py -v -s
    
    # Or with specific server URL:
    SWEREX_SERVER_URL=http://localhost:8080 pytest tests/test_file_operations.py -v -s
"""

import asyncio
import base64
import os
import socket
import sys

import aiohttp
import pytest
import pytest_asyncio

# Test configuration
SWEREX_SERVER_URL = os.getenv("SWEREX_SERVER_URL", "http://localhost:8080")

# Configure pytest-asyncio
pytest_plugins = ('pytest_asyncio',)


def is_server_available() -> bool:
    """Check if server is available."""
    try:
        url = SWEREX_SERVER_URL.replace("http://", "").replace("https://", "")
        if ":" in url:
            host, port = url.split(":")
            port = int(port)
        else:
            host = url
            port = 80
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


@pytest.fixture
def check_server():
    """Skip if server not available."""
    if not is_server_available():
        pytest.skip(f"Server not available at {SWEREX_SERVER_URL}")


@pytest_asyncio.fixture
async def http_client():
    """Create an aiohttp client session."""
    async with aiohttp.ClientSession() as session:
        yield session


class TestFileUpload:
    """Tests for file upload during session acquisition."""
    
    @pytest.mark.asyncio
    async def test_single_file_upload(self, check_server, http_client):
        """Test uploading a single file during session acquisition."""
        # Prepare file content
        content = "Hello, World!\nThis is a test file."
        encoded = base64.b64encode(content.encode()).decode()
        
        # Acquire session with file
        async with http_client.post(
            f"{SWEREX_SERVER_URL}/session/acquire",
            json={"files": {"test.txt": encoded}}
        ) as resp:
            assert resp.status == 200, await resp.text()
            data = await resp.json()
            session_id = data["session_id"]
        
        try:
            # Verify file exists and has correct content
            async with http_client.post(
                f"{SWEREX_SERVER_URL}/session/{session_id}/execute",
                json={"command": "cat test.txt"}
            ) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "Success"
                assert data["stdout"].strip() == content
        finally:
            # Release session
            await http_client.post(f"{SWEREX_SERVER_URL}/session/{session_id}/release")
    
    @pytest.mark.asyncio
    async def test_multiple_files_upload(self, check_server, http_client):
        """Test uploading multiple files during session acquisition."""
        files = {
            "file1.txt": base64.b64encode(b"Content of file 1").decode(),
            "file2.txt": base64.b64encode(b"Content of file 2").decode(),
            "subdir/file3.txt": base64.b64encode(b"Content in subdirectory").decode(),
        }
        
        # Acquire session with files
        async with http_client.post(
            f"{SWEREX_SERVER_URL}/session/acquire",
            json={"files": files}
        ) as resp:
            assert resp.status == 200
            data = await resp.json()
            session_id = data["session_id"]
        
        try:
            # Verify all files exist
            async with http_client.post(
                f"{SWEREX_SERVER_URL}/session/{session_id}/execute",
                json={"command": "cat file1.txt && echo '---' && cat file2.txt && echo '---' && cat subdir/file3.txt"}
            ) as resp:
                data = await resp.json()
                assert data["status"] == "Success"
                parts = data["stdout"].split("---")
                assert "Content of file 1" in parts[0]
                assert "Content of file 2" in parts[1]
                assert "Content in subdirectory" in parts[2]
        finally:
            await http_client.post(f"{SWEREX_SERVER_URL}/session/{session_id}/release")
    
    @pytest.mark.asyncio
    async def test_binary_file_upload(self, check_server, http_client):
        """Test uploading binary content (non-UTF8)."""
        # Create binary content with null bytes and high bytes
        binary_content = bytes(range(256))
        encoded = base64.b64encode(binary_content).decode()
        
        # Acquire session with binary file
        async with http_client.post(
            f"{SWEREX_SERVER_URL}/session/acquire",
            json={"files": {"binary.bin": encoded}}
        ) as resp:
            assert resp.status == 200
            data = await resp.json()
            session_id = data["session_id"]
        
        try:
            # Verify file size matches
            async with http_client.post(
                f"{SWEREX_SERVER_URL}/session/{session_id}/execute",
                json={"command": "wc -c < binary.bin"}
            ) as resp:
                data = await resp.json()
                assert data["status"] == "Success"
                assert data["stdout"].strip() == "256"
        finally:
            await http_client.post(f"{SWEREX_SERVER_URL}/session/{session_id}/release")


class TestFileFetching:
    """Tests for file fetching after command execution."""
    
    @pytest.mark.asyncio
    async def test_fetch_single_file(self, check_server, http_client):
        """Test fetching a single file after command execution."""
        # Acquire session
        async with http_client.post(
            f"{SWEREX_SERVER_URL}/session/acquire",
            json={}
        ) as resp:
            assert resp.status == 200
            data = await resp.json()
            session_id = data["session_id"]
        
        try:
            # Create a file
            async with http_client.post(
                f"{SWEREX_SERVER_URL}/session/{session_id}/execute",
                json={"command": "echo 'Hello from test' > output.txt"}
            ) as resp:
                data = await resp.json()
                assert data["status"] == "Success"
            
            # Fetch the file
            async with http_client.post(
                f"{SWEREX_SERVER_URL}/session/{session_id}/execute",
                json={
                    "command": "echo 'fetch test'",
                    "fetch_files": ["output.txt"]
                }
            ) as resp:
                data = await resp.json()
                assert data["status"] == "Success"
                assert "files" in data
                assert "output.txt" in data["files"]
                
                # Decode and verify content
                content = base64.b64decode(data["files"]["output.txt"]).decode()
                assert "Hello from test" in content
        finally:
            await http_client.post(f"{SWEREX_SERVER_URL}/session/{session_id}/release")
    
    @pytest.mark.asyncio
    async def test_fetch_multiple_files(self, check_server, http_client):
        """Test fetching multiple files."""
        # Acquire session
        async with http_client.post(
            f"{SWEREX_SERVER_URL}/session/acquire",
            json={}
        ) as resp:
            data = await resp.json()
            session_id = data["session_id"]
        
        try:
            # Create multiple files
            async with http_client.post(
                f"{SWEREX_SERVER_URL}/session/{session_id}/execute",
                json={"command": "echo 'File A' > a.txt && echo 'File B' > b.txt && mkdir -p dir && echo 'File C' > dir/c.txt"}
            ) as resp:
                data = await resp.json()
                assert data["status"] == "Success"
            
            # Fetch all files
            async with http_client.post(
                f"{SWEREX_SERVER_URL}/session/{session_id}/execute",
                json={
                    "command": "ls",
                    "fetch_files": ["a.txt", "b.txt", "dir/c.txt"]
                }
            ) as resp:
                data = await resp.json()
                assert data["status"] == "Success"
                assert "files" in data
                assert len(data["files"]) == 3
                
                # Verify each file
                assert "File A" in base64.b64decode(data["files"]["a.txt"]).decode()
                assert "File B" in base64.b64decode(data["files"]["b.txt"]).decode()
                assert "File C" in base64.b64decode(data["files"]["dir/c.txt"]).decode()
        finally:
            await http_client.post(f"{SWEREX_SERVER_URL}/session/{session_id}/release")
    
    @pytest.mark.asyncio
    async def test_fetch_nonexistent_file(self, check_server, http_client):
        """Test that fetching non-existent files doesn't fail."""
        # Acquire session
        async with http_client.post(
            f"{SWEREX_SERVER_URL}/session/acquire",
            json={}
        ) as resp:
            data = await resp.json()
            session_id = data["session_id"]
        
        try:
            # Try to fetch non-existent file
            async with http_client.post(
                f"{SWEREX_SERVER_URL}/session/{session_id}/execute",
                json={
                    "command": "echo 'test'",
                    "fetch_files": ["does_not_exist.txt"]
                }
            ) as resp:
                data = await resp.json()
                # Command should still succeed
                assert data["status"] == "Success"
                # Files dict should be empty or not contain the missing file
                files = data.get("files", {})
                assert "does_not_exist.txt" not in files or files["does_not_exist.txt"] == ""
        finally:
            await http_client.post(f"{SWEREX_SERVER_URL}/session/{session_id}/release")
    
    @pytest.mark.asyncio
    async def test_fetch_partial_files(self, check_server, http_client):
        """Test fetching mix of existing and non-existing files."""
        # Acquire session
        async with http_client.post(
            f"{SWEREX_SERVER_URL}/session/acquire",
            json={}
        ) as resp:
            data = await resp.json()
            session_id = data["session_id"]
        
        try:
            # Create one file
            async with http_client.post(
                f"{SWEREX_SERVER_URL}/session/{session_id}/execute",
                json={"command": "echo 'exists' > exists.txt"}
            ) as resp:
                pass
            
            # Fetch mix of existing and non-existing
            async with http_client.post(
                f"{SWEREX_SERVER_URL}/session/{session_id}/execute",
                json={
                    "command": "echo 'done'",
                    "fetch_files": ["exists.txt", "missing.txt"]
                }
            ) as resp:
                data = await resp.json()
                assert data["status"] == "Success"
                files = data.get("files", {})
                # Should have the existing file
                assert "exists.txt" in files
                assert "exists" in base64.b64decode(files["exists.txt"]).decode()
        finally:
            await http_client.post(f"{SWEREX_SERVER_URL}/session/{session_id}/release")


class TestFileRoundTrip:
    """Tests for full file upload -> modify -> fetch workflow."""
    
    @pytest.mark.asyncio
    async def test_upload_modify_fetch(self, check_server, http_client):
        """Test uploading a file, modifying it, and fetching it back."""
        original_content = "Original content line 1\nOriginal content line 2"
        encoded = base64.b64encode(original_content.encode()).decode()
        
        # Acquire session with file
        async with http_client.post(
            f"{SWEREX_SERVER_URL}/session/acquire",
            json={"files": {"data.txt": encoded}}
        ) as resp:
            data = await resp.json()
            session_id = data["session_id"]
        
        try:
            # Modify the file
            async with http_client.post(
                f"{SWEREX_SERVER_URL}/session/{session_id}/execute",
                json={"command": "echo 'Modified line 3' >> data.txt"}
            ) as resp:
                data = await resp.json()
                assert data["status"] == "Success"
            
            # Fetch the modified file
            async with http_client.post(
                f"{SWEREX_SERVER_URL}/session/{session_id}/execute",
                json={
                    "command": "cat data.txt",
                    "fetch_files": ["data.txt"]
                }
            ) as resp:
                data = await resp.json()
                assert data["status"] == "Success"
                
                # Verify both original and modified content
                fetched = base64.b64decode(data["files"]["data.txt"]).decode()
                assert "Original content line 1" in fetched
                assert "Original content line 2" in fetched
                assert "Modified line 3" in fetched
        finally:
            await http_client.post(f"{SWEREX_SERVER_URL}/session/{session_id}/release")
    
    @pytest.mark.asyncio
    async def test_script_execution_with_files(self, check_server, http_client):
        """Test uploading a script, executing it, and fetching output."""
        script_content = """#!/bin/bash
echo "Script started"
echo "Arguments: $@"
date > output.txt
echo "Result: $(cat output.txt)" >> output.txt
echo "Script finished"
"""
        encoded = base64.b64encode(script_content.encode()).decode()
        
        # Acquire session with script
        async with http_client.post(
            f"{SWEREX_SERVER_URL}/session/acquire",
            json={"files": {"script.sh": encoded}}
        ) as resp:
            data = await resp.json()
            session_id = data["session_id"]
        
        try:
            # Execute the script
            async with http_client.post(
                f"{SWEREX_SERVER_URL}/session/{session_id}/execute",
                json={"command": "chmod +x script.sh && ./script.sh arg1 arg2"}
            ) as resp:
                data = await resp.json()
                assert data["status"] == "Success"
                assert "Script started" in data["stdout"]
                assert "arg1 arg2" in data["stdout"]
                assert "Script finished" in data["stdout"]
            
            # Fetch the output file
            async with http_client.post(
                f"{SWEREX_SERVER_URL}/session/{session_id}/execute",
                json={
                    "command": "echo 'fetching'",
                    "fetch_files": ["output.txt"]
                }
            ) as resp:
                data = await resp.json()
                assert "output.txt" in data["files"]
                output = base64.b64decode(data["files"]["output.txt"]).decode()
                assert "Result:" in output
        finally:
            await http_client.post(f"{SWEREX_SERVER_URL}/session/{session_id}/release")


class TestLargeFiles:
    """Tests for handling larger files."""
    
    @pytest.mark.asyncio
    async def test_large_file_upload(self, check_server, http_client):
        """Test uploading a larger file (1MB)."""
        # Create 1MB of content
        content = "x" * (1024 * 1024)  # 1MB
        encoded = base64.b64encode(content.encode()).decode()
        
        # Acquire session with large file
        async with http_client.post(
            f"{SWEREX_SERVER_URL}/session/acquire",
            json={"files": {"large.txt": encoded}},
            timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            assert resp.status == 200
            data = await resp.json()
            session_id = data["session_id"]
        
        try:
            # Verify file size
            async with http_client.post(
                f"{SWEREX_SERVER_URL}/session/{session_id}/execute",
                json={"command": "wc -c < large.txt"}
            ) as resp:
                data = await resp.json()
                assert data["status"] == "Success"
                size = int(data["stdout"].strip())
                assert size == 1024 * 1024
        finally:
            await http_client.post(f"{SWEREX_SERVER_URL}/session/{session_id}/release")
    
    @pytest.mark.asyncio
    async def test_large_file_fetch(self, check_server, http_client):
        """Test fetching a larger file."""
        # Acquire session
        async with http_client.post(
            f"{SWEREX_SERVER_URL}/session/acquire",
            json={}
        ) as resp:
            data = await resp.json()
            session_id = data["session_id"]
        
        try:
            # Create a 100KB file
            async with http_client.post(
                f"{SWEREX_SERVER_URL}/session/{session_id}/execute",
                json={"command": "dd if=/dev/zero bs=1024 count=100 2>/dev/null | tr '\\0' 'A' > bigfile.txt"}
            ) as resp:
                data = await resp.json()
                assert data["status"] == "Success"
            
            # Fetch the large file
            async with http_client.post(
                f"{SWEREX_SERVER_URL}/session/{session_id}/execute",
                json={
                    "command": "wc -c < bigfile.txt",
                    "fetch_files": ["bigfile.txt"]
                },
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                data = await resp.json()
                assert data["status"] == "Success"
                assert "bigfile.txt" in data["files"]
                
                # Verify content size
                content = base64.b64decode(data["files"]["bigfile.txt"])
                assert len(content) == 100 * 1024
        finally:
            await http_client.post(f"{SWEREX_SERVER_URL}/session/{session_id}/release")


if __name__ == "__main__":
    # Run with pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))
