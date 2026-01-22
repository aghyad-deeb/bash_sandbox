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
Stress test for SweRex Session Server.

Simulates 1000 workers running multiple commands with detailed timing and health tracking.

Usage:
    # Start containers first (e.g., 8 containers)
    for i in $(seq 0 7); do
        docker run -d --name swerex-stress-$i -p $((18000 + i)):8000 \
            --read-only --tmpfs /tmp:size=500M,mode=1777 --tmpfs /root:size=100M \
            -e AUTH_TOKEN=test-token swerex-sandbox:latest
    done
    
    # Run stress test
    python tests/experimental/agent_loop/test_swerex_stress.py --workers 1000 --commands 5
"""

import argparse
import asyncio
import importlib.util
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

# Configuration
DEFAULT_ENDPOINTS = [f"http://localhost:{18000 + i}" for i in range(8)]
AUTH_TOKEN = os.getenv("SWEREX_AUTH_TOKEN", "test-token")


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
    
    # Timing stats (in seconds)
    acquire_times: list
    execute_times: list
    release_times: list
    total_times: list
    
    # Health snapshots
    health_snapshots: list
    
    # Errors
    errors: list
    
    # Duration
    start_time: float
    end_time: float
    
    def print_summary(self):
        """Print a summary of the test results."""
        duration = self.end_time - self.start_time
        
        print("\n" + "=" * 70)
        print("STRESS TEST RESULTS")
        print("=" * 70)
        
        print(f"\nDuration: {duration:.2f}s")
        print(f"Throughput: {self.total_commands / duration:.1f} commands/sec")
        
        print(f"\nWorkers:")
        print(f"  Total: {self.total_workers}")
        print(f"  Successful: {self.successful_workers} ({100*self.successful_workers/self.total_workers:.1f}%)")
        print(f"  Failed: {self.failed_workers}")
        
        print(f"\nCommands:")
        print(f"  Total: {self.total_commands}")
        print(f"  Successful: {self.successful_commands} ({100*self.successful_commands/self.total_commands:.1f}%)")
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
            # Percentiles
            sorted_times = sorted(times)
            p50 = sorted_times[int(len(sorted_times) * 0.50)]
            p90 = sorted_times[int(len(sorted_times) * 0.90)]
            p95 = sorted_times[int(len(sorted_times) * 0.95)]
            p99 = sorted_times[int(len(sorted_times) * 0.99)]
            print(f"  p50: {p50:.4f}")
            print(f"  p90: {p90:.4f}")
            print(f"  p95: {p95:.4f}")
            print(f"  p99: {p99:.4f}")
        
        print_timing_stats("Acquire", self.acquire_times)
        print_timing_stats("Execute", self.execute_times)
        print_timing_stats("Release", self.release_times)
        print_timing_stats("Total (per worker)", self.total_times)
        
        print(f"\nHealth Snapshots ({len(self.health_snapshots)} samples):")
        if self.health_snapshots:
            first = self.health_snapshots[0]
            last = self.health_snapshots[-1]
            max_in_use = max(h.in_use_sessions for h in self.health_snapshots)
            max_cleaning = max(h.cleaning_sessions for h in self.health_snapshots)
            max_broken = max(h.broken_sessions for h in self.health_snapshots)
            
            print(f"  Initial: {first.total_sessions} total, {first.available_sessions} available")
            print(f"  Final: {last.total_sessions} total, {last.available_sessions} available")
            print(f"  Peak in_use: {max_in_use}")
            print(f"  Peak cleaning: {max_cleaning}")
            print(f"  Peak broken: {max_broken}")
        
        if self.errors:
            print(f"\nErrors ({len(self.errors)} total):")
            # Count error types
            error_counts = {}
            for err in self.errors:
                key = str(err)[:50]
                error_counts[key] = error_counts.get(key, 0) + 1
            for err, count in sorted(error_counts.items(), key=lambda x: -x[1])[:5]:
                print(f"  {count}x: {err}")
        
        print("\n" + "=" * 70)


async def run_worker(
    manager,
    worker_id: int,
    num_commands: int,
    command_delay: float = 0.0,
) -> WorkerStats:
    """Run a single simulated worker."""
    stats = WorkerStats(worker_id=worker_id)
    
    try:
        # Acquire session
        start = time.time()
        session_id = await manager.acquire()
        stats.acquire_time = time.time() - start
        
        try:
            # Execute commands
            for i in range(num_commands):
                start = time.time()
                result = await manager.execute(
                    session_id, 
                    f"echo 'Worker {worker_id} command {i}' && sleep 0.01"
                )
                exec_time = time.time() - start
                stats.execute_times.append(exec_time)
                
                if result.status == "Success":
                    stats.commands_executed += 1
                else:
                    stats.success = False
                    stats.error = f"Command failed: {result.stderr}"
                
                if command_delay > 0:
                    await asyncio.sleep(command_delay)
            
        finally:
            # Release session
            start = time.time()
            await manager.release(session_id)
            stats.release_time = time.time() - start
        
        stats.total_time = stats.acquire_time + sum(stats.execute_times) + stats.release_time
        
    except Exception as e:
        stats.success = False
        stats.error = str(e)
    
    return stats


async def health_monitor(manager, interval: float, results: list, stop_event: asyncio.Event):
    """Monitor health at regular intervals."""
    while not stop_event.is_set():
        try:
            health = manager.get_health()
            snapshot = HealthSnapshot(
                timestamp=time.time(),
                total_sessions=health.total_sessions,
                available_sessions=health.available_sessions,
                in_use_sessions=health.in_use_sessions,
                cleaning_sessions=health.cleaning_sessions,
                broken_sessions=health.broken_sessions,
                healthy_containers=health.healthy_containers,
                unhealthy_containers=health.unhealthy_containers,
            )
            results.append(snapshot)
        except Exception as e:
            print(f"Health check error: {e}")
        
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass


async def run_stress_test(
    endpoints: list[str],
    num_workers: int,
    num_commands: int,
    sessions_per_container: int,
    concurrency_limit: int,
    auth_token: str,
) -> TestResults:
    """Run the full stress test."""
    
    # Import SessionManager
    module_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
        "verl", "experimental", "agent_loop", "swerex_server.py"
    )
    spec = importlib.util.spec_from_file_location("swerex_server", module_path)
    swerex_server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(swerex_server)
    SessionManager = swerex_server.SessionManager
    
    print(f"\nInitializing SessionManager...")
    print(f"  Endpoints: {len(endpoints)}")
    print(f"  Sessions per container: {sessions_per_container}")
    print(f"  Total sessions: {len(endpoints) * sessions_per_container}")
    
    manager = SessionManager(
        endpoints=endpoints,
        sessions_per_container=sessions_per_container,
        auth_token=auth_token,
    )
    
    await manager.initialize()
    
    initial_health = manager.get_health()
    print(f"\nInitial health:")
    print(f"  Total sessions: {initial_health.total_sessions}")
    print(f"  Available: {initial_health.available_sessions}")
    print(f"  Healthy containers: {initial_health.healthy_containers}")
    
    # Results
    worker_stats: list[WorkerStats] = []
    health_snapshots: list[HealthSnapshot] = []
    errors: list[str] = []
    
    # Start health monitor
    stop_event = asyncio.Event()
    health_task = asyncio.create_task(
        health_monitor(manager, 0.5, health_snapshots, stop_event)
    )
    
    print(f"\nStarting stress test...")
    print(f"  Workers: {num_workers}")
    print(f"  Commands per worker: {num_commands}")
    print(f"  Concurrency limit: {concurrency_limit}")
    
    start_time = time.time()
    
    # Semaphore to limit concurrency
    semaphore = asyncio.Semaphore(concurrency_limit)
    
    async def run_with_semaphore(worker_id):
        async with semaphore:
            return await run_worker(manager, worker_id, num_commands)
    
    # Run all workers with progress
    tasks = [run_with_semaphore(i) for i in range(num_workers)]
    
    completed = 0
    for coro in asyncio.as_completed(tasks):
        stats = await coro
        worker_stats.append(stats)
        completed += 1
        
        if completed % 100 == 0 or completed == num_workers:
            elapsed = time.time() - start_time
            health = manager.get_health()
            print(f"  Progress: {completed}/{num_workers} workers "
                  f"({elapsed:.1f}s, {health.in_use_sessions} in_use, "
                  f"{health.cleaning_sessions} cleaning)")
    
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
    
    # Shutdown
    print(f"\nShutting down...")
    await manager.shutdown()
    
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
    )


def check_containers(endpoints: list[str]) -> list[str]:
    """Check which containers are available."""
    import socket
    
    available = []
    for endpoint in endpoints:
        try:
            host, port = endpoint.replace("http://", "").split(":")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex((host, int(port)))
            sock.close()
            if result == 0:
                available.append(endpoint)
        except Exception:
            pass
    
    return available


def main():
    parser = argparse.ArgumentParser(
        description="Stress test for SweRex Session Server"
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=1000,
        help="Number of simulated workers (default: 1000)"
    )
    parser.add_argument(
        "--commands", "-c",
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
        "--endpoints", "-e",
        type=str,
        default="",
        help="Comma-separated endpoints (default: auto-detect localhost:18000-18007)"
    )
    parser.add_argument(
        "--base-port",
        type=int,
        default=18000,
        help="Base port for auto-detected endpoints (default: 18000)"
    )
    parser.add_argument(
        "--max-containers",
        type=int,
        default=16,
        help="Max containers to auto-detect (default: 16)"
    )
    
    args = parser.parse_args()
    
    # Determine endpoints
    if args.endpoints:
        endpoints = [e.strip() for e in args.endpoints.split(",") if e.strip()]
    else:
        # Auto-detect
        potential = [f"http://localhost:{args.base_port + i}" for i in range(args.max_containers)]
        endpoints = check_containers(potential)
    
    if not endpoints:
        print("ERROR: No containers available!")
        print("Start containers first:")
        print("  for i in $(seq 0 7); do")
        print("    docker run -d --name swerex-stress-$i -p $((18000 + i)):8000 \\")
        print("      --read-only --tmpfs /tmp:size=500M,mode=1777 --tmpfs /root:size=100M \\")
        print("      -e AUTH_TOKEN=test-token swerex-sandbox:latest")
        print("  done")
        sys.exit(1)
    
    print(f"Found {len(endpoints)} available containers")
    
    # Calculate total sessions
    total_sessions = len(endpoints) * args.sessions_per_container
    print(f"Total sessions: {total_sessions}")
    
    if args.concurrency > total_sessions:
        print(f"Warning: concurrency ({args.concurrency}) > total sessions ({total_sessions})")
        print(f"  Some workers may wait for sessions")
    
    # Run test
    results = asyncio.run(run_stress_test(
        endpoints=endpoints,
        num_workers=args.workers,
        num_commands=args.commands,
        sessions_per_container=args.sessions_per_container,
        concurrency_limit=args.concurrency,
        auth_token=AUTH_TOKEN,
    ))
    
    results.print_summary()


if __name__ == "__main__":
    main()
