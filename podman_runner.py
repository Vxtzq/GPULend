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
    """
    Copy job_folder into a writable 'workspace' and run `cmd` with /workspace mounted rw inside the container.
    No virtualenvs are created. If `requirements` is provided it will be installed inside the container with
    `python -m pip install -r /workspace/<req>` (ephemeral, inside container).
    Returns a zip of the workspace (only workspace content).
    """
    podman_path = find_podman()
    if not podman_path:
        return {"ok": False, "error": "podman not found on PATH"}

    # Ensure podman machine running where applicable
    machine_err = await _ensure_podman_machine_running(podman_path)
    if machine_err is not None:
        return {"ok": False, "error": f"podman machine problem: {machine_err}"}

    _default_tmp_base = os.environ.get("PODRUN_WORKDIR", "/var/tmp")
    if not os.path.isdir(_default_tmp_base):
        _default_tmp_base = tempfile.gettempdir()

    tmp_root = tempfile.mkdtemp(prefix="podrun_", dir=_default_tmp_base)
    workspace = os.path.join(tmp_root, "workspace")
    os.makedirs(workspace, exist_ok=True)

    # copy requirements file name if provided (we also accept full path)
    if requirements:
        req_name = os.path.basename(requirements)
        try:
            shutil.copy2(requirements, os.path.join(workspace, req_name))
        except Exception:
            req_name = None
    else:
        req_name = None

    if container_name is None:
        container_name = "gpulend-" + uuid.uuid4().hex[:12]

    try:
        src = Path(job_folder)
        if not src.exists():
            return {"ok": False, "error": f"job_folder not found: {job_folder}", "container_name": container_name}

        # COPY job_folder into workspace (host copy) so original is not touched
        if src.is_dir():
            shutil.copytree(src, workspace, dirs_exist_ok=True)
        else:
            shutil.copy2(src, os.path.join(workspace, src.name))

        # Best-effort: make workspace and its contents writeable so container can modify them.
        try:
            for root, dirs, files in os.walk(workspace):
                try:
                    os.chmod(root, 0o777)
                except Exception:
                    pass
                for d in dirs:
                    try:
                        os.chmod(os.path.join(root, d), 0o777)
                    except Exception:
                        pass
                for f in files:
                    try:
                        os.chmod(os.path.join(root, f), 0o666)
                    except Exception:
                        pass
        except Exception:
            pass  # best-effort

        # resource flags
        resource_flags = []
        if cpu_cores:
            resource_flags += ["--cpus", str(cpu_cores)]
        if ram:
            resource_flags += ["--memory", str(ram)]
        if gpu:
            resource_flags += ["--gpus", str(gpu)]

        network_flags = _get_podman_network_args()

        # Mount options: add SELinux label on Linux
        is_linux = sys.platform.startswith("linux")
        workspace_opts = "rw,Z" if is_linux else "rw"

        # Build inner command:
        # - fail fast
        # - cd into /workspace
        # - if requirements exist, run pip install inside the container (no venv)
        # - run user's cmd and capture stdout/stderr into /workspace/*.log
        inner_parts = ["set -euo pipefail", "cd /workspace || exit 1"]

        if req_name:
            # install requirements inside container (ephemeral)
            inner_parts.append(f"python -m pip install --upgrade pip setuptools wheel --no-cache-dir --disable-pip-version-check || true")
            inner_parts.append(f"python -m pip install --no-cache-dir -r /workspace/{req_name}")

        # run the user command and capture logs inside workspace
        inner_parts.append(f"HOME=/tmp sh -c '{cmd}' > /workspace/stdout.log 2> /workspace/stderr.log")

        inner_cmd = "; ".join(inner_parts)

        run_container_cmd = [
            podman_path, "run", "--rm",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "128",
            "--cap-drop", "ALL",
            *network_flags,
            "--name", container_name,
            "-v", f"{os.path.abspath(workspace)}:/workspace:{workspace_opts}",
            *resource_flags,
            image,
            "sh", "-c", inner_cmd
        ]

        rc, stdout_b, stderr_b = await _run_subproc(run_container_cmd, timeout=timeout)
        pod_stdout = stdout_b.decode("utf-8", errors="replace")
        pod_stderr = stderr_b.decode("utf-8", errors="replace")

        # After run: collect stdout/stderr from workspace if present, else fall back to podman output
        out_files = {}
        for name in ("stdout.log", "stderr.log"):
            pth = os.path.join(workspace, name)
            if os.path.exists(pth):
                try:
                    with open(pth, "r", encoding="utf-8", errors="replace") as f:
                        out_files[name] = f.read()
                except Exception as e:
                    out_files[name] = f"(read error: {e})"
            else:
                out_files[name] = pod_stdout if name == "stdout.log" else pod_stderr

        # Zip only the workspace contents to return artifacts
        zip_path = os.path.join(tmp_root, "workspace.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(workspace):
                for file in files:
                    abs_path = os.path.join(root, file)
                    rel_path = os.path.relpath(abs_path, workspace)
                    zipf.write(abs_path, rel_path)

        return {
            "ok": True,
            "exit_code": rc,
            "logs": {
                "stdout": _truncate(out_files.get("stdout.log", "")),
                "stderr": _truncate(out_files.get("stderr.log", "")),
                "out_files": {k: (_truncate(v) if isinstance(v, str) else str(v)) for k, v in out_files.items()}
            },
            "podman_cmd_stdout": _truncate(pod_stdout),
            "podman_cmd_stderr": _truncate(pod_stderr),
            "workspace_zip": zip_path,
            "workspace": workspace if keep_dirs else None,
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
