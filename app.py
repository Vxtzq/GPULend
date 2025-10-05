# Standard library imports
import sys
import os
import shutil
import tempfile
import json
import random
import threading
import asyncio
import zipfile
import subprocess
import webbrowser
import platform
import urllib.request
from pathlib import Path
import traceback
from typing import Any, Dict, Optional

# Third-party imports
import httpx
import psutil
import GPUtil
from podman_runner import find_podman

# PyQt6 imports
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt, QUrl, QTimer, QThread, pyqtSignal, QPropertyAnimation, QEasingCurve, QMetaObject, QSettings, QStandardPaths, QRect
from PyQt6.QtGui import QDesktopServices, QTextCursor, QFont, QColor, QPalette, QIcon
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QDialog, QWidget, QLabel, QTextEdit, QTextBrowser,
    QPushButton, QCheckBox, QVBoxLayout, QHBoxLayout, QFrame, QStackedWidget,
    QListWidget, QListWidgetItem, QComboBox, QFileDialog, QLineEdit, QMessageBox, 
    QProgressBar, QSizePolicy
)

SETTINGS_FILE = "settings.json"



# ---------------------------
# Configuration
# ---------------------------
#SERVER_HOST = "127.0.0.1"
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000
SERVER_BASE = f"http://{SERVER_HOST}:{SERVER_PORT}"



def prepare_job(job_path: str) -> str:
    """
    If job_path is a .zip, extract into a temp dir and return that dir.
    If it's already a folder, return it unchanged.
    """
    p = Path(job_path)
    if p.is_file() and p.suffix == ".zip":
        tmp_dir = tempfile.mkdtemp(prefix="job_unpacked_")
        with zipfile.ZipFile(p, 'r') as zf:
            zf.extractall(tmp_dir)
        return tmp_dir
    elif p.is_dir():
        return str(p)
    else:
        raise FileNotFoundError(f"Invalid job path: {job_path}")
# ---------------------------
# GPU detection helper
# ---------------------------



def get_system_info() -> Dict[str, Any]:
    """Return dict with GPU, VRAM, CPU cores, RAM, and number of GPUs."""
    info = {
        "gpu": "None",
        "vram": 0,
        "cpu_cores": psutil.cpu_count(logical=False),  # physical cores
        "cpu_threads": psutil.cpu_count(logical=True), # logical cores
        "ram_gb": round(psutil.virtual_memory().total / (1024 ** 3), 2),
        "num_gpu": 0
    }

    # GPU info via GPUtil
    try:
        gpus = GPUtil.getGPUs()
        if gpus:
            info["gpu"] = gpus[0].name
            info["vram"] = round(gpus[0].memoryTotal, 2)
            info["num_gpu"] = len(gpus)
    except Exception as e:
        info["gpu"] = f"Unknown ({e})"
        info["vram"] = 0
        info["num_gpu"] = 0

    return info

    return info


# ---------------------------
# Async client helpers
# ---------------------------

class AsyncWorker(QThread):
    """
    Simple QThread-based runner for awaiting asyncio coroutines and
    emitting results back to the Qt main thread.
    Emits result_ready with (result, callback).
    """
    result_ready = pyqtSignal(object, object)

    def __init__(self):
        super().__init__()
        self.tasks = []

    def add_task(self, coro, callback=None):
        self.tasks.append((coro, callback))
        if not self.isRunning():
            self.start()

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        for coro, callback in self.tasks:
            try:
                result = loop.run_until_complete(coro)
                self.result_ready.emit(result, callback)
            except Exception as e:
                error_result = {"ok": False, "error": str(e)}
                self.result_ready.emit(error_result, callback)

        self.tasks.clear()
        loop.close()


