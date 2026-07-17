"""
Lightroom Assistant - Core Engine
==================================
Business logic (no GUI dependencies) for reading Lightroom Classic catalogs,
extracting develop settings (Exposure / Shadows) + visual/EXIF features,
storing them in a local SQLite bank, and suggesting + writing Exposure/Shadows
values into new catalogs based on similarity to previously edited photos.

This module can be imported and tested with plain python3 (no PySide6 needed).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import hashlib
import os
import re
import shutil
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

try:
    import safety  # v12 additive safety/diagnostics/performance layer
except ImportError:  # pragma: no cover
    safety = None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logger(logs_dir: Path) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("lightroom_assistant")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fh = logging.FileHandler(logs_dir / f"log_{ts}.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


# ---------------------------------------------------------------------------
# Folder auto-detection
# ---------------------------------------------------------------------------

def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def _norm(text: str) -> str:
    return _strip_accents(text).lower()


FOLDER_KEYWORDS = {
    "alimentar": "feed_catalogs",       # catalogs used to feed/train the database
    "editar": "catalogs_to_edit",       # new catalogs waiting to be edited
    "editado": "edited_catalogs",       # finished/output catalogs
    "editados": "edited_catalogs",
    "preset": "presets",
    "banco": "database",
    "backup": "backups",
    "log": "logs",
}


def detect_project_folders(base_dir: Path) -> dict[str, Path]:
    """Scan base_dir (recursively, shallow) for folders matching known roles.

    Matching rules (in priority order) to avoid 'editar' matching 'editados':
    - 'editados'/'editado' -> edited_catalogs
    - 'alimentar' -> feed_catalogs
    - 'editar' (but not already matched as edited) -> catalogs_to_edit
    - 'preset' -> presets
    - 'banco' -> database
    - 'backup' -> backups
    - 'log' -> logs
    """
    found: dict[str, Path] = {}
    if not base_dir.exists():
        return found

    for root, dirs, _files in os.walk(base_dir):
        for d in dirs:
            full = Path(root) / d
            name_norm = _norm(d)

            if "editado" in name_norm:
                found.setdefault("edited_catalogs", full)
            elif "alimentar" in name_norm:
                found.setdefault("feed_catalogs", full)
            elif "editar" in name_norm:
                found.setdefault("catalogs_to_edit", full)
            elif "preset" in name_norm:
                found.setdefault("presets", full)
            elif "banco" in name_norm:
                found.setdefault("database", full)
            elif "backup" in name_norm:
                found.setdefault("backups", full)
            elif "log" in name_norm:
                found.setdefault("logs", full)

    return found


# ---------------------------------------------------------------------------
# Database (our own SQLite bank - NOT the Lightroom catalog)
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
-- ---------------------------------------------------------------------------
-- schema_version: tracks applied migrations so future versions can upgrade
-- existing banks safely without re-running already-applied changes.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);

-- ---------------------------------------------------------------------------
-- photos: one row per photo ingested from a Lightroom catalog.
-- New columns are always added via _MIGRATIONS (never by dropping/recreating
-- the table) so existing banks are never lost on upgrade.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS photos (
    -- ---- Identity & file info ----
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_catalog      TEXT    NOT NULL,
    image_id_local      INTEGER,
    id_global           TEXT,               -- Adobe_images.id_global UUID
    file_path           TEXT,
    filename            TEXT,
    file_extension      TEXT,               -- e.g. "JPG", "CR3", "NEF", "ARW"
    file_size_bytes     INTEGER,

    -- ---- Lightroom catalog metadata ----
    rating              INTEGER,            -- star rating 0-5
    color_label         TEXT,               -- "Red","Yellow","Green","Blue","Purple","None"
    capture_time        TEXT,               -- ISO-8601 capture timestamp

    -- ---- GPS ----
    gps_latitude        REAL,
    gps_longitude       REAL,
    gps_altitude        REAL,

    -- ---- EXIF: camera / lens ----
    camera              TEXT,               -- camera model (AgInternedExifCameraModel)
    camera_make         TEXT,               -- manufacturer (Canon, Nikon, Sony…)
    lens                TEXT,               -- lens name   (AgInternedExifLens)
    focal_length        REAL,               -- mm
    focal_length_35mm   REAL,               -- 35 mm-equivalent
    aperture            REAL,               -- f-number
    shutter_speed       REAL,               -- seconds  (e.g. 0.001 = 1/1000s)
    iso                 REAL,
    flash               INTEGER,            -- 0 = no flash, 1 = fired
    metering_mode       INTEGER,
    exposure_program    INTEGER,
    exposure_bias       REAL,               -- EV compensation
    orientation         INTEGER,

    -- ---- Develop / develop settings (all main Lightroom sliders) ----
    preset_name         TEXT,
    process_version     TEXT,               -- "11.0"=PV2012, "6.7"=PV2010 …
    exposure            REAL,               -- Exposure2012
    contrast_dev        REAL,               -- Contrast2012
    highlights          REAL,               -- Highlights2012
    shadows             REAL,               -- Shadows2012
    whites              REAL,               -- Whites2012
    blacks              REAL,               -- Blacks2012
    clarity             REAL,               -- Clarity2012
    texture             REAL,               -- Texture (LR Classic 8+)
    dehaze              REAL,               -- Dehaze
    vibrance            REAL,
    saturation_dev      REAL,               -- Saturation slider
    temperature         REAL,               -- White balance Temperature (K)
    tint                REAL,               -- White balance Tint
    sharpness           REAL,
    noise_lum           REAL,               -- Noise reduction – Luminance
    noise_color         REAL,               -- Noise reduction – Color
    lens_profile_enable INTEGER,            -- 0/1
    ca_enable           INTEGER,            -- Chromatic-aberration correction 0/1
    vignette            REAL,               -- Post-crop vignette amount
    grain_amount        REAL,

    -- ---- Visual features (computed from preview / original image) ----
    brightness          REAL,               -- mean luminance 0-255
    visual_contrast     REAL,               -- std-dev luminance
    luminance_p10       REAL,
    luminance_p25       REAL,
    luminance_p50       REAL,
    luminance_p75       REAL,
    luminance_p90       REAL,
    rg_ratio            REAL,               -- gray-world R/G mean ratio
    bg_ratio            REAL,               -- gray-world B/G mean ratio
    mean_r              REAL,               -- mean red channel 0-255
    mean_g              REAL,               -- mean green channel 0-255
    mean_b              REAL,               -- mean blue channel 0-255
    saturation_mean     REAL,               -- mean HSV saturation 0-1
    dominant_hue        REAL,               -- dominant hue angle 0-360
    histogram_json      TEXT,               -- 32-bin luminance histogram (JSON)
    histogram_rgb_json  TEXT,               -- 32-bin per-channel RGB histogram (JSON)
    thumbnail_webp      BLOB,               -- ~128 px WebP thumbnail bytes
    has_image_features  INTEGER DEFAULT 0,

    created_at          TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL,   -- 'feed' or 'edit'
    catalog_path TEXT,
    photos_count INTEGER,
    notes        TEXT,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- image_features: compositional / AI features per photo (populated by future
-- AI/detection passes — not filled during normal feed). Kept separate from
-- photos so a heavy AI column never slows down the basic KNN queries.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS image_features (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id            INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    -- Scene / compositional classification
    room_type           TEXT,               -- "living_room", "bedroom", "kitchen", …
    has_people          INTEGER,            -- 0/1
    has_reflection      INTEGER,            -- 0/1
    has_tv              INTEGER,            -- 0/1
    has_window          INTEGER,            -- 0/1
    window_area         REAL,               -- fraction of image area, 0-1
    window_direction    TEXT,               -- "left","right","center","full"
    has_pool            INTEGER,            -- 0/1
    has_bathroom        INTEGER,            -- 0/1
    has_kitchen         INTEGER,            -- 0/1
    has_balcony         INTEGER,            -- 0/1
    -- Aesthetic / quality scores (future AI)
    lux_score           REAL,               -- perceived brightness quality 0-1
    composition_score   REAL,               -- rule-of-thirds / compositional 0-1
    symmetry_score      REAL,               -- bilateral symmetry 0-1
    lines_score         REAL,               -- leading lines strength 0-1
    color_temperature   REAL,               -- scene color temperature (K)
    -- Embedding vectors (store as raw float32 bytes)
    ai_embedding        BLOB,               -- generic model embedding
    clip_embedding      BLOB,               -- OpenAI CLIP (512/768/1024 float32)
    created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(photo_id)
);

-- ---------------------------------------------------------------------------
-- edits_history: one row per photo per edit run — lets the AI learn how your
-- style evolves over time and enables "why did it suggest X?" explanations.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS edits_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id        INTEGER REFERENCES photos(id) ON DELETE SET NULL,
    run_id          INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    catalog_path    TEXT    NOT NULL,
    filename        TEXT,
    image_id_local  INTEGER,
    -- Values actually applied
    preset          TEXT,
    exposure        REAL,
    shadows         REAL,
    temperature     REAL,
    tint            REAL,
    correction_note TEXT,
    edited_at       TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_edits_history_photo_id ON edits_history(photo_id);
CREATE INDEX IF NOT EXISTS idx_edits_history_run_id   ON edits_history(run_id);

-- ---------------------------------------------------------------------------
-- objects: object-detection results per photo.  Each column is a boolean
-- (0/1) flag; detected_count is the total number of distinct object classes
-- detected. Populated by a future detection pass.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS objects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id        INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    person          INTEGER DEFAULT 0,
    reflection      INTEGER DEFAULT 0,
    car             INTEGER DEFAULT 0,
    tv              INTEGER DEFAULT 0,
    window          INTEGER DEFAULT 0,
    pool            INTEGER DEFAULT 0,
    tree            INTEGER DEFAULT 0,
    kitchen         INTEGER DEFAULT 0,
    bed             INTEGER DEFAULT 0,
    dining_table    INTEGER DEFAULT 0,
    mirror          INTEGER DEFAULT 0,
    detected_count  INTEGER DEFAULT 0,      -- total distinct classes found
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(photo_id)
);

-- ---------------------------------------------------------------------------
-- realty_signs: real-estate sign / agency detection per photo.
-- Populated by a separate detection pass (e.g. YOLO, template matching, OCR).
-- Kept in its own table so detection can be run or re-run independently of
-- the main feed without touching the photos row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS realty_signs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id            INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    has_sign            INTEGER,            -- 0 = no sign, 1 = sign detected
    confidence          REAL,              -- detection confidence 0-1
    agency_name         TEXT,              -- e.g. "Lello", "RE/MAX", "Apolar"
    sign_color          TEXT,              -- dominant color of the sign (hex or label)
    sign_position       TEXT,              -- "foreground", "background", "left", "right"
    sign_area_fraction  REAL,              -- fraction of image area occupied 0-1
    ocr_text            TEXT,              -- raw OCR output from the sign
    extra_json          TEXT,              -- JSON blob for future fields
    detected_at         TEXT DEFAULT CURRENT_TIMESTAMP,
    model_version       TEXT,              -- which model/version produced this result
    UNIQUE(photo_id)
);
CREATE INDEX IF NOT EXISTS idx_realty_signs_has_sign ON realty_signs(has_sign);

-- ---------------------------------------------------------------------------
-- ai_suggestions: snapshot of every suggestion the program made at edit time.
-- This is the "what did the AI think?" record.  Combined with correction_log
-- it gives us explicit error signals for bias learning.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_suggestions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    catalog_path        TEXT    NOT NULL,
    filename            TEXT,
    image_id_local      INTEGER,
    suggested_exposure  REAL,
    suggested_shadows   REAL,
    -- Feature snapshot that drove this suggestion (for bias KNN)
    brightness          REAL,
    visual_contrast     REAL,
    luminance_p10       REAL,
    luminance_p25       REAL,
    luminance_p50       REAL,
    luminance_p75       REAL,
    luminance_p90       REAL,
    rg_ratio            REAL,
    bg_ratio            REAL,
    preset_name         TEXT,              -- develop preset in effect when suggested (bias-by-preset reports)
    camera              TEXT,              -- camera model (bias-by-camera reports)
    created_at          TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ai_suggestions_catalog ON ai_suggestions(catalog_path, filename);

-- ---------------------------------------------------------------------------
-- correction_log: explicit error signals produced when the user reimports a
-- catalog they have finished editing in Lightroom.
-- error_exposure > 0  → AI underexposed (human needed more exposure)
-- error_exposure < 0  → AI overexposed  (human needed less exposure)
-- Same sign convention for shadows.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS correction_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_path        TEXT    NOT NULL,
    filename            TEXT,
    image_id_local      INTEGER,
    error_exposure      REAL,           -- human_final − ai_suggested
    error_shadows       REAL,
    -- Feature snapshot at correction time (for nearest-neighbour bias lookup)
    brightness          REAL,
    visual_contrast     REAL,
    luminance_p10       REAL,
    luminance_p25       REAL,
    luminance_p50       REAL,
    preset_name         TEXT,              -- develop preset in effect (bias-by-preset reports)
    camera              TEXT,              -- camera model (bias-by-camera reports)
    corrected_at        TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_correction_log_catalog ON correction_log(catalog_path);
"""

# ---------------------------------------------------------------------------
# Migration list — every column ever added after the first public release.
# Format: (column_name, ALTER TABLE DDL).
# init_database() checks PRAGMA table_info(photos) and applies only missing
# columns, so re-running on an existing bank is always safe.
# ---------------------------------------------------------------------------
_MIGRATIONS: list[tuple[str, str]] = [
    # v1 → v2: White balance + color ratios + lens/camera
    ("temperature",         "ALTER TABLE photos ADD COLUMN temperature REAL"),
    ("tint",                "ALTER TABLE photos ADD COLUMN tint REAL"),
    ("lens",                "ALTER TABLE photos ADD COLUMN lens TEXT"),
    ("camera",              "ALTER TABLE photos ADD COLUMN camera TEXT"),
    ("rg_ratio",            "ALTER TABLE photos ADD COLUMN rg_ratio REAL"),
    ("bg_ratio",            "ALTER TABLE photos ADD COLUMN bg_ratio REAL"),
    # v2 → v3: Full EXIF, all develop sliders, rich visual features
    ("id_global",           "ALTER TABLE photos ADD COLUMN id_global TEXT"),
    ("file_extension",      "ALTER TABLE photos ADD COLUMN file_extension TEXT"),
    ("file_size_bytes",     "ALTER TABLE photos ADD COLUMN file_size_bytes INTEGER"),
    ("rating",              "ALTER TABLE photos ADD COLUMN rating INTEGER"),
    ("color_label",         "ALTER TABLE photos ADD COLUMN color_label TEXT"),
    ("capture_time",        "ALTER TABLE photos ADD COLUMN capture_time TEXT"),
    ("gps_latitude",        "ALTER TABLE photos ADD COLUMN gps_latitude REAL"),
    ("gps_longitude",       "ALTER TABLE photos ADD COLUMN gps_longitude REAL"),
    ("gps_altitude",        "ALTER TABLE photos ADD COLUMN gps_altitude REAL"),
    ("camera_make",         "ALTER TABLE photos ADD COLUMN camera_make TEXT"),
    ("focal_length_35mm",   "ALTER TABLE photos ADD COLUMN focal_length_35mm REAL"),
    ("flash",               "ALTER TABLE photos ADD COLUMN flash INTEGER"),
    ("metering_mode",       "ALTER TABLE photos ADD COLUMN metering_mode INTEGER"),
    ("exposure_program",    "ALTER TABLE photos ADD COLUMN exposure_program INTEGER"),
    ("exposure_bias",       "ALTER TABLE photos ADD COLUMN exposure_bias REAL"),
    ("orientation",         "ALTER TABLE photos ADD COLUMN orientation INTEGER"),
    ("process_version",     "ALTER TABLE photos ADD COLUMN process_version TEXT"),
    ("contrast_dev",        "ALTER TABLE photos ADD COLUMN contrast_dev REAL"),
    ("highlights",          "ALTER TABLE photos ADD COLUMN highlights REAL"),
    ("whites",              "ALTER TABLE photos ADD COLUMN whites REAL"),
    ("blacks",              "ALTER TABLE photos ADD COLUMN blacks REAL"),
    ("clarity",             "ALTER TABLE photos ADD COLUMN clarity REAL"),
    ("texture",             "ALTER TABLE photos ADD COLUMN texture REAL"),
    ("dehaze",              "ALTER TABLE photos ADD COLUMN dehaze REAL"),
    ("vibrance",            "ALTER TABLE photos ADD COLUMN vibrance REAL"),
    ("saturation_dev",      "ALTER TABLE photos ADD COLUMN saturation_dev REAL"),
    ("sharpness",           "ALTER TABLE photos ADD COLUMN sharpness REAL"),
    ("noise_lum",           "ALTER TABLE photos ADD COLUMN noise_lum REAL"),
    ("noise_color",         "ALTER TABLE photos ADD COLUMN noise_color REAL"),
    ("lens_profile_enable", "ALTER TABLE photos ADD COLUMN lens_profile_enable INTEGER"),
    ("ca_enable",           "ALTER TABLE photos ADD COLUMN ca_enable INTEGER"),
    ("vignette",            "ALTER TABLE photos ADD COLUMN vignette REAL"),
    ("grain_amount",        "ALTER TABLE photos ADD COLUMN grain_amount REAL"),
    ("visual_contrast",     "ALTER TABLE photos ADD COLUMN visual_contrast REAL"),
    ("mean_r",              "ALTER TABLE photos ADD COLUMN mean_r REAL"),
    ("mean_g",              "ALTER TABLE photos ADD COLUMN mean_g REAL"),
    ("mean_b",              "ALTER TABLE photos ADD COLUMN mean_b REAL"),
    ("saturation_mean",     "ALTER TABLE photos ADD COLUMN saturation_mean REAL"),
    ("dominant_hue",        "ALTER TABLE photos ADD COLUMN dominant_hue REAL"),
    ("histogram_rgb_json",  "ALTER TABLE photos ADD COLUMN histogram_rgb_json TEXT"),
    ("thumbnail_webp",      "ALTER TABLE photos ADD COLUMN thumbnail_webp BLOB"),
    # v3 → v4: thumbnail disk cache
    ("thumbnail_path",      "ALTER TABLE photos ADD COLUMN thumbnail_path TEXT"),
    ("schema_version",      "SELECT 1"),    # sentinel – never a real ALTER, just marks v4 applied
    # v4 → v5: ai_suggestions / correction_log / realty_signs tables (new; no ALTER needed),
    # plus new columns on image_features for room_confidence and scene_tags
    ("room_confidence",
     "ALTER TABLE image_features ADD COLUMN room_confidence REAL"),
    ("scene_tags",
     "ALTER TABLE image_features ADD COLUMN scene_tags TEXT"),  # JSON list
    # v5 → v6: link an ai_suggestions snapshot to the FINAL output catalog
    # path (the renamed "..._editado_<timestamp>.lrcat" file the program
    # writes), not just the original input path. Without this, reimporting
    # the program-edited catalog for feedback ("Modo de atualização") could
    # never find its own suggestions — catalog_path matched the *original*
    # "editar" file, but the user always reimports the *renamed* "editado"
    # file, so the lookup found nothing and no bias was ever learned.
    ("edited_catalog_path",
     "ALTER TABLE ai_suggestions ADD COLUMN edited_catalog_path TEXT"),
    # v6 → v7: correction_log rows now remember which ai_suggestions row they
    # came from, with a UNIQUE index on it, so re-feeding the same "editado"
    # catalog twice (e.g. the user re-imports it, or touches it again after
    # more corrections) REPLACES the previous correction instead of adding a
    # second, duplicate error sample that would double-count the same human
    # edit in the bias-learning average.
    ("suggestion_id",
     "ALTER TABLE correction_log ADD COLUMN suggestion_id INTEGER"),
    ("idx_correction_log_suggestion_unique",
     "CREATE UNIQUE INDEX IF NOT EXISTS idx_correction_log_suggestion_unique "
     "ON correction_log(suggestion_id)"),
    # v7 → v8: preset/camera captured alongside each suggestion and
    # correction, so the bias report can break error down by preset and by
    # camera model instead of only a single global number (see
    # get_bias_report's "by_preset"/"by_camera" sections).
    ("preset_name_ai_suggestions",
     "ALTER TABLE ai_suggestions ADD COLUMN preset_name TEXT"),
    ("camera_ai_suggestions",
     "ALTER TABLE ai_suggestions ADD COLUMN camera TEXT"),
    ("preset_name_correction_log",
     "ALTER TABLE correction_log ADD COLUMN preset_name TEXT"),
    ("camera_correction_log",
     "ALTER TABLE correction_log ADD COLUMN camera TEXT"),
]

# Schema version recorded inside the DB so future tools know what level the
# bank was built at without counting migration rows manually.
_CURRENT_SCHEMA_VERSION = 8


