#!/usr/bin/env python3
"""
Health check script for SweRex Session Server and Docker containers.

Usage:
    ./check_health.py                    # Check with defaults (128 containers, port 8180)
    ./check_health.py --containers 64    # Check 64 containers
    ./check_health.py --port 8200        # Check server on different port
    ./check_health.py --verbose          # Show detailed container status
    ./check_health.py --json             # Output as JSON

Exit codes:
    0 - All healthy
    1 - Server unhealthy or unreachable
    2 - Some containers unhealthy
    3 - Both server and some containers unhealthy
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from typing import Optional

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx")
    sys.exit(1)


# =============================================================================
# Configuration
# =============================================================================

DEFAULT_SERVER_PORT = 8180
DEFAULT_NUM_CONTAINERS = 128
DEFAULT_CONTAINER_BASE_PORT = 18000
DEFAULT_AUTH_TOKEN = os.getenv("SWEREX_AUTH_TOKEN", "default-token")
REQUEST_TIMEOUT = 5.0


# =============================================================================
# Colors for terminal output
# =============================================================================

class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    @classmethod
    def disable(cls):
        cls.GREEN = ""
        cls.RED = ""
        cls.YELLOW = ""
        cls.BLUE = ""
        cls.BOLD = ""
        cls.RESET = ""


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class ServerHealth:
    """Health status of the session server."""
    reachable: bool
    status: Optional[str] = None  # "healthy", "degraded", "unhealthy"
    total_sessions: int = 0
    available_sessions: int = 0
    in_use_sessions: int = 0
    cleaning_sessions: int = 0
    broken_sessions: int = 0
    healthy_containers: int = 0
    unhealthy_containers: int = 0
    error: Optional[str] = None


@dataclass
class ContainerHealth:
    """Health status of a single container."""
    index: int
    port: int
    reachable: bool
    error: Optional[str] = None


@dataclass
class OverallHealth:
    """Combined health status."""
    server: ServerHealth
    containers: list[ContainerHealth]
    
    @property
    def healthy_containers_count(self) -> int:
        return sum(1 for c in self.containers if c.reachable)
    
    @property
    def unhealthy_containers_count(self) -> int:
        return sum(1 for c in self.containers if not c.reachable)
    
    @property
    def is_healthy(self) -> bool:
        return (
            self.server.reachable and 
            self.server.status == "healthy" and
            self.unhealthy_containers_count == 0
        )
    
    @property
    def exit_code(self) -> int:
        server_bad = not self.server.reachable or self.server.status == "unhealthy"
        containers_bad = self.unhealthy_containers_count > 0
        
        if server_bad and containers_bad:
            return 3
        elif server_bad:
            return 1
        elif containers_bad:
            return 2
        return 0


# =============================================================================
# Health Check Functions
# =============================================================================

async def check_server_health(port: int) -> ServerHealth:
    """Check the session server's health endpoint."""
    url = f"http://localhost:{port}/health"
    
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(url)
            
            if response.status_code != 200:
                return ServerHealth(
                    reachable=False,
                    error=f"HTTP {response.status_code}"
                )
            
            data = response.json()
            return ServerHealth(
                reachable=True,
                status=data.get("status"),
                total_sessions=data.get("total_sessions", 0),
                available_sessions=data.get("available_sessions", 0),
                in_use_sessions=data.get("in_use_sessions", 0),
                cleaning_sessions=data.get("cleaning_sessions", 0),
                broken_sessions=data.get("broken_sessions", 0),
                healthy_containers=data.get("healthy_containers", 0),
                unhealthy_containers=data.get("unhealthy_containers", 0),
            )
            
    except httpx.ConnectError:
        return ServerHealth(reachable=False, error="Connection refused")
    except httpx.TimeoutException:
        return ServerHealth(reachable=False, error="Timeout")
    except Exception as e:
        return ServerHealth(reachable=False, error=str(e))


async def check_container_health(
    index: int,
    base_port: int,
    auth_token: str
) -> ContainerHealth:
    """Check a single container's health."""
    port = base_port + index
    url = f"http://localhost:{port}/is_alive"
    
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            headers = {"X-API-Key": auth_token}
            response = await client.get(url, headers=headers)
            
            if response.status_code != 200:
                return ContainerHealth(
                    index=index,
                    port=port,
                    reachable=False,
                    error=f"HTTP {response.status_code}"
                )
            
            # Check if response indicates alive
            text = response.text.lower()
            if "true" in text or response.status_code == 200:
                return ContainerHealth(index=index, port=port, reachable=True)
            else:
                return ContainerHealth(
                    index=index,
                    port=port,
                    reachable=False,
                    error="Not alive"
                )
                
    except httpx.ConnectError:
        return ContainerHealth(
            index=index,
            port=port,
            reachable=False,
            error="Connection refused"
        )
    except httpx.TimeoutException:
        return ContainerHealth(
            index=index,
            port=port,
            reachable=False,
            error="Timeout"
        )
    except Exception as e:
        return ContainerHealth(
            index=index,
            port=port,
            reachable=False,
            error=str(e)
        )


