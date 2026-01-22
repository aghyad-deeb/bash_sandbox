#!/usr/bin/env python3
"""
Simple stress test client that hits an existing SweRex session server.

Usage:
    python stress_test_client.py --server http://localhost:8180 --workers 10000
"""

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp


@dataclass
class WorkerStats:
    worker_id: int
    acquire_time: float = 0.0
    execute_times: list = field(default_factory=list)
    release_time: float = 0.0
    total_time: float = 0.0
    commands_executed: int = 0
    success: bool = True
    error: Optional[str] = None


async def run_worker(
    session: aiohttp.ClientSession,
    server_url: str,
    worker_id: int,
    num_commands: int,
) -> WorkerStats:
    """Run a single simulated worker."""
    stats = WorkerStats(worker_id=worker_id)
    session_id = None
    
    try:
        # Acquire session
        start = time.time()
        async with session.post(
            f"{server_url}/session/acquire",
            json={},
            timeout=aiohttp.ClientTimeout(total=120)
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
            pass
        stats.release_time = time.time() - start
        
        stats.total_time = stats.acquire_time + sum(stats.execute_times) + stats.release_time
        
    except Exception as e:
        stats.success = False
        stats.error = str(e)
        
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


async def run_stress_test(
    server_url: str,
    num_workers: int,
    num_commands: int,
    concurrency_limit: int,
):
    """Run the stress test."""
    
    print(f"\n{'=' * 70}")
    print("STRESS TEST")
    print(f"{'=' * 70}")
    print(f"Server: {server_url}")
    print(f"Workers: {num_workers}")
    print(f"Commands per worker: {num_commands}")
    print(f"Concurrency: {concurrency_limit}")
    
    # Check server health first
    connector = aiohttp.TCPConnector(limit=concurrency_limit + 10)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(f"{server_url}/health") as resp:
            health = await resp.json()
            print(f"\nServer health: {health['total_sessions']} sessions, "
                  f"{health['healthy_containers']} containers")
    
    worker_stats: list[WorkerStats] = []
    
    connector = aiohttp.TCPConnector(limit=concurrency_limit + 10)
    async with aiohttp.ClientSession(connector=connector) as session:
        
        print(f"\nRunning...")
        start_time = time.time()
        
        semaphore = asyncio.Semaphore(concurrency_limit)
        
        async def run_with_semaphore(worker_id):
            async with semaphore:
                return await run_worker(session, server_url, worker_id, num_commands)
        
        tasks = [run_with_semaphore(i) for i in range(num_workers)]
        
        completed = 0
        for coro in asyncio.as_completed(tasks):
            stats = await coro
            worker_stats.append(stats)
            completed += 1
            
            if completed % 500 == 0 or completed == num_workers:
                elapsed = time.time() - start_time
                try:
                    async with session.get(f"{server_url}/health", 
                                          timeout=aiohttp.ClientTimeout(total=2)) as resp:
                        if resp.status == 200:
                            h = await resp.json()
                            print(f"  {completed}/{num_workers} ({elapsed:.1f}s) - "
                                  f"in_use: {h['in_use_sessions']}, cleaning: {h['cleaning_sessions']}")
                except Exception:
                    print(f"  {completed}/{num_workers} ({elapsed:.1f}s)")
        
        end_time = time.time()
    
    # Print results
    duration = end_time - start_time
    successful = sum(1 for s in worker_stats if s.success)
    failed = num_workers - successful
    total_commands = num_workers * num_commands
    successful_commands = sum(s.commands_executed for s in worker_stats)
    
    acquire_times = [s.acquire_time for s in worker_stats if s.acquire_time > 0]
    execute_times = [t for s in worker_stats for t in s.execute_times]
    release_times = [s.release_time for s in worker_stats if s.release_time > 0]
    total_times = [s.total_time for s in worker_stats if s.total_time > 0]
    
    print(f"\n{'=' * 70}")
    print("RESULTS")
    print(f"{'=' * 70}")
    
    print(f"\nDuration: {duration:.2f}s")
    print(f"Throughput: {total_commands / duration:.1f} commands/sec")
    print(f"Workers/sec: {num_workers / duration:.1f}")
    
    print(f"\nWorkers: {successful}/{num_workers} successful ({100*successful/num_workers:.1f}%)")
    print(f"Commands: {successful_commands}/{total_commands} successful")
    
    def print_timing(name, times):
        if not times:
            return
        sorted_times = sorted(times)
        n = len(sorted_times)
        print(f"\n{name}:")
        print(f"  min: {min(times):.4f}s, max: {max(times):.4f}s")
        print(f"  mean: {statistics.mean(times):.4f}s, median: {statistics.median(times):.4f}s")
        print(f"  p90: {sorted_times[int(n * 0.90)]:.4f}s, p99: {sorted_times[min(int(n * 0.99), n-1)]:.4f}s")
    
    print_timing("Acquire", acquire_times)
    print_timing("Execute", execute_times)
    print_timing("Release", release_times)
    print_timing("Total (per worker)", total_times)
    
    if failed > 0:
        errors = [s.error for s in worker_stats if s.error]
        print(f"\nErrors ({len(errors)}):")
        error_counts = {}
        for e in errors:
            key = str(e)[:60]
            error_counts[key] = error_counts.get(key, 0) + 1
        for e, c in sorted(error_counts.items(), key=lambda x: -x[1])[:5]:
            print(f"  {c}x: {e}")
    
    print(f"\n{'=' * 70}")


def main():
    parser = argparse.ArgumentParser(description="Stress test client")
    parser.add_argument("--server", "-s", default="http://localhost:8180", help="Server URL")
    parser.add_argument("--workers", "-w", type=int, default=1000, help="Number of workers")
    parser.add_argument("--commands", "-c", type=int, default=5, help="Commands per worker")
    parser.add_argument("--concurrency", "-n", type=int, default=200, help="Max concurrent workers")
    
    args = parser.parse_args()
    
    asyncio.run(run_stress_test(
        server_url=args.server,
        num_workers=args.workers,
        num_commands=args.commands,
        concurrency_limit=args.concurrency,
    ))


if __name__ == "__main__":
    main()
