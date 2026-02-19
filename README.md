# CdA Analyser

## Description

The CdA Analyser is a Python-based application designed to help cyclists and aerodynamicists analyze their aerodynamic drag (CdA) from .fit files. It processes ride data to calculate and visualize CdA, providing insights into aerodynamic performance.

## Release Command

To create a standalone executable for the CdA Analyser, use the following command:

```bash
cd src
python -m PyInstaller --onefile --windowed --icon=icons/logo_blue.ico --name="CdA-Analyser" --noupx --add-data "icons;icons" main.py
```
or faster bigger multi file alternativ:

```bash
cd src
python -m PyInstaller --onedir --windowed --icon=icons/logo_blue.ico --name="CdA-Analyser" --noupx --add-data "icons;icons" main.py
```

## Future improvements / TODO's

*   **Units:** Change units to km/h instead of m/s for speed display.
*   **Stability:** Investigate and resolve potential crashes when re-analyzing dataset more then ones. possibly related to weather data caching?
*   ~~**User Interface:** Add a slider for `wind_effect_factor` and make it trigger a re-analysis.~~
*   **Add Guide:** Write down how the program works and what the parameters do.

## Dependencies

> This project uses the following open-source libraries:
>
> - [fitparse](https://github.com/dtcooper/python-fitparse) (BSD License)
> - [folium](https://python-visualization.github.io/folium/) (MIT License)
> - [geopy](https://github.com/geopy/geopy) (MIT License)
> - [matplotlib](https://matplotlib.org/) (Matplotlib License, BSD-compatible)
> - [numpy](https://numpy.org/) (BSD-3-Clause)
> - [pandas](https://pandas.pydata.org/) (BSD-3-Clause)
> - [Pillow](https://python-pillow.org/) (PIL Software License, MIT-like)
> - [PyQt5](https://riverbankcomputing.com/software/pyqt/intro) (GPL v3)
> - [PyQt5_sip](https://pypi.org/project/PyQt5-sip/) (GPL v3)
> - [requests](https://docs.python-requests.org/) (Apache-2.0)
> - [scipy](https://scipy.org/) (BSD License)
>
> All libraries retain their original licenses. Attribution is preserved.

## License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for details.