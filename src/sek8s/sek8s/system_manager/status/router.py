"""Status submodule: FastAPI router and route handlers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from aiocache import cached as aiocache_cached
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from loguru import logger

from sek8s.config import SystemStatusConfig
from sek8s.services.util import authorize

from .models import SERVICE_ALLOWLIST
from .responses import (
    DiskSpaceResponse,
    HealthResponse,
    NvidiaSmiResponse,
    OverviewResponse,
    ServiceInfo,
    ServiceLogsResponse,
    ServicesListResponse,
    ServiceStatusResponse,
    ShutdownResponse,
)
from .util import (
    collect_service_status,
    get_disk_space_diagnostic,
    get_disk_space_simple,
    nvidia_smi_impl,
    resolve_service,
    run_command,
    validate_path,
)

router = APIRouter()


@lru_cache(maxsize=1)
def get_config() -> SystemStatusConfig:
    """Return cached SystemStatusConfig (reads env once)."""
    return SystemStatusConfig()


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    description="Returns OK if service is running",
)
@aiocache_cached(ttl=30)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get(
    "/services",
    response_model=ServicesListResponse,
    summary="List available services",
    description="Returns list of all services that can be monitored",
)
@aiocache_cached(ttl=30)
async def list_services(
    _auth: bool = Depends(
        authorize(allow_miner=True, allow_validator=True, purpose="status")
    ),
) -> ServicesListResponse:
    return ServicesListResponse(
        services=[
            ServiceInfo(
                id=service.service_id,
                unit=service.unit,
                description=service.description,
            )
            for service in SERVICE_ALLOWLIST.values()
        ]
    )


@router.get(
    "/services/{service_id}/status",
    response_model=ServiceStatusResponse,
    summary="Get service status",
    description="Returns systemd status for a specific service",
)
@aiocache_cached(ttl=30)
async def get_service_status(
    service_id: str,
    config: SystemStatusConfig = Depends(get_config),
    _auth: bool = Depends(
        authorize(allow_miner=True, allow_validator=True, purpose="status")
    ),
) -> ServiceStatusResponse:
    service = resolve_service(service_id)
    return await collect_service_status(service, config)


@router.get(
    "/services/{service_id}/logs",
    response_model=ServiceLogsResponse,
    summary="Get service logs",
    description="Returns the last N journal log lines for a specific service",
)
async def get_service_logs(
    service_id: str,
    config: SystemStatusConfig = Depends(get_config),
    lines: int = Query(200, ge=1, description="Number of recent log lines to return"),
    since_minutes: int = Query(
        0, ge=0, le=1440, description="Only logs from last N minutes (0 = no filter)"
    ),
    _auth: bool = Depends(
        authorize(allow_miner=True, allow_validator=True, purpose="status")
    ),
) -> ServiceLogsResponse:
    service = resolve_service(service_id)
    clamped_lines = max(1, min(lines, config.log_tail_max))

    command = [
        "journalctl",
        f"--unit={service.unit}",
        "--no-pager",
        "--output=short",
        f"--lines={clamped_lines}",
    ]

    if since_minutes > 0:
        window_limit = min(since_minutes, config.log_window_max_minutes)
        since_time = datetime.now(timezone.utc) - timedelta(minutes=window_limit)
        command.append(f"--since={since_time.isoformat()}")

    result = await run_command(
        command,
        config.command_timeout_seconds,
        config.max_output_bytes,
        keep_tail=True,
    )

    entries = [line for line in result.stdout.splitlines() if line]

    return ServiceLogsResponse(
        service={
            "id": service.service_id,
            "unit": service.unit,
        },
        requested_lines=lines,
        returned_lines=len(entries),
        stdout_truncated=result.stdout_truncated,
        logs=entries,
    )


@router.get(
    "/services/{service_id}/logs/stream",
    summary="Stream service logs",
    description="Stream live journal logs for a specific service via journalctl --follow",
)
async def stream_service_logs(
    service_id: str,
    config: SystemStatusConfig = Depends(get_config),
    since_minutes: int = Query(
        0, ge=0, le=1440, description="Only logs from last N minutes (0 = no filter)"
    ),
    _auth: bool = Depends(
        authorize(allow_miner=True, allow_validator=True, purpose="status")
    ),
):
    service = resolve_service(service_id)
    return StreamingResponse(
        _stream_journal(service, since_minutes, config),
        media_type="text/plain",
    )


async def _stream_journal(service, since_minutes: int, config: SystemStatusConfig):
    """Async generator that follows journalctl output for a service."""
    command = [
        "journalctl",
        f"--unit={service.unit}",
        "--no-pager",
        "--output=short",
        "--follow",
    ]

    if since_minutes > 0:
        window_limit = min(since_minutes, config.log_window_max_minutes)
        since_time = datetime.now(timezone.utc) - timedelta(minutes=window_limit)
        command.append(f"--since={since_time.isoformat()}")

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        if process.stdout is None:
            return
        async for raw_line in process.stdout:
            yield raw_line.decode("utf-8", errors="replace")
    except (asyncio.CancelledError, GeneratorExit):
        pass
    finally:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


@router.get(
    "/gpu/nvidia-smi",
    response_model=NvidiaSmiResponse,
    summary="Get GPU status",
    description="Returns nvidia-smi output for GPUs",
)
@aiocache_cached(ttl=60)
async def nvidia_smi(
    detail: bool = Query(False, description="Return detailed (-q) output"),
    gpu: str = Query("all", description="GPU index or 'all'"),
    _auth: bool = Depends(
        authorize(allow_miner=True, allow_validator=True, purpose="status")
    ),
) -> NvidiaSmiResponse:
    return await nvidia_smi_impl(detail, gpu, get_config)


@router.get(
    "/overview",
    response_model=OverviewResponse,
    summary="System overview",
    description="Returns combined status of all services and GPUs",
)
@aiocache_cached(ttl=30)
async def overview(
    config: SystemStatusConfig = Depends(get_config),
    _auth: bool = Depends(
        authorize(allow_miner=True, allow_validator=True, purpose="status")
    ),
) -> OverviewResponse:
    services = await asyncio.gather(
        *(
            collect_service_status(service, config, tolerate_errors=True)
            for service in SERVICE_ALLOWLIST.values()
        )
    )

    gpu_info = await nvidia_smi_impl(detail=False, gpu="all", get_config_fn=get_config)
    gpu_healthy = gpu_info.status == "ok"
    services_healthy = all(entry.healthy for entry in services)
    overall_status = "ok" if services_healthy and gpu_healthy else "degraded"

    return OverviewResponse(
        status=overall_status,
        services=services,
        gpu=gpu_info,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@router.get(
    "/disk/space",
    response_model=DiskSpaceResponse,
    summary="Get directory sizes",
    description="Returns sizes of immediate subdirectories within a given path",
)
@aiocache_cached(ttl=120)
async def get_disk_space(
    config: SystemStatusConfig = Depends(get_config),
    path: str = Query("/", description="Directory path to analyze"),
    diagnostic: bool = Query(
        False, description="Enable diagnostic mode for deep analysis"
    ),
    max_depth: int = Query(
        3, ge=1, le=10, description="Maximum depth for diagnostic mode (default: 3)"
    ),
    top_n: int = Query(
        10, ge=1, le=100, description="Show top N directories per level (default: 10)"
    ),
    cross_filesystems: bool = Query(
        False,
        description="Cross filesystem boundaries (include mounted volumes)",
    ),
    _auth: bool = Depends(
        authorize(allow_miner=True, allow_validator=True, purpose="status")
    ),
):
    validated_path = validate_path(path)

    if diagnostic:
        return await get_disk_space_diagnostic(
            validated_path, config, max_depth, top_n, cross_filesystems
        )
    return await get_disk_space_simple(validated_path, config, cross_filesystems)


@router.post(
    "/system/shutdown",
    response_model=ShutdownResponse,
    summary="Graceful system shutdown",
    description="Initiates a graceful system shutdown (miner-only, validators excluded)",
    dependencies=[
        Depends(authorize(allow_miner=True, allow_validator=False, purpose="status"))
    ],
)
async def shutdown_system() -> ShutdownResponse:
    timestamp = datetime.now(timezone.utc).isoformat()
    logger.warning("Graceful shutdown requested at {}", timestamp)

    async def delayed_shutdown():
        await asyncio.sleep(2)
        logger.critical("Executing graceful shutdown...")
        try:
            process = await asyncio.create_subprocess_exec(
                "sudo",
                "/sbin/shutdown",
                "-h",
                "now",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            # `shutdown` returning non-zero is NOT an exception — without this check a failed
            # poweroff (e.g. sudo/AppArmor/inhibitor) is swallowed silently and the VM just
            # keeps running. Surface it loudly so the cause is visible in the logs.
            if process.returncode != 0:
                logger.error(
                    "Shutdown command failed (rc={}): stdout={!r} stderr={!r}",
                    process.returncode,
                    stdout.decode(errors="replace").strip(),
                    stderr.decode(errors="replace").strip(),
                )
        except Exception as e:
            logger.error("Failed to execute shutdown: {}", e)

    asyncio.create_task(delayed_shutdown())

    return ShutdownResponse(
        status="initiated",
        message="Graceful shutdown initiated. System will power off in 2 seconds.",
        timestamp=timestamp,
    )
