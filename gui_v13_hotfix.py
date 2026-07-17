"""Small UI cleanup layered on top of gui_v13."""
import sys

from PySide6.QtWidgets import QApplication, QPushButton

import gui
import gui_v13


class MainWindow(gui_v13.MainWindow):
    def __init__(self):
        super().__init__()
        for tab in (self.tab_preset, self.tab_exterior):
            for button in tab.findChildren(QPushButton):
                text = button.text().lower()
                if (
                    "selecionar catálogo" in text
                    or "classificar" in text
                    or "aplicar presets ao catálogo" in text
                    or "aplicar preset às fotos externas" in text
                ):
                    button.hide()


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(gui.STYLE)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