def _get_thumbs_dir(con: sqlite3.Connection) -> Optional[Path]:
    """Derive the thumbs/ folder path from the open bank connection.

    Uses PRAGMA database_list to get the actual .db file path, then returns
    a 'thumbs' subfolder next to it.  Returns None if the connection is
    in-memory or the path can't be determined."""
    try:
        row = con.execute("PRAGMA database_list").fetchone()
        if row and row[2]:  # row[2] = file path, empty for :memory:
            return Path(row[2]).parent / "thumbs"
    except Exception:
        pass
    return None


def _save_thumbnail_to_disk(thumbs_dir: Path, photo_id: int, webp_bytes: bytes) -> Optional[str]:
    """Write thumbnail bytes to thumbs/<photo_id>.webp and return the path string.

    Returns None on any IO error (thumbnail loss is acceptable)."""
    try:
        thumbs_dir.mkdir(parents=True, exist_ok=True)
        dest = thumbs_dir / f"{photo_id}.webp"
        dest.write_bytes(webp_bytes)
        return str(dest)
    except Exception:
        return None


def _migrate_thumbnails_to_disk(con: sqlite3.Connection, logger: logging.Logger) -> None:
    """One-time migration: extract thumbnail BLOBs already in the DB to disk.

    Finds all rows where thumbnail_webp IS NOT NULL and thumbnail_path IS NULL,
    writes each BLOB to thumbs/<id>.webp, updates thumbnail_path, and NULLs the
    BLOB to reclaim space.  Safe to call repeatedly — already-migrated rows are
    skipped automatically."""
    thumbs_dir = _get_thumbs_dir(con)
    if thumbs_dir is None:
        return  # in-memory DB or path unknown — skip

    try:
        rows = con.execute(
            "SELECT id, thumbnail_webp FROM photos "
            "WHERE thumbnail_webp IS NOT NULL AND (thumbnail_path IS NULL OR thumbnail_path = '')"
        ).fetchall()
    except Exception:
        return

    if not rows:
        return

    migrated = 0
    for photo_id, blob in rows:
        if not blob:
            continue
        path = _save_thumbnail_to_disk(thumbs_dir, photo_id, blob)
        if path:
            con.execute(
                "UPDATE photos SET thumbnail_path = ?, thumbnail_webp = NULL WHERE id = ?",
                (path, photo_id),
            )
            migrated += 1

    if migrated:
        con.commit()
        logger.info(f"Thumbnails migrados para disco: {migrated} arquivo(s) em {thumbs_dir}")


def init_database(db_path: Path) -> sqlite3.Connection:
    """Create (or open and upgrade) the local SQLite bank.

    Safe to call on any existing bank from any previous version of this app:
    it only adds new columns / tables and never drops or renames anything.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the GUI runs feed/edit jobs on a background
    # QThread (one at a time, never concurrently), while this connection is
    # created on the main thread. SQLite refuses cross-thread use by default,
    # so we relax that check here — safe because the app never touches this
    # connection from two threads at the same time.
    con = sqlite3.connect(str(db_path), check_same_thread=False)
    con.row_factory = sqlite3.Row   # allow dict-style column access everywhere
    con.executescript(SCHEMA_SQL)
    con.commit()

    # Apply all migrations via try-except so they work for ANY source table
    # (not just `photos`).  ALTER TABLE ADD COLUMN is idempotent under the
    # except clause: if the column already exists SQLite raises OperationalError
    # which we swallow, so running on an already-migrated DB is always safe.
    for _col, ddl in _MIGRATIONS:
        try:
            con.execute(ddl)
        except sqlite3.OperationalError:
            pass
    con.commit()

    # Record / update schema version
    current = con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
    if current < _CURRENT_SCHEMA_VERSION:
        con.execute(
            "INSERT OR REPLACE INTO schema_version (version, description) VALUES (?,?)",
            (_CURRENT_SCHEMA_VERSION,
             f"v{_CURRENT_SCHEMA_VERSION}: ai_suggestions + correction_log + realty_signs + bias learning"),
        )
        con.commit()

    # One-time migration: move existing thumbnail BLOBs out of the DB onto disk.
    _noop_logger = logging.getLogger("lightroom_assistant.init_noop")
    _noop_logger.addHandler(logging.NullHandler())
    _migrate_thumbnails_to_disk(con, _noop_logger)

    # v12: additive-only safety/diagnostics schema (outlier quarantine, lens
    # aliases, cache, backup metadata, benchmark history). Wrapped so a
    # failure here can never prevent the v11 bank from opening.
    if safety is not None:
        try:
            safety.init_safety_schema(con, _noop_logger)
        except Exception:
            pass

    return con


def count_photos(con: sqlite3.Connection) -> int:
    cur = con.execute("SELECT COUNT(*) FROM photos")
    return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Bias learning: AI suggestion tracking + correction error computation
# ---------------------------------------------------------------------------

# Feature dimensions used for bias-correction KNN.  Values in correction_log
# are stored in their natural 0-255 (luminance) or unit-less (contrast) scale;
# we normalize by these fixed max-ranges so no per-run normalization is needed.
_BIAS_DIMS:   list[str]   = ["brightness", "visual_contrast", "luminance_p10", "luminance_p25", "luminance_p50"]
_BIAS_RANGES: dict[str, float] = {
    "brightness":      255.0,
    "visual_contrast": 128.0,
    "luminance_p10":   255.0,
    "luminance_p25":   255.0,
    "luminance_p50":   255.0,
}


def _save_ai_suggestions(
    con: sqlite3.Connection,
    run_id: int,
    catalog_path: str,
    photos: list,
    report_rows: list,
    features_by_filename: dict,
    edited_catalog_path: Optional[str] = None,
) -> None:
    """Persist the AI suggestions made for this edit run to ai_suggestions.

    This is the "what did the AI think?" snapshot.  When the user reimports the
    corrected catalog later, the delta between the human values and these
    suggestions becomes the error signal in correction_log.

    Parameters
    ----------
    catalog_path : str
        The ORIGINAL input catalog path (the file that was read from the
        "editar" folder).
    edited_catalog_path : str, optional
        The FINAL output catalog path the program actually wrote to (e.g. the
        renamed "..._editado_<timestamp>.lrcat" in the "editado" folder, or
        the same path as catalog_path for in-place edits). This is what the
        user will later reimport for feedback, so it must be stored too —
        otherwise reimporting that file can never be matched back to its own
        suggestions (see _compute_and_save_corrections).
    features_by_filename : {filename: feature_dict_or_None}
        Visual-feature dict (from extract_visual_features) for each photo, or
        None when no image data was available.  Stored as a feature snapshot
        alongside the suggestion so bias KNN can be anchored to real feature
        values, not reconstructed approximations.
    """
    photo_by_name = {p.filename: p for p in photos}
    for filename, exp, sha, _temp, _tint, _note in report_rows:
        if exp is None:
            continue
        p     = photo_by_name.get(filename)
        feats = features_by_filename.get(filename) or {}
        con.execute(
            """
            INSERT INTO ai_suggestions
                (run_id, catalog_path, edited_catalog_path, filename, image_id_local,
                 suggested_exposure, suggested_shadows,
                 brightness, visual_contrast,
                 luminance_p10, luminance_p25, luminance_p50,
                 luminance_p75, luminance_p90, rg_ratio, bg_ratio,
                 preset_name, camera)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id, catalog_path, edited_catalog_path or catalog_path, filename,
                p.image_id_local if p else None,
                exp, sha,
                feats.get("brightness"),        feats.get("visual_contrast"),
                feats.get("luminance_p10"),     feats.get("luminance_p25"),
                feats.get("luminance_p50"),     feats.get("luminance_p75"),
                feats.get("luminance_p90"),     feats.get("rg_ratio"),
                feats.get("bg_ratio"),
                p.preset_name if p else None,   p.camera if p else None,
            ),
        )
    con.commit()


