"""UI fixes layered on top of gui_v13."""
from __future__ import annotations

import base64
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QPushButton

import gui
import gui_v13


_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp")
_THUMBNAIL_KEYS = (
    "thumbnail_path", "thumb_path", "preview_path", "jpeg_path",
    "cached_thumbnail", "cache_path", "thumbnail_file", "preview_file",
)
_THUMBNAIL_DATA_KEYS = ("thumbnail", "thumbnail_data", "thumb_data", "preview_data")


def _valid_path(value):
    """Return a usable Path without ever treating an empty value as '.'."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == ".":
        return None
    try:
        path = Path(text)
    except (TypeError, ValueError, OSError):
        return None
    return path if path.name else None


def _append_candidate(candidates, seen, value):
    path = _valid_path(value)
    if path is None:
        return
    try:
        key = str(path.resolve(strict=False)).lower()
    except (OSError, RuntimeError):
        key = str(path).lower()
    if key not in seen:
        seen.add(key)
        candidates.append(path)


def _pixmap_from_data(value):
    if value is None:
        return None
    data = None
    if isinstance(value, memoryview):
        data = value.tobytes()
    elif isinstance(value, (bytes, bytearray)):
        data = bytes(value)
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("data:image") and "," in text:
            text = text.split(",", 1)[1]
        try:
            data = base64.b64decode(text, validate=True)
        except Exception:
            return None
    if not data:
        return None
    pixmap = QPixmap()
    return pixmap if pixmap.loadFromData(data) and not pixmap.isNull() else None


def _load_outlier_pixmap(self, item):
    """Load original/sidecar first, then stored or cached thumbnail."""
    candidates, seen = [], set()
    original = _valid_path(item.get("file_path"))
    _append_candidate(candidates, seen, original)

    # Explicit thumbnail/preview paths returned by the quarantine database.
    for key in _THUMBNAIL_KEYS:
        _append_candidate(candidates, seen, item.get(key))

    filename = str(item.get("filename") or "").strip()
    stem = Path(filename).stem if filename else (original.stem if original else "")

    # JPEG/PNG/TIFF generated beside the RAW.
    if original is not None and original.name:
        for ext in _IMAGE_EXTENSIONS:
            _append_candidate(candidates, seen, original.with_suffix(ext))

    # Common thumbnail/cache directories used by this application and old banks.
    roots = []
    if original is not None:
        roots.append(original.parent)
    app_dir = Path(getattr(gui, "APP_DIR", Path.cwd()))
    roots.extend((app_dir, app_dir / "banco"))
    folders = ("thumbnails", "thumbnail", "miniaturas", "previews", "preview", "cache", "_tmp")
    if stem:
        for root in roots:
            for folder in ("", *folders):
                base = root / folder if folder else root
                for ext in _IMAGE_EXTENSIONS:
                    _append_candidate(candidates, seen, base / f"{stem}{ext}")

    for candidate in candidates:
        try:
            if candidate.is_file():
                pixmap = QPixmap(str(candidate))
                if not pixmap.isNull():
                    return pixmap, candidate
        except OSError:
            continue

    # Some database versions may store the thumbnail bytes/base64 directly.
    for key in _THUMBNAIL_DATA_KEYS:
        pixmap = _pixmap_from_data(item.get(key))
        if pixmap is not None:
            return pixmap, None

    return None, None


def _show_selected_safe(self):
    item = self._current_item()
    if not item:
        self._clear_preview("Selecione um outlier")
        return

    original = _valid_path(item.get("file_path"))
    pixmap, loaded_from = _load_outlier_pixmap(self, item)
    self._pixmap = pixmap

    if original is not None:
        label = str(original)
    elif loaded_from is not None:
        label = f"não registrado — miniatura: {loaded_from}"
    else:
        label = "não registrado"
    self.lbl_path.setText(f"Caminho: {label}")

    if pixmap is not None and not pixmap.isNull():
        self.preview.setPixmap(pixmap.scaled(
            self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
        ))
        self.preview.setText("")
    else:
        self.preview.setPixmap(QPixmap())
        self.preview.setText(
            "Foto e miniatura indisponíveis.\n"
            "O registro continua seguro na quarentena."
        )


def _load_pixmap_safe(self, path):
    """Compatibility wrapper for callers that still pass only one path."""
    valid = _valid_path(path)
    if valid is None:
        return None
    pixmap, _ = _load_outlier_pixmap(self, {
        "file_path": str(valid),
        "filename": valid.name,
    })
    return pixmap


def _open_original_safe(self):
    item = self._current_item()
    original = _valid_path(item.get("file_path")) if item else None
    if original is None or not original.is_file():
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(self, "Arquivo", "O arquivo original não foi encontrado nesse caminho.")
        return
    gui_v13.EnhancedOutliersTab._original_open_original(self)


# Install the defensive preview behavior before MainWindow creates the tab.
gui_v13.EnhancedOutliersTab._original_open_original = gui_v13.EnhancedOutliersTab._open_original
gui_v13.EnhancedOutliersTab._show_selected = _show_selected_safe
gui_v13.EnhancedOutliersTab._load_pixmap = _load_pixmap_safe
gui_v13.EnhancedOutliersTab._open_original = _open_original_safe


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
