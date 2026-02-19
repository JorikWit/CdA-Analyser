# CdA Analyser

## Description

The **CdA Analyser** is a Python-based application designed to help cyclists and aerodynamicists analyze their aerodynamic drag (CdA) from `.fit` files. It processes ride data to calculate and visualize CdA, providing insights into aerodynamic performance.

---

## Recreating the Virtual Environment (.venv)

If you cloned the repository or pulled new changes, follow these steps:

### 1. Delete old `.venv` (optional)

**Windows PowerShell**

```powershell
Remove-Item -Recurse -Force .venv
```

**Linux/macOS**

```bash
rm -rf .venv
```

### 2. Create a new virtual environment

**Windows PowerShell**

```powershell
python -m venv .venv
```

**Linux/macOS**

```bash
python3 -m venv .venv
```

### 3. Activate the virtual environment

**Windows PowerShell**

```powershell
.\.venv\Scripts\Activate.ps1
```

**Windows CMD**

```cmd
.\.venv\Scripts\activate.bat
```

**Linux/macOS**

```bash
source .venv/bin/activate
```

You should see `(venv)` in your prompt.

### 4. Install project dependencies

```bash
pip install -r requirements.txt
```

### 5. Test the environment

```bash
cd src
python main.py --gui
```

The application should launch without errors.

---

## Release Command

To create a standalone executable for the CdA Analyser:

**Single-file executable**

```powershell
.\.venv\Scripts\Activate.ps1
cd src
python -m PyInstaller --onefile --icon=icons/logo_blue.ico --name="CdA-Analyser" --noupx --add-data "icons;icons" main.py
```

**Multi-file (faster)**

```powershell
.\.venv\Scripts\Activate.ps1
cd src
python -m PyInstaller --onedir --icon=icons/logo_blue.ico --name="CdA-Analyser" --noupx --add-data "icons;icons" main.py
```

---

## Future Improvements / TODOs

* **Units:** Change units to km/h instead of m/s for speed display.
* **Stability:** Investigate potential crashes when re-analyzing a dataset multiple times (possibly related to weather data caching).
* ~~**User Interface:** Add a slider for `wind_effect_factor` to trigger a re-analysis.~~
* **Add Guide:** Document how the program works and what the parameters do.

---

## Dependencies

This project uses the following open-source libraries:

* [fitparse](https://github.com/dtcooper/python-fitparse) (BSD License)
* [folium](https://python-visualization.github.io/folium/) (MIT License)
* [geopy](https://github.com/geopy/geopy) (MIT License)
* [matplotlib](https://matplotlib.org/) (Matplotlib License, BSD-compatible)
* [numpy](https://numpy.org/) (BSD-3-Clause)
* [pandas](https://pandas.pydata.org/) (BSD-3-Clause)
* [Pillow](https://python-pillow.org/) (PIL Software License, MIT-like)
* [PyQt5](https://riverbankcomputing.com/software/pyqt/intro) (GPL v3)
* [PyQt5_sip](https://pypi.org/project/PyQt5-sip/) (GPL v3)
* [requests](https://docs.python-requests.org/) (Apache-2.0)
* [scipy](https://scipy.org/) (BSD License)

All libraries retain their original licenses.

---

## License

This project is licensed under the **GNU General Public License v3.0**. See the [LICENSE](LICENSE) file for details.