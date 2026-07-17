"""
Lightroom Assistant - GUI (PySide6)
====================================
Interface modernizada com 3 abas:
  1. Banco      — alimenta o banco com catálogos editados (seleção livre de .lrcat)
  2. Editar     — aplica sugestões de Exp/Sombras/WB diretamente no catálogo
  3. Por Lente  — aplica preset XMP por lente a qualquer catálogo
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QSizePolicy, QSplitter, QStatusBar,
    QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
    QWidget, QCheckBox, QFrame,
)

import core

try:
    import gui_tools  # v12 — "Ferramentas e Saúde" tab area
except ImportError:  # pragma: no cover
    gui_tools = None

APP_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Configurações persistentes (salvas em banco/app_config.json)
# ---------------------------------------------------------------------------
class AppConfig:
    """Lê e escreve configurações do app em banco/app_config.json.

    Cada chamada a set() persiste imediatamente.  Segura para usar do
    thread principal — nunca chamada de threads de worker.
    """
    _PATH = APP_DIR / "banco" / "app_config.json"

    _DEFAULTS: dict = {
        "window_width":          880,
        "window_height":         700,
        "window_x":              None,
        "window_y":              None,
        "banco_update_mode":     False,
        "banco_catalogs":        [],
        "editar_backup":         True,
        "editar_catalog":        "",
        "editar_last_tab":       0,
        # v12 — safety/diagnostics/performance features. Default True: the
        # detector's own min-sample-size guard means it never flags
        # anything on small/empty banks, so a fresh bank behaves exactly
        # like v11 until enough history exists to say anything meaningful.
        "feature_outlier_detection": True,
        "feature_lens_aliases":      True,
        "feature_cache":             True,
    }

    def __init__(self):
        self._data: dict = dict(self._DEFAULTS)
        self._load()

    # ------------------------------------------------------------------
    def _load(self):
        if self._PATH.exists():
            try:
                raw = json.loads(self._PATH.read_text(encoding="utf-8"))
                self._data.update(raw)
            except Exception:
                pass  # invalid JSON → start from defaults

    def save(self):
        try:
            self._PATH.parent.mkdir(parents=True, exist_ok=True)
            self._PATH.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass  # best-effort — never crash the app over a config write

    def get(self, key: str, default=None):
        return self._data.get(key, default if default is not None else self._DEFAULTS.get(key))

    def set(self, key: str, value) -> None:
        self._data[key] = value
        self.save()

# ---------------------------------------------------------------------------
# Estilo visual (dark theme)
# ---------------------------------------------------------------------------
STYLE = """
QMainWindow, QWidget {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: 'Segoe UI', Arial, sans-serif;
    font-size: 13px;
}
QTabWidget::pane {
    border: 1px solid #313244;
    border-radius: 6px;
    margin-top: -1px;
}
QTabBar::tab {
    background: #181825;
    color: #bac2de;
    padding: 9px 22px;
    border-radius: 4px;
    margin: 2px 2px 0 2px;
    min-width: 100px;
}
QTabBar::tab:selected {
    background: #89b4fa;
    color: #1e1e2e;
    font-weight: bold;
}
QTabBar::tab:hover:!selected {
    background: #313244;
}
QPushButton {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 7px 16px;
    min-width: 120px;
}
QPushButton:hover  { background-color: #45475a; }
QPushButton:pressed { background-color: #585b70; }
QPushButton:disabled { background-color: #181825; color: #585b70; border-color: #313244; }
QPushButton#primary {
    background-color: #89b4fa;
    color: #1e1e2e;
    font-weight: bold;
    border: none;
}
QPushButton#primary:hover   { background-color: #b4d0fb; }
QPushButton#primary:disabled { background-color: #313244; color: #585b70; }
QPushButton#danger {
    background-color: #f38ba8;
    color: #1e1e2e;
    border: none;
}
QPushButton#danger:hover { background-color: #f7afc0; }
QTextEdit, QListWidget {
    background-color: #181825;
    color: #a6e3a1;
    font-family: Consolas, 'Courier New', monospace;
    font-size: 12px;
    border: 1px solid #313244;
    border-radius: 4px;
}
QListWidget { color: #cdd6f4; }
QTableWidget {
    background-color: #181825;
    color: #cdd6f4;
    border: 1px solid #313244;
    border-radius: 4px;
    gridline-color: #313244;
}
QTableWidget::item:selected { background-color: #313244; color: #cdd6f4; }
QHeaderView::section {
    background-color: #313244;
    color: #bac2de;
    padding: 5px;
    border: none;
    font-weight: bold;
}
QProgressBar {
    background-color: #313244;
    border-radius: 4px;
    text-align: center;
    color: #cdd6f4;
    min-height: 18px;
}
QProgressBar::chunk { background-color: #89b4fa; border-radius: 4px; }
QLabel { color: #cdd6f4; }
QLabel#section_title {
    font-size: 15px;
    font-weight: bold;
    color: #89b4fa;
    padding: 4px 0;
}
QLabel#stat_label {
    background-color: #313244;
    border-radius: 5px;
    padding: 5px 12px;
    color: #a6e3a1;
    font-weight: bold;
}
QLineEdit {
    background-color: #181825;
    border: 1px solid #45475a;
    border-radius: 4px;
    padding: 5px 8px;
    color: #cdd6f4;
}
QLineEdit:focus { border-color: #89b4fa; }
QCheckBox { color: #cdd6f4; spacing: 6px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #45475a;
    border-radius: 3px;
    background-color: #181825;
}
QCheckBox::indicator:checked {
    background-color: #89b4fa;
    border-color: #89b4fa;
}
QFrame#separator {
    background-color: #313244;
    max-height: 1px;
}
QStatusBar {
    background-color: #181825;
    color: #6c7086;
    font-size: 11px;
}
"""


def _sep() -> QFrame:
    f = QFrame()
    f.setObjectName("separator")
    f.setFrameShape(QFrame.HLine)
    return f


# ---------------------------------------------------------------------------
# Worker thread genérico
# ---------------------------------------------------------------------------
class WorkerThread(QThread):
    progress = Signal(int, int)
    log      = Signal(str)
    done_ok  = Signal(object)
    done_err = Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn     = fn
        self.args   = args
        self.kwargs = kwargs

    def run(self):
        try:
            result = self.fn(
                *self.args,
                progress_cb=lambda a, b: self.progress.emit(a, b),
                **self.kwargs,
            )
            self.done_ok.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.done_err.emit(f"{exc}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Aba 1 — Banco
# ---------------------------------------------------------------------------
class BancoTab(QWidget):
    status_msg = Signal(str)

    _CFG_PATH = APP_DIR / "banco" / "banco_tab_config.json"

    def __init__(self, con, logger, config: "AppConfig", parent=None):
        super().__init__(parent)
        self.con    = con
        self.logger = logger
        self.cfg    = config
        self._build()
        self._load_state()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(14, 14, 14, 14)

        title = QLabel("Alimentar Banco de Dados")
        title.setObjectName("section_title")
        lay.addWidget(title)

        info = QLabel(
            "Selecione catálogos <b>.lrcat</b> já editados por você no Lightroom. "
            "O assistente aprenderá Exposição e Sombras de cada foto. "
            "Para re-aprender a partir de correções mais recentes, ative "
            "<b>Modo de atualização</b> abaixo."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #bac2de; padding-bottom: 4px;")
        lay.addWidget(info)
        lay.addWidget(_sep())

        btns = QHBoxLayout()
        btn_add = QPushButton("＋  Adicionar catálogos…")
        btn_add.setObjectName("primary")
        btn_add.clicked.connect(self._add_catalogs)
        btn_clear = QPushButton("Limpar lista")
        btn_clear.clicked.connect(self._clear_list)
        btns.addWidget(btn_add)
        btns.addWidget(btn_clear)
        btns.addStretch()
        lay.addLayout(btns)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        lay.addWidget(self.list_widget)

        self.chk_update = QCheckBox(
            "🔄  Modo de atualização — selecione aqui o catálogo da pasta "
            "\"editado\" (o que o programa gerou) DEPOIS de você corrigi-lo "
            "no Lightroom. É esse arquivo que ensina o programa; catálogos "
            "que você editou do zero, sem passar pelo programa antes, não "
            "geram aprendizado."
        )
        self.chk_update.setChecked(False)
        self.chk_update.setStyleSheet("color: #f9e2af; padding: 4px 0;")
        lay.addWidget(self.chk_update)

        stats_row = QHBoxLayout()
        self.lbl_bank  = QLabel("Fotos no banco: 0")
        self.lbl_bank.setObjectName("stat_label")
        self.lbl_sess  = QLabel("Processados agora: 0")
        self.lbl_sess.setObjectName("stat_label")
        stats_row.addWidget(self.lbl_bank)
        stats_row.addWidget(self.lbl_sess)
        stats_row.addStretch()
        lay.addLayout(stats_row)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        lay.addWidget(self.progress)

        btn_feed = QPushButton("▶  Processar catálogos selecionados")
        btn_feed.setObjectName("primary")
        btn_feed.clicked.connect(self._feed)
        self.btn_feed = btn_feed
        lay.addWidget(btn_feed)

        lay.addWidget(_sep())

        # ------ Painel de aprendizado (bias report) ------
        bias_title = QLabel("🧠  Aprendizado por retroalimentação")
        bias_title.setStyleSheet("color: #89dceb; font-weight: bold; margin-top: 4px;")
        lay.addWidget(bias_title)

        bias_grid = QHBoxLayout()
        self.lbl_corrections = QLabel("Correções aprendidas: 0")
        self.lbl_corrections.setObjectName("stat_label")
        self.lbl_corrections.setToolTip(
            "Quantas vezes você reimportou um catálogo já editado e o programa "
            "comparou o que ele sugeriu com o que você de fato deixou. Cada "
            "reimportação vira uma \"correção\" usada para aprender."
        )
        self.lbl_bias_exp = QLabel("Bias exposição: —")
        self.lbl_bias_exp.setObjectName("stat_label")
        self.lbl_bias_exp.setToolTip(
            "Diferença média entre o que você ajustou e o que a IA sugeriu.\n"
            "Positivo = você geralmente aumenta mais a exposição do que a IA sugere.\n"
            "Negativo = você geralmente reduz mais.\n"
            "Quanto mais perto de 0, melhor a IA está acertando."
        )
        self.lbl_bias_sha = QLabel("Bias sombras: —")
        self.lbl_bias_sha.setObjectName("stat_label")
        self.lbl_bias_sha.setToolTip(
            "Mesma ideia do bias de exposição, mas para o slider de Sombras."
        )
        self.lbl_accuracy = QLabel("Taxa de acerto: —")
        self.lbl_accuracy.setObjectName("stat_label")
        self.lbl_accuracy.setToolTip(
            "Percentual de correções em que a sugestão da IA já estava bem "
            "próxima do que você deixou (dentro de ±0.15 EV de exposição e "
            "±5 de sombras) — ou seja, exigiu pouco ou nenhum ajuste."
        )
        self.lbl_bias_trend = QLabel("")
        bias_grid.addWidget(self.lbl_corrections)
        bias_grid.addWidget(self.lbl_bias_exp)
        bias_grid.addWidget(self.lbl_bias_sha)
        bias_grid.addWidget(self.lbl_accuracy)
        bias_grid.addWidget(self.lbl_bias_trend)
        bias_grid.addStretch()
        lay.addLayout(bias_grid)

        confidence_row = QHBoxLayout()
        lbl_conf_caption = QLabel("Confiança do aprendizado:")
        lbl_conf_caption.setObjectName("stat_label")
        lbl_conf_caption.setToolTip(
            "Quanto o programa confia no bias aprendido antes de aplicá-lo às "
            "próximas sugestões. Sobe conforme mais correções são registradas; "
            "com poucas correções, a IA ainda aplica o ajuste com cautela para "
            "não reagir a ruído/casos isolados."
        )
        self.bar_confidence = QProgressBar()
        self.bar_confidence.setRange(0, 100)
        self.bar_confidence.setValue(0)
        self.bar_confidence.setFixedWidth(160)
        self.bar_confidence.setTextVisible(True)
        confidence_row.addWidget(lbl_conf_caption)
        confidence_row.addWidget(self.bar_confidence)

        self.lbl_sparkline = QLabel("")
        self.lbl_sparkline.setObjectName("stat_label")
        self.lbl_sparkline.setToolTip(
            "Tendência do erro de exposição nas últimas correções (esquerda = "
            "mais antiga, direita = mais recente). Barras mais baixas/perto do "
            "meio = erro menor."
        )
        confidence_row.addWidget(self.lbl_sparkline)
        confidence_row.addStretch()

        btn_bias_details = QPushButton("📊  Ver detalhes")
        btn_bias_details.clicked.connect(self._show_bias_details)
        self.btn_bias_details = btn_bias_details
        confidence_row.addWidget(btn_bias_details)

        btn_bias_export = QPushButton("⬇  Exportar CSV")
        btn_bias_export.clicked.connect(self._export_bias_csv)
        self.btn_bias_export = btn_bias_export
        confidence_row.addWidget(btn_bias_export)

        lay.addLayout(confidence_row)

        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMaximumHeight(140)
        lay.addWidget(self.log_area)

        self._session = 0
        self.refresh_stats()

    def _log(self, msg: str):
        self.log_area.append(msg)
        self.logger.info(msg)
        self.status_msg.emit(msg)

    def refresh_stats(self):
        self.lbl_bank.setText(f"Fotos no banco: {core.count_photos(self.con)}")
        self.lbl_sess.setText(f"Processados agora: {self._session}")
        self._refresh_bias()

    # Thresholds for color-coding bias magnitude - green/yellow/red bands so
    # the user can tell "healthy" from "needs attention" at a glance instead
    # of doing mental math on raw numbers.
    _BIAS_COLOR_GOOD   = "#a6e3a1"  # green
    _BIAS_COLOR_WARN   = "#f9e2af"  # yellow
    _BIAS_COLOR_BAD    = "#f38ba8"  # red
    _BIAS_COLOR_NEUTRAL = "#bac2de"  # gray-blue (no data yet)

    @staticmethod
    def _bias_color(abs_error: float, good: float, warn: float) -> str:
        if abs_error <= good:
            return BancoTab._BIAS_COLOR_GOOD
        if abs_error <= warn:
            return BancoTab._BIAS_COLOR_WARN
        return BancoTab._BIAS_COLOR_BAD

    @staticmethod
    def _sparkline(values: list, lo: float = None, hi: float = None) -> str:
        """Render a list of numbers as a tiny unicode bar chart (▁▂▃▄▅▆▇█),
        centered so 0 sits in the middle of the block range - lets the user
        see at a glance whether recent errors trend toward over/under."""
        blocks = "▁▂▃▄▅▆▇█"
        if not values:
            return ""
        if lo is None:
            lo = min(values)
        if hi is None:
            hi = max(values)
        span = max(abs(lo), abs(hi), 1e-6)
        lo, hi = -span, span
        out = []
        for v in values:
            v = max(lo, min(hi, v))
            idx = int((v - lo) / (hi - lo) * (len(blocks) - 1))
            out.append(blocks[idx])
        return "".join(out)

    def _refresh_bias(self):
        try:
            rep = core.get_bias_report(self.con)
        except Exception:
            return
        self._last_bias_report = rep
        n = rep["total"]
        self.lbl_corrections.setText(f"Correções aprendidas: {n}  ({rep['ai_suggestions']} sugestões guardadas)")
        self.bar_confidence.setValue(int(round((rep.get("confidence") or 0.0) * 100)))

        if n == 0:
            self.lbl_bias_exp.setText("Bias exposição: ainda sem dados")
            self.lbl_bias_exp.setStyleSheet(f"color: {self._BIAS_COLOR_NEUTRAL};")
            self.lbl_bias_sha.setText("Bias sombras: ainda sem dados")
            self.lbl_bias_sha.setStyleSheet(f"color: {self._BIAS_COLOR_NEUTRAL};")
            self.lbl_accuracy.setText("Taxa de acerto: —")
            self.lbl_accuracy.setStyleSheet(f"color: {self._BIAS_COLOR_NEUTRAL};")
            self.lbl_bias_trend.setText("")
            self.lbl_sparkline.setText("")
            self.btn_bias_details.setEnabled(False)
            self.btn_bias_export.setEnabled(False)
            return

        self.btn_bias_details.setEnabled(True)
        self.btn_bias_export.setEnabled(True)

        exp = rep["mean_error_exp"] or 0.0
        sha = rep["mean_error_sha"] or 0.0
        mae_exp = rep["mae_exp"] or 0.0
        mae_sha = rep["mae_sha"] or 0.0
        self.lbl_bias_exp.setText(
            f"Bias exposição: {exp:+.2f} EV (típico ±{mae_exp:.2f})"
            + (f"  →  recente: {rep['recent_error_exp']:+.2f}" if rep["recent_error_exp"] is not None else "")
        )
        self.lbl_bias_exp.setStyleSheet(f"color: {self._bias_color(mae_exp, 0.10, 0.30)};")
        self.lbl_bias_sha.setText(
            f"Bias sombras: {sha:+.1f} (típico ±{mae_sha:.1f})"
            + (f"  →  recente: {rep['recent_error_sha']:+.1f}" if rep["recent_error_sha"] is not None else "")
        )
        self.lbl_bias_sha.setStyleSheet(f"color: {self._bias_color(mae_sha, 4.0, 12.0)};")

        if rep["accuracy_rate"] is not None:
            acc_pct = rep["accuracy_rate"] * 100.0
            self.lbl_accuracy.setText(f"Taxa de acerto: {acc_pct:.0f}%")
            self.lbl_accuracy.setStyleSheet(
                f"color: {self._bias_color(100 - acc_pct, 30, 55)};"
            )
        else:
            self.lbl_accuracy.setText("Taxa de acerto: —")
            self.lbl_accuracy.setStyleSheet(f"color: {self._BIAS_COLOR_NEUTRAL};")

        if rep["improving"]:
            self.lbl_bias_trend.setText("📈  melhorando")
            self.lbl_bias_trend.setStyleSheet(f"color: {self._BIAS_COLOR_GOOD};")
        else:
            self.lbl_bias_trend.setText("📊  estável")
            self.lbl_bias_trend.setStyleSheet(f"color: {self._BIAS_COLOR_NEUTRAL};")

        exp_series = [t[0] for t in rep.get("trend_series", []) if t[0] is not None]
        self.lbl_sparkline.setText(self._sparkline(exp_series) if exp_series else "")

    def _show_bias_details(self):
        rep = getattr(self, "_last_bias_report", None) or core.get_bias_report(self.con)
        dlg = QDialog(self)
        dlg.setWindowTitle("Detalhes do aprendizado por retroalimentação")
        dlg.resize(560, 420)
        v = QVBoxLayout(dlg)

        summary = QLabel(
            f"Total de correções: {rep['total']}  |  "
            f"Confiança: {(rep.get('confidence') or 0.0) * 100:.0f}%  |  "
            f"Taxa de acerto: {(rep['accuracy_rate'] or 0.0) * 100:.0f}%"
        )
        summary.setStyleSheet("font-weight: bold;")
        v.addWidget(summary)

        def _table_for(title: str, rows: list) -> None:
            v.addWidget(QLabel(title))
            table = QTableWidget(len(rows), 5)
            table.setHorizontalHeaderLabels(
                ["Nome", "Nº correções", "Erro médio Exp.", "Erro médio Sombras", "Erro típico (MAE)"]
            )
            for i, r in enumerate(rows):
                table.setItem(i, 0, QTableWidgetItem(str(r["label"])))
                table.setItem(i, 1, QTableWidgetItem(str(r["count"])))
                table.setItem(i, 2, QTableWidgetItem(f"{r['mean_error_exp']:+.2f} EV"))
                table.setItem(i, 3, QTableWidgetItem(f"{r['mean_error_sha']:+.1f}"))
                table.setItem(i, 4, QTableWidgetItem(f"±{r['mae_exp']:.2f} EV / ±{r['mae_sha']:.1f}"))
            table.horizontalHeader().setStretchLastSection(True)
            table.setEditTriggers(QTableWidget.NoEditTriggers)
            v.addWidget(table)

        if rep["by_preset"]:
            _table_for("Erro por preset", rep["by_preset"])
        else:
            v.addWidget(QLabel("Sem correções suficientes agrupadas por preset ainda."))

        if rep["by_camera"]:
            _table_for("Erro por câmera", rep["by_camera"])
        else:
            v.addWidget(QLabel("Sem correções suficientes agrupadas por câmera ainda."))

        btn_close = QPushButton("Fechar")
        btn_close.clicked.connect(dlg.accept)
        v.addWidget(btn_close)
        dlg.exec()

    def _export_bias_csv(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Exportar histórico de aprendizado", "historico_aprendizado.csv", "CSV (*.csv)"
        )
        if not path:
            return
        try:
            n = core.export_bias_history_csv(self.con, path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Exportar CSV", f"Não foi possível exportar: {exc}")
            return
        QMessageBox.information(self, "Exportar CSV", f"{n} correção(ões) exportada(s) para:\n{path}")

    # ----------------------------------------------------------------- state
    def _load_state(self):
        """Restore catalog list and checkbox state from AppConfig."""
        self.chk_update.setChecked(self.cfg.get("banco_update_mode", False))
        catalogs = self.cfg.get("banco_catalogs", [])
        for cat_str in catalogs:
            p = Path(cat_str)
            if p.exists():
                item = QListWidgetItem(str(p))
                item.setToolTip(str(p))
                self.list_widget.addItem(item)
        self.chk_update.stateChanged.connect(self._save_state)

    def _save_state(self):
        """Persist catalog list and checkbox state to AppConfig."""
        catalogs = [
            self.list_widget.item(i).text()
            for i in range(self.list_widget.count())
        ]
        self.cfg.set("banco_update_mode", self.chk_update.isChecked())
        self.cfg.set("banco_catalogs", catalogs)

    def _add_catalogs(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Selecionar catálogos Lightroom", "",
            "Lightroom Catalog (*.lrcat)"
        )
        for f in files:
            p = Path(f)
            exists = [self.list_widget.item(i).text()
                      for i in range(self.list_widget.count())]
            if str(p) not in exists:
                item = QListWidgetItem(str(p))
                item.setToolTip(str(p))
                self.list_widget.addItem(item)
        self._save_state()

    def _clear_list(self):
        self.list_widget.clear()
        self._save_state()

    def _feed(self):
        catalogs = [
            Path(self.list_widget.item(i).text())
            for i in range(self.list_widget.count())
        ]
        if not catalogs:
            QMessageBox.information(self, "Aviso", "Adicione catálogos à lista primeiro.")
            return
        self.btn_feed.setEnabled(False)
        self._log(f"Iniciando alimentação com {len(catalogs)} catálogo(s)…")
        self._run_next(catalogs, 0)

    def _run_next(self, catalogs: list[Path], idx: int, force_update: bool = False):
        if idx >= len(catalogs):
            self.btn_feed.setEnabled(True)
            self.refresh_stats()
            self._log("✔ Alimentação concluída.")
            return

        cat = catalogs[idx]
        workdir = APP_DIR / "banco" / "_tmp"
        update_mode = force_update or self.chk_update.isChecked()

        def _prog(a, b):
            self.progress.setMaximum(b or 1)
            self.progress.setValue(a)

        def _ok(_):
            self._session += 1
            self.refresh_stats()
            mode_str = " (atualizado)" if update_mode else ""
            self._log(f"✔ {cat.name}{mode_str}")
            self._run_next(catalogs, idx + 1)

        def _err(msg):
            first_line = msg.splitlines()[0]
            # Duplicate catalog detected — offer to re-learn from it
            if first_line.startswith("CATALOG_ALREADY_EXISTS:"):
                n = first_line.split(":")[1]
                answer = QMessageBox.question(
                    self,
                    "Catálogo já importado",
                    f"<b>{cat.name}</b> já está no banco ({n} fotos).<br><br>"
                    "Deseja <b>atualizar</b> o banco com as edições mais recentes "
                    "deste catálogo? (Os valores antigos serão substituídos.)",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer == QMessageBox.Yes:
                    self._log(f"↺ Re-aprendendo: {cat.name}…")
                    self._run_next_single(cat, catalogs, idx, force_update=True)
                    return
                else:
                    self._log(f"— {cat.name}: ignorado (já importado).")
            else:
                self._log(f"✗ {cat.name}: {first_line}")
            self._run_next(catalogs, idx + 1)

        self.thread = WorkerThread(
            core.feed_database_from_catalog,
            self.con, cat, workdir, self.logger,
            force_update=update_mode,
            outlier_check=self.cfg.get("feature_outlier_detection", True),
        )
        self.thread.progress.connect(_prog)
        self.thread.done_ok.connect(_ok)
        self.thread.done_err.connect(_err)
        self.thread.start()

    def _run_next_single(self, cat: Path, catalogs: list[Path], idx: int, force_update: bool):
        """Re-run a single catalog with force_update=True, then continue the queue."""
        workdir = APP_DIR / "banco" / "_tmp"

        def _prog(a, b):
            self.progress.setMaximum(b or 1)
            self.progress.setValue(a)

        def _ok(_):
            self._session += 1
            self.refresh_stats()
            self._log(f"✔ {cat.name} (banco atualizado — novos valores aprendidos)")
            self._run_next(catalogs, idx + 1)

        def _err(msg):
            self._log(f"✗ {cat.name} (re-importação): {msg.splitlines()[0]}")
            self._run_next(catalogs, idx + 1)

        self.thread = WorkerThread(
            core.feed_database_from_catalog,
            self.con, cat, workdir, self.logger,
            force_update=True,
            outlier_check=self.cfg.get("feature_outlier_detection", True),
        )
        self.thread.progress.connect(_prog)
        self.thread.done_ok.connect(_ok)
        self.thread.done_err.connect(_err)
        self.thread.start()


# ---------------------------------------------------------------------------
# Aba 2 — Editar Catálogo
# ---------------------------------------------------------------------------
class EditarTab(QWidget):
    status_msg   = Signal(str)
    bank_changed = Signal()

    def __init__(self, con, logger, config: "AppConfig", parent=None):
        super().__init__(parent)
        self.con     = con
        self.logger  = logger
        self.cfg     = config
        self._catalog: Path | None = None
        self._build()
        self._load_state()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(14, 14, 14, 14)

        title = QLabel("Editar Catálogo")
        title.setObjectName("section_title")
        lay.addWidget(title)

        info = QLabel(
            "Selecione qualquer <b>.lrcat</b> — o assistente aplica sugestões de "
            "Exposição e Sombras <b>diretamente no arquivo</b>. "
            "O Balanço de Branco é preservado (ajuste manual no Lightroom)."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #bac2de; padding-bottom: 4px;")
        lay.addWidget(info)
        lay.addWidget(_sep())

        sel_row = QHBoxLayout()
        btn_sel = QPushButton("📂  Selecionar catálogo…")
        btn_sel.clicked.connect(self._select_catalog)
        self.lbl_cat = QLabel("Nenhum catálogo selecionado")
        self.lbl_cat.setStyleSheet("color: #6c7086; font-style: italic;")
        self.lbl_cat.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        sel_row.addWidget(btn_sel)
        sel_row.addWidget(self.lbl_cat, stretch=1)
        lay.addLayout(sel_row)

        self.chk_backup = QCheckBox("Fazer backup (.lrcat.bak) antes de editar  (recomendado)")
        self.chk_backup.setChecked(True)
        lay.addWidget(self.chk_backup)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        lay.addWidget(self.progress)

        btn_edit = QPushButton("⚡  Aplicar sugestões de exposição / sombras")
        btn_edit.setObjectName("primary")
        btn_edit.clicked.connect(self._edit)
        self.btn_edit = btn_edit
        lay.addWidget(btn_edit)

        lay.addWidget(_sep())

        self.lbl_result = QLabel("")
        self.lbl_result.setWordWrap(True)
        self.lbl_result.setStyleSheet("color: #a6e3a1; font-weight: bold;")
        lay.addWidget(self.lbl_result)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Arquivo", "Exposure", "Shadows", "Nota"]
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self.table, stretch=1)

        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMaximumHeight(130)
        lay.addWidget(self.log_area)

    def _log(self, msg: str):
        self.log_area.append(msg)
        self.logger.info(msg)
        self.status_msg.emit(msg)

    # ----------------------------------------------------------------- state
    def _load_state(self):
        cat_str = self.cfg.get("editar_catalog", "")
        if cat_str and Path(cat_str).exists():
            self._catalog = Path(cat_str)
            self.lbl_cat.setText(cat_str)
            self.lbl_cat.setStyleSheet("color: #cdd6f4;")
        self.chk_backup.setChecked(self.cfg.get("editar_backup", True))
        self.chk_backup.stateChanged.connect(
            lambda: self.cfg.set("editar_backup", self.chk_backup.isChecked())
        )

    def _select_catalog(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Selecionar catálogo Lightroom", "",
            "Lightroom Catalog (*.lrcat)"
        )
        if f:
            self._catalog = Path(f)
            self.lbl_cat.setText(str(self._catalog))
            self.lbl_cat.setStyleSheet("color: #cdd6f4;")
            self.lbl_result.setText("")
            self.table.setRowCount(0)
            self.cfg.set("editar_catalog", str(self._catalog))

    def _edit(self):
        if self._catalog is None:
            QMessageBox.warning(self, "Aviso", "Selecione um catálogo .lrcat primeiro.")
            return
        if core.count_photos(self.con) == 0:
            QMessageBox.warning(
                self, "Aviso",
                "O banco está vazio.\nAlimente o banco com catálogos editados antes de usar esta função."
            )
            return

        backup = self.chk_backup.isChecked()
        if not backup:
            confirm = QMessageBox.question(
                self, "Atenção",
                "Você optou por NÃO fazer backup.\n"
                "As alterações serão escritas diretamente no catálogo e não poderão ser desfeitas.\n\n"
                "Deseja continuar?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return

        self.btn_edit.setEnabled(False)
        self.table.setRowCount(0)
        self.lbl_result.setText("")
        self._log(f"Editando: {self._catalog.name}…")

        def _prog(a, b):
            self.progress.setMaximum(b or 1)
            self.progress.setValue(a)

        def _ok(result: dict):
            self.btn_edit.setEnabled(True)
            n = result["updated_count"]
            t = result["total_photos"]
            self.lbl_result.setText(f"✔ {n} / {t} fotos atualizadas em {self._catalog.name}")
            self._log(f"✔ Concluído — {n}/{t} fotos.")
            self._populate_table(result.get("report_rows", []))
            self.bank_changed.emit()

        def _err(msg: str):
            self.btn_edit.setEnabled(True)
            self._log(f"✗ Erro: {msg.splitlines()[0]}")
            QMessageBox.critical(self, "Erro", msg.splitlines()[0])

        self.thread = WorkerThread(
            core.edit_catalog_inplace,
            self.con, self._catalog, self.logger,
            backup=backup,
        )
        self.thread.progress.connect(_prog)
        self.thread.done_ok.connect(_ok)
        self.thread.done_err.connect(_err)
        self.thread.start()

    def _populate_table(self, rows):
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            fname, exp, sha, _temp, _tint, note = row   # WB columns ignored
            for c, val in enumerate([fname, exp, sha, note]):
                if val is None:
                    text = "—"
                elif c in (1, 2):
                    text = f"{val:+.2f}"
                else:
                    text = str(val)
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter if c != 3 else Qt.AlignLeft)
                self.table.setItem(r, c, item)


# ---------------------------------------------------------------------------
# Aba 3 — Presets por Lente
# ---------------------------------------------------------------------------
class PresetLenteTab(QWidget):
    status_msg = Signal(str)

    _SETTINGS_PATH = APP_DIR / "banco" / "preset_lentes_config.json"

    def __init__(self, con, logger, parent=None):
        super().__init__(parent)
        self.con      = con
        self.logger   = logger
        self._catalog: Path | None = None
        self._loading = False          # suprime saves automáticos durante _load_settings
        self._build()
        self._load_settings()          # restaura estado salvo anteriormente

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(14, 14, 14, 14)

        title = QLabel("Aplicar Presets por Lente (XMP)")
        title.setObjectName("section_title")
        lay.addWidget(title)

        info = QLabel(
            "Associe um arquivo <b>.xmp</b> a cada lente. "
            "O assistente aplica o preset em <em>todas</em> as fotos do catálogo tiradas com aquela lente. "
            "Isso é independente das sugestões de exposição."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #bac2de; padding-bottom: 4px;")
        lay.addWidget(info)
        lay.addWidget(_sep())

        # --- tabela lente / xmp ---
        tbl_btns = QHBoxLayout()
        btn_add_row = QPushButton("＋  Nova linha")
        btn_add_row.clicked.connect(self._add_row)
        btn_del_row = QPushButton("－  Remover linha")
        btn_del_row.setObjectName("danger")
        btn_del_row.clicked.connect(self._del_row)
        btn_load_lenses = QPushButton("🔍  Carregar lentes do banco")
        btn_load_lenses.clicked.connect(self._load_from_bank)
        tbl_btns.addWidget(btn_add_row)
        tbl_btns.addWidget(btn_del_row)
        tbl_btns.addWidget(btn_load_lenses)
        tbl_btns.addStretch()
        lay.addLayout(tbl_btns)

        self.tbl = QTableWidget(0, 2)
        self.tbl.setHorizontalHeaderLabels(["Lente (nome ou parte do nome)", "Arquivo XMP"])
        self.tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setMinimumHeight(180)
        lay.addWidget(self.tbl)

        lay.addWidget(_sep())

        # --- seleção do catálogo ---
        cat_row = QHBoxLayout()
        btn_sel_cat = QPushButton("📂  Selecionar catálogo…")
        btn_sel_cat.clicked.connect(self._select_catalog)
        self.lbl_cat = QLabel("Nenhum catálogo selecionado")
        self.lbl_cat.setStyleSheet("color: #6c7086; font-style: italic;")
        self.lbl_cat.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        cat_row.addWidget(btn_sel_cat)
        cat_row.addWidget(self.lbl_cat, stretch=1)
        lay.addLayout(cat_row)

        self.chk_backup = QCheckBox("Fazer backup (.lrcat.bak) antes de aplicar  (recomendado)")
        self.chk_backup.setChecked(True)
        self.chk_backup.stateChanged.connect(lambda _: self._save_settings())
        lay.addWidget(self.chk_backup)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        lay.addWidget(self.progress)

        btn_apply = QPushButton("⚡  Aplicar presets ao catálogo")
        btn_apply.setObjectName("primary")
        btn_apply.clicked.connect(self._apply)
        self.btn_apply = btn_apply
        lay.addWidget(btn_apply)

        self.lbl_result = QLabel("")
        self.lbl_result.setWordWrap(True)
        self.lbl_result.setStyleSheet("color: #a6e3a1; font-weight: bold;")
        lay.addWidget(self.lbl_result)

        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMaximumHeight(130)
        lay.addWidget(self.log_area)

    def _log(self, msg: str):
        self.log_area.append(msg)
        self.logger.info(msg)
        self.status_msg.emit(msg)

    def _add_row(self, lens_name: str = "", xmp_path: str = ""):
        row = self.tbl.rowCount()
        self.tbl.insertRow(row)

        lens_edit = QLineEdit(lens_name)
        lens_edit.setPlaceholderText("ex: RF 85mm, 50mm f/1.4, FE 24-70…")
        lens_edit.textChanged.connect(lambda _: self._save_settings())
        self.tbl.setCellWidget(row, 0, lens_edit)

        xmp_widget = QWidget()
        xmp_lay = QHBoxLayout(xmp_widget)
        xmp_lay.setContentsMargins(2, 2, 2, 2)
        xmp_lay.setSpacing(4)
        xmp_field = QLineEdit(xmp_path)
        xmp_field.setPlaceholderText("Caminho do arquivo .xmp…")
        xmp_field.setReadOnly(True)
        btn_browse = QPushButton("…")
        btn_browse.setFixedWidth(34)
        btn_browse.setToolTip("Procurar arquivo XMP")

        def _browse(checked=False, field=xmp_field):
            f, _ = QFileDialog.getOpenFileName(
                self, "Selecionar preset XMP", "",
                "Lightroom Preset (*.xmp);;All files (*.*)"
            )
            if f:
                field.setText(f)
                self._save_settings()

        btn_browse.clicked.connect(_browse)
        xmp_lay.addWidget(xmp_field, stretch=1)
        xmp_lay.addWidget(btn_browse)
        self.tbl.setCellWidget(row, 1, xmp_widget)
        self.tbl.setRowHeight(row, 36)

    def _del_row(self):
        rows = sorted({idx.row() for idx in self.tbl.selectedIndexes()}, reverse=True)
        for r in rows:
            self.tbl.removeRow(r)
        if not rows:
            n = self.tbl.rowCount()
            if n > 0:
                self.tbl.removeRow(n - 1)
        self._save_settings()

    def _load_from_bank(self):
        lenses = core.list_known_lenses(self.con)
        if not lenses:
            QMessageBox.information(self, "Banco vazio",
                "Nenhuma lente encontrada no banco.\nAlimente o banco com catálogos primeiro.")
            return
        existing = self._get_lens_map()
        added = 0
        for lens in lenses:
            if lens not in existing:
                self._add_row(lens_name=lens)
                added += 1
        self._log(f"{len(lenses)} lente(s) no banco — {added} nova(s) adicionada(s) à lista.")
        self._save_settings()

    def _select_catalog(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Selecionar catálogo Lightroom", "",
            "Lightroom Catalog (*.lrcat)"
        )
        if f:
            self._catalog = Path(f)
            self.lbl_cat.setText(str(self._catalog))
            self.lbl_cat.setStyleSheet("color: #cdd6f4;")
            self._save_settings()

    def _save_settings(self) -> None:
        """Persiste o estado atual da aba (lentes, XMPs, catálogo, backup) em JSON."""
        if self._loading:
            return
        rows = []
        for r in range(self.tbl.rowCount()):
            lens_w = self.tbl.cellWidget(r, 0)
            xmp_w  = self.tbl.cellWidget(r, 1)
            if not lens_w or not xmp_w:
                continue
            lens = lens_w.text().strip() if isinstance(lens_w, QLineEdit) else ""
            xmp_field = xmp_w.findChild(QLineEdit)
            xmp = xmp_field.text().strip() if xmp_field else ""
            rows.append({"lens": lens, "xmp": xmp})
        data = {
            "catalog": str(self._catalog) if self._catalog else "",
            "backup":  self.chk_backup.isChecked(),
            "rows":    rows,
        }
        try:
            self._SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._SETTINGS_PATH.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            self.logger.warning(f"Não foi possível salvar configurações de preset: {exc}")

    def _load_settings(self) -> None:
        """Restaura o estado salvo anteriormente (chamado uma vez no __init__)."""
        if not self._SETTINGS_PATH.exists():
            return
        try:
            data = json.loads(self._SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            self.logger.warning(f"Não foi possível carregar configurações de preset: {exc}")
            return
        self._loading = True
        try:
            for row_data in data.get("rows", []):
                self._add_row(
                    lens_name=row_data.get("lens", ""),
                    xmp_path=row_data.get("xmp", ""),
                )
            cat = data.get("catalog", "")
            if cat:
                p = Path(cat)
                if p.exists():           # só restaura se o arquivo ainda existe
                    self._catalog = p
                    self.lbl_cat.setText(cat)
                    self.lbl_cat.setStyleSheet("color: #cdd6f4;")
            self.chk_backup.setChecked(data.get("backup", True))
        finally:
            self._loading = False

    def _get_lens_map(self) -> dict[str, Path]:
        lens_map: dict[str, Path] = {}
        for row in range(self.tbl.rowCount()):
            lens_w = self.tbl.cellWidget(row, 0)
            xmp_w  = self.tbl.cellWidget(row, 1)
            if not lens_w or not xmp_w:
                continue
            lens_name = lens_w.text().strip() if isinstance(lens_w, QLineEdit) else (lens_w.findChild(QLineEdit) or lens_w).text().strip()
            xmp_field = xmp_w.findChild(QLineEdit)
            xmp_path  = xmp_field.text().strip() if xmp_field else ""
            if lens_name and xmp_path:
                lens_map[lens_name] = Path(xmp_path)
        return lens_map

    def _apply(self):
        if self._catalog is None:
            QMessageBox.warning(self, "Aviso", "Selecione um catálogo .lrcat primeiro.")
            return

        lens_map = self._get_lens_map()
        if not lens_map:
            QMessageBox.warning(self, "Aviso",
                "Adicione ao menos uma linha com lente e preset XMP.")
            return

        for lens, xmp_path in lens_map.items():
            if not xmp_path.is_file():
                QMessageBox.warning(self, "Arquivo não encontrado",
                    f"Preset XMP para '{lens}' não encontrado:\n{xmp_path}")
                return

        backup = self.chk_backup.isChecked()
        self.btn_apply.setEnabled(False)
        self.lbl_result.setText("")
        self._log(f"Aplicando {len(lens_map)} preset(s) em {self._catalog.name}…")

        def _prog(a, b):
            self.progress.setMaximum(b or 1)
            self.progress.setValue(a)

        def _ok(result: dict):
            self.btn_apply.setEnabled(True)
            a, s, t = result["applied"], result["skipped"], result["total"]
            identical = result.get("identical", 0)
            sem_metadata = result.get("sem_metadata", 0)
            sem_preset = result.get("sem_preset", 0)
            erros = result.get("erros", 0)
            update_sem_efeito = result.get("update_sem_efeito", 0)
            verificacao_falhou = result.get("verificacao_falhou", 0)
            self.lbl_result.setText(
                f"✔ {a} foto(s) atualizadas e verificadas, {s} ignoradas — total {t}."
            )
            self._log(
                f"✔ Presets aplicados e confirmados no catálogo: {a}/{t}  "
                f"(já idênticas: {identical}, sem metadado de lente: {sem_metadata}, "
                f"sem preset correspondente: {sem_preset}, "
                f"update sem efeito: {update_sem_efeito}, "
                f"verificação falhou: {verificacao_falhou}, erros: {erros})."
            )
            for row in result.get("lens_comparison", []):
                status = "OK" if row["encontrado_no_catalogo"] else "SEM CORRESPONDÊNCIA"
                self._log(f"   • {row['configurado']} → {status} ({row['fotos_correspondentes']} foto(s))")
            unmatched = result.get("unmatched_catalog_lenses") or []
            if unmatched:
                self._log(
                    "   Lentes no catálogo sem preset configurado: " + ", ".join(unmatched)
                )
            report_path = result.get("report_path")
            if report_path:
                self._log(f"   Relatório detalhado de fotos ignoradas: {report_path}")

        def _err(msg: str):
            self.btn_apply.setEnabled(True)
            self._log(f"✗ Erro: {msg.splitlines()[0]}")
            QMessageBox.critical(self, "Erro", msg.splitlines()[0])

        alias_lookup = None
        try:
            import safety as _safety
            if AppConfig().get("feature_lens_aliases", True):
                alias_lookup = _safety.build_alias_lookup(self.con)
        except Exception:
            alias_lookup = None  # v12 layer unavailable — fall back to v11 matching only

        self.thread = WorkerThread(
            core.apply_xmp_by_lens,
            self._catalog, lens_map, self.logger,
            backup=backup,
            alias_lookup=alias_lookup,
        )
        self.thread.progress.connect(_prog)
        self.thread.done_ok.connect(_ok)
        self.thread.done_err.connect(_err)
        self.thread.start()


# ---------------------------------------------------------------------------
# Aba 4 — Presets por Área Externa
# ---------------------------------------------------------------------------
class ExteriorTab(QWidget):
    status_msg = Signal(str)

    _SETTINGS_PATH = APP_DIR / "banco" / "exterior_config.json"

    def __init__(self, con, logger, parent=None):
        super().__init__(parent)
        self.con      = con
        self.logger   = logger
        self._catalog: Path | None = None
        self._xmp:     Path | None = None
        self._loading  = False
        self._build()
        self._load_settings()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(14, 14, 14, 14)

        title = QLabel("Presets para Fotos Externas")
        title.setObjectName("section_title")
        lay.addWidget(title)

        info = QLabel(
            "Classifica automaticamente as fotos como <b>exterior</b>, "
            "<b>transição</b> (sacada, varanda…) ou <b>interior</b> usando GPS, EXIF "
            "e features visuais do banco — depois aplica um preset XMP às fotos externas."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #bac2de; padding-bottom: 4px;")
        lay.addWidget(info)
        lay.addWidget(_sep())

        # Preset XMP
        xmp_row = QHBoxLayout()
        xmp_row.addWidget(QLabel("Preset XMP:"))
        self.lbl_xmp = QLabel("Nenhum arquivo selecionado")
        self.lbl_xmp.setStyleSheet("color: #6c7086; font-style: italic;")
        self.lbl_xmp.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        btn_xmp = QPushButton("📂  Selecionar…")
        btn_xmp.clicked.connect(self._select_xmp)
        xmp_row.addWidget(self.lbl_xmp, stretch=1)
        xmp_row.addWidget(btn_xmp)
        lay.addLayout(xmp_row)

        # Sensitivity (score threshold)
        sens_row = QHBoxLayout()
        sens_row.addWidget(QLabel("Sensibilidade:"))
        self.cmb_threshold = QComboBox()
        self.cmb_threshold.addItems([
            "Conservadora  (pontuação ≥ 7  — menos falsos positivos)",
            "Normal         (pontuação ≥ 6)",
            "Agressiva      (pontuação ≥ 4  — inclui cenas ambíguas)",
        ])
        self.cmb_threshold.setCurrentIndex(1)
        self.cmb_threshold.currentIndexChanged.connect(lambda _: self._save_settings())
        sens_row.addWidget(self.cmb_threshold, stretch=1)
        lay.addLayout(sens_row)

        self.chk_transition = QCheckBox(
            "Aplicar também a fotos de transição (sacadas, varandas, garagens…)"
        )
        self.chk_transition.stateChanged.connect(lambda _: self._save_settings())
        lay.addWidget(self.chk_transition)

        lay.addWidget(_sep())

        # Catalog
        cat_row = QHBoxLayout()
        btn_cat = QPushButton("📂  Selecionar catálogo…")
        btn_cat.clicked.connect(self._select_catalog)
        self.lbl_cat = QLabel("Nenhum catálogo selecionado")
        self.lbl_cat.setStyleSheet("color: #6c7086; font-style: italic;")
        self.lbl_cat.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        cat_row.addWidget(btn_cat)
        cat_row.addWidget(self.lbl_cat, stretch=1)
        lay.addLayout(cat_row)

        self.chk_backup = QCheckBox("Fazer backup (.lrcat.bak) antes de aplicar  (recomendado)")
        self.chk_backup.setChecked(True)
        self.chk_backup.stateChanged.connect(lambda _: self._save_settings())
        lay.addWidget(self.chk_backup)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        lay.addWidget(self.progress)

        # Action buttons
        btn_row = QHBoxLayout()
        btn_classify = QPushButton("🔍  Classificar (pré-visualizar)")
        btn_classify.clicked.connect(self._classify)
        self.btn_apply = QPushButton("⚡  Aplicar preset às fotos externas")
        self.btn_apply.setObjectName("primary")
        self.btn_apply.clicked.connect(self._apply)
        btn_row.addWidget(btn_classify)
        btn_row.addWidget(self.btn_apply)
        lay.addLayout(btn_row)

        self.lbl_result = QLabel("")
        self.lbl_result.setWordWrap(True)
        self.lbl_result.setStyleSheet("color: #a6e3a1; font-weight: bold;")
        lay.addWidget(self.lbl_result)

        # Results table: filename | score | classification | status
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Arquivo", "Pontuação", "Classificação", "Status"]
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        for col in (1, 2):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self.table, stretch=1)

        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMaximumHeight(120)
        lay.addWidget(self.log_area)

    # ------------------------------------------------------------------ helpers

    def _log(self, msg: str):
        self.log_area.append(msg)
        self.logger.info(msg)
        self.status_msg.emit(msg)

    def _threshold_value(self) -> int:
        return [7, 6, 4][self.cmb_threshold.currentIndex()]

    def _populate_table(self, report_rows: list):
        LABEL_PT = {"exterior": "Exterior", "transicao": "Transição", "interior": "Interior"}
        STATUS_PT = {
            "aplicada":         "✔ Aplicada",
            "ignorada":         "—",
            "sem_configuração":  "Sem config.",
        }
        LABEL_COLOR = {
            "exterior":  "#a6e3a1",
            "transicao": "#f9e2af",
            "interior":  "#bac2de",
        }
        self.table.setRowCount(len(report_rows))
        for r, (fname, score, label, status) in enumerate(report_rows):
            vals = [fname, str(score), LABEL_PT.get(label, label), STATUS_PT.get(status, status)]
            for c, val in enumerate(vals):
                item = QTableWidgetItem(val)
                item.setTextAlignment(
                    Qt.AlignLeft | Qt.AlignVCenter if c == 0 else Qt.AlignCenter
                )
                if c == 2:
                    item.setForeground(QColor(LABEL_COLOR.get(label, "#cdd6f4")))
                self.table.setItem(r, c, item)

    # ------------------------------------------------------------------ actions

    def _select_xmp(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Selecionar preset XMP", "",
            "Lightroom Preset (*.xmp);;All files (*.*)"
        )
        if f:
            self._xmp = Path(f)
            self.lbl_xmp.setText(f)
            self.lbl_xmp.setStyleSheet("color: #cdd6f4;")
            self._save_settings()

    def _select_catalog(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Selecionar catálogo Lightroom", "",
            "Lightroom Catalog (*.lrcat)"
        )
        if f:
            self._catalog = Path(f)
            self.lbl_cat.setText(f)
            self.lbl_cat.setStyleSheet("color: #cdd6f4;")
            self._save_settings()

    def _classify(self):
        if self._catalog is None:
            QMessageBox.warning(self, "Aviso", "Selecione um catálogo .lrcat primeiro.")
            return
        self.table.setRowCount(0)
        self.lbl_result.setText("")
        self.progress.setValue(0)
        self._log(f"Classificando {self._catalog.name}…")

        workdir = APP_DIR / "banco" / "_tmp"

        def _prog(a, b):
            self.progress.setMaximum(b or 1)
            self.progress.setValue(a)

        def _ok(results: list):
            ext   = sum(1 for r in results if r["label"] == "exterior")
            tran  = sum(1 for r in results if r["label"] == "transicao")
            inter = sum(1 for r in results if r["label"] == "interior")
            self.lbl_result.setText(
                f"🔍  {ext} exterior  •  {tran} transição  •  {inter} interior"
                f"  (total: {len(results)})"
            )
            self.lbl_result.setStyleSheet("color: #89b4fa; font-weight: bold;")
            # Use label as status for preview
            self._populate_table(
                [(r["filename"], r["score"], r["label"], r["label"]) for r in results]
            )
            self._log(f"✔ Classificação concluída — {ext} exteriores, {tran} transição.")

        def _err(msg: str):
            self._log(f"✗ Erro: {msg.splitlines()[0]}")
            QMessageBox.critical(self, "Erro", msg.splitlines()[0])

        self.thread = WorkerThread(
            core.classify_catalog_by_exterior,
            self.con, self._catalog, workdir, self.logger,
        )
        self.thread.progress.connect(_prog)
        self.thread.done_ok.connect(_ok)
        self.thread.done_err.connect(_err)
        self.thread.start()

    def _apply(self):
        if self._catalog is None:
            QMessageBox.warning(self, "Aviso", "Selecione um catálogo .lrcat primeiro.")
            return
        if self._xmp is None:
            QMessageBox.warning(self, "Aviso", "Selecione um arquivo preset .xmp primeiro.")
            return
        if not self._xmp.is_file():
            QMessageBox.warning(self, "Arquivo não encontrado",
                f"Preset XMP não encontrado:\n{self._xmp}")
            return

        backup = self.chk_backup.isChecked()
        if not backup:
            confirm = QMessageBox.question(
                self, "Atenção",
                "Você optou por NÃO fazer backup.\n"
                "As alterações serão escritas diretamente no catálogo e não poderão ser desfeitas.\n\n"
                "Deseja continuar?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return

        self.table.setRowCount(0)
        self.lbl_result.setText("")
        self.btn_apply.setEnabled(False)
        self.progress.setValue(0)
        self._log(
            f"Aplicando '{self._xmp.stem}' nas fotos externas de {self._catalog.name}…"
        )

        workdir = APP_DIR / "banco" / "_tmp"

        def _prog(a, b):
            self.progress.setMaximum(b or 1)
            self.progress.setValue(a)

        def _ok(result: dict):
            self.btn_apply.setEnabled(True)
            a, t = result["applied"], result["total"]
            self.lbl_result.setText(
                f"✔ Preset aplicado em {a} / {t} foto(s) de {self._catalog.name}"
            )
            self.lbl_result.setStyleSheet("color: #a6e3a1; font-weight: bold;")
            self._populate_table(result.get("report_rows", []))
            self._log(f"✔ Concluído — {a}/{t} fotos.")

        def _err(msg: str):
            self.btn_apply.setEnabled(True)
            self._log(f"✗ Erro: {msg.splitlines()[0]}")
            QMessageBox.critical(self, "Erro", msg.splitlines()[0])

        self.thread = WorkerThread(
            core.apply_xmp_to_exterior_photos,
            self.con, self._catalog, workdir, self._xmp, self.logger,
            score_threshold=self._threshold_value(),
            apply_to_transition=self.chk_transition.isChecked(),
            backup=backup,
        )
        self.thread.progress.connect(_prog)
        self.thread.done_ok.connect(_ok)
        self.thread.done_err.connect(_err)
        self.thread.start()

    # ---------------------------------------------------------------- persistence

    def _save_settings(self) -> None:
        if self._loading:
            return
        data = {
            "xmp":              str(self._xmp)      if self._xmp      else "",
            "catalog":          str(self._catalog)   if self._catalog  else "",
            "threshold_index":  self.cmb_threshold.currentIndex(),
            "apply_transition": self.chk_transition.isChecked(),
            "backup":           self.chk_backup.isChecked(),
        }
        try:
            self._SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._SETTINGS_PATH.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            self.logger.warning(f"Não foi possível salvar configurações de exterior: {exc}")

    def _load_settings(self) -> None:
        if not self._SETTINGS_PATH.exists():
            return
        try:
            data = json.loads(self._SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        self._loading = True
        try:
            xmp = data.get("xmp", "")
            if xmp and Path(xmp).is_file():
                self._xmp = Path(xmp)
                self.lbl_xmp.setText(xmp)
                self.lbl_xmp.setStyleSheet("color: #cdd6f4;")
            cat = data.get("catalog", "")
            if cat and Path(cat).exists():
                self._catalog = Path(cat)
                self.lbl_cat.setText(cat)
                self.lbl_cat.setStyleSheet("color: #cdd6f4;")
            self.cmb_threshold.setCurrentIndex(data.get("threshold_index", 1))
            self.chk_transition.setChecked(data.get("apply_transition", False))
            self.chk_backup.setChecked(data.get("backup", True))
        finally:
            self._loading = False


# ---------------------------------------------------------------------------
# Janela principal
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lightroom Assistant")

        db_path   = APP_DIR / "banco" / "lightroom_assistant.db"
        logs_dir  = APP_DIR / "logs"
        self.logger = core.setup_logger(logs_dir)
        # v12: additive rotating/leveled log handlers (app/database/processing/
        # errors/benchmark), layered onto the SAME logger — never replaces or
        # removes the v11 plain-file handler above. Wrapped so a failure here
        # can never prevent the app from starting.
        if gui_tools is not None and gui_tools.safety is not None:
            try:
                gui_tools.safety.setup_structured_logging(logs_dir)
            except Exception as exc:  # noqa: BLE001
                self.logger.error(f"[v12:logging] falha ao instalar logs estruturados: {exc}")
        self.con    = core.init_database(db_path)
        self.cfg    = AppConfig()

        # Restore window geometry
        w = self.cfg.get("window_width",  880)
        h = self.cfg.get("window_height", 700)
        self.resize(w, h)
        x = self.cfg.get("window_x")
        y = self.cfg.get("window_y")
        if x is not None and y is not None:
            self.move(x, y)

        tabs = QTabWidget()

        self.tab_banco    = BancoTab(self.con, self.logger, self.cfg)
        self.tab_editar   = EditarTab(self.con, self.logger, self.cfg)
        self.tab_preset   = PresetLenteTab(self.con, self.logger)
        self.tab_exterior = ExteriorTab(self.con, self.logger)

        tabs.addTab(self.tab_banco,    "🗄  Banco")
        tabs.addTab(self.tab_editar,   "✏  Editar Catálogo")
        tabs.addTab(self.tab_preset,   "🎨  Presets por Lente")
        tabs.addTab(self.tab_exterior, "🌿  Exterior / Interior")

        # v12 — additive "Ferramentas e Saúde" tab. Wrapped so a failure to
        # build it never prevents the 4 existing tabs above from working.
        self.tab_tools = None
        if gui_tools is not None:
            try:
                self.tab_tools = gui_tools.FerramentasSaudeTab(self.con, self.logger, db_path, logs_dir)
                tabs.addTab(self.tab_tools, "🛠  Ferramentas e Saúde")
            except Exception as exc:  # noqa: BLE001
                self.logger.error(f"[v12:ui] falha ao montar 'Ferramentas e Saúde': {exc}")

        # Restore last active tab
        last_tab = self.cfg.get("editar_last_tab", 0)
        tabs.setCurrentIndex(last_tab)
        tabs.currentChanged.connect(lambda i: self.cfg.set("editar_last_tab", i))
        self.tabs = tabs

        self.setCentralWidget(tabs)

        bar = QStatusBar()
        self.setStatusBar(bar)
        self._status = bar

        n = core.count_photos(self.con)
        bar.showMessage(f"Banco: {n} foto(s)  —  pronto.")

        for tab in (self.tab_banco, self.tab_editar, self.tab_preset, self.tab_exterior):
            tab.status_msg.connect(lambda m: self._status.showMessage(m))

        self.tab_banco.bank_changed = self.tab_editar.bank_changed
        self.tab_editar.bank_changed.connect(self._refresh_status)

    def closeEvent(self, event):
        """Persist window size/position on close."""
        geo = self.geometry()
        self.cfg.set("window_width",  geo.width())
        self.cfg.set("window_height", geo.height())
        self.cfg.set("window_x",      geo.x())
        self.cfg.set("window_y",      geo.y())
        self.tab_banco._save_state()
        super().closeEvent(event)

    def _refresh_status(self):
        n = core.count_photos(self.con)
        self._status.showMessage(f"Banco: {n} foto(s)")
        self.tab_banco.refresh_stats()


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
