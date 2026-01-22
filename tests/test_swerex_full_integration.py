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
Full HTTP Integration Test for SweRex Session Server.

This test:
1. Starts N containers (default: 128)
2. Starts the HTTP server
3. Runs 1000 simulated workers via HTTP
4. Tracks timing and health data
5. Cleans up everything

Usage:
    python tests/experimental/agent_loop/test_swerex_full_integration.py
    
    # With options
    python tests/experimental/agent_loop/test_swerex_full_integration.py \
        --containers 128 --workers 1000 --commands 5
"""

import argparse
import asyncio
import json
import os
import signal
import socket
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

# Configuration
BASE_PORT = 18000
SERVER_PORT = 8080
AUTH_TOKEN = "test-token"
CONTAINER_IMAGE = "swerex-sandbox:latest"


@dataclass
class WorkerStats:
    """Statistics for a single worker."""
    worker_id: int
    acquire_time: float = 0.0
    execute_times: list = field(default_factory=list)
    release_time: float = 0.0
    total_time: float = 0.0
    commands_executed: int = 0
    success: bool = True
    error: Optional[str] = None


@dataclass
class HealthSnapshot:
    """Health snapshot at a point in time."""
    timestamp: float
    status: str
    total_sessions: int
    available_sessions: int
    in_use_sessions: int
    cleaning_sessions: int
    broken_sessions: int
    healthy_containers: int
    unhealthy_containers: int


@dataclass 
class TestResults:
    """Aggregated test results."""
    total_workers: int
    successful_workers: int
    failed_workers: int
    total_commands: int
    successful_commands: int
    failed_commands: int
    
    acquire_times: list
    execute_times: list
    release_times: list
    total_times: list
    
    health_snapshots: list
    errors: list
    
    start_time: float
    end_time: float
    
    num_containers: int
    sessions_per_container: int
    
    def print_summary(self):
        """Print a summary of the test results."""
        duration = self.end_time - self.start_time
        
        print("\n" + "=" * 80)
        print("FULL HTTP INTEGRATION TEST RESULTS")
        print("=" * 80)
        
        print(f"\nConfiguration:")
        print(f"  Containers: {self.num_containers}")
        print(f"  Sessions per container: {self.sessions_per_container}")
        print(f"  Total sessions: {self.num_containers * self.sessions_per_container}")
        
        print(f"\nDuration: {duration:.2f}s")
        print(f"Throughput: {self.total_commands / duration:.1f} commands/sec")
        print(f"Workers/sec: {self.total_workers / duration:.1f}")
        
        print(f"\nWorkers:")
        print(f"  Total: {self.total_workers}")
        print(f"  Successful: {self.successful_workers} ({100*self.successful_workers/self.total_workers:.1f}%)")
        print(f"  Failed: {self.failed_workers}")
        
        print(f"\nCommands:")
        print(f"  Total: {self.total_commands}")
        print(f"  Successful: {self.successful_commands} ({100*self.successful_commands/max(1,self.total_commands):.1f}%)")
        print(f"  Failed: {self.failed_commands}")
        
        def print_timing_stats(name, times):
            if not times:
                print(f"\n{name} Timing: No data")
                return
            print(f"\n{name} Timing (seconds):")
            print(f"  Min: {min(times):.4f}")
            print(f"  Max: {max(times):.4f}")
            print(f"  Mean: {statistics.mean(times):.4f}")
            print(f"  Median: {statistics.median(times):.4f}")
            if len(times) >= 2:
                print(f"  Stdev: {statistics.stdev(times):.4f}")
            sorted_times = sorted(times)
            n = len(sorted_times)
            print(f"  p50: {sorted_times[int(n * 0.50)]:.4f}")
            print(f"  p90: {sorted_times[int(n * 0.90)]:.4f}")
            print(f"  p95: {sorted_times[int(n * 0.95)]:.4f}")
            print(f"  p99: {sorted_times[min(int(n * 0.99), n-1)]:.4f}")
        
        print_timing_stats("Acquire (HTTP)", self.acquire_times)
        print_timing_stats("Execute (HTTP)", self.execute_times)
        print_timing_stats("Release (HTTP)", self.release_times)
        print_timing_stats("Total (per worker)", self.total_times)
        
        print(f"\nHealth Snapshots ({len(self.health_snapshots)} samples):")
        if self.health_snapshots:
            first = self.health_snapshots[0]
            last = self.health_snapshots[-1]
            max_in_use = max(h.in_use_sessions for h in self.health_snapshots)
            max_cleaning = max(h.cleaning_sessions for h in self.health_snapshots)
            max_broken = max(h.broken_sessions for h in self.health_snapshots)
            
            print(f"  Initial: {first.total_sessions} total, {first.available_sessions} available, status={first.status}")
            print(f"  Final: {last.total_sessions} total, {last.available_sessions} available, status={last.status}")
            print(f"  Peak in_use: {max_in_use}")
            print(f"  Peak cleaning: {max_cleaning}")
            print(f"  Peak broken: {max_broken}")
        
        if self.errors:
            print(f"\nErrors ({len(self.errors)} total):")
            error_counts = {}
            for err in self.errors:
                key = str(err)[:60]
                error_counts[key] = error_counts.get(key, 0) + 1
            for err, count in sorted(error_counts.items(), key=lambda x: -x[1])[:10]:
                print(f"  {count}x: {err}")
        
        print("\n" + "=" * 80)


class ContainerManager:
    """Manages Docker containers for the test."""
    
    def __init__(self, num_containers: int, base_port: int = BASE_PORT):
        self.num_containers = num_containers
        self.base_port = base_port
        self.container_ids = []
    
    def start_containers(self) -> list[str]:
        """Start all containers and return endpoints."""
        print(f"\nStarting {self.num_containers} containers...")
        
        endpoints = []
        
        for i in range(self.num_containers):
            port = self.base_port + i
            name = f"swerex-integration-{i}"
            
            # Remove existing container if any
            subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True
            )
            
            # Start container
            result = subprocess.run(
                [
                    "docker", "run", "-d",
                    "--name", name,
                    "-p", f"{port}:8000",
                    "--read-only",
                    "--tmpfs", "/tmp:size=500M,mode=1777",
                    "--tmpfs", "/root:size=100M",
                    "-e", f"AUTH_TOKEN={AUTH_TOKEN}",
                    CONTAINER_IMAGE
                ],
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                print(f"  ERROR starting container {i}: {result.stderr}")
                continue
            
            container_id = result.stdout.strip()
            self.container_ids.append(container_id)
            endpoints.append(f"http://localhost:{port}")
            
            if (i + 1) % 20 == 0 or i == self.num_containers - 1:
                print(f"  Started {i + 1}/{self.num_containers} containers")
        
        print(f"  Waiting for containers to be healthy...")
        time.sleep(10)  # Give containers time to start
        
        # Check health
        healthy = 0
        for i, endpoint in enumerate(endpoints):
            for attempt in range(10):
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(1)
                    port = self.base_port + i
                    result = sock.connect_ex(("localhost", port))
                    sock.close()
                    if result == 0:
                        healthy += 1
                        break
                except Exception:
                    pass
                time.sleep(0.5)
        
        print(f"  {healthy}/{len(endpoints)} containers healthy")
        
        return endpoints
    
    def stop_containers(self):
        """Stop and remove all containers."""
        print(f"\nStopping {len(self.container_ids)} containers...")
        
        for i in range(self.num_containers):
            name = f"swerex-integration-{i}"
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        
        self.container_ids.clear()
        print("  Done")


class ServerManager:
    """Manages the session server process."""
    
    def __init__(self, endpoints: list[str], sessions_per_container: int, port: int = SERVER_PORT):
        self.endpoints = endpoints
        self.sessions_per_container = sessions_per_container
        self.port = port
        self.process = None
        self.url = f"http://localhost:{port}"
    
    def start(self):
        """Start the server process."""
        print(f"\nStarting session server on port {self.port}...")
        
        env = os.environ.copy()
        env["SWEREX_ENDPOINTS"] = ",".join(self.endpoints)
        env["SWEREX_SESSIONS_PER_CONTAINER"] = str(self.sessions_per_container)
        env["SWEREX_AUTH_TOKEN"] = AUTH_TOKEN
        env["SWEREX_SERVER_PORT"] = str(self.port)
        
        # Find the server script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(script_dir)))
        server_script = os.path.join(
            project_root, "verl", "experimental", "agent_loop", "swerex_server.py"
        )
        
        # Run server with uvicorn directly to avoid verl package imports
        # Set env vars via os.environ in the script so they're available at module load time
        endpoints_str = ",".join(self.endpoints)
        self.process = subprocess.Popen(
            [
                sys.executable, "-c",
                f"""
