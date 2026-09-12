"""Status submodule: command execution, parsing, disk space, service status."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import HTTPException
from loguru import logger

from sek8s.config import SystemStatusConfig

from .models import SERVICE_ALLOWLIST, CommandResult, ServiceDefinition
from .responses import (
    DirectoryInfo,
    DiskSpaceResponse,
    FilesystemInfo,
    NvidiaSmiResponse,
    ServiceInfo,
    ServiceStatus,
    ServiceStatusResponse,
)


def parse_key_value(output: str) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key] = value
    return parsed


def truncate(value: str, limit: int, *, keep_tail: bool = False) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    if keep_tail:
        return value[-limit:], True
    return value[:limit], True


async def run_command(
    command: List[str],
    timeout: float,
    limit: int,
    *,
    keep_tail: bool = False,
) -> CommandResult:
    """Run a command and return its output. Raises HTTPException on failure.

    When *keep_tail* is True the last *limit* bytes of stdout/stderr are
    kept instead of the first, which is the correct behaviour for log
    output where the most recent entries are at the end.
    """
    logger.debug("Executing command: {}", command)
    command_name = command[1] if command[0] == "sudo" else command[0]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        logger.error("Binary not found for {}", command)
        raise HTTPException(
            status_code=503,
            detail={"error": "missing_binary", "binary": command_name},
        ) from exc
    except (BlockingIOError, OSError):
        raise

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError as exc:
        logger.error("Command timeout for {}", command)
        raise HTTPException(
            status_code=504,
            detail={"error": "timeout", "command": command_name},
        ) from exc

    stdout, stdout_truncated = truncate(
        stdout_bytes.decode("utf-8", errors="replace"),
        limit,
        keep_tail=keep_tail,
    )
    stderr, stderr_truncated = truncate(
        stderr_bytes.decode("utf-8", errors="replace"),
        limit,
        keep_tail=keep_tail,
    )

    result = CommandResult(
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )

    if result.exit_code != 0:
        logger.warning(
            "Command {} returned exit code {}", command_name, result.exit_code
        )

    return result


def resolve_service(service_id: str) -> ServiceDefinition:
    if service_id not in SERVICE_ALLOWLIST:
        raise HTTPException(status_code=404, detail="service not allowed")
    return SERVICE_ALLOWLIST[service_id]


def is_service_healthy(status: ServiceStatus, *, masked_ok: bool = False) -> bool:
    # A masked unit is intentionally disabled — treat as healthy when permitted.
    if status.unit_file_state == "masked":
        return masked_ok
    if status.load_state != "loaded" or status.active_state != "active":
        return False
    if status.sub_state in {"running", "listening", None}:
        return True
    # Oneshot services report active/exited after successful completion.
    if status.sub_state == "exited" and status.exit_status == "0":
        return True
    return False


async def collect_service_status(
    service: ServiceDefinition,
    config: SystemStatusConfig,
    *,
    tolerate_errors: bool = False,
) -> ServiceStatusResponse:
    properties = [
        "Id",
        "LoadState",
        "ActiveState",
        "SubState",
        "MainPID",
        "ExecMainStatus",
        "ExecMainCode",
        "UnitFileState",
    ]
    command = [
        "systemctl",
        "show",
        service.unit,
        "--no-pager",
    ] + [f"--property={prop}" for prop in properties]

    try:
        result = await run_command(
            command, config.command_timeout_seconds, config.max_output_bytes
        )
    except HTTPException as exc:
        if tolerate_errors:
            error_value = (
                exc.detail
                if isinstance(exc.detail, dict)
                else {"error": str(exc.detail)}
            )
            return ServiceStatusResponse(
                service=ServiceInfo(
                    id=service.service_id,
                    unit=service.unit,
                    description=service.description,
                ),
                status=None,
                healthy=False,
                error=error_value,
            )
        raise

    if result.exit_code != 0:
        error_detail = {
            "error": "command_failed",
            "command": "systemctl",
            "exit_code": result.exit_code,
            "stderr": result.stderr,
        }
        if tolerate_errors:
            return ServiceStatusResponse(
                service=ServiceInfo(
                    id=service.service_id,
                    unit=service.unit,
                    description=service.description,
                ),
                status=None,
                healthy=False,
                error=error_detail,
            )
        raise HTTPException(status_code=502, detail=error_detail)

    data = parse_key_value(result.stdout)
    status = ServiceStatus(
        load_state=data.get("LoadState"),
        active_state=data.get("ActiveState"),
        sub_state=data.get("SubState"),
        unit_file_state=data.get("UnitFileState"),
        main_pid=data.get("MainPID"),
        exit_code=data.get("ExecMainCode"),
        exit_status=data.get("ExecMainStatus"),
    )

    return ServiceStatusResponse(
        service=ServiceInfo(
            id=service.service_id,
            unit=service.unit,
            description=service.description,
        ),
        status=status,
        healthy=is_service_healthy(status, masked_ok=service.masked_ok),
        error=None,
    )


VIRTUAL_FS_EXCLUDES = ["/proc", "/sys", "/dev", "/run", "/snap"]


def _du_exclude_args(target_path: Path) -> List[str]:
    """Return --exclude flags for virtual/pseudo filesystems when scanning root.

    These directories cause permission errors and slow scans without
    providing useful disk-utilization data.
    """
    if str(target_path) != "/":
        return []
    return [f"--exclude={d}" for d in VIRTUAL_FS_EXCLUDES]


def validate_path(path: str) -> Path:
    """Validate and resolve path, ensuring it's safe to query."""
    try:
        resolved = Path(path).resolve()
    except (ValueError, RuntimeError) as exc:
        logger.error("Invalid path: {}", path)
        raise HTTPException(status_code=400, detail="Invalid path") from exc

    if not resolved.exists():
        raise HTTPException(status_code=404, detail="Path does not exist")

    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")

    return resolved