def _compute_and_save_corrections(
    con: sqlite3.Connection,
    catalog_path: str,
    photos: list,
    logger: logging.Logger,
) -> int:
    """Compare current (human-corrected) catalog values against stored AI
    suggestions for the same catalog and save error deltas to correction_log.

    Call this BEFORE deleting old bank rows during a force-update reimport, so
    the old values are not lost before corrections can be computed.

    Error convention:
        error_exposure > 0  →  AI underexposed (human boosted exposure further)
        error_exposure < 0  →  AI overexposed  (human pulled exposure down)

    Returns the number of correction rows inserted.
    """
    # Prefer matching by image_id_local: the "editado" catalog is a literal
    # copy of the same catalog file (never rebuilt/reimported into a new
    # one), so Lightroom's internal per-photo id is identical between the
    # snapshot taken when the suggestion was saved and this later read. That
    # makes it a much safer join key than filename alone, which collides
    # whenever two photos in different subfolders share a name (e.g.
    # "Imovel_A/IMG_0001.CR3" and "Imovel_B/IMG_0001.CR3" both reduce to
    # "IMG_0001.CR3" and would otherwise silently overwrite one another).
    photo_by_id = {
        p.image_id_local: p for p in photos
        if getattr(p, "image_id_local", None) is not None
    }
    photo_by_name = {p.filename: p for p in photos}
    # Match on EITHER the original input path OR the final output path the
    # program wrote to. The user normally reimports the "..._editado_..."
    # file from the "editado" folder for feedback — that path never equals
    # the original "editar" path used when the suggestion was first saved,
    # so matching on catalog_path alone silently found nothing and no
    # correction was ever learned. This is the "retroalimentação" fix.
    suggestions = con.execute(
        """SELECT id AS suggestion_id, filename, image_id_local,
                  suggested_exposure, suggested_shadows,
                  brightness, visual_contrast,
                  luminance_p10, luminance_p25, luminance_p50,
                  preset_name, camera
           FROM ai_suggestions
           WHERE catalog_path = ? OR edited_catalog_path = ?""",
        (catalog_path, catalog_path),
    ).fetchall()

    if not suggestions:
        logger.info("Nenhuma sugestão anterior armazenada para este catálogo — correction_log não atualizado.")
        return 0

    saved = 0
    for row in suggestions:
        fname    = row["filename"]
        img_id   = row["image_id_local"]
        sugg_exp = row["suggested_exposure"]
        sugg_sha = row["suggested_shadows"]
        if sugg_exp is None or sugg_sha is None:
            continue
        p = photo_by_id.get(img_id) if img_id is not None else None
        if p is None:
            p = photo_by_name.get(fname)
        if p is None or p.exposure is None or p.shadows is None:
            continue

        error_exp = p.exposure - sugg_exp   # + = AI underexposed
        error_sha = p.shadows  - sugg_sha

        # Prefer the freshly re-read photo's own preset/camera (it reflects
        # what's actually in the reimported catalog right now); fall back to
        # what was snapshotted at suggestion time if the photo object
        # doesn't have it for some reason.
        preset_name = getattr(p, "preset_name", None) or row["preset_name"]
        camera      = getattr(p, "camera", None) or row["camera"]

        # INSERT OR REPLACE + the UNIQUE(suggestion_id) index: if this exact
        # suggestion was already scored once before (e.g. the user re-feeds
        # the same "editado" catalog again), this REPLACES that prior
        # correction row instead of adding a second copy of the same human
        # edit — otherwise the bias-learning average would double-count it.
        con.execute(
            """
            INSERT OR REPLACE INTO correction_log
                (suggestion_id, catalog_path, filename, image_id_local,
                 error_exposure, error_shadows,
                 brightness, visual_contrast,
                 luminance_p10, luminance_p25, luminance_p50,
                 preset_name, camera)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["suggestion_id"],
                catalog_path, fname, p.image_id_local,
                error_exp, error_sha,
                row["brightness"],      row["visual_contrast"],
                row["luminance_p10"],   row["luminance_p25"],
                row["luminance_p50"],
                preset_name, camera,
            ),
        )
        saved += 1

    con.commit()
    if saved:
        logger.info(
            f"Retroalimentação: {saved} erro(s) calculado(s) e salvos no correction_log "
            f"(catálogo: {Path(catalog_path).name})."
        )
    return saved


def _bias_confidence(n_corrections: int, midpoint: float = 25.0, power: float = 0.7) -> float:
    """Shared 0..1 confidence ramp used both to attenuate the KNN bias
    correction and to show the user how much the learned bias can be
    trusted. Reaches 1.0 at `midpoint` corrections; sub-linear (power<1) so
    the climb from 0 is fast enough to feel like progress early on, but
    still requires real volume before hitting full trust."""
    return min(1.0, max(0.0, n_corrections / midpoint) ** power)


# "Good enough" tolerance used for the accuracy-rate metric: a correction
# within this band means the AI suggestion was close enough that we count
# it as a hit rather than a miss. Kept generous - these are starting points
# for a human edit pass, not the final word.
_ACCURACY_TOLERANCE_EXP = 0.15   # EV
_ACCURACY_TOLERANCE_SHA = 5.0    # shadows slider units


def _get_bias_correction(
    con: sqlite3.Connection,
    features: Optional[dict],
    top_k: int = 15,
) -> tuple[float, float, int]:
    """Estimate the systematic bias the model makes for photos like this one.

    Uses inverse-distance-weighted KNN over the correction_log feature space.
    Returns (bias_exposure, bias_shadows, n_corrections_used).

    bias_exposure > 0  → add to the KNN exposure estimate (AI was too low)
    bias_exposure < 0  → subtract from it (AI was too high)

    The correction is confidence-scaled: attenuated heavily when the bank has
    few corrections (to avoid wild swings from early noisy data) and clamped
    to ±1.0 EV / ±40 shadows so one outlier can never derail a suggestion.
    """
    if not features:
        return 0.0, 0.0, 0

    rows = con.execute(
        """SELECT error_exposure, error_shadows,
                  brightness, visual_contrast,
                  luminance_p10, luminance_p25, luminance_p50
           FROM correction_log
           WHERE error_exposure IS NOT NULL AND error_shadows IS NOT NULL"""
    ).fetchall()

    if not rows:
        return 0.0, 0.0, 0

    new_vec = [
        (features.get(d) or 0.0) / max(_BIAS_RANGES.get(d, 1.0), 1e-9)
        for d in _BIAS_DIMS
    ]

    scored: list[tuple[float, float, float]] = []
    for row in rows:
        feat_vals = [
            (row[d] or 0.0) / max(_BIAS_RANGES.get(d, 1.0), 1e-9)
            for d in _BIAS_DIMS
        ]
        dist = sum((a - b) ** 2 for a, b in zip(new_vec, feat_vals)) ** 0.5
        scored.append((dist, row["error_exposure"], row["error_shadows"]))

    scored.sort(key=lambda t: t[0])
    top = scored[:top_k]

    total_w = sum(1.0 / (d + 1e-3) for d, _, _ in top)
    bias_exp = sum((1.0 / (d + 1e-3)) * e for d, e, _ in top) / total_w
    bias_sha = sum((1.0 / (d + 1e-3)) * s for d, _, s in top) / total_w

    # Confidence ramp: slow trust build-up so early noisy data doesn't dominate.
    confidence = _bias_confidence(len(rows))

    return (
        max(-1.0,  min(1.0,  bias_exp * confidence)),
        max(-40.0, min(40.0, bias_sha * confidence)),
        len(rows),
    )


def get_bias_report(con: sqlite3.Connection, recent_window: int = 20, trend_points: int = 30) -> dict:
    """Return a rich summary of learned bias for GUI display.

    Beyond the original two signed means, this adds the metrics needed for
    an honest "how much can I trust this?" read: MAE (typical error size,
    doesn't cancel + and - like a signed mean can), an accuracy rate (% of
    corrections the AI got close enough on), the same 0..1 confidence score
    used to attenuate live suggestions, and breakdowns by preset/camera so
    a systematic problem with one preset or body doesn't hide inside a
    healthy-looking global average.

    Keys:
        total             — total corrections in log
        ai_suggestions    — total AI suggestion records stored
        confidence        — 0..1, same ramp used to attenuate live suggestions
        mean_error_exp/sha  — signed mean error (direction of the bias)
        mae_exp/sha         — mean ABSOLUTE error (typical magnitude, sign-blind)
        accuracy_rate       — fraction of corrections within tolerance (0..1)
        recent_error_exp/sha, recent_mae_exp/sha — same stats over the last
            `recent_window` corrections only (trend indicator)
        improving         — True when recent MAE is smaller than historical MAE
        trend_series      — up to `trend_points` (error_exposure, error_shadows)
            tuples in chronological order, for a sparkline
        by_preset         — list of {label, count, mae_exp, mae_sha, mean_error_exp,
            mean_error_sha} for the presets with the most corrections logged
        by_camera         — same shape, grouped by camera model
    """
    total = con.execute("SELECT COUNT(*) FROM correction_log").fetchone()[0]
    n_suggestions = con.execute("SELECT COUNT(*) FROM ai_suggestions").fetchone()[0]

    empty = {
        "total": 0, "ai_suggestions": n_suggestions, "confidence": 0.0,
        "mean_error_exp": None, "mean_error_sha": None,
        "mae_exp": None, "mae_sha": None, "accuracy_rate": None,
        "recent_error_exp": None, "recent_error_sha": None,
        "recent_mae_exp": None, "recent_mae_sha": None,
        "improving": False, "trend_series": [], "by_preset": [], "by_camera": [],
    }
    if total == 0:
        return empty

    row = con.execute(
        """SELECT AVG(error_exposure), AVG(error_shadows),
                  AVG(ABS(error_exposure)), AVG(ABS(error_shadows)),
                  AVG(CASE WHEN ABS(error_exposure) <= ? AND ABS(error_shadows) <= ? THEN 1.0 ELSE 0.0 END)
           FROM correction_log
           WHERE error_exposure IS NOT NULL AND error_shadows IS NOT NULL""",
        (_ACCURACY_TOLERANCE_EXP, _ACCURACY_TOLERANCE_SHA),
    ).fetchone()
    mean_exp, mean_sha, mae_exp, mae_sha, accuracy_rate = row

    recent = con.execute(
        """SELECT AVG(error_exposure), AVG(error_shadows), AVG(ABS(error_exposure)), AVG(ABS(error_shadows)) FROM
           (SELECT error_exposure, error_shadows FROM correction_log ORDER BY id DESC LIMIT ?)""",
        (recent_window,),
    ).fetchone()
    recent_exp, recent_sha, recent_mae_exp, recent_mae_sha = recent

    improving = (
        total >= recent_window
        and mae_exp is not None
        and recent_mae_exp is not None
        and (recent_mae_exp < mae_exp or (recent_mae_sha or 0.0) < (mae_sha or 0.0))
    )

    trend_rows = con.execute(
        """SELECT error_exposure, error_shadows FROM
           (SELECT id, error_exposure, error_shadows FROM correction_log ORDER BY id DESC LIMIT ?)
           ORDER BY id ASC""",
        (trend_points,),
    ).fetchall()
    trend_series = [(r[0], r[1]) for r in trend_rows]

    def _breakdown(group_col: str, top_n: int = 5) -> list[dict]:
        rows = con.execute(
            f"""SELECT {group_col} AS label, COUNT(*) AS n,
                       AVG(error_exposure), AVG(error_shadows),
                       AVG(ABS(error_exposure)), AVG(ABS(error_shadows))
                FROM correction_log
                WHERE {group_col} IS NOT NULL AND {group_col} != ''
                GROUP BY {group_col}
                ORDER BY n DESC
                LIMIT ?""",
            (top_n,),
        ).fetchall()
        return [
            {
                "label": r[0], "count": r[1],
                "mean_error_exp": r[2], "mean_error_sha": r[3],
                "mae_exp": r[4], "mae_sha": r[5],
            }
            for r in rows
        ]

    return {
        "total": total,
        "ai_suggestions": n_suggestions,
        "confidence": _bias_confidence(total),
        "mean_error_exp": mean_exp, "mean_error_sha": mean_sha,
        "mae_exp": mae_exp, "mae_sha": mae_sha,
        "accuracy_rate": accuracy_rate,
        "recent_error_exp": recent_exp, "recent_error_sha": recent_sha,
        "recent_mae_exp": recent_mae_exp, "recent_mae_sha": recent_mae_sha,
        "improving": improving,
        "trend_series": trend_series,
        "by_preset": _breakdown("preset_name"),
        "by_camera": _breakdown("camera"),
    }


def export_bias_history_csv(con: sqlite3.Connection, out_path: str) -> int:
    """Dump the full correction_log (one row per learned correction) to a
    CSV file for external inspection/audit. Returns the number of rows
    written.

    Kept deliberately simple (stdlib csv, no pandas dependency) since this
    is a rarely-used export path, not a hot loop.
    """
    import csv

    rows = con.execute(
        """SELECT corrected_at, catalog_path, filename, preset_name, camera,
                  error_exposure, error_shadows,
                  brightness, visual_contrast,
                  luminance_p10, luminance_p25, luminance_p50
           FROM correction_log
           ORDER BY id ASC"""
    ).fetchall()

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "corrigido_em", "catalogo", "arquivo", "preset", "camera",
            "erro_exposicao", "erro_sombras",
            "brilho", "contraste_visual",
            "luminancia_p10", "luminancia_p25", "luminancia_p50",
        ])
        for r in rows:
            writer.writerow(list(r))

    return len(rows)


# ---------------------------------------------------------------------------
# Lightroom catalog reading
# ---------------------------------------------------------------------------

# The develop settings are stored as a human-readable Lua-style table in the
# `text` column of Adobe_imageDevelopSettings, e.g.:
#   s = { ... Exposure2012 = 1.18, ... Shadows2012 = 24, ... }
# Real catalogs use *2012 fields (current Process Version). Legacy Exposure /
# Shadows fields (pre-2012 process) are also present but usually 0 once a
# photo has been migrated. We read/write the 2012 fields, matching what
# Lightroom Classic itself edits when you move the Exposure/Shadows sliders.

EXPOSURE_RE = re.compile(r"(Exposure2012\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)")
SHADOWS_RE  = re.compile(r"(Shadows2012\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)")
# White balance numeric fields + the quoted WhiteBalance string.
# Lightroom only honours custom Temperature/Tint on a raw photo when
# WhiteBalance = "Custom", so we also rewrite that whenever we write numbers.
TEMPERATURE_RE   = re.compile(r"(Temperature\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)")
TINT_RE          = re.compile(r"(Tint\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)")
WHITE_BALANCE_RE = re.compile(r'(WhiteBalance\s*=\s*")([^"]*)(")')

# All remaining Develop sliders we want to learn from and store.
# Names match what Lightroom Classic writes into Adobe_imageDevelopSettings.text
# (Lua-style key = value pairs). Using word-boundary \b on the left so
# "Contrast2012" doesn't match a bare "Contrast" pattern.
_DEV_RE: dict[str, re.Pattern] = {
    "process_version": re.compile(r'ProcessVersion\s*=\s*"([^"]*)"'),          # quoted string
    "contrast_dev":    re.compile(r"(Contrast2012\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)"),
    "highlights":      re.compile(r"(Highlights2012\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)"),
    "whites":          re.compile(r"(Whites2012\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)"),
    "blacks":          re.compile(r"(Blacks2012\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)"),
    "clarity":         re.compile(r"(Clarity2012\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)"),
    "texture":         re.compile(r"(Texture\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)"),
    "dehaze":          re.compile(r"(Dehaze\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)"),
    "vibrance":        re.compile(r"(Vibrance\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)"),
    "saturation_dev":  re.compile(r"(Saturation\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)"),
    "sharpness":       re.compile(r"(Sharpness\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)"),
    "noise_lum":       re.compile(r"(LuminanceSmoothing\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)"),
    "noise_color":     re.compile(r"(ColorNoiseReduction\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)"),
    "lens_profile_enable": re.compile(r"(LensProfileEnable\s*=\s*)([0-9]+)"),
    "ca_enable":       re.compile(r"(AutoLateralCA\s*=\s*)([0-9]+)"),
    "vignette":        re.compile(r"(PostCropVignetteAmount\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)"),
    "grain_amount":    re.compile(r"(GrainAmount\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)"),
}


def _extract_dev(text: Optional[str], key: str) -> Optional[float]:
    """Extract a numeric Develop slider value using the _DEV_RE table."""
    if not text:
        return None
    pat = _DEV_RE.get(key)
    if pat is None:
        return None
    m = pat.search(text)
    if not m:
        return None
    # process_version has only one capture group (the whole value string)
    grp = m.group(1) if key == "process_version" else m.group(2)
    try:
        return float(grp)
    except (TypeError, ValueError):
        return None


def _extract_process_version(text: Optional[str]) -> Optional[str]:
    """Return ProcessVersion as a string (e.g. '11.0'), not float."""
    if not text:
        return None
    m = _DEV_RE["process_version"].search(text)
    return m.group(1) if m else None


def _extract_field(text: str, pattern: re.Pattern) -> Optional[float]:
    if not text:
        return None
    m = pattern.search(text)
    if not m:
        return None
    return float(m.group(2))


def open_catalog_copy(catalog_path: Path, workdir: Path) -> Path:
    """Copy a catalog file to a scratch location so we never touch the
    original, and so an open Lightroom instance holding a lock on the
    original doesn't block us from reading it."""
    workdir.mkdir(parents=True, exist_ok=True)
    dest = workdir / f"{catalog_path.stem}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.lrcat"
    shutil.copy2(catalog_path, dest)
    return dest


@dataclass
class CatalogPhoto:
    # ---- identity ----
    image_id_local: int
    file_path: str
    filename: str
    id_global: Optional[str] = None
    file_extension: Optional[str] = None
    file_size_bytes: Optional[int] = None
    # ---- LR metadata ----
    rating: Optional[int] = None
    color_label: Optional[str] = None
    capture_time: Optional[str] = None
    # ---- GPS ----
    gps_latitude: Optional[float] = None
    gps_longitude: Optional[float] = None
    gps_altitude: Optional[float] = None
    # ---- EXIF ----
    camera: Optional[str] = None
    camera_make: Optional[str] = None
    lens: Optional[str] = None
    focal_length: Optional[float] = None
    focal_length_35mm: Optional[float] = None
    aperture: Optional[float] = None
    shutter_speed: Optional[float] = None
    iso: Optional[float] = None
    flash: Optional[int] = None
    metering_mode: Optional[int] = None
    exposure_program: Optional[int] = None
    exposure_bias: Optional[float] = None
    orientation: Optional[int] = None
    # ---- Develop settings ----
    preset_name: Optional[str] = None
    process_version: Optional[str] = None
    exposure: Optional[float] = None
    contrast_dev: Optional[float] = None
    highlights: Optional[float] = None
    shadows: Optional[float] = None
    whites: Optional[float] = None
    blacks: Optional[float] = None
    clarity: Optional[float] = None
    texture: Optional[float] = None
    dehaze: Optional[float] = None
    vibrance: Optional[float] = None
    saturation_dev: Optional[float] = None
    temperature: Optional[float] = None
    tint: Optional[float] = None
    sharpness: Optional[float] = None
    noise_lum: Optional[float] = None
    noise_color: Optional[float] = None
    lens_profile_enable: Optional[int] = None
    ca_enable: Optional[int] = None
    vignette: Optional[float] = None
    grain_amount: Optional[float] = None
    # ---- visual (filled later by extract_visual_features) ----
    features: dict = field(default_factory=dict)


def _available_cols(cur: sqlite3.Cursor, table: str) -> set[str]:
    """Return the set of column names for a table (empty if table missing)."""
    try:
        return {row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.OperationalError:
        return set()


def _available_tables(cur: sqlite3.Cursor) -> set[str]:
    rows = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def read_catalog_photos(catalog_copy_path: Path, logger: logging.Logger) -> list[CatalogPhoto]:
    """Read every photo from a (copied) .lrcat.

    Extracts all available EXIF fields, develop settings, GPS, rating/label,
    and file metadata. Works with any Lightroom Classic version by probing
    the actual table/column layout before building queries."""
    con = sqlite3.connect(f"file:{catalog_copy_path}?mode=ro", uri=True)
    cur = con.cursor()

    tables = _available_tables(cur)
    img_cols = _available_cols(cur, "Adobe_images")
    file_cols = _available_cols(cur, "AgLibraryFile")

    # ---- base file / image info ------------------------------------------------
    extra_img = []
    if "rating" in img_cols:        extra_img.append("i.rating")
    if "colorLabels" in img_cols:   extra_img.append("i.colorLabels")
    if "captureTime" in img_cols:   extra_img.append("i.captureTime")
    extra_img_sql = (", " + ", ".join(extra_img)) if extra_img else ""

    extra_file = []
    if "fileSize" in file_cols:     extra_file.append("f.fileSize")
    extra_file_sql = (", " + ", ".join(extra_file)) if extra_file else ""

    cur.execute(
        f"""
        SELECT i.id_local, f.idx_filename, fo.pathFromRoot, rf.absolutePath,
               i.id_global{extra_img_sql}{extra_file_sql}
        FROM Adobe_images i
        JOIN AgLibraryFile f   ON f.id_local  = i.rootFile
        JOIN AgLibraryFolder fo ON fo.id_local = f.folder
        JOIN AgLibraryRootFolder rf ON rf.id_local = fo.rootFolder
        """
    )
    # map image_id -> tuple of all selected columns
    file_rows: dict[int, tuple] = {}
    for row in cur.fetchall():
        file_rows[row[0]] = row

    # ---- develop settings text -------------------------------------------------
    cur.execute("SELECT image, text FROM Adobe_imageDevelopSettings")
    settings_rows = {row[0]: row[1] for row in cur.fetchall()}

    # ---- EXIF ------------------------------------------------------------------
    # Probe which columns actually exist in AgHarvestedExifMetadata (varies by LR version)
    exif_cols = _available_cols(cur, "AgHarvestedExifMetadata") if "AgHarvestedExifMetadata" in tables else set()

    def _ecol(c: str, alias: str = "") -> str:
        """Return 'e.<c> [AS alias]' if column exists, else 'NULL'."""
        alias = alias or c
        return f"e.{c}" if c in exif_cols else "NULL"

    # Interned lens / camera / camera-make strings
    lens_join = ""
    lc_lens = "NULL"; lc_cam = "NULL"; lc_make = "NULL"
    if "AgInternedExifLens" in tables and "lensRef" in exif_cols:
        lc_lens = "il.value"
        lens_join += " LEFT JOIN AgInternedExifLens il ON il.id_local = e.lensRef"
    if "AgInternedExifCameraModel" in tables and "cameraModelRef" in exif_cols:
        lc_cam = "ic.value"
        lens_join += " LEFT JOIN AgInternedExifCameraModel ic ON ic.id_local = e.cameraModelRef"
    if "AgInternedExifCameraMaker" in tables and "cameraMakerRef" in exif_cols:
        lc_make = "im.value"
        lens_join += " LEFT JOIN AgInternedExifCameraMaker im ON im.id_local = e.cameraMakerRef"

    exif_rows: dict[int, tuple] = {}
    if exif_cols:
        cur.execute(
            f"""
            SELECT e.image,
                   {_ecol('aperture')},
                   {_ecol('focalLength')},
                   {_ecol('focalLength35mm')},
                   {_ecol('isoSpeedRating')},
                   {_ecol('shutterSpeed')},
                   {_ecol('flashFired')},
                   {_ecol('meteringMode')},
                   {_ecol('exposureProgram')},
                   {_ecol('exposureBias')},
                   {_ecol('orientation')},
                   {_ecol('gpsLatitude')},
                   {_ecol('gpsLongitude')},
                   {_ecol('gpsAltitude')},
                   {lc_lens},
                   {lc_cam},
                   {lc_make}
            FROM AgHarvestedExifMetadata e
            {lens_join}
            """
        )
        exif_rows = {row[0]: row[1:] for row in cur.fetchall()}

    # ---- Preset heuristic (history step names) --------------------------------
    preset_by_image: dict[int, str] = {}
    try:
        cur.execute(
            """
            SELECT image, name, dateCreated
            FROM Adobe_libraryImageDevelopHistoryStep
            WHERE name IS NOT NULL
            ORDER BY dateCreated ASC
            """
        )
        for image_id, name, _date in cur.fetchall():
            preset_by_image[image_id] = name
    except sqlite3.OperationalError:
        logger.warning("Tabela de histórico de revelação não encontrada neste catálogo.")

    # ---- Assemble CatalogPhoto objects ----------------------------------------
    # Column offsets in file_rows depend on which optional columns were selected.
    # Fixed: 0=id_local, 1=filename, 2=pathFromRoot, 3=absolutePath, 4=id_global
    _has_rating   = "i.rating"       in extra_img
    _has_label    = "i.colorLabels"  in extra_img
    _has_captime  = "i.captureTime"  in extra_img
    _has_filesize = "f.fileSize"     in extra_file
    _base = 5
    _off_rating   = _base + extra_img.index("i.rating")       if _has_rating   else -1
    _off_label    = _base + extra_img.index("i.colorLabels")  if _has_label    else -1
    _off_captime  = _base + extra_img.index("i.captureTime")  if _has_captime  else -1
    _off_filesize = _base + len(extra_img) + extra_file.index("f.fileSize") if _has_filesize else -1

    photos: list[CatalogPhoto] = []
    for image_id, row in file_rows.items():
        id_local, idx_filename, path_from_root, abs_path, id_global = row[:5]
        rating      = row[_off_rating]   if _off_rating   >= 0 else None
        color_label = row[_off_label]    if _off_label    >= 0 else None
        capture_time= row[_off_captime]  if _off_captime  >= 0 else None
        file_size   = row[_off_filesize] if _off_filesize >= 0 else None

        full_path = os.path.join(abs_path or "", path_from_root or "", idx_filename or "")
        ext = Path(idx_filename).suffix.lstrip(".").upper() if idx_filename else None

        # Develop settings
        text = settings_rows.get(image_id)
        exposure    = _extract_field(text, EXPOSURE_RE)    if text else None
        shadows     = _extract_field(text, SHADOWS_RE)     if text else None
        temperature = _extract_field(text, TEMPERATURE_RE) if text else None
        tint        = _extract_field(text, TINT_RE)        if text else None
        process_ver = _extract_process_version(text)

        # Remaining sliders via _DEV_RE
        dev_keys = [
            "contrast_dev","highlights","whites","blacks","clarity","texture",
            "dehaze","vibrance","saturation_dev","sharpness",
            "noise_lum","noise_color","lens_profile_enable","ca_enable",
            "vignette","grain_amount",
        ]
        dev_vals: dict[str, Optional[float]] = {k: _extract_dev(text, k) for k in dev_keys}

        # Preset
        preset_raw  = preset_by_image.get(image_id)
        preset_name = _clean_preset_name(preset_raw)

        # EXIF tuple: (ap, fl, fl35, iso, shutter, flash, meter, eprog, ebias,
        #              orient, lat, lon, alt, lens_str, cam_str, make_str)
        ex = exif_rows.get(image_id, ())
        def _ex(i: int): return ex[i] if len(ex) > i else None
        aperture       = _ex(0)
        focal_length   = _ex(1)
        focal_length_35= _ex(2)
        iso            = _ex(3)
        shutter_speed  = _ex(4)
        flash          = int(_ex(5)) if _ex(5) is not None else None
        metering_mode  = _ex(6)
        exposure_prog  = _ex(7)
        exposure_bias  = _ex(8)
        orientation    = _ex(9)
        gps_lat        = _ex(10)
        gps_lon        = _ex(11)
        gps_alt        = _ex(12)
        lens           = _ex(13)
        camera         = _ex(14)
        camera_make    = _ex(15)

        photos.append(CatalogPhoto(
            image_id_local=image_id,
            file_path=full_path,
            filename=idx_filename or "",
            id_global=id_global,
            file_extension=ext,
            file_size_bytes=file_size,
            rating=rating,
            color_label=color_label,
            capture_time=capture_time,
            gps_latitude=gps_lat,
            gps_longitude=gps_lon,
            gps_altitude=gps_alt,
            camera=camera,
            camera_make=camera_make,
            lens=lens,
            focal_length=focal_length,
            focal_length_35mm=focal_length_35,
            aperture=aperture,
            shutter_speed=shutter_speed,
            iso=iso,
            flash=flash,
            metering_mode=metering_mode,
            exposure_program=exposure_prog,
            exposure_bias=exposure_bias,
            orientation=orientation,
            preset_name=preset_name,
            process_version=process_ver,
            exposure=exposure,
            shadows=shadows,
            temperature=temperature,
            tint=tint,
            contrast_dev=dev_vals["contrast_dev"],
            highlights=dev_vals["highlights"],
            whites=dev_vals["whites"],
            blacks=dev_vals["blacks"],
            clarity=dev_vals["clarity"],
            texture=dev_vals["texture"],
            dehaze=dev_vals["dehaze"],
            vibrance=dev_vals["vibrance"],
            saturation_dev=dev_vals["saturation_dev"],
            sharpness=dev_vals["sharpness"],
            noise_lum=dev_vals["noise_lum"],
            noise_color=dev_vals["noise_color"],
            lens_profile_enable=int(dev_vals["lens_profile_enable"]) if dev_vals["lens_profile_enable"] is not None else None,
            ca_enable=int(dev_vals["ca_enable"]) if dev_vals["ca_enable"] is not None else None,
            vignette=dev_vals["vignette"],
            grain_amount=dev_vals["grain_amount"],
        ))

    con.close()
    return photos


# ---------------------------------------------------------------------------
# Lightroom's own preview cache (Previews.lrdata)
# ---------------------------------------------------------------------------
#
# Lightroom keeps a rendered JPEG preview of every photo on the local disk
# (next to the .lrcat), independent of where the original RAW/JPEG file
# lives. This is essential because:
#   - RAW formats (.CR3, .CR2, .NEF, .ARW, .DNG, ...) can't be opened by
#     Pillow directly.
#   - The original files often live on external/network drives that may not
#     be connected when we run (as in this project: paths like "M:/...").
# The preview cache is always local and always a plain JPEG, so we read
# straight from it instead of touching the original photo file at all.

def find_previews_dir(catalog_path: Path) -> Optional[Path]:
    """Locate the '<catalog name> Previews.lrdata' folder Lightroom keeps
    next to the .lrcat file."""
    candidate = catalog_path.parent / f"{catalog_path.stem} Previews.lrdata"
    if candidate.is_dir():
        return candidate
    # Fallback: any "*Previews.lrdata" folder in the same directory.
    matches = list(catalog_path.parent.glob("*Previews.lrdata"))
    return matches[0] if matches else None


def _extract_jpeg_bytes(raw: bytes) -> Optional[bytes]:
    """.lrprev files are a small proprietary header followed by a plain
    JPEG. Extract the JPEG by locating its SOI/EOI markers."""
    start = raw.find(b"\xff\xd8\xff")
    if start == -1:
        return None
    end = raw.rfind(b"\xff\xd9")
    if end == -1 or end < start:
        return None
    return raw[start:end + 2]


_preview_index_cache: dict[Path, dict[str, list[Path]]] = {}
_preview_root_db_cache: dict[Path, dict[int, str]] = {}


def _read_preview_root_db(previews_dir: Path, logger: logging.Logger) -> dict[int, str]:
    """Previews.lrdata ships its own tiny SQLite database (root.db) that maps
    each photo's catalog id_local to the UUID actually used in the .lrprev
    filenames. Those preview UUIDs are NOT the same as Adobe_images.id_global
    - they are assigned independently when the preview cache is built - so
    root.db is the only reliable link between a catalog photo and its
    cached preview file."""
    if previews_dir in _preview_root_db_cache:
        return _preview_root_db_cache[previews_dir]

    mapping: dict[int, str] = {}
    root_db = previews_dir / "root.db"
    if not root_db.is_file():
        root_db = previews_dir / "previews.db"
    if not root_db.is_file():
        _preview_root_db_cache[previews_dir] = mapping
        return mapping

    tmp_copy: Optional[Path] = None
    try:
        tmp_copy = previews_dir.parent / f".__root_db_copy_{os.getpid()}.db"
        shutil.copy2(root_db, tmp_copy)
        con = sqlite3.connect(str(tmp_copy))
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            candidate_tables = [t for t in tables if "imagecache" in t.lower() or t.lower() == "imagecacheentry"]
            if not candidate_tables:
                candidate_tables = tables
            for table in candidate_tables:
                try:
                    cols = [r[1].lower() for r in con.execute(f"PRAGMA table_info('{table}')").fetchall()]
                except sqlite3.Error:
                    continue
                id_col = next((c for c in cols if c in ("imageid", "id_local", "id")), None)
                uuid_col = next((c for c in cols if "uuid" in c or c == "digest"), None)
                if not id_col or not uuid_col:
                    continue
                try:
                    rows = con.execute(f'SELECT "{id_col}", "{uuid_col}" FROM "{table}"').fetchall()
                except sqlite3.Error:
                    continue
                found = 0
                for image_id, uuid_val in rows:
                    if image_id is None or not uuid_val:
                        continue
                    try:
                        mapping[int(image_id)] = str(uuid_val).lower()
                        found += 1
                    except (TypeError, ValueError):
                        continue
                if found:
                    logger.info(
                        f"root.db: mapeados {found} id_local -> uuid de preview via tabela '{table}' "
                        f"(colunas {id_col}/{uuid_col})."
                    )
                    break
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Não foi possível ler root.db em {previews_dir}: {exc}")
    finally:
        if tmp_copy is not None:
            try:
                os.remove(tmp_copy)
            except OSError:
                pass

    _preview_root_db_cache[previews_dir] = mapping
    return mapping


def _build_preview_index(previews_dir: Path, logger: logging.Logger) -> dict[str, list[Path]]:
    """Scan the whole Previews.lrdata folder once and index every .lrprev
    file by the UUID at the start of its filename (case-insensitive,
    dashes ignored - Lightroom versions differ slightly on this). Reused
    across every photo in a catalog instead of re-globbing per photo."""
    if previews_dir in _preview_index_cache:
        return _preview_index_cache[previews_dir]

    index: dict[str, list[Path]] = {}
    total_files = 0
    try:
        for f in previews_dir.rglob("*.lrprev"):
            total_files += 1
            stem = f.stem  # e.g. "1A2B3C4D-...-abcdef" or "...-1-large"
            key = stem.split("-")[0:5]  # first 5 dash-separated groups = uuid
            uuid_guess = "-".join(key).lower()
            index.setdefault(uuid_guess, []).append(f)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Erro ao escanear pasta de previews {previews_dir}: {exc}")

    logger.info(f"Índice de previews do Lightroom: {total_files} arquivo(s) .lrprev encontrados em {previews_dir}")
    sample_keys = list(index.keys())[:3]
    if sample_keys:
        logger.info(f"Exemplo de chaves extraídas dos nomes de preview: {sample_keys}")
    _preview_index_cache[previews_dir] = index
    return index


def _diagnose_preview_mismatch(previews_dir: Optional[Path], photos: list, logger: logging.Logger) -> None:
    """When we found a Previews.lrdata folder with files in it but couldn't
    match a single photo to a preview, print an explicit side-by-side
    comparison so the mismatch pattern is visible directly in the log
    instead of requiring another guess."""
    if not previews_dir:
        return
    index = _build_preview_index(previews_dir, logger)
    if not index:
        return
    root_map = _read_preview_root_db(previews_dir, logger)
    sample_ids = [p.image_id_local for p in photos[:3]]
    sample_global = [p.id_global for p in photos[:3] if p.id_global]
    sample_keys = list(index.keys())[:3]
    logger.error(
        "Nenhuma foto foi casada com um preview do Lightroom, mesmo havendo "
        f"{sum(len(v) for v in index.values())} arquivo(s) .lrprev na pasta. "
        f"id_local do catálogo: {sample_ids} | id_global: {sample_global} | "
        f"entradas no root.db: {len(root_map)} | "
        f"chave(s) extraídas dos nomes de arquivo de preview: {sample_keys}. "
        "Envie esse log para eu ajustar o casamento de nomes."
    )


# extract_visual_features() always downsizes to this many pixels on the
# longest edge before computing histograms/brightness/contrast, so there is
# zero benefit to decoding a preview larger than this (plus a little
# headroom so the resize itself has real detail to average, not upscaled
# blur). Picking the SMALLEST cached preview pyramid level that already
# clears this bar avoids decoding/resizing preview files that are often
# 2000px+ only to immediately throw most of that resolution away — same
# accuracy, less CPU time per photo.
_MIN_USABLE_PREVIEW_DIM = 320


def load_preview_image_bytes(
    previews_dir: Optional[Path],
    id_local: Optional[int],
    id_global: Optional[str],
    logger: logging.Logger,
) -> Optional[bytes]:
    """Find the SMALLEST cached Lightroom preview that is still big enough
    for feature extraction, and return its raw JPEG bytes, without touching
    the original photo file.

    Lightroom caches each photo at several pyramid sizes (thumbnail up to
    near-full-res) inside Previews.lrdata. Feature extraction only ever
    needs ~320px on the long edge (see _MIN_USABLE_PREVIEW_DIM), so grabbing
    the largest cached size every time — the previous behavior — decoded
    and resized far more pixels than necessary for every single photo. We
    now check candidates from smallest to largest and use the first one
    that already meets the size floor, falling back to the largest
    available if none do (better a soft, oversized image than no features
    at all).

    The authoritative link is Previews.lrdata/root.db, which maps the
    catalog's Adobe_images.id_local to the UUID actually used in the
    .lrprev filenames (that UUID is unrelated to Adobe_images.id_global -
    matching directly against id_global silently fails on real catalogs).
    If root.db can't be read for any reason, we fall back to the old
    id_global-based filename guess so previews still work on older/odd
    Lightroom versions.
    """
    if not previews_dir:
        return None

    index = _build_preview_index(previews_dir, logger)
    matches = None

    if id_local is not None:
        root_map = _read_preview_root_db(previews_dir, logger)
        preview_uuid = root_map.get(id_local)
        if preview_uuid:
            matches = index.get(preview_uuid.lower())
            if not matches:
                uuid_compact = preview_uuid.lower().replace("-", "")
                for k, files in index.items():
                    if k.replace("-", "") == uuid_compact:
                        matches = files
                        break

    if not matches and id_global:
        key = id_global.lower()
        matches = index.get(key)
        if not matches:
            uuid_compact = key.replace("-", "")
            for k, files in index.items():
                if k.replace("-", "") == uuid_compact:
                    matches = files
                    break

    if not matches:
        return None

    # File size is a reasonable, cheap proxy for pyramid resolution level
    # (no need to decode anything to rank candidates). Walk smallest-first
    # so we can stop as soon as one is big enough - avoids decoding the
    # largest cached size (often 2000px+) when analysis only needs ~320px.
    matches = sorted(matches, key=lambda p: p.stat().st_size)
    largest_usable: Optional[bytes] = None
    for match in matches:
        try:
            raw = match.read_bytes()
        except Exception:
            continue
        jpeg_bytes = _extract_jpeg_bytes(raw)
        if not jpeg_bytes:
            continue
        # Remember the biggest one we've successfully decoded so far, in
        # case none clear the size floor and we need a fallback.
        largest_usable = jpeg_bytes
        if Image is not None:
            try:
                with Image.open(io.BytesIO(jpeg_bytes)) as probe:
                    # .size only reads the header - JPEG decoding in
                    # Pillow is lazy until .load()/.getdata(), so this
                    # does not pay the full decode cost.
                    w, h = probe.size
                if max(w, h) >= _MIN_USABLE_PREVIEW_DIM:
                    return jpeg_bytes
                continue
            except Exception:
                # Header unreadable - fall through and just use it as-is
                # rather than risk skipping every candidate.
                return jpeg_bytes
        else:
            # Pillow unavailable - can't probe dimensions, so we have no
            # way to pick a "good enough" size; just use the first decode.
            return jpeg_bytes
    return largest_usable


def _clean_preset_name(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    # "Importar/MASTERBLASTER2025-2 (10/07/2026 15:17:39)" -> "MASTERBLASTER2025-2"
    # Strip the trailing "(date time)" FIRST - the date itself contains "/"
    # characters, so splitting on "/" before stripping it would corrupt the
    # preset name.
    name = re.sub(r"\s*\([^)]*\)\s*$", "", raw).strip()
    name = name.split("/")[-1].strip()
    return name or None


# ---------------------------------------------------------------------------
# Visual + EXIF feature extraction
# ---------------------------------------------------------------------------

def extract_visual_features(
    image_path: str,
    logger: logging.Logger,
    max_dim: int = 256,
    preview_bytes: Optional[bytes] = None,
) -> Optional[dict]:
    """Compute cheap-but-useful visual features: grayscale histogram (32
    bins), mean brightness, contrast (std dev), and luminance percentiles.

    Preference order:
    1. `preview_bytes` - the JPEG already rendered by Lightroom itself and
       cached in Previews.lrdata. This is the "smart" path: it works for
       RAW files (.CR3/.CR2/.NEF/...) that Pillow can't open directly, and
       it works even when the original photo lives on a drive that isn't
       connected right now.
    2. The original file at `image_path`, if it exists and Pillow can open
       it (plain JPEG/TIFF/PNG catalogs).
    Returns None if neither source is usable - callers fall back to a
    preset/EXIF-based average in that case."""
    if Image is None or np is None:
        return None

    img = None
    if preview_bytes:
        try:
            img = Image.open(io.BytesIO(preview_bytes))
            img.load()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Preview do Lightroom encontrado mas inválido para {image_path}: {exc}")
            img = None

    if img is None:
        if not image_path or not os.path.isfile(image_path):
            return None
        try:
            img = Image.open(image_path)
            img.load()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Não foi possível abrir imagem para extrair features: {image_path} ({exc})")
            return None

    try:
        with img:
            rgb_img = img.convert("RGB")
            w, h = rgb_img.size
            scale = max_dim / max(w, h) if max(w, h) > max_dim else 1.0
            if scale < 1.0:
                rgb_img = rgb_img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
            rgb_arr = np.asarray(rgb_img, dtype=np.float64)
            gray_arr = np.asarray(rgb_img.convert("L"), dtype=np.float64)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Não foi possível processar imagem: {image_path} ({exc})")
        return None

    # ---- luminance stats -------------------------------------------------------
    hist_lum, _ = np.histogram(gray_arr, bins=32, range=(0, 255))
    hist_lum_norm = (hist_lum / hist_lum.sum()).tolist() if hist_lum.sum() > 0 else hist_lum.tolist()
    percentiles = np.percentile(gray_arr, [10, 25, 50, 75, 90]).tolist()

    # ---- per-channel RGB means & gray-world WB ratios -------------------------
    mean_r = float(rgb_arr[..., 0].mean())
    mean_g = float(rgb_arr[..., 1].mean())
    mean_b = float(rgb_arr[..., 2].mean())
    rg_ratio = mean_r / mean_g if mean_g > 1e-6 else 1.0
    bg_ratio = mean_b / mean_g if mean_g > 1e-6 else 1.0

    # ---- per-channel RGB histograms (32 bins each) ----------------------------
    hist_r, _ = np.histogram(rgb_arr[..., 0], bins=32, range=(0, 255))
    hist_g, _ = np.histogram(rgb_arr[..., 1], bins=32, range=(0, 255))
    hist_b, _ = np.histogram(rgb_arr[..., 2], bins=32, range=(0, 255))
    def _norm_h(h):
        s = h.sum()
        return (h / s).tolist() if s > 0 else h.tolist()
    hist_rgb = {"r": _norm_h(hist_r), "g": _norm_h(hist_g), "b": _norm_h(hist_b)}

    # ---- HSV saturation & dominant hue ----------------------------------------
    # Convert to HSV to get perceptual saturation and hue information.
    # We use numpy directly to avoid requiring colorsys on the full array.
    r_n = rgb_arr[..., 0] / 255.0
    g_n = rgb_arr[..., 1] / 255.0
    b_n = rgb_arr[..., 2] / 255.0
    v   = np.maximum(np.maximum(r_n, g_n), b_n)
    c_min = np.minimum(np.minimum(r_n, g_n), b_n)
    chroma = v - c_min
    saturation_hsv = np.where(v > 1e-6, chroma / v, 0.0)
    saturation_mean = float(saturation_hsv.mean())

    # Dominant hue: weighted circular mean of pixel hues, weighted by chroma
    # (low-chroma pixels have ill-defined hue and shouldn't dominate).
    eps = 1e-9
    with np.errstate(invalid="ignore", divide="ignore"):
        hue_r = np.where(chroma > eps, np.where(v == r_n,
                    ((g_n - b_n) / chroma) % 6, np.where(v == g_n,
                    (b_n - r_n) / chroma + 2,
                    (r_n - g_n) / chroma + 4)), 0.0)
    hue_deg = hue_r * 60.0  # 0-360
    rad = np.deg2rad(hue_deg)
    w = chroma  # use chroma as weight so gray pixels don't contribute
    sin_w = float(np.sum(np.sin(rad) * w))
    cos_w = float(np.sum(np.cos(rad) * w))
    dominant_hue = float(np.degrees(np.arctan2(sin_w, cos_w)) % 360)

    # ---- thumbnail (WebP, ~128 px, stored as bytes) ---------------------------
    thumb_bytes: Optional[bytes] = None
    try:
        thumb_img = rgb_img.copy()
        tw, th = thumb_img.size
        tscale = 128 / max(tw, th) if max(tw, th) > 128 else 1.0
        if tscale < 1.0:
            thumb_img = thumb_img.resize(
                (max(1, int(tw * tscale)), max(1, int(th * tscale))),
                resample=Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS,
            )
        buf = io.BytesIO()
        thumb_img.save(buf, format="WEBP", quality=60, method=0)
        thumb_bytes = buf.getvalue()
    except Exception:  # noqa: BLE001 — thumbnail is a nice-to-have
        thumb_bytes = None

    return {
        "brightness":      float(gray_arr.mean()),
        "visual_contrast": float(gray_arr.std()),
        "luminance_p10":   percentiles[0],
        "luminance_p25":   percentiles[1],
        "luminance_p50":   percentiles[2],
        "luminance_p75":   percentiles[3],
        "luminance_p90":   percentiles[4],
        "rg_ratio":        rg_ratio,
        "bg_ratio":        bg_ratio,
        "mean_r":          mean_r,
        "mean_g":          mean_g,
        "mean_b":          mean_b,
        "saturation_mean": saturation_mean,
        "dominant_hue":    dominant_hue,
        "histogram":       hist_lum_norm,      # kept as "histogram" internally
        "histogram_rgb":   hist_rgb,
        "thumbnail_webp":  thumb_bytes,
    }


# ---------------------------------------------------------------------------
# Feeding the bank
# ---------------------------------------------------------------------------

def feed_database_from_catalog(
    con: sqlite3.Connection,
    catalog_path: Path,
    workdir: Path,
    logger: logging.Logger,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    force_update: bool = False,
    outlier_check: bool = False,
) -> int:
    """Import (or re-import) a Lightroom catalog into the learning bank.

    Parameters
    ----------
    force_update : bool
        When False (default), raises ``ValueError("CATALOG_ALREADY_EXISTS:N")``
        if ``catalog_path`` was previously imported (N = existing photo count).
        When True, deletes the existing rows for this catalog first and
        re-imports — use this after you have finished editing the catalog in
        Lightroom so the bank learns from your actual corrections.
    outlier_check : bool
        v12, OFF by default so default behavior stays byte-for-byte
        identical to v11. When True, each photo's develop parameters are
        screened (median/MAD, see safety.py) before being learned from:
        "invalid" photos are excluded from the bank (quarantined only, not
        inserted); "suspicious" ones are inserted into ``outlier_quarantine``
        instead of ``photos`` for manual review; everything else follows the
        exact v11 insert path below, unchanged. Any failure in the outlier
        subsystem itself is swallowed and treated as "accepted" so this flag
        can never block a normal feed.
    """
    existing_count = con.execute(
        "SELECT COUNT(*) FROM photos WHERE source_catalog = ?",
        (str(catalog_path),),
    ).fetchone()[0]

    if existing_count > 0 and not force_update:
        raise ValueError(f"CATALOG_ALREADY_EXISTS:{existing_count}")

    # Whether to learn from this import (retroalimentação) must NOT depend
    # on `existing_count` — the program always writes the edited catalog to
    # a brand-new, timestamped filename ("..._editado_<carimbo>.lrcat"), so
    # the very FIRST time the user feeds that file back in, `existing_count`
    # is necessarily 0 (this exact path was never in `photos` before) even
    # though an ai_suggestions row IS waiting for it. The old code only ever
    # checked corrections inside the `existing_count > 0` branch, so that
    # first — and most common — feedback import never triggered
    # _compute_and_save_corrections at all. Instead, check independently
    # whether ANY suggestion (by original or edited path) is waiting.
    has_suggestion = con.execute(
        "SELECT 1 FROM ai_suggestions WHERE catalog_path = ? OR edited_catalog_path = ? LIMIT 1",
        (str(catalog_path), str(catalog_path)),
    ).fetchone() is not None

    if has_suggestion:
        corr_copy = open_catalog_copy(catalog_path, workdir)
        try:
            corr_photos = read_catalog_photos(corr_copy, logger)
        finally:
            try:
                os.remove(corr_copy)
            except OSError:
                pass
        _compute_and_save_corrections(con, str(catalog_path), corr_photos, logger)

    if existing_count > 0:
        con.execute("DELETE FROM photos WHERE source_catalog = ?", (str(catalog_path),))
        con.commit()
        logger.info(
            f"Re-importação: {existing_count} registro(s) antigo(s) de '{catalog_path.name}' "
            f"removidos — banco será atualizado com edições mais recentes."
        )

    thumbs_dir = _get_thumbs_dir(con)   # None for in-memory DBs; thumbnails skip gracefully
    logger.info(f"Abrindo catálogo (cópia de leitura): {catalog_path}")
    copy_path = open_catalog_copy(catalog_path, workdir)
    previews_dir = find_previews_dir(catalog_path)
    if previews_dir:
        logger.info(f"Cache de previews do Lightroom encontrado: {previews_dir}")
    else:
        logger.warning("Cache de previews do Lightroom (Previews.lrdata) não encontrado ao lado do catálogo.")
    try:
        photos = read_catalog_photos(copy_path, logger)
        logger.info(f"{len(photos)} fotos encontradas no catálogo.")

        inserted = 0
        used_preview = 0
        used_original = 0
        no_features = 0
        quarantined = 0
        excluded_invalid = 0
        for i, photo in enumerate(photos):
            # v12 (opt-in, default off — see outlier_check docstring above):
            # screen this photo's develop parameters against the bank's
            # existing history BEFORE it is learned from. Wrapped so any
            # failure in the outlier subsystem falls back to "accepted" and
            # the row proceeds through the exact v11 path below.
            if outlier_check and safety is not None:
                try:
                    verdicts = safety.evaluate_photo_for_outliers(con, photo)
                    outlier_status = safety.overall_outlier_status(verdicts)
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[v12:outlier] verificação falhou para '{photo.filename}', foto aceita como no v11: {exc!r}")
                    outlier_status = "accepted"
                    verdicts = []
                if outlier_status in ("invalid", "suspicious"):
                    try:
                        safety.quarantine_photo(con, str(catalog_path), photo, verdicts, outlier_status)
                    except Exception as exc:  # noqa: BLE001
                        logger.error(f"[v12:outlier] falha ao registrar quarentena para '{photo.filename}': {exc!r}")
                    if outlier_status == "invalid":
                        excluded_invalid += 1
                    else:
                        quarantined += 1
                    # Both invalid and suspicious photos are quarantined
                    # instead of learned from — they are not inserted into
                    # `photos` and can be reviewed/restored from the
                    # Outliers screen later (safety.restore_quarantine_item).
                    if progress_cb:
                        progress_cb(i + 1, len(photos))
                    continue

            preview_bytes = load_preview_image_bytes(previews_dir, photo.image_id_local, photo.id_global, logger)
            # v12: safe read-through cache around the expensive visual-feature
            # extraction below. The signature includes everything that could
            # change the result (file identity + a hash of the preview bytes,
            # since a different preview yields different pixels); any cache
            # error/miss falls straight through to the exact v11 computation.
            features = None
            _cache_path_ok = False
            if safety is not None:
                try:
                    _preview_hash = hashlib.sha1(preview_bytes).hexdigest() if preview_bytes else "no_preview"
                    _stat = os.stat(photo.file_path) if os.path.exists(photo.file_path) else None
                    _sig = safety.make_cache_signature(
                        "extract_visual_features", photo.file_path,
                        _stat.st_size if _stat else None, _stat.st_mtime if _stat else None,
                        _preview_hash,
                    )

                    def _compute():
                        return extract_visual_features(photo.file_path, logger, preview_bytes=preview_bytes)

                    features = safety.cached_feature_extraction(con, (_sig,), _compute)
                    _cache_path_ok = True  # ran to completion (features may legitimately be None)
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[v12:cache] falha no cache de features, calculando normalmente: {exc!r}")
            if not _cache_path_ok:
                features = extract_visual_features(photo.file_path, logger, preview_bytes=preview_bytes)
            has_features = 1 if features else 0
            if features and preview_bytes:
                used_preview += 1
            elif features:
                used_original += 1
            else:
                no_features += 1

            def _f(key):
                return features[key] if features and key in features else None

            cur = con.execute(
                """
                INSERT INTO photos (
                    source_catalog, image_id_local, id_global, file_path, filename,
                    file_extension, file_size_bytes,
                    rating, color_label, capture_time,
                    gps_latitude, gps_longitude, gps_altitude,
                    camera, camera_make, lens,
                    focal_length, focal_length_35mm, aperture, shutter_speed, iso,
                    flash, metering_mode, exposure_program, exposure_bias, orientation,
                    preset_name, process_version,
                    exposure, contrast_dev, highlights, shadows, whites, blacks,
                    clarity, texture, dehaze, vibrance, saturation_dev,
                    temperature, tint, sharpness, noise_lum, noise_color,
                    lens_profile_enable, ca_enable, vignette, grain_amount,
                    brightness, visual_contrast,
                    luminance_p10, luminance_p25, luminance_p50, luminance_p75, luminance_p90,
                    rg_ratio, bg_ratio, mean_r, mean_g, mean_b,
                    saturation_mean, dominant_hue,
                    histogram_json, histogram_rgb_json,
                    has_image_features
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                    ?,?,?,?,?,?,?,?,?
                )
                """,
                (
                    str(catalog_path),
                    photo.image_id_local,
                    photo.id_global,
                    photo.file_path,
                    photo.filename,
                    photo.file_extension,
                    photo.file_size_bytes,
                    photo.rating,
                    photo.color_label,
                    photo.capture_time,
                    photo.gps_latitude,
                    photo.gps_longitude,
                    photo.gps_altitude,
                    photo.camera,
                    photo.camera_make,
                    photo.lens,
                    photo.focal_length,
                    photo.focal_length_35mm,
                    photo.aperture,
                    photo.shutter_speed,
                    photo.iso,
                    photo.flash,
                    photo.metering_mode,
                    photo.exposure_program,
                    photo.exposure_bias,
                    photo.orientation,
                    photo.preset_name,
                    photo.process_version,
                    photo.exposure,
                    photo.contrast_dev,
                    photo.highlights,
                    photo.shadows,
                    photo.whites,
                    photo.blacks,
                    photo.clarity,
                    photo.texture,
                    photo.dehaze,
                    photo.vibrance,
                    photo.saturation_dev,
                    photo.temperature,
                    photo.tint,
                    photo.sharpness,
                    photo.noise_lum,
                    photo.noise_color,
                    photo.lens_profile_enable,
                    photo.ca_enable,
                    photo.vignette,
                    photo.grain_amount,
                    _f("brightness"),
                    _f("visual_contrast"),
                    _f("luminance_p10"),
                    _f("luminance_p25"),
                    _f("luminance_p50"),
                    _f("luminance_p75"),
                    _f("luminance_p90"),
                    _f("rg_ratio"),
                    _f("bg_ratio"),
                    _f("mean_r"),
                    _f("mean_g"),
                    _f("mean_b"),
                    _f("saturation_mean"),
                    _f("dominant_hue"),
                    json.dumps(_f("histogram")),
                    json.dumps(_f("histogram_rgb")),
                    has_features,
                ),
            )
            # Save thumbnail to disk (avoids storing large BLOBs in the DB).
            photo_id = cur.lastrowid
            thumb_bytes = _f("thumbnail_webp")
            if thumb_bytes and thumbs_dir:
                thumb_path = _save_thumbnail_to_disk(thumbs_dir, photo_id, thumb_bytes)
                if thumb_path:
                    con.execute(
                        "UPDATE photos SET thumbnail_path = ? WHERE id = ?",
                        (thumb_path, photo_id),
                    )
            inserted += 1
            if progress_cb:
                progress_cb(i + 1, len(photos))

        con.execute(
            "INSERT INTO runs (kind, catalog_path, photos_count, notes) VALUES (?,?,?,?)",
            ("feed", str(catalog_path), inserted, ""),
        )
        con.commit()
        logger.info(f"{inserted} fotos gravadas no banco de dados.")
        logger.info(
            f"Features visuais: {used_preview} via preview do Lightroom, "
            f"{used_original} via arquivo original, {no_features} sem features (fallback por preset)."
        )
        if outlier_check:
            logger.info(
                f"[v12:outlier] {quarantined} foto(s) suspeita(s) e {excluded_invalid} inválida(s) "
                f"enviadas para quarentena (não aprendidas nesta importação)."
            )
        if used_preview == 0 and previews_dir:
            _diagnose_preview_mismatch(previews_dir, photos, logger)
        return inserted
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass


_QUARANTINE_RESTORE_FIELDS = (
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
)


def insert_photo_snapshot(con: sqlite3.Connection, source_catalog: str, snapshot: dict) -> int:
    """v12: insert a single photo row from a plain-dict snapshot (as stored
    by safety.quarantine_photo) directly into `photos`, bypassing feature
    re-extraction. Used exclusively by the Outliers screen's explicit
    "restore to bank" action — never called during normal feed/edit."""
    cols = ["source_catalog"] + list(_QUARANTINE_RESTORE_FIELDS)
    placeholders = ",".join("?" for _ in cols)
    values = [source_catalog] + [snapshot.get(f) for f in _QUARANTINE_RESTORE_FIELDS]
    cur = con.execute(
        f"INSERT INTO photos ({', '.join(cols)}) VALUES ({placeholders})", values
    )
    con.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Suggesting + applying Exposure/Shadows on a new catalog
# ---------------------------------------------------------------------------

@dataclass
class Suggestion:
    exposure: float
    shadows: float
    matched_preset: Optional[str]
    reference_count: int
    correction_note: str
    temperature: Optional[float] = None
    tint: Optional[float] = None


def _bank_rows(con: sqlite3.Connection, require_features: bool = False) -> list[sqlite3.Row]:
    # Set row_factory on the CURSOR, not the connection. `con` is the single
    # long-lived bank connection shared by the whole app for the entire
    # session — every other function (get_bias_report, _compute_and_save_
    # corrections, etc.) assumes con.row_factory stays sqlite3.Row so it can
    # do dict-style access like row["filename"]. The previous code set
    # con.row_factory = sqlite3.Row then reset it to None afterwards,
    # permanently downgrading the CONNECTION default to None for the rest of
    # the process the first time a suggestion was requested (i.e. as soon as
    # the user used the "Editar" tab once, which is required before there is
    # anything to reimport). Every later dict-style row access anywhere else
    # in the app — most importantly _compute_and_save_corrections(), the
    # heart of the "reaprender" (relearn) feedback loop — then raised
    # `TypeError: tuple indices must be integers or slices, not str`, which
    # the GUI's worker thread caught and logged as an easy-to-miss error
    # line instead of a learned correction, making it look like reimporting
    # an edited catalog "did nothing". Cursor-level row_factory is scoped to
    # just this query and never touches the connection's setting.
    cur = con.cursor()
    cur.row_factory = sqlite3.Row
    query = "SELECT * FROM photos WHERE exposure IS NOT NULL AND shadows IS NOT NULL"
    if require_features:
        query += " AND has_image_features = 1"
    cur.execute(query)
    return cur.fetchall()


def _bank_rows_with_wb(con: sqlite3.Connection, require_features: bool = False) -> list[sqlite3.Row]:
    """Same as _bank_rows, but additionally requires learned White Balance
    (temperature/tint) values, for the WB half of the suggestion."""
    cur = con.cursor()
    cur.row_factory = sqlite3.Row
    query = (
        "SELECT * FROM photos WHERE exposure IS NOT NULL AND shadows IS NOT NULL "
        "AND temperature IS NOT NULL AND tint IS NOT NULL"
    )
    if require_features:
        query += " AND has_image_features = 1"
    cur.execute(query)
    return cur.fetchall()


def suggest_exposure_shadows(
    con: sqlite3.Connection,
    new_photo_features: Optional[dict],
    new_preset_name: Optional[str],
    logger: logging.Logger,
    top_k: int = 5,
    new_lens: Optional[str] = None,
    new_camera: Optional[str] = None,
) -> Optional[Suggestion]:
    """Find the most visually similar previously-edited photos in the bank
    and suggest Exposure/Shadows for a new photo.

    White Balance is intentionally NOT suggested — left to the photographer.

    Algorithm improvements:
    - Adaptive top_k with sqrt scaling.
    - Same-lens / same-camera proximity bonus.
    - Per-dimension weights (contrast + percentile extremes matter most).
    - Highlight-aware exposure nudge: protects already-clipping highlights.
    - Deep-shadow boost: aggressive recovery when p10 is near-black.
    - Contrast-trust scaling: corrections are attenuated when neighbor
      contrast differs significantly from the new photo."""
    bank_all = _bank_rows(con, require_features=False)
    if not bank_all:
        logger.warning("Banco de dados vazio - impossível sugerir valores.")
        return None

    bank = _bank_rows(con, require_features=True)

    if not new_photo_features or not bank:
        logger.warning("Sem features visuais — usando média simples por preset.")
        candidates = [r for r in bank_all if r["preset_name"] == new_preset_name] or bank_all
        exp = sum(r["exposure"] for r in candidates) / len(candidates)
        sha = sum(r["shadows"] for r in candidates) / len(candidates)
        return Suggestion(
            exposure=round(exp, 2), shadows=round(sha, 2),
            matched_preset=new_preset_name, reference_count=len(candidates),
            correction_note="sem features visuais - média simples por preset",
            temperature=None, tint=None,
        )

    dims = [
        "brightness", "visual_contrast",
        "luminance_p10", "luminance_p25", "luminance_p50", "luminance_p75", "luminance_p90",
        "rg_ratio", "bg_ratio", "saturation_mean", "dominant_hue",
    ]
    values_by_dim = {d: [r[d] for r in bank if r[d] is not None] for d in dims}
    ranges = {d: (min(v), max(v)) if v else (0.0, 1.0) for d, v in values_by_dim.items()}

    def norm(value: float, d: str) -> float:
        lo, hi = ranges[d]
        return 0.0 if hi - lo < 1e-9 else (value - lo) / (hi - lo)

    new_vec = [
        norm(new_photo_features[d], d)
        if d in new_photo_features and new_photo_features[d] is not None else 0.0
        for d in dims
    ]

    # Adaptive neighbour count (sqrt scaling)
    effective_top_k = max(top_k, min(len(bank), int(round(len(bank) ** 0.5)) + top_k))

    # Per-dimension weights: contrast + luminance extremes predict editing needs best
    dim_weights = {
        "brightness": 1.2, "visual_contrast": 1.5,
        "luminance_p10": 1.4, "luminance_p25": 1.3, "luminance_p50": 1.2,
        "luminance_p75": 1.1, "luminance_p90": 1.4,
        "rg_ratio": 0.6, "bg_ratio": 0.6, "saturation_mean": 0.7, "dominant_hue": 0.5,
    }
    w_vec = [dim_weights.get(d, 1.0) for d in dims]

    scored = []
    for r in bank:
        if any(r[d] is None for d in dims):
            continue
        vec = [norm(r[d], d) for d in dims]
        dist = sum(ww * (a - b) ** 2 for ww, a, b in zip(w_vec, new_vec, vec)) ** 0.5
        if new_preset_name and r["preset_name"] and r["preset_name"] != new_preset_name:
            dist += 0.30
        r_lens   = r["lens"]   if "lens"   in r.keys() else None
        r_camera = r["camera"] if "camera" in r.keys() else None
        if new_lens and r_lens and new_lens.strip().lower() == r_lens.strip().lower():
            dist -= 0.22
        if new_camera and r_camera and new_camera.strip().lower() == r_camera.strip().lower():
            dist -= 0.08
        contrast_diff = abs((new_photo_features.get("visual_contrast") or 0.0) - (r["visual_contrast"] or 0.0))
        if contrast_diff < 10.0:
            dist -= 0.05
        scored.append((max(dist, 0.0), r))

    if not scored:
        return None

    scored.sort(key=lambda t: t[0])
    top = scored[:effective_top_k] if len(scored) >= effective_top_k else scored

    total_weight = 0.0
    exp_acc = sha_acc = 0.0
    ref_brightness_acc = ref_p10_acc = ref_p25_acc = ref_p90_acc = ref_contrast_acc = 0.0
    matched_presets = []
    for dist, r in top:
        w = 1.0 / (dist + 1e-3)
        total_weight      += w
        exp_acc           += w * r["exposure"]
        sha_acc           += w * r["shadows"]
        ref_brightness_acc += w * (r["brightness"]      or 0.0)
        ref_p10_acc       += w * (r["luminance_p10"]   or 0.0)
        ref_p25_acc       += w * (r["luminance_p25"]   or 0.0)
        ref_p90_acc       += w * (r["luminance_p90"]   or 0.0)
        ref_contrast_acc  += w * (r["visual_contrast"] or 0.0)
        if r["preset_name"]:
            matched_presets.append(r["preset_name"])

    base_exposure = exp_acc / total_weight
    base_shadows  = sha_acc / total_weight
    ref_brightness = ref_brightness_acc / total_weight
    ref_p10 = ref_p10_acc / total_weight
    ref_p25 = ref_p25_acc / total_weight
    ref_p90 = ref_p90_acc / total_weight
    ref_contrast = ref_contrast_acc / total_weight

    new_brightness = new_photo_features.get("brightness",      0.0) or 0.0
    new_p10        = new_photo_features.get("luminance_p10",   0.0) or 0.0
    new_p25        = new_photo_features.get("luminance_p25",   0.0) or 0.0
    new_p90        = new_photo_features.get("luminance_p90", 255.0) or 255.0
    new_contrast   = new_photo_features.get("visual_contrast", 0.0) or 0.0

    # Exposure correction — attenuated by contrast mismatch + highlight protection
    brightness_diff  = new_brightness - ref_brightness
    contrast_trust   = max(0.3, 1.0 - abs(new_contrast - ref_contrast) / 128.0)
    exposure_correction = -(brightness_diff / 255.0) * 0.65 * contrast_trust
    highlight_headroom = (255.0 - new_p90) / 255.0
    if highlight_headroom < 0.18 and exposure_correction > 0:
        exposure_correction *= highlight_headroom / 0.18

    # Shadows correction — boosted for deep-shadow recovery
    shadow_diff = new_p25 - ref_p25
    shadows_correction  = -(shadow_diff / 255.0) * 45.0
    shadow_floor_deficit = max(0.0, 20.0 - new_p10) / 20.0
    shadows_correction  += shadow_floor_deficit * 12.0
    if ref_p10 > new_p10 + 30:
        shadows_correction *= 0.75

    final_exposure = max(-5.0,   min(5.0,   base_exposure + exposure_correction))
    final_shadows  = max(-100.0, min(100.0, base_shadows  + shadows_correction))
    matched_preset = max(set(matched_presets), key=matched_presets.count) if matched_presets else None

    # --- Bias correction (retroalimentação) --------------------------------
    # Use learned systematic errors from correction_log to nudge the KNN
    # estimate.  This turns the "replace + reimport" workflow into a genuine
    # online bias-correction loop: after the user corrects the AI's edits in
    # Lightroom and reimports the catalog, the delta is stored and future
    # similar photos get pre-corrected estimates.
    bias_exp, bias_sha, n_corrections = _get_bias_correction(con, new_photo_features)
    if n_corrections > 0:
        final_exposure = max(-5.0,   min(5.0,   final_exposure + bias_exp))
        final_shadows  = max(-100.0, min(100.0, final_shadows  + bias_sha))

    correction_note = (
        f"vizinhos: {len(top)}, "
        f"Δbrilho: {exposure_correction:+.2f} EV (confiança contraste: {contrast_trust:.2f}), "
        f"Δsombras: {shadows_correction:+.1f}"
        + (", proteção altas-luzes" if highlight_headroom < 0.18 else "")
        + (", recuperação sombras profundas" if shadow_floor_deficit > 0.1 else "")
        + (f", bias aprendido: Δexp={bias_exp:+.2f}/Δsha={bias_sha:+.1f} ({n_corrections} correções)"
           if n_corrections > 0 else "")
    )

    return Suggestion(
        exposure=round(final_exposure, 2), shadows=round(final_shadows, 2),
        matched_preset=matched_preset, reference_count=len(top),
        correction_note=correction_note, temperature=None, tint=None,
    )


def apply_settings_to_catalog_copy(
    copy_path: Path,
    updates: dict[int, tuple[float, float]],
    logger: logging.Logger,
    wb_updates: Optional[dict[int, tuple[float, float]]] = None,
) -> dict[int, tuple[Optional[float], Optional[float], Optional[float], Optional[float]]]:
    """Write Exposure2012/Shadows2012 (and optionally Temperature2012/Tint +
    WhiteBalance="Custom") into a *copy* of a catalog.
    Returns the values read back after writing, for verification."""
    wb_updates = wb_updates or {}
    con = sqlite3.connect(str(copy_path))
    cur = con.cursor()

    all_ids = set(updates) | set(wb_updates)
    for image_id in all_ids:
        cur.execute("SELECT text FROM Adobe_imageDevelopSettings WHERE image = ?", (image_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            logger.warning(f"Imagem {image_id}: sem registro de Develop Settings, pulando.")
            continue
        text = row[0]

        if image_id in updates:
            exposure, shadows = updates[image_id]
            new_text, n1 = EXPOSURE_RE.subn(rf"\g<1>{exposure}", text)
            new_text, n2 = SHADOWS_RE.subn(rf"\g<1>{shadows}", new_text)
            if n1 == 0 or n2 == 0:
                logger.warning(f"Imagem {image_id}: não foi possível localizar Exposure2012/Shadows2012 no texto.")
                new_text = text  # keep unchanged, still try WB below
            text = new_text

        if image_id in wb_updates:
            temperature, tint = wb_updates[image_id]
            # Temperature/Tint fields may use both "Temperature2012" (process
            # version 2012+) and "Temperature" (older). Replace whichever is
            # present; if absent, do NOT insert a new line (Lightroom may
            # reject settings blocks with unknown fields).
            temp2_re = re.compile(r"(Temperature2012\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)")
            tint2_re = re.compile(r"(Tint2012\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)")
            nt1 = 0
            nt2 = 0
            new_text = text
            if temp2_re.search(new_text):
                new_text, nt1 = temp2_re.subn(rf"\g<1>{int(temperature)}", new_text)
            elif TEMPERATURE_RE.search(new_text):
                new_text, nt1 = TEMPERATURE_RE.subn(rf"\g<1>{int(temperature)}", new_text)
            if tint2_re.search(new_text):
                new_text, nt2 = tint2_re.subn(rf"\g<1>{int(tint)}", new_text)
            elif TINT_RE.search(new_text):
                new_text, nt2 = TINT_RE.subn(rf"\g<1>{int(tint)}", new_text)
            # Force WhiteBalance = "Custom" so Lightroom honours the numeric values
            if nt1 > 0 or nt2 > 0:
                if WHITE_BALANCE_RE.search(new_text):
                    new_text = WHITE_BALANCE_RE.sub(r'\g<1>Custom\g<3>', new_text)
                logger.info(f"Imagem {image_id}: WB escrito -> Temperature={int(temperature)}, Tint={int(tint)}")
            else:
                logger.warning(f"Imagem {image_id}: campos Temperature/Tint não encontrados no texto, WB ignorado.")
            text = new_text

        cur.execute(
            "UPDATE Adobe_imageDevelopSettings SET text = ? WHERE image = ?",
            (text, image_id),
        )

    con.commit()

    integrity = cur.execute("PRAGMA integrity_check").fetchone()[0]
    logger.info(f"PRAGMA integrity_check -> {integrity}")
    if integrity != "ok":
        con.close()
        raise RuntimeError(f"Falha na verificação de integridade do catálogo copiado: {integrity}")

    readback: dict[int, tuple[Optional[float], Optional[float], Optional[float], Optional[float]]] = {}
    for image_id in all_ids:
        cur.execute("SELECT text FROM Adobe_imageDevelopSettings WHERE image = ?", (image_id,))
        row = cur.fetchone()
        text = row[0] if row else None
        temp2_re = re.compile(r"(Temperature2012\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)")
        tint2_re = re.compile(r"(Tint2012\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?)")
        rb_temp = _extract_field(text, temp2_re) or _extract_field(text, TEMPERATURE_RE) if text else None
        rb_tint = _extract_field(text, tint2_re) or _extract_field(text, TINT_RE) if text else None
        readback[image_id] = (
            _extract_field(text, EXPOSURE_RE) if text else None,
            _extract_field(text, SHADOWS_RE) if text else None,
            rb_temp,
            rb_tint,
        )

    con.close()
    return readback


def _insert_edits_history(
    con: sqlite3.Connection,
    run_id: int,
    catalog_path: str,
    photos: list,
    report_rows: list,
) -> None:
    """Insert one edits_history row per photo that received a suggestion.

    ``report_rows`` is the list of ``(filename, exposure, shadows, temperature,
    tint, correction_note)`` tuples built during the suggestion loop.
    ``photos`` is the raw list of CatalogPhoto objects in iteration order,
    used to look up ``image_id_local`` and ``preset_name`` by filename."""
    photo_by_name: dict[str, object] = {p.filename: p for p in photos}
    for filename, exp, sha, temp, tint, note in report_rows:
        if exp is None:
            continue   # no suggestion generated for this photo
        p = photo_by_name.get(filename)
        con.execute(
            """
            INSERT INTO edits_history
                (run_id, catalog_path, filename, image_id_local,
                 preset, exposure, shadows, temperature, tint, correction_note)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                catalog_path,
                filename,
                p.image_id_local if p else None,
                p.preset_name if p else None,
                exp,
                sha,
                temp,
                tint,
                note,
            ),
        )


def edit_new_catalog(
    con: sqlite3.Connection,
    catalog_path: Path,
    workdir: Path,
    edited_output_dir: Path,
    logger: logging.Logger,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Full pipeline for a single new catalog: copy -> read -> suggest ->
    write -> integrity check -> move to edited folder. Never touches the
    original file."""
    logger.info(f"Processando catálogo novo: {catalog_path}")
    copy_path = open_catalog_copy(catalog_path, workdir)
    previews_dir = find_previews_dir(catalog_path)
    if previews_dir:
        logger.info(f"Cache de previews do Lightroom encontrado: {previews_dir}")
    else:
        logger.warning("Cache de previews do Lightroom (Previews.lrdata) não encontrado ao lado do catálogo.")

    photos = read_catalog_photos(copy_path, logger)
    logger.info(f"{len(photos)} fotos no catálogo novo.")

    updates:              dict[int, tuple[float, float]] = {}
    report_rows:          list  = []
    features_by_filename: dict  = {}   # {filename: feature_dict_or_None} — saved for bias learning
    used_preview = 0
    used_original = 0
    no_features = 0
    for i, photo in enumerate(photos):
        preview_bytes = load_preview_image_bytes(previews_dir, photo.image_id_local, photo.id_global, logger)
        features = extract_visual_features(photo.file_path, logger, preview_bytes=preview_bytes)
        features_by_filename[photo.filename] = features
        if features and preview_bytes:
            used_preview += 1
        elif features:
            used_original += 1
        else:
            no_features += 1
        suggestion = suggest_exposure_shadows(
            con, features, photo.preset_name, logger,
            new_lens=photo.lens, new_camera=photo.camera,
        )
        if suggestion is None:
            logger.warning(f"Foto {photo.filename}: nenhuma sugestão gerada (banco vazio?).")
            report_rows.append((photo.filename, None, None, None, None, "sem sugestão"))
        else:
            updates[photo.image_id_local] = (suggestion.exposure, suggestion.shadows)
            report_rows.append((
                photo.filename,
                suggestion.exposure,
                suggestion.shadows,
                None,   # temperature — WB não é sugerido automaticamente
                None,   # tint
                suggestion.correction_note,
            ))
        if progress_cb:
            progress_cb(i + 1, len(photos))

    logger.info(
        f"Features visuais: {used_preview} via preview do Lightroom, "
        f"{used_original} via arquivo original, {no_features} sem features (fallback por preset)."
    )
    if used_preview == 0 and previews_dir:
        _diagnose_preview_mismatch(previews_dir, photos, logger)

    if not updates:
        try:
            os.remove(copy_path)
        except OSError:
            pass
        raise RuntimeError("Nenhuma foto pôde ser atualizada (banco vazio ou catálogo sem configurações de revelação).")

    readback = apply_settings_to_catalog_copy(copy_path, updates, logger, wb_updates={})

    for image_id, (exp, sha) in updates.items():
        rb = readback.get(image_id, (None, None, None, None))
        got_exp, got_sha = rb[0], rb[1]
        if got_exp is None or abs(got_exp - exp) > 1e-6 or got_sha is None or abs(got_sha - sha) > 1e-6:
            logger.error(f"Imagem {image_id}: valores gravados não conferem (esperado {exp}/{sha}, lido {got_exp}/{got_sha}).")
        else:
            got_temp, got_tint = rb[2], rb[3]
            wb_str = f", WB={got_temp:.0f}K/{got_tint:+.0f}" if got_temp is not None else ""
            logger.info(f"Imagem {image_id}: Exposure={got_exp}, Shadows={got_sha}{wb_str} confirmados na cópia.")

    edited_output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    final_stem = f"{catalog_path.stem}_editado_{stamp}"
    final_name = f"{final_stem}.lrcat"
    final_path = edited_output_dir / final_name
    shutil.move(str(copy_path), str(final_path))
    logger.info(f"Catálogo editado movido para: {final_path}")

    # Lightroom expects the catalog's companion files (preview caches, smart
    # previews, catalog settings) to sit right next to the .lrcat with a
    # matching name, or it will rebuild them from scratch / complain about a
    # missing cache. Copy every "<catalog name>*" sibling from the ORIGINAL
    # catalog's folder (never move/touch the original itself) into the
    # edited folder, renamed to match the new file.
    # SQLite/Lightroom leave transient companion files next to an OPEN
    # catalog: "<name>.lrcat-wal" (uncommitted transactions), "-shm"
    # (shared-memory index for the WAL) and "-lock"/"-journal" (lock
    # markers). These are only meaningful paired with the exact catalog
    # file Lightroom currently has open — copying/renaming a stale one
    # alongside our finished, already-committed copy can make Lightroom
    # think a transaction is still pending, replay it, or believe the
    # catalog is already open elsewhere. Never carry these over; only
    # genuine data caches (Previews.lrdata, Smart Previews.lrdata, etc.)
    # are safe to copy.
    _UNSAFE_CATALOG_SIBLING_SUFFIXES = (
        ".lrcat-wal", ".lrcat-shm", ".lrcat-lock", ".lrcat-journal", "-journal",
    )
    original_stem = catalog_path.stem
    original_dir = catalog_path.parent
    for sibling in original_dir.glob(f"{original_stem}*"):
        if sibling.resolve() == catalog_path.resolve():
            continue  # the .lrcat itself was already copied/edited above
        suffix = sibling.name[len(original_stem):]  # e.g. " Previews.lrdata", ".lrcat-wal"
        if suffix.lower().endswith(_UNSAFE_CATALOG_SIBLING_SUFFIXES):
            logger.info(f"Ignorado (arquivo temporário do SQLite/Lightroom): {sibling.name}")
            continue
        dest = edited_output_dir / f"{final_stem}{suffix}"
        try:
            if sibling.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(sibling, dest)
            else:
                shutil.copy2(sibling, dest)
            logger.info(f"Arquivo/pasta do catálogo copiado: {sibling.name} -> {dest.name}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Não foi possível copiar {sibling} para a pasta de editados: {exc}")

    runs_cur = con.execute(
        "INSERT INTO runs (kind, catalog_path, photos_count, notes) VALUES (?,?,?,?)",
        ("edit", str(catalog_path), len(updates), str(final_path)),
    )
    run_id = runs_cur.lastrowid
    _insert_edits_history(con, run_id, str(catalog_path), photos, report_rows)
    # Save AI suggestions for future bias learning (retroalimentação)
    _save_ai_suggestions(
        con, run_id, str(catalog_path), photos, report_rows, features_by_filename,
        edited_catalog_path=str(final_path),
    )
    con.commit()

    return {
        "final_path": str(final_path),
        "updated_count": len(updates),
        "total_photos": len(photos),
        "report_rows": report_rows,
    }


# ---------------------------------------------------------------------------
# XMP preset parsing + application by lens
# ---------------------------------------------------------------------------

def parse_xmp_preset(xmp_path: Path) -> dict[str, str]:
    """Parse a Lightroom .xmp preset and return {setting_name: value_string}.

    Handles both attribute-style and child-element-style XMP.
    All numeric values come back as strings so the caller can substitute
    them verbatim into the develop-settings text."""
    import xml.etree.ElementTree as ET

    CRS = "http://ns.adobe.com/camera-raw-settings/1.0/"
    try:
        tree = ET.parse(str(xmp_path))
        root = tree.getroot()
    except ET.ParseError as exc:
        raise ValueError(f"XMP inválido ({xmp_path.name}): {exc}") from exc

    settings: dict[str, str] = {}
    for elem in root.iter():
        # --- attribute style: crs:Exposure2012="-0.5" ---
        for attr, val in elem.attrib.items():
            if attr.startswith(f"{{{CRS}}}"):
                settings[attr[len(f"{{{CRS}}}"):]  ] = val
        # --- element style: <crs:Exposure2012>-0.5</crs:Exposure2012> ---
        if elem.tag.startswith(f"{{{CRS}}}") and elem.text and elem.text.strip():
            settings[elem.tag[len(f"{{{CRS}}}"):]  ] = elem.text.strip()

    return settings


# ---------------------------------------------------------------------------
# XMP preset application helpers
# ---------------------------------------------------------------------------

# Keys present in XMP files that are PRESET METADATA, not develop settings,
# or that are COMPLEX NESTED STRUCTURES (curves, local adjustments, looks)
# that this simple flat key/value parser cannot faithfully reconstruct.
# These must never be written into a catalog's develop-settings text block —
# writing a wrong-shaped value for one of these would corrupt the Lua table
# and make Lightroom reset every slider for that photo to its default (the
# "fotos zeradas" bug). Everything else in the preset — including full
# calibration (RedHue/GreenHue/BlueHue/*Saturation, ShadowTint), color (HSL,
# Split Toning, Color Grading), and White Balance — IS applied.
_XMP_SKIP_KEYS: frozenset = frozenset({
    "Version", "ProcessVersion", "PresetType", "Cluster", "UUID",
    "SupportsAmount", "SupportsColor", "SupportsMonochrome",
    "SupportsHighDynamicRange", "SupportsNormalDynamicRange",
    "SupportsSceneReferred", "SupportsOutputReferred",
    "CameraModelRestriction", "Copyright", "ContactInfo",
    "HasSettings", "Name", "ShortName", "SortName", "Group", "Description",
    # Complex list structures serialised as <rdf:Seq> — can't be inlined
    # safely as a flat scalar without corrupting the Lua table.
    "ToneCurvePV2012", "ToneCurvePV2012Red", "ToneCurvePV2012Green",
    "ToneCurvePV2012Blue", "Look", "GradientBasedCorrections", "RadialFilter",
    "RetouchAreas", "MaskGroupBasedCorrections", "PaintBasedCorrections",
    "CircularGradientBasedCorrections",
})

# Keys whose values are QUOTED STRINGS in the develop-settings text block,
# not plain numbers.  All others are treated as numeric scalars.
_XMP_STRING_KEYS: frozenset = frozenset({
    "LensProfileSetup", "LensProfileName", "LensProfileFilename",
    "LensProfileDigest", "CameraProfile", "CameraProfileDigest",
    "Clarity",   # occasionally quoted in older presets
    "WhiteBalance",
})

# Keys whose values are Lua BOOLEANS (bare, lowercase, unquoted `true`/`false`)
# in the develop-settings text block — NOT quoted strings and NOT 0/1
# integers. XMP represents them as "True"/"False" (capitalised) strings, so
# they need their own conversion + their own search/replace regex. Writing
# these as quoted strings (e.g. `LensProfileIsEmbedded = "False"`) produces
# invalid Lua and is what causes Lightroom to silently reset the whole
# develop-settings block for that photo (all sliders back to zero/default).
_XMP_BOOL_KEYS: frozenset = frozenset({
    "LensProfileIsEmbedded", "ConvertToGrayscale",
})


def _xmp_value_to_lua(key: str, raw_value: str) -> str:
    """Convert a raw XMP attribute value to the string that should appear
    on the right-hand side of a Lua-style develop-settings assignment.

    * Boolean keys : bare lowercase `true` / `false` (no quotes).
    * String keys  : wrap in double-quotes (Lightroom stores them that way).
    * Numeric values: strip leading '+', keep leading '-'.
    """
    if key in _XMP_BOOL_KEYS:
        return "true" if raw_value.strip().lower() in ("true", "1") else "false"
    if key in _XMP_STRING_KEYS:
        return f'"{raw_value}"'
    # Strip a leading '+' so "+86" → "86" (Lua is fine with plain integers)
    cleaned = raw_value.lstrip("+") if raw_value.startswith("+") else raw_value
    # If it still looks numeric, return as-is; otherwise fall back to quoted.
    if re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?', cleaned):
        return cleaned
    # Non-numeric, non-string-key value → quote it for safety
    return f'"{raw_value}"'


def _validate_develop_text(text: str) -> bool:
    """Cheap structural sanity check on a Lua-ish develop-settings block.

    This is NOT a Lua parser — it is a fast guard against the most common
    ways a hand-rolled string substitution can corrupt the block: an
    unbalanced brace count, or an odd number of (unescaped) double quotes
    left over from a broken insert/replace. PRAGMA integrity_check does NOT
    catch either of these: it only verifies the SQLite page structure is
    intact, never that a TEXT column's contents still parse as valid Lua —
    which is exactly how this bug slipped through before (integrity_check
    passed while the photo's sliders still came back zeroed in Lightroom).
    """
    if text.count("{") != text.count("}"):
        return False
    if len(re.findall(r'(?<!\\)"', text)) % 2 != 0:
        return False
    return True


def _apply_xmp_to_develop_text_impl(
    text: str, xmp_settings: dict[str, str],
) -> tuple[str, bool, list[str]]:
    """Real implementation shared by apply_xmp_to_develop_text and the
    lens pipeline. Returns (result_text, validation_failed, applied_keys).

    validation_failed=True means the substitution produced a structurally
    broken Lua block and result_text was rolled back to the ORIGINAL input
    — the caller must NOT treat this the same as "nothing needed to
    change" (that would silently misreport a real failure as
    PRESET_JA_IDENTICO). applied_keys lists every non-skipped key that was
    actually written (replaced or inserted), for verification/reporting.
    """
    original = text
    applied_keys: list[str] = []
    for key, raw_value in xmp_settings.items():
        if key in _XMP_SKIP_KEYS:
            continue

        lua_value = _xmp_value_to_lua(key, raw_value)

        if key in _XMP_BOOL_KEYS:
            # Match a bare Lua boolean: Key = true  /  Key = false
            bool_pat = re.compile(rf'({re.escape(key)}\s*=\s*)(true|false)', re.IGNORECASE)
            if bool_pat.search(text):
                text = bool_pat.sub(rf'\g<1>{lua_value}', text)
            else:
                text = _insert_develop_key(text, key, lua_value)
        elif key in _XMP_STRING_KEYS:
            # Match:  Key = "anything"
            str_pat = re.compile(
                rf'({re.escape(key)}\s*=\s*)"[^"]*"'
            )
            if str_pat.search(text):
                text = str_pat.sub(rf'\g<1>{lua_value}', text)
            else:
                text = _insert_develop_key(text, key, lua_value)
        else:
            # Match numeric:  Key = -3.14  or  Key = 0
            num_pat = re.compile(
                rf'({re.escape(key)}\s*=\s*)(-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)'
            )
            if num_pat.search(text):
                text = num_pat.sub(rf'\g<1>{lua_value}', text)
            else:
                text = _insert_develop_key(text, key, lua_value)
        applied_keys.append(key)

    if not _validate_develop_text(text):
        return original, True, applied_keys

    return text, False, applied_keys


def apply_xmp_to_develop_text(text: str, xmp_settings: dict[str, str]) -> str:
    """Apply XMP preset values into a Lightroom develop-settings text block.

    Behaviour:
    * Keys that already exist in the text → value is REPLACED in-place.
    * Keys that do NOT exist in the text  → a new "Key = value," line is
      INSERTED before the closing '}' of the develop-settings Lua table.
      This ensures the full preset is applied, including calibration
      (Red/Green/Blue Hue+Saturation, ShadowTint), color (HSL, Split
      Toning, Color Grading), lens-profile identifiers, defringe amounts,
      sharpening details, and White Balance.
    * Metadata / structural keys (listed in _XMP_SKIP_KEYS) are skipped —
      these are either preset bookkeeping or complex nested structures
      (curves, local adjustments) that can't be safely inlined as a flat
      scalar; writing them wrong would corrupt the Lua table and reset
      every other slider on that photo to default. Every other setting in
      the preset IS applied.
    * Every substitution only ever touches the matched key's own value —
      the rest of the Lua table (every other setting already on the photo)
      is left byte-for-byte untouched, so applying one preset never zeroes
      out unrelated settings.
    * The result is validated before being returned; if anything ends up
      structurally broken, the ORIGINAL text is returned unchanged rather
      than risk writing a corrupted block (see _validate_develop_text).

    NOTE: this wrapper intentionally keeps its original signature/behavior
    (returns only the text) for existing callers. apply_xmp_by_lens calls
    _apply_xmp_to_develop_text_impl directly so it can tell a genuine
    validation failure apart from "nothing needed to change" — see that
    function's use of the `validation_failed` flag.
    """
    new_text, _validation_failed, _applied_keys = _apply_xmp_to_develop_text_impl(text, xmp_settings)
    return new_text


def _insert_develop_key(text: str, key: str, lua_value: str) -> str:
    """Insert '  Key = lua_value,' before the last '}' in the Lua block.

    Lua table constructors require a separator between fields. This app's
    own catalogs always seem to end each field with a trailing comma, but
    that is an assumption, not a guarantee — some serializers omit the
    comma after the LAST field in a table. If we blindly insert a new
    "Key = value," line right before the closing '}' without checking
    that the preceding field already ends in a comma, we can silently
    produce:

        LastExistingKey = 5
        NewKey = "value",
    }

    — two adjacent table fields with no separator, which is invalid Lua.
    Lightroom does not surface this as an error: it just fails to parse
    the whole develop-settings block for that photo and falls back to
    defaults, which is exactly the "sliders zeradas" regression. This is
    the most likely reason it kept happening specifically on lens
    correction: lens-profile keys are usually ABSENT on photos that never
    had a lens profile applied before, so applying them always goes
    through this insert path, whereas most other preset fields already
    exist on the photo and only need to be replaced in place.
    """
    last_brace = text.rfind("}")
    if last_brace < 0:
        return text  # malformed block — leave untouched

    prefix = text[:last_brace]
    stripped = prefix.rstrip()
    if stripped and not stripped.endswith((",", "{")):
        prefix = stripped + ",\n"

    line = f"  {key} = {lua_value},\n"
    return prefix + line + text[last_brace:]


def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def _normalize_lens_name(raw: str) -> tuple[str, frozenset]:
    """Normalize a lens name for robust matching.

    Returns (ordered_normalized_string, token_set).

    Rather than blindly splitting on letter/digit boundaries (which breaks
    down on real lens names — "RF15-35mm" vs "RF 15-35mm" vs "RF 15-35"),
    this extracts the two numeric identifiers that actually distinguish one
    lens from another — the focal length and the aperture — as single
    atomic tokens, always in a canonical shape, regardless of spacing,
    slashes, or whether "mm" was written at all. Everything else (brand,
    mount, series letters like "L"/"USM"/"Art") is lowercased, stripped of
    accents/punctuation, and kept as plain word tokens in their original
    relative order.

    This keeps two different lenses that merely SHARE a focal length or
    aperture from looking alike (their brand/mount/series tokens still
    differ), while making "Canon RF 15-35mm F2.8 L IS USM",
    "Canon RF15-35mm F2.8 L IS USM" and "RF 15-35 F2.8L IS USM" normalize
    to compatible representations.
    """
    if not raw or not raw.strip():
        return "", frozenset()

    s = _strip_accents(raw.strip().lower())
    extracted: list[str] = []

    # Focal length written with an explicit "mm": "15-35mm", "35 mm", "50mm".
    # No leading \b — a focal length glued directly to a mount code
    # ("rf15-35mm") still has no word boundary between the letter and the
    # digit, but the number+"mm" pattern itself is what we're anchoring on.
    def _focal_mm_sub(m):
        nums = re.findall(r"\d+(?:\.\d+)?", m.group(0))
        extracted.append("-".join(nums) + "mm")
        return " "

    s = re.sub(
        r"\d{1,4}(?:\.\d+)?(?:\s*-\s*\d{1,4}(?:\.\d+)?)?\s*mm\b",
        _focal_mm_sub, s,
    )
    # Focal length range written WITHOUT "mm" at all, e.g. "15-35". Always
    # canonicalized with an "mm" suffix so it matches the case above.
    def _focal_bare_sub(m):
        nums = re.findall(r"\d+(?:\.\d+)?", m.group(0))
        extracted.append("-".join(nums) + "mm")
        return " "

    s = re.sub(
        r"\d{2,4}(?:\.\d+)?\s*-\s*\d{2,4}(?:\.\d+)?",
        _focal_bare_sub, s,
    )

    # Aperture: "f/2.8", "f 2.8", "F2.8" -> "f2.8". The negative lookbehind
    # excludes an 'f' glued to a preceding letter (the 'f' in "rf" or "af"),
    # so a lens-mount code is never mistaken for an aperture value.
    def _aperture_sub(m):
        extracted.append("f" + m.group(1).replace(",", "."))
        return " "

    s = re.sub(r"(?<![a-z])f\s*/?\s*(\d+(?:[.,]\d+)?)", _aperture_sub, s)

    # Remaining separators -> spaces, then strip any other punctuation.
    s = re.sub(r"[|/\-_()]", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    word_tokens = [t for t in s.split(" ") if t]

    all_tokens = word_tokens + extracted
    ordered = " ".join(all_tokens)
    return ordered, frozenset(all_tokens)


# Token-similarity thresholds for the last-resort fuzzy match. Both must
# hold for a match to be accepted, so two lenses that merely share a couple
# of generic tokens (e.g. both "f2.8", both a similar focal range) never
# match unless the *bulk* of their identifying tokens line up.
_LENS_TOKEN_RATIO_THRESHOLD = 0.8
_LENS_TOKEN_JACCARD_THRESHOLD = 0.5


def _focal_tokens(tokens: frozenset) -> frozenset:
    """The subset of a lens's tokens that are canonical focal-length tokens
    (see _normalize_lens_name — always end in 'mm', e.g. '24-70mm')."""
    return frozenset(t for t in tokens if t.endswith("mm"))


def _score_lens_candidate(
    key_ordered: str, key_tokens: frozenset,
    lens_ordered: str, lens_tokens: frozenset,
) -> tuple[Optional[str], float]:
    """Score how well one configured lens key matches one catalog lens name.

    Returns (match_type, score) where match_type is "exact", "substring",
    "token", or None (no acceptable match). Higher score = more specific /
    more trustworthy; exact always outranks substring, which always
    outranks token, so a later, weaker match type never displaces an
    earlier, stronger one for the same photo.
    """
    if not key_ordered or not lens_ordered:
        return None, 0.0

    if key_ordered == lens_ordered:
        return "exact", 3_000_000 + len(key_ordered)

    if key_ordered in lens_ordered or lens_ordered in key_ordered:
        return "substring", 2_000_000 + len(key_ordered)

    if not key_tokens or not lens_tokens:
        return None, 0.0

    # Focal length is the primary identifier distinguishing lenses that
    # otherwise share brand/mount/series/aperture words (e.g. "Sony FE
    # 24-70mm F2.8 GM" vs "Sony FE 70-200mm F2.8 GM"). If BOTH sides
    # explicitly state a focal length and they disagree, this can never be
    # a token-similarity match — no amount of shared brand/aperture wording
    # should paper over two genuinely different lenses. (Bug: previously,
    # 4 shared generic tokens out of 5 total was enough to pass the ratio/
    # jaccard thresholds even with completely different focal lengths.)
    key_focal = _focal_tokens(key_tokens)
    lens_focal = _focal_tokens(lens_tokens)
    if key_focal and lens_focal and not (key_focal & lens_focal):
        return None, 0.0

    overlap = key_tokens & lens_tokens
    if not overlap:
        return None, 0.0

    ratio = len(overlap) / min(len(key_tokens), len(lens_tokens))
    jaccard = len(overlap) / len(key_tokens | lens_tokens)
    if ratio >= _LENS_TOKEN_RATIO_THRESHOLD and jaccard >= _LENS_TOKEN_JACCARD_THRESHOLD:
        return "token", 1_000_000 + len(overlap) * 1000 + jaccard * 100

    return None, 0.0


def _best_lens_match(
    lens_ordered: str, lens_tokens: frozenset,
    keys_info: dict[str, tuple[str, frozenset]],
) -> tuple[Optional[str], Optional[str]]:
    """Pick the best-matching configured lens key for one catalog lens name.

    Considers EVERY configured key (never stops at the first hit) and picks
    the highest-scoring one, so a more specific key is always preferred
    over a more generic one that also happens to match, and dict ordering
    never decides the outcome.
    """
    best_key: Optional[str] = None
    best_type: Optional[str] = None
    best_score = -1.0
    for key, (key_ordered, key_tokens) in keys_info.items():
        match_type, score = _score_lens_candidate(key_ordered, key_tokens, lens_ordered, lens_tokens)
        if match_type is not None and score > best_score:
            best_key, best_type, best_score = key, match_type, score
    return best_key, best_type


# Human-readable reasons a photo did not get a lens preset applied.
_LENS_SKIP_SEM_TEXTO = "SEM_TEXTO_DEVELOP"
_LENS_SKIP_SEM_METADATA = "SEM_METADATA_DE_LENTE"
_LENS_SKIP_SEM_PRESET = "SEM_PRESET_COMPATIVEL"
_LENS_SKIP_JA_IDENTICO = "PRESET_JA_IDENTICO"
_LENS_SKIP_ERRO = "ERRO_APLICACAO"
_LENS_SKIP_UPDATE_NAO_AFETOU_LINHA = "UPDATE_NAO_AFETOU_LINHA"
_LENS_SKIP_VERIFICACAO_FALHOU = "VERIFICACAO_FALHOU"

# A handful of preset fields that, if present in the preset being applied,
# MUST be readable back from the catalog after the UPDATE for that photo to
# be trusted as genuinely changed — not just "the UPDATE statement ran
# without raising". These are exactly the fields most likely to be silently
# dropped by a broken insert/replace (they are usually ABSENT on a photo
# that never had a lens profile before, so they go through the riskier
# _insert_develop_key path rather than a simple in-place replace).
_LENS_VERIFY_CRITICAL_KEYS = (
    "LensProfileEnable", "AutoLateralCA", "LensProfileSetup", "LensProfileName",
)


def apply_xmp_by_lens(
    catalog_path: Path,
    lens_map: dict[str, Path],
    logger: logging.Logger,
    backup: bool = True,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    alias_lookup: Optional[Callable[[str], Optional[str]]] = None,
) -> dict:
    """Apply XMP presets to photos in a catalog matched by lens name.

    lens_map: {configured_lens_key -> xmp_path}. Matching is done via
    normalized exact match, then normalized substring, then token
    similarity (see _normalize_lens_name / _best_lens_match) — so
    "Canon RF 15-35mm F2.8 L IS USM", "Canon RF15-35mm F2.8 L IS USM" and
    "RF 15-35 F2.8L IS USM" are all treated as the same lens, without
    risking a false match between two genuinely different lenses.

    The full preset (including LensProfile*, AutoLateralCA, Defringe*) is
    applied via apply_xmp_to_develop_text, which already replaces the
    lens-profile fields in place — this function only decides WHICH preset
    a photo should get.

    Writes directly to the original catalog. Creates a .lrcat.bak if
    backup=True. Every photo that does not receive a preset is recorded
    with a specific reason (see _LENS_SKIP_* constants) in the returned
    dict's "skipped_photos" list and in a CSV report written next to the
    catalog, so silent, unexplained skips can't happen again."""
    if backup:
        bak = catalog_path.with_suffix(".lrcat.bak")
        shutil.copy2(str(catalog_path), str(bak))
        logger.info(f"Backup criado: {bak.name}")

    parsed: dict[str, dict[str, str]] = {}
    keys_info: dict[str, tuple[str, frozenset]] = {}
    for lens_key, xmp_path in lens_map.items():
        try:
            parsed[lens_key] = parse_xmp_preset(Path(xmp_path))
            keys_info[lens_key] = _normalize_lens_name(lens_key)
            logger.info(
                f"Preset '{xmp_path.name}' carregado para lente '{lens_key}' "
                f"({len(parsed[lens_key])} configurações)"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Erro ao ler XMP para '{lens_key}': {exc}")

    if not parsed:
        raise RuntimeError("Nenhum preset XMP válido foi carregado.")

    con = sqlite3.connect(str(catalog_path))
    cur = con.cursor()

    tables = _available_tables(cur)
    exif_cols = (
        _available_cols(cur, "AgHarvestedExifMetadata")
        if "AgHarvestedExifMetadata" in tables
        else set()
    )

    # Primary source: AgHarvestedExifMetadata.lensRef -> AgInternedExifLens.value
    lens_by_image: dict[int, str] = {}
    if "AgInternedExifLens" in tables and "lensRef" in exif_cols:
        cur.execute(
            """
            SELECT e.image, il.value
            FROM AgHarvestedExifMetadata e
            JOIN AgInternedExifLens il ON il.id_local = e.lensRef
            WHERE il.value IS NOT NULL AND il.value != ''
            """
        )
        lens_by_image = {row[0]: row[1] for row in cur.fetchall()}

    # Fallback: some catalog variants also carry a flat text lens field
    # directly on AgHarvestedExifMetadata (not via a *Ref lookup table).
    # Only used to fill in photos the primary lookup found nothing for —
    # never overrides a value already found, and never invents anything.
    fallback_lens_cols = sorted(
        c for c in exif_cols
        if "lens" in c.lower() and not c.lower().endswith("ref")
    )
    if fallback_lens_cols and "AgHarvestedExifMetadata" in tables:
        cols_sql = ", ".join(f"e.{c}" for c in fallback_lens_cols)
        try:
            cur.execute(f"SELECT e.image, {cols_sql} FROM AgHarvestedExifMetadata e")
            for row in cur.fetchall():
                image_id = row[0]
                if image_id in lens_by_image:
                    continue
                for val in row[1:]:
                    if isinstance(val, str) and val.strip():
                        lens_by_image[image_id] = val.strip()
                        break
        except sqlite3.OperationalError:
            pass

    # Filename lookup, for reporting only — never touches anything besides
    # Adobe_imageDevelopSettings.text.
    filename_by_image: dict[int, str] = {}
    if {"Adobe_images", "AgLibraryFile", "AgLibraryFolder", "AgLibraryRootFolder"} <= tables:
        try:
            cur.execute(
                """
                SELECT i.id_local, f.idx_filename
                FROM Adobe_images i
                JOIN AgLibraryFile f ON f.id_local = i.rootFile
                """
            )
            filename_by_image = {row[0]: (row[1] or "") for row in cur.fetchall()}
        except sqlite3.OperationalError:
            pass

    cur.execute("SELECT image, text FROM Adobe_imageDevelopSettings")
    all_settings = cur.fetchall()
    total = len(all_settings)
    applied = 0
    identical = 0
    sem_texto = 0
    sem_metadata = 0
    sem_preset = 0
    erros = 0
    update_sem_efeito = 0
    verificacao_falhou = 0
    skipped_photos: list[dict] = []
    # One row per photo actually considered (has develop text + lens
    # metadata), regardless of outcome — the full audit trail requested:
    # arquivo, image_id, lente_catalogo, lente_configurada, tipo_match,
    # preset_xmp, campos_xmp, alterado, verificado, motivo, erro.
    full_report_rows: list[dict] = []
    # Per configured key: how many photos actually matched it, and via what
    # match type — used both for the final lens-comparison report and to
    # avoid ever reporting a key as "unused" when it quietly matched via a
    # fuzzy path.
    key_match_counts: dict[str, int] = {k: 0 for k in parsed}
    distinct_catalog_lenses: set[str] = set()

    # Cache normalization per distinct catalog lens string so we don't
    # re-run the regex pipeline once per photo when many photos share a lens.
    lens_norm_cache: dict[str, tuple[str, frozenset]] = {}

    def _norm_cached(lens_text: str) -> tuple[str, frozenset]:
        cached = lens_norm_cache.get(lens_text)
        if cached is None:
            cached = _normalize_lens_name(lens_text)
            lens_norm_cache[lens_text] = cached
        return cached

    for idx, (image_id, text) in enumerate(all_settings):
        if progress_cb:
            progress_cb(idx + 1, total)

        photo_lens_raw = (lens_by_image.get(image_id) or "").strip()
        filename = filename_by_image.get(image_id, "")

        if not text:
            sem_texto += 1
            skipped_photos.append({
                "image_id": image_id, "filename": filename,
                "lens_found": photo_lens_raw, "motivo": _LENS_SKIP_SEM_TEXTO,
            })
            continue

        if not photo_lens_raw:
            sem_metadata += 1
            skipped_photos.append({
                "image_id": image_id, "filename": filename,
                "lens_found": "", "motivo": _LENS_SKIP_SEM_METADATA,
            })
            continue

        distinct_catalog_lenses.add(photo_lens_raw)
        lens_ordered, lens_tokens = _norm_cached(photo_lens_raw)
        best_key, match_type = _best_lens_match(lens_ordered, lens_tokens, keys_info)

        # v12: only consulted when the existing v11 matcher above found
        # NOTHING — this alias layer never overrides or competes with an
        # exact/substring/token match _best_lens_match already found; it
        # purely fills the gap for lens strings the user has explicitly
        # mapped via the "Lentes e Aliases" screen. Any failure here is
        # swallowed and behaves exactly like v11 (best_key stays None).
        if best_key is None and alias_lookup is not None:
            try:
                canonical = alias_lookup(photo_lens_raw)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[v12:lens_alias] busca de alias falhou para '{photo_lens_raw}': {exc!r}")
                canonical = None
            if canonical:
                alias_ordered, alias_tokens = _norm_cached(canonical)
                best_key, match_type = _best_lens_match(alias_ordered, alias_tokens, keys_info)

        if best_key is None:
            sem_preset += 1
            skipped_photos.append({
                "image_id": image_id, "filename": filename,
                "lens_found": photo_lens_raw, "motivo": _LENS_SKIP_SEM_PRESET,
            })
            full_report_rows.append({
                "arquivo": filename, "image_id": image_id,
                "lente_catalogo": photo_lens_raw, "lente_configurada": "",
                "tipo_match": "", "preset_xmp": "", "campos_xmp": "",
                "alterado": "nao", "verificado": "", "motivo": _LENS_SKIP_SEM_PRESET, "erro": "",
            })
            continue

        preset_settings = parsed[best_key]
        xmp_name = Path(lens_map[best_key]).name

        try:
            new_text, validation_failed, applied_keys = _apply_xmp_to_develop_text_impl(text, preset_settings)
        except Exception as exc:  # noqa: BLE001
            erros += 1
            logger.error(f"Erro ao aplicar preset em image_id={image_id} ({filename}): {exc}")
            skipped_photos.append({
                "image_id": image_id, "filename": filename,
                "lens_found": photo_lens_raw, "motivo": _LENS_SKIP_ERRO,
            })
            full_report_rows.append({
                "arquivo": filename, "image_id": image_id,
                "lente_catalogo": photo_lens_raw, "lente_configurada": best_key,
                "tipo_match": match_type or "", "preset_xmp": xmp_name,
                "campos_xmp": len(preset_settings), "alterado": "nao",
                "verificado": "nao", "motivo": _LENS_SKIP_ERRO, "erro": str(exc),
            })
            continue

        if validation_failed:
            # apply_xmp_to_develop_text's own structural guard rejected the
            # substitution and reverted to the ORIGINAL text — this is a
            # real failure, not "nothing needed to change". Reporting it as
            # PRESET_JA_IDENTICO here would be exactly the "aplicado sem
            # nunca ter sido aplicado" bug: silently masking a broken write
            # as a harmless no-op.
            erros += 1
            logger.error(
                f"Validação estrutural falhou ao aplicar preset em image_id={image_id} "
                f"({filename}) — texto revertido ao original, NADA foi alterado nesta foto."
            )
            skipped_photos.append({
                "image_id": image_id, "filename": filename,
                "lens_found": photo_lens_raw, "motivo": _LENS_SKIP_ERRO,
            })
            full_report_rows.append({
                "arquivo": filename, "image_id": image_id,
                "lente_catalogo": photo_lens_raw, "lente_configurada": best_key,
                "tipo_match": match_type or "", "preset_xmp": xmp_name,
                "campos_xmp": len(preset_settings), "alterado": "nao",
                "verificado": "nao", "motivo": _LENS_SKIP_ERRO,
                "erro": "validacao estrutural do bloco Lua falhou",
            })
            continue

        if new_text == text:
            identical += 1
            key_match_counts[best_key] += 1
            skipped_photos.append({
                "image_id": image_id, "filename": filename,
                "lens_found": photo_lens_raw, "motivo": _LENS_SKIP_JA_IDENTICO,
            })
            full_report_rows.append({
                "arquivo": filename, "image_id": image_id,
                "lente_catalogo": photo_lens_raw, "lente_configurada": best_key,
                "tipo_match": match_type or "", "preset_xmp": xmp_name,
                "campos_xmp": len(preset_settings), "alterado": "nao",
                "verificado": "n/a", "motivo": _LENS_SKIP_JA_IDENTICO, "erro": "",
            })
            continue

        # Per-photo savepoint: the UPDATE + verification below either both
        # succeed (RELEASE keeps the change) or the change is rolled back
        # and this single photo is marked VERIFICACAO_FALHOU — a bad write
        # on one photo can never corrupt or abandon the rest of the batch,
        # and never gets silently counted as "applied".
        cur.execute("SAVEPOINT lens_photo")
        try:
            cur.execute(
                "UPDATE Adobe_imageDevelopSettings SET text = ? WHERE image = ?",
                (new_text, image_id),
            )
            rows_affected = cur.rowcount

            if rows_affected != 1:
                cur.execute("ROLLBACK TO lens_photo")
                update_sem_efeito += 1
                logger.error(
                    f"UPDATE não afetou a linha ativa de Develop para image_id={image_id} "
                    f"({filename}) — rowcount={rows_affected}. Nenhuma alteração foi salva."
                )
                skipped_photos.append({
                    "image_id": image_id, "filename": filename,
                    "lens_found": photo_lens_raw, "motivo": _LENS_SKIP_UPDATE_NAO_AFETOU_LINHA,
                })
                full_report_rows.append({
                    "arquivo": filename, "image_id": image_id,
                    "lente_catalogo": photo_lens_raw, "lente_configurada": best_key,
                    "tipo_match": match_type or "", "preset_xmp": xmp_name,
                    "campos_xmp": len(preset_settings), "alterado": "nao",
                    "verificado": "nao", "motivo": _LENS_SKIP_UPDATE_NAO_AFETOU_LINHA,
                    "erro": f"rowcount={rows_affected}",
                })
                continue

            # Mandatory post-write verification: read the row back and
            # confirm the critical lens-profile fields this preset actually
            # touched really are present with the value we just wrote —
            # PRAGMA integrity_check only proves the SQLite page structure
            # is intact, never that the TEXT column still holds what we
            # think it holds.
            verify_row = cur.execute(
                "SELECT text FROM Adobe_imageDevelopSettings WHERE image = ?",
                (image_id,),
            ).fetchone()
            persisted_text = verify_row[0] if verify_row else None

            keys_to_verify = [k for k in _LENS_VERIFY_CRITICAL_KEYS if k in applied_keys]
            verification_ok = persisted_text == new_text
            if verification_ok:
                for vkey in keys_to_verify:
                    expected_val = _xmp_value_to_lua(vkey, preset_settings[vkey])
                    if not re.search(
                        rf'{re.escape(vkey)}\s*=\s*{re.escape(expected_val)}(?![\w.])',
                        persisted_text,
                    ):
                        verification_ok = False
                        break

            if not verification_ok:
                cur.execute("ROLLBACK TO lens_photo")
                verificacao_falhou += 1
                logger.error(
                    f"Verificação pós-escrita falhou para image_id={image_id} ({filename}) — "
                    f"transação revertida, NENHUMA alteração foi salva nesta foto."
                )
                skipped_photos.append({
                    "image_id": image_id, "filename": filename,
                    "lens_found": photo_lens_raw, "motivo": _LENS_SKIP_VERIFICACAO_FALHOU,
                })
                full_report_rows.append({
                    "arquivo": filename, "image_id": image_id,
                    "lente_catalogo": photo_lens_raw, "lente_configurada": best_key,
                    "tipo_match": match_type or "", "preset_xmp": xmp_name,
                    "campos_xmp": len(preset_settings), "alterado": "nao",
                    "verificado": "nao", "motivo": _LENS_SKIP_VERIFICACAO_FALHOU, "erro": "",
                })
                continue

            cur.execute("RELEASE lens_photo")
        except Exception as exc:  # noqa: BLE001
            cur.execute("ROLLBACK TO lens_photo")
            erros += 1
            logger.error(f"Erro ao gravar/verificar preset em image_id={image_id} ({filename}): {exc}")
            skipped_photos.append({
                "image_id": image_id, "filename": filename,
                "lens_found": photo_lens_raw, "motivo": _LENS_SKIP_ERRO,
            })
            full_report_rows.append({
                "arquivo": filename, "image_id": image_id,
                "lente_catalogo": photo_lens_raw, "lente_configurada": best_key,
                "tipo_match": match_type or "", "preset_xmp": xmp_name,
                "campos_xmp": len(preset_settings), "alterado": "nao",
                "verificado": "nao", "motivo": _LENS_SKIP_ERRO, "erro": str(exc),
            })
            continue

        applied += 1
        key_match_counts[best_key] += 1
        full_report_rows.append({
            "arquivo": filename, "image_id": image_id,
            "lente_catalogo": photo_lens_raw, "lente_configurada": best_key,
            "tipo_match": match_type or "", "preset_xmp": xmp_name,
            "campos_xmp": len(preset_settings), "alterado": "sim",
            "verificado": "sim", "motivo": "", "erro": "",
        })

    con.commit()
    integrity = cur.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        con.close()
        raise RuntimeError(f"Falha de integridade após aplicar presets: {integrity}")
    con.close()

    skipped = (
        sem_texto + sem_metadata + sem_preset + erros
        + update_sem_efeito + verificacao_falhou
    )  # backward-compat total

    # Lens comparison report: which configured keys actually found a match
    # in this catalog, and which catalog lenses had no configured preset.
    lens_comparison: list[dict] = []
    for key in parsed:
        lens_comparison.append({
            "configurado": key,
            "encontrado_no_catalogo": key_match_counts[key] > 0,
            "fotos_correspondentes": key_match_counts[key],
        })
    unmatched_catalog_lenses = sorted(
        lens for lens in distinct_catalog_lenses
        if _best_lens_match(*_norm_cached(lens), keys_info)[0] is None
    )

    # Detailed CSV report of every photo that was considered (has develop
    # text + lens metadata) — one row per photo, whatever the outcome, so
    # nothing is a silent skip: arquivo, image_id, lente_catalogo,
    # lente_configurada, tipo_match, preset_xmp, campos_xmp, alterado,
    # verificado, motivo, erro.
    report_path: Optional[str] = None
    if full_report_rows:
        try:
            report_file = catalog_path.with_name(
                catalog_path.stem + "_relatorio_lentes.csv"
            )
            fieldnames = [
                "arquivo", "image_id", "lente_catalogo", "lente_configurada",
                "tipo_match", "preset_xmp", "campos_xmp", "alterado",
                "verificado", "motivo", "erro",
            ]
            with open(report_file, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(full_report_rows)
            report_path = str(report_file)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Não foi possível salvar relatório de lentes: {exc}")

    logger.info(
        "Presets por lente — resumo:\n"
        f"  Total de fotos: {total}\n"
        f"  Preset aplicado (gravado E verificado): {applied}\n"
        f"  Já estavam idênticas: {identical}\n"
        f"  Sem texto de develop: {sem_texto}\n"
        f"  Sem metadado de lente: {sem_metadata}\n"
        f"  Sem preset correspondente: {sem_preset}\n"
        f"  UPDATE não afetou a linha ativa: {update_sem_efeito}\n"
        f"  Verificação pós-escrita falhou (revertido): {verificacao_falhou}\n"
        f"  Erros: {erros}"
    )
    for row in lens_comparison:
        status = "correspondência encontrada" if row["encontrado_no_catalogo"] else "NENHUMA correspondência no catálogo"
        logger.info(f"  Configuração: {row['configurado']}  →  {status} ({row['fotos_correspondentes']} foto(s))")
    if unmatched_catalog_lenses:
        logger.info(
            "  Lentes no catálogo sem preset configurado: "
            + ", ".join(unmatched_catalog_lenses)
        )
    if report_path:
        logger.info(f"  Relatório detalhado de fotos ignoradas: {report_path}")

    return {
        "applied": applied,
        "skipped": skipped,
        "total": total,
        "identical": identical,
        "sem_texto": sem_texto,
        "sem_metadata": sem_metadata,
        "sem_preset": sem_preset,
        "erros": erros,
        "update_sem_efeito": update_sem_efeito,
        "verificacao_falhou": verificacao_falhou,
        "skipped_photos": skipped_photos,
        "full_report_rows": full_report_rows,
        "lens_comparison": lens_comparison,
        "unmatched_catalog_lenses": unmatched_catalog_lenses,
        "report_path": report_path,
    }


def list_known_lenses(con: sqlite3.Connection) -> list[str]:
    """Return distinct non-null lens names stored in the bank, sorted."""
    rows = con.execute(
        "SELECT DISTINCT lens FROM photos WHERE lens IS NOT NULL AND lens != '' ORDER BY lens"
    ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Exterior / interior classification + preset application
# ---------------------------------------------------------------------------

def _sky_fraction_from_thumbnail(img_bytes: bytes) -> float:
    """Estimate the fraction of sky-like pixels in the top third of a thumbnail.

    A pixel is "sky" when the blue channel is clearly dominant, moderately
    bright, and not achromatic (to avoid white walls / ceilings).
    Returns 0.0 when PIL or numpy are unavailable or analysis fails.
    """
    if Image is None or np is None:
        return 0.0
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        arr = np.array(img, dtype=np.float32) / 255.0
        h = arr.shape[0]
        top = arr[:max(1, h // 3), :, :]         # analyse top third only
        r, g, b = top[:, :, 0], top[:, :, 1], top[:, :, 2]
        # Sky heuristic: blue dominant, not too dark, not achromatic
        sky = (
            (b > 0.35)
            & (b > r * 1.15)          # clearly bluer than red
            & (b > g * 0.92)          # bluer than or close to green
            & ((b - np.minimum(r, g)) > 0.08)  # not achromatic (not white/grey)
        )
        return float(np.mean(sky))
    except Exception:
        return 0.0


def score_exterior_photo(
    *,
    gps_latitude: Optional[float] = None,
    iso: Optional[float] = None,
    shutter_speed: Optional[float] = None,     # seconds (0.002 = 1/500 s)
    aperture: Optional[float] = None,           # f-number
    luminance_p50: Optional[float] = None,      # 0-255 (raw from numpy histogram)
    luminance_p90: Optional[float] = None,      # 0-255
    mean_r: Optional[float] = None,             # 0-255
    mean_g: Optional[float] = None,             # 0-255
    mean_b: Optional[float] = None,             # 0-255
    thumbnail_path: Optional[str] = None,
    thumbnail_bytes: Optional[bytes] = None,    # raw bytes — preferred over path
) -> tuple[int, str]:
    """Score a photo for exterior likelihood and return (score, label).

    Scoring rules (max reachable ≈ 15):
      +3  GPS coordinates present
      +2  ISO ≤ 200  (very low ISO = outdoor daylight)
      +1  ISO ≤ 400  (low ISO, could be well-lit interior too)
      +2  shutter ≥ 1/500 s  (very fast = lots of outdoor light)
      +1  shutter ≥ 1/250 s
      +1  aperture ≥ f/5.6   (stopped-down = outdoor DOF)
      +2  luminance_p50 ≥ 140 (bright midtones, 0-255 scale ≈ 55 %)
      +1  luminance_p50 ≥ 100 (moderately bright)
      +1  luminance_p90 ≥ 200 (bright highlights / windows)
      +1  mean_blue > mean_red × 1.08   (cool/blue cast = sky/outdoor light)
      +1  mean_green > mean_red × 1.05  (green cast = foliage)
      +3  sky fraction in top-third thumbnail > 25 %
      +2  sky fraction in top-third thumbnail > 10 %

    Labels:
      score ≥ 6  → 'exterior'
      score ≥ 4  → 'transicao'
      score <  4 → 'interior'

    NOTE: luminance values are stored as 0-255 (raw numpy percentile output).
    The previous 0-1 thresholds were a scale bug — now corrected.
    """
    score = 0

    # --- GPS (strong outdoor signal) ---
    if gps_latitude is not None:
        score += 3

    # --- ISO (outdoor daylight → very low ISO) ---
    if iso is not None:
        if iso <= 200:
            score += 2
        elif iso <= 400:
            score += 1

    # --- Shutter speed (fast = lots of light = outdoor) ---
    if shutter_speed is not None:
        if shutter_speed <= (1.0 / 500.0):
            score += 2
        elif shutter_speed <= (1.0 / 250.0):
            score += 1

    # --- Aperture (stopped-down typical for outdoor wide-angle) ---
    if aperture is not None and aperture >= 5.6:
        score += 1

    # --- Luminance (0-255 scale — previous thresholds were for 0-1, now fixed) ---
    if luminance_p50 is not None:
        if luminance_p50 >= 140:
            score += 2
        elif luminance_p50 >= 100:
            score += 1
    if luminance_p90 is not None and luminance_p90 >= 200:
        score += 1

    # --- Colour cast (ratios are scale-independent) ---
    if mean_r is not None and mean_b is not None and mean_b > mean_r * 1.08:
        score += 1   # blue dominant = sky / daylight colour temperature
    if mean_r is not None and mean_g is not None and mean_g > mean_r * 1.05:
        score += 1   # green dominant = foliage

    # --- Sky detection (use bytes when available, fall back to disk path) ---
    sky_bytes: Optional[bytes] = thumbnail_bytes
    if sky_bytes is None and thumbnail_path:
        try:
            sky_bytes = Path(thumbnail_path).read_bytes()
        except Exception:
            pass
    if sky_bytes:
        try:
            sky = _sky_fraction_from_thumbnail(sky_bytes)
            if sky >= 0.25:
                score += 3   # clear sky in top-third is very strong exterior signal
            elif sky >= 0.10:
                score += 2
        except Exception:
            pass

    if score >= 6:
        label = "exterior"
    elif score >= 4:
        label = "transicao"
    else:
        label = "interior"
    return score, label


def classify_catalog_by_exterior(
    con: sqlite3.Connection,
    catalog_path: Path,
    workdir: Path,
    logger: logging.Logger,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> list[dict]:
    """Classify every photo in catalog_path as exterior / transicao / interior.

    Feature sourcing priority (best → fallback):
      1. Bank lookup by file_path  — photo was already imported; richest data.
      2. Bank lookup by id_global  — same, but matched via Adobe UUID.
      3. Live preview extraction   — load the photo's JPEG preview from the
         catalog's own Previews.lrdata and extract visual features on-the-fly.
         This is the key path for catalogs that have NEVER been fed into the
         bank and makes classification work even for brand-new catalogs.
      4. EXIF-only                 — last resort when no image data is available.

    Each result dict has keys: filename, image_id_local, score, label, in_bank.
    """
    copy_path = open_catalog_copy(catalog_path, workdir)
    previews_dir = find_previews_dir(catalog_path)   # may be None — graceful
    if previews_dir:
        logger.info(f"Previews do catálogo alvo encontrados: {previews_dir}")
    else:
        logger.warning("Previews.lrdata não encontrado — usando apenas EXIF + banco para classificação.")

    try:
        photos = read_catalog_photos(copy_path, logger)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass

    results: list[dict] = []
    from_bank = 0
    from_preview = 0
    exif_only = 0

    for i, photo in enumerate(photos):
        # --- Priority 1 & 2: bank lookup ---
        bank_row = con.execute(
            """SELECT luminance_p50, luminance_p90, mean_r, mean_g, mean_b, thumbnail_path
               FROM photos WHERE file_path = ? LIMIT 1""",
            (photo.file_path,),
        ).fetchone()
        if bank_row is None and photo.id_global:
            bank_row = con.execute(
                """SELECT luminance_p50, luminance_p90, mean_r, mean_g, mean_b, thumbnail_path
                   FROM photos WHERE id_global = ? LIMIT 1""",
                (photo.id_global,),
            ).fetchone()

        lum50 = lum90 = mr = mg = mb = thumb_path = None
        thumb_bytes: Optional[bytes] = None

        if bank_row:
            lum50, lum90, mr, mg, mb, thumb_path = bank_row
            from_bank += 1
        else:
            # --- Priority 3: extract from catalog's own preview ---
            if previews_dir:
                preview_bytes = load_preview_image_bytes(
                    previews_dir, photo.image_id_local, photo.id_global, logger
                )
                if preview_bytes:
                    feats = extract_visual_features(
                        photo.file_path, logger, preview_bytes=preview_bytes
                    )
                    if feats:
                        lum50      = feats["luminance_p50"]
                        lum90      = feats["luminance_p90"]
                        mr         = feats["mean_r"]
                        mg         = feats["mean_g"]
                        mb         = feats["mean_b"]
                        thumb_bytes = feats.get("thumbnail_webp")
                        from_preview += 1
                    else:
                        exif_only += 1
                else:
                    exif_only += 1
            else:
                exif_only += 1

        score, label = score_exterior_photo(
            gps_latitude=photo.gps_latitude,
            iso=photo.iso,
            shutter_speed=photo.shutter_speed,
            aperture=photo.aperture,
            luminance_p50=lum50,
            luminance_p90=lum90,
            mean_r=mr,
            mean_g=mg,
            mean_b=mb,
            thumbnail_path=thumb_path,
            thumbnail_bytes=thumb_bytes,
        )
        results.append({
            "filename":       photo.filename,
            "image_id_local": photo.image_id_local,
            "score":          score,
            "label":          label,
            "in_bank":        bank_row is not None,
        })
        if progress_cb:
            progress_cb(i + 1, len(photos))

    logger.info(
        f"Classificação: {len(results)} foto(s) — "
        f"{from_bank} via banco, {from_preview} via preview direto, {exif_only} só EXIF."
    )
    return results


def apply_xmp_to_exterior_photos(
    con: sqlite3.Connection,
    catalog_path: Path,
    workdir: Path,
    xmp_path: Path,
    logger: logging.Logger,
    score_threshold: int = 6,
    apply_to_transition: bool = False,
    backup: bool = True,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Apply an XMP preset to exterior (and optionally transition) photos.

    Pipeline:
      1. Backup catalog if requested.
      2. Parse the XMP preset.
      3. Classify all photos via EXIF + bank visual features + thumbnail sky.
      4. Edit the catalog in-place, writing preset values to the develop-
         settings text of every photo whose score meets the threshold.

    score_threshold: minimum score to qualify (6 = exterior, 4 = transition).
    apply_to_transition: when True, also includes photos scored 4-5 regardless
                         of score_threshold (effective threshold → min(score_threshold, 4)).

    Returns dict: applied, skipped, total, report_rows (filename, score, label, status).
    """
    if backup:
        bak = catalog_path.with_suffix(".lrcat.bak")
        shutil.copy2(str(catalog_path), str(bak))
        logger.info(f"Backup criado: {bak.name}")

    preset = parse_xmp_preset(xmp_path)
    if not preset:
        raise ValueError(f"Preset XMP vazio ou inválido: {xmp_path.name}")
    logger.info(f"Preset '{xmp_path.stem}': {len(preset)} configuração(ões).")

    logger.info("Classificando fotos…")
    classifications = classify_catalog_by_exterior(con, catalog_path, workdir, logger)
    in_bank = sum(1 for r in classifications if r["in_bank"])
    ext_n   = sum(1 for r in classifications if r["label"] == "exterior")
    tran_n  = sum(1 for r in classifications if r["label"] == "transicao")
    int_n   = sum(1 for r in classifications if r["label"] == "interior")
    logger.info(
        f"{len(classifications)} foto(s) — {in_bank} com features visuais no banco. "
        f"Classificação: {ext_n} exterior, {tran_n} transição, {int_n} interior."
    )

    # Effective score threshold (include transition range when requested)
    effective_min = min(score_threshold, 4) if apply_to_transition else score_threshold
    target_ids = {r["image_id_local"] for r in classifications if r["score"] >= effective_min}
    class_by_id = {r["image_id_local"]: r for r in classifications}

    if not target_ids:
        logger.warning("Nenhuma foto atingiu o limiar — nada a aplicar.")
        return {
            "applied":     0,
            "skipped":     len(classifications),
            "total":       len(classifications),
            "report_rows": [(r["filename"], r["score"], r["label"], "ignorada")
                            for r in classifications],
        }

    cat_con = sqlite3.connect(str(catalog_path))
    try:
        cur = cat_con.cursor()
        all_ids = [row[0] for row in cur.execute(
            "SELECT id_local FROM Adobe_images ORDER BY id_local"
        ).fetchall()]

        applied = 0
        report_rows: list[tuple] = []
        total = len(all_ids)

        for idx, image_id in enumerate(all_ids):
            row = cur.execute(
                "SELECT text FROM Adobe_imageDevelopSettings WHERE image = ?",
                (image_id,),
            ).fetchone()

            cls    = class_by_id.get(image_id, {})
            fname  = cls.get("filename", str(image_id))
            score  = cls.get("score", 0)
            label  = cls.get("label", "interior")

            if not row or not row[0]:
                report_rows.append((fname, score, label, "sem_configuração"))
                if progress_cb:
                    progress_cb(idx + 1, total)
                continue

            if image_id not in target_ids:
                report_rows.append((fname, score, label, "ignorada"))
                if progress_cb:
                    progress_cb(idx + 1, total)
                continue

            # Route through apply_xmp_to_develop_text (same safe, type-aware,
            # insert-or-replace logic used by "Por Lente"): it (a) writes the
            # FULL preset — calibration, HSL/color, split toning, color
            # grading, lens profile, white balance — not just settings that
            # happen to already exist on the photo, and (b) never inserts a
            # malformed/unquoted value for a string or boolean field. The old
            # raw-regex substitution here quoted nothing and inserted nothing
            # missing, which could write invalid Lua (e.g. an unquoted lens
            # profile name) and made Lightroom silently reset every slider on
            # that photo to default — the "fotos zeradas" regression.
            text = apply_xmp_to_develop_text(row[0], preset)

            cur.execute(
                "UPDATE Adobe_imageDevelopSettings SET text = ? WHERE image = ?",
                (text, image_id),
            )
            applied += 1
            report_rows.append((fname, score, label, "aplicada"))

            if progress_cb:
                progress_cb(idx + 1, total)

        cat_con.commit()
    finally:
        cat_con.close()

    logger.info(f"Preset '{xmp_path.stem}' aplicado em {applied}/{total} foto(s).")
    return {
        "applied":     applied,
        "skipped":     total - applied,
        "total":       total,
        "report_rows": report_rows,
    }


# ---------------------------------------------------------------------------
# Direct (in-place) catalog editing
# ---------------------------------------------------------------------------

def edit_catalog_inplace(
    con: sqlite3.Connection,
    catalog_path: Path,
    logger: logging.Logger,
    backup: bool = True,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Apply AI exposure/WB suggestions directly to the original .lrcat.

    Unlike edit_new_catalog (which copies first and moves to an output folder),
    this writes to the original file in place.
    A .lrcat.bak backup is created beforehand when backup=True."""
    logger.info(f"Editando catálogo in place: {catalog_path}")

    if backup:
        bak = catalog_path.with_suffix(".lrcat.bak")
        shutil.copy2(str(catalog_path), str(bak))
        logger.info(f"Backup criado: {bak.name}")

    previews_dir = find_previews_dir(catalog_path)
    if previews_dir:
        logger.info(f"Previews encontrados: {previews_dir}")
    else:
        logger.warning("Previews.lrdata não encontrado ao lado do catálogo.")

    photos = read_catalog_photos(catalog_path, logger)
    logger.info(f"{len(photos)} fotos no catálogo.")

    updates: dict[int, tuple[float, float]] = {}
    wb_updates: dict[int, tuple[float, float]] = {}
    report_rows = []
    features_by_filename: dict = {}
    used_preview = used_original = no_features = 0

    for i, photo in enumerate(photos):
        preview_bytes = load_preview_image_bytes(
            previews_dir, photo.image_id_local, photo.id_global, logger
        )
        features = extract_visual_features(photo.file_path, logger, preview_bytes=preview_bytes)
        features_by_filename[photo.filename] = features
        if features and preview_bytes:
            used_preview += 1
        elif features:
            used_original += 1
        else:
            no_features += 1

        suggestion = suggest_exposure_shadows(
            con, features, photo.preset_name, logger,
            new_lens=photo.lens, new_camera=photo.camera,
        )
        if suggestion is None:
            logger.warning(f"Foto {photo.filename}: sem sugestão (banco vazio?).")
            report_rows.append((photo.filename, None, None, None, None, "sem sugestão"))
        else:
            updates[photo.image_id_local] = (suggestion.exposure, suggestion.shadows)
            if suggestion.temperature is not None and suggestion.tint is not None:
                wb_updates[photo.image_id_local] = (suggestion.temperature, suggestion.tint)
            report_rows.append((
                photo.filename, suggestion.exposure, suggestion.shadows,
                suggestion.temperature, suggestion.tint, suggestion.correction_note,
            ))
        if progress_cb:
            progress_cb(i + 1, len(photos))

    logger.info(
        f"Features: {used_preview} via preview, {used_original} via original, "
        f"{no_features} sem imagem."
    )

    if not updates:
        raise RuntimeError(
            "Nenhuma foto atualizada — banco vazio ou catálogo sem configurações de revelação."
        )

    readback = apply_settings_to_catalog_copy(
        catalog_path, updates, logger, wb_updates=wb_updates
    )

    for image_id, (exp, sha) in updates.items():
        rb = readback.get(image_id, (None, None, None, None))
        got_exp, got_sha = rb[0], rb[1]
        if got_exp is None or abs(got_exp - exp) > 1e-6 or got_sha is None or abs(got_sha - sha) > 1e-6:
            logger.error(
                f"Imagem {image_id}: mismatch ({exp}/{sha} vs {got_exp}/{got_sha})."
            )
        else:
            gt, gti = rb[2], rb[3]
            wb_s = f", WB={gt:.0f}K/{gti:+.0f}" if gt is not None else ""
            logger.info(f"Imagem {image_id}: Exp={got_exp} Sha={got_sha}{wb_s} ✓")

    runs_cur = con.execute(
        "INSERT INTO runs (kind, catalog_path, photos_count, notes) VALUES (?,?,?,?)",
        ("edit_inplace", str(catalog_path), len(updates), ""),
    )
    run_id = runs_cur.lastrowid
    _insert_edits_history(con, run_id, str(catalog_path), photos, report_rows)
    # Snapshot the suggestions so a later "Alimentar banco" (Modo de
    # atualização) reimport of this same file can compute a correction
    # against them — in-place edits previously skipped this entirely, so
    # retroalimentação could never learn from them.
    _save_ai_suggestions(
        con, run_id, str(catalog_path), photos, report_rows, features_by_filename,
        edited_catalog_path=str(catalog_path),
    )
    con.commit()

    return {
        "catalog_path": str(catalog_path),
        "updated_count": len(updates),
        "total_photos": len(photos),
        "report_rows": report_rows,
    }
