# GPULend 🚀

## 🌟 Overview
**Free computation for all** – GPULend allows you to seamlessly offload GPU-intensive tasks to available resources.

![Screenshot 1](https://github.com/user-attachments/assets/f6740670-25af-4d6b-b901-50e6b0f8685a)
![Screenshot 2](https://github.com/user-attachments/assets/cd48c461-b910-4c32-a4ec-35b84c470f97)

Rent your low-VRAM GPU to access high-VRAM GPUs elsewhere.


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

