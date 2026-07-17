"""
Lightroom Assistant - GUI: "Ferramentas e Saúde" (v12)
========================================================
New tab area holding the safety/diagnostics/performance screens described
in the v12 spec: Saúde do Banco, Estatísticas, Outliers, Lentes e Aliases,
Cache, Backups, Benchmark, Logs.

Every sub-tab is wrapped so a bug in ONE screen can never crash the whole
app or interfere with the existing Banco/Editar/Presets/Exterior tabs —
each sub-tab's data-loading calls are guarded with try/except and show an
inline error message instead of raising.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressBar,
    QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem, QTabWidget,
    QTextEdit, QVBoxLayout, QWidget,
)

import core

try:
    import safety
except ImportError:  # pragma: no cover
    safety = None


def _sep_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("section_title")
    return lbl


def _safe(fn, *args, **kwargs):
    """Call fn(*args, **kwargs); return (result, error_str_or_None)."""
    try:
        return fn(*args, **kwargs), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{exc}\n{traceback.format_exc()}"


# ---------------------------------------------------------------------------
# Saúde do Banco
# ---------------------------------------------------------------------------
class HealthTab(QWidget):
    def __init__(self, con, logger, db_path: Path, parent=None):
        super().__init__(parent)
        self.con, self.logger, self.db_path = con, logger, db_path
        lay = QVBoxLayout(self)
        lay.addWidget(_sep_label("Saúde do Banco"))

        self.lbl_overall = QLabel("")
        self.lbl_overall.setStyleSheet("font-weight: bold; font-size: 14px;")
        lay.addWidget(self.lbl_overall)

        btn = QPushButton("🔄  Atualizar")
        btn.clicked.connect(self.refresh)
        lay.addWidget(btn)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Verificação", "Valor", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table)

        self.refresh()

    def refresh(self):
        if safety is None:
            self.lbl_overall.setText("Recurso v12 indisponível (módulo safety não carregado).")
            return
        health, err = _safe(safety.get_database_health, self.con, self.db_path)
        if err:
            self.lbl_overall.setText("Erro ao calcular saúde do banco — ver log.")
            self.logger.error(f"[v12:health] {err}")
            return
        colors = {"ok": "#a6e3a1", "atencao": "#f9e2af", "critico": "#f38ba8"}
        overall = health.get("overall", "ok")
        self.lbl_overall.setText(f"Status geral: {overall.upper()}")
        self.lbl_overall.setStyleSheet(f"font-weight: bold; font-size: 14px; color: {colors.get(overall, '#cdd6f4')};")
        checks = health.get("checks", [])
        self.table.setRowCount(len(checks))
        for i, c in enumerate(checks):
            self.table.setItem(i, 0, QTableWidgetItem(str(c["label"])))
            self.table.setItem(i, 1, QTableWidgetItem(str(c["value"])))
            status_item = QTableWidgetItem(c["status"])
            self.table.setItem(i, 2, status_item)


# ---------------------------------------------------------------------------
# Estatísticas
# ---------------------------------------------------------------------------
class StatsTab(QWidget):
    def __init__(self, con, logger, parent=None):
        super().__init__(parent)
        self.con, self.logger = con, logger
        lay = QVBoxLayout(self)
        lay.addWidget(_sep_label("Estatísticas Detalhadas"))

        row = QHBoxLayout()
        row.addWidget(QLabel("Agrupar por:"))
        self.cmb_group = QComboBox()
        self.cmb_group.addItems(["(nenhum)", "lens", "preset", "camera", "iso"])
        row.addWidget(self.cmb_group)
        btn_refresh = QPushButton("🔄  Calcular")
        btn_refresh.clicked.connect(self.refresh)
        row.addWidget(btn_refresh)
        btn_csv = QPushButton("⬇  Exportar CSV")
        btn_csv.clicked.connect(self._export_csv)
        row.addWidget(btn_csv)
        btn_json = QPushButton("⬇  Exportar JSON")
        btn_json.clicked.connect(self._export_json)
        row.addWidget(btn_json)
        row.addStretch()
        lay.addLayout(row)

        self.table = QTableWidget(0, 0)
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table)

        self._last_stats = []
        self.refresh()

    def _group_key(self):
        g = self.cmb_group.currentText()
        return None if g == "(nenhum)" else g

    def refresh(self):
        if safety is None:
            return
        stats, err = _safe(safety.get_parameter_statistics, self.con, group_by=self._group_key())
        if err:
            self.logger.error(f"[v12:stats] {err}")
            QMessageBox.warning(self, "Erro", "Não foi possível calcular estatísticas — ver log.")
            return
        self._last_stats = stats or []
        params = safety._STAT_PARAMS
        headers = ["grupo"] + [f"{p} (n/mediana/media)" for p in params]
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(self._last_stats))
        for i, row in enumerate(self._last_stats):
            self.table.setItem(i, 0, QTableWidgetItem(str(row.get("group"))))
            for j, p in enumerate(params, start=1):
                d = row.get(p, {})
                if d.get("count", 0):
                    txt = f"{d['count']} / {d['median']:.2f} / {d['mean']:.2f}"
                else:
                    txt = "—"
                self.table.setItem(i, j, QTableWidgetItem(txt))

    def _export_csv(self):
        if not self._last_stats:
            return
        f, _ = QFileDialog.getSaveFileName(self, "Exportar estatísticas (CSV)", "estatisticas.csv", "CSV (*.csv)")
        if f:
            _, err = _safe(safety.export_statistics_csv, self._last_stats, Path(f))
            if err:
                self.logger.error(f"[v12:stats_export] {err}")
                QMessageBox.warning(self, "Erro", "Falha ao exportar CSV — ver log.")

    def _export_json(self):
        if not self._last_stats:
            return
        f, _ = QFileDialog.getSaveFileName(self, "Exportar estatísticas (JSON)", "estatisticas.json", "JSON (*.json)")
        if f:
            _, err = _safe(safety.export_statistics_json, self._last_stats, Path(f))
            if err:
                self.logger.error(f"[v12:stats_export] {err}")
                QMessageBox.warning(self, "Erro", "Falha ao exportar JSON — ver log.")


# ---------------------------------------------------------------------------
# Outliers
# ---------------------------------------------------------------------------
class OutliersTab(QWidget):
    def __init__(self, con, logger, parent=None):
        super().__init__(parent)
        self.con, self.logger = con, logger
        lay = QVBoxLayout(self)
        lay.addWidget(_sep_label("Fotos em Quarentena (Outliers)"))
        info = QLabel(
            "Fotos com valores de exposição/sombras/temperatura/tinta estatisticamente "
            "incomuns em relação ao banco ficam aqui em vez de serem aprendidas "
            "silenciosamente. Nada é apagado — aprove, ignore ou restaure manualmente."
        )
        info.setWordWrap(True)
        lay.addWidget(info)

        row = QHBoxLayout()
        btn_refresh = QPushButton("🔄  Atualizar")
        btn_refresh.clicked.connect(self.refresh)
        btn_approve = QPushButton("✔  Aprovar selecionadas")
        btn_approve.clicked.connect(lambda: self._resolve_selected("approve"))
        btn_ignore = QPushButton("✕  Ignorar selecionadas")
        btn_ignore.clicked.connect(lambda: self._resolve_selected("ignore"))
        btn_restore = QPushButton("↺  Restaurar ao banco")
        btn_restore.clicked.connect(self._restore_selected)
        row.addWidget(btn_refresh)
        row.addWidget(btn_approve)
        row.addWidget(btn_ignore)
        row.addWidget(btn_restore)
        row.addStretch()
        lay.addLayout(row)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["ID", "Arquivo", "Parâmetro", "Valor", "Status", "Motivo"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table)

        self.refresh()

    def refresh(self):
        if safety is None:
            return
        items, err = _safe(safety.list_quarantine, self.con)
        if err:
            self.logger.error(f"[v12:outliers] {err}")
            return
        items = items or []
        self.table.setRowCount(len(items))
        self._items = items
        for i, it in enumerate(items):
            self.table.setItem(i, 0, QTableWidgetItem(str(it["id"])))
            self.table.setItem(i, 1, QTableWidgetItem(str(it.get("filename") or "")))
            self.table.setItem(i, 2, QTableWidgetItem(str(it.get("parameter") or "")))
            self.table.setItem(i, 3, QTableWidgetItem(str(it.get("value"))))
            self.table.setItem(i, 4, QTableWidgetItem(str(it.get("status"))))
            self.table.setItem(i, 5, QTableWidgetItem(str(it.get("reason") or "")))

    def _selected_ids(self) -> list[int]:
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()})
        return [self._items[r]["id"] for r in rows if r < len(self._items)]

    def _resolve_selected(self, action: str):
        if safety is None:
            return
        for item_id in self._selected_ids():
            _, err = _safe(safety.resolve_quarantine_item, self.con, item_id, action)
            if err:
                self.logger.error(f"[v12:outliers] {err}")
        self.refresh()

    def _restore_selected(self):
        if safety is None:
            return
        for item_id in self._selected_ids():
            def _insert(con, snapshot):
                # source_catalog is injected by safety.restore_quarantine_item
                # from the original quarantine row — never derived from the
                # photo's own file path — so restored rows keep correct
                # provenance for re-import/update semantics.
                source_catalog = snapshot.get("_source_catalog") or "quarantine_restore"
                return core.insert_photo_snapshot(con, source_catalog, snapshot)
            _, err = _safe(safety.restore_quarantine_item, self.con, item_id, _insert)
            if err:
                self.logger.error(f"[v12:outliers] {err}")
        self.refresh()
        QMessageBox.information(self, "Restauração", "Itens selecionados restaurados ao banco (quando aplicável).")


# ---------------------------------------------------------------------------
# Lentes e Aliases
# ---------------------------------------------------------------------------
class LensAliasesTab(QWidget):
    def __init__(self, con, logger, parent=None):
        super().__init__(parent)
        self.con, self.logger = con, logger
        lay = QVBoxLayout(self)
        lay.addWidget(_sep_label("Lentes e Aliases"))
        info = QLabel(
            "Mapeie um nome de lente alternativo (ex: como aparece em um catálogo antigo) "
            "para o nome canônico usado no aplicativo. Isso NUNCA altera a lógica de "
            "correspondência do v11 — é consultado apenas quando ela não encontra nada."
        )
        info.setWordWrap(True)
        lay.addWidget(info)

        form = QHBoxLayout()
        self.txt_original = QLineEdit()
        self.txt_original.setPlaceholderText("Nome original (como aparece no catálogo)…")
        self.txt_canonical = QLineEdit()
        self.txt_canonical.setPlaceholderText("Nome canônico (como está configurado)…")
        btn_add = QPushButton("＋  Adicionar/Atualizar")
        btn_add.clicked.connect(self._add)
        form.addWidget(self.txt_original)
        form.addWidget(self.txt_canonical)
        form.addWidget(btn_add)
        lay.addLayout(form)

        row = QHBoxLayout()
        btn_refresh = QPushButton("🔄  Atualizar lista")
        btn_refresh.clicked.connect(self.refresh)
        btn_del = QPushButton("－  Excluir selecionado")
        btn_del.setObjectName("danger")
        btn_del.clicked.connect(self._delete_selected)
        row.addWidget(btn_refresh)
        row.addWidget(btn_del)
        row.addStretch()
        lay.addLayout(row)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["ID", "Original", "Canônico"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table)

        self.refresh()

    def refresh(self):
        if safety is None:
            return
        items, err = _safe(safety.list_lens_aliases, self.con)
        if err:
            self.logger.error(f"[v12:lens_alias] {err}")
            return
        items = items or []
        self._items = items
        self.table.setRowCount(len(items))
        for i, it in enumerate(items):
            self.table.setItem(i, 0, QTableWidgetItem(str(it["id"])))
            self.table.setItem(i, 1, QTableWidgetItem(str(it.get("original_name"))))
            self.table.setItem(i, 2, QTableWidgetItem(str(it.get("canonical_name"))))

    def _add(self):
        if safety is None:
            return
        orig = self.txt_original.text().strip()
        canon = self.txt_canonical.text().strip()
        if not orig or not canon:
            QMessageBox.warning(self, "Aviso", "Preencha os dois campos.")
            return
        _, err = _safe(safety.add_lens_alias, self.con, orig, canon)
        if err:
            self.logger.error(f"[v12:lens_alias] {err}")
            QMessageBox.warning(self, "Erro", "Falha ao salvar alias — ver log.")
            return
        self.txt_original.clear()
        self.txt_canonical.clear()
        self.refresh()

    def _delete_selected(self):
        if safety is None:
            return
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()})
        for r in rows:
            if r < len(self._items):
                _safe(safety.delete_lens_alias, self.con, self._items[r]["id"])
        self.refresh()


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
class CacheTab(QWidget):
    def __init__(self, con, logger, parent=None):
        super().__init__(parent)
        self.con, self.logger = con, logger
        lay = QVBoxLayout(self)
        lay.addWidget(_sep_label("Cache Seguro"))
        info = QLabel(
            "Acelera leituras repetidas (features/metadados/normalização de lente). "
            "Qualquer alteração relevante nos dados/versão invalida automaticamente "
            "as entradas antigas. Qualquer falha do cache cai de volta ao cálculo "
            "normal, sem cache."
        )
        info.setWordWrap(True)
        lay.addWidget(info)

        self.lbl_stats = QLabel("")
        lay.addWidget(self.lbl_stats)

        row = QHBoxLayout()
        btn_refresh = QPushButton("🔄  Atualizar estatísticas")
        btn_refresh.clicked.connect(self.refresh)
        btn_clear = QPushButton("🗑  Limpar cache")
        btn_clear.setObjectName("danger")
        btn_clear.clicked.connect(self._clear)
        row.addWidget(btn_refresh)
        row.addWidget(btn_clear)
        row.addStretch()
        lay.addLayout(row)

        self.refresh()

    def refresh(self):
        if safety is None:
            self.lbl_stats.setText("Recurso v12 indisponível.")
            return
        stats, err = _safe(safety.cache_stats, self.con)
        if err:
            self.logger.error(f"[v12:cache] {err}")
            self.lbl_stats.setText("Erro ao ler estatísticas do cache — ver log.")
            return
        lines = [f"Total de entradas: {stats.get('total_entries', 0)}"]
        for ns in stats.get("by_namespace", []):
            lines.append(f"  • {ns['namespace']}: {ns['entries']} entrada(s), {ns.get('total_hits') or 0} hit(s)")
        self.lbl_stats.setText("\n".join(lines))

    def _clear(self):
        if safety is None:
            return
        n, err = _safe(safety.cache_clear, self.con)
        if err:
            self.logger.error(f"[v12:cache] {err}")
            QMessageBox.warning(self, "Erro", "Falha ao limpar cache — ver log.")
            return
        self.refresh()
        QMessageBox.information(self, "Cache", f"{n} entrada(s) removida(s).")


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------
class BackupsTab(QWidget):
    def __init__(self, con, logger, db_path: Path, parent=None):
        super().__init__(parent)
        self.con, self.logger, self.db_path = con, logger, db_path
        self.backups_dir = db_path.parent / "backups_v12"
        lay = QVBoxLayout(self)
        lay.addWidget(_sep_label("Backups & Restauração"))

        row = QHBoxLayout()
        btn_refresh = QPushButton("🔄  Atualizar")
        btn_refresh.clicked.connect(self.refresh)
        btn_manual = QPushButton("💾  Fazer backup agora")
        btn_manual.setObjectName("primary")
        btn_manual.clicked.connect(self._manual_backup)
        btn_restore = QPushButton("↺  Restaurar selecionado")
        btn_restore.setObjectName("danger")
        btn_restore.clicked.connect(self._restore_selected)
        btn_folder = QPushButton("📂  Abrir pasta")
        btn_folder.clicked.connect(self._open_folder)
        row.addWidget(btn_refresh)
        row.addWidget(btn_manual)
        row.addWidget(btn_restore)
        row.addWidget(btn_folder)
        row.addStretch()
        lay.addLayout(row)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Data", "Arquivo", "Tamanho", "Registros", "Íntegro"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table)

        self.refresh()

    def refresh(self):
        if safety is None:
            return
        items, err = _safe(safety.list_backups, self.con)
        if err:
            self.logger.error(f"[v12:backups] {err}")
            return
        items = items or []
        self._items = items
        self.table.setRowCount(len(items))
        for i, it in enumerate(items):
            self.table.setItem(i, 0, QTableWidgetItem(str(it.get("created_at"))))
            self.table.setItem(i, 1, QTableWidgetItem(Path(it.get("backup_path", "")).name))
            size_mb = (it.get("size_bytes") or 0) / 1024 / 1024
            self.table.setItem(i, 2, QTableWidgetItem(f"{size_mb:.2f} MB"))
            self.table.setItem(i, 3, QTableWidgetItem(str(it.get("record_count"))))
            self.table.setItem(i, 4, QTableWidgetItem("sim" if it.get("integrity_ok") else "NÃO"))

    def _manual_backup(self):
        if safety is None:
            return
        meta, err = _safe(safety.create_backup, self.con, self.db_path, self.backups_dir, reason="manual", is_manual=True)
        if err:
            self.logger.error(f"[v12:backups] {err}")
            QMessageBox.warning(self, "Erro", "Falha ao criar backup — ver log.")
            return
        self.refresh()
        QMessageBox.information(self, "Backup", f"Backup criado: {Path(meta['backup_path']).name}")

    def _restore_selected(self):
        if safety is None:
            return
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()})
        if not rows:
            QMessageBox.warning(self, "Aviso", "Selecione um backup na lista.")
            return
        item = self._items[rows[0]]
        answer = QMessageBox.question(
            self, "Confirmar restauração",
            "Isso substitui o banco atual pelo backup selecionado "
            "(o estado atual é salvo automaticamente antes, para reversão em caso de falha). Continuar?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        result, err = _safe(safety.restore_backup, self.db_path, Path(item["backup_path"]), self.logger)
        if err or not (result and result.get("ok")):
            self.logger.error(f"[v12:backups] restauração falhou: {err or result}")
            QMessageBox.critical(self, "Erro", "Restauração falhou (revertida automaticamente). Reinicie o app para reabrir o banco.")
            return
        QMessageBox.information(self, "Restauração", "Banco restaurado. Reinicie o aplicativo para recarregar os dados.")

    def _open_folder(self):
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(self.backups_dir))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.backups_dir)])
            else:
                subprocess.Popen(["xdg-open", str(self.backups_dir)])
        except Exception as exc:
            self.logger.error(f"[v12:backups] não foi possível abrir a pasta: {exc}")


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------
class BenchmarkTab(QWidget):
    def __init__(self, con, logger, parent=None):
        super().__init__(parent)
        self.con, self.logger = con, logger
        lay = QVBoxLayout(self)
        lay.addWidget(_sep_label("Benchmark (manual, não-destrutivo)"))
        info = QLabel(
            "Executado apenas quando você clicar — nunca automaticamente. "
            "Não treina, não altera bias e não grava correções."
        )
        info.setWordWrap(True)
        lay.addWidget(info)

        row = QHBoxLayout()
        btn_tech = QPushButton("⚙  Benchmark técnico")
        btn_tech.clicked.connect(self._run_technical)
        btn_stab = QPushButton("🔁  Benchmark de estabilidade")
        btn_stab.clicked.connect(self._run_stability)
        row.addWidget(btn_tech)
        row.addWidget(btn_stab)
        row.addStretch()
        lay.addLayout(row)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        lay.addWidget(self.output)

    def _run_technical(self):
        if safety is None:
            return
        result, err = _safe(safety.run_technical_benchmark, self.con, self.logger)
        if err:
            self.logger.error(f"[v12:benchmark] {err}")
            self.output.append("✗ Erro no benchmark técnico — ver log.")
            return
        self.output.append(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    def _run_stability(self):
        if safety is None:
            return
        result, err = _safe(
            safety.run_stability_benchmark, self.con, core.suggest_exposure_shadows, self.logger,
        )
        if err:
            self.logger.error(f"[v12:benchmark] {err}")
            self.output.append("✗ Erro no benchmark de estabilidade — ver log.")
            return
        self.output.append(json.dumps(result, indent=2, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
class LogsTab(QWidget):
    def __init__(self, con, logger, logs_dir: Path, db_path: Path, parent=None):
        super().__init__(parent)
        self.con, self.logger, self.logs_dir, self.db_path = con, logger, logs_dir, db_path
        lay = QVBoxLayout(self)
        lay.addWidget(_sep_label("Logs & Diagnóstico"))

        row = QHBoxLayout()
        btn_folder = QPushButton("📂  Abrir pasta de logs")
        btn_folder.clicked.connect(self._open_folder)
        btn_export = QPushButton("📦  Exportar pacote de diagnóstico")
        btn_export.clicked.connect(self._export_diagnostics)
        row.addWidget(btn_folder)
        row.addWidget(btn_export)
        row.addStretch()
        lay.addLayout(row)

        info = QLabel(
            "O pacote de diagnóstico inclui logs, versão do schema e verificação de "
            "integridade — nunca o banco de dados completo."
        )
        info.setWordWrap(True)
        lay.addWidget(info)

    def _open_folder(self):
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(self.logs_dir))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.logs_dir)])
            else:
                subprocess.Popen(["xdg-open", str(self.logs_dir)])
        except Exception as exc:
            self.logger.error(f"[v12:logs] não foi possível abrir a pasta: {exc}")

    def _export_diagnostics(self):
        if safety is None:
            return
        f, _ = QFileDialog.getSaveFileName(self, "Exportar diagnóstico", "diagnostico.zip", "ZIP (*.zip)")
        if not f:
            return
        _, err = _safe(safety.export_diagnostics_package, self.logs_dir, self.db_path, self.con, Path(f))
        if err:
            self.logger.error(f"[v12:diagnostics] {err}")
            QMessageBox.warning(self, "Erro", "Falha ao exportar diagnóstico — ver log.")
            return
        QMessageBox.information(self, "Diagnóstico", f"Pacote exportado em:\n{f}")


# ---------------------------------------------------------------------------
# Aggregate tab
# ---------------------------------------------------------------------------
class FerramentasSaudeTab(QWidget):
    status_msg = Signal(str)

    def __init__(self, con, logger, db_path: Path, logs_dir: Path, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        sub = QTabWidget()
        lay.addWidget(sub)

        def _add(widget, title):
            try:
                sub.addTab(widget, title)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[v12:ui] falha ao montar aba '{title}': {exc}")

        _add(HealthTab(con, logger, db_path), "🩺 Saúde do Banco")
        _add(StatsTab(con, logger), "📊 Estatísticas")
        _add(OutliersTab(con, logger), "🚨 Outliers")
        _add(LensAliasesTab(con, logger), "🔤 Lentes e Aliases")
        _add(CacheTab(con, logger), "⚡ Cache")
        _add(BackupsTab(con, logger, db_path), "💾 Backups")
        _add(BenchmarkTab(con, logger), "⏱ Benchmark")
        _add(LogsTab(con, logger, logs_dir, db_path), "📄 Logs")
