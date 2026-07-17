"""
Lightroom Assistant - Safety, Diagnostics & Performance Layer (v12)
====================================================================
Purely additive module: outlier quarantine, lens aliasing, a safe
read-through cache, a database health dashboard, detailed statistics,
versioned backup/rollback, structured rotating logs + diagnostics export,
and a non-destructive benchmark.

Hard rule: nothing in this module may change the output of core.py's
suggestion algorithm, KNN, bias learning, lens-matching scoring, or
catalog-writing logic. Every public entry point here is:
  - OFF by default at the core.py call-site (new features are additive
    kwargs with safe defaults), and
  - wrapped so a failure disables only that one feature and never raises
    into the caller's normal feed/edit/lens-correction path.

This module can be imported and unit-tested with plain python3 (no PySide6
needed), same as core.py.
"""

from __future__ import annotations

import csv
import functools
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import statistics
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Exception isolation helper
# ---------------------------------------------------------------------------

def safe_feature(feature_name: str, default=None):
    """Decorator: catch ANY exception raised by a v12 subsystem function,
    log it clearly via the logger passed as the function's `logger` kwarg/arg
    (if present), and return `default` instead of propagating.

    This guarantees a bug in, say, the health dashboard can never interrupt
    catalog feeding/editing/lens-correction, which never go through this
    decorator at all (they only ever call this module's helpers optionally,
    from the GUI layer)."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - intentional catch-all boundary
                logger = kwargs.get("logger")
                if logger is None:
                    for a in args:
                        if isinstance(a, logging.Logger):
                            logger = a
                            break
                msg = f"[v12:{feature_name}] recurso desabilitado nesta chamada devido a erro: {exc!r}"
                if logger is not None:
                    try:
                        logger.error(msg)
                    except Exception:
                        pass
                else:
                    logging.getLogger("lightroom_assistant.safety").error(msg)
                return default
        return wrapper
    return deco


# ---------------------------------------------------------------------------
# 1. Additive schema migration
# ---------------------------------------------------------------------------

SAFETY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS safety_schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);

-- Quarantine for training examples whose numeric develop parameters look
-- statistically suspicious/invalid at feed time. Never deletes anything:
-- rows here are simply never inserted into `photos`, and can be restored.
CREATE TABLE IF NOT EXISTS outlier_quarantine (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_catalog      TEXT,
    filename            TEXT,
    image_id_local      INTEGER,
    parameter           TEXT,       -- e.g. "exposure", "shadows"
    value                REAL,
    status              TEXT NOT NULL DEFAULT 'suspicious',  -- suspicious|invalid|approved|ignored|restored
    reason              TEXT,       -- human-readable explanation
    detection_method    TEXT,       -- e.g. "median_mad"
    score               REAL,       -- modified z-score or similar
    photo_snapshot_json TEXT,       -- full photo record snapshot, for restore
    created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
    resolved_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_outlier_quarantine_status ON outlier_quarantine(status);
CREATE INDEX IF NOT EXISTS idx_outlier_quarantine_catalog ON outlier_quarantine(source_catalog);

-- Lens name aliasing, alongside (never inside) the v11 normalization logic.
CREATE TABLE IF NOT EXISTS lens_aliases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    original_name   TEXT NOT NULL,      -- raw name as seen in a catalog / typed by user
    normalized_key  TEXT,               -- lowercased/stripped form for lookup
    canonical_name  TEXT NOT NULL,      -- the name the user wants it treated as
    manufacturer    TEXT,
    focal_range     TEXT,
    aperture        TEXT,
    confidence      REAL DEFAULT 1.0,
    source          TEXT DEFAULT 'manual',   -- manual|auto
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(original_name)
);
CREATE INDEX IF NOT EXISTS idx_lens_aliases_normalized ON lens_aliases(normalized_key);

-- Safe read-through cache. Keyed by an opaque signature string (caller
-- builds the signature from whatever inputs matter: file hash/size/mtime,
-- schema version, algorithm version, bank "version", relevant config).
CREATE TABLE IF NOT EXISTS safe_cache (
    cache_key    TEXT PRIMARY KEY,
    namespace    TEXT NOT NULL,       -- e.g. "features", "lens_norm", "metadata"
    value_json   TEXT NOT NULL,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    hits         INTEGER DEFAULT 0,
    last_hit_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_safe_cache_namespace ON safe_cache(namespace);

-- Versioned backup metadata sidecar (the actual .bak files live on disk;
-- this table just indexes them for the Backups screen).
CREATE TABLE IF NOT EXISTS backup_metadata (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    backup_path     TEXT NOT NULL,
    source_path     TEXT NOT NULL,
    reason          TEXT,
    size_bytes      INTEGER,
    sha256          TEXT,
    record_count    INTEGER,
    integrity_ok    INTEGER,
    is_manual       INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Benchmark run history (informational only; never feeds back into the
-- algorithm).
CREATE TABLE IF NOT EXISTS benchmark_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,    -- "technical" | "stability"
    started_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    duration_ms     REAL,
    sample_size     INTEGER,
    result_json     TEXT
);
"""