async def check_all_containers(
    num_containers: int,
    base_port: int,
    auth_token: str
) -> list[ContainerHealth]:
    """Check all containers in parallel."""
    tasks = [
        check_container_health(i, base_port, auth_token)
        for i in range(num_containers)
    ]
    return await asyncio.gather(*tasks)


async def check_all(
    server_port: int,
    num_containers: int,
    container_base_port: int,
    auth_token: str
) -> OverallHealth:
    """Check both server and all containers."""
    # Run server and container checks in parallel
    server_task = check_server_health(server_port)
    containers_task = check_all_containers(
        num_containers, container_base_port, auth_token
    )
    
    server_health, containers_health = await asyncio.gather(
        server_task, containers_task
    )
    
    return OverallHealth(server=server_health, containers=containers_health)


# =============================================================================
# Output Functions
# =============================================================================

def print_status_icon(healthy: bool) -> str:
    if healthy:
        return f"{Colors.GREEN}[OK]{Colors.RESET}"
    else:
        return f"{Colors.RED}[FAIL]{Colors.RESET}"


def print_server_health(health: ServerHealth) -> None:
    """Print server health status."""
    print(f"\n{Colors.BOLD}=== Session Server ==={Colors.RESET}")
    print(f"  Status: {print_status_icon(health.reachable)} ", end="")
    
    if not health.reachable:
        print(f"{Colors.RED}UNREACHABLE{Colors.RESET} ({health.error})")
        return
    
    # Color status based on value
    if health.status == "healthy":
        status_str = f"{Colors.GREEN}{health.status}{Colors.RESET}"
    elif health.status == "degraded":
        status_str = f"{Colors.YELLOW}{health.status}{Colors.RESET}"
    else:
        status_str = f"{Colors.RED}{health.status}{Colors.RESET}"
    
    print(status_str)
    print(f"  Sessions:")
    print(f"    Total:     {health.total_sessions}")
    print(f"    Available: {Colors.GREEN}{health.available_sessions}{Colors.RESET}")
    print(f"    In Use:    {Colors.BLUE}{health.in_use_sessions}{Colors.RESET}")
    print(f"    Cleaning:  {Colors.YELLOW}{health.cleaning_sessions}{Colors.RESET}")
    print(f"    Broken:    {Colors.RED}{health.broken_sessions}{Colors.RESET}")
    print(f"  Containers (server view):")
    print(f"    Healthy:   {Colors.GREEN}{health.healthy_containers}{Colors.RESET}")
    print(f"    Unhealthy: {Colors.RED}{health.unhealthy_containers}{Colors.RESET}")


def print_containers_summary(containers: list[ContainerHealth]) -> None:
    """Print container health summary."""
    healthy = sum(1 for c in containers if c.reachable)
    unhealthy = len(containers) - healthy
    
    print(f"\n{Colors.BOLD}=== Docker Containers ==={Colors.RESET}")
    print(f"  Total:     {len(containers)}")
    print(f"  Healthy:   {Colors.GREEN}{healthy}{Colors.RESET}")
    print(f"  Unhealthy: {Colors.RED}{unhealthy}{Colors.RESET}")
    
    if unhealthy > 0:
        print(f"\n  {Colors.RED}Unhealthy containers:{Colors.RESET}")
        for c in containers:
            if not c.reachable:
                print(f"    - swerex-{c.index} (port {c.port}): {c.error}")


def print_containers_verbose(containers: list[ContainerHealth]) -> None:
    """Print detailed container status."""
    print(f"\n{Colors.BOLD}=== Container Details ==={Colors.RESET}")
    
    # Group by health status
    healthy = [c for c in containers if c.reachable]
    unhealthy = [c for c in containers if not c.reachable]
    
    if healthy:
        print(f"\n  {Colors.GREEN}Healthy ({len(healthy)}):{Colors.RESET}")
        # Print in columns for compactness
        cols = 8
        for i in range(0, len(healthy), cols):
            chunk = healthy[i:i+cols]
            names = [f"swerex-{c.index}" for c in chunk]
            print(f"    {', '.join(names)}")
    
    if unhealthy:
        print(f"\n  {Colors.RED}Unhealthy ({len(unhealthy)}):{Colors.RESET}")
        for c in unhealthy:
            print(f"    - swerex-{c.index} (port {c.port}): {c.error}")


def print_overall_status(health: OverallHealth) -> None:
    """Print overall health summary."""
    print(f"\n{Colors.BOLD}=== Overall Status ==={Colors.RESET}")
    
    if health.is_healthy:
        print(f"  {Colors.GREEN}ALL SYSTEMS HEALTHY{Colors.RESET}")
    else:
        issues = []
        if not health.server.reachable:
            issues.append("Server unreachable")
        elif health.server.status != "healthy":
            issues.append(f"Server {health.server.status}")
        if health.unhealthy_containers_count > 0:
            issues.append(f"{health.unhealthy_containers_count} containers unhealthy")
        
        print(f"  {Colors.RED}ISSUES DETECTED:{Colors.RESET} {', '.join(issues)}")


