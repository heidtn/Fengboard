"""Entry point: python -m Fengboard"""
from __future__ import annotations

import logging
import os
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from .constants import APP_NAME
from .gui import MainWindow


def _configure_logging():
    # FENGBOARD_LOG_LEVEL=DEBUG also surfaces CHIRP's raw serial-protocol trace.
    level = os.environ.get("FENGBOARD_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main():
    _configure_logging()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    win = MainWindow()
    app.aboutToQuit.connect(win.shutdown)
    # Route through closeEvent (not app.quit() directly) so Ctrl+C respects the
    # same guards as the window's close button: refuses to quit mid-clone, and
    # confirms before discarding unsaved channel edits.
    signal.signal(signal.SIGINT, lambda *_: win.close())
    tick = QTimer()  # lets Python see Ctrl+C while Qt runs
    tick.start(300)
    tick.timeout.connect(lambda: None)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