_SAFETY_CURRENT_SCHEMA_VERSION = 1


def init_safety_schema(con: sqlite3.Connection, logger: Optional[logging.Logger] = None) -> bool:
    """Additive-only migration for all v12 tables. Never touches v11 tables.

    Returns True on success. On ANY failure, logs the error and returns
    False, leaving the connection exactly as v11 left it (init_database()
    already ran and committed before this is ever called) — the app keeps
    running in v11-equivalent mode with the new features unavailable."""
    try:
        con.executescript(SAFETY_SCHEMA_SQL)
        con.commit()
        current = con.execute(
            "SELECT MAX(version) FROM safety_schema_version"
        ).fetchone()[0] or 0
        if current < _SAFETY_CURRENT_SCHEMA_VERSION:
            con.execute(
                "INSERT OR REPLACE INTO safety_schema_version (version, description) VALUES (?,?)",
                (_SAFETY_CURRENT_SCHEMA_VERSION,
                 "v12: outlier_quarantine + lens_aliases + safe_cache + backup_metadata + benchmark_runs"),
            )
            con.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        msg = f"[v12:schema] Migração aditiva do safety layer falhou — recursos v12 desabilitados: {exc!r}"
        if logger is not None:
            logger.error(msg)
        return False


# ---------------------------------------------------------------------------
# 2. Outlier detector (median / MAD) + quarantine
# ---------------------------------------------------------------------------

# Develop parameters worth screening. Kept intentionally small & numeric —
# these are exactly the fields the KNN/bias pipeline reads (see core.py
# _BIAS_DIMS and the exposure/shadows suggestion path).
OUTLIER_PARAMETERS = ("exposure", "shadows", "temperature", "tint")

# Below this many existing history samples for a given parameter we always
# accept — not enough data to trust a MAD estimate, and this also means
# small/empty banks (a very common regression-test scenario) behave exactly
# like v11 (nothing is ever quarantined).
_MIN_SAMPLES_FOR_DETECTION = 8

# Modified z-score thresholds (Iglewicz & Hoaglin's 0.6745-scaled MAD
# z-score). >3.5 is the commonly cited "likely outlier" cutoff; we use a
# conservative widened band so normal stylistic variation is never flagged.
_Z_SUSPICIOUS = 5.0
_Z_INVALID = 8.0

# Absolute Lightroom slider ranges — a hard technical range check that
# applies even with very little history.
_TECHNICAL_RANGES = {
    "exposure":    (-5.0, 5.0),
    "shadows":     (-100.0, 100.0),
    "temperature": (2000.0, 50000.0),
    "tint":        (-150.0, 150.0),
}


@dataclass
class OutlierVerdict:
    parameter: str
    value: float
    status: str          # "accepted" | "suspicious" | "invalid"
    reason: str
    score: Optional[float]
    method: str


def _median_mad(values: list[float]) -> tuple[float, float]:
    med = statistics.median(values)
    abs_dev = [abs(v - med) for v in values]
    mad = statistics.median(abs_dev)
    return med, mad


def classify_value(param: str, value: Optional[float], history: list[float]) -> OutlierVerdict:
    """Classify a single numeric develop parameter value against the bank's
    existing history for that parameter, using median+MAD with a
    minimum-sample-size guard and a hard technical-range check."""
    if value is None:
        return OutlierVerdict(param, value, "accepted", "sem valor", None, "n/a")

    lo, hi = _TECHNICAL_RANGES.get(param, (None, None))
    if lo is not None and (value < lo or value > hi):
        return OutlierVerdict(
            param, value, "invalid",
            f"fora do intervalo técnico do Lightroom ({lo}..{hi})",
            None, "technical_range",
        )

    clean_history = [v for v in history if v is not None]
    if len(clean_history) < _MIN_SAMPLES_FOR_DETECTION:
        return OutlierVerdict(param, value, "accepted", "histórico insuficiente para análise", None, "n/a")

    med, mad = _median_mad(clean_history)
    if mad == 0:
        # No spread in the existing data — fall back to a simple absolute
        # distance from the median so a truly identical bank can't produce
        # a divide-by-zero and mis-accept everything.
        return OutlierVerdict(param, value, "accepted", "sem variação no histórico", None, "median_mad")

    modified_z = 0.6745 * (value - med) / mad
    abs_z = abs(modified_z)
    if abs_z >= _Z_INVALID:
        return OutlierVerdict(
            param, value, "invalid",
            f"desvio extremo do histórico (z={abs_z:.1f}, mediana={med:.2f})",
            modified_z, "median_mad",
        )
    if abs_z >= _Z_SUSPICIOUS:
        return OutlierVerdict(
            param, value, "suspicious",
            f"desvio incomum do histórico (z={abs_z:.1f}, mediana={med:.2f})",
            modified_z, "median_mad",
        )
    return OutlierVerdict(param, value, "accepted", "dentro do esperado", modified_z, "median_mad")


