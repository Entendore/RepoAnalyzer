#!/usr/bin/env python3
"""
GitHub Repo State Lister — Enhanced Edition
Improvements & new features added to the original app.

NEW FEATURES:
  - Topics/Tags enrichment
  - Contributors count enrichment
  - Language filter & Topic filter (dynamic, auto-populated)
  - Repo Detail Panel (click any row to see full info)
  - Cancel Fetch button
  - Keyboard shortcuts (Ctrl+Enter, Ctrl+Shift+F, Escape)
  - Bulk Annotate (multi-select repos)
  - Topics bar chart, Size distribution chart, Activity timeline chart
  - Topics in reports & markdown export
  - Tab badge showing repo count

IMPROVEMENTS:
  - Multi-column search (name, description, language, owner, notes, topics)
  - GitHub-like language color coding in table cells
  - Human-readable size formatting (KB/MB/GB)
  - Alternating row colors for readability
  - Copy clone commands to clipboard (instead of directly cloning)
  - Open Issues/PRs in browser from context menu
  - Select All / Deselect All for enrichment checkboxes
  - Proper exception handling (no bare except)
  - Enhanced dark theme with focus states & hover effects
  - Multi-select rows (ExtendedSelection)
  - Better row height
  - Grid lines in table
  - Tooltips on menu separators
  - Cache size display & auto-trim
  - Status bar summary after fetch
  - Richer markdown export with all enrichment columns

ROUND 2 IMPROVEMENTS:
  - Collapsible sidebar (toggle button with arrow)
  - Permanent status bar widgets (auth user, repo count, cache size, API limit)
  - Dynamic window title shows fetched owner(s)
  - Duplicate fetch prevention (user == org dedup)
  - Select All Rows in multi-select context menu
  - Copy Selected URLs / Full Names (multi-select bulk copy)
  - Cache auto-trim actually triggered on write
  - Fixed dynamic filter signal duplication (proper disconnect before reconnect)
  - Better status bar auth indicator with color-coded permanent label
"""

import sys
import os
import json
import csv
import sqlite3
import subprocess
import logging
import webbrowser
import hashlib
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import Counter

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QFormLayout, QLabel, QLineEdit, QComboBox, QSpinBox,
    QCheckBox, QPushButton, QTabWidget, QTableView, QTextEdit,
    QFileDialog, QMessageBox, QHeaderView, QStatusBar, QProgressBar,
    QMenu, QDialog, QScrollArea, QSplitter, QShortcut, QAbstractItemView,
    QToolButton, QSizePolicy,
)
from PySide6.QtCore import Qt, QThread, Signal, QObject, QSortFilterProxyModel, QSettings
from PySide6.QtGui import (
    QStandardItemModel, QStandardItem, QColor, QTextCursor,
    QKeySequence, QAction, QIcon,
)

# Optional matplotlib import for Charts
try:
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.figure import Figure
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False


# ============================================
# HELPER UTILITIES
# ============================================
def extract_username(text: str) -> str:
    """Accept 'Entendore', 'https://github.com/Entendore', etc."""
    if not text:
        return ""
    text = text.strip().rstrip("/")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text


def format_size(kb: int) -> str:
    """Format repo size in KB to human-readable string."""
    if kb >= 1024 * 1024:
        return f"{kb / (1024 * 1024):.1f} GB"
    elif kb >= 1024:
        return f"{kb / 1024:.1f} MB"
    else:
        return f"{kb} KB"


def format_duration(date_str: str) -> str:
    """Format an ISO date string as relative duration."""
    try:
        past = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        days = (now - past).days
        if days > 365:
            return f"{days // 365}y ago"
        elif days > 30:
            return f"{days // 30}mo ago"
        else:
            return f"{days}d ago"
    except (ValueError, TypeError):
        return "N/A"


# GitHub-like language colors for common languages
LANGUAGE_COLORS = {
    'Python': '#3572A5', 'JavaScript': '#f1e05a', 'TypeScript': '#3178c6',
    'Java': '#b07219', 'Go': '#00ADD8', 'Rust': '#dea584',
    'C++': '#f34b7d', 'C': '#555555', 'C#': '#178600',
    'Ruby': '#701516', 'PHP': '#4F5D95', 'Swift': '#F05138',
    'Kotlin': '#A97BFF', 'Dart': '#00B4AB', 'Scala': '#c22d40',
    'Shell': '#89e051', 'HTML': '#e34c26', 'CSS': '#563d7c',
    'Lua': '#000080', 'R': '#198CE7', 'Julia': '#a270ba',
    'Perl': '#0298c3', 'Haskell': '#5e5086', 'Elixir': '#6e4a7e',
    'Vue': '#41b883', 'Svelte': '#ff3e00', 'Nix': '#7e7eff',
}


# ============================================================
# CACHING SYSTEM (improved with size limits)
# ============================================================
class RepoCache:
    """Disk-based cache for API responses with auto-trim."""
    MAX_CACHE_SIZE_MB = 100

    def __init__(self, cache_dir=".repo_cache", ttl_hours=24):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        self.ttl = timedelta(hours=ttl_hours)

    def _cache_key(self, owner, is_org, flags_hash):
        raw = f"{owner}_{'org' if is_org else 'user'}_{flags_hash}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _flags_hash(self, flags):
        relevant = {k: v for k, v in sorted(flags.items())}
        return hashlib.md5(str(relevant).encode()).hexdigest()[:8]

    def get(self, owner, is_org, flags):
        fh = self._flags_hash(flags)
        key = self._cache_key(owner, is_org, fh)
        cache_file = self.cache_dir / f"{key}.pkl"
        if not cache_file.exists():
            return None
        mtime = datetime.fromtimestamp(cache_file.stat().st_mtime, tz=timezone.utc)
        if datetime.now(timezone.utc) - mtime > self.ttl:
            cache_file.unlink()
            return None
        try:
            with open(cache_file, 'rb') as f:
                data = pickle.load(f)
            return data.get('data')
        except Exception:
            return None

    def set(self, owner, is_org, flags, data):
        fh = self._flags_hash(flags)
        key = self._cache_key(owner, is_org, fh)
        cache_file = self.cache_dir / f"{key}.pkl"
        try:
            with open(cache_file, 'wb') as f:
                pickle.dump({'owner': owner, 'data': data}, f)
            self.trim_if_needed()  # auto-trim after each write
        except OSError as e:
            logging.warning(f"Cache write failed: {e}")

    def clear(self):
        for f in self.cache_dir.glob("*.pkl"):
            f.unlink()

    def cache_size_mb(self):
        """Return total cache size in MB."""
        return sum(f.stat().st_size for f in self.cache_dir.glob("*.pkl")) / (1024 * 1024)

    def trim_if_needed(self):
        """Remove oldest cache files if total size exceeds limit."""
        total = self.cache_size_mb()
        if total <= self.MAX_CACHE_SIZE_MB:
            return
        files = sorted(self.cache_dir.glob("*.pkl"), key=lambda f: f.stat().st_mtime)
        for f in files:
            if self.cache_size_mb() <= self.MAX_CACHE_SIZE_MB * 0.8:
                break
            f.unlink()


