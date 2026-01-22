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
Generate docker-compose.yaml for SweRex Session Server with N containers.

Usage:
    # Generate with 128 containers
    python generate_compose.py --containers 128
    
    # Generate with custom settings
    python generate_compose.py --containers 64 --sessions-per-container 16 --output my-compose.yaml
    
    # Generate with port mapping for local development
    python generate_compose.py --containers 8 --with-ports --base-port 18000
"""

import argparse
import os
from pathlib import Path


def generate_compose(
    num_containers: int,
    sessions_per_container: int = 8,
    with_ports: bool = False,
    base_port: int = 18000,
    include_server: bool = True,
    output_path: str = "docker-compose.yaml",
) -> str:
    """
    Generate docker-compose.yaml content for N swerex containers.
    
    Args:
        num_containers: Number of swerex sandbox containers
        sessions_per_container: Sessions per container (for env file)
        with_ports: Whether to expose ports on host (for local dev)
        base_port: Starting port number if with_ports=True
        include_server: Whether to include the session-server service
        output_path: Path to write the compose file
        
    Returns:
        The generated YAML content
    """
    # Generate endpoint list
    endpoints = [f"http://swerex-{i}:8000" for i in range(num_containers)]
    endpoints_str = ",".join(endpoints)
    
    # Build YAML content
    lines = [
        "# Auto-generated docker-compose.yaml",
        f"# Generated with: python generate_compose.py --containers {num_containers}",
        "#",
        "# Usage:",
        "#   docker compose up -d",
        "#   docker compose logs -f session-server",
        "#   curl http://localhost:8080/health",
        "",
        'version: "3.8"',
        "",
        "services:",
    ]
    
    # Add session-server service if requested
    if include_server:
        lines.extend([
            "  # Session server - manages all containers",
            "  session-server:",
            "    build:",
            "      context: ../..",
            "      dockerfile: docker/swerex-server/Dockerfile.server",
            "    ports:",
            '      - "8080:8080"',
            "    environment:",
            f"      - SWEREX_ENDPOINTS=${{SWEREX_ENDPOINTS:-{endpoints_str}}}",
            f"      - SWEREX_SESSIONS_PER_CONTAINER=${{SWEREX_SESSIONS_PER_CONTAINER:-{sessions_per_container}}}",
            "      - SWEREX_AUTH_TOKEN=${SWEREX_AUTH_TOKEN:-default-token}",
            "    depends_on:",
        ])
        
        # Add depends_on for all containers
        for i in range(num_containers):
            lines.extend([
                f"      swerex-{i}:",
                "        condition: service_healthy",
            ])
        
        lines.extend([
            '    healthcheck:',
            '      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]',
            "      interval: 10s",
            "      timeout: 5s",
            "      retries: 3",
            "    restart: unless-stopped",
            "",
        ])
    
    # Add base swerex service definition (using YAML anchor)
    lines.extend([
        "  # Base swerex sandbox configuration",
        "  swerex-base:",
        "    image: swerex-sandbox:latest",
        "    read_only: true",
        "    tmpfs:",
        "      - /tmp:size=500M,mode=1777",
        "      - /root:size=100M",
        "    environment:",
        "      - AUTH_TOKEN=${SWEREX_AUTH_TOKEN:-default-token}",
        "    healthcheck:",
        '      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]',
        "      interval: 10s",
        "      timeout: 5s",
        "      retries: 3",
        "      start_period: 5s",
        "    restart: unless-stopped",
        "    deploy:",
        "      resources:",
        "        limits:",
        "          cpus: '1'",
        "          memory: 1G",
        "",
    ])
    
    # Add individual swerex services
    for i in range(num_containers):
        lines.extend([
            f"  swerex-{i}:",
            "    extends:",
            "      service: swerex-base",
        ])
        
        if with_ports:
            port = base_port + i
            lines.extend([
                "    ports:",
                f'      - "{port}:8000"',
            ])
        
        lines.append("")
    
    content = "\n".join(lines)
    
    # Write to file
    output_file = Path(output_path)
    output_file.write_text(content)
    print(f"Generated {output_path} with {num_containers} containers")
    
    return content


def generate_env_example(
    num_containers: int,
    sessions_per_container: int = 8,
    output_path: str = ".env.example",
) -> str:
    """Generate .env.example file."""
    endpoints = [f"http://swerex-{i}:8000" for i in range(num_containers)]
    endpoints_str = ",".join(endpoints)
    
    content = f"""# SweRex Session Server Configuration