def evaluate_photo_for_outliers(con: sqlite3.Connection, photo) -> list[OutlierVerdict]:
    """Evaluate every screened parameter of one photo object (as read by
    core.read_catalog_photos) against the current bank history. Read-only —
    never mutates anything."""
    verdicts: list[OutlierVerdict] = []
    for param in OUTLIER_PARAMETERS:
        value = getattr(photo, param, None)
        rows = con.execute(
            f"SELECT {param} FROM photos WHERE {param} IS NOT NULL"
        ).fetchall()
        history = [r[0] for r in rows]
        verdicts.append(classify_value(param, value, history))
    return verdicts


def overall_outlier_status(verdicts: list[OutlierVerdict]) -> str:
    """invalid beats suspicious beats accepted."""
    statuses = {v.status for v in verdicts}
    if "invalid" in statuses:
        return "invalid"
    if "suspicious" in statuses:
        return "suspicious"
    return "accepted"


def quarantine_photo(
    con: sqlite3.Connection,
    source_catalog: str,
    photo,
    verdicts: list[OutlierVerdict],
    status: str,
) -> int:
    """Insert one row per flagged parameter into outlier_quarantine, storing
    a full snapshot of the photo's fields so it can be restored later without
    re-reading the original catalog."""
    snapshot = {}
    for f in (
        "filename", "image_id_local", "id_global", "file_path", "file_extension",
        "file_size_bytes", "rating", "color_label", "capture_time",
        "gps_latitude", "gps_longitude", "gps_altitude", "camera", "camera_make",
        "lens", "focal_length", "focal_length_35mm", "aperture", "shutter_speed",
        "iso", "flash", "metering_mode", "exposure_program", "exposure_bias",
        "orientation", "preset_name", "process_version", "exposure", "contrast_dev",
        "highlights", "shadows", "whites", "blacks", "clarity", "texture", "dehaze",
        "vibrance", "saturation_dev", "temperature", "tint", "sharpness",
        "noise_lum", "noise_color", "lens_profile_enable", "ca_enable",
        "vignette", "grain_amount",
    ):
        snapshot[f] = getattr(photo, f, None)

    inserted = 0
    for v in verdicts:
        if v.status == "accepted":
            continue
        con.execute(
            """INSERT INTO outlier_quarantine
               (source_catalog, filename, image_id_local, parameter, value,
                status, reason, detection_method, score, photo_snapshot_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                source_catalog, photo.filename, photo.image_id_local,
                v.parameter, v.value, status, v.reason, v.method, v.score,
                json.dumps(snapshot),
            ),
        )
        inserted += 1
    if inserted:
        con.commit()
    return inserted


def list_quarantine(con: sqlite3.Connection, status: Optional[str] = None) -> list[dict]:
    q = "SELECT * FROM outlier_quarantine"
    args: tuple = ()
    if status:
        q += " WHERE status = ?"
        args = (status,)
    q += " ORDER BY created_at DESC"
    cur = con.cursor()
    cur.row_factory = sqlite3.Row
    return [dict(r) for r in cur.execute(q, args).fetchall()]


def resolve_quarantine_item(con: sqlite3.Connection, item_id: int, action: str) -> bool:
    """action: 'approve' | 'ignore'. Approve just marks the row resolved —
    it does NOT insert into `photos` (that would be indistinguishable from
    silently reversing the outlier check on a single click); use
    restore_quarantine_item for that, which is explicit about writing to the
    live bank."""
    if action not in ("approve", "ignore"):
        raise ValueError("action deve ser 'approve' ou 'ignore'")
    con.execute(
        "UPDATE outlier_quarantine SET status = ?, resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
        (action + "d" if action == "approve" else "ignored", item_id),
    )
    con.commit()
    return True


def restore_quarantine_item(con: sqlite3.Connection, item_id: int, insert_photo_fn: Callable[[sqlite3.Connection, dict], int]) -> Optional[int]:
    """Restore a quarantined photo snapshot back into the live bank via the
    caller-supplied insert function (kept decoupled from core.py's INSERT
    statement so this module never has to duplicate/replicate it).

    The snapshot dict passed to insert_photo_fn always carries the ORIGINAL
    catalog path the photo was quarantined from, under the "_source_catalog"
    key — never a file path or other stand-in — so restored rows keep
    correct provenance for re-import/update semantics."""
    row = con.execute(
        "SELECT source_catalog, photo_snapshot_json FROM outlier_quarantine WHERE id = ?", (item_id,)
    ).fetchone()
    if not row or not row[1]:
        return None
    source_catalog, snapshot_json = row
    snapshot = json.loads(snapshot_json)
    snapshot["_source_catalog"] = source_catalog
    new_id = insert_photo_fn(con, snapshot)
    con.execute(
        "UPDATE outlier_quarantine SET status = 'restored', resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
        (item_id,),
    )
    con.commit()
    return new_id


# ---------------------------------------------------------------------------
# 3. Lens alias layer (sits alongside core._normalize_lens_name, never inside it)
# ---------------------------------------------------------------------------

def _alias_key(name: str) -> str:
    return " ".join(name.strip().lower().split())


def add_lens_alias(
    con: sqlite3.Connection,
    original_name: str,
    canonical_name: str,
    manufacturer: str = "",
    focal_range: str = "",
    aperture: str = "",
    confidence: float = 1.0,
    source: str = "manual",
) -> int:
    cur = con.execute(
        """INSERT INTO lens_aliases
           (original_name, normalized_key, canonical_name, manufacturer, focal_range, aperture, confidence, source)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(original_name) DO UPDATE SET
               canonical_name=excluded.canonical_name,
               normalized_key=excluded.normalized_key,
               manufacturer=excluded.manufacturer,
               focal_range=excluded.focal_range,
               aperture=excluded.aperture,
               confidence=excluded.confidence,
               source=excluded.source
        """,
        (original_name, _alias_key(original_name), canonical_name, manufacturer, focal_range, aperture, confidence, source),
    )
    con.commit()
    return cur.lastrowid


def delete_lens_alias(con: sqlite3.Connection, alias_id: int) -> None:
    """Deletes only the alias row — never touches `photos` or any bank data."""
    con.execute("DELETE FROM lens_aliases WHERE id = ?", (alias_id,))
    con.commit()


def list_lens_aliases(con: sqlite3.Connection) -> list[dict]:
    cur = con.cursor()
    cur.row_factory = sqlite3.Row
    return [dict(r) for r in cur.execute("SELECT * FROM lens_aliases ORDER BY original_name").fetchall()]


def resolve_lens_alias(con: sqlite3.Connection, raw_lens_name: str) -> Optional[str]:
    """Look up a raw lens name and return its canonical alias, or None.

    Priority: exact original_name match first, then normalized-key match.
    Never invents a match — pure dictionary lookup, so it can never merge
    two genuinely different lenses on its own (that only happens if a user
    explicitly creates that alias)."""
    if not raw_lens_name:
        return None
    row = con.execute(
        "SELECT canonical_name FROM lens_aliases WHERE original_name = ?",
        (raw_lens_name,),
    ).fetchone()
    if row:
        return row[0]
    row = con.execute(
        "SELECT canonical_name FROM lens_aliases WHERE normalized_key = ?",
        (_alias_key(raw_lens_name),),
    ).fetchone()
    return row[0] if row else None


def build_alias_lookup(con: sqlite3.Connection) -> Callable[[str], Optional[str]]:
    """Returns a closure suitable for core.apply_xmp_by_lens(alias_lookup=...)."""
    def _lookup(raw_lens_name: str) -> Optional[str]:
        try:
            return resolve_lens_alias(con, raw_lens_name)
        except Exception:
            return None
    return _lookup


# ---------------------------------------------------------------------------
# 4. Safe read-through cache
# ---------------------------------------------------------------------------

def make_cache_signature(*parts: Any) -> str:
    """Build a stable cache key from arbitrary signature parts (file hash,
    size, mtime, schema version, algorithm version, config values, ...).
    Any part changing produces a different key, so stale entries are simply
    never looked up again rather than needing explicit invalidation."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cache_get(con: sqlite3.Connection, namespace: str, key_parts: tuple) -> Optional[Any]:
    try:
        key = make_cache_signature(namespace, *key_parts)
        row = con.execute(
            "SELECT value_json FROM safe_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        con.execute(
            "UPDATE safe_cache SET hits = hits + 1, last_hit_at = CURRENT_TIMESTAMP WHERE cache_key = ?",
            (key,),
        )
        con.commit()
        return json.loads(row[0])
    except Exception:
        # Any cache corruption/failure => behave as a miss, never raise.
        return None


def cache_set(con: sqlite3.Connection, namespace: str, key_parts: tuple, value: Any) -> None:
    try:
        key = make_cache_signature(namespace, *key_parts)
        con.execute(
            "INSERT OR REPLACE INTO safe_cache (cache_key, namespace, value_json) VALUES (?,?,?)",
            (key, namespace, json.dumps(value)),
        )
        con.commit()
    except Exception:
        pass  # caching is best-effort; never let a write failure surface


def cache_clear(con: sqlite3.Connection, namespace: Optional[str] = None) -> int:
    if namespace:
        cur = con.execute("DELETE FROM safe_cache WHERE namespace = ?", (namespace,))
    else:
        cur = con.execute("DELETE FROM safe_cache")
    con.commit()
    return cur.rowcount


def cache_stats(con: sqlite3.Connection) -> dict:
    cur = con.cursor()
    cur.row_factory = sqlite3.Row
    rows = cur.execute(
        "SELECT namespace, COUNT(*) AS entries, SUM(hits) AS total_hits, "
        "SUM(LENGTH(value_json)) AS bytes FROM safe_cache GROUP BY namespace"
    ).fetchall()
    total_entries = con.execute("SELECT COUNT(*) FROM safe_cache").fetchone()[0]
    return {
        "total_entries": total_entries,
        "by_namespace": [dict(r) for r in rows],
    }


def cached_feature_extraction(
    con: sqlite3.Connection,
    signature_parts: tuple,
    compute_fn: Callable[[], Optional[dict]],
) -> Optional[dict]:
    """Read-through cache wrapper for an expensive feature-extraction call.
    On any cache error, transparently falls back to calling compute_fn() —
    the cache can never be the sole source of truth, and can never block a
    computation from happening."""
    cached = cache_get(con, "features", signature_parts)
    if cached is not None:
        return cached
    result = compute_fn()
    if result is not None:
        cache_set(con, "features", signature_parts, result)
    return result


# ---------------------------------------------------------------------------
# 5. Database health dashboard
# ---------------------------------------------------------------------------

def get_database_health(con: sqlite3.Connection, db_path: Path) -> dict:
    health: dict[str, Any] = {"checks": []}

    def add(label, value, status="ok"):
        health["checks"].append({"label": label, "value": value, "status": status})

    try:
        health["db_path"] = str(db_path)
        health["size_bytes"] = db_path.stat().st_size if db_path.exists() else 0
        add("Caminho do banco", str(db_path))
        add("Tamanho do arquivo", f"{health['size_bytes'] / 1024 / 1024:.2f} MB")
    except Exception as exc:
        add("Caminho/tamanho", f"erro: {exc}", "atencao")

    try:
        v = con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        add("Versão do schema (v11)", v if v is not None else "desconhecida")
    except Exception:
        add("Versão do schema (v11)", "indisponível", "atencao")

    try:
        v = con.execute("SELECT MAX(version) FROM safety_schema_version").fetchone()[0]
        add("Versão do schema (v12/safety)", v if v is not None else "não aplicado", "ok" if v else "atencao")
    except Exception:
        add("Versão do schema (v12/safety)", "não aplicado", "atencao")

    counts = {}
    for table in ("photos", "runs", "ai_suggestions", "correction_log", "realty_signs"):
        try:
            counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            add(f"Registros em {table}", counts[table])
        except Exception:
            add(f"Registros em {table}", "n/d", "atencao")

    for table, label in (("outlier_quarantine", "Fotos em quarentena"), ("lens_aliases", "Aliases de lente")):
        try:
            counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            add(label, counts[table])
        except Exception:
            add(label, 0)

    try:
        n_photos = counts.get("photos", 0)
        n_lenses = con.execute("SELECT COUNT(DISTINCT lens) FROM photos WHERE lens IS NOT NULL AND lens != ''").fetchone()[0]
        n_presets = con.execute("SELECT COUNT(DISTINCT preset_name) FROM photos WHERE preset_name IS NOT NULL AND preset_name != ''").fetchone()[0]
        add("Lentes distintas no banco", n_lenses)
        add("Presets distintos no banco", n_presets)
    except Exception:
        pass

    try:
        pending = con.execute(
            "SELECT COUNT(*) FROM outlier_quarantine WHERE status IN ('suspicious','invalid')"
        ).fetchone()[0]
        add("Itens de quarentena pendentes de revisão", pending, "atencao" if pending else "ok")
    except Exception:
        pass

    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        add("Integridade do banco (PRAGMA integrity_check)", integrity, "ok" if integrity == "ok" else "critico")
        health["integrity_ok"] = integrity == "ok"
    except Exception as exc:
        add("Integridade do banco", f"erro: {exc}", "critico")
        health["integrity_ok"] = False

    try:
        last_backup = con.execute(
            "SELECT created_at FROM backup_metadata ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        add("Último backup registrado", last_backup[0] if last_backup else "nenhum", "ok" if last_backup else "atencao")
    except Exception:
        add("Último backup registrado", "indisponível", "atencao")

    try:
        last_bench = con.execute(
            "SELECT started_at FROM benchmark_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        add("Último benchmark executado", last_bench[0] if last_bench else "nenhum")
    except Exception:
        pass

    statuses = [c["status"] for c in health["checks"]]
    if "critico" in statuses:
        health["overall"] = "critico"
    elif "atencao" in statuses:
        health["overall"] = "atencao"
    else:
        health["overall"] = "ok"
    return health


# ---------------------------------------------------------------------------
# 6. Detailed statistics
# ---------------------------------------------------------------------------

_STAT_PARAMS = ("exposure", "shadows", "temperature", "tint", "brightness", "visual_contrast")
_GROUP_COLUMNS = {
    "lens": "lens",
    "preset": "preset_name",
    "camera": "camera",
    "iso": "iso",
}


def _describe(values: list[float]) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"count": 0}
    values_sorted = sorted(values)
    n = len(values_sorted)
    out = {
        "count": n,
        "min": values_sorted[0],
        "max": values_sorted[-1],
        "mean": statistics.fmean(values_sorted),
        "median": statistics.median(values_sorted),
    }
    if n > 1:
        out["stddev"] = statistics.stdev(values_sorted)
    med = out["median"]
    out["mad"] = statistics.median([abs(v - med) for v in values_sorted])
    for p in (10, 25, 75, 90):
        idx = min(n - 1, max(0, round((p / 100) * (n - 1))))
        out[f"p{p}"] = values_sorted[idx]
    return out


def get_parameter_statistics(
    con: sqlite3.Connection,
    group_by: Optional[str] = None,
    filters: Optional[dict] = None,
) -> list[dict]:
    """Per-parameter descriptive statistics, optionally grouped by lens /
    preset / camera / iso. Read-only, informational — never used by the
    suggestion algorithm."""
    filters = filters or {}
    where = []
    args: list = []
    for col, val in filters.items():
        if col in ("lens", "preset_name", "camera") and val:
            where.append(f"{col} = ?")
            args.append(val)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    group_col = _GROUP_COLUMNS.get(group_by)
    results: list[dict] = []

    if group_col:
        groups = [r[0] for r in con.execute(
            f"SELECT DISTINCT {group_col} FROM photos{where_sql}", args
        ).fetchall()]
        for g in groups:
            group_where = where_sql + (" AND " if where_sql else " WHERE ") + f"{group_col} IS ?"
            row_out = {"group": g}
            for param in _STAT_PARAMS:
                vals = [r[0] for r in con.execute(
                    f"SELECT {param} FROM photos{group_where} AND {param} IS NOT NULL"
                    if where_sql else f"SELECT {param} FROM photos WHERE {group_col} IS ? AND {param} IS NOT NULL",
                    args + [g],
                ).fetchall()]
                row_out[param] = _describe(vals)
            results.append(row_out)
    else:
        row_out = {"group": "todos"}
        for param in _STAT_PARAMS:
            vals = [r[0] for r in con.execute(
                f"SELECT {param} FROM photos{where_sql}"
                + (" AND " if where_sql else " WHERE ") + f"{param} IS NOT NULL",
                args,
            ).fetchall()]
            row_out[param] = _describe(vals)
        results.append(row_out)

    quarantine_counts = con.execute(
        "SELECT status, COUNT(*) FROM outlier_quarantine GROUP BY status"
    ).fetchall()
    for row in results:
        row["quarantine_counts"] = {s: c for s, c in quarantine_counts}
    return results


def export_statistics_csv(stats: list[dict], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["group", "parameter", "count", "min", "max", "mean", "median", "stddev", "mad", "p10", "p25", "p75", "p90"])
        for row in stats:
            for param in _STAT_PARAMS:
                d = row.get(param, {})
                writer.writerow([
                    row.get("group"), param, d.get("count", 0), d.get("min"), d.get("max"),
                    d.get("mean"), d.get("median"), d.get("stddev"), d.get("mad"),
                    d.get("p10"), d.get("p25"), d.get("p75"), d.get("p90"),
                ])
    return out_path


def export_statistics_json(stats: list[dict], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# 7. Versioned backup & rollback
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def create_backup(
    con: sqlite3.Connection,
    db_path: Path,
    backups_dir: Path,
    reason: str = "",
    is_manual: bool = False,
) -> dict:
    """Timestamped, integrity-checked, versioned backup of the SQLite bank.
    Uses sqlite3's online backup API so it is safe even while the DB is open
    (no need to close the live connection)."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backups_dir / f"{db_path.stem}_backup_{ts}.db"

    dest_con = sqlite3.connect(str(dest))
    try:
        con.backup(dest_con)
    finally:
        dest_con.close()

    integrity_con = sqlite3.connect(str(dest))
    try:
        integrity = integrity_con.execute("PRAGMA integrity_check").fetchone()[0]
        record_count = integrity_con.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    finally:
        integrity_con.close()

    sha = _sha256_file(dest)
    size = dest.stat().st_size
    integrity_ok = integrity == "ok"

    con.execute(
        """INSERT INTO backup_metadata
           (backup_path, source_path, reason, size_bytes, sha256, record_count, integrity_ok, is_manual)
           VALUES (?,?,?,?,?,?,?,?)""",
        (str(dest), str(db_path), reason, size, sha, record_count, int(integrity_ok), int(is_manual)),
    )
    con.commit()

    return {
        "backup_path": str(dest),
        "reason": reason,
        "size_bytes": size,
        "sha256": sha,
        "record_count": record_count,
        "integrity_ok": integrity_ok,
        "is_manual": is_manual,
    }


def list_backups(con: sqlite3.Connection) -> list[dict]:
    cur = con.cursor()
    cur.row_factory = sqlite3.Row
    return [dict(r) for r in cur.execute(
        "SELECT * FROM backup_metadata ORDER BY created_at DESC"
    ).fetchall()]


def validate_backup(backup_path: Path) -> dict:
    if not backup_path.exists():
        return {"ok": False, "reason": "arquivo não encontrado"}
    try:
        test_con = sqlite3.connect(str(backup_path))
        try:
            integrity = test_con.execute("PRAGMA integrity_check").fetchone()[0]
            count = test_con.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        finally:
            test_con.close()
        return {"ok": integrity == "ok", "integrity": integrity, "record_count": count}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


def restore_backup(db_path: Path, backup_path: Path, logger: Optional[logging.Logger] = None) -> dict:
    """Atomic restore: backup the CURRENT state first (safety-of-safety),
    validate the target backup, swap the files, verify the result, and
    automatically revert to the pre-restore state if anything goes wrong."""
    validation = validate_backup(backup_path)
    if not validation.get("ok"):
        return {"ok": False, "stage": "validate_target", "detail": validation}

    pre_restore_copy = db_path.with_suffix(db_path.suffix + f".pre_restore_{int(time.time())}")
    try:
        if db_path.exists():
            shutil.copy2(str(db_path), str(pre_restore_copy))
    except Exception as exc:
        return {"ok": False, "stage": "backup_current", "detail": str(exc)}

    try:
        shutil.copy2(str(backup_path), str(db_path))
        post = validate_backup(db_path)
        if not post.get("ok"):
            raise RuntimeError(f"verificação pós-restauração falhou: {post}")
        try:
            pre_restore_copy.unlink(missing_ok=True)
        except Exception:
            pass
        if logger:
            logger.info(f"Restauração concluída a partir de {backup_path.name}.")
        return {"ok": True, "restored_from": str(backup_path)}
    except Exception as exc:
        # Auto-revert
        try:
            if pre_restore_copy.exists():
                shutil.copy2(str(pre_restore_copy), str(db_path))
        except Exception:
            pass
        if logger:
            logger.error(f"Restauração falhou e foi revertida automaticamente: {exc}")
        return {"ok": False, "stage": "swap_or_verify", "detail": str(exc), "reverted": True}


# ---------------------------------------------------------------------------
# 8. Structured rotating logging + diagnostics export
# ---------------------------------------------------------------------------

def setup_structured_logging(logs_dir: Path) -> logging.Logger:
    """Adds rotating, leveled handlers to the SAME 'lightroom_assistant'
    logger core.setup_logger() configures, WITHOUT removing the existing
    plain-file handler (so v11 log behavior/content is untouched — this is
    purely additive handlers). Safe to call multiple times (checks for a
    marker attribute).

    Never logs full binary payloads (thumbnails, catalog bytes) or secrets —
    only text messages the app itself constructs, exactly like v11 already
    does."""
    logger = logging.getLogger("lightroom_assistant")
    if getattr(logger, "_v12_structured_handlers_installed", False):
        return logger

    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    specs = [
        ("app.log", logging.INFO),
        ("database.log", logging.DEBUG),
        ("processing.log", logging.DEBUG),
        ("errors.log", logging.WARNING),
        ("benchmark.log", logging.DEBUG),
    ]
    for filename, level in specs:
        try:
            handler = RotatingFileHandler(
                logs_dir / filename, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
            )
            handler.setLevel(level)
            handler.setFormatter(fmt)
            logger.addHandler(handler)
        except Exception as exc:
            logging.getLogger("lightroom_assistant.safety").error(
                f"Não foi possível configurar log rotativo '{filename}': {exc}"
            )

    logger._v12_structured_handlers_installed = True
    return logger


def export_diagnostics_package(
    logs_dir: Path,
    db_path: Path,
    con: sqlite3.Connection,
    out_zip_path: Path,
    app_version: str = "v12",
) -> Path:
    """Bundle logs + schema + integrity + non-sensitive environment info
    into a zip. NEVER includes the actual bank database file."""
    import platform
    import zipfile

    out_zip_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    except Exception as exc:
        integrity = f"erro: {exc}"

    try:
        schema_v11 = con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    except Exception:
        schema_v11 = None
    try:
        schema_v12 = con.execute("SELECT MAX(version) FROM safety_schema_version").fetchone()[0]
    except Exception:
        schema_v12 = None

    manifest = {
        "app_version": app_version,
        "generated_at": datetime.now().isoformat(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "db_path": str(db_path),
        "db_size_bytes": db_path.stat().st_size if db_path.exists() else None,
        "schema_version_v11": schema_v11,
        "schema_version_v12": schema_v12,
        "integrity_check": integrity,
    }

    with zipfile.ZipFile(out_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        if logs_dir.exists():
            for f in logs_dir.glob("*.log*"):
                try:
                    zf.write(f, arcname=f"logs/{f.name}")
                except Exception:
                    continue
    return out_zip_path


# ---------------------------------------------------------------------------
# 9. Non-destructive benchmark
# ---------------------------------------------------------------------------

def run_technical_benchmark(con: sqlite3.Connection, logger: Optional[logging.Logger] = None) -> dict:
    """Manual-only, read-only timing/health benchmark. Never writes to
    `photos`, `ai_suggestions`, or `correction_log` — only to
    `benchmark_runs` (its own bookkeeping table)."""
    t0 = time.perf_counter()
    result: dict[str, Any] = {}

    t = time.perf_counter()
    n = con.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    result["count_photos_ms"] = (time.perf_counter() - t) * 1000
    result["photo_count"] = n

    t = time.perf_counter()
    con.execute(
        "SELECT * FROM photos WHERE exposure IS NOT NULL AND shadows IS NOT NULL LIMIT 200"
    ).fetchall()
    result["sample_query_ms"] = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    result["integrity_check_ms"] = (time.perf_counter() - t) * 1000
    result["integrity_ok"] = integrity == "ok"

    cache_stat = cache_stats(con)
    result["cache"] = cache_stat

    result["total_ms"] = (time.perf_counter() - t0) * 1000

    con.execute(
        "INSERT INTO benchmark_runs (kind, duration_ms, sample_size, result_json) VALUES (?,?,?,?)",
        ("technical", result["total_ms"], n, json.dumps(result)),
    )
    con.commit()
    if logger:
        logger.info(f"Benchmark técnico concluído em {result['total_ms']:.1f}ms ({n} fotos no banco).")
    return result


def run_stability_benchmark(
    con: sqlite3.Connection,
    suggest_fn: Callable[..., Any],
    logger: Optional[logging.Logger] = None,
    sample_size: int = 20,
) -> dict:
    """Re-runs the EXISTING suggestion function (passed in by the caller —
    this module never reimplements or alters the algorithm) on a fixed
    sample of existing valid bank rows, twice, and checks the two runs
    produce the same output — a determinism/stability check, not an
    accuracy claim. Never writes corrections or touches the bank's learned
    data."""
    t0 = time.perf_counter()
    cur = con.cursor()
    cur.row_factory = sqlite3.Row
    rows = cur.execute(
        "SELECT * FROM photos WHERE exposure IS NOT NULL AND shadows IS NOT NULL "
        "ORDER BY id LIMIT ?",
        (sample_size,),
    ).fetchall()

    mismatches = []
    for row in rows:
        features = {
            "brightness": row["brightness"], "visual_contrast": row["visual_contrast"],
            "luminance_p10": row["luminance_p10"], "luminance_p25": row["luminance_p25"],
            "luminance_p50": row["luminance_p50"], "luminance_p75": row["luminance_p75"],
            "luminance_p90": row["luminance_p90"], "rg_ratio": row["rg_ratio"], "bg_ratio": row["bg_ratio"],
        }
        try:
            r1 = suggest_fn(con, features, row["preset_name"], logger or logging.getLogger("lightroom_assistant.safety"),
                             new_lens=row["lens"], new_camera=row["camera"])
            r2 = suggest_fn(con, features, row["preset_name"], logger or logging.getLogger("lightroom_assistant.safety"),
                             new_lens=row["lens"], new_camera=row["camera"])
        except Exception as exc:
            mismatches.append({"filename": row["filename"], "error": str(exc)})
            continue
        if r1 is None or r2 is None:
            continue
        if abs(r1.exposure - r2.exposure) > 1e-9 or abs(r1.shadows - r2.shadows) > 1e-9:
            mismatches.append({
                "filename": row["filename"],
                "run1": (r1.exposure, r1.shadows),
                "run2": (r2.exposure, r2.shadows),
            })

    result = {
        "sample_size": len(rows),
        "mismatches": mismatches,
        "stable": len(mismatches) == 0,
        "total_ms": (time.perf_counter() - t0) * 1000,
        "note": "Verifica apenas se o algoritmo é determinístico neste conjunto — "
                "não existe um 'gabarito' de qualidade para comparar (fora do escopo deste protótipo).",
    }
    con.execute(
        "INSERT INTO benchmark_runs (kind, duration_ms, sample_size, result_json) VALUES (?,?,?,?)",
        ("stability", result["total_ms"], len(rows), json.dumps(result)),
    )
    con.commit()
    if logger:
        logger.info(
            f"Benchmark de estabilidade: {len(rows)} amostra(s), "
            f"{'estável' if result['stable'] else str(len(mismatches)) + ' divergência(s)'}."
        )
    return result