def parse_du_line(line: str) -> tuple[int, str]:
    """Parse a line from du output: size<tab>path."""
    parts = line.split("\t", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid du output line: {line}")
    try:
        size_kb = int(parts[0])
    except ValueError as exc:
        raise ValueError(f"Invalid size in du output: {parts[0]}") from exc
    return size_kb * 1024, parts[1]


def human_readable_size(size_bytes: int) -> str:
    """Convert bytes to human-readable format."""
    size: float = size_bytes
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def _parse_df_line(line: str) -> Optional[FilesystemInfo]:
    """Parse a line from df -k output (default or --output=source,size,used,avail,pcent,target)."""
    parts = line.split()
    if len(parts) < 6:
        return None
    try:
        size_kb = int(parts[1])
        used_kb = int(parts[2])
        avail_kb = int(parts[3])
    except ValueError:
        return None
    source = parts[0]
    pcent_str = parts[4].rstrip("%")
    try:
        used_percent = float(pcent_str)
    except ValueError:
        used_percent = (used_kb / size_kb * 100) if size_kb > 0 else 0.0
    target = " ".join(parts[5:]) if len(parts) > 5 else ""
    total_bytes = size_kb * 1024
    used_bytes = used_kb * 1024
    available_bytes = avail_kb * 1024
    return FilesystemInfo(
        source=source,
        target=target,
        total_bytes=total_bytes,
        used_bytes=used_bytes,
        available_bytes=available_bytes,
        total_human=human_readable_size(total_bytes),
        used_human=human_readable_size(used_bytes),
        available_human=human_readable_size(available_bytes),
        used_percent=round(used_percent, 2),
    )


async def _get_filesystems(
    validated_path: Path,
    config: SystemStatusConfig,
) -> Optional[List[FilesystemInfo]]:
    """Run df -k for path (all mounts if path is /, else single filesystem). Returns None on failure."""
    path_str = str(validated_path.resolve())
    if path_str == "/":
        command = ["df", "-k"]
    else:
        command = ["df", "-k", path_str]
    try:
        result = await run_command(
            command,
            config.command_timeout_seconds,
            config.max_output_bytes,
        )
    except HTTPException:
        return None
    if result.exit_code != 0:
        return None
    filesystems: List[FilesystemInfo] = []
    for line in result.stdout.strip().splitlines():
        info = _parse_df_line(line)
        if info is not None:
            filesystems.append(info)
    return filesystems if filesystems else None


async def get_disk_space_simple(
    validated_path: Path,
    config: SystemStatusConfig,
    cross_filesystems: bool = False,
) -> DiskSpaceResponse:
    command = ["sudo", "du", "-k"]
    if not cross_filesystems:
        command.append("-x")
    command.extend(_du_exclude_args(validated_path))
    command.extend(["--max-depth=1", str(validated_path)])

    timeout = max(config.command_timeout_seconds * 5, 120)

    result = await run_command(
        command,
        timeout,
        config.max_output_bytes,
    )

    directories: List[DirectoryInfo] = []
    parent_size = 0

    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        try:
            size_bytes, dir_path = parse_du_line(line)
        except ValueError as exc:
            logger.warning("Failed to parse du line: {}", exc)
            continue

        if Path(dir_path).resolve() == validated_path:
            parent_size = size_bytes
            continue

        dir_name = Path(dir_path).name
        directories.append(
            DirectoryInfo(
                name=dir_name,
                path=dir_path,
                size_bytes=size_bytes,
                size_human=human_readable_size(size_bytes),
                depth=1,
                percentage=None,
            )
        )

    directories.sort(key=lambda d: d.size_bytes, reverse=True)
    total_bytes = (
        parent_size if parent_size > 0 else sum(d.size_bytes for d in directories)
    )

    if total_bytes > 0:
        for d in directories:
            d.percentage = (d.size_bytes / total_bytes) * 100

    filesystems = await _get_filesystems(validated_path, config)
    return DiskSpaceResponse(
        path=str(validated_path),
        directories=directories,
        total_size_bytes=total_bytes,
        total_size_human=human_readable_size(total_bytes),
        stdout_truncated=result.stdout_truncated,
        diagnostic_mode=False,
        max_depth=None,
        top_n=None,
        filesystems=filesystems,
    )


async def get_disk_space_diagnostic(
    validated_path: Path,
    config: SystemStatusConfig,
    max_depth: int,
    top_n: int,
    cross_filesystems: bool = False,
) -> DiskSpaceResponse:
    command = ["sudo", "du", "-k"]
    if not cross_filesystems:
        command.append("-x")
    command.extend(_du_exclude_args(validated_path))
    command.extend([f"--max-depth={max_depth}", str(validated_path)])

    timeout = max(config.command_timeout_seconds * 5, 120)

    result = await run_command(
        command,
        timeout,
        config.max_output_bytes * 2,
    )

    all_entries: List[tuple[int, str, int]] = []
    root_size = 0

    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        try:
            size_bytes, dir_path = parse_du_line(line)
        except ValueError as exc:
            logger.warning("Failed to parse du line: {}", exc)
            continue

        resolved = Path(dir_path).resolve()
        try:
            relative = resolved.relative_to(validated_path)
            depth = len(relative.parts) if str(relative) != "." else 0
        except ValueError:
            continue

        if depth == 0:
            root_size = size_bytes
            continue

        all_entries.append((size_bytes, str(resolved), depth))

    depth_groups: Dict[int, List[tuple[int, str]]] = {}
    for size_bytes, path, depth in all_entries:
        if depth not in depth_groups:
            depth_groups[depth] = []
        depth_groups[depth].append((size_bytes, path))

    top_offenders: List[DirectoryInfo] = []
    for depth in sorted(depth_groups.keys()):
        entries = depth_groups[depth]
        entries.sort(reverse=True)
        for size_bytes, path in entries[:top_n]:
            percentage = (size_bytes / root_size * 100) if root_size > 0 else 0
            top_offenders.append(
                DirectoryInfo(
                    name=Path(path).name,
                    path=path,
                    size_bytes=size_bytes,
                    size_human=human_readable_size(size_bytes),
                    depth=depth,
                    percentage=percentage,
                )
            )

    top_offenders.sort(key=lambda d: d.size_bytes, reverse=True)

    filesystems = await _get_filesystems(validated_path, config)
    return DiskSpaceResponse(
        path=str(validated_path),
        directories=top_offenders,
        total_size_bytes=root_size,
        total_size_human=human_readable_size(root_size),
        stdout_truncated=result.stdout_truncated,
        diagnostic_mode=True,
        max_depth=max_depth,
        top_n=top_n,
        filesystems=filesystems,
    )


async def nvidia_smi_impl(detail: bool, gpu: str, get_config_fn) -> NvidiaSmiResponse:
    """Implementation for nvidia-smi (cached by detail/gpu only, not config)."""
    config = get_config_fn()
    command = ["nvidia-smi"]
    if detail:
        command.append("-q")

    if gpu != "all":
        try:
            gpu_index = int(gpu)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="gpu must be an integer or 'all'"
            ) from exc
        if gpu_index < 0:
            raise HTTPException(status_code=400, detail="gpu must be non-negative")
        command.extend(["-i", str(gpu_index)])

    result = await run_command(
        command, config.command_timeout_seconds, config.max_output_bytes
    )

    status_code = 200 if result.exit_code == 0 else 502
    stdout_lines = result.stdout.splitlines()
    stderr_lines = result.stderr.splitlines()
    return NvidiaSmiResponse(
        command=command,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        stdout_lines=stdout_lines,
        stderr_lines=stderr_lines,
        stdout_truncated=result.stdout_truncated,
        stderr_truncated=result.stderr_truncated,
        detail=detail,
        gpu=gpu,
        status="ok" if status_code == 200 else "error",
    )
