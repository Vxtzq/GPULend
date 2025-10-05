# GPULend 🚀

| Screenshots | Overview |
|------------|----------|
| <img src="https://github.com/user-attachments/assets/f6740670-25af-4d6b-b901-50e6b0f8685a" width="200"><br><img src="https://github.com/user-attachments/assets/cd48c461-b910-4c32-a4ec-35b84c470f97" width="200"> | **Free computation for all** – GPULend allows you to seamlessly offload GPU-intensive tasks to available resources in a simple, secure way.<br><br>Whether you are training ML models, rendering, or crunching large datasets, GPULend simplifies distributed GPU usage.<br><br>Rent your own low-VRAM GPU to access high-VRAM GPUs elsewhere and maximize computation power.<br><br>**⚡ Features:**<br>- P2P GPU Sharing – Connect your GPU to the network or rent access to high-VRAM GPUs.<br>- Distributed Computation – Offload tasks safely across multiple GPUs.<br>- Cross-Platform – Runs on Windows and Linux (macOS support coming soon).<br>- Easy Setup – Launch via standalone executable, AppImage, or Python source.<br>- Podman Integration – Containers ensure isolated, reproducible environments. |


---
## ✨ Features

- Lightweight and portable GUI
- Automatic GPU detection and management
- Cross-platform support (Windows & Linux)
- Works with Podman containers for secure execution
- Easy installation: standalone executable or AppImage  

---

## 🛠 Installation

### Windows

1. Download the standalone `.exe` file.  
2. Or run the installer batch file:  

```
git clone https://github.com/Vxtzq/GPULend
```

```bat
install_and_run.bat
```
### Linux
```
git clone https://github.com/Vxtzq/GPULend
```

```
chmod +x GPULend.AppImage
./GPULend.AppImage
```

Optional: Run from Python

If you prefer running from source:
```python
python -m pip install -r requirements.txt
python app.py
```
## 💻 Supported Platforms

| Platform             | Notes                                    |
|--------------------|------------------------------------------|
| Windows 10/11        | Standalone `.exe` or batch installer   |
| Linux (Ubuntu/Fedora)| AppImage or Python source              |
| macOS                | [TODO: Add macOS support]              |

---

## ⚡ Quick Start

- Launch GPULend.

- Ensure Podman is installed and detected.

- Offload computation tasks to available GPUs effortlessly.


---

## 📝 TODO / Customization

- Add more screenshots or GIF demos.
- Include configuration or settings section.
- Add badges for GitHub releases, license, CI/CD.
- Update macOS support instructions.

