"""
Desktop application (PySide6 / Qt). Runs RiskService in-process: no server
needed. Uses the same riskmesh.toml as the CLI and the API.

Start with `riskmesh gui`, `riskmesh-gui` or `py -m riskmesh.gui [--config FILE]`.
Requires the optional dependency:  pip install "riskmesh[gui]"
"""

import argparse
import sys
from typing import List, Optional

from ..config import Settings, load_settings


def main(settings: Optional[Settings] = None, argv: Optional[List[str]] = None) -> int:
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        print('The desktop GUI needs PySide6:  pip install "riskmesh[gui]"', file=sys.stderr)
        return 1
    from .app import MainWindow

    app = QApplication.instance() or QApplication(argv or sys.argv[:1])
    app.setApplicationName("RiskMesh")
    app.setStyle("Fusion")
    try:
        window = MainWindow(settings or load_settings())
    except Exception as e:  # configuration or database problems: show them instead of a traceback
        QMessageBox.critical(None, "RiskMesh", f"Could not start:\n\n{e}")
        return 1
    window.show()
    return app.exec()


def run() -> int:
    """Entry point for the `riskmesh-gui` command."""
    parser = argparse.ArgumentParser(prog="riskmesh-gui")
    parser.add_argument("--config", help="riskmesh.toml to use")
    args = parser.parse_args()
    try:
        settings = load_settings(args.config)
    except (OSError, ValueError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    return main(settings)