import os
import sys

# Set environment variables BEFORE importing the server module
os.environ['SWEREX_ENDPOINTS'] = '{endpoints_str}'
os.environ['SWEREX_SESSIONS_PER_CONTAINER'] = '{self.sessions_per_container}'
os.environ['SWEREX_AUTH_TOKEN'] = '{AUTH_TOKEN}'
os.environ['SWEREX_SERVER_PORT'] = '{self.port}'

sys.path.insert(0, '{project_root}')

# Import server module directly without going through verl package
import importlib.util
spec = importlib.util.spec_from_file_location("swerex_server", "{server_script}")
swerex_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(swerex_server)

import uvicorn
uvicorn.run(swerex_server.app, host="127.0.0.1", port={self.port}, log_level="warning")
"""
            ],
            cwd=project_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        
        # Wait for server to be ready
        print("  Waiting for server to initialize...")
        for attempt in range(120):  # Up to 2 minutes
            # Check if process died
            if self.process.poll() is not None:
                # Process exited - read output
                output = self.process.stdout.read().decode() if self.process.stdout else ""
                print(f"  Server process exited with code {self.process.returncode}")
                print(f"  Output: {output[:2000]}")
                raise RuntimeError(f"Server process exited: {output[:500]}")
            
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                result = sock.connect_ex(("localhost", self.port))
                sock.close()
                if result == 0:
                    # Try health endpoint
                    import urllib.request
                    try:
                        resp = urllib.request.urlopen(f"{self.url}/health", timeout=5)
                        if resp.status == 200:
                            data = json.loads(resp.read())
                            print(f"  Server ready: {data['total_sessions']} sessions, "
                                  f"{data['healthy_containers']} healthy containers")
                            return
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(1)
            if (attempt + 1) % 10 == 0:
                print(f"  Still waiting... ({attempt + 1}s)")
        
        # Timeout - get any output
        if self.process.stdout:
            try:
                output = self.process.stdout.read(10000).decode()
                print(f"  Server output: {output[:2000]}")
            except Exception:
                pass
        
        raise RuntimeError("Server failed to start within timeout")
    
    def stop(self):
        """Stop the server process."""
        if self.process:
            print("\nStopping server...")
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
            print("  Done")


async def run_worker_http(
    session: aiohttp.ClientSession,
    server_url: str,
    worker_id: int,
    num_commands: int,
) -> WorkerStats:
    """Run a single simulated worker via HTTP."""
    stats = WorkerStats(worker_id=worker_id)
    session_id = None
    
    try:
        # Acquire session
        start = time.time()
        async with session.post(
            f"{server_url}/session/acquire",
            json={},
            timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            if resp.status != 200:
                error = await resp.text()
                raise RuntimeError(f"Acquire failed: {resp.status} - {error}")
            data = await resp.json()
            session_id = data["session_id"]
        stats.acquire_time = time.time() - start
        
        # Execute commands
        for i in range(num_commands):
            start = time.time()
            async with session.post(
                f"{server_url}/session/{session_id}/execute",
                json={"command": f"echo 'w{worker_id}c{i}' && sleep 0.01", "timeout": 30},
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    stats.success = False
                    stats.error = f"Execute failed: {resp.status} - {error}"
                    break
                data = await resp.json()
                if data["status"] == "Success":
                    stats.commands_executed += 1
                else:
                    stats.success = False
                    stats.error = f"Command failed: {data.get('stderr', 'unknown')}"
            stats.execute_times.append(time.time() - start)
        
        # Release session
        start = time.time()
        async with session.post(
            f"{server_url}/session/{session_id}/release",
            timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            pass  # Ignore response
        stats.release_time = time.time() - start
        
        stats.total_time = stats.acquire_time + sum(stats.execute_times) + stats.release_time
        
    except Exception as e:
        stats.success = False
        stats.error = str(e)
        
        # Try to release if we got a session
        if session_id:
            try:
                async with session.post(
                    f"{server_url}/session/{session_id}/release",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    pass
            except Exception:
                pass
    
    return stats


async def health_monitor_http(
    session: aiohttp.ClientSession,
    server_url: str,
    interval: float,
    results: list,
    stop_event: asyncio.Event
):
    """Monitor server health via HTTP."""
    while not stop_event.is_set():
        try:
            async with session.get(
                f"{server_url}/health",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    snapshot = HealthSnapshot(
                        timestamp=time.time(),
                        status=data["status"],
                        total_sessions=data["total_sessions"],
                        available_sessions=data["available_sessions"],
                        in_use_sessions=data["in_use_sessions"],
                        cleaning_sessions=data["cleaning_sessions"],
                        broken_sessions=data["broken_sessions"],
                        healthy_containers=data["healthy_containers"],
                        unhealthy_containers=data["unhealthy_containers"],
                    )
                    results.append(snapshot)
        except Exception as e:
            print(f"  Health check error: {e}")
        
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass


async def run_stress_test_http(
    server_url: str,
    num_workers: int,
    num_commands: int,
    concurrency_limit: int,
    num_containers: int,
    sessions_per_container: int,
) -> TestResults:
    """Run the stress test via HTTP."""
    
    worker_stats: list[WorkerStats] = []
    health_snapshots: list[HealthSnapshot] = []
    
    # Create shared HTTP session
    connector = aiohttp.TCPConnector(limit=concurrency_limit + 10)
    async with aiohttp.ClientSession(connector=connector) as session:
        
        # Start health monitor
        stop_event = asyncio.Event()
        health_task = asyncio.create_task(
            health_monitor_http(session, server_url, 0.5, health_snapshots, stop_event)
        )
        
        print(f"\nRunning stress test...")
        print(f"  Workers: {num_workers}")
        print(f"  Commands per worker: {num_commands}")
        print(f"  Concurrency limit: {concurrency_limit}")
        
        start_time = time.time()
        
        # Semaphore to limit concurrency
        semaphore = asyncio.Semaphore(concurrency_limit)
        
        async def run_with_semaphore(worker_id):
            async with semaphore:
                return await run_worker_http(session, server_url, worker_id, num_commands)
        
        # Run all workers
        tasks = [run_with_semaphore(i) for i in range(num_workers)]
        
        completed = 0
        for coro in asyncio.as_completed(tasks):
            stats = await coro
            worker_stats.append(stats)
            completed += 1
            
            if completed % 100 == 0 or completed == num_workers:
                elapsed = time.time() - start_time
                # Get current health
                try:
                    async with session.get(f"{server_url}/health", timeout=aiohttp.ClientTimeout(total=2)) as resp:
                        if resp.status == 200:
                            health = await resp.json()
                            print(f"  Progress: {completed}/{num_workers} workers "
                                  f"({elapsed:.1f}s, {health['in_use_sessions']} in_use, "
                                  f"{health['cleaning_sessions']} cleaning)")
                except Exception:
                    print(f"  Progress: {completed}/{num_workers} workers ({elapsed:.1f}s)")
        
        end_time = time.time()
        
        # Stop health monitor
        stop_event.set()
        await health_task
    
    # Collect results
    successful_workers = sum(1 for s in worker_stats if s.success)
    failed_workers = num_workers - successful_workers
    
    total_commands = num_workers * num_commands
    successful_commands = sum(s.commands_executed for s in worker_stats)
    failed_commands = total_commands - successful_commands
    
    acquire_times = [s.acquire_time for s in worker_stats if s.acquire_time > 0]
    execute_times = [t for s in worker_stats for t in s.execute_times]
    release_times = [s.release_time for s in worker_stats if s.release_time > 0]
    total_times = [s.total_time for s in worker_stats if s.total_time > 0]
    
    errors = [s.error for s in worker_stats if s.error]
    
    return TestResults(
        total_workers=num_workers,
        successful_workers=successful_workers,
        failed_workers=failed_workers,
        total_commands=total_commands,
        successful_commands=successful_commands,
        failed_commands=failed_commands,
        acquire_times=acquire_times,
        execute_times=execute_times,
        release_times=release_times,
        total_times=total_times,
        health_snapshots=health_snapshots,
        errors=errors,
        start_time=start_time,
        end_time=end_time,
        num_containers=num_containers,
        sessions_per_container=sessions_per_container,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Full HTTP Integration Test for SweRex Session Server"
    )
    parser.add_argument(
        "--containers", "-c",
        type=int,
        default=128,
        help="Number of containers (default: 128)"
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=1000,
        help="Number of simulated workers (default: 1000)"
    )
    parser.add_argument(
        "--commands",
        type=int,
        default=5,
        help="Number of commands per worker (default: 5)"
    )
    parser.add_argument(
        "--sessions-per-container", "-s",
        type=int,
        default=8,
        help="Sessions per container (default: 8)"
    )
    parser.add_argument(
        "--concurrency", "-n",
        type=int,
        default=200,
        help="Max concurrent workers (default: 200)"
    )
    parser.add_argument(
        "--skip-cleanup",
        action="store_true",
        help="Don't stop containers after test"
    )
    parser.add_argument(
        "--use-existing-containers",
        action="store_true",
        help="Use existing containers instead of starting new ones"
    )
    parser.add_argument(
        "--server-port",
        type=int,
        default=8090,
        help="Port for the session server (default: 8090)"
    )
    
    args = parser.parse_args()
    
    total_sessions = args.containers * args.sessions_per_container
    print(f"\n{'=' * 80}")
    print("FULL HTTP INTEGRATION TEST")
    print(f"{'=' * 80}")
    print(f"\nConfiguration:")
    print(f"  Containers: {args.containers}")
    print(f"  Sessions per container: {args.sessions_per_container}")
    print(f"  Total sessions: {total_sessions}")
    print(f"  Workers: {args.workers}")
    print(f"  Commands per worker: {args.commands}")
    print(f"  Concurrency limit: {args.concurrency}")
    
    if args.concurrency > total_sessions:
        print(f"\n  WARNING: Concurrency ({args.concurrency}) > total sessions ({total_sessions})")
        print(f"  Some workers will wait for sessions to become available")
    
    # Initialize managers
    container_manager = None if args.use_existing_containers else ContainerManager(args.containers)
    server_manager = None
    
    try:
        if args.use_existing_containers:
            # Use existing containers on localhost
            print(f"\nUsing existing containers on ports 18000-{18000 + args.containers - 1}")
            endpoints = [f"http://localhost:{18000 + i}" for i in range(args.containers)]
            
            # Check which are available
            import socket
            available = []
            for ep in endpoints:
                try:
                    port = int(ep.split(":")[-1])
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(1)
                    if sock.connect_ex(("localhost", port)) == 0:
                        available.append(ep)
                    sock.close()
                except Exception:
                    pass
            
            print(f"  {len(available)}/{len(endpoints)} containers available")
            endpoints = available
        else:
            # Start containers
            endpoints = container_manager.start_containers()
        
        if len(endpoints) < args.containers:
            print(f"\nWARNING: Only {len(endpoints)}/{args.containers} containers available")
        
        # Start server
        server_manager = ServerManager(
            endpoints=endpoints,
            sessions_per_container=args.sessions_per_container,
            port=args.server_port
        )
        server_manager.start()
        
        # Run stress test
        results = asyncio.run(run_stress_test_http(
            server_url=server_manager.url,
            num_workers=args.workers,
            num_commands=args.commands,
            concurrency_limit=args.concurrency,
            num_containers=len(endpoints),
            sessions_per_container=args.sessions_per_container,
        ))
        
        # Print results
        results.print_summary()
        
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    except Exception as e:
        print(f"\n\nERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Cleanup
        if server_manager:
            server_manager.stop()
        
        if container_manager and not args.skip_cleanup:
            container_manager.stop_containers()
        elif args.use_existing_containers:
            print("\nContainers left running (--use-existing-containers)")
        elif args.skip_cleanup:
            print("\nSkipping container cleanup (--skip-cleanup)")


if __name__ == "__main__":
    main()