def output_json(health: OverallHealth) -> None:
    """Output health as JSON."""
    data = {
        "healthy": health.is_healthy,
        "exit_code": health.exit_code,
        "server": {
            "reachable": health.server.reachable,
            "status": health.server.status,
            "error": health.server.error,
            "total_sessions": health.server.total_sessions,
            "available_sessions": health.server.available_sessions,
            "in_use_sessions": health.server.in_use_sessions,
            "cleaning_sessions": health.server.cleaning_sessions,
            "broken_sessions": health.server.broken_sessions,
            "healthy_containers": health.server.healthy_containers,
            "unhealthy_containers": health.server.unhealthy_containers,
        },
        "containers": {
            "total": len(health.containers),
            "healthy": health.healthy_containers_count,
            "unhealthy": health.unhealthy_containers_count,
            "unhealthy_list": [
                {"index": c.index, "port": c.port, "error": c.error}
                for c in health.containers if not c.reachable
            ]
        }
    }
    print(json.dumps(data, indent=2))


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Check health of SweRex session server and containers"
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=DEFAULT_SERVER_PORT,
        help=f"Session server port (default: {DEFAULT_SERVER_PORT})"
    )
    parser.add_argument(
        "--containers", "-c",
        type=int,
        default=DEFAULT_NUM_CONTAINERS,
        help=f"Number of containers to check (default: {DEFAULT_NUM_CONTAINERS})"
    )
    parser.add_argument(
        "--base-port", "-b",
        type=int,
        default=DEFAULT_CONTAINER_BASE_PORT,
        help=f"Base port for containers (default: {DEFAULT_CONTAINER_BASE_PORT})"
    )
    parser.add_argument(
        "--token", "-t",
        type=str,
        default=DEFAULT_AUTH_TOKEN,
        help="Auth token for containers (default: from SWEREX_AUTH_TOKEN env or 'default-token')"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed container status"
    )
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        help="Output as JSON"
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output"
    )
    parser.add_argument(
        "--server-only",
        action="store_true",
        help="Only check server health, skip container checks"
    )
    parser.add_argument(
        "--containers-only",
        action="store_true",
        help="Only check container health, skip server check"
    )
    parser.add_argument(
        "--brief",
        action="store_true",
        help="Show compact one-line status (good for monitoring)"
    )
    
    args = parser.parse_args()
    
    # Disable colors if requested or if not a TTY
    if args.no_color or not sys.stdout.isatty():
        Colors.disable()
    
    # Run health checks
    async def run_checks():
        if args.server_only:
            server_health = await check_server_health(args.port)
            return OverallHealth(server=server_health, containers=[])
        elif args.containers_only:
            containers = await check_all_containers(
                args.containers, args.base_port, args.token
            )
            return OverallHealth(
                server=ServerHealth(reachable=True, status="healthy"),
                containers=containers
            )
        else:
            return await check_all(
                args.port, args.containers, args.base_port, args.token
            )
    
    health = asyncio.run(run_checks())
    
    # Output results
    if args.json:
        output_json(health)
    elif args.brief:
        # Compact one-line output for monitoring
        from datetime import datetime
        timestamp = datetime.now().strftime("%H:%M:%S")
        
        server = health.server
        if server.reachable:
            status_icon = Colors.GREEN + "●" + Colors.RESET
            sessions_info = f"{server.available_sessions}/{server.total_sessions} avail"
            in_use = server.in_use_sessions or 0
            if in_use > 0:
                sessions_info += f", {in_use} in use"
        else:
            status_icon = Colors.RED + "●" + Colors.RESET
            sessions_info = "UNREACHABLE"
        
        healthy_containers = sum(1 for c in health.containers if c.healthy)
        total_containers = len(health.containers)
        
        if total_containers > 0:
            container_info = f"{healthy_containers}/{total_containers} containers"
        else:
            container_info = "no container checks"
        
        print(f"[{timestamp}] {status_icon} Sessions: {sessions_info} | {container_info}")
    else:
        print(f"\n{Colors.BOLD}{'='*60}{Colors.RESET}")
        print(f"{Colors.BOLD}           SWEREX HEALTH CHECK{Colors.RESET}")
        print(f"{Colors.BOLD}{'='*60}{Colors.RESET}")
        
        if not args.containers_only:
            print_server_health(health.server)
        
        if not args.server_only and health.containers:
            if args.verbose:
                print_containers_verbose(health.containers)
            else:
                print_containers_summary(health.containers)
        
        print_overall_status(health)
        print()
    
    sys.exit(health.exit_code)


if __name__ == "__main__":
    main()