# Copy this file to .env and modify as needed:
#   cp .env.example .env

# Number of bash sessions per container (default: 8)
# Total sessions = SWEREX_SESSIONS_PER_CONTAINER * num_containers
SWEREX_SESSIONS_PER_CONTAINER={sessions_per_container}

# Authentication token for swerex containers
# IMPORTANT: Change this in production!
SWEREX_AUTH_TOKEN=your-secret-token-here

# Container endpoints (auto-generated for {num_containers} containers)
# Modify if using different hostnames or ports
SWEREX_ENDPOINTS={endpoints_str}
"""
    
    output_file = Path(output_path)
    output_file.write_text(content)
    print(f"Generated {output_path}")
    
    return content


def generate_env_local(
    num_containers: int,
    base_port: int = 18000,
    sessions_per_container: int = 8,
    output_path: str = ".env.local",
) -> str:
    """Generate .env.local file for running server outside Docker."""
    endpoints = [f"http://localhost:{base_port + i}" for i in range(num_containers)]
    endpoints_str = ",".join(endpoints)
    
    content = f"""# Local development configuration
# Use this when running the session server outside Docker
# but containers inside Docker with port mappings.
#
# Usage:
#   export $(cat .env.local | xargs)
#   python -m verl.experimental.agent_loop.swerex_server

SWEREX_SESSIONS_PER_CONTAINER={sessions_per_container}
SWEREX_AUTH_TOKEN=default-token
SWEREX_ENDPOINTS={endpoints_str}
"""
    
    output_file = Path(output_path)
    output_file.write_text(content)
    print(f"Generated {output_path}")
    
    return content


def main():
    parser = argparse.ArgumentParser(
        description="Generate docker-compose.yaml for SweRex Session Server"
    )
    parser.add_argument(
        "--containers", "-n",
        type=int,
        default=4,
        help="Number of swerex containers (default: 4)"
    )
    parser.add_argument(
        "--sessions-per-container", "-s",
        type=int,
        default=8,
        help="Sessions per container (default: 8)"
    )
    parser.add_argument(
        "--with-ports",
        action="store_true",
        help="Expose container ports on host (for local dev)"
    )
    parser.add_argument(
        "--base-port",
        type=int,
        default=18000,
        help="Starting port number when --with-ports is used (default: 18000)"
    )
    parser.add_argument(
        "--no-server",
        action="store_true",
        help="Don't include session-server service (containers only)"
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default="docker-compose.yaml",
        help="Output file path (default: docker-compose.yaml)"
    )
    parser.add_argument(
        "--env-only",
        action="store_true",
        help="Only generate .env files, not docker-compose.yaml"
    )
    
    args = parser.parse_args()
    
    # Get output directory
    output_dir = Path(args.output).parent
    if output_dir and not output_dir.exists():
        output_dir.mkdir(parents=True)
    
    if not args.env_only:
        # Generate docker-compose.yaml
        generate_compose(
            num_containers=args.containers,
            sessions_per_container=args.sessions_per_container,
            with_ports=args.with_ports,
            base_port=args.base_port,
            include_server=not args.no_server,
            output_path=args.output,
        )
    
    # Generate .env files in same directory as compose file
    env_dir = Path(args.output).parent or Path(".")
    
    generate_env_example(
        num_containers=args.containers,
        sessions_per_container=args.sessions_per_container,
        output_path=str(env_dir / ".env.example"),
    )
    
    if args.with_ports:
        generate_env_local(
            num_containers=args.containers,
            base_port=args.base_port,
            sessions_per_container=args.sessions_per_container,
            output_path=str(env_dir / ".env.local"),
        )
    
    # Print summary
    total_sessions = args.containers * args.sessions_per_container
    print(f"\nConfiguration summary:")
    print(f"  Containers: {args.containers}")
    print(f"  Sessions per container: {args.sessions_per_container}")
    print(f"  Total sessions: {total_sessions}")
    if args.with_ports:
        print(f"  Host ports: {args.base_port}-{args.base_port + args.containers - 1}")


if __name__ == "__main__":
    main()
