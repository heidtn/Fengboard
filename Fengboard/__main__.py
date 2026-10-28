"""Entry point: python -m Fengboard"""
from __future__ import annotations

import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from .constants import APP_NAME
from .gui import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    win = MainWindow()
    app.aboutToQuit.connect(win.shutdown)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    tick = QTimer()  # lets Python see Ctrl+C while Qt runs
    tick.start(300)
    tick.timeout.connect(lambda: None)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