# ============================================================
# ANNOTATION STORE (SQLite — improved with bulk ops)
# ============================================================
class AnnotationStore:
    def __init__(self, db_path=".repo_annotations.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS annotations (
                repo_full_name TEXT PRIMARY KEY,
                note TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.commit()

    def get_note(self, full_name):
        row = self.conn.execute(
            "SELECT note FROM annotations WHERE repo_full_name = ?", (full_name,)
        ).fetchone()
        return row[0] if row else ""

    def get_tags(self, full_name):
        row = self.conn.execute(
            "SELECT tags FROM annotations WHERE repo_full_name = ?", (full_name,)
        ).fetchone()
        return row[0] if row else ""

    def set_note(self, full_name, note, tags=""):
        self.conn.execute("""
            INSERT INTO annotations (repo_full_name, note, tags, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(repo_full_name) DO UPDATE SET
                note = excluded.note, tags = excluded.tags, updated_at = CURRENT_TIMESTAMP
        """, (full_name, note, tags))
        self.conn.commit()

    def bulk_set_note(self, full_names, note, tags=""):
        """Set the same note/tags for multiple repos at once."""
        self.conn.executemany("""
            INSERT INTO annotations (repo_full_name, note, tags, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(repo_full_name) DO UPDATE SET
                note = excluded.note, tags = excluded.tags, updated_at = CURRENT_TIMESTAMP
        """, [(fn, note, tags) for fn in full_names])
        self.conn.commit()

    def get_all(self):
        rows = self.conn.execute(
            "SELECT repo_full_name, note, tags FROM annotations"
        ).fetchall()
        return {r[0]: {"note": r[1], "tags": r[2]} for r in rows}

    def delete(self, full_name):
        self.conn.execute(
            "DELETE FROM annotations WHERE repo_full_name = ?", (full_name,)
        )
        self.conn.commit()


# ============================================================
# PROFILE MANAGER
# ============================================================
class ProfileManager:
    PROFILES_DIR = Path(".repo_profiles")

    def __init__(self):
        self.PROFILES_DIR.mkdir(exist_ok=True)

    def save_profile(self, name, config):
        path = self.PROFILES_DIR / f"{name}.json"
        with open(path, 'w') as f:
            json.dump(config, f, indent=2)

    def load_profile(self, name):
        path = self.PROFILES_DIR / f"{name}.json"
        if not path.exists():
            return None
        with open(path) as f:
            return json.load(f)

    def list_profiles(self):
        return [p.stem for p in self.PROFILES_DIR.glob("*.json")]

    def delete_profile(self, name):
        path = self.PROFILES_DIR / f"{name}.json"
        if path.exists():
            path.unlink()


# ============================================================
# LOGGING SETUP
# ============================================================
class QLogHandler(logging.Handler, QObject):
    log_signal = Signal(str)

    def __init__(self):
        logging.Handler.__init__(self)
        QObject.__init__(self)

    def emit(self, record):
        msg = self.format(record)
        self.log_signal.emit(msg)


logger = logging.getLogger("RepoExplorer")
logger.setLevel(logging.DEBUG)
_qt_handler = QLogHandler()
_qt_handler.setFormatter(
    logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
)
logger.addHandler(_qt_handler)

_file_handler = logging.FileHandler("repo_explorer.log")
_file_handler.setFormatter(
    logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")
)
logger.addHandler(_file_handler)


# ============================================================
# CHART WIDGETS (expanded with new chart types)
# ============================================================
if MATPLOTLIB_AVAILABLE:
    def _dark_fig(figsize=(5, 4)):
        """Create a dark-themed matplotlib figure + axes pair."""
        fig = Figure(figsize=figsize, facecolor='#0d1117')
        ax = fig.add_subplot(111)
        ax.set_facecolor('#161b22')
        for spine in ('top', 'right'):
            ax.spines[spine].set_visible(False)
        for spine in ('bottom', 'left'):
            ax.spines[spine].set_color('#30363d')
        ax.tick_params(colors='#8b949e')
        return fig, ax

    PALETTE = [
        '#238636', '#1f6feb', '#8957e5', '#da3633', '#d29922',
        '#3fb950', '#58a6ff', '#bc8cff', '#f78166', '#7ee787',
    ]

    class PieChartWidget(FigureCanvas):
        def __init__(self, data_dict, title="", parent=None):
            fig, ax = _dark_fig()
            labels = list(data_dict.keys())
            values = list(data_dict.values())
            wedges, texts, autotexts = ax.pie(
                values, labels=labels, autopct='%1.1f%%',
                colors=PALETTE[:len(labels)], startangle=90,
                textprops={'color': '#c9d1d9', 'fontsize': 9},
            )
            for t in autotexts:
                t.set_color('white')
                t.set_fontsize(8)
            ax.set_title(title, color='#c9d1d9', fontsize=12, fontweight='bold')
            fig.tight_layout()
            super().__init__(fig)

    class BarChartWidget(FigureCanvas):
        def __init__(self, data_dict, title="", xlabel="",
                     color='#238636', parent=None):
            fig, ax = _dark_fig()
            items = sorted(data_dict.items(), key=lambda x: x[1], reverse=True)
            labels = [i[0] for i in items]
            values = [i[1] for i in items]
            bars = ax.barh(labels, values, color=color, edgecolor='#30363d')
            ax.set_xlabel(xlabel, color='#8b949e')
            ax.set_title(title, color='#c9d1d9', fontsize=12, fontweight='bold')
            for bar, val in zip(bars, values):
                ax.text(bar.get_width() + 0.3,
                        bar.get_y() + bar.get_height() / 2,
                        str(val), va='center', color='#c9d1d9', fontsize=9)
            fig.tight_layout()
            super().__init__(fig)

    class TimelineChartWidget(FigureCanvas):
        """Bar chart showing repo update activity over recent months."""
        def __init__(self, month_counts, title="Activity Timeline", parent=None):
            fig, ax = _dark_fig(figsize=(6, 3))
            months = list(month_counts.keys())
            counts = list(month_counts.values())
            ax.bar(months, counts, color='#1f6feb', edgecolor='#30363d')
            ax.set_xlabel("Month", color='#8b949e')
            ax.set_ylabel("Repos Updated", color='#8b949e')
            ax.set_title(title, color='#c9d1d9', fontsize=12, fontweight='bold')
            ax.tick_params(axis='x', rotation=45)
            fig.tight_layout()
            super().__init__(fig)

    class SizeDistChartWidget(FigureCanvas):
        """Histogram of repo sizes."""
        def __init__(self, sizes, title="Size Distribution", parent=None):
            fig, ax = _dark_fig(figsize=(6, 3))
            if sizes:
                bins = min(20, max(5, len(sizes) // 3))
                ax.hist(sizes, bins=bins, color='#8957e5', edgecolor='#30363d')
            ax.set_xlabel("Size (KB)", color='#8b949e')
            ax.set_ylabel("Count", color='#8b949e')
            ax.set_title(title, color='#c9d1d9', fontsize=12, fontweight='bold')
            fig.tight_layout()
            super().__init__(fig)


# ============================================================
# DIALOGS (expanded with bulk annotation)
# ============================================================
class AnnotationDialog(QDialog):
    def __init__(self, full_name, current_note, current_tags, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Note: {full_name}")
        self.setMinimumSize(450, 300)
        self.setStyleSheet(parent.styleSheet() if parent else "")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>{full_name}</b>"))

        layout.addWidget(QLabel("Tags (comma-separated):"))
        self.tags_input = QLineEdit(current_tags)
        self.tags_input.setPlaceholderText("e.g. deprecated, needs-review")
        layout.addWidget(self.tags_input)

        layout.addWidget(QLabel("Notes:"))
        self.note_edit = QTextEdit()
        self.note_edit.setPlainText(current_note)
        layout.addWidget(self.note_edit)

        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def get_values(self):
        return self.note_edit.toPlainText(), self.tags_input.text()


class BulkAnnotationDialog(QDialog):
    """Annotate multiple repos at once."""
    def __init__(self, repo_names, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Bulk Annotate ({len(repo_names)} repos)")
        self.setMinimumSize(500, 350)
        self.setStyleSheet(parent.styleSheet() if parent else "")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>Annotating {len(repo_names)} repos</b>"))

        preview = ", ".join(repo_names[:5])
        if len(repo_names) > 5:
            preview += f" ... +{len(repo_names) - 5} more"
        names_label = QLabel(preview)
        names_label.setWordWrap(True)
        names_label.setStyleSheet("color: #8b949e; font-size: 11px;")
        layout.addWidget(names_label)

        layout.addWidget(QLabel("Tags (comma-separated):"))
        self.tags_input = QLineEdit()
        self.tags_input.setPlaceholderText("e.g. needs-review, deprecated")
        layout.addWidget(self.tags_input)

        layout.addWidget(QLabel("Notes:"))
        self.note_edit = QTextEdit()
        self.note_edit.setPlaceholderText(
            "This note will be applied to all selected repos..."
        )
        layout.addWidget(self.note_edit)

        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Apply to All")
        save_btn.setStyleSheet("font-weight: bold;")
        save_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def get_values(self):
        return self.note_edit.toPlainText(), self.tags_input.text()


class ProfileDialog(QDialog):
    def __init__(self, profile_manager, current_config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Profile Manager")
        self.setMinimumWidth(400)
        self.setStyleSheet(parent.styleSheet() if parent else "")
        self.pm = profile_manager
        self.current_config = current_config
        self.selected_profile = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Saved Profiles:"))
        self.profile_list = QComboBox()
        self.refresh_profiles()
        layout.addWidget(self.profile_list)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("New profile name...")
        layout.addWidget(self.name_input)

        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save Current")
        save_btn.clicked.connect(self.save_profile)
        load_btn = QPushButton("Load")
        load_btn.clicked.connect(self.load_profile)
        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(self.delete_profile)
        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(load_btn)
        btn_layout.addWidget(delete_btn)
        layout.addLayout(btn_layout)

        layout.addWidget(QLabel("<b>Built-in Presets:</b>"))
        presets_layout = QHBoxLayout()
        for label, key in [("Security Audit", "security_audit"),
                           ("Spring Cleaning", "spring_cleaning"),
                           ("Quick Overview", "quick_overview")]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda checked, k=key: self.apply_preset(k))
            presets_layout.addWidget(btn)
        layout.addLayout(presets_layout)

    def refresh_profiles(self):
        self.profile_list.clear()
        self.profile_list.addItems(self.pm.list_profiles())

    def save_profile(self):
        name = self.name_input.text().strip()
        if name:
            self.pm.save_profile(name, self.current_config())
            self.refresh_profiles()
            self.profile_list.setCurrentText(name)

    def load_profile(self):
        name = self.profile_list.currentText()
        if name:
            self.selected_profile = self.pm.load_profile(name)
            self.accept()

    def delete_profile(self):
        name = self.profile_list.currentText()
        if name:
            self.pm.delete_profile(name)
            self.refresh_profiles()

    def apply_preset(self, preset_name):
        presets = {
            "security_audit": {
                "vis_filter": "All", "status_filter": "Active", "type_filter": "Source",
                "stars_filter": 0, "stale_days": 90, "chk_health": False,
                "chk_popularity": False, "chk_license": True, "chk_ci": False,
                "chk_unprotected": True, "chk_stale": True, "chk_release": False,
                "chk_topics": False, "chk_contributors": False,
            },
            "spring_cleaning": {
                "vis_filter": "All", "status_filter": "All", "type_filter": "All",
                "stars_filter": 0, "stale_days": 180, "chk_health": True,
                "chk_popularity": True, "chk_license": False, "chk_ci": False,
                "chk_unprotected": False, "chk_stale": True, "chk_release": False,
                "chk_topics": True, "chk_contributors": False,
            },
            "quick_overview": {
                "vis_filter": "All", "status_filter": "Active", "type_filter": "Source",
                "stars_filter": 0, "stale_days": 365, "chk_health": False,
                "chk_popularity": True, "chk_license": False, "chk_ci": False,
                "chk_unprotected": False, "chk_stale": False, "chk_release": True,
                "chk_topics": True, "chk_contributors": True,
            },
        }
        self.selected_profile = presets.get(preset_name)
        if self.selected_profile:
            self.accept()


# ============================================================
# MULTI-COLUMN SEARCH PROXY (improvement)
# ============================================================
class MultiColumnSearchProxy(QSortFilterProxyModel):
    """Filter across multiple columns: name, description, language, owner, notes, topics."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._search_text = ""
        self._search_columns: list[int] = []

    def set_search_columns(self, columns: list[int]):
        self._search_columns = columns

    def setFilterFixedString(self, text: str):
        self._search_text = text.lower()
        super().setFilterFixedString(text)

    def filterAcceptsRow(self, source_row, source_parent):
        if not self._search_text:
            return True
        model = self.sourceModel()
        cols = self._search_columns if self._search_columns else [1, 2, 6]
        for col in cols:
            index = model.index(source_row, col, source_parent)
            text = model.data(index)
            if text and self._search_text in str(text).lower():
                return True
        return False


# ============================================================
# WORKER THREAD (expanded with cancel + new enrichment)
# ============================================================
class GithubWorker(QThread):
    data_ready = Signal(str, list)
    progress = Signal(str, int)
    error = Signal(str)
    finished = Signal()

    def __init__(self, owner, is_org, flags):
        super().__init__()
        self.owner = owner
        self.is_org = is_org
        self.flags = flags
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True

    def run(self):
        try:
            self.progress.emit(f"Fetching repos for {self.owner}...", 0)
            repos = self.fetch_repos()
            if self._cancel_requested:
                self.progress.emit(f"Cancelled fetching {self.owner}", -1)
                return
            if self.flags.get('stale_days'):
                self.mark_stale(repos, self.flags['stale_days'])
            self.enrich_repos(repos)
            if self._cancel_requested:
                self.progress.emit(f"Cancelled enriching {self.owner}", -1)
                return
            self.data_ready.emit(self.owner, repos)
            self.progress.emit(f"Completed fetching {self.owner}", 100)
        except Exception as e:
            logger.error(f"Worker error for {self.owner}: {e}")
            self.error.emit(str(e))
        finally:
            self.finished.emit()

    def run_gh(self, endpoint, silent_errors=False):
        if self._cancel_requested:
            return None
        cmd = ["gh", "api", endpoint]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        except subprocess.CalledProcessError as e:
            if silent_errors and ("HTTP 404" in e.stderr or "HTTP 409" in e.stderr):
                return None
            logger.error(f"API Error: {e.stderr.strip()}")
            return None
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"gh CLI error: {e}")
            return None

    def fetch_repos(self):
        endpoint = (f"orgs/{self.owner}/repos" if self.is_org
                    else f"users/{self.owner}/repos")
        all_repos = []
        page = 1
        while True:
            if self._cancel_requested:
                break
            data = self.run_gh(f"{endpoint}?per_page=100&page={page}")
            if not data:
                break
            all_repos.extend(data)
            if len(data) < 100:
                break
            page += 1
            self.progress.emit(f"Fetched {len(all_repos)} repos...", -1)
        return all_repos

    def enrich_repos(self, repos):
        needs_enrichment = any([
            self.flags.get('license'), self.flags.get('ci'),
            self.flags.get('unprotected'), self.flags.get('health'),
            self.flags.get('release'), self.flags.get('topics'),
            self.flags.get('contributors'),
        ])
        if not needs_enrichment:
            return

        total = len(repos)
        for i, repo in enumerate(repos):
            if self._cancel_requested:
                break
            full_name = repo['full_name']
            self.progress.emit(
                f"Enriching [{i + 1}/{total}]: {full_name}",
                int((i / total) * 100),
            )

            is_empty = (repo.get('size', 0) == 0
                        and repo.get('fork', False) is False)
            repo['default_branch'] = repo.get('default_branch', 'main')

            if self.flags.get('license'):
                res = self.run_gh(f"repos/{full_name}", silent_errors=True)
                if res:
                    lic = res.get('license')
                    repo['license_det'] = (
                        lic.get('spdx_id', 'NONE') if lic else 'NONE'
                    )
                    repo['default_branch'] = res.get(
                        'default_branch', repo['default_branch']
                    )
                else:
                    repo['license_det'] = 'NONE'

            if self.flags.get('ci'):
                res = self.run_gh(
                    f"repos/{full_name}/actions/workflows", silent_errors=True
                )
                repo['has_ci'] = (
                    res.get('total_count', 0) > 0 if res else False
                )

            if self.flags.get('unprotected'):
                res = self.run_gh(f"repos/{full_name}", silent_errors=True)
                default_branch = (
                    res.get('default_branch', 'main') if res else 'main'
                )
                repo['checked_branch'] = default_branch
                repo['default_branch'] = default_branch
                if is_empty:
                    repo['branch_protected'] = False
                else:
                    prot_res = self.run_gh(
                        f"repos/{full_name}/branches/{default_branch}/protection",
                        silent_errors=True,
                    )
                    repo['branch_protected'] = prot_res is not None

            if self.flags.get('health'):
                if is_empty:
                    repo['last_commit'] = ''
                    repo['open_prs'] = 0
                else:
                    commits = self.run_gh(
                        f"repos/{full_name}/commits?per_page=1",
                        silent_errors=True,
                    )
                    repo['last_commit'] = (
                        commits[0].get('commit', {}).get('author', {}).get(
                            'date', ''
                        )
                        if commits and len(commits) > 0 else ''
                    )
                    prs = self.run_gh(
                        f"search/issues?q=repo:{full_name}+type:pr+state:open&per_page=1",
                        silent_errors=True,
                    )
                    repo['open_prs'] = (
                        prs.get('total_count', 0) if prs else 0
                    )

            if self.flags.get('release'):
                if is_empty:
                    repo['latest_release'] = 'None'
                    repo['release_date'] = ''
                else:
                    res = self.run_gh(
                        f"repos/{full_name}/releases/latest",
                        silent_errors=True,
                    )
                    repo['latest_release'] = (
                        res.get('tag_name', 'None') if res else 'None'
                    )
                    repo['release_date'] = (
                        res.get('published_at', '') if res else ''
                    )

            # --- NEW: Topics enrichment ---
            if self.flags.get('topics'):
                res = self.run_gh(
                    f"repos/{full_name}/topics", silent_errors=True
                )
                if res and isinstance(res, dict):
                    repo['topics'] = res.get('names', [])
                else:
                    repo['topics'] = []

            # --- NEW: Contributors count enrichment ---
            if self.flags.get('contributors'):
                res = self.run_gh(
                    f"repos/{full_name}/contributors?per_page=1",
                    silent_errors=True,
                )
                repo['contributors_count'] = (
                    len(res) if res and isinstance(res, list) else 0
                )

    def mark_stale(self, repos, stale_days):
        threshold = timedelta(days=stale_days)
        now = datetime.now(timezone.utc)
        for repo in repos:
            updated_str = repo.get('updated_at', '')
            try:
                updated_at = datetime.fromisoformat(
                    updated_str.replace('Z', '+00:00')
                )
                repo['is_stale'] = (now - updated_at) > threshold
            except (ValueError, TypeError):
                repo['is_stale'] = False


# ============================================================
# REPO DETAIL PANEL (new feature)
# ============================================================
class RepoDetailPanel(QWidget):
    """Shows detailed info about the currently selected repo."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)

        self.title_label = QLabel("Select a repo to view details")
        self.title_label.setStyleSheet(
            "font-weight: bold; font-size: 14px; color: #c9d1d9;"
        )
        layout.addWidget(self.title_label)

        self.detail_text = QTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setMaximumHeight(160)
        self.detail_text.setStyleSheet("font-size: 12px;")
        layout.addWidget(self.detail_text)

    def show_repo(self, repo_data: dict):
        if not repo_data:
            self.title_label.setText("Select a repo to view details")
            self.detail_text.clear()
            return

        name = repo_data.get('full_name', 'unknown')
        self.title_label.setText(name)

        lines = []
        url = repo_data.get('html_url', '')
        lines.append(
            f"<b>URL:</b> <a href=\"{url}\">{url}</a>"
        )
        desc = repo_data.get('description') or '\u2014'
        lines.append(f"<b>Description:</b> {desc}")
        lines.append(
            f"<b>Language:</b> "
            f"{repo_data.get('language') or '\u2014'}"
        )
        lines.append(
            f"<b>Default Branch:</b> "
            f"{repo_data.get('default_branch', 'main')}"
        )
        lines.append(f"<b>Size:</b> {format_size(repo_data.get('size', 0))}")
        lines.append(
            f"<b>Stars:</b> {repo_data.get('stargazers_count', 0)} | "
            f"<b>Forks:</b> {repo_data.get('forks_count', 0)} | "
            f"<b>Issues:</b> {repo_data.get('open_issues_count', 0)}"
        )
        lines.append(
            f"<b>Visibility:</b> "
            f"{'Private' if repo_data.get('private') else 'Public'} | "
            f"<b>Archived:</b> "
            f"{'Yes' if repo_data.get('archived') else 'No'} | "
            f"<b>Fork:</b> "
            f"{'Yes' if repo_data.get('fork') else 'No'}"
        )
        lines.append(
            f"<b>Created:</b> {repo_data.get('created_at', '')[:10]} | "
            f"<b>Updated:</b> {repo_data.get('updated_at', '')[:10]} | "
            f"<b>Pushed:</b> {repo_data.get('pushed_at', '')[:10]}"
        )

        if 'license_det' in repo_data:
            lines.append(f"<b>License:</b> {repo_data['license_det']}")
        if 'has_ci' in repo_data:
            lines.append(
                f"<b>CI (GitHub Actions):</b> "
                f"{'Yes' if repo_data['has_ci'] else 'No'}"
            )
        if 'branch_protected' in repo_data:
            lines.append(
                f"<b>Branch Protection "
                f"({repo_data.get('checked_branch', 'main')}):</b> "
                f"{'Yes' if repo_data['branch_protected'] else 'No'}"
            )
        if 'latest_release' in repo_data:
            rel = repo_data['latest_release']
            rd = repo_data.get('release_date', '')
            extra = f" ({rd[:10]})" if rd else ""
            lines.append(f"<b>Latest Release:</b> {rel}{extra}")
        if 'topics' in repo_data and repo_data['topics']:
            lines.append(
                f"<b>Topics:</b> {', '.join(repo_data['topics'])}"
            )
        if 'last_commit' in repo_data:
            lines.append(
                f"<b>Last Commit:</b> {format_duration(repo_data['last_commit'])}"
            )
        if 'open_prs' in repo_data:
            lines.append(f"<b>Open PRs:</b> {repo_data['open_prs']}")
        if 'is_stale' in repo_data:
            lines.append(
                f"<b>Stale:</b> "
                f"{'Yes' if repo_data['is_stale'] else 'No'}"
            )

        lines.append("<br><b>Clone:</b>")
        lines.append(
            f"&nbsp;&nbsp;SSH: <code>git clone git@github.com:{name}.git</code>"
        )
        lines.append(
            f"&nbsp;&nbsp;HTTPS: <code>git clone {url}.git</code>"
        )

        self.detail_text.setHtml("<br>".join(lines))

    def clear(self):
        self.title_label.setText("Select a repo to view details")
        self.detail_text.clear()


# ============================================================
# MAIN WINDOW
# ============================================================
class RepoExplorer(QMainWindow):
    def __init__(self):
        self.settings = QSettings("RepoExplorer", "Config")
        self.cache = RepoCache()
        self.annotation_store = AnnotationStore()
        self.profile_manager = ProfileManager()

        super().__init__()
        self.setWindowTitle("GitHub Repo State Lister")
        self.resize(1280, 860)

        self.repos_data: dict[str, list] = {}
        self.previous_repos_data: dict[str, list] = {}
        self.workers: list[GithubWorker] = []
        self._all_repos_flat: list[dict] = []
        self._tab_widget: QTabWidget | None = None
        self._sidebar_visible = True

        self.init_ui()
        self.load_settings()
        self.setup_shortcuts()
        self.check_gh_auth()
        self.update_rate_limit()

    # ============================================
    # UI CONSTRUCTION
    # ============================================
    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(4, 4, 4, 4)

        # --- LEFT SIDEBAR (collapsible) ---
        sidebar_container = QWidget()
        sidebar_container.setObjectName("sidebarContainer")
        sb_lay = QHBoxLayout(sidebar_container)
        sb_lay.setContentsMargins(0, 0, 0, 0)
        sb_lay.setSpacing(0)

        sidebar = QWidget()
        sidebar.setMinimumWidth(280)
        sidebar.setMaximumWidth(320)
        self._sidebar = sidebar
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(4, 4, 4, 4)

        # Sidebar toggle button
        self._sidebar_toggle = QToolButton()
        self._sidebar_toggle.setArrowType(Qt.ArrowType.LeftArrow)
        self._sidebar_toggle.setFixedSize(20, 60)
        self._sidebar_toggle.setStyleSheet(
            "QToolButton { border: none; background: #21262d; color: #8b949e; "
            "border-radius: 4px; } "
            "QToolButton:hover { background: #30363d; color: #c9d1d9; }"
        )
        self._sidebar_toggle.clicked.connect(self._toggle_sidebar)
        sb_lay.addWidget(sidebar)
        sb_lay.addWidget(self._sidebar_toggle)

        # Target
        tgt = QGroupBox("Target")
        tgt_lay = QFormLayout()
        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("Auto-detected if empty")
        self.org_input = QLineEdit()
        self.org_input.setPlaceholderText("e.g. my-org")
        tgt_lay.addRow("Username:", self.user_input)
        tgt_lay.addRow("Organization:", self.org_input)
        tgt.setLayout(tgt_lay)

        # Filters
        flt = QGroupBox("Filters")
        flt_lay = QFormLayout()
        self.vis_filter = QComboBox()
        self.vis_filter.addItems(["All", "Public", "Private"])
        self.status_filter = QComboBox()
        self.status_filter.addItems(["All", "Active", "Archived"])
        self.type_filter = QComboBox()
        self.type_filter.addItems(["All", "Source", "Fork"])
        self.language_filter = QComboBox()  # NEW
        self.language_filter.addItem("All")
        self.topic_filter = QComboBox()  # NEW
        self.topic_filter.addItem("All")
        self.stars_filter = QSpinBox()
        self.stars_filter.setRange(0, 1000000)
        self.stale_days = QSpinBox()
        self.stale_days.setRange(1, 3650)
        self.stale_days.setValue(180)

        flt_lay.addRow("Visibility:", self.vis_filter)
        flt_lay.addRow("Status:", self.status_filter)
        flt_lay.addRow("Type:", self.type_filter)
        flt_lay.addRow("Language:", self.language_filter)
        flt_lay.addRow("Topic:", self.topic_filter)
        flt_lay.addRow("Min Stars:", self.stars_filter)
        flt_lay.addRow("Stale Days:", self.stale_days)
        flt.setLayout(flt_lay)

        # Enrichment features
        feat = QGroupBox("Enrichment Features (Slower)")
        feat_lay = QVBoxLayout()
        self.chk_health = QCheckBox("Health (PRs, Last Commit)")
        self.chk_popularity = QCheckBox("Popularity (Stars, Forks)")
        self.chk_license = QCheckBox("License Detection")
        self.chk_ci = QCheckBox("CI Detection (GitHub Actions)")
        self.chk_unprotected = QCheckBox("Branch Protection")
        self.chk_stale = QCheckBox("Highlight Stale Repos")
        self.chk_release = QCheckBox("Latest Release")
        self.chk_topics = QCheckBox("Topics / Tags")  # NEW
        self.chk_contributors = QCheckBox("Contributors Count")  # NEW
        self.chk_use_cache = QCheckBox("Use Cache (24h TTL)")
        self.chk_use_cache.setChecked(True)

        for chk in [self.chk_popularity, self.chk_health, self.chk_license,
                     self.chk_ci, self.chk_unprotected, self.chk_stale,
                     self.chk_release, self.chk_topics, self.chk_contributors,
                     self.chk_use_cache]:
            feat_lay.addWidget(chk)

        # Select All / Deselect All
        chk_btns = QHBoxLayout()
        all_on = QPushButton("All On")
        all_on.setFixedHeight(24)
        all_on.clicked.connect(self._select_all_enrichment)
        all_off = QPushButton("All Off")
        all_off.setFixedHeight(24)
        all_off.clicked.connect(self._deselect_all_enrichment)
        chk_btns.addWidget(all_on)
        chk_btns.addWidget(all_off)
        feat_lay.addLayout(chk_btns)
        feat.setLayout(feat_lay)

        # Fetch + Cancel
        fetch_row = QHBoxLayout()
        self.fetch_btn = QPushButton("Fetch Repos")
        self.fetch_btn.setStyleSheet(
            "font-weight: bold; padding: 10px; "
            "background-color: #238636; color: white;"
        )
        self.fetch_btn.clicked.connect(self.start_fetch)
        self.cancel_btn = QPushButton("Cancel")  # NEW
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setStyleSheet(
            "padding: 10px; background-color: #da3633; color: white;"
        )
        self.cancel_btn.clicked.connect(self.cancel_fetch)
        fetch_row.addWidget(self.fetch_btn)
        fetch_row.addWidget(self.cancel_btn)

        # Bottom buttons
        self.export_csv_btn = QPushButton("Export CSV")
        self.export_csv_btn.clicked.connect(lambda: self.export_data("csv"))
        self.export_json_btn = QPushButton("Export JSON")
        self.export_json_btn.clicked.connect(lambda: self.export_data("json"))
        self.export_md_btn = QPushButton("Export Markdown")
        self.export_md_btn.clicked.connect(lambda: self.export_data("markdown"))
        self.profile_btn = QPushButton("Profiles / Presets")
        self.profile_btn.clicked.connect(self.open_profile_manager)
        self.btn_clear_cache = QPushButton("Clear Cache")
        self.btn_clear_cache.clicked.connect(self.clear_cache)

        sb.addWidget(tgt)
        sb.addWidget(flt)
        sb.addWidget(feat)
        sb.addLayout(fetch_row)
        sb.addStretch()
        sb.addWidget(self.profile_btn)
        sb.addWidget(self.btn_clear_cache)
        sb.addWidget(self.export_csv_btn)
        sb.addWidget(self.export_json_btn)
        sb.addWidget(self.export_md_btn)

        # --- RIGHT PANEL (tab widget) ---
        self._tab_widget = QTabWidget()

        # Search bar
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "Search repos by name, description, language, owner, topics..."
        )
        self.search_input.textChanged.connect(self.apply_search)

        # Table
        table_container = QWidget()
        table_lay = QVBoxLayout(table_container)
        table_lay.setContentsMargins(0, 0, 0, 0)

        self.table_view = QTableView()
        self.model = QStandardItemModel()
        self.proxy_model = MultiColumnSearchProxy()  # IMPROVED
        self.proxy_model.setSourceModel(self.model)
        self.proxy_model.setSortRole(Qt.UserRole)
        self.proxy_model.setFilterCaseSensitivity(
            Qt.CaseSensitivity.CaseInsensitive
        )
        self.table_view.setModel(self.proxy_model)
        self.table_view.setSortingEnabled(True)
        self.table_view.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table_view.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.table_view.customContextMenuRequested.connect(self.show_context_menu)
        self.table_view.doubleClicked.connect(self.open_repo_in_browser)
        self.table_view.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table_view.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection  # multi-select
        )
        self.table_view.setAlternatingRowColors(True)  # IMPROVED
        self.table_view.verticalHeader().setDefaultSectionSize(26)
        self.table_view.clicked.connect(self._on_row_clicked)

        table_lay.addWidget(self.search_input)
        table_lay.addWidget(self.table_view)

        # Detail panel
        self.detail_panel = RepoDetailPanel()
        self.detail_panel.setVisible(False)

        # Splitter: table on top, detail on bottom
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(table_container)
        splitter.addWidget(self.detail_panel)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)

        # Other tabs
        self.reports_text = QTextEdit()
        self.reports_text.setReadOnly(True)

        self.charts_scroll = QScrollArea()
        self.charts_scroll.setWidgetResizable(True)
        self.charts_container = QWidget()
        self.charts_layout = QVBoxLayout(self.charts_container)
        self.charts_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.charts_scroll.setWidget(self.charts_container)
        if not MATPLOTLIB_AVAILABLE:
            self.charts_layout.addWidget(
                QLabel("Install matplotlib to enable charts:\npip install matplotlib")
            )

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        _qt_handler.log_signal.connect(self.append_log)

        self._tab_widget.addTab(splitter, "Repositories")
        self._tab_widget.addTab(self.reports_text, "Reports")
        self._tab_widget.addTab(self.charts_scroll, "Charts")
        self._tab_widget.addTab(self.log_text, "Logs")

        right_panel_lay = QVBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        right_panel_lay.addWidget(self.progress_bar)
        right_panel_lay.addWidget(self._tab_widget)

        main_layout.addWidget(sidebar_container)
        main_layout.addLayout(right_panel_lay, stretch=1)

        # --- STATUS BAR with permanent widgets ---
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._status_auth_label = QLabel("")
        self._status_auth_label.setStyleSheet("color: #3fb950; padding: 0 6px;")
        self._status_repos_label = QLabel("0 repos")
        self._status_repos_label.setStyleSheet("color: #8b949e; padding: 0 6px;")
        self._status_cache_label = QLabel("")
        self._status_cache_label.setStyleSheet("color: #8b949e; padding: 0 6px;")
        self._status_api_label = QLabel("")
        self._status_api_label.setStyleSheet("color: #8b949e; padding: 0 6px;")
        for w in [self._status_auth_label, self._status_repos_label,
                  self._status_cache_label, self._status_api_label]:
            self.status_bar.addPermanentWidget(w)

    # ============================================
    # KEYBOARD SHORTCUTS (new)
    # ============================================
    def setup_shortcuts(self):
        s1 = QShortcut(QKeySequence("Ctrl+Return"), self)
        s1.activated.connect(self.start_fetch)

        s2 = QShortcut(QKeySequence("Ctrl+Shift+F"), self)
        s2.activated.connect(self._focus_search)

        s3 = QShortcut(QKeySequence("Escape"), self)
        s3.activated.connect(self.search_input.clear)

    def _focus_search(self):
        if self._tab_widget:
            self._tab_widget.setCurrentIndex(0)
        self.search_input.setFocus()
        self.search_input.selectAll()

    def _toggle_sidebar(self):
        """Collapse/expand the left sidebar."""
        self._sidebar_visible = not self._sidebar_visible
        self._sidebar.setVisible(self._sidebar_visible)
        self._sidebar_toggle.setArrowType(
            Qt.ArrowType.RightArrow if not self._sidebar_visible
            else Qt.ArrowType.LeftArrow
        )

    # ============================================
    # ENRICHMENT SELECT ALL / DESELECT ALL
    # ============================================
    def _enrichment_checkboxes(self):
        return [
            self.chk_popularity, self.chk_health, self.chk_license,
            self.chk_ci, self.chk_unprotected, self.chk_stale,
            self.chk_release, self.chk_topics, self.chk_contributors,
        ]

    def _select_all_enrichment(self):
        for c in self._enrichment_checkboxes():
            c.setChecked(True)

    def _deselect_all_enrichment(self):
        for c in self._enrichment_checkboxes():
            c.setChecked(False)

    # ============================================
    # SETTINGS
    # ============================================
    def load_settings(self):
        s = self.settings
        self.stale_days.setValue(s.value("stale_days", 180, int))
        self.vis_filter.setCurrentText(s.value("vis_filter", "All"))
        self.status_filter.setCurrentText(s.value("status_filter", "All"))
        self.type_filter.setCurrentText(s.value("type_filter", "All"))
        self.stars_filter.setValue(s.value("stars_filter", 0, int))
        self.chk_stale.setChecked(s.value("chk_stale", False, bool))
        self.chk_license.setChecked(s.value("chk_license", False, bool))
        self.chk_ci.setChecked(s.value("chk_ci", False, bool))
        self.chk_unprotected.setChecked(s.value("chk_unprotected", False, bool))
        self.chk_health.setChecked(s.value("chk_health", False, bool))
        self.chk_popularity.setChecked(s.value("chk_popularity", True, bool))
        self.chk_release.setChecked(s.value("chk_release", False, bool))
        self.chk_use_cache.setChecked(s.value("chk_use_cache", True, bool))
        self.chk_topics.setChecked(s.value("chk_topics", False, bool))
        self.chk_contributors.setChecked(s.value("chk_contributors", False, bool))
        self.user_input.setText(s.value("user_input", ""))
        self.org_input.setText(s.value("org_input", ""))

    def save_settings(self):
        s = self.settings
        s.setValue("stale_days", self.stale_days.value())
        s.setValue("vis_filter", self.vis_filter.currentText())
        s.setValue("status_filter", self.status_filter.currentText())
        s.setValue("type_filter", self.type_filter.currentText())
        s.setValue("stars_filter", self.stars_filter.value())
        for attr in ('chk_stale', 'chk_license', 'chk_ci', 'chk_unprotected',
                      'chk_health', 'chk_popularity', 'chk_release',
                      'chk_use_cache', 'chk_topics', 'chk_contributors'):
            s.setValue(attr, getattr(self, attr).isChecked())
        s.setValue("user_input", self.user_input.text())
        s.setValue("org_input", self.org_input.text())

    def closeEvent(self, event):
        self.save_settings()
        super().closeEvent(event)

    # ============================================
    # UI INTERACTIONS
    # ============================================
    def apply_search(self, text):
        search_cols = [0, 1, 2, 6, 9]  # owner, repo, desc, lang, notes
        headers = [
            (self.model.horizontalHeaderItem(c).text()
             if self.model.horizontalHeaderItem(c) else "")
            for c in range(self.model.columnCount())
        ]
        if "Topics" in headers:
            search_cols.append(headers.index("Topics"))
        self.proxy_model.set_search_columns(search_cols)
        self.proxy_model.setFilterFixedString(text)

    def _on_row_clicked(self, index):
        """Show repo details in detail panel."""
        src = self.proxy_model.mapToSource(index)
        row = src.row()
        if 0 <= row < len(self._all_repos_flat):
            self.detail_panel.setVisible(True)
            self.detail_panel.show_repo(self._all_repos_flat[row])
        else:
            self.detail_panel.clear()

    def open_repo_in_browser(self, index):
        row = index.row()
        owner = self.proxy_model.data(self.proxy_model.index(row, 0))
        repo = self.proxy_model.data(self.proxy_model.index(row, 1))
        if owner and repo:
            webbrowser.open(f"https://github.com/{owner}/{repo}")

    def show_context_menu(self, pos):
        indexes = self.table_view.selectionModel().selectedRows()
        if not indexes:
            return

        is_single = len(indexes) == 1
        menu = QMenu()

        if is_single:
            menu.addAction("Open in Browser")
            menu.addAction("Copy URL")
            menu.addAction("Copy Full Name")
            menu.addSeparator()
            menu.addAction("Copy Clone Cmd (SSH)")
            menu.addAction("Copy Clone Cmd (HTTPS)")
            menu.addSeparator()
            menu.addAction("Open Issues in Browser")
            menu.addAction("Open PRs in Browser")
            menu.addSeparator()
            menu.addAction("Add Note / Tags")
        else:
            menu.addAction("Select All Rows")
            menu.addSeparator()
            menu.addAction("Copy Selected URLs")
            menu.addAction("Copy Selected Full Names")
            menu.addSeparator()
            menu.addAction(f"Bulk Annotate ({len(indexes)} repos)")

        action = menu.exec(self.table_view.viewport().mapToGlobal(pos))
        if not action:
            return

        row = indexes[0].row()
        owner = self.proxy_model.data(self.proxy_model.index(row, 0))
        repo = self.proxy_model.data(self.proxy_model.index(row, 1))
        full_name = f"{owner}/{repo}"
        text = action.text()

        if is_single:
            if text == "Open in Browser":
                webbrowser.open(f"https://github.com/{full_name}")
            elif text == "Copy URL":
                QApplication.clipboard().setText(f"https://github.com/{full_name}")
                self.status_bar.showMessage("URL copied!", 2000)
            elif text == "Copy Full Name":
                QApplication.clipboard().setText(full_name)
                self.status_bar.showMessage("Name copied!", 2000)
            elif text == "Copy Clone Cmd (SSH)":
                QApplication.clipboard().setText(
                    f"git clone git@github.com:{full_name}.git"
                )
                self.status_bar.showMessage("SSH clone command copied!", 2000)
            elif text == "Copy Clone Cmd (HTTPS)":
                QApplication.clipboard().setText(
                    f"git clone https://github.com/{full_name}.git"
                )
                self.status_bar.showMessage("HTTPS clone command copied!", 2000)
            elif text == "Open Issues in Browser":
                webbrowser.open(f"https://github.com/{full_name}/issues")
            elif text == "Open PRs in Browser":
                webbrowser.open(f"https://github.com/{full_name}/pulls")
            elif text == "Add Note / Tags":
                self._open_annotation_dialog(full_name)

        if text.startswith("Bulk Annotate"):
            self._open_bulk_annotation_dialog(indexes)

        if text == "Select All Rows":
            self.table_view.selectAll()

        if text == "Copy Selected URLs":
            urls = []
            for idx in indexes:
                r = idx.row()
                o = self.proxy_model.data(self.proxy_model.index(r, 0))
                rp = self.proxy_model.data(self.proxy_model.index(r, 1))
                urls.append(f"https://github.com/{o}/{rp}")
            QApplication.clipboard().setText("\n".join(urls))
            self.status_bar.showMessage(
                f"Copied {len(urls)} URLs", 2000
            )

        if text == "Copy Selected Full Names":
            names = []
            for idx in indexes:
                r = idx.row()
                o = self.proxy_model.data(self.proxy_model.index(r, 0))
                rp = self.proxy_model.data(self.proxy_model.index(r, 1))
                names.append(f"{o}/{rp}")
            QApplication.clipboard().setText("\n".join(names))
            self.status_bar.showMessage(
                f"Copied {len(names)} names", 2000
            )

    def _open_annotation_dialog(self, full_name):
        note = self.annotation_store.get_note(full_name)
        tags = self.annotation_store.get_tags(full_name)
        dlg = AnnotationDialog(full_name, note, tags, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            n, t = dlg.get_values()
            self.annotation_store.set_note(full_name, n, t)
            self.refresh_table()

    def _open_bulk_annotation_dialog(self, indexes):
        names = []
        for idx in indexes:
            r = idx.row()
            o = self.proxy_model.data(self.proxy_model.index(r, 0))
            rp = self.proxy_model.data(self.proxy_model.index(r, 1))
            names.append(f"{o}/{rp}")
        dlg = BulkAnnotationDialog(names, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            note, tags = dlg.get_values()
            self.annotation_store.bulk_set_note(names, note, tags)
            self.refresh_table()
            self.status_bar.showMessage(
                f"Annotated {len(names)} repos", 3000
            )

    def open_profile_manager(self):
        dlg = ProfileDialog(self.profile_manager, self.get_current_config, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_profile:
            self.apply_config(dlg.selected_profile)
            self.status_bar.showMessage("Profile loaded", 3000)

    def clear_cache(self):
        self.cache.clear()
        self.status_bar.showMessage("Cache cleared", 3000)

    # ============================================
    # CORE LOGIC
    # ============================================
    def check_gh_auth(self):
        try:
            result = subprocess.run(
                ["gh", "auth", "status"], capture_output=True, text=True
            )
            if "Logged in" in result.stderr:
                for line in result.stderr.splitlines():
                    if "Logged in to github.com account" in line:
                        uname = line.split()[-1].strip("()")
                        self.user_input.setPlaceholderText(uname)
                        self.status_bar.showMessage(
                            f"Authenticated as {uname}", 5000
                        )
                        self._status_auth_label.setText(f"  {uname}  ")
                        self._status_auth_label.setStyleSheet(
                            "color: #3fb950; padding: 0 6px; font-weight: bold;"
                        )
                        return
            self.status_bar.showMessage(
                "Not authenticated. Run 'gh auth login'", 10000
            )
            self._status_auth_label.setText("  NOT AUTHENTICATED  ")
            self._status_auth_label.setStyleSheet(
                "color: #f85149; padding: 0 6px; font-weight: bold;"
            )
        except FileNotFoundError:
            QMessageBox.critical(
                self, "Error", "'gh' CLI not found. Please install it."
            )
            self._status_auth_label.setText("  gh CLI MISSING  ")
            self._status_auth_label.setStyleSheet(
                "color: #f85149; padding: 0 6px; font-weight: bold;"
            )

    def update_rate_limit(self):
        try:
            result = subprocess.run(
                ["gh", "api", "rate_limit", "-q", ".resources.core.remaining"],
                capture_output=True, text=True, check=True,
            )
            remaining = result.stdout.strip()
            self._status_api_label.setText(f"API: {remaining}")
            self.status_bar.showMessage(
                f"API Calls Remaining: {remaining}"
            )
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            self._status_api_label.setText("API: ?")

    def get_current_config(self):
        return {
            "vis_filter": self.vis_filter.currentText(),
            "status_filter": self.status_filter.currentText(),
            "type_filter": self.type_filter.currentText(),
            "stars_filter": self.stars_filter.value(),
            "stale_days": self.stale_days.value(),
            "chk_health": self.chk_health.isChecked(),
            "chk_popularity": self.chk_popularity.isChecked(),
            "chk_license": self.chk_license.isChecked(),
            "chk_ci": self.chk_ci.isChecked(),
            "chk_unprotected": self.chk_unprotected.isChecked(),
            "chk_stale": self.chk_stale.isChecked(),
            "chk_release": self.chk_release.isChecked(),
            "chk_topics": self.chk_topics.isChecked(),
            "chk_contributors": self.chk_contributors.isChecked(),
        }

    def apply_config(self, cfg):
        self.vis_filter.setCurrentText(cfg.get("vis_filter", "All"))
        self.status_filter.setCurrentText(cfg.get("status_filter", "All"))
        self.type_filter.setCurrentText(cfg.get("type_filter", "All"))
        self.stars_filter.setValue(cfg.get("stars_filter", 0))
        self.stale_days.setValue(cfg.get("stale_days", 180))
        for key, attr in [
            ("chk_health", "chk_health"), ("chk_popularity", "chk_popularity"),
            ("chk_license", "chk_license"), ("chk_ci", "chk_ci"),
            ("chk_unprotected", "chk_unprotected"), ("chk_stale", "chk_stale"),
            ("chk_release", "chk_release"), ("chk_topics", "chk_topics"),
            ("chk_contributors", "chk_contributors"),
        ]:
            getattr(self, attr).setChecked(cfg.get(key, False))

    # ============================================
    # FETCH + CANCEL
    # ============================================
    def start_fetch(self):
        self.fetch_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.previous_repos_data = dict(self.repos_data)
        self.repos_data.clear()
        self.model.clear()
        self.reports_text.clear()
        self.detail_panel.clear()

        flags = {
            'health': self.chk_health.isChecked(),
            'popularity': self.chk_popularity.isChecked(),
            'license': self.chk_license.isChecked(),
            'ci': self.chk_ci.isChecked(),
            'unprotected': self.chk_unprotected.isChecked(),
            'stale': self.chk_stale.isChecked(),
            'stale_days': self.stale_days.value(),
            'release': self.chk_release.isChecked(),
            'topics': self.chk_topics.isChecked(),
            'contributors': self.chk_contributors.isChecked(),
        }

        targets = []
        user_raw = self.user_input.text() or self.user_input.placeholderText()
        if user_raw:
            user = extract_username(user_raw)
            if user:
                targets.append((user, False))
        org_raw = self.org_input.text()
        if org_raw:
            org = extract_username(org_raw)
            if org:
                # Avoid duplicate fetch if user and org resolve to same name
                if not any(o == org and not is_o for o, is_o in targets):
                    targets.append((org, True))

        if not targets:
            QMessageBox.warning(
                self, "Warning", "Please enter a Username or Organization."
            )
            self.fetch_btn.setEnabled(True)
            self.cancel_btn.setEnabled(False)
            self.progress_bar.setVisible(False)
            return

        for owner, is_org in targets:
            if self.chk_use_cache.isChecked():
                cached = self.cache.get(owner, is_org, flags)
                if cached:
                    logger.info(f"Loaded {owner} from cache")
                    self.process_data(owner, cached)
                    self.update_progress(f"Loaded {owner} from cache", 100)
                    continue
            worker = GithubWorker(owner, is_org, flags)
            worker.data_ready.connect(self.process_data)
            worker.progress.connect(self.update_progress)
            worker.error.connect(self.show_error)
            worker.finished.connect(self.worker_finished)
            self.workers.append(worker)
            worker.start()

        if not self.workers:
            self.worker_finished()

    def cancel_fetch(self):
        for w in self.workers:
            w.cancel()
        self.status_bar.showMessage("Cancelling...", 3000)

    def worker_finished(self):
        self.workers = [w for w in self.workers if w.isRunning()]
        if not self.workers:
            self.fetch_btn.setEnabled(True)
            self.cancel_btn.setEnabled(False)
            self.progress_bar.setVisible(False)
            self.update_rate_limit()
            self._populate_dynamic_filters()
            self.generate_reports()
            self.generate_charts()
            total = sum(len(r) for r in self.repos_data.values())
            owners = ", ".join(self.repos_data.keys())
            cache_mb = self.cache.cache_size_mb()

            # Update permanent status bar widgets
            self._status_repos_label.setText(f"{total} repos")
            self._status_cache_label.setText(f"Cache: {cache_mb:.1f} MB")

            # Update window title with owner
            if owners:
                self.setWindowTitle(f"GitHub Repo State Lister - {owners}")

            self.status_bar.showMessage(
                f"Fetched {total} repos from {owners} | "
                f"Cache: {cache_mb:.1f} MB",
                10000,
            )

    def update_progress(self, msg, pct):
        self.status_bar.showMessage(msg)
        if pct >= 0:
            self.progress_bar.setValue(pct)

    def show_error(self, msg):
        logger.error(msg)
        QMessageBox.critical(self, "Error", msg)

    def append_log(self, msg):
        self.log_text.append(msg)
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.log_text.setTextCursor(cursor)

    # ============================================
    # DYNAMIC FILTER POPULATION (new)
    # ============================================
    def _populate_dynamic_filters(self):
        languages = set()
        topics = set()
        for repos in self.repos_data.values():
            for r in repos:
                lang = r.get('language')
                if lang:
                    languages.add(lang)
                for t in r.get('topics', []):
                    topics.add(t)

        # --- Language filter ---
        try:
            self.language_filter.currentTextChanged.disconnect(self.refresh_table)
        except RuntimeError:
            pass
        cur_lang = self.language_filter.currentText()
        self.language_filter.blockSignals(True)
        self.language_filter.clear()
        self.language_filter.addItem("All")
        for lang in sorted(languages):
            self.language_filter.addItem(lang)
        idx = self.language_filter.findText(cur_lang)
        if idx >= 0:
            self.language_filter.setCurrentIndex(idx)
        self.language_filter.blockSignals(False)
        self.language_filter.currentTextChanged.connect(self.refresh_table)

        # --- Topic filter ---
        try:
            self.topic_filter.currentTextChanged.disconnect(self.refresh_table)
        except RuntimeError:
            pass
        cur_topic = self.topic_filter.currentText()
        self.topic_filter.blockSignals(True)
        self.topic_filter.clear()
        self.topic_filter.addItem("All")
        for topic in sorted(topics):
            self.topic_filter.addItem(topic)
        idx = self.topic_filter.findText(cur_topic)
        if idx >= 0:
            self.topic_filter.setCurrentIndex(idx)
        self.topic_filter.blockSignals(False)
        self.topic_filter.currentTextChanged.connect(self.refresh_table)

    # ============================================
    # DATA PROCESSING & TABLE
    # ============================================
    def process_data(self, owner, repos):
        if self.chk_use_cache.isChecked():
            self.cache.set(owner, False, {}, repos)
        self.repos_data[owner] = repos
        self.refresh_table()

    def refresh_table(self):
        self.model.clear()
        self._all_repos_flat = []

        headers = ["Owner", "Repo", "Description", "Visibility", "Status",
                    "Type", "Language", "Size", "Updated", "Notes"]
        if self.chk_popularity.isChecked():
            headers.extend(["Stars", "Forks"])
        if self.chk_health.isChecked():
            headers.extend(["Issues", "PRs", "Last Commit"])
        if self.chk_license.isChecked():
            headers.append("License")
        if self.chk_ci.isChecked():
            headers.append("CI")
        if self.chk_unprotected.isChecked():
            headers.append("Protected")
        if self.chk_stale.isChecked():
            headers.append("Stale")
        if self.chk_release.isChecked():
            headers.extend(["Release", "Release Date"])
        if self.chk_topics.isChecked():
            headers.append("Topics")
        if self.chk_contributors.isChecked():
            headers.append("Contributors")

        self.model.setHorizontalHeaderLabels(headers)

        lang_f = self.language_filter.currentText().lower()
        topic_f = self.topic_filter.currentText().lower()
        vis_f = self.vis_filter.currentText().lower()
        status_f = self.status_filter.currentText().lower()
        type_f = self.type_filter.currentText().lower()
        stars_f = self.stars_filter.value()

        all_annotations = self.annotation_store.get_all()

        for owner, repos in self.repos_data.items():
            for r in repos:
                is_private = r.get('private', False)
                vis = "private" if is_private else "public"
                if vis_f != "all" and vis != vis_f:
                    continue

                is_archived = r.get('archived', False)
                status = "archived" if is_archived else "active"
                if status_f != "all" and status != status_f:
                    continue

                is_fork = r.get('fork', False)
                rtype = "fork" if is_fork else "source"
                if type_f != "all" and rtype != type_f:
                    continue

                if r.get('stargazers_count', 0) < stars_f:
                    continue

                lang = r.get('language') or ''
                if lang_f != "all" and lang.lower() != lang_f:
                    continue

                repo_topics = r.get('topics', [])
                if (topic_f != "all"
                        and topic_f not in [t.lower() for t in repo_topics]):
                    continue

                self._all_repos_flat.append(r)

                row_items = []

                def add_item(text, sort_val=None, color=None, tooltip=None):
                    item = QStandardItem(str(text))
                    if sort_val is not None:
                        item.setData(sort_val, Qt.UserRole)
                    if color:
                        item.setForeground(color)
                    if tooltip:
                        item.setToolTip(str(tooltip))
                    row_items.append(item)

                add_item(owner, owner)
                add_item(r.get('name', ''), r.get('name', ''),
                         tooltip=f"https://github.com/{r.get('full_name', '')}")

                desc = r.get('description') or '\u2014'
                add_item(desc, desc, tooltip=desc)

                vis_color = QColor("#f85149") if is_private else QColor("#3fb950")
                add_item(vis.capitalize(), is_private, vis_color)

                stat_color = (QColor("#d29922") if is_archived
                              else QColor("#3fb950"))
                add_item(status.capitalize(), is_archived, stat_color)
                add_item(rtype.capitalize(), is_fork)

                # Language with GitHub-like color
                lang_text = lang or '\u2014'
                if lang and lang in LANGUAGE_COLORS:
                    try:
                        add_item(lang_text, lang, QColor(LANGUAGE_COLORS[lang]))
                    except Exception:
                        add_item(lang_text, lang)
                else:
                    add_item(lang_text, lang)

                # Human-readable size
                size_kb = r.get('size', 0)
                add_item(format_size(size_kb), size_kb)
                add_item(r.get('updated_at', '').split('T')[0],
                         r.get('updated_at', ''))

                full_name = r.get('full_name', '')
                ann = all_annotations.get(full_name, {})
                note_text = ann.get('note', '')
                note_tags = ann.get('tags', '')
                parts = []
                if note_tags:
                    parts.append(f"[{note_tags}]")
                if note_text:
                    parts.append(note_text[:20] + ('...' if len(note_text) > 20 else ''))
                note_display = " ".join(parts) if parts else '\u2014'
                tt_parts = []
                if note_tags:
                    tt_parts.append(f"Tags: {note_tags}")
                if note_text:
                    tt_parts.append(f"Note: {note_text}")
                add_item(note_display, note_text,
                         tooltip=" | ".join(tt_parts) if tt_parts else "")

                if self.chk_popularity.isChecked():
                    add_item(str(r.get('stargazers_count', 0)),
                             r.get('stargazers_count', 0))
                    add_item(str(r.get('forks_count', 0)),
                             r.get('forks_count', 0))

                if self.chk_health.isChecked():
                    add_item(str(r.get('open_issues_count', 0)),
                             r.get('open_issues_count', 0))
                    add_item(str(r.get('open_prs', 0)),
                             r.get('open_prs', 0))
                    lc = r.get('last_commit', '')
                    add_item(format_duration(lc) if lc else "N/A", lc)

                if self.chk_license.isChecked():
                    add_item(r.get('license_det', 'NONE'),
                             r.get('license_det', 'NONE'))

                if self.chk_ci.isChecked():
                    has_ci = r.get('has_ci', False)
                    add_item("Yes" if has_ci else "No", 1 if has_ci else 0,
                             QColor("#3fb950") if has_ci else QColor("#f85149"))

                if self.chk_unprotected.isChecked():
                    is_prot = r.get('branch_protected', False)
                    add_item("Yes" if is_prot else "No", 1 if is_prot else 0,
                             QColor("#3fb950") if is_prot else QColor("#d29922"))

                if self.chk_stale.isChecked():
                    is_stale = r.get('is_stale', False)
                    add_item("Stale" if is_stale else "Fresh",
                             1 if is_stale else 0,
                             QColor("#bc8cff") if is_stale else None)

                if self.chk_release.isChecked():
                    add_item(r.get('latest_release', 'None'),
                             r.get('latest_release', 'None'))
                    rd = r.get('release_date', '')
                    add_item(rd.split('T')[0] if rd else '\u2014', rd)

                if self.chk_topics.isChecked():
                    topics_text = (", ".join(repo_topics)
                                   if repo_topics else '\u2014')
                    add_item(topics_text, topics_text,
                             tooltip="\n".join(repo_topics) if repo_topics else "")

                if self.chk_contributors.isChecked():
                    add_item(str(r.get('contributors_count', 0)),
                             r.get('contributors_count', 0))

                self.model.appendRow(row_items)

        # Tab badge
        total = self.model.rowCount()
        if self._tab_widget:
            self._tab_widget.setTabText(0, f"Repositories ({total})")

    # ============================================
    # CHARTS (expanded)
    # ============================================
    def generate_charts(self):
        if not MATPLOTLIB_AVAILABLE:
            return
        while self.charts_layout.count():
            item = self.charts_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self.repos_data:
            return

        for owner, repos in self.repos_data.items():
            # Language pie
            langs: dict[str, int] = {}
            for r in repos:
                l = r.get('language') or 'None'
                langs[l] = langs.get(l, 0) + 1
            if len(langs) > 8:
                s = sorted(langs.items(), key=lambda x: x[1], reverse=True)
                langs = dict(s[:8])
                langs['Other'] = sum(v for _, v in s[8:])
            self.charts_layout.addWidget(
                QLabel(f"<h3>{owner} - Languages</h3>")
            )
            self.charts_layout.addWidget(
                PieChartWidget(langs, f"Languages - {owner}")
            )

            # Visibility pie
            vis_data = {
                'Public': sum(1 for r in repos if not r.get('private')),
                'Private': sum(1 for r in repos if r.get('private')),
            }
            self.charts_layout.addWidget(
                PieChartWidget(vis_data, f"Visibility - {owner}")
            )

            # Stars bar
            if self.chk_popularity.isChecked():
                star_data = {
                    r.get('name', ''): r.get('stargazers_count', 0)
                    for r in sorted(
                        repos, key=lambda x: x.get('stargazers_count', 0),
                        reverse=True,
                    )[:10]
                }
                if star_data:
                    self.charts_layout.addWidget(
                        BarChartWidget(
                            star_data, f"Top 10 by Stars - {owner}",
                            "Stars", color='#d29922',
                        )
                    )

            # NEW: Topics bar chart
            if self.chk_topics.isChecked():
                tc = Counter()
                for r in repos:
                    for t in r.get('topics', []):
                        tc[t] += 1
                if tc:
                    self.charts_layout.addWidget(
                        BarChartWidget(
                            dict(tc.most_common(10)),
                            f"Top Topics - {owner}",
                            "Repos", color='#8957e5',
                        )
                    )

            # NEW: Size distribution
            sizes = [r.get('size', 0) for r in repos if r.get('size', 0) > 0]
            if sizes:
                self.charts_layout.addWidget(
                    SizeDistChartWidget(sizes, f"Size Distribution - {owner}")
                )

            # NEW: Activity timeline
            mc: Counter = Counter()
            for r in repos:
                u = r.get('updated_at', '')
                if u:
                    try:
                        dt = datetime.fromisoformat(u.replace('Z', '+00:00'))
                        mc[dt.strftime("%Y-%m")] += 1
                    except (ValueError, TypeError):
                        pass
            if mc:
                sm = dict(sorted(mc.items()))
                if len(sm) > 12:
                    sm = dict(list(sm.items())[-12:])
                self.charts_layout.addWidget(
                    TimelineChartWidget(sm, f"Update Activity - {owner}")
                )

    # ============================================
    # REPORTS (expanded with topics)
    # ============================================
    def generate_reports(self):
        report = ""

        if self.previous_repos_data:
            for owner in self.repos_data:
                if owner in self.previous_repos_data:
                    diff = self.compute_diff(
                        self.previous_repos_data[owner],
                        self.repos_data[owner],
                    )
                    if diff['added'] or diff['removed'] or diff['changed']:
                        report += "<h2>Changes Since Last Fetch</h2>"
                        report += (
                            f"<p>"
                            f"<span style='color:#3fb950'>"
                            f"+{len(diff['added'])} new</span> | "
                            f"<span style='color:#f85149'>"
                            f"-{len(diff['removed'])} removed</span> | "
                            f"<span style='color:#d29922'>"
                            f"~{len(diff['changed'])} changed</span></p>"
                        )
                        if diff['added']:
                            report += "<h3 style='color:#3fb950'>New Repos</h3><ul>"
                            for n in diff['added']:
                                report += f"<li>{n}</li>"
                            report += "</ul>"
                        if diff['removed']:
                            report += "<h3 style='color:#f85149'>Removed Repos</h3><ul>"
                            for n in diff['removed']:
                                report += f"<li>{n}</li>"
                            report += "</ul>"
                        if diff['changed']:
                            report += "<h3 style='color:#d29922'>Changed</h3><ul>"
                            for item in diff['changed'][:20]:
                                report += f"<li><b>{item['repo']}</b><ul>"
                                for f_name, v in item['changes'].items():
                                    report += (
                                        f"<li>{f_name}: {v['from']} "
                                        f"\u2192 {v['to']}</li>"
                                    )
                                report += "</ul></li>"
                            report += "</ul>"

        for owner, repos in self.repos_data.items():
            report += f"<h2>Summary for {owner}</h2>"
            total = len(repos)
            public = sum(1 for r in repos if not r.get('private'))
            active = sum(1 for r in repos if not r.get('archived'))
            forks = sum(1 for r in repos if r.get('fork'))
            report += (
                f"<b>Total:</b> {total} | "
                f"<span style='color:#3fb950'>Public: {public}</span> | "
                f"<span style='color:#f85149'>"
                f"Private: {total - public}</span><br>"
            )
            report += (
                f"<span style='color:#3fb950'>Active: {active}</span> | "
                f"<span style='color:#d29922'>"
                f"Archived: {total - active}</span> | "
                f"Forks: {forks}<br>"
            )
            total_stars = sum(r.get('stargazers_count', 0) for r in repos)
            total_forks = sum(r.get('forks_count', 0) for r in repos)
            report += (
                f"<b>Total Stars:</b> {total_stars} | "
                f"<b>Total Forks:</b> {total_forks}<br><hr>"
            )

            langs: dict[str, int] = {}
            for r in repos:
                l = r.get('language', 'None') or 'None'
                langs[l] = langs.get(l, 0) + 1
            report += "<h3>Languages</h3><ul>"
            for k, v in sorted(langs.items(), key=lambda x: x[1], reverse=True):
                report += f"<li>{k}: {v}</li>"
            report += "</ul>"

            # NEW: Topics statistics
            if self.chk_topics.isChecked():
                tc = Counter()
                for r in repos:
                    for t in r.get('topics', []):
                        tc[t] += 1
                if tc:
                    report += "<h3>Topics</h3><ul>"
                    for topic, count in tc.most_common(20):
                        report += f"<li>{topic}: {count} repos</li>"
                    report += "</ul>"

            if self.chk_stale.isChecked():
                stale = [r for r in repos if r.get('is_stale')]
                report += (
                    f"<h3 style='color:#bc8cff'>"
                    f"Stale Repos (&gt;{self.stale_days.value()} days)</h3><ul>"
                )
                if stale:
                    for r in stale:
                        report += (
                            f"<li>{r.get('full_name')} "
                            f"(Last: {r.get('updated_at', '')[:10]})</li>"
                        )
                else:
                    report += "<li>No stale repos!</li>"
                report += "</ul>"

            if self.chk_unprotected.isChecked():
                unprot = [
                    r for r in repos
                    if not r.get('branch_protected') and not r.get('archived')
                ]
                report += (
                    "<h3 style='color:#f85149'>"
                    "Unprotected Default Branches</h3><ul>"
                )
                if unprot:
                    for r in unprot:
                        report += (
                            f"<li>{r.get('full_name')} "
                            f"({r.get('checked_branch', 'main')})</li>"
                        )
                else:
                    report += "<li>All protected!</li>"
                report += "</ul>"

        self.reports_text.setHtml(report)

    def compute_diff(self, old_repos, new_repos):
        old_map = {r['full_name']: r for r in old_repos}
        new_map = {r['full_name']: r for r in new_repos}
        old_names = set(old_map)
        new_names = set(new_map)
        changed = []
        for name in old_names & new_names:
            old_r, new_r = old_map[name], new_map[name]
            changes = {}
            for field in ('stargazers_count', 'forks_count',
                          'open_issues_count', 'private', 'archived',
                          'language', 'size', 'updated_at'):
                if old_r.get(field) != new_r.get(field):
                    changes[field] = {
                        'from': old_r.get(field),
                        'to': new_r.get(field),
                    }
            if changes:
                changed.append({'repo': name, 'changes': changes})
        return {
            'added': sorted(new_names - old_names),
            'removed': sorted(old_names - new_names),
            'changed': changed,
        }

    # ============================================
    # EXPORTS (improved markdown)
    # ============================================
    def export_data(self, fmt):
        if not self.repos_data:
            QMessageBox.warning(self, "No Data", "Fetch repositories first.")
            return

        if fmt == "csv":
            path, _ = QFileDialog.getSaveFileName(
                self, "Save CSV", "repos.csv", "CSV Files (*.csv)"
            )
            if path:
                with open(path, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        self.model.horizontalHeaderItem(c).text()
                        for c in range(self.model.columnCount())
                    ])
                    for r in range(self.proxy_model.rowCount()):
                        writer.writerow([
                            str(self.proxy_model.data(
                                self.proxy_model.index(r, c)
                            ))
                            for c in range(self.model.columnCount())
                        ])
                QMessageBox.information(self, "Exported", f"Saved to {path}")

        elif fmt == "json":
            path, _ = QFileDialog.getSaveFileName(
                self, "Save JSON", "repos.json", "JSON Files (*.json)"
            )
            if path:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(self.repos_data, f, indent=2, ensure_ascii=False)
                QMessageBox.information(self, "Exported", f"Saved to {path}")

        elif fmt == "markdown":
            path, _ = QFileDialog.getSaveFileName(
                self, "Save Markdown", "repos.md", "Markdown (*.md)"
            )
            if path:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write("# Repository Report\n\n")
                    for owner, repos in self.repos_data.items():
                        f.write(f"## {owner}\n\n")
                        hdrs = ["Repo", "Description", "Visibility",
                                "Language", "Size", "Stars", "Forks",
                                "Updated"]
                        if self.chk_topics.isChecked():
                            hdrs.append("Topics")
                        if self.chk_license.isChecked():
                            hdrs.append("License")
                        if self.chk_ci.isChecked():
                            hdrs.append("CI")
                        if self.chk_release.isChecked():
                            hdrs.append("Release")
                        f.write("| " + " | ".join(hdrs) + " |\n")
                        f.write("| " + " | ".join(["---"] * len(hdrs)) + " |\n")
                        for r in repos:
                            vis = "Private" if r.get('private') else "Public"
                            desc = (r.get('description') or '').replace('|', '\\|')
                            row = [
                                f"[{r['name']}]({r['html_url']})",
                                desc, vis,
                                r.get('language') or '\u2014',
                                format_size(r.get('size', 0)),
                                str(r.get('stargazers_count', 0)),
                                str(r.get('forks_count', 0)),
                                r.get('updated_at', '')[:10],
                            ]
                            if self.chk_topics.isChecked():
                                t = ", ".join(r.get('topics', []))
                                row.append(t if t else '\u2014')
                            if self.chk_license.isChecked():
                                row.append(r.get('license_det', 'NONE'))
                            if self.chk_ci.isChecked():
                                row.append("Yes" if r.get('has_ci') else "No")
                            if self.chk_release.isChecked():
                                row.append(r.get('latest_release', 'None'))
                            f.write("| " + " | ".join(row) + " |\n")
                        f.write("\n")
                QMessageBox.information(self, "Exported", f"Saved to {path}")


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    app = QApplication(sys.argv)

    app.setStyleSheet("""
        QMainWindow { background-color: #0d1117; color: #c9d1d9; }
        QWidget { background-color: #0d1117; color: #c9d1d9; }
        QGroupBox {
            border: 1px solid #30363d; border-radius: 6px;
            margin-top: 1em; padding-top: 10px; font-weight: bold;
        }
        QGroupBox::title { subcontrol-origin: margin; padding: 0 3px; }
        QLineEdit, QComboBox, QSpinBox {
            background-color: #161b22; border: 1px solid #30363d;
            border-radius: 4px; padding: 4px; color: #c9d1d9;
        }
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
            border: 1px solid #1f6feb;
        }
        QPushButton {
            background-color: #21262d; border: 1px solid #30363d;
            border-radius: 6px; padding: 5px; color: #c9d1d9;
        }
        QPushButton:hover { background-color: #30363d; }
        QPushButton:pressed { background-color: #1f6feb; }
        QPushButton:disabled { color: #484f58; border-color: #21262d; }
        QTableView {
            background-color: #161b22; alternate-background-color: #0d1117;
            border: none; selection-background-color: #1f6feb;
            gridline-color: #21262d;
        }
        QTableView::item { padding: 2px 4px; }
        QHeaderView::section {
            background-color: #161b22; color: #c9d1d9;
            border: 1px solid #30363d; padding: 4px; font-weight: bold;
        }
        QHeaderView::section:hover { background-color: #21262d; }
        QTextEdit {
            background-color: #161b22; border: 1px solid #30363d; color: #c9d1d9;
        }
        QTabWidget::pane { border: 1px solid #30363d; }
        QTabBar::tab {
            background-color: #161b22; color: #8b949e; padding: 8px;
            border: 1px solid #30363d; border-bottom: none;
            border-top-left-radius: 4px; border-top-right-radius: 4px;
        }
        QTabBar::tab:selected { background-color: #0d1117; color: #c9d1d9; }
        QTabBar::tab:hover { color: #c9d1d9; }
        QProgressBar {
            border: 1px solid #30363d; border-radius: 4px;
            text-align: center; background-color: #161b22; color: #c9d1d9;
        }
        QProgressBar::chunk { background-color: #238636; border-radius: 3px; }
        QStatusBar { background-color: #161b22; color: #8b949e; }
        QMenu {
            background-color: #161b22; color: #c9d1d9;
            border: 1px solid #30363d;
        }
        QMenu::item:selected { background-color: #1f6feb; }
        QMenu::separator { height: 1px; background: #30363d; margin: 4px 8px; }
        QScrollBar:vertical {
            border: none; background: #0d1117; width: 10px; margin: 0px;
        }
        QScrollBar::handle:vertical {
            background: #30363d; min-height: 20px; border-radius: 5px;
        }
        QScrollBar::handle:vertical:hover { background: #484f58; }
        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical { border: none; background: none; }
        QDialog { background-color: #0d1117; color: #c9d1d9; }
        QSplitter::handle { background-color: #30363d; }
        QScrollArea { border: none; }
        QToolTip {
            background-color: #161b22; color: #c9d1d9;
            border: 1px solid #30363d; padding: 4px;
        }
    """)

    window = RepoExplorer()
    window.show()
    sys.exit(app.exec())