class AsyncLoopThread(threading.Thread):
    """Run an asyncio event loop in a background thread and allow submitting coroutines."""
    def __init__(self):
        super().__init__(daemon=True)
        self.loop = asyncio.new_event_loop()

    def run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        """Submit coroutine, returns concurrent.futures.Future."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


async def server_post(path: str, data: Dict[str, Any], timeout: float = 3.0):
    url = SERVER_BASE + path
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(url, json=data)
            resp.raise_for_status()
            return resp.json()
        except httpx.RequestError as e:
            return {"ok": False, "error": str(e)}
        except httpx.HTTPStatusError as e:
            return {"ok": False, "error": f"HTTP {e.response.status_code}: {e.response.text}"}


async def server_get(path: str, params: Dict[str, Any] = None, timeout: float = 3.0):
    url = SERVER_BASE + path
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.RequestError as e:
            return {"ok": False, "error": str(e)}
        except httpx.HTTPStatusError as e:
            return {"ok": False, "error": f"HTTP {e.response.status_code}: {e.response.text}"}


# ---------------------------
# PyQt application (client)
# ---------------------------
class Job:
    def __init__(self, name: str, folder: str, command: str, priority: str = "Medium"):
        self.name = name
        self.folder = folder
        self.command = command
        self.priority = priority
        self.status = "Waiting"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GPULend — Peer GPU Share")
        self.resize(520, 820)
        self.setFixedSize(520, 820)
        QTimer.singleShot(500, self.show_newsletter_dialog)
        # state
        self.current_user: Optional[Dict[str, Any]] = None  # {"username","pwd","credits"...}
        self.credits = 0
        self.job_queue: list[Job] = []
        self.current_job: Optional[Job] = None
        self.uploaded_folder: Optional[str] = None
        self.people_sharing = 0
        self.people_renting = 0
        self.is_sharing = False
        self.is_renting = False
        self.is_offline=True
        #self.gpu_info = get_system_info()
        self.load_settings()
        self.vram = self.gpu_info["vram"]
        self.auto_accept = False
        # timers
        self.timer = QTimer()
        # poll server for pending, credits and counts on the same timer
        self.timer.timeout.connect(self.poll_server_for_pending)
        self.timer.timeout.connect(self.poll_credits)
        self.timer.timeout.connect(self.poll_counts)
        self.timer.start(10000)
        self.setWindowIcon(QtGui.QIcon("assets/icons/logo2.png"))
        self.renter_poll_timer = QTimer()
        self.renter_poll_timer.setInterval(3000)  # poll while waiting
        self.renter_poll_timer.timeout.connect(self.poll_renter_pending)
        self._empty_poll_count = 0
        self._waiting_for_sharer = False
        self._pending_sharer = None
        self.current_max_time= 0
        # async helpers
        self.worker = AsyncWorker()
        self.worker.result_ready.connect(self._handle_async_result)
        self.async_runner = AsyncLoopThread()
        self.async_runner.start()

        # UI
        self.main_layout = QVBoxLayout()
        self.topbar = self.create_topbar()
        self.main_layout.addWidget(self.topbar)

        self.stack = QStackedWidget()
        self.login_screen = self.create_login_screen()
        self.main_screen = self.create_main_screen_content()
        self.create_job_screen = self.create_job_screen_content()
        self.settings_screen = self.create_settings_screen()
        self.rent_gpu_screen = self.create_rent_gpu_screen()
        self._signal_poll_in_flight = False

        self.stack.addWidget(self.login_screen)
        self.stack.addWidget(self.main_screen)
        self.stack.addWidget(self.create_job_screen)
        self.stack.addWidget(self.rent_gpu_screen)
        self.stack.addWidget(self.settings_screen)

        self.main_layout.addWidget(self.stack)

        container = QWidget()
        container.setLayout(self.main_layout)
        self.setCentralWidget(container)

        # visual styling
        self.set_theme()
        self.set_styles()
        self.current_request = None
        # initial UI state
        self.update_indicators()
        self.update_queue_list()
        # initially offline in login screen
        self.set_offline(True)
        self.session_token: Optional[str] = None
        self._last_msg_index = 0

        # signal polling timer (both roles use it to receive "begin" / "output" messages)
        self.signal_timer = QTimer()
        self.signal_timer.setInterval(2000)  # poll every 2s
        self.signal_timer.timeout.connect(self.poll_signalling)
        if self.ensure_podman(self):
            # Podman is ready, continue launching app
            pass
        else:
            # User cancelled or Podman still not installed
            QMessageBox.warning(self, "Podman required",
                                "Podman is required to run containers. Please install and retry.")
            sys.exit(1)


    
    def show_newsletter_dialog(self):
        
        pref_file = "newsletter_pref.json"

        # Load previous preference
        show_newsletter = True
        try:
            with open(pref_file, "r") as f:
                data = json.load(f)
                show_newsletter = data.get("show_newsletter", True)
        except FileNotFoundError:
            pass

        if not show_newsletter:
            return  # skip showing dialog

        dialog = QDialog(self)
        dialog.setWindowTitle("📬 GPULend Newsletter")
        dialog.setMinimumSize(500, 400)
        dialog.setStyleSheet("""
            QDialog { background-color: #0b1624; }
            QLabel { font-weight: 600; font-size: 14px; color: #ffffff; }
            QTextBrowser { 
                background-color: #071026; 
                border: 1px solid rgba(255,255,255,0.06);
                border-radius: 6px; 
                padding: 6px; 
                font-family: Consolas, monospace; 
                color: #ffffff; 
            }
            QPushButton { background-color: #4aa3f0; color: #fff; border-radius: 8px; padding: 8px 12px; }
            QPushButton:hover { background-color: #2f7fd2; }
            QCheckBox { color: #ffffff; font-weight: 500; }
        """)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(15)

        label = QLabel("Welcome to GPULend! Here's the latest newsletter:")
        layout.addWidget(label)

        # QTextBrowser for logs / links
        text_area = QTextBrowser()
        text_area.setOpenExternalLinks(True)
        text_area.setHtml(
            '<div style="color: #ffffff; font-family: Consolas, monospace;">'
            '<b>⚠️ GPULend Beta Notice:</b><br>'
            'This application is currently in <b>beta</b>! Please follow these guidelines:<br>'
            '• Do not attempt to fraud as ghost sharer.<br>'
            '• Avoid running sensitive information in jobs.<br>'
            '• Use responsibly and provide feedback.<br><br>'
            '📢 What\'s new in this beta release:<br>'
            '• Working GPU sharing features!<br>'
            '• Improved container support with Podman.<br>'
            '• Bug fixes and performance improvements.<br>'
            '• Join our community chat for updates.<br><br>'
            '🔗 Report issues at <a href="https://github.com/Vxtzq/GPULend/issues/new" '
            'style="color: #ffd66b; text-decoration: underline;">GitHub</a>'
            '</div>'
        )
        layout.addWidget(text_area)

        # "Do not show again" checkbox
        
        checkbox = QCheckBox("Do not show again")
        layout.addWidget(checkbox, alignment=Qt.AlignmentFlag.AlignLeft)

        # Centered button
        btn_container = QHBoxLayout()
        btn_container.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ok_btn = QPushButton("Got it!")
        ok_btn.clicked.connect(dialog.accept)
        btn_container.addWidget(ok_btn)
        layout.addLayout(btn_container)

        dialog.exec()

        # Save preference
        with open(pref_file, "w") as f:
            json.dump({"show_newsletter": not checkbox.isChecked()}, f)



    def ensure_podman(self, parent=None) -> bool:
        """
        Check if Podman exists. Returns True if found.
        If missing, open dialog for user to install Podman.
        """
        

        # 1️⃣ Check PATH
        podman_path = find_podman()

        # 2️⃣ On Windows, check default program files location
        if not podman_path and os.name == "nt":
            default_path = r"C:\Program Files\RedHat\Podman\podman.exe"
            if os.path.exists(default_path):
                podman_path = default_path

        if podman_path:
            print(f"✅ Podman detected: {podman_path}")
            return True  # Podman ready, skip dialog

        # 3️⃣ Podman missing — show installer dialog
        dlg = EnsurePodmanDialog(parent)
        result = dlg.exec()  # user can install Podman here
        return result == QDialog.DialogCode.Accepted
    def _normalize_field_text(self, field, as_type=float, decimals=2):
        """Normalize a QLineEdit's text to a canonical numeric string when editing finishes."""
        raw = (field.text() or "").strip()
        if raw == "":
            return  # leave empty; save will fallback
        # allow comma as decimal separator
        raw = raw.replace(",", ".")
        try:
            if as_type is int:
                val = int(float(raw))
                field.setText(str(val))
                return val
            else:
                val = round(float(raw), decimals)
                # strip trailing .0 if you prefer int-like display:
                field.setText(str(val))
                return val
        except Exception:
            # invalid -> clear to signal fallback will happen
            field.setText("")
            return None
    def load_settings(self):
        """Load settings from file, merge with system specs, update widgets."""
        sys_info = get_system_info() or {}
        # Canonical system specs
        self.system_specs = {
            "gpu": sys_info.get("gpu", "Unknown GPU"),
            "vram": sys_info.get("vram", 0),
            "cpu_cores": sys_info.get("cpu_cores", 0),
            "cpu_threads": sys_info.get("cpu_threads", 0),
            "ram_gb": sys_info.get("ram_gb", 0),
            "num_gpu": sys_info.get("num_gpu", 0),
        }

        # Load saved settings
        data = {}
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except Exception as e:
                print("[LOAD_SETTINGS] failed to read settings.json:", e)

        saved = data.get("gpu_info", {})
        # Merge saved settings with system defaults
        self.gpu_info = {k: saved.get(k, v) for k, v in self.system_specs.items()}
        self.auto_accept = bool(data.get("auto_accept", False))

        # Update widgets if they exist
        for field_name in ["vram", "cpu_cores", "cpu_threads", "ram_gb", "num_gpu"]:
            widget_name = f"{field_name}_input"
            if hasattr(self, widget_name):
                getattr(self, widget_name).setText(str(self.gpu_info.get(field_name, "")))

        if hasattr(self, "auto_accept_checkbox"):
            self.auto_accept_checkbox.setChecked(self.auto_accept)

        print("[LOAD_SETTINGS] merged gpu_info:", self.gpu_info, "auto_accept:", self.auto_accept)


    def create_settings_screen(self):
        
        """Create settings screen with inputs and validators."""
        widget = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(30, 16, 30, 16)
        layout.setSpacing(12)

        # Title at the top
        title = QLabel("System Resource Settings")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        title.setStyleSheet("color: #e6eef8;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        # Warning label
        self.settings_warning = QLabel("")
        self.settings_warning.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        self.settings_warning.setStyleSheet("color: #f87171;")
        self.settings_warning.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.settings_warning.setVisible(False)
        layout.addWidget(self.settings_warning)

        # Create inputs dynamically
        self.inputs = {}
        fields = [
            ("vram", float, 2),
            ("cpu_cores", int, 0),
            ("cpu_threads", int, 0),
            ("ram_gb", float, 2),
            ("num_gpu", int, 0),
        ]

        for name, typ, dec in fields:
            lbl = QLabel(f"{name.replace('_', ' ').title()}:")
            inp = QLineEdit(str(self.gpu_info.get(name, 0)))
            inp.editingFinished.connect(lambda inp=inp, t=typ, d=dec: self._normalize_field_text(inp, t, d))
            # Validators
            try:
                if typ is int:
                    inp.setValidator(QIntValidator(0, max(1, int(self.system_specs.get(name, 0)))))
                else:
                    inp.setValidator(QDoubleValidator(0.0, float(self.system_specs.get(name, 0)), dec))
            except Exception:
                pass
            layout.addWidget(lbl)
            layout.addWidget(inp)
            self.inputs[name] = inp

        # Auto-accept checkbox
        self.auto_accept_checkbox = QCheckBox("Auto-accept incoming GPU requests")
        self.auto_accept_checkbox.setChecked(self.auto_accept)
        layout.addWidget(self.auto_accept_checkbox)

        # Buttons
        self.save_btn = QPushButton("Save Settings")
        self.save_btn.setMinimumHeight(46)
        self.save_btn.setStyleSheet(
            "background-color: #3b82f6; color: white; border-radius: 6px; font-weight: bold;"
        )
        self.save_btn.clicked.connect(self.save_settings)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setMinimumHeight(46)
        """self.cancel_btn.setStyleSheet(
            "background-color: #9ca3af; color: white; border-radius: 6px; font-weight: bold;"
        )"""
        self.cancel_btn.clicked.connect(lambda: self.stack.setCurrentWidget(self.main_screen))

        layout.addWidget(self.save_btn)
        layout.addWidget(self.cancel_btn)

        widget.setLayout(layout)
        return widget


    def _normalize_field_text(self, field: QLineEdit, as_type=float, decimals=2):
        """Normalize QLineEdit input (float/int) after editing finishes."""
        txt = (field.text() or "").strip().replace(",", ".")
        if not txt:
            return None
        try:
            if as_type is int:
                val = int(float(txt))
            else:
                val = round(float(txt), decimals)
            field.setText(str(val))
            return val
        except Exception:
            return None


    def save_settings(self):
        """Save settings robustly."""
        if getattr(self, "is_sharing", False):
            if hasattr(self, "settings_warning"):
                self.settings_warning.setText("⚠️ Cannot change settings while sharing is active.")
                self.settings_warning.setVisible(True)
            return

        # Commit edits
        for inp in self.inputs.values():
            inp.clearFocus()
            inp.editingFinished.emit()

        # Parse & clamp values
        for name, typ, dec in [
            ("vram", float, 2),
            ("cpu_cores", int, 0),
            ("cpu_threads", int, 0),
            ("ram_gb", float, 2),
            ("num_gpu", int, 0),
        ]:
            raw = (self.inputs[name].text() or "").strip().replace(",", ".")
            try:
                val = typ(raw) if raw else typ(self.gpu_info.get(name, self.system_specs[name]))
            except Exception:
                val = typ(self.gpu_info.get(name, self.system_specs[name]))
            # Clamp to system max
            val = min(val, typ(self.system_specs.get(name, val)))
            if typ is float:
                val = round(val, dec)
            self.gpu_info[name] = val
            self.inputs[name].setText(str(val))

        self.auto_accept = bool(self.auto_accept_checkbox.isChecked())

        # Save atomically
        try:
            tmp_file = SETTINGS_FILE + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump({"gpu_info": self.gpu_info, "auto_accept": self.auto_accept}, f, indent=2)
            os.replace(tmp_file, SETTINGS_FILE)
            print("[SAVE_SETTINGS] saved:", self.gpu_info)
        except Exception as e:
            print("[SAVE_SETTINGS] write error:", e)
            if hasattr(self, "log_area"):
                self.log_area.append(f"⚠️ Error saving settings: {e}")
            return

        if hasattr(self, "log_area"):
            self.log_area.append("✅ Settings saved successfully.")

        # Return to main screen
        if hasattr(self, "stack") and hasattr(self, "main_screen"):
            self.stack.setCurrentWidget(self.main_screen)



    def validate_settings(self):
        """Save user-modified specs to gpu_info, respecting system limits, unless sharing is active."""
        try:
            # --- Block settings save if sharing ---
            if getattr(self, "is_sharing", False):
                self.settings_warning.setText("⚠ Cannot change settings while sharing is active!")
                self.settings_warning.setVisible(True)
                print("Settings update blocked: currently sharing.")
                return

            self.settings_warning.setVisible(False)  # clear warning if allowed

            system = self.gpu_info  # immutable max specs

            # Read user inputs
            vram_text = self.vram_input.text().strip().replace(",", ".")
            cpu_text = self.cpu_cores_input.text().strip()
            threads_text = self.cpu_threads_input.text().strip()
            ram_text = self.ram_gb_input.text().strip().replace(",", ".")
            gpu_text = self.num_gpu_input.text().strip()

            # Default to 1 if field is empty or invalid
            vram = min(max(float(vram_text) if vram_text else 1, 1), system['vram'])
            cpu_cores = min(max(int(cpu_text) if cpu_text else 1, 1), system['cpu_cores'])
            cpu_threads = min(max(int(threads_text) if threads_text else 1, 1), system['cpu_threads'])
            ram = min(max(float(ram_text) if ram_text else 1, 1), system['ram_gb'])
            num_gpu = min(max(int(gpu_text) if gpu_text else 0, 0), system['num_gpu'])

            # Round floats
            vram = round(vram, 2)
            ram = round(ram, 2)

            # Update text boxes
            self.vram_input.setText(str(vram))
            self.cpu_cores_input.setText(str(cpu_cores))
            self.cpu_threads_input.setText(str(cpu_threads))
            self.ram_gb_input.setText(str(ram))
            self.num_gpu_input.setText(str(num_gpu))

            # Save to gpu_info
            self.gpu_info.update({
                'vram': vram,
                'cpu_cores': cpu_cores,
                'cpu_threads': cpu_threads,
                'ram_gb': ram,
                'num_gpu': num_gpu
            })

            print(f"Settings saved and applied to gpu_info: {self.gpu_info}")

            # Close screen
            self.stack.setCurrentWidget(self.main_screen)

        except Exception as e:
            print("Unexpected error in validate_settings!")
            import traceback
            traceback.print_exc()

    def set_poll_mode(self, mode: str):
        if mode == "idle":
            self.timer.setInterval(30000)  # 30s
            self.signal_timer.stop()
        elif mode == "active":
            self.timer.setInterval(10000)  # 10s
            self.signal_timer.setInterval(2000)
            self.signal_timer.start()
        elif mode == "waiting":
            self.timer.setInterval(5000)   # 5s
            self.signal_timer.setInterval(1000)
            self.signal_timer.start()
    def poll_signalling(self):
        if not self.session_token or self._signal_poll_in_flight:
            return
        self._signal_poll_in_flight = True
        role = "sharer" if getattr(self, "is_sharing", False) else "renter"
        params = {"token": self.session_token, "role": role, "since": self._last_msg_index}
        self.run_async(server_get("/signal/poll", params), callback=self._on_signal_poll_wrapper)

    def _on_signal_poll_wrapper(self, res):
        try:
            self._on_signal_poll(res)
        finally:
            self._signal_poll_in_flight = False
        
        
    async def _async_download_and_run_podman(
        self,
        token: str,
        artifact_id: Optional[str] = None,
        filename: str = "",
        cmd: str = "",
        timeout: int = 0,
        cpu_cores: Optional[int] = None,
        ram: Optional[str] = None,  # e.g., "4g"
        gpu: Optional[str] = None   # optional GPU assignment
    ) -> Dict[str, Any]:
        """
        Download/upload prep work happens here (runs in the worker).
        The *actual* long-running run is offloaded to a dedicated background thread
        by _background_run_and_report(), which will perform run_job_in_podman(...) via asyncio.run().
        _async_download_and_run_podman returns quickly so the worker isn't blocked.
        The background thread will invoke self._on_sharer_job_done(...) on the GUI thread
        when finished.
        """
        tmp_root = None
        try:
            # 1) Download input archive
            if artifact_id:
                dl_url = f"{SERVER_BASE}/artifact/download"
                params = {"artifact_id": artifact_id}
                if not filename:
                    filename = f"{artifact_id}.zip"
            else:
                dl_url = f"{SERVER_BASE}/download"
                params = {"token": token, "filename": filename}

            # download (worker coroutine — uses async httpx)
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.get(dl_url, params=params)
                r.raise_for_status()
                data = r.content

            # 2) Write and extract to workspace (fast file ops)
            tmp_root = tempfile.mkdtemp(prefix="gpulend_sharer_")
            saved = os.path.join(tmp_root, filename)
            with open(saved, "wb") as f:
                f.write(data)

            workspace_dir = os.path.join(tmp_root, "workspace")
            os.makedirs(workspace_dir, exist_ok=True)

            extracted = False
            try:
                shutil.unpack_archive(saved, workspace_dir)
                extracted = True
            except (shutil.ReadError, ValueError):
                if zipfile.is_zipfile(saved):
                    with zipfile.ZipFile(saved, "r") as z:
                        z.extractall(workspace_dir)
                    extracted = True

            if not extracted:
                shutil.copy2(saved, os.path.join(workspace_dir, os.path.basename(saved)))

            # Normalize layout
            try:
                entries = list(Path(workspace_dir).iterdir())
                if len(entries) == 1 and entries[0].is_dir():
                    top = entries[0]
                    for child in top.iterdir():
                        shutil.move(str(child), workspace_dir)
                    try:
                        top.rmdir()
                    except Exception:
                        pass
            except Exception as e:
                # non-fatal
                self.log_area.append(f"⚠️ Workspace normalization warning: {e}")

            if not any(Path(workspace_dir).iterdir()):
                # nothing to run
                # cleanup
                try:
                    shutil.rmtree(tmp_root)
                except Exception:
                    pass
                return {"ok": False, "error": "workspace empty after extraction"}

            # 3) Offload long-running run to a dedicated background thread.
            #    Pass a copy of relevant parameters (workspace_dir, cmd, token, cpu_cores, ram, gpu)
            #    The thread will:
            #      - run run_job_in_podman via asyncio.run(...)
            #      - upload artifact (sync httpx)
            #      - post output/done signals (sync httpx)
            #      - schedule GUI callback self._on_sharer_job_done(result) via QTimer.singleShot
            thread = threading.Thread(
                target=lambda: self._background_run_and_report(token, workspace_dir, cmd, cpu_cores, ram, gpu, tmp_root,timeout),
                daemon=True
            )
            thread.start()

            # Return quickly so worker is free to handle polling etc.
            return {"ok": True, "started": True}

        except Exception as e:
            # cleanup
            try:
                if tmp_root and os.path.exists(tmp_root):
                    shutil.rmtree(tmp_root)
            except Exception:
                pass
            return {"ok": False, "error": str(e)}
    
    def _background_run_and_report(self, token: str, workspace_dir: str, cmd: str,
                                   cpu_cores: Optional[int], ram: Optional[str], gpu: Optional[str],
                                   tmp_root: Optional[str],timeout: Optional[int]):
        """
        Runs in a separate thread. Uses asyncio.run to execute the async run_job_in_podman coroutine,
        then performs artifact upload and signalling synchronously, and finally posts
        the final result back to the GUI thread by calling self._on_sharer_(...) via QTimer.singleShot.
        """
        final_result: Dict[str, Any] = {"ok": False}
        job_result = None
        artifact_info = None
        
        if timeout == None:
            timeout = 600
        try:
            # Import the async runner and execute it inside a fresh event loop (asyncio.run)
            try:
                from podman_runner import run_job_in_podman  # async func
            except Exception as e:
                final_result = {"ok": False, "error": f"podman runner import error: {e}"}
                # schedule callback on GUI thread
                QTimer.singleShot(0, lambda: self._on_sharer_job_done(final_result))
                return
            
            # Run the async runner in its own event loop so we don't block the worker thread.
            try:
                # Note: run_job_in_podman is async; run it inside this thread's event loop:
                job_result = asyncio.run(
                    run_job_in_podman(
                        job_folder=workspace_dir,
                        cmd=cmd,
                        image="python:3.12-slim",
                        timeout=timeout,
                        keep_dirs=True,
                        cpu_cores=cpu_cores,
                        ram=ram,
                        gpu=gpu
                    )
                )
            except Exception as e:
                job_result = {"ok": False, "error": f"run_job exception: {e}"}

            # If job_result produced a workspace zip, attempt synchronous upload using httpx.Client
            try:
                workspace_zip = job_result.get("workspace_zip") if isinstance(job_result, dict) else None
                if workspace_zip and os.path.exists(workspace_zip):
                    zip_name = os.path.basename(workspace_zip)
                    uploaded_ok = False
                    new_artifact_id = None
                    upload_err = None
                    try:
                        with httpx.Client(timeout=120.0) as client:
                            data = {"token": token, "role": "sharer"}
                            files = {"file": (zip_name, open(workspace_zip, "rb"), "application/zip")}
                            r = client.post(f"{SERVER_BASE}/upload", data=data, files=files)
                            r.raise_for_status()
                            j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                            uploaded_ok = j.get("ok", True)
                            new_artifact_id = j.get("artifact_id")
                    except Exception as e:
                        upload_err = str(e)
                        # Note: we can't append to GUI log_area here safely (not GUI thread).
                        uploaded_ok = False

                    artifact_info = {
                        "filename": zip_name,
                        "size": os.path.getsize(workspace_zip) if os.path.exists(workspace_zip) else None,
                        "uploaded": bool(uploaded_ok),
                        "artifact_id": new_artifact_id,
                        "error": upload_err
                    }
                else:
                    artifact_info = {"uploaded": False, "artifact_id": None, "error": "no artifact produced"}
            except Exception as e:
                artifact_info = {"uploaded": False, "artifact_id": None, "error": f"artifact handling: {e}"}

            # 5) Send 'output' logs to sharer (sync)
            try:
                if isinstance(job_result, dict) and job_result.get("ok"):
                    exit_code = job_result.get("exit_code", -1)
                    logs = job_result.get("logs", {}) or {}
                    stdout = logs.get("stdout", "")
                    stderr = logs.get("stderr", "")
                    out_payload = {
                        "flag": "output",
                        "status": "done" if exit_code == 0 else "failed",
                        "message": f"exit={exit_code}\nstdout:\n{stdout}\nstderr:\n{stderr}"
                    }
                else:
                    out_payload = {
                        "flag": "output",
                        "status": "failed",
                        "message": f"runner error: {job_result.get('error') if isinstance(job_result, dict) else str(job_result)}"
                    }

                with httpx.Client(timeout=10.0) as client:
                    client.post(f"{SERVER_BASE}/signal/message", json={"token": token, "role": "sharer", "payload": out_payload})
            except Exception:
                # ignore network failures here (we'll still deliver final done below)
                pass

            # 6) Send final 'done' only to renter (sync)
            try:
                done_payload = {
                    "flag": "done",
                    "status": out_payload.get("status", "failed"),
                    "artifact": artifact_info,
                    "message": f"exit={exit_code}\nstdout:\n{stdout}\nstderr:\n{stderr}"
                }
                with httpx.Client(timeout=10.0) as client:
                    client.post(f"{SERVER_BASE}/signal/message", json={"token": token, "role": "renter", "payload": done_payload})
            except Exception:
                pass

            # Build final_result for GUI callback
            final_result = {
                "ok": True,
                "job_result": job_result,
                "artifact": artifact_info,
                "message": f"exit={exit_code}\nstdout:\n{stdout}\nstderr:\n{stderr}"
            }

        except Exception as e:
            final_result = {"ok": False, "error": str(e)}
        finally:
            # Attempt best-effort cleanup of tmp_root: only remove if exists and not needed by keep_dirs
            try:
                if tmp_root and os.path.exists(tmp_root):
                    # run_job_in_podman may have returned a workspace reference; we only remove if safe
                    shutil.rmtree(tmp_root, ignore_errors=True)
            except Exception:
                pass

            # Schedule GUI callback on the GUI thread so _on_sharer_job_done runs safely (uses GUI objects)
            try:
                QTimer.singleShot(0, lambda res=final_result: self._on_sharer_job_done(res))
            except Exception:
                # as fallback, call directly (risky if not GUI thread) — but we try QTimer first
                try:
                    self._on_sharer_job_done(final_result)
                except Exception:
                    pass

    async def _async_download_artifact(self, artifact_id: str, dest_path: str) -> Dict[str, Any]:
        """
        Stream-download an artifact from the server to dest_path (async) using artifact_id.
        """
        url = f"{SERVER_BASE}/artifact/download"
        params = {"artifact_id": artifact_id}
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.get(url, params=params)
                r.raise_for_status()
                # write in binary
                with open(dest_path, "wb") as fh:
                    fh.write(r.content)
            return {"ok": True, "path": dest_path}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def _on_artifact_downloaded(self, res: Dict[str, Any], dest_path: str):
        if not isinstance(res, dict):
            self.log_area.append("⚠️ Artifact download callback returned unexpected result.")
            return

        if not res.get("ok"):
            self.styled_message_box("Download failed", f"Could not download artifact: {res.get('error')}", QMessageBox.Icon.Critical).exec()
            self.log_area.append(f"⚠️ Artifact download failed: {res.get('error')}")
            return

        self.log_area.append(f"✅ Artifact saved to: {dest_path}")
        box = self.styled_message_box("Download complete", f"Saved to: {dest_path}\nOpen file now?", QMessageBox.Icon.Question)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        ans = box.exec()
        if ans == QMessageBox.StandardButton.Yes:
            QDesktopServices.openUrl(QUrl.fromLocalFile(dest_path))
    async def _async_download_artifact_by_id(self, artifact_id: str, dest_path: str) -> Dict[str, Any]:
        url = f"{SERVER_BASE}/artifact/download"
        params = {"artifact_id": artifact_id}
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.get(url, params=params)
                r.raise_for_status()
                with open(dest_path, "wb") as fh:
                    fh.write(r.content)
            return {"ok": True, "path": dest_path}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    def show_job_output_dialog(self, output: str, artifact: Optional[Dict[str, Any]]):
        """
        Show dialog offering to download artifact (if uploaded) or open downloads folder,
        and display job output logs nicely with centered buttons below.
        """
        if not artifact:
            return

        artifact_id = artifact.get("artifact_id")
        filename = artifact.get("filename")
        uploaded = artifact.get("uploaded", False)
        size = artifact.get("size", None)
        err = artifact.get("error")

        # --- Create job info header ---
        header_text = f"<b>Job produced:</b> {filename}<br>"
        if size:
            header_text += f"<b>Size:</b> {size} bytes<br>"
        if err:
            header_text += f"<b>Artifact error:</b> {err}<br>"
        header_text += f"<b>Uploaded to server:</b> {'✅ Yes' if uploaded else '❌ No'}<br>"

        # Create the QMessageBox
        msg = self.styled_message_box("Job output available", "", QMessageBox.Icon.Information)

        # --- Hide Ubuntu info icon (clean fix) ---
        
        for child in msg.findChildren(QLabel):
            if child.pixmap():  # QLabel showing the icon
                child.clear()
                child.setFixedSize(0, 0)
                break
        

        # --- Build a custom container layout ---
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Header info (top)
        header_label = QLabel(header_text)
        header_label.setTextFormat(Qt.TextFormat.RichText)
        header_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(header_label)

        # Scrollable log viewer
        text_widget = QTextEdit()
        text_widget.setReadOnly(True)
        text_widget.setPlainText(output)
        text_widget.setMinimumSize(600, 400)
        text_widget.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        text_widget.setStyleSheet("font-family: Consolas, monospace; background-color: #071026; color: #E0E0E0;")
        layout.addWidget(text_widget)

        # Buttons (centered)
        button_container = QWidget()
        button_layout = QHBoxLayout(button_container)
        button_layout.setContentsMargins(0, 10, 0, 0)
        button_layout.setSpacing(20)
        button_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(button_container)

        # Add the custom layout to the message box
        msg.layout().addWidget(container, 0, 0, 1, msg.layout().columnCount())

        # --- Buttons ---
        download_btn = msg.addButton("Download", QMessageBox.ButtonRole.AcceptRole)
        open_btn = msg.addButton("Open folder", QMessageBox.ButtonRole.ActionRole)
        cancel_btn = msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)

        # Add buttons to the centered layout
        button_layout.addWidget(download_btn)
        button_layout.addWidget(open_btn)
        button_layout.addWidget(cancel_btn)

        # Execute dialog
        msg.exec()

        # --- Artifact handling (unchanged) ---
        clicked = msg.clickedButton()
        if clicked == download_btn and uploaded and artifact_id:
            dest_path, _ = QFileDialog.getSaveFileName(self, "Save job output as...", filename, "Zip files (*.zip);;All files (*)")
            if dest_path:
                self.run_async(
                    self._async_download_artifact_by_id(artifact_id, dest_path),
                    callback=lambda res: self._on_artifact_downloaded(res, dest_path)
                )
        elif clicked == download_btn and not uploaded:
            self.styled_message_box("Not available", "Sharer did not upload artifact to server.", QMessageBox.Icon.Warning).exec()
        elif clicked == open_btn:
            dl = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
            if dl:
                QDesktopServices.openUrl(QUrl.fromLocalFile(dl))
            else:
                self.styled_message_box("Open folder", "Cannot determine Downloads folder on this platform.", QMessageBox.Icon.Warning).exec()





    def _on_signal_poll(self, res: Dict[str, Any]):
        if not res:
            return
        if res and res.get("messages"):
            self._empty_poll_count = 0
        else:
            self._empty_poll_count += 1
        base = 1000  # 1s base for signal_timer (ms)
        new_interval = min(10000, base * (2 ** min(self._empty_poll_count, 4)))  # cap at 10s
        # add jitter
        jitter = random.randint(-200, 200)
        self.signal_timer.setInterval(max(100, new_interval + jitter))

        # Handle session ended regardless of ok
        if res.get("session_ended"):
            if not getattr(self, "_session_ended_handled", False):
                self._session_ended_handled = True

                if getattr(self, "is_sharing", False):
                    # Sharer side cleanup
                    self.status_label.setText("Sharing")
                    self.current_renter = None
                else:
                    # Renter side cleanup
                    self.is_renting = False
                    self.rent_btn.setText("Rent GPU")
                    self.status_label.setText("Idle" if not self.is_sharing else "Sharing")
                    if self.signal_timer.isActive():
                        self.signal_timer.stop()

                self.session_token = None
                self._last_msg_index = 0
                self.log_area.append("ℹ️ Session ended.")

            # If session is ended and server already said ok==False, nothing more to do
            if not res.get("ok"):
                return

        # reset flag if we're continuing with a new session
        if getattr(self, "_session_ended_handled", False) and not res.get("session_ended"):
            self._session_ended_handled = False
        msgs = res.get("messages", [])
        next_index = res.get("next_msg_index", self._last_msg_index)

        local_role = "sharer" if getattr(self, "is_sharing", False) else "renter"

        msgs = res.get("messages", [])
        next_index = res.get("next_msg_index", self._last_msg_index)

        for m in msgs:
            idx = m.get("index", 0)
            payload = m.get("payload", {}) or {}
            sender = m.get("from")  # this is 'sharer' or 'renter' as the sender role recorded by server

            # skip messages we've already seen
            if idx < self._last_msg_index:
                continue
            # advance last seen index right away (prevents re-processing if a handler triggers more polling)
            self._last_msg_index = max(self._last_msg_index, idx + 1)

            # IGNORE messages authored by ourselves (server echoes everything)
            if sender == local_role:
                continue

            # now process only messages from the *other* role
            flag = payload.get("flag")
            if flag == "begin":
                fname = payload.get("filename")
                cmd = payload.get("cmd", "")
                max_time=payload.get("max_time",0)
                artifact_id = payload.get("artifact_id")  # may be None for legacy clients
                self.log_area.append(f"📥 Received begin: {fname} — cmd: {cmd} — artifact_id: {artifact_id!r}")

                if getattr(self, "is_sharing", False):
                    token = self.session_token
                    # Choose the correct async function call depending on artifact_id presence
                    if artifact_id:
                        # schedule download+run in worker (this returns quickly — actual run happens in background thread)
                        self.run_async(self._async_download_and_run_podman(token, artifact_id, fname, cmd,timeout=max_time))
                    else:
                        # legacy flow
                        self.run_async(self._async_download_and_run_podman_legacy(token, fname, cmd,timeout=max_time))

            # 'output' -> intermediate logs (renter sees logs)
            elif flag == "output":
                status = payload.get("status", "")
                message = payload.get("message", "")
                self.log_area.append(f"🔔 Job output — {status}: {message}")
                if getattr(self, "is_renting", False):
                    # just show logs; renter handles finalization on 'done'
                    self._handle_renter_job_output(payload)

            # 'done' -> final + artifact metadata
            elif flag == "done":
                artifact = payload.get("artifact")
                status = payload.get("status", "")
                message = payload.get("message", "")
                self.log_area.append(f"🔔 Job finished — {status} (done payload received)")
                self.log_area.append(f"🔍 done payload artifact: {artifact!r}")

                # Renter-side finalization: close out current job, offer download, then continue queue or finish renting
                if not getattr(self, "is_sharing", False):
                    # show artifact dialog if uploaded (non-blocking UI)
                    if artifact and artifact.get("uploaded"):
                        self.show_job_output_dialog(message,artifact)

                    # mark current job completed
                    if self.current_job:
                        self.log_area.append(f"✅ Remote job finished: {self.current_job.name} — {status}")
                        self.current_job = None
                        self.update_queue_list()

                    # next job or finish renting
                    if self.job_queue:
                        QTimer.singleShot(200, self.upload_current_job)
                    else:
                        self.log_area.append("🛑 All remote jobs finished — rent session complete.")
                        self.is_renting = False
                        self.rent_btn.setText("Rent GPU")

                        # keep server session alive until server decides to close it,
                        # but stop local signalling/polling so we don't get pruned.
                        # IMPORTANT: do NOT clear self.session_token here — server will close session,
                        # and the client will learn about it via signal_poll returning session_ended.
                        self._last_msg_index = 0
                        self.status_label.setText("Idle" if not self.is_sharing else "Sharing")
                        if self.signal_timer.isActive():
                            self.signal_timer.stop()

                    self.update_indicators()

        self._last_msg_index = max(self._last_msg_index, next_index)

    def _handle_renter_job_output(self, payload: Dict[str, Any]):
        """
        Called when sharer sent an 'output' message. Treat as log/update only.
        Finalization (clearing current_job, continuing queue or finishing the session)
        must happen only on 'done'.
        """
        status = payload.get("status", "")
        message = payload.get("message", "")

        # Present logs to user
        if self.current_job:
            self.log_area.append(f"🔔 Job output — {status}: {message}")
            # Optionally reflect intermediate status on the job entry:
            self.current_job.status = f"Remote: {status}"
        else:
            # No current job tracked (still show log)
            self.log_area.append(f"⚠️ Received job output but no current job tracked — {status}: {message}")

        # update UI representation but do not finalize anything
        self.update_queue_list()
    # ---- add this callback (place near other async callbacks) ----
    def _on_sharer_job_done(self, res: Dict[str, Any]):
        if not isinstance(res, dict):
            self.log_area.append("⚠️ Sharer job callback got unexpected result.")
            return

        if not res.get("ok"):
            self.log_area.append(f"⚠️ Sharer job failed: {res.get('error')}")
            return

        job_result = res.get("job_result", {})
        artifact = res.get("artifact")
        exit_code = job_result.get("exit_code", -1)
        logs = job_result.get("logs", {})
        stdout = logs.get("stdout", "")
        stderr = logs.get("stderr", "")

        self.log_area.append(f"✅ Sharer job finished — exit={exit_code}")
        if stdout:
            self.log_area.append(f"--- stdout (truncated) ---\n{stdout[:400]}...\n")
        if stderr:
            self.log_area.append(f"--- stderr (truncated) ---\n{stderr[:400]}...\n")

        # optionally close session on server if desired
        if self.session_token:
            self.run_async(server_post("/end_session", {"token": self.session_token}), callback=lambda r: self.log_area.append("ℹ️ Session closed on server."))
        self.session_token = None
        self.current_renter = None
        self._last_msg_index = 0
        self.log_area.append("ℹ️ Back to idle sharing mode.")
        

    def _after_session_closed(self, res):
        """Cleanup after session is closed on server."""
        if res and res.get("ok"):
            self.log_area.append("ℹ️ Session ended, back to idle sharing mode.")
        else:
            self.log_area.append("⚠️ Failed to close session cleanly.")

        # reset local state
        self.session_token = None
        self.current_renter = None
        self._last_msg_index = 0
        self.is_sharing = True   # still available for new renters

    async def _async_download_and_run(self, artifact_id: str, dest_filename: str, cmd: str):
        """
        Download an artifact by ID, extract to temp, run job, send output/done messages.
        """
        # 1) Download artifact using new server endpoint
        dl_url = f"{SERVER_BASE}/artifact/download"
        params = {"artifact_id": artifact_id}

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.get(dl_url, params=params)
                r.raise_for_status()
                data = r.content
        except Exception as e:
            return {"ok": False, "error": f"download failed: {e}"}

        # 2) Save to temp dir
        tmpd = tempfile.mkdtemp(prefix="gpulend_sharer_")
        saved_path = os.path.join(tmpd, dest_filename)
        with open(saved_path, "wb") as f:
            f.write(data)

        # 3) Try to extract if zip
        try:
            shutil.unpack_archive(saved_path, tmpd)
        except Exception:
            # not a zip, ignore
            pass

        # 4) Simulate running job
        await asyncio.sleep(2.0)  # replace with real Podman/container runner

        # 5) Notify renter about output
        payload = {"flag": "output", "status": "done", "message": f"Ran {dest_filename}: simulated OK"}
        notice = await server_post("/signal/message", {"token": artifact_id, "role": "sharer", "payload": payload}, timeout=5.0)

        # 6) Notify renter that job is done
        done_payload = {"flag": "done", "status": payload.get("status", "failed"), "artifact": {"artifact_id": artifact_id}}
        await server_post("/signal/message", {"token": artifact_id, "role": "sharer", "payload": done_payload}, timeout=5.0)

        return {"ok": True, "notice": notice}
    # ---- async bridge ----
    def _handle_async_result(self, result, callback):
        if callback:
            callback(result)

    def run_async(self, coro, callback=None):
        self.worker.add_task(coro, callback)

    # ---- offline UI helper ----
    def set_offline(self, is_offline: bool, reason: str = None):
        """Update UI to show server offline/online status."""
        # offline_icon may be created after init, guard with hasattr
        if is_offline:
            self.is_offline=True
            if hasattr(self, "offline_icon"):
                self.offline_icon.setText("⛔ OFFLINE")
                self.offline_icon.setVisible(True)
            if hasattr(self, "status_label"):
                self.status_label.setText("Offline")
                self.status_label.setStyleSheet("color: #ff6b6b; padding: 6px;")
            if reason and hasattr(self, "log_area"):
                self.log_area.append(f"⚠️ Server error/offline: {reason}")
        else:
            self.is_offline=False
            if hasattr(self, "offline_icon"):
                self.offline_icon.setVisible(False)
            if hasattr(self, "status_label"):
                if self.is_sharing:
                    self.status_label.setText("Sharing")
                    # more frequent updates while sharing
                    self.timer.setInterval(5000)         # 5s
                    self.signal_timer.setInterval(1000)  # 1s
                    if not self.signal_timer.isActive():
                        self.signal_timer.start()

                elif self.people_renting > 0:
                    self.status_label.setText("Renting")
                    # renter in session → keep responsive
                    self.timer.setInterval(5000)         # 5s
                    self.signal_timer.setInterval(1000)  # 1s
                    if not self.signal_timer.isActive():
                        self.signal_timer.start()

                else:
                    self.status_label.setText("Idle")
                    # back off polling when idle
                    self.timer.setInterval(30000)        # 30s
                    self.signal_timer.stop()

                self.status_label.setStyleSheet("color: #bcd2ff; padding: 6px;")
        if hasattr(self, "settings_btn"):
            self.settings_btn.setVisible(not self.is_offline)

    # ---- server polling / callbacks ----
    def poll_counts(self):
        # counts endpoint is public
        self.run_async(server_get("/counts"), callback=self._on_counts)

    def _on_counts(self, res):
        # tolerant parsing: server may or may not include "ok"
        if res is None:
            return
        if not res.get("ok", True) and "sharing" not in res and "renting" not in res:
            return
        try:
            sharing = int(res.get("sharing", 0))
            renting = int(res.get("renting", 0))
        except Exception:
            return
        self.people_sharing = sharing
        self.people_renting = renting

        # keep the rent button consistent
        if getattr(self, "is_renting", False):
            self.rent_btn.setText("Stop Rent")
        elif self._waiting_for_sharer:
            self.rent_btn.setText("Cancel Request")
        else:
            self.rent_btn.setText("Rent GPU")
        self.update_indicators()

    def poll_server_for_pending(self):
        if not self.current_user or not self.is_sharing:
            return
        username = self.current_user["username"]
        self.run_async(server_get("/poll", {"username": username}), callback=lambda r, u=username: self._on_poll(r, u))

    def _on_poll(self, res, username):
        if not res.get("ok"):
            self.set_offline(True, reason=res.get("error"))
            return
        self.set_offline(False)

        pend = res.get("pending")
        if not pend:
            return

        state = pend.get("state")
        renter = pend.get("renter")

        if state == "pending":
            # don’t show again if we already asked for this renter
            if self.current_request == renter:
                return

            self.current_request = renter
            self.log_area.append(f"🔔 Incoming request from {renter} to use your GPU.")

            msg = self.styled_message_box(
                "Incoming request",
                f"{renter} wants to use your GPU. Accept?",
                QMessageBox.Icon.Question
            )
            msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            ans = msg.exec()

            if ans == QMessageBox.StandardButton.Yes:
                self.run_async(
                    server_post("/respond", {"sharer": username, "accept": True}),
                    callback=lambda r, rent=renter: self._on_respond_accept(r, rent)
                )
            else:
                self.run_async(server_post("/respond", {"sharer": username, "accept": False}))
                self.log_area.append("⛔ You rejected the incoming request.")

            # clear once answered
            self.current_request = None

        elif state == "accepted":
            self.current_request = None
            self.log_area.append(f"🔒 Match established with {renter}.")


    def poll_renter_pending(self):
        if not self.current_user or not self._waiting_for_sharer:
            return
        self.run_async(server_get("/pending_for", {"renter": self.current_user["username"]}), callback=self._on_renter_pending)

    def _on_renter_pending(self, res):
        if not res.get("ok"):
            self.log_area.append(f"⚠️ Pending-check failed: {res.get('error')}")
            return

        pend = res.get("pending")
        if pend is None:
            if self._waiting_for_sharer:
                self._waiting_for_sharer = False
                self._pending_sharer = None
                if self.renter_poll_timer.isActive():
                    self.renter_poll_timer.stop()

                # renter gave up → go idle mode
                self.status_label.setText("Idle")
                self.timer.setInterval(30000)        # 30s slow poll
                self.signal_timer.stop()

                self.log_area.append("⛔ Sharer rejected or request timed out.")
                self.rent_btn.setText("Rent GPU")
            return

        # renter still waiting → keep high-frequency polls
        if not self.renter_poll_timer.isActive():
            self.renter_poll_timer.start()
        self.renter_poll_timer.setInterval(2000)     # 2s while waiting
        self.status_label.setText("Waiting for Sharer")
        self.timer.setInterval(5000)                 # 5s main poll
        self.signal_timer.setInterval(1000)          # 1s signalling
        if not self.signal_timer.isActive():
            self.signal_timer.start()

        state = pend.get("state")
        sharer = pend.get("sharer")
        gpu_name = pend.get("gpu") or pend.get("tier")

        if state == "pending":
            self.status_label.setText(f"Waiting for {sharer}...")
            return

        if state == "accepted":
            # server created a session and deducted credit; refresh credit info
            self._waiting_for_sharer = False
            if self.renter_poll_timer.isActive():
                self.renter_poll_timer.stop()
            self._pending_sharer = None

            self.run_async(
                server_post("/credits", {"username": self.current_user["username"], "pwd": self.current_user["pwd"]}),
                callback=self._on_credits_after_accept
            )

            self.is_renting = True
            self.rent_btn.setText("Stop Rent")
            self.people_renting += 1

            # Capture session token (server returns it when session is accepted)
            token = pend.get("token") or res.get("token")
            if token:
                self.session_token = token
                self._last_msg_index = 0
                if not self.signal_timer.isActive():
                    self.signal_timer.start()

                # if we have queued jobs, upload the first one immediately
                if self.job_queue:
                    # upload in background, async
                    self.upload_current_job()
            
            self.status_label.setText(f"Renting {gpu_name}")
            self.log_area.append(f"🔒 Sharer {sharer} accepted — renting started ({gpu_name}).")
            self.update_indicators()
            return

        self.log_area.append(f"⚠️ Unexpected pending state: {state}")
    def upload_current_job(self):
        """Zip the first job folder and upload it to the server for the current session."""
        if not self.session_token:
            self.log_area.append("⚠️ No active session token for upload.")
            return
        if not self.job_queue:
            self.log_area.append("⚠️ No job to upload.")
            return

        # pop next job and mark it as current so we can track it while waiting for sharer output
        job = self.job_queue.pop(0)
        self.current_job = job
        job.status = "Uploading..."
        self.update_queue_list()

        # zip to temp file
        tmpdir = tempfile.mkdtemp(prefix="gpulend_")
        zip_base = os.path.join(tmpdir, "job_upload")
        zip_path = shutil.make_archive(zip_base, 'zip', job.folder)  # creates job_upload.zip
        filename = os.path.basename(zip_path)  # e.g. job_upload.zip

        # schedule async upload
        self.log_area.append(f"⬆️ Uploading job '{job.name}' ({filename})...")
        self.run_async(self._async_upload_file(self.session_token, filename, zip_path, job.command),
                       callback=lambda res, j=job: self._on_uploaded(res, j))


    async def _async_upload_file(self, token: str, filename: str, path: str, command: str) -> Dict[str, Any]:
        url = SERVER_BASE + "/upload"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                data = {"token": token, "role": "renter"}
                with open(path, "rb") as fh:
                    files = {"file": (filename, fh, "application/zip")}
                    resp = await client.post(url, data=data, files=files)
                resp.raise_for_status()
                j = resp.json() if resp.headers.get("content-type","").startswith("application/json") else {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"upload failed: {e}"}

        # get artifact_id if present
        artifact_id = j.get("artifact_id")

        # send begin message to sharer with filename + command + artifact_id
        payload = {"flag": "begin", "filename": filename, "cmd": command,"max_time":self.current_max_time*60}
        if artifact_id:
            payload["artifact_id"] = artifact_id

        try:
            notice = await server_post("/signal/message", {"token": token, "role": "renter", "payload": payload}, timeout=5.0)
        except Exception as e:
            notice = {"ok": False, "error": f"signal send failed: {e}"}

        return {"ok": True, "upload_resp": j, "notice": notice}
    def _on_uploaded(self, res, job: Job):
        if not res.get("ok"):
            self.log_area.append(f"⚠️ Upload failed: {res.get('error')}")
            # requeue job for retry (put at front)
            self.job_queue.insert(0, job)
            # clear current_job because upload didn't succeed
            if self.current_job is job:
                self.current_job = None
            self.update_queue_list()
            return

        # upload succeeded — mark the job as running on the sharer and wait for the 'output' signal
        self.log_area.append(f"✅ Upload complete: {job.name}. Notified sharer.")
        if self.current_job is job:
            self.current_job.status = "Running on sharer"
        self.update_queue_list()
        # nothing else to do here — we'll react when the sharer sends an 'output' message

    # ---- credits handling ----
    def poll_credits(self):
        if not self.current_user:
            return
        data = {"username": self.current_user["username"], "pwd": self.current_user["pwd"]}
        self.run_async(server_post("/credits", data), callback=self._on_credits_update_single)

    def _on_credits_update_single(self, res):
        last_credits = getattr(self, "credits", 0.0)
        if not res.get("ok"):
            self.log_area.append(f"⚠️ credits fetch failed: {res.get('error')}")
            return

        # normalize to 2 decimals immediately
        credits = round(float(res.get("credits", 0)), 2)
        last_credits = round(float(last_credits), 2)

        # update internal state
        self.credits = credits
        self.credits_label.setText(f"Credits: {credits:.2f}")

        # diff calculation also normalized
        diff = round(credits - last_credits, 2)
        if abs(diff) < 0.01:  # guard tiny float noise
            return

        if diff > 0:
            if self.is_sharing:
                self.log_area.append(f"✅ Received {diff:.2f} credits for sharing GPU")
            else:
                self.log_area.append(f"✅ Received {diff:.2f} credits as refund")
        elif diff < 0:
            self.log_area.append(f"✅ Used {abs(diff):.2f} credits to rent GPU")

    def _on_credits_after_accept(self, res):
        if not res.get("ok"):
            self.log_area.append(f"⚠️ Could not refresh credits after accept: {res.get('error')}")
            return

        # normalize to 2 decimals
        new_credits = round(float(res.get("credits", self.credits)), 2)
        delta = round(new_credits - self.credits, 2)

        self.credits = new_credits
        self.credits_label.setText(f"Credits: {self.credits:.2f}")

        if abs(delta) >= 0.01:
            sign = "+" if delta >= 0 else ""
            self.log_area.append(f"✅ Credits updated ({sign}{delta:.2f}) after rent start.")
    # ---- register / login ----
    def handle_register(self):
        username = self.reg_user.text().strip()
        pwd = self.reg_pwd.text().strip()
        credits = 0
        try:
            credits = int(credits) if credits else 0
        except Exception:
            credits = 0
        if not username or not pwd:
            self.styled_message_box("Register failed", "Username + password required.", QMessageBox.Icon.Warning).exec()
            return

        self.reg_btn.setEnabled(False)
        self.reg_btn.setText("Registering...")

        self.run_async(
            server_post("/register", {"username": username, "pwd": pwd, "credits": credits}),
            callback=lambda res, u=username, p=pwd, c=credits: self._on_register(res, u, p, c)
        )

    def _on_register(self, res, username, pwd, credits):
        self.reg_btn.setEnabled(True)
        self.reg_btn.setText("Register")
        if not res.get("ok"):
            self.set_offline(True, reason=res.get("error"))
            self.styled_message_box("Register failed", str(res.get("error", res)), QMessageBox.Icon.Critical).exec()
            return

        self.set_offline(False)
        self.current_user = {"username": username, "pwd": pwd}
        self.credits = credits
        self.credits_label.setText(f"Credits: {self.credits}")
        self.user_label.setText(username)
        self.log_area.append(f"🔐 Registered and logged in as {username}")

        QTimer.singleShot(50, lambda: self.stack.setCurrentWidget(self.main_screen))

    def handle_login(self):
        username = self.login_user.text().strip()
        pwd = self.login_pwd.text().strip()
        if not username or not pwd:
            self.styled_message_box("Login failed", "Username + password required.", QMessageBox.Icon.Warning).exec()
            return

        self.login_btn.setEnabled(False)
        self.login_btn.setText("Logging in...")

        self.run_async(
            server_post("/login", {"username": username, "pwd": pwd}),
            callback=lambda res, u=username, p=pwd: self._on_login(res, u, p)
        )

    def _on_login(self, res, username, pwd):
        self.login_btn.setEnabled(True)
        self.login_btn.setText("Login")
        self.set_offline(False)
        if not res.get("ok"):
            self.styled_message_box("Login failed", "User not found or wrong password.", QMessageBox.Icon.Critical).exec()
            return

        self.current_user = {"username": username, "pwd": pwd}
        self.credits = res.get("credits", 0)
        self.credits_label.setText(f"Credits: {self.credits}")
        self.user_label.setText(username)
        self.log_area.append(f"🔐 Logged in as {username}")
        self.stack.setCurrentWidget(self.main_screen)

    # ---- jobs UI & logic ----
    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select job folder")
        if folder:
            self.uploaded_folder = folder
            self.folder_preview.setText(folder)
            self.log_area.append(f"📁 Selected folder: {folder}")

    def preset_selected(self, idx):
        if idx <= 0:
            return
        preset = self.preset_box.currentText()
        self.cmd_input.setPlainText(preset)

    def validate_job(self):
        if not self.uploaded_folder or not self.cmd_input.toPlainText().strip():
            self.log_area.append("⚠️ Please choose a folder and enter a command.")
            return
        name = self.name_input.text().strip()
        cmd_text = self.cmd_input.toPlainText().strip()
        if not name:
            first_line = cmd_text.splitlines()[0]
            name = first_line if len(first_line) <= 40 else first_line[:40] + "..."
        priority = self.priority_box.currentText()
        job = Job(name=name, folder=self.uploaded_folder, command=cmd_text, priority=priority)
        self.job_queue.append(job)
        self.log_area.append(f"✅ Job added: \"{job.name}\" (priority: {job.priority})")
        self.uploaded_folder = None
        self.folder_preview.setText("No folder selected")
        self.name_input.clear()
        self.cmd_input.clear()
        self.preset_box.setCurrentIndex(0)
        self.priority_box.setCurrentText("Medium")
        self.update_queue_list()
        self.stack.setCurrentWidget(self.main_screen)

    def delete_job_item(self, item: QListWidgetItem):
        idx = self.queue_list.row(item)
        if 0 <= idx < len(self.job_queue):
            removed = self.job_queue.pop(idx)
            self.log_area.append(f"❌ Job removed: {removed.name}")
            self.update_queue_list()

    def update_queue_list(self):
        self.queue_list.clear()
        if self.current_job:
            self.queue_list.addItem(f"[RUNNING] {self.current_job.name} [{self.current_job.priority}] — {self.current_job.status}")
        for job in self.job_queue:
            preview = job.command.splitlines()[0]
            if len(preview) > 80:
                preview = preview[:77] + "..."
            self.queue_list.addItem(f"{job.name} [{job.priority}] — {preview}")

    # ---- sharing / renting UI & logic ----
    def toggle_share_gpu(self):
        if not self.current_user:
            self.styled_message_box("Not logged", "Please register/login first.", QMessageBox.Icon.Warning).exec()
            return

        if not self.is_sharing:
            # start sharing
            self.is_sharing = True
            self.people_sharing += 1
            self.get_btn.setText("Stop Sharing")
            self.status_label.setText("Sharing")
            self.log_area.append("🔌 You started sharing your GPU.")

            gpu_info = self.gpu_info
            self.log_area.append(f"📊 Detected GPU: {gpu_info['gpu']} ({gpu_info['vram']} GB VRAM)")

            payload = {
                "username": self.current_user["username"],
                "pwd": self.current_user["pwd"],
                "status": "sharing",
                "gpu": gpu_info["gpu"],
                "vram": int(gpu_info["vram"]),
                "num_gpu": int(gpu_info["num_gpu"]),
                "cpu_cores": int(gpu_info["cpu_cores"]),
                "cpu_threads": int(gpu_info["cpu_threads"]),
                "ram_gb": int(gpu_info["ram_gb"]),
            }
            self.run_async(server_post("/update_status", payload))

            self.timer.start(10000)
    
        else:
            self.is_sharing = False
            self.people_sharing = max(0, self.people_sharing - 1)
            self.get_btn.setText("Share GPU")

            self.run_async(server_post("/update_status", {
                "username": self.current_user["username"],
                "pwd": self.current_user["pwd"],
                "status": "idle"
            }))

            # update status label
            self.status_label.setText("Idle" if self.people_renting == 0 else "Renting")
            self.log_area.append("🛑 You stopped sharing your GPU.")

            # instead of stopping polling entirely → switch to idle mode
            self.timer.setInterval(30000)   # slow poll when idle
            if not self.timer.isActive():
                self.timer.start()
            self.signal_timer.stop()

        self.update_indicators()

    def create_rent_gpu_screen(self):
        widget = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(30, 16, 30, 16)
        layout.setSpacing(12)

        title = QLabel("Rent GPU")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        title.setStyleSheet("color: #e6eef8;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # VRAM input
        self.vram_input = QLineEdit()
        self.vram_input.setPlaceholderText("Preferred VRAM (GB)")

        # Maximum job time
        self.max_time_input = QLineEdit()
        self.max_time_input.setPlaceholderText("Max job runtime (minutes)")

        # CPU cores
        self.cpu_cores_input = QLineEdit()
        self.cpu_cores_input.setPlaceholderText("Number of CPU cores")

        # RAM in GB
        self.ram_gb_input = QLineEdit()
        self.ram_gb_input.setPlaceholderText("RAM (GB)")

        # Number of GPUs
        self.num_gpu_input = QLineEdit()
        self.num_gpu_input.setPlaceholderText("Number of GPUs")

        
        
        
        # Buttons
        self.rent_confirm_btn = QPushButton("Request GPU")
        self.rent_confirm_btn.setObjectName("rent_btn")
        self.rent_confirm_btn.setMinimumHeight(46)
        self.rent_confirm_btn.clicked.connect(self.validate_rent_request)

        self.rent_cancel_btn = QPushButton("Cancel")
        self.rent_cancel_btn.setObjectName("cancel_btn")
        self.rent_cancel_btn.setMinimumHeight(46)
        self.rent_cancel_btn.clicked.connect(lambda: self.stack.setCurrentWidget(self.main_screen))

        # Layout
        layout.addWidget(title)
        layout.addWidget(QLabel("Preferred VRAM:"))
        layout.addWidget(self.vram_input)
        layout.addWidget(QLabel("Maximum job time:"))
        layout.addWidget(self.max_time_input)
        layout.addWidget(QLabel("CPU cores:"))
        layout.addWidget(self.cpu_cores_input)
        layout.addWidget(QLabel("RAM (GB):"))
        layout.addWidget(self.ram_gb_input)
        layout.addWidget(QLabel("Number of GPUs:"))
        layout.addWidget(self.num_gpu_input)
        
        layout.addWidget(self.rent_confirm_btn)
        layout.addWidget(self.rent_cancel_btn)

        widget.setLayout(layout)
        return widget

    def update_indicators(self):
        self.sharing_label.setText(f"Sharing: {self.people_sharing}")
        color = "#0fbf6b" if self.people_sharing > 0 else "#b91c1c"
        self.sharing_dot.setStyleSheet(f"border-radius:7px; background: {color};")
        self.renting_label.setText(f"Renting: {self.people_renting}")
        rcolor = "#0fbf6b" if self.people_renting > 0 else "#b91c1c"
        self.renting_dot.setStyleSheet(f"border-radius:7px; background: {rcolor};")

    def launch_job(self):
        if not self.current_user:
            self.styled_message_box("Not logged", "Please register/login first.", QMessageBox.Icon.Warning).exec()
            return
        # open rent dialog
        self.stack.setCurrentWidget(self.rent_gpu_screen)

    def on_rent_btn_clicked(self):
        """Toggle behaviour: if not renting & not waiting -> open request UI.
           If waiting -> cancel pending request. If currently renting -> stop active rent."""
        if not self.current_user:
            self.styled_message_box("Not logged", "Please register/login first.", QMessageBox.Icon.Warning).exec()
            return

        if self._waiting_for_sharer:
            self.cancel_rent()
            return

        if self.is_renting:
            self.cancel_rent()
            return

        # Do not allow renting if there are no jobs queued or running
        if not self.job_queue and self.current_job is None:
            self.styled_message_box("No jobs", "Please add at least one job before renting a GPU.", QMessageBox.Icon.Warning).exec()
            return

        self.launch_job()

    def cancel_rent(self):
        self.log_area.append("🛑 Cancelling rent / pending request...")
        
        data = {"username": self.current_user["username"], "pwd": self.current_user["pwd"]}
        self.run_async(server_post("/cancel_rent", data), callback=self._on_cancel_rent)

    def _on_cancel_rent(self, res):
        if not res.get("ok"):
            self.log_area.append(f"⚠️ Cancel failed: {res.get('error')}")
            return

        self._waiting_for_sharer = False
        self._pending_sharer = None
        if self.renter_poll_timer.isActive():
            self.renter_poll_timer.stop()

        # update renting state
        self.is_renting = False
        self.people_renting = max(0, self.people_renting - 1)
        self.rent_btn.setText("Rent GPU")
        self.session_token = None
        self._last_msg_index = 0
        self.stack.setCurrentWidget(self.main_screen)

        # log what server says
        if res.get("cancelled_active_session"):
            if res.get("refunded"):
                self.log_area.append("🛑 Active rent cancelled and credit refunded.")
            else:
                self.log_area.append("🛑 Active rent cancelled (no refund, session completed).")
            # refresh credits from server
            self.run_async(
                server_post(
                    "/credits",
                    {"username": self.current_user["username"], "pwd": self.current_user["pwd"]}
                ),
                callback=self._on_credits_update_single
            )
        elif res.get("cancelled_pending"):
            self.log_area.append("🛑 Pending rent request cancelled.")
        else:
            self.log_area.append("ℹ️ Nothing to cancel (no pending or active session).")

        # Update status label
        if self.is_sharing:
            self.status_label.setText("Sharing")
            self.timer.setInterval(10000)
            if not self.timer.isActive():
                self.timer.start()
        else:
            self.status_label.setText("Idle")
            self.timer.setInterval(30000)
            if not self.timer.isActive():
                self.timer.start()

        # stop signalling timer if not renting
        if self.signal_timer.isActive():
            self.signal_timer.stop()

        self.update_indicators()

    def _on_respond_accept(self, res, renter):
        if not res.get("ok"):
            self.log_area.append(f"⚠️ Could not accept request from {renter}: {res.get('error')}")
            return

        self.log_area.append(f"✅ You accepted {renter}. Session started.")
        self.people_renting += 1
        self.update_indicators()
        self.status_label.setText(f"Sharing — rented to {renter}")

        session = res.get("session")
        rent_credits = res.get("renter_credits")
        if rent_credits is not None:
            self.log_area.append(f"ℹ️ Renter {renter} credits left: {rent_credits}")
        token = res.get("token") or (res.get("session") or {}).get("token")
        if token:
            self.session_token = token
            self._last_msg_index = 0
            if not self.signal_timer.isActive():
                self.signal_timer.start()

    def _on_match(self, res, target):
        if not res.get("ok"):
            self.log_area.append("⚠️ Matchmaking error.")
            return
        self.people_renting += 0  # no-op, counts will be updated by /counts
        self.status_label.setText("Renting (waiting acceptance)")
        self.log_area.append(f"🚀 Match requested with {target}. Waiting for acceptance...")
        self.update_indicators()
        if not self.timer.isActive():
            self.timer.start(3000)

    def _on_sharers(self, res):
        if not res.get("ok"):
            self.set_offline(True, reason=res.get("error"))
            self.log_area.append("⚠️ Could not contact server for sharers.")
            return
        self.set_offline(False)
        sharers = res.get("sharers", [])

        # fallback demo logic if server returned empty list and a local server state exists
        if not sharers:
            try:
                for uname in list(_server_state.get("users", {}).keys()):
                    if _server_state["users"][uname].get("status") == "sharing" and uname != self.current_user["username"]:
                        sharers.append(uname)
            except Exception:
                pass

        if not sharers:
            self.log_area.append("⚠️ No sharers available (demo).")
            return

        target = sharers[0]
        self.run_async(server_post("/match", {"renter": self.current_user["username"], "sharer": target}),
                       callback=lambda r, t=target: self._on_match(r, t))

    # ---- rent request flow ----
    def validate_rent_request(self):
        try:
            vram = int(self.vram_input.text().strip())
            max_time = int(self.max_time_input.text().strip())
            self.current_max_time = max_time
            cpu_cores = int(self.cpu_cores_input.text().strip())
            cpu_threads = int(self.cpu_cores_input.text().strip())
            num_gpu = int(self.num_gpu_input.text().strip())
            ram_gb = int(self.ram_gb_input.text().strip())
        except ValueError:
            self.styled_message_box("Invalid input", "Please enter valid numbers for job rent.", QMessageBox.Icon.Warning).exec()
            return

        # send request to server
        data = {"username": self.current_user["username"], "vram": vram, "max_time": max_time,"cpu_cores": cpu_cores,"cpu_threads": cpu_threads,"num_gpu": num_gpu,"ram_gb":ram_gb}
        self.log_area.append(f"🔎 Requesting GPU with {vram}GB VRAM, max {max_time} min...")
        self.run_async(server_post("/request_gpu", data), callback=self._on_rent_response)

    def _on_rent_response(self, res):
        if not res.get("ok"):
            self.log_area.append(f"⚠️ Server error: {res.get('error')}")
            self.stack.setCurrentWidget(self.main_screen)
            return

        sharer_info = res.get("sharer") or res.get("gpu")
        if not sharer_info:
            self.log_area.append("⚠️ No matching GPUs available right now.")
            self.stack.setCurrentWidget(self.main_screen)
            return

        requested = sharer_info.get("vram") if isinstance(sharer_info, dict) else None
        self._pending_sharer = sharer_info.get("username") if isinstance(sharer_info, dict) else None
        self._waiting_for_sharer = True

        self.status_label.setText(f"Waiting for sharer consent (requested {requested}GB) — {self._pending_sharer}")
        self.log_area.append(f"🔎 Request sent to {self._pending_sharer}. Waiting for consent...")
        if not self.renter_poll_timer.isActive():
            self.renter_poll_timer.start()
        self.is_renting = False
        self.rent_btn.setText("Cancel Request")
        self.stack.setCurrentWidget(self.main_screen)

    # ---- job processing simulation ----
    def process_job_queue(self):
        if self.current_job is None and self.job_queue:
            self.current_job = self.job_queue.pop(0)
            self.current_job.status = "In progress..."
            self.status_label.setText("Running")
            self.log_area.append(f"🔄 Job in progress: {self.current_job.name}")
            self.update_queue_list()
            QTimer.singleShot(2600, self.finish_current_job)

        if self.is_renting:
            self.log_area.append("🛑 All local jobs finished — rent session complete.")
            self.is_renting = False
            self.rent_btn.setText("Rent GPU")
            self.session_token = None
            self._last_msg_index = 0
            self.status_label.setText("Idle" if not self.is_sharing else "Sharing")
            if self.signal_timer.isActive():
                self.signal_timer.stop()

    def finish_current_job(self):
        if not self.current_job:
            return
        res = random.choice(["Done ✅", "Failed ❌"])
        self.log_area.append(f"{res} Job finished: {self.current_job.name}")
        self.current_job = None
        self.update_queue_list()
        QTimer.singleShot(50, self.process_job_queue)

    # ---- UI building pieces ----
    def styled_message_box(self, title: str, text: str, icon=QMessageBox.Icon.Information) -> QMessageBox:
        msg = QMessageBox(self)
        msg.setWindowTitle(title)
        msg.setText(text)
        msg.setIcon(icon)
        msg.setStyleSheet("""
            QMessageBox {
                background-color: #0b1624;
                color: #e6eef8;
                border-radius: 12px;
                font: 10pt "Segoe UI";
            }
            QPushButton {
                background-color: #4aa3f0;
                color: #ffffff;
                border-radius: 6px;
                padding: 6px 12px;
                min-width: 80px;
            }
            QPushButton:hover {
                background-color: #2f7fd2;
            }
            QLabel {
                color: #cfe6ff;
            }
        """)
        return msg

    def set_theme(self):
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor("#0e1320"))
        pal.setColor(QPalette.ColorRole.WindowText, QColor("#e8eef8"))
        pal.setColor(QPalette.ColorRole.Base, QColor("#0e1320"))
        pal.setColor(QPalette.ColorRole.Text, QColor("#e8eef8"))
        self.setPalette(pal)

    def set_styles(self):
        style = """
        QLabel#appTitle { color: #ffd66b; font-weight: 700; font-size: 20px; }
        QFrame#topBar { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #12213a, stop:1 #1b3658); border-bottom: 1px solid rgba(255,255,255,0.04);}
        QFrame.indicator { background: rgba(255,255,255,0.03); border-radius: 10px; padding: 8px 12px; }
        QPushButton { color: #ffffff; border-radius: 8px; padding: 10px 12px; font-weight: 600; }
        QPushButton#create_job_btn { background: #ff8a3d; }
        QPushButton#rent_btn { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #4aa3f0, stop:1 #2f7fd2); }
        QPushButton#rent_btn:hover { background: #2f7fd2; }
        QPushButton#get_btn { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #48d6b6, stop:1 #1fb494); }
        QPushButton#get_btn:hover { background: #1fb494; }
        QPushButton#upload_btn { background: #8e43c2; }
        QPushButton#validate_btn { background: #3fb14f; }
        QPushButton#cancel_btn { background: #e04848; }
        QListWidget { background: #071026; border: 1px solid rgba(255,255,255,0.03); border-radius: 8px; color: #dbeefe; padding: 6px; }
        QTextEdit { background: #071026; border: 1px solid rgba(255,255,255,0.03); border-radius: 8px; color: #cfe6ff; padding: 8px; }
        QLineEdit { background: #0b1624; border: 1px solid rgba(255,255,255,0.03); border-radius: 6px; padding: 8px; color: #e6eef8;}
        QComboBox { background: #0b1624; border: 1px solid rgba(255,255,255,0.03); border-radius: 6px; padding: 6px; color: #e6eef8; }
        """
        self.setStyleSheet(style)

    def create_topbar(self):
        wrapper = QFrame()
        wrapper_layout = QVBoxLayout()
        wrapper_layout.setContentsMargins(8, 8, 8, 8)
        wrapper_layout.setSpacing(10)

        top = QFrame()
        top.setObjectName("topBar")
        top.setFixedHeight(68)
        tlayout = QHBoxLayout()
        tlayout.setContentsMargins(16, 8, 16, 8)
        tlayout.setSpacing(12)
        top.setMaximumWidth(488)

        self.title_icon = QLabel()
        self.title_icon.setPixmap(QtGui.QPixmap("assets/icons/logo.png").scaled(48, 48, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.title_icon.setObjectName("appTitleIcon")

        right = QFrame()
        rlayout = QHBoxLayout()
        rlayout.setContentsMargins(0, 0, 0, 0)
        rlayout.setSpacing(12)

        self.offline_icon = QLabel("")   # will show "⛔ OFFLINE" when server unreachable
        self.offline_icon.setStyleSheet("color: #ff6b6b; font-weight: 700;")
        self.offline_icon.setVisible(False)
        rlayout.addWidget(self.offline_icon)

        self.user_label = QLabel("")
        self.user_label.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        self.user_label.setStyleSheet("color: #ffd66b; padding: 6px;")

        self.credits_label = QLabel(f"Credits: {self.credits}")
        self.credits_label.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        self.credits_label.setStyleSheet("color: #dbeafe; padding: 6px;")

        self.status_label = QLabel("Idle")
        self.status_label.setFont(QFont("Segoe UI", 11))
        self.status_label.setStyleSheet("color: #bcd2ff; padding: 6px;")

        rlayout.addWidget(self.user_label)
        rlayout.addWidget(self.credits_label)
        rlayout.addWidget(self.status_label)

        # --- New Settings Button ---
        
        
        self.settings_btn = QtWidgets.QPushButton()
        self.settings_btn.setIcon(QtGui.QIcon("assets/icons/gear.png"))
        self.settings_btn.setIconSize(QtCore.QSize(22, 22))  # default smaller size
        self.settings_btn.setToolTip("Settings")
        self.settings_btn.setFixedSize(34, 34)
        self.settings_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: none;
            }
        """)
        self.settings_btn.clicked.connect(lambda: self.stack.setCurrentWidget(self.settings_screen))

        # Hide button if offline
        self.settings_btn.setVisible(not getattr(self, "is_offline", False))

        rlayout.addWidget(self.settings_btn)

        # --- Animate icon size on hover ---
        def animate_icon(btn, grow=True):
            anim = QPropertyAnimation(btn, b"iconSize")
            anim.setDuration(150)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)

            current = btn.iconSize()
            if grow:
                target = QtCore.QSize(28, 28)  # gentle grow
            else:
                target = QtCore.QSize(22, 22)  # back to normal

            anim.setStartValue(current)
            anim.setEndValue(target)
            anim.start()
            btn._anim = anim  # keep ref alive

        def enterEvent(event, btn=self.settings_btn):
            animate_icon(btn, grow=True)
            return QtWidgets.QPushButton.enterEvent(btn, event)

        def leaveEvent(event, btn=self.settings_btn):
            animate_icon(btn, grow=False)
            return QtWidgets.QPushButton.leaveEvent(btn, event)

        self.settings_btn.enterEvent = enterEvent
        self.settings_btn.leaveEvent = leaveEvent

        # -----------------------------

        right.setLayout(rlayout)

        tlayout.addWidget(self.title_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        tlayout.addStretch()
        tlayout.addWidget(right, 0, Qt.AlignmentFlag.AlignVCenter)
        top.setLayout(tlayout)

        indicators = QFrame()
        ind_layout = QHBoxLayout()
        ind_layout.setContentsMargins(20, 0, 20, 6)
        ind_layout.setSpacing(18)

        sharing_frame, self.sharing_dot, self.sharing_label = self.create_status_label("Sharing: 0")
        renting_frame, self.renting_dot, self.renting_label = self.create_status_label("Renting: 0")

        ind_layout.addWidget(sharing_frame)
        ind_layout.addStretch()
        ind_layout.addWidget(renting_frame)
        indicators.setLayout(ind_layout)

        wrapper_layout.addWidget(top)
        wrapper_layout.addWidget(indicators)
        wrapper.setLayout(wrapper_layout)
        return wrapper

    def create_status_label(self, text):
        frame = QFrame()
        frame.setObjectName("indicator")
        frame.setProperty("class", "indicator")
        layout = QHBoxLayout()
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(10)

        dot = QLabel()
        dot.setFixedSize(14, 14)
        dot.setStyleSheet("border-radius:7px; background: #b91c1c;")
        text_lbl = QLabel(text)
        text_lbl.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        text_lbl.setStyleSheet("color: #eaf2ff;")
        layout.addWidget(dot)
        layout.addWidget(text_lbl)
        layout.addStretch()
        frame.setLayout(layout)
        return frame, dot, text_lbl

    def create_login_screen(self):
        w = QWidget()
        outer_layout = QVBoxLayout()
        outer_layout.setContentsMargins(40, 20, 40, 20)
        outer_layout.setSpacing(20)

        box = QFrame()
        box.setStyleSheet("""
            QFrame {
                background-color: #0b1624;
                border-radius: 16px;
            }
            QLineEdit {
                background-color: #1a1c26;
                border: 1px solid rgba(255,255,255,0.1);
                border-radius: 6px;
                padding: 6px;
                color: #e6eef8;
            }
            QPushButton {
                background-color: #4aa3f0;
                color: #fff;
                border-radius: 8px;
                padding: 8px 12px;
            }
            QPushButton:hover {
                background-color: #2f7fd2;
            }
            QLabel {
                color: #cfe6ff;
            }
        """)
        box_layout = QVBoxLayout()
        box_layout.setContentsMargins(80, 40, 80, 40)
        box_layout.setSpacing(20)

        title = QLabel("Welcome to GPULend")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        desc = QLabel("Register :")
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)

        def mk_lineedit(placeholder: str, echo=QLineEdit.EchoMode.Normal) -> QLineEdit:
            le = QLineEdit()
            le.setPlaceholderText(placeholder)
            le.setMaximumWidth(360)
            le.setEchoMode(echo)
            return le

        self.reg_user = mk_lineedit("Username")
        self.reg_pwd = mk_lineedit("Password", QLineEdit.EchoMode.Password)
        self.reg_btn = QPushButton("Register")
        self.reg_btn.setMaximumWidth(240)
        self.reg_btn.clicked.connect(self.handle_register)

        self.login_user = mk_lineedit("Username")
        self.login_pwd = mk_lineedit("Password", QLineEdit.EchoMode.Password)
        self.login_btn = QPushButton("Login")
        self.login_btn.setMaximumWidth(240)
        self.login_btn.clicked.connect(self.handle_login)

        reg_box = QVBoxLayout()
        reg_box.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        reg_box.addWidget(title)
        reg_box.addWidget(desc)
        reg_box.addWidget(self.reg_user)
        reg_box.addWidget(self.reg_pwd)
        reg_box.addWidget(self.reg_btn)

        login_box = QVBoxLayout()
        login_box.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        login_box.addWidget(QLabel("Already have an account?"))
        login_box.addWidget(self.login_user)
        login_box.addWidget(self.login_pwd)
        login_box.addWidget(self.login_btn)

        box_layout.addLayout(reg_box)
        box_layout.addSpacing(24)
        box_layout.addLayout(login_box)
        box.setLayout(box_layout)

        outer_layout.addWidget(box, alignment=Qt.AlignmentFlag.AlignHCenter)

        w.setLayout(outer_layout)
        w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        return w

    def create_main_screen_content(self):
        widget = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(26, 16, 26, 18)
        layout.setSpacing(14)

        actions_v = QVBoxLayout()
        actions_v.setSpacing(12)

        # create rent button
        self.rent_btn = QPushButton("Rent GPU")
        self.rent_btn.setObjectName("rent_btn")
        self.rent_btn.setMinimumHeight(50)
        self.rent_btn.clicked.connect(self.on_rent_btn_clicked)

        self.get_btn = QPushButton("Share GPU")
        self.get_btn.setObjectName("get_btn")
        self.get_btn.setMinimumHeight(50)
        self.get_btn.clicked.connect(self.toggle_share_gpu)

        self.create_job_btn = QPushButton("Create Job")
        self.create_job_btn.setObjectName("create_job_btn")
        self.create_job_btn.setMinimumHeight(50)
        self.create_job_btn.clicked.connect(lambda: self.stack.setCurrentWidget(self.create_job_screen))

        actions_v.addWidget(self.rent_btn)
        actions_v.addWidget(self.get_btn)
        actions_v.addWidget(self.create_job_btn)

        queue_header = QHBoxLayout()
        lbl = QLabel("Job Queue")
        lbl.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        lbl.setStyleSheet("color: #dbeafe;")
        desc = QLabel("(double-click an item to remove)")
        desc.setFont(QFont("Segoe UI", 9))
        desc.setStyleSheet("color: rgba(255,255,255,0.6);")
        queue_header.addWidget(lbl)
        queue_header.addStretch()
        queue_header.addWidget(desc)

        self.queue_list = QListWidget()
        self.queue_list.setMinimumHeight(200)
        self.queue_list.itemDoubleClicked.connect(self.delete_job_item)

        log_label = QLabel("Activity Log:")
        log_label.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        log_label.setStyleSheet("color: #dbeafe;")
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMinimumHeight(150)

        layout.addLayout(actions_v)
        layout.addLayout(queue_header)
        layout.addWidget(self.queue_list)
        layout.addWidget(log_label)
        layout.addWidget(self.log_area)

        widget.setLayout(layout)
        return widget

    def create_job_screen_content(self):
        widget = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(30, 16, 30, 16)
        layout.setSpacing(12)

        title = QLabel("Create Job")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        title.setStyleSheet("color: #e6eef8;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Job name (optional)")

        self.priority_box = QComboBox()
        self.priority_box.addItems(["Low", "Medium", "High"])
        self.priority_box.setCurrentText("Medium")

        self.upload_btn = QPushButton("Choose job folder")
        self.upload_btn.setObjectName("upload_btn")
        self.upload_btn.setMinimumHeight(46)
        self.upload_btn.clicked.connect(self.select_folder)

        self.folder_preview = QLabel("No folder selected")
        self.folder_preview.setFont(QFont("Segoe UI", 9))
        self.folder_preview.setStyleSheet("color: rgba(255,255,255,0.6);")

        self.preset_box = QComboBox()
        self.preset_box.addItem("— Presets —")
        self.preset_box.addItem("python train.py --epochs 10 --batch 32")
        self.preset_box.addItem("python infer.py --model model.pt --input data/")
        self.preset_box.addItem("python render.py --scene scene.glb")
        self.preset_box.currentIndexChanged.connect(self.preset_selected)

        self.cmd_input = QTextEdit()
        self.cmd_input.setPlaceholderText("Job command, e.g.: python train.py --epochs 10\nYou can paste multi-line scripts here.")

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Job")
        add_btn.setObjectName("validate_btn")
        add_btn.setMinimumHeight(44)
        add_btn.clicked.connect(self.validate_job)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("cancel_btn")
        cancel_btn.setMinimumHeight(44)
        cancel_btn.clicked.connect(lambda: self.stack.setCurrentWidget(self.main_screen))

        btn_row.addWidget(add_btn)
        btn_row.addWidget(cancel_btn)

        layout.addWidget(title)
        layout.addWidget(QLabel("Name:"))
        layout.addWidget(self.name_input)
        layout.addWidget(QLabel("Priority:"))
        layout.addWidget(self.priority_box)
        layout.addWidget(self.upload_btn)
        layout.addWidget(self.folder_preview)
        layout.addWidget(QLabel("Presets:"))
        layout.addWidget(self.preset_box)
        layout.addWidget(QLabel("Command:"))
        layout.addWidget(self.cmd_input)
        layout.addLayout(btn_row)
        widget.setLayout(layout)
        return widget


# ---------------------------
# Run app
# ---------------------------


class PodmanInstallerThread(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)  # success, path_or_error

    def run(self):
        try:
            podman_path = find_podman()
            if podman_path:
                self.log_signal.emit(f"✅ Podman already found: {podman_path}")
                self.finished_signal.emit(True, podman_path)
                return

            self.log_signal.emit("🔄 Podman not found, installing...")

            system = platform.system().lower()
            arch = platform.machine().lower()

            if system == "linux":
                self.log_signal.emit("📦 Installing Podman via system package manager...")
                try:
                    if shutil.which("apt"):
                        subprocess.run(["pkexec", "apt", "update"], check=True)
                        subprocess.run(["pkexec", "apt", "install", "-y", "podman"], check=True)
                    elif shutil.which("dnf"):
                        subprocess.run(["pkexec", "dnf", "install", "-y", "podman"], check=True)
                    elif shutil.which("yum"):
                        subprocess.run(["pkexec", "yum", "install", "-y", "podman"], check=True)
                    elif shutil.which("pacman"):
                        subprocess.run(["pkexec", "pacman", "-Sy", "podman", "--noconfirm"], check=True)
                    else:
                        raise RuntimeError("No supported package manager found (apt, dnf, yum, pacman). Please install Podman manually.")
                except subprocess.CalledProcessError as e:
                    self.finished_signal.emit(False, f"System package manager install failed: {e}")
                    return

            elif system == "windows":
                file_name = (
                    "podman-installer-windows-amd64.exe"
                    if arch in ("amd64", "x86_64")
                    else "podman-installer-windows-arm64.exe"
                )
                local_dir = os.path.join(os.getcwd(), "podman_bin")
                os.makedirs(local_dir, exist_ok=True)
                local_path = os.path.join(local_dir, file_name)
                url = f"https://github.com/containers/podman/releases/latest/download/{file_name}"
                self.log_signal.emit(f"⬇️ Downloading {file_name} ...")
                urllib.request.urlretrieve(url, local_path)
                self.log_signal.emit(f"✅ Downloaded to {local_path}")
                self.log_signal.emit("⚙️ Launching Podman installer...")
                subprocess.run([local_path], check=True)

            elif system == "darwin":
                file_name = (
                    "podman-installer-macos-amd64.pkg"
                    if arch == "x86_64"
                    else "podman-installer-macos-arm64.pkg"
                )
                local_dir = os.path.join(os.getcwd(), "podman_bin")
                os.makedirs(local_dir, exist_ok=True)
                local_path = os.path.join(local_dir, file_name)
                url = f"https://github.com/containers/podman/releases/latest/download/{file_name}"
                self.log_signal.emit(f"⬇️ Downloading {file_name} ...")
                urllib.request.urlretrieve(url, local_path)
                self.log_signal.emit(f"✅ Downloaded to {local_path}")
                subprocess.run(["open", local_path], check=True)

            else:
                raise RuntimeError(f"Unsupported OS/arch: {system}/{arch}")

            # 🔄 Re-check podman after installation
            podman_path = find_podman()
            if podman_path:
                self.log_signal.emit(f"✅ Podman ready: {podman_path}")
                self.finished_signal.emit(True, podman_path)
            else:
                self.log_signal.emit("⚠️ Podman not detected after installation.")
                self.finished_signal.emit(False, "Podman not found; please complete installation manually.")

        except Exception as e:
            self.finished_signal.emit(False, str(e))



class EnsurePodmanDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ensure Podman")
        self.resize(500, 340)
        self.setStyleSheet("""
    QDialog {
        background-color: #2b2b2b;  /* dark gray */
        color: #ffffff;             /* text color */
    }
    QPushButton {
        background-color: #555555;  /* medium gray */
        color: #ffffff;
        border-radius: 5px;
        padding: 5px 10px;
    }
    QPushButton:hover {
        background-color: #777777;
    }
    QTextEdit {
        background-color: #1e1e1e;
        color: #ffffff;
    }
    QProgressBar {
        background-color: #3c3c3c;
        color: #ffffff;
    }
        """)

        self.layout = QVBoxLayout()
        self.label = QLabel("Checking Podman installation...")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # indeterminate

        self.finish_btn = QPushButton("Finish")
        self.finish_btn.setEnabled(False)
        self.finish_btn.clicked.connect(self.accept)

        self.retry_btn = QPushButton("Retry Installation")
        self.retry_btn.clicked.connect(self.start_thread)
        self.retry_btn.hide()

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)

        self.layout.addWidget(self.label)
        self.layout.addWidget(self.log_area)
        self.layout.addWidget(self.progress)
        self.layout.addWidget(self.finish_btn)
        self.layout.addWidget(self.retry_btn)
        self.layout.addWidget(self.cancel_btn)
        self.setLayout(self.layout)

        self.start_thread()

    def start_thread(self):
        self.finish_btn.setEnabled(False)
        self.retry_btn.hide()
        self.progress.setRange(0, 0)
        self.log_area.append("\n🔄 Starting Podman check/install...")

        self.thread = PodmanInstallerThread()
        self.thread.log_signal.connect(self.add_log)
        self.thread.finished_signal.connect(self.on_finished)
        self.thread.start()

    def add_log(self, text: str):
        self.log_area.append(text)

    def on_finished(self, success: bool, path_or_error: str):
        if success:
            self.log_area.append(f"✅ Podman detected: {path_or_error}")
            self.progress.setRange(0, 1)
            self.finish_btn.setEnabled(True)
        else:
            self.log_area.append(f"⚠️ {path_or_error}")
            self.progress.setRange(0, 1)
            self.retry_btn.show()

            
            
if __name__ == "__main__":
      # modal; will block until finished, but UI is interactive
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
