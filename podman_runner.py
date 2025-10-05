# podman_runner.py
import asyncio
import os
import sys
import shutil
import tempfile
import uuid
import zipfile
import platform
from pathlib import Path
from typing import Optional, Dict, Any

MAX_LOG_BYTES = 200_000  # truncate logs to this many bytes

def _truncate(s: str, n: int = MAX_LOG_BYTES) -> str:
    if s is None:
        return ""
    b = s.encode("utf-8", errors="replace")
    if len(b) <= n:
        return s
    return b[:n].decode("utf-8", errors="replace") + "\n...[truncated]\n"

def find_podman() -> Optional[str]:
    """Locate a podman executable, tries PATH then common install locations."""
    pod = shutil.which("podman")
    if pod:
        return pod

    plat = sys.platform
    if plat.startswith("win"):
        default = Path(r"C:\Program Files\RedHat\Podman\podman.exe")
        if default.exists():
            return str(default)
        # remote client binary shipped with Podman Desktop sometimes:
        alt = Path(r"C:\Program Files\Podman\podman.exe")
        if alt.exists():
            return str(alt)
    else:
        for path in ("/usr/local/bin/podman", "/usr/bin/podman", "/bin/podman"):
            if Path(path).exists():
                return path
    return None


async def _run_subproc(cmd, timeout: Optional[int]):
    """Run subprocess asynchronously and return (exit_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, b"", b"timeout"
    return proc.returncode, stdout or b"", stderr or b""


async def _ensure_podman_machine_running(podman_path: str, timeout: int = 120) -> Optional[str]:
    """
    On mac/windows Podman often needs a VM ("machine"). Try 'podman info' first.
    If it reports inability to connect or the machine doesn't exist, try to init & start.
    Returns None on success, or an error string on failure.
    """
    # On Linux: nothing to do
    if sys.platform.startswith("linux"):
        return None

    # 1) quick 'podman info'
    rc, out, err = await _run_subproc([podman_path, "info"], timeout=20)
    if rc == 0:
        return None  # OK

    err_text = err.decode("utf-8", errors="replace").lower()
    # Common failure messages to trigger init/start
    triggers = ("vm does not exist", "unable to connect", "no connection", "cannot connect", "connection refused")
    if not any(t in err_text for t in triggers):
        # unknown error but not a clear machine issue -> return the error for debugging
        return f"podman info failed: {err.decode('utf-8', errors='replace')}".strip()

    # 2) try to start the default machine
    rc, out, err = await _run_subproc([podman_path, "machine", "start"], timeout=timeout)
    if rc == 0:
        return None

    err_text_full = err.decode("utf-8", errors="replace")
    # If start failed because VM doesn't exist, try init -> start
    if "vm does not exist" in err_text_full.lower() or "not found" in err_text_full.lower():
        rc2, out2, err2 = await _run_subproc([podman_path, "machine", "init"], timeout=timeout)
        if rc2 != 0:
            return f"podman machine init failed: {err2.decode('utf-8', errors='replace')}"
        # now start again
        rc3, out3, err3 = await _run_subproc([podman_path, "machine", "start"], timeout=timeout)
        if rc3 != 0:
            return f"podman machine start failed after init: {err3.decode('utf-8', errors='replace')}"
        return None

    # Other start failure
    return f"podman machine start failed: {err_text_full.strip()}"



def _get_podman_network_args() -> list[str]:
    system = platform.system().lower()

    # On Windows/macOS, podman machine handles networking, slirp4netns is not used
    if system in ("windows", "darwin"):
        return []  # let podman pick default network (bridge)

    # On Linux, prefer slirp4netns if installed
    if shutil.which("slirp4netns"):
        return ["--network", "slirp4netns:allow_host_loopback=false"]

    # Fallback to bridge
    return ["--network", "bridge"]

async def run_job_in_podman(
    job_folder: str,
    cmd: str,
    *,
    image: str = "python:3.12-slim",
    timeout: int = 600,
    keep_dirs: bool = False,
    requirements: Optional[str] = None,
    cpu_cores: Optional[int] = None,
    ram: Optional[str] = None,
    gpu: Optional[str] = None,
    container_name: Optional[str] = None
) -> Dict[str, Any]:
    podman_path = find_podman()
    if not podman_path:
        return {"ok": False, "error": "podman not found on PATH"}

    _default_tmp_base = os.environ.get("PODRUN_WORKDIR", "/var/tmp")
    if not os.path.isdir(_default_tmp_base):
        _default_tmp_base = tempfile.gettempdir()

    tmp_root = tempfile.mkdtemp(prefix="podrun_", dir=_default_tmp_base)
    workspace = os.path.join(tmp_root, "workspace")
    output = os.path.join(tmp_root, "output")
    os.makedirs(workspace, exist_ok=True)
    os.makedirs(output, exist_ok=True)

    # make output world-writable so container non-root user can write logs
    try:
        os.chmod(output, 0o777)
    except Exception:
        # best-effort; continue even if chmod fails
        pass

    # copy requirements file (if provided) into workspace host copy
    if requirements:
        req_name = os.path.basename(requirements)
        try:
            shutil.copy2(requirements, os.path.join(workspace, req_name))
        except Exception as e:
            # if requirements path was invalid => proceed without it (caller can detect)
            req_name = None
    else:
        req_name = None

    if container_name is None:
        container_name = "gpulend-" + uuid.uuid4().hex[:12]

    mount_suffix = ",Z" if sys.platform.startswith("linux") else ""

    try:
        src = Path(job_folder)
        if not src.exists():
            return {"ok": False, "error": f"job_folder not found: {job_folder}", "container_name": container_name}

        # create a lightweight host copy (read-only mount in container)
        if src.is_dir():
            shutil.copytree(src, workspace, dirs_exist_ok=True)
        else:
            shutil.copy2(src, os.path.join(workspace, src.name))

        resource_flags = []
        if cpu_cores:
            resource_flags += ["--cpus", str(cpu_cores)]
        if ram:
            resource_flags += ["--memory", str(ram)]
        if gpu:
            resource_flags += ["--gpus", str(gpu)]

        network_flags = _get_podman_network_args()

        workspace_tmpfs_size = os.environ.get("PODRUN_TMPFS_WORKSPACE_SIZE", "15g")
        tmp_tmpfs_size = os.environ.get("PODRUN_TMPFS_TMP_SIZE", "15g")

        # Build container-side script:
        # - copy host read-only /workspace-src into writable tmpfs /workspace (no preserved ownership)
        # - create venv in /workspace and install pip/setuptools/wheel
        # - optionally install requirements
        # - run the command, writing logs to /output which is host-mounted writable
        install_and_run_script_parts = [
            "set -euo pipefail",
            # copy source into writable tmpfs; use simple recursive copy (avoid preserving ownership)
            "rm -rf /workspace || true",
            "mkdir -p /workspace",
            "cp -r /workspace-src/. /workspace || true",
            "cd /workspace || exit 1",
            "rm -rf .venv || true",
            "python -m venv .venv",
            "./.venv/bin/python -m pip install --upgrade pip setuptools wheel --no-cache-dir --disable-pip-version-check",
        ]

        if req_name:
            # ensure TMPDIR points to tmpfs /tmp during installs
            install_and_run_script_parts.append("export TMPDIR=/tmp")
            install_and_run_script_parts.append(
                f"./.venv/bin/python -m pip install --no-cache-dir --disable-pip-version-check -r /workspace/{req_name}"
            )

        # run the user command, logs to /output which is a writable host mount
        safe_cmd = (
            "PATH=/workspace/.venv/bin:$PATH HOME=/tmp sh -c "
            f"'{cmd}' > /output/stdout.log 2> /output/stderr.log"
        )
        install_and_run_script_parts.append(safe_cmd)

        installer_cmd = "; ".join(install_and_run_script_parts)

        run_container_cmd = [
            podman_path, "run", "--rm",
            # keep container rootfs read-only for safety, but give writable tmpfs and explicit writable output mount
            #"--read-only",
            "--tmpfs", f"/workspace:rw,size={workspace_tmpfs_size}",
            "--tmpfs", f"/tmp:rw,size={tmp_tmpfs_size}",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "128",
            "--cap-drop", "ALL",
            *network_flags,
            # run as nobody/nogroup inside the container (less privileges)
            "--user", "65534:65534",
            "--name", container_name,
            # host workspace copy mounted read-only (container copies it into tmpfs)
            "-v", f"{os.path.abspath(workspace)}:/workspace-src:ro{mount_suffix}",
            # host output mounted writable so container can write logs/results
            "-v", f"{os.path.abspath(output)}:/output:rw{mount_suffix}",
            *resource_flags,
            image,
            "sh", "-c", installer_cmd
        ]

        # run container and capture output
        rc, stdout_b, stderr_b = await _run_subproc(run_container_cmd, timeout=timeout)
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")

        # read logs from output dir if present
        out_files = {}
        for log_name in ("stdout.log", "stderr.log"):
            log_path = os.path.join(output, log_name)
            if os.path.exists(log_path):
                try:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        out_files[log_name] = f.read()
                except Exception as e:
                    out_files[log_name] = f"(read error: {e})"
            else:
                # fallback to captured podman stdout/stderr
                out_files[log_name] = stdout if log_name == "stdout.log" else stderr

        # zip the host workspace copy (not the tmpfs venv) to return user code & small artifacts
        zip_path = os.path.join(tmp_root, "workspace.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            # include workspace (user code)
            for root, dirs, files in os.walk(workspace):
                for file in files:
                    abs_path = os.path.join(root, file)
                    rel_path = os.path.relpath(abs_path, workspace)
                    zipf.write(abs_path, rel_path)
    # include /output (job output/logs)
    for root, dirs, files in os.walk(output):
        for file in files:
            abs_path = os.path.join(root, file)
            rel_path = os.path.relpath(abs_path, tmp_root)  # keep relative path
            zipf.write(abs_path, rel_path)

        return {
            "ok": True,
            "exit_code": rc,
            "logs": {
                "stdout": _truncate(out_files.get("stdout.log", "")),
                "stderr": _truncate(out_files.get("stderr.log", "")),
                "out_files": {k: (_truncate(v) if isinstance(v, str) else str(v)) for k, v in out_files.items()}
            },
            "podman_cmd_stdout": _truncate(stdout),
            "podman_cmd_stderr": _truncate(stderr),
            "workspace_zip": zip_path,
            "workspace": tmp_root if keep_dirs else None,
            "container_name": container_name
        }

    except Exception as e:
        return {"ok": False, "error": f"exception: {e}", "container_name": container_name}
    finally:
        if not keep_dirs:
            try:
                shutil.rmtree(tmp_root)
            except Exception:
                pass
