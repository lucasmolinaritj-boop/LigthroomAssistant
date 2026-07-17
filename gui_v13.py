"""Lightroom Assistant v13 UI integration.

Adds a single-catalog automatic pipeline and an enhanced outlier viewer while
keeping the proven v12 widgets and processing engine intact.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import core
import gui
import gui_tools


class ClickablePreview(QLabel):
    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class EnhancedOutliersTab(QWidget):
    """Outlier review with full path, visible thumbnail and large preview."""

    def __init__(self, con, logger, parent=None):
        super().__init__(parent)
        self.con, self.logger = con, logger
        self._items = []
        self._pixmap = None
        lay = QVBoxLayout(self)
        title = QLabel("Fotos em Quarentena (Outliers)")
        title.setObjectName("section_title")
        lay.addWidget(title)
        info = QLabel(
            "Confira a foto, o caminho original e o motivo antes de decidir. "
            "Clique na miniatura para ampliar; nada é removido automaticamente."
        )
        info.setWordWrap(True)
        lay.addWidget(info)
        body = QHBoxLayout()
        left = QVBoxLayout()
        actions = QHBoxLayout()
        for text, slot in (
            ("🔄 Atualizar", self.refresh),
            ("✔ Aprovar", lambda: self._resolve_selected("approve")),
            ("✕ Ignorar", lambda: self._resolve_selected("ignore")),
            ("↺ Restaurar ao banco", self._restore_selected),
        ):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            actions.addWidget(btn)
        actions.addStretch()
        left.addLayout(actions)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["ID", "Arquivo", "Caminho", "Parâmetro", "Valor", "Status", "Motivo"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._show_selected)
        self.table.itemDoubleClicked.connect(lambda *_: self._open_original())
        left.addWidget(self.table)
        body.addLayout(left, 3)
        preview_box = QVBoxLayout()
        preview_box.addWidget(QLabel("Pré-visualização"))
        self.preview = ClickablePreview("Selecione um outlier")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(300, 260)
        self.preview.setMaximumWidth(420)
        self.preview.setStyleSheet(
            "background:#181825;border:1px solid #45475a;border-radius:6px;"
            "color:#6c7086;padding:8px;"
        )
        self.preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview.clicked.connect(self._open_preview)
        preview_box.addWidget(self.preview, 1)
        self.lbl_path = QLabel("Caminho: —")
        self.lbl_path.setWordWrap(True)
        self.lbl_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        preview_box.addWidget(self.lbl_path)
        path_buttons = QHBoxLayout()
        btn_open = QPushButton("🖼 Abrir foto")
        btn_open.clicked.connect(self._open_original)
        btn_folder = QPushButton("📁 Abrir pasta")
        btn_folder.clicked.connect(self._open_folder)
        path_buttons.addWidget(btn_open)
        path_buttons.addWidget(btn_folder)
        preview_box.addLayout(path_buttons)
        body.addLayout(preview_box, 1)
        lay.addLayout(body, 1)
        self.refresh()

    def refresh(self):
        if gui_tools.safety is None:
            return
        items, err = gui_tools._safe(gui_tools.safety.list_quarantine, self.con)
        if err:
            self.logger.error(f"[v13:outliers] {err}")
            return
        self._items = items or []
        self.table.setRowCount(len(self._items))
        for row, item in enumerate(self._items):
            path = str(item.get("file_path") or "")
            values = [item.get("id", ""), item.get("filename", ""), path,
                      item.get("parameter", ""), item.get("value", ""),
                      item.get("status", ""), item.get("reason", "")]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(str(value if value is not None else ""))
                if col == 2:
                    cell.setToolTip(path)
                self.table.setItem(row, col, cell)
        if self._items:
            self.table.selectRow(0)
        else:
            self._clear_preview("Nenhum outlier pendente")

    def _selected_rows(self):
        return sorted({idx.row() for idx in self.table.selectedIndexes()})

    def _selected_ids(self):
        return [self._items[r]["id"] for r in self._selected_rows() if r < len(self._items)]

    def _current_item(self):
        rows = self._selected_rows()
        return self._items[rows[0]] if rows and rows[0] < len(self._items) else None

    def _show_selected(self):
        item = self._current_item()
        if not item:
            self._clear_preview("Selecione um outlier")
            return
        path = Path(str(item.get("file_path") or ""))
        self.lbl_path.setText(f"Caminho: {path if str(path) != '.' else 'não registrado'}")
        pixmap = self._load_pixmap(path)
        self._pixmap = pixmap
        if pixmap and not pixmap.isNull():
            self.preview.setPixmap(pixmap.scaled(
                self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            ))
            self.preview.setText("")
        else:
            self.preview.setPixmap(QPixmap())
            self.preview.setText(
                "Miniatura indisponível para este RAW.\n"
                "Clique para abrir o arquivo no visualizador do sistema."
            )

    def _load_pixmap(self, path: Path):
        candidates = [path]
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}:
            candidates += [path.with_suffix(ext) for ext in (".jpg", ".jpeg", ".png")]
        for candidate in candidates:
            if candidate.is_file():
                pixmap = QPixmap(str(candidate))
                if not pixmap.isNull():
                    return pixmap
        return None

    def _clear_preview(self, text):
        self._pixmap = None
        self.preview.setPixmap(QPixmap())
        self.preview.setText(text)
        self.lbl_path.setText("Caminho: —")

    def _open_preview(self):
        item = self._current_item()
        if self._pixmap and not self._pixmap.isNull():
            dlg = QDialog(self)
            dlg.setWindowTitle(str(item.get("filename") or "Outlier"))
            dlg.resize(1000, 760)
            layout = QVBoxLayout(dlg)
            scroll = QScrollArea()
            label = QLabel()
            label.setAlignment(Qt.AlignCenter)
            label.setPixmap(self._pixmap)
            scroll.setWidget(label)
            scroll.setWidgetResizable(True)
            layout.addWidget(scroll)
            dlg.exec()
        else:
            self._open_original()

    def _open_original(self):
        item = self._current_item()
        path = Path(str(item.get("file_path") or "")) if item else None
        if not path or not path.exists():
            QMessageBox.warning(self, "Arquivo", "O arquivo original não foi encontrado nesse caminho.")
            return
        os.startfile(str(path)) if os.name == "nt" else subprocess.Popen(["xdg-open", str(path)])

    def _open_folder(self):
        item = self._current_item()
        path = Path(str(item.get("file_path") or "")) if item else None
        folder = path.parent if path else None
        if not folder or not folder.exists():
            QMessageBox.warning(self, "Pasta", "A pasta original não foi encontrada.")
            return
        os.startfile(str(folder)) if os.name == "nt" else subprocess.Popen(["xdg-open", str(folder)])

    def _resolve_selected(self, action):
        for item_id in self._selected_ids():
            _, err = gui_tools._safe(gui_tools.safety.resolve_quarantine_item, self.con, item_id, action)
            if err:
                self.logger.error(f"[v13:outliers] {err}")
        self.refresh()

    def _restore_selected(self):
        for item_id in self._selected_ids():
            def _insert(con, snapshot):
                source = snapshot.get("_source_catalog") or "quarantine_restore"
                return core.insert_photo_snapshot(con, source, snapshot)
            _, err = gui_tools._safe(gui_tools.safety.restore_quarantine_item, self.con, item_id, _insert)
            if err:
                self.logger.error(f"[v13:outliers] {err}")
        self.refresh()


gui_tools.OutliersTab = EnhancedOutliersTab


class MainWindow(gui.MainWindow):
    """Existing window plus a single-catalog, three-stage automatic pipeline."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lightroom Assistant — Fluxo Automático")
        self._pipeline_backup = True
        self._prepare_configuration_tabs()
        self._prepare_edit_tab()

    def _hide_catalog_controls(self, tab):
        for button in tab.findChildren(QPushButton):
            if "Selecionar catálogo" in button.text():
                button.hide()
        for label in tab.findChildren(QLabel):
            if label.text() == "Nenhum catálogo selecionado" or (
                getattr(tab, "_catalog", None) and label.text() == str(tab._catalog)
            ):
                label.hide()
        for name in ("btn_apply", "chk_backup", "progress", "lbl_result", "log_area"):
            widget = getattr(tab, name, None)
            if widget is not None:
                widget.hide()

    def _prepare_configuration_tabs(self):
        self._hide_catalog_controls(self.tab_preset)
        self._hide_catalog_controls(self.tab_exterior)
        for tab, text in (
            (self.tab_preset, "Configuração usada automaticamente na etapa 3 do fluxo Editar Catálogo."),
            (self.tab_exterior, "Configuração usada automaticamente na etapa 1 do fluxo Editar Catálogo."),
        ):
            label = QLabel(text)
            label.setWordWrap(True)
            label.setStyleSheet("color:#89b4fa;font-weight:bold;padding:6px;")
            tab.layout().insertWidget(1, label)

    def _prepare_edit_tab(self):
        tab = self.tab_editar
        tab.btn_edit.clicked.disconnect()
        tab.btn_edit.setText("⚡ Executar edição completa")
        tab.btn_edit.clicked.connect(self._start_pipeline)
        self.stage_labels = []
        stage_box = QVBoxLayout()
        for text in ("○ 1. Exterior / Interior", "○ 2. Exposição / Sombras", "○ 3. Presets por lente"):
            label = QLabel(text)
            label.setStyleSheet("color:#6c7086;font-weight:bold;")
            stage_box.addWidget(label)
            self.stage_labels.append(label)
        self.overall_progress = QProgressBar()
        self.overall_progress.setRange(0, 3)
        self.overall_progress.setValue(0)
        stage_box.addWidget(self.overall_progress)
        tab.layout().insertLayout(5, stage_box)

    def _set_stage(self, index, state):
        icons = {"waiting": "○", "running": "⟳", "done": "✔", "skipped": "—", "error": "✗"}
        colors = {"waiting": "#6c7086", "running": "#89b4fa", "done": "#a6e3a1", "skipped": "#f9e2af", "error": "#f38ba8"}
        names = ["1. Exterior / Interior", "2. Exposição / Sombras", "3. Presets por lente"]
        self.stage_labels[index].setText(f"{icons[state]} {names[index]}")
        self.stage_labels[index].setStyleSheet(f"color:{colors[state]};font-weight:bold;")

    def _start_pipeline(self):
        tab = self.tab_editar
        if tab._catalog is None:
            QMessageBox.warning(self, "Aviso", "Selecione um catálogo .lrcat primeiro.")
            return
        if core.count_photos(self.con) == 0:
            QMessageBox.warning(self, "Aviso", "O banco está vazio. Alimente o banco antes de editar.")
            return
        self._pipeline_backup = tab.chk_backup.isChecked()
        tab.btn_edit.setEnabled(False)
        tab.table.setRowCount(0)
        tab.lbl_result.setText("")
        tab.log_area.clear()
        self.overall_progress.setValue(0)
        for i in range(3):
            self._set_stage(i, "waiting")
        self._run_exterior()

    def _worker(self, fn, args, kwargs, stage, ok):
        self._set_stage(stage, "running")
        worker = gui.WorkerThread(fn, *args, **kwargs)
        self.tab_editar.thread = worker
        worker.progress.connect(self._update_progress)
        worker.done_ok.connect(ok)
        worker.done_err.connect(lambda msg: self._pipeline_error(stage, msg))
        worker.start()

    def _update_progress(self, a, b):
        self.tab_editar.progress.setMaximum(b or 1)
        self.tab_editar.progress.setValue(a)

    def _run_exterior(self):
        tab, ext = self.tab_editar, self.tab_exterior
        ext._catalog = tab._catalog
        if ext._xmp is None or not ext._xmp.is_file():
            tab._log("— Etapa 1 ignorada: nenhum preset exterior configurado.")
            self._set_stage(0, "skipped")
            self.overall_progress.setValue(1)
            self._run_exposure()
            return
        tab._log(f"[1/3] Classificando Exterior/Interior e aplicando '{ext._xmp.stem}'…")
        self._worker(
            core.apply_xmp_to_exterior_photos,
            (self.con, tab._catalog, gui.APP_DIR / "banco" / "_tmp", ext._xmp, self.logger),
            dict(score_threshold=ext._threshold_value(), apply_to_transition=ext.chk_transition.isChecked(), backup=self._pipeline_backup),
            0, self._after_exterior,
        )

    def _after_exterior(self, result):
        self.tab_editar._log(f"✔ Exterior/Interior concluído: {result.get('applied', 0)} aplicação(ões).")
        self._set_stage(0, "done")
        self.overall_progress.setValue(1)
        self._run_exposure()

    def _run_exposure(self):
        tab = self.tab_editar
        tab._log("[2/3] Aplicando sugestões de Exposição e Sombras…")
        exterior_will_backup = self.tab_exterior._xmp is not None and self.tab_exterior._xmp.is_file()
        self._worker(core.edit_catalog_inplace, (self.con, tab._catalog, self.logger),
                     dict(backup=self._pipeline_backup and not exterior_will_backup), 1, self._after_exposure)

    def _after_exposure(self, result):
        tab = self.tab_editar
        tab._populate_table(result.get("report_rows", []))
        tab._log(f"✔ Exposição/Sombras: {result.get('updated_count', 0)}/{result.get('total_photos', 0)} fotos.")
        self._set_stage(1, "done")
        self.overall_progress.setValue(2)
        self._run_lenses()

    def _run_lenses(self):
        tab, preset = self.tab_editar, self.tab_preset
        preset._catalog = tab._catalog
        lens_map = preset._get_lens_map()
        valid = {lens: path for lens, path in lens_map.items() if path.is_file()}
        if not valid:
            tab._log("— Etapa 3 ignorada: nenhum preset por lente válido configurado.")
            self._set_stage(2, "skipped")
            self._finish_pipeline()
            return
        alias_lookup = None
        try:
            if gui_tools.safety is not None:
                alias_lookup = gui_tools.safety.build_alias_lookup(self.con)
        except Exception:
            pass
        tab._log(f"[3/3] Aplicando {len(valid)} preset(s) por lente…")
        self._worker(core.apply_xmp_by_lens, (tab._catalog, valid, self.logger),
                     dict(backup=False, alias_lookup=alias_lookup), 2, self._after_lenses)

    def _after_lenses(self, result):
        self.tab_editar._log(f"✔ Presets por lente: {result.get('applied', 0)}/{result.get('total', 0)} fotos verificadas.")
        self._set_stage(2, "done")
        self._finish_pipeline()

    def _finish_pipeline(self):
        self.overall_progress.setValue(3)
        self.tab_editar.btn_edit.setEnabled(True)
        self.tab_editar.lbl_result.setText(f"✔ Fluxo completo concluído em {self.tab_editar._catalog.name}")
        self.tab_editar._log("✔ Processamento completo finalizado.")
        self.tab_editar.bank_changed.emit()

    def _pipeline_error(self, stage, msg):
        self._set_stage(stage, "error")
        self.tab_editar.btn_edit.setEnabled(True)
        first = msg.splitlines()[0]
        self.tab_editar._log(f"✗ Etapa {stage + 1} falhou: {first}")
        QMessageBox.critical(self, "Erro no processamento", first)


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(gui.STYLE)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
