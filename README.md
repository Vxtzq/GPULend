# GPULend 🚀

<div style="display: flex; gap: 20px; align-items: flex-start; justify-content: center;">

  <!-- Left: Screenshots -->
  <div style="flex: 1; text-align: center;">
    <img src="https://github.com/user-attachments/assets/f6740670-25af-4d6b-b901-50e6b0f8685a" 
         alt="Screenshot 1" width="200" style="margin-bottom: 10px;">
    <img src="https://github.com/user-attachments/assets/cd48c461-b910-4c32-a4ec-35b84c470f97" 
         alt="Screenshot 2" width="200">
  </div>

  <!-- Right: Overview Text -->
  <div style="flex: 1;">
    <h2>🌟 Overview</h2>
    <p>
      <strong>Free computation for all</strong> – GPULend allows you to seamlessly offload GPU-intensive tasks to available resources in a simple, secure way.
      Whether you are training ML models, rendering, or crunching large datasets, GPULend simplifies distributed GPU usage.
      <br><br>
      Rent your own low-VRAM GPU to access high-VRAM GPUs elsewhere and maximize computation power.
    </p>
    <h2>⚡ Features</h2>
    <ul>
      <li><strong>P2P GPU Sharing</strong> – Connect your GPU to the network or rent access to high-VRAM GPUs.</li>
      <li><strong>Distributed Computation</strong> – Offload tasks safely across multiple GPUs.</li>
      <li><strong>Cross-Platform</strong> – Runs on Windows and Linux (macOS support coming soon).</li>
      <li><strong>Easy Setup</strong> – Launch via standalone executable, AppImage, or Python source.</li>
      <li><strong>Podman Integration</strong> – Containers ensure isolated, reproducible environments.</li>
    </ul>
  </div>

</div>

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

