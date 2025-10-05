import sys
import os
import platform
import urllib.request
import subprocess
import tempfile
import shutil

def ensure_podman(local_dir="bin"):
    podman_path = shutil.which("podman")
    if podman_path:
        print(f"✅ Podman found at {podman_path}")
        return podman_path

    os.makedirs(local_dir, exist_ok=True)

    # --- Detect OS & architecture ---
    system = platform.system().lower()   # 'windows', 'darwin', 'linux'
    arch = platform.machine().lower()    # 'amd64', 'arm64', etc.

    if system == "windows":
        if arch in ("amd64", "x86_64"):
            file_name = "podman-installer-windows-amd64.exe"
        elif "arm" in arch:
            file_name = "podman-installer-windows-arm64.exe"
        else:
            raise RuntimeError(f"Unsupported Windows arch: {arch}")
    elif system == "darwin":
        if arch == "x86_64":
            file_name = "podman-installer-macos-amd64.pkg"
        elif arch == "arm64":
            file_name = "podman-installer-macos-arm64.pkg"
        else:
            raise RuntimeError(f"Unsupported macOS arch: {arch}")
    elif system == "linux":
        if arch in ("x86_64", "amd64"):
            file_name = "podman-remote-static-linux_amd64.tar.gz"
        elif "arm" in arch:
            file_name = "podman-remote-static-linux_arm64.tar.gz"
        else:
            raise RuntimeError(f"Unsupported Linux arch: {arch}")
    else:
        raise RuntimeError(f"Unsupported OS: {system}")

    # --- Download Podman ---
    release_url = f"https://github.com/containers/podman/releases/latest/download/{file_name}"
    local_path = os.path.join(local_dir, file_name)
    print(f"Downloading Podman {file_name}...")
    urllib.request.urlretrieve(release_url, local_path)
    print(f"✅ Downloaded to {local_path}")

    # --- Install / Extract ---
    if system == "windows":
        print("Running Windows installer quietly...")
        subprocess.run([local_path, "/quiet", "/norestart"], check=True)
    elif system == "darwin":
        print("Running macOS installer (requires sudo)...")
        subprocess.run(["sudo", "installer", "-pkg", local_path, "-target", "/"], check=True)
    elif system == "linux":
        print("Extracting Linux static Podman...")
        extract_dir = os.path.join(local_dir, "podman")
        os.makedirs(extract_dir, exist_ok=True)
        subprocess.run(["tar", "-xzf", local_path, "-C", extract_dir], check=True)
        # Optional: add extract_dir to PATH in current session
        os.environ["PATH"] = extract_dir + os.pathsep + os.environ.get("PATH", "")

    # --- Verify installation ---
    podman_path = shutil.which("podman")
    if podman_path:
        print(f"✅ Podman installed successfully: {podman_path}")
        return podman_path
    else:
        raise RuntimeError("❌ Podman installation failed or not in PATH.")

