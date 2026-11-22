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

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QFormLayout, QLabel, QLineEdit, QComboBox, QSpinBox,
    QCheckBox, QPushButton, QTabWidget, QTableView, QTextEdit,
    QFileDialog, QMessageBox, QHeaderView, QStatusBar, QProgressBar,
    QMenu, QDialog, QScrollArea
)
from PySide6.QtCore import Qt, QThread, Signal, QObject, QSortFilterProxyModel, QSettings
from PySide6.QtGui import QStandardItemModel, QStandardItem, QColor, QTextCursor

# Optional matplotlib import for Charts
try:
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.figure import Figure
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False


# ============================================
# HELPER
# ============================================
def extract_username(text: str) -> str:
    """Accept 'Entendore', 'https://github.com/Entendore', etc."""
    if not text:
        return ""
    text = text.strip().rstrip("/")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text


# ============================================================
# CACHING SYSTEM
# ============================================================
class RepoCache:
    """Simple disk-based cache for API responses."""
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
        except Exception as e:
            logging.warning(f"Cache write failed: {e}")

    def clear(self):
        for f in self.cache_dir.glob("*.pkl"):
            f.unlink()


# ============================================================
# ANNOTATION STORE (SQLite)
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
        row = self.conn.execute("SELECT note FROM annotations WHERE repo_full_name = ?", (full_name,)).fetchone()
        return row[0] if row else ""

    def get_tags(self, full_name):
        row = self.conn.execute("SELECT tags FROM annotations WHERE repo_full_name = ?", (full_name,)).fetchone()
        return row[0] if row else ""

    def set_note(self, full_name, note, tags=""):
        self.conn.execute("""
            INSERT INTO annotations (repo_full_name, note, tags, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(repo_full_name) DO UPDATE SET
                note = excluded.note, tags = excluded.tags, updated_at = CURRENT_TIMESTAMP
        """, (full_name, note, tags))
        self.conn.commit()

    def get_all(self):
        rows = self.conn.execute("SELECT repo_full_name, note, tags FROM annotations").fetchall()
        return {r[0]: {"note": r[1], "tags": r[2]} for r in rows}

    def delete(self, full_name):
        self.conn.execute("DELETE FROM annotations WHERE repo_full_name = ?", (full_name,))
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
handler = QLogHandler()
handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(handler)

file_handler = logging.FileHandler("repo_explorer.log")
file_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
logger.addHandler(file_handler)


# ============================================================
# CHART WIDGETS (Matplotlib)
# ============================================================
if MATPLOTLIB_AVAILABLE:
    class PieChartWidget(FigureCanvas):
        def __init__(self, data_dict, title="", parent=None):
            fig = Figure(figsize=(5, 4), facecolor='#0d1117')
            ax = fig.add_subplot(111)
            labels = list(data_dict.keys())
            values = list(data_dict.values())
            colors = ['#238636', '#1f6feb', '#8957e5', '#da3633', '#d29922', '#3fb950', '#58a6ff', '#bc8cff']
            wedges, texts, autotexts = ax.pie(values, labels=labels, autopct='%1.1f%%', colors=colors[:len(labels)], startangle=90, textprops={'color': '#c9d1d9', 'fontsize': 9})
            for t in autotexts: 
                t.set_color('white')
                t.set_fontsize(8)
            ax.set_title(title, color='#c9d1d9', fontsize=12, fontweight='bold')
            fig.tight_layout()
            super().__init__(fig)

    class BarChartWidget(FigureCanvas):
        def __init__(self, data_dict, title="", xlabel="", parent=None):
            fig = Figure(figsize=(5, 4), facecolor='#0d1117')
            ax = fig.add_subplot(111)
            ax.set_facecolor('#161b22')
            labels = list(data_dict.keys())
            values = list(data_dict.values())
            bars = ax.barh(labels, values, color='#238636', edgecolor='#30363d')
            ax.set_xlabel(xlabel, color='#8b949e')
            ax.set_title(title, color='#c9d1d9', fontsize=12, fontweight='bold')
            ax.tick_params(colors='#8b949e')
            ax.spines['bottom'].set_color('#30363d')
            ax.spines['left'].set_color('#30363d')
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            for bar, val in zip(bars, values):
                ax.text(bar.get_width() + 0.3, bar.get_y() + bar.get_height()/2, str(val), va='center', color='#c9d1d9', fontsize=9)
            fig.tight_layout()
            super().__init__(fig)


# ============================================================
# DIALOGS
# ============================================================
class AnnotationDialog(QDialog):
    def __init__(self, full_name, current_note, current_tags, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"📝 Note: {full_name}")
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
        save_btn = QPushButton("💾 Save")
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
        save_btn = QPushButton("💾 Save Current")
        save_btn.clicked.connect(self.save_profile)
        load_btn = QPushButton("📂 Load")
        load_btn.clicked.connect(self.load_profile)
        delete_btn = QPushButton("🗑️ Delete")
        delete_btn.clicked.connect(self.delete_profile)
        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(load_btn)
        btn_layout.addWidget(delete_btn)
        layout.addLayout(btn_layout)
        
        layout.addWidget(QLabel("<b>Built-in Presets:</b>"))
        presets_layout = QHBoxLayout()
        sec_audit = QPushButton("🔒 Security Audit")
        sec_audit.clicked.connect(lambda: self.apply_preset("security_audit"))
        spring_clean = QPushButton("🧹 Spring Cleaning")
        spring_clean.clicked.connect(lambda: self.apply_preset("spring_cleaning"))
        overview = QPushButton("📋 Quick Overview")
        overview.clicked.connect(lambda: self.apply_preset("quick_overview"))
        presets_layout.addWidget(sec_audit)
        presets_layout.addWidget(spring_clean)
        presets_layout.addWidget(overview)
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
                "stars_filter": 0, "stale_days": 90, "chk_health": False, "chk_popularity": False, 
                "chk_license": True, "chk_ci": False, "chk_unprotected": True, "chk_stale": True, "chk_release": False
            },
            "spring_cleaning": {
                "vis_filter": "All", "status_filter": "All", "type_filter": "All", 
                "stars_filter": 0, "stale_days": 180, "chk_health": True, "chk_popularity": True, 
                "chk_license": False, "chk_ci": False, "chk_unprotected": False, "chk_stale": True, "chk_release": False
            },
            "quick_overview": {
                "vis_filter": "All", "status_filter": "Active", "type_filter": "Source", 
                "stars_filter": 0, "stale_days": 365, "chk_health": False, "chk_popularity": True, 
                "chk_license": False, "chk_ci": False, "chk_unprotected": False, "chk_stale": False, "chk_release": True
            }
        }
        self.selected_profile = presets.get(preset_name)
        if self.selected_profile:
            self.accept()


# ============================================================
# WORKER THREAD
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

    def run(self):
        try:
            self.progress.emit(f"Fetching repos for {self.owner}...", 0)
            repos = self.fetch_repos()
            if self.flags.get('stale_days'):
                self.mark_stale(repos, self.flags['stale_days'])
            self.enrich_repos(repos)
            self.data_ready.emit(self.owner, repos)
            self.progress.emit(f"Completed fetching {self.owner}", 100)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self.finished.emit()

    def run_gh(self, endpoint, silent_errors=False):
        cmd = ["gh", "api", endpoint]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        except subprocess.CalledProcessError as e:
            # Silently handle 404 (not found) and 409 (empty repo)
            if silent_errors and ("HTTP 404" in e.stderr or "HTTP 409" in e.stderr):
                return None
            logger.error(f"API Error: {e.stderr.strip()}")
            return None

    def fetch_repos(self):
        endpoint = f"orgs/{self.owner}/repos" if self.is_org else f"users/{self.owner}/repos"
        all_repos = []
        page = 1
        while True:
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
            self.flags.get('unprotected'), self.flags.get('health'), self.flags.get('release')
        ])
        if not needs_enrichment:
            return

        total = len(repos)
        for i, repo in enumerate(repos):
            full_name = repo['full_name']
            self.progress.emit(f"Enriching [{i + 1}/{total}]: {full_name}", int((i / total) * 100))

            # Detect empty repos early — they will 409 on many endpoints
            is_empty = repo.get('size', 0) == 0 and repo.get('fork', False) is False

            if self.flags.get('license'):
                res = self.run_gh(f"repos/{full_name}", silent_errors=True)
                if res:
                    lic = res.get('license')
                    repo['license_det'] = lic.get('spdx_id', 'NONE') if lic else 'NONE'
                else:
                    repo['license_det'] = 'NONE'

            if self.flags.get('ci'):
                res = self.run_gh(f"repos/{full_name}/actions/workflows", silent_errors=True)
                repo['has_ci'] = res.get('total_count', 0) > 0 if res else False

            if self.flags.get('unprotected'):
                res = self.run_gh(f"repos/{full_name}", silent_errors=True)
                default_branch = res.get('default_branch', 'main') if res else 'main'
                repo['checked_branch'] = default_branch
                # Empty repos have no branches, so protection check will 409
                if is_empty:
                    repo['branch_protected'] = False
                else:
                    prot_res = self.run_gh(
                        f"repos/{full_name}/branches/{default_branch}/protection",
                        silent_errors=True  # Now handles both 404 and 409
                    )
                    repo['branch_protected'] = prot_res is not None

            if self.flags.get('health'):
                # Empty repos have no commits — will 409
                if is_empty:
                    repo['last_commit'] = ''
                    repo['open_prs'] = 0
                else:
                    commits = self.run_gh(f"repos/{full_name}/commits?per_page=1", silent_errors=True)
                    repo['last_commit'] = (
                        commits[0].get('commit', {}).get('author', {}).get('date', '')
                        if commits and len(commits) > 0 else ''
                    )
                    prs = self.run_gh(
                        f"search/issues?q=repo:{full_name}+type:pr+state:open&per_page=1",
                        silent_errors=True
                    )
                    repo['open_prs'] = prs.get('total_count', 0) if prs else 0

            if self.flags.get('release'):
                # Empty repos have no releases — will 409
                if is_empty:
                    repo['latest_release'] = 'None'
                    repo['release_date'] = ''
                else:
                    res = self.run_gh(f"repos/{full_name}/releases/latest", silent_errors=True)
                    repo['latest_release'] = res.get('tag_name', 'None') if res else 'None'
                    repo['release_date'] = res.get('published_at', '') if res else ''

    def mark_stale(self, repos, stale_days):
        threshold = timedelta(days=stale_days)
        now = datetime.now(timezone.utc)
        for repo in repos:
            updated_str = repo.get('updated_at', '')
            try:
                updated_at = datetime.fromisoformat(updated_str.replace('Z', '+00:00'))
                repo['is_stale'] = (now - updated_at) > threshold
            except:
                repo['is_stale'] = False


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
        self.setWindowTitle("📊 GitHub Repo State Lister")
        self.resize(1200, 800)

        self.repos_data = {}
        self.previous_repos_data = {}
        self.workers = []

        self.init_ui()
        self.load_settings()
        self.check_gh_auth()
        self.update_rate_limit()

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # ============================================
        # LEFT SIDEBAR
        # ============================================
        sidebar = QWidget()
        sidebar.setMaximumWidth(320)
        sidebar_layout = QVBoxLayout(sidebar)

        target_group = QGroupBox("Target")
        target_layout = QFormLayout()
        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("Auto-detected if empty")
        self.org_input = QLineEdit()
        target_layout.addRow("Username:", self.user_input)
        target_layout.addRow("Organization:", self.org_input)
        target_group.setLayout(target_layout)

        filter_group = QGroupBox("Filters")
        filter_layout = QFormLayout()
        self.vis_filter = QComboBox()
        self.vis_filter.addItems(["All", "Public", "Private"])
        self.status_filter = QComboBox()
        self.status_filter.addItems(["All", "Active", "Archived"])
        self.type_filter = QComboBox()
        self.type_filter.addItems(["All", "Source", "Fork"])
        self.stars_filter = QSpinBox()
        self.stars_filter.setRange(0, 1000000)
        self.stale_days = QSpinBox()
        self.stale_days.setRange(1, 3650)
        self.stale_days.setValue(180)
        
        filter_layout.addRow("Visibility:", self.vis_filter)
        filter_layout.addRow("Status:", self.status_filter)
        filter_layout.addRow("Type:", self.type_filter)
        filter_layout.addRow("Min Stars:", self.stars_filter)
        filter_layout.addRow("Stale Days:", self.stale_days)
        filter_group.setLayout(filter_layout)

        feat_group = QGroupBox("Enrichment Features (Slower)")
        feat_layout = QVBoxLayout()
        self.chk_health = QCheckBox("Health (PRs, Last Commit)")
        self.chk_popularity = QCheckBox("Popularity (Stars, Forks)")
        self.chk_license = QCheckBox("License Detection")
        self.chk_ci = QCheckBox("CI Detection (GitHub Actions)")
        self.chk_unprotected = QCheckBox("Branch Protection")
        self.chk_stale = QCheckBox("Highlight Stale Repos")
        self.chk_release = QCheckBox("Latest Release")
        self.chk_use_cache = QCheckBox("Use Cache (24h TTL)")
        self.chk_use_cache.setChecked(True)
        
        feat_layout.addWidget(self.chk_popularity)
        feat_layout.addWidget(self.chk_health)
        feat_layout.addWidget(self.chk_license)
        feat_layout.addWidget(self.chk_ci)
        feat_layout.addWidget(self.chk_unprotected)
        feat_layout.addWidget(self.chk_stale)
        feat_layout.addWidget(self.chk_release)
        feat_layout.addWidget(self.chk_use_cache)
        feat_group.setLayout(feat_layout)

        self.fetch_btn = QPushButton("🚀 Fetch Repos")
        self.fetch_btn.setStyleSheet("font-weight: bold; padding: 10px; background-color: #238636; color: white;")
        self.fetch_btn.clicked.connect(self.start_fetch)

        self.export_csv_btn = QPushButton("📁 Export CSV")
        self.export_csv_btn.clicked.connect(lambda: self.export_data("csv"))
        self.export_json_btn = QPushButton("📄 Export JSON")
        self.export_json_btn.clicked.connect(lambda: self.export_data("json"))
        self.export_md_btn = QPushButton("📝 Export Markdown")
        self.export_md_btn.clicked.connect(lambda: self.export_data("markdown"))
        self.profile_btn = QPushButton("⚙️ Profiles / Presets")
        self.profile_btn.clicked.connect(self.open_profile_manager)
        self.btn_clear_cache = QPushButton("🗑️ Clear Cache")
        self.btn_clear_cache.clicked.connect(self.clear_cache)

        sidebar_layout.addWidget(target_group)
        sidebar_layout.addWidget(filter_group)
        sidebar_layout.addWidget(feat_group)
        sidebar_layout.addWidget(self.fetch_btn)
        sidebar_layout.addStretch()
        sidebar_layout.addWidget(self.profile_btn)
        sidebar_layout.addWidget(self.btn_clear_cache)
        sidebar_layout.addWidget(self.export_csv_btn)
        sidebar_layout.addWidget(self.export_json_btn)
        sidebar_layout.addWidget(self.export_md_btn)

        # ============================================
        # RIGHT SIDE
        # ============================================
        right_panel = QTabWidget()

        # Search bar
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 Search repos by name or description...")
        self.search_input.textChanged.connect(self.apply_search)

        # Table Tab
        table_container = QWidget()
        table_layout = QVBoxLayout(table_container)
        table_layout.setContentsMargins(0, 0, 0, 0)
        
        self.table_view = QTableView()
        self.model = QStandardItemModel()
        self.proxy_model = QSortFilterProxyModel()
        self.proxy_model.setSourceModel(self.model)
        self.proxy_model.setSortRole(Qt.UserRole)
        self.proxy_model.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.table_view.setModel(self.proxy_model)
        self.table_view.setSortingEnabled(True)
        self.table_view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(self.show_context_menu)
        self.table_view.doubleClicked.connect(self.open_repo_in_browser)
        
        table_layout.addWidget(self.search_input)
        table_layout.addWidget(self.table_view)

        # Reports Tab
        self.reports_text = QTextEdit()
        self.reports_text.setReadOnly(True)

        # Charts Tab
        self.charts_scroll = QScrollArea()
        self.charts_scroll.setWidgetResizable(True)
        self.charts_container = QWidget()
        self.charts_layout = QVBoxLayout(self.charts_container)
        self.charts_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.charts_scroll.setWidget(self.charts_container)
        if not MATPLOTLIB_AVAILABLE:
            self.charts_layout.addWidget(QLabel("📊 Install matplotlib to enable charts:\npip install matplotlib"))

        # Log Tab
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        handler.log_signal.connect(self.append_log)

        right_panel.addTab(table_container, "📊 Repositories")
        right_panel.addTab(self.reports_text, "📈 Reports")
        right_panel.addTab(self.charts_scroll, "📊 Charts")
        right_panel.addTab(self.log_text, "📜 Logs")

        main_layout.addWidget(sidebar)
        main_panel = QVBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        main_panel.addWidget(self.progress_bar)
        main_panel.addWidget(right_panel)
        main_layout.addLayout(main_panel, stretch=1)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

    # ============================================
    # SETTINGS PERSISTENCE
    # ============================================
    def load_settings(self):
        self.stale_days.setValue(self.settings.value("stale_days", 180, int))
        self.vis_filter.setCurrentText(self.settings.value("vis_filter", "All"))
        self.status_filter.setCurrentText(self.settings.value("status_filter", "All"))
        self.type_filter.setCurrentText(self.settings.value("type_filter", "All"))
        self.stars_filter.setValue(self.settings.value("stars_filter", 0, int))
        self.chk_stale.setChecked(self.settings.value("chk_stale", False, bool))
        self.chk_license.setChecked(self.settings.value("chk_license", False, bool))
        self.chk_ci.setChecked(self.settings.value("chk_ci", False, bool))
        self.chk_unprotected.setChecked(self.settings.value("chk_unprotected", False, bool))
        self.chk_health.setChecked(self.settings.value("chk_health", False, bool))
        self.chk_popularity.setChecked(self.settings.value("chk_popularity", True, bool))
        self.chk_release.setChecked(self.settings.value("chk_release", False, bool))
        self.chk_use_cache.setChecked(self.settings.value("chk_use_cache", True, bool))
        self.user_input.setText(self.settings.value("user_input", ""))
        self.org_input.setText(self.settings.value("org_input", ""))

    def save_settings(self):
        self.settings.setValue("stale_days", self.stale_days.value())
        self.settings.setValue("vis_filter", self.vis_filter.currentText())
        self.settings.setValue("status_filter", self.status_filter.currentText())
        self.settings.setValue("type_filter", self.type_filter.currentText())
        self.settings.setValue("stars_filter", self.stars_filter.value())
        self.settings.setValue("chk_stale", self.chk_stale.isChecked())
        self.settings.setValue("chk_license", self.chk_license.isChecked())
        self.settings.setValue("chk_ci", self.chk_ci.isChecked())
        self.settings.setValue("chk_unprotected", self.chk_unprotected.isChecked())
        self.settings.setValue("chk_health", self.chk_health.isChecked())
        self.settings.setValue("chk_popularity", self.chk_popularity.isChecked())
        self.settings.setValue("chk_release", self.chk_release.isChecked())
        self.settings.setValue("chk_use_cache", self.chk_use_cache.isChecked())
        self.settings.setValue("user_input", self.user_input.text())
        self.settings.setValue("org_input", self.org_input.text())

    def closeEvent(self, event):
        self.save_settings()
        super().closeEvent(event)

    # ============================================
    # UI INTERACTIONS
    # ============================================
    def apply_search(self, text):
        self.proxy_model.setFilterFixedString(text)
        self.proxy_model.setFilterKeyColumn(2)  # Description column

    def open_repo_in_browser(self, index):
        row = index.row()
        owner = self.proxy_model.data(self.proxy_model.index(row, 0))
        repo = self.proxy_model.data(self.proxy_model.index(row, 1))
        if owner and repo:
            webbrowser.open(f"https://github.com/{owner}/{repo}")

    def show_context_menu(self, pos):
        index = self.table_view.indexAt(pos)
        if not index.isValid():
            return
        
        menu = QMenu()
        open_browser = menu.addAction("🌐 Open in Browser")
        copy_url = menu.addAction("📋 Copy URL")
        copy_name = menu.addAction("📋 Copy Full Name")
        menu.addSeparator()
        clone_ssh = menu.addAction("📦 Clone (SSH)")
        clone_https = menu.addAction("📦 Clone (HTTPS)")
        menu.addSeparator()
        annotate_action = menu.addAction("📝 Add Note / Tags")
        
        action = menu.exec(self.table_view.viewport().mapToGlobal(pos))
        row = index.row()
        owner = self.proxy_model.data(self.proxy_model.index(row, 0))
        repo = self.proxy_model.data(self.proxy_model.index(row, 1))
        full_name = f"{owner}/{repo}"

        if action == open_browser:
            webbrowser.open(f"https://github.com/{full_name}")
        elif action == copy_url:
            QApplication.clipboard().setText(f"https://github.com/{full_name}")
            self.status_bar.showMessage("URL copied!", 2000)
        elif action == copy_name:
            QApplication.clipboard().setText(full_name)
            self.status_bar.showMessage("Name copied!", 2000)
        elif action == clone_ssh:
            subprocess.Popen(["git", "clone", f"git@github.com:{full_name}.git"])
            self.status_bar.showMessage(f"Cloning {full_name} (SSH)...", 3000)
        elif action == clone_https:
            subprocess.Popen(["git", "clone", f"https://github.com/{full_name}.git"])
            self.status_bar.showMessage(f"Cloning {full_name} (HTTPS)...", 3000)
        elif action == annotate_action:
            self.open_annotation_dialog(full_name)

    def open_annotation_dialog(self, full_name):
        existing_note = self.annotation_store.get_note(full_name)
        existing_tags = self.annotation_store.get_tags(full_name)
        dialog = AnnotationDialog(full_name, existing_note, existing_tags, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            note, tags = dialog.get_values()
            self.annotation_store.set_note(full_name, note, tags)
            self.refresh_table()

    def open_profile_manager(self):
        dialog = ProfileDialog(self.profile_manager, self.get_current_config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_profile:
            self.apply_config(dialog.selected_profile)
            self.status_bar.showMessage("Profile loaded", 3000)

    def clear_cache(self):
        self.cache.clear()
        self.status_bar.showMessage("Cache cleared", 3000)

    # ============================================
    # CORE LOGIC
    # ============================================
    def check_gh_auth(self):
        try:
            result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
            if "Logged in" in result.stderr:
                for line in result.stderr.splitlines():
                    if "Logged in to github.com account" in line:
                        uname = line.split()[-1].strip("()")
                        self.user_input.setPlaceholderText(uname)
                        self.status_bar.showMessage(f"✅ Authenticated as {uname}", 5000)
                        return
            self.status_bar.showMessage("⚠️ Not authenticated. Run 'gh auth login'", 10000)
        except FileNotFoundError:
            QMessageBox.critical(self, "Error", "'gh' CLI not found. Please install it.")

    def update_rate_limit(self):
        try:
            result = subprocess.run(["gh", "api", "rate_limit", "-q", ".resources.core.remaining"], capture_output=True, text=True, check=True)
            self.status_bar.showMessage(f"API Calls Remaining: {result.stdout.strip()}")
        except:
            pass

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
            "chk_release": self.chk_release.isChecked()
        }

    def apply_config(self, config):
        self.vis_filter.setCurrentText(config.get("vis_filter", "All"))
        self.status_filter.setCurrentText(config.get("status_filter", "All"))
        self.type_filter.setCurrentText(config.get("type_filter", "All"))
        self.stars_filter.setValue(config.get("stars_filter", 0))
        self.stale_days.setValue(config.get("stale_days", 180))
        self.chk_health.setChecked(config.get("chk_health", False))
        self.chk_popularity.setChecked(config.get("chk_popularity", True))
        self.chk_license.setChecked(config.get("chk_license", False))
        self.chk_ci.setChecked(config.get("chk_ci", False))
        self.chk_unprotected.setChecked(config.get("chk_unprotected", False))
        self.chk_stale.setChecked(config.get("chk_stale", False))
        self.chk_release.setChecked(config.get("chk_release", False))

    def start_fetch(self):
        self.fetch_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.previous_repos_data = dict(self.repos_data)
        self.repos_data.clear()
        self.model.clear()
        self.reports_text.clear()

        flags = {
            'health': self.chk_health.isChecked(),
            'popularity': self.chk_popularity.isChecked(),
            'license': self.chk_license.isChecked(),
            'ci': self.chk_ci.isChecked(),
            'unprotected': self.chk_unprotected.isChecked(),
            'stale': self.chk_stale.isChecked(),
            'stale_days': self.stale_days.value(),
            'release': self.chk_release.isChecked()
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
                targets.append((org, True))

        if not targets:
            QMessageBox.warning(self, "Warning", "Please enter a Username or Organization.")
            self.fetch_btn.setEnabled(True)
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
        
        # If everything was loaded from cache, trigger finished manually
        if not self.workers:
            self.worker_finished()

    def worker_finished(self):
        self.workers = [w for w in self.workers if w.isRunning()]
        if not self.workers:
            self.fetch_btn.setEnabled(True)
            self.progress_bar.setVisible(False)
            self.update_rate_limit()
            self.generate_reports()
            self.generate_charts()

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
    # DATA PROCESSING & TABLE
    # ============================================
    def process_data(self, owner, repos):
        if self.chk_use_cache.isChecked():
            self.cache.set(owner, False, {}, repos)
        self.repos_data[owner] = repos
        self.refresh_table()

    def refresh_table(self):
        self.model.clear()
        headers = ["Owner", "Repo", "Description", "Visibility", "Status", "Type", "Language", "Size(KB)", "Updated", "📝 Notes"]
        
        if self.chk_popularity.isChecked():
            headers.extend(["★", "⑂"])
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
        
        self.model.setHorizontalHeaderLabels(headers)
        
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

                row_items = []
                def add_item(text, sort_val=None, color=None, tooltip=None):
                    item = QStandardItem(str(text))
                    if sort_val is not None:
                        item.setData(sort_val, Qt.UserRole)
                    if color:
                        item.setForeground(color)
                    if tooltip:
                        item.setToolTip(tooltip)
                    row_items.append(item)

                add_item(owner, owner)
                add_item(r.get('name', ''), r.get('name', ''), tooltip=f"https://github.com/{r.get('full_name', '')}")
                
                desc = r.get('description', '—') or '—'
                add_item(desc, desc, tooltip=desc)
                
                vis_color = QColor("red") if is_private else QColor("green")
                add_item(vis.capitalize(), is_private, vis_color)
                
                stat_color = QColor("orange") if is_archived else QColor("green")
                add_item(status.capitalize(), is_archived, stat_color)
                
                add_item(rtype.capitalize(), is_fork)
                add_item(r.get('language', '—'), r.get('language', ''))
                add_item(str(r.get('size', 0)), r.get('size', 0))
                add_item(r.get('updated_at', '').split('T')[0], r.get('updated_at', ''))

                full_name = r.get('full_name', '')
                ann = all_annotations.get(full_name, {})
                note_text = ann.get('note', '')
                note_display = f"📝 {note_text[:20]}{'...' if len(note_text) > 20 else ''}" if note_text else "—"
                add_item(note_display, note_text, tooltip=note_text)

                if self.chk_popularity.isChecked():
                    add_item(str(r.get('stargazers_count', 0)), r.get('stargazers_count', 0))
                    add_item(str(r.get('forks_count', 0)), r.get('forks_count', 0))

                if self.chk_health.isChecked():
                    add_item(str(r.get('open_issues_count', 0)), r.get('open_issues_count', 0))
                    add_item(str(r.get('open_prs', 0)), r.get('open_prs', 0))
                    lc = r.get('last_commit', '')
                    add_item(self.format_duration(lc) if lc else "N/A", lc)

                if self.chk_license.isChecked():
                    add_item(r.get('license_det', 'NONE'), r.get('license_det', 'NONE'))

                if self.chk_ci.isChecked():
                    has_ci = r.get('has_ci', False)
                    add_item("✅" if has_ci else "❌", 1 if has_ci else 0)

                if self.chk_unprotected.isChecked():
                    is_prot = r.get('branch_protected', False)
                    add_item("🛡️ Yes" if is_prot else "⚠️ No", 1 if is_prot else 0)

                if self.chk_stale.isChecked():
                    is_stale = r.get('is_stale', False)
                    stale_color = QColor("magenta") if is_stale else None
                    add_item("⚠️ Stale" if is_stale else "Fresh", 1 if is_stale else 0, stale_color)

                if self.chk_release.isChecked():
                    add_item(r.get('latest_release', 'None'), r.get('latest_release', 'None'))
                    rd = r.get('release_date', '')
                    add_item(rd.split('T')[0] if rd else '—', rd)

                self.model.appendRow(row_items)

    def format_duration(self, date_str):
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
        except:
            return "N/A"

    # ============================================
    # CHARTS
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
            langs = {}
            for r in repos:
                lang = r.get('language') or 'None'
                langs[lang] = langs.get(lang, 0) + 1
            
            if len(langs) > 8:
                sorted_langs = sorted(langs.items(), key=lambda x: x[1], reverse=True)
                top = dict(sorted_langs[:8])
                top['Other'] = sum(v for _, v in sorted_langs[8:])
                langs = top
                
            self.charts_layout.addWidget(QLabel(f"<h3>{owner} — Languages</h3>"))
            self.charts_layout.addWidget(PieChartWidget(langs, f"Languages — {owner}"))
            
            vis_data = {
                'Public': sum(1 for r in repos if not r.get('private')),
                'Private': sum(1 for r in repos if r.get('private'))
            }
            self.charts_layout.addWidget(PieChartWidget(vis_data, f"Visibility — {owner}"))
            
            if self.chk_popularity.isChecked():
                star_data = {
                    r.get('name', ''): r.get('stargazers_count', 0) 
                    for r in sorted(repos, key=lambda x: x.get('stargazers_count', 0), reverse=True)[:10]
                }
                self.charts_layout.addWidget(BarChartWidget(star_data, f"Top 10 by Stars — {owner}", "Stars"))

    # ============================================
    # REPORTS & DIFFS
    # ============================================
    def generate_reports(self):
        report = ""
        
        if self.previous_repos_data:
            for owner in self.repos_data:
                if owner in self.previous_repos_data:
                    diff = self.compute_diff(self.previous_repos_data[owner], self.repos_data[owner])
                    if diff['added'] or diff['removed'] or diff['changed']:
                        report += "<h2>🔄 Changes Since Last Fetch</h2>"
                        report += f"<p><span style='color:#3fb950'>+{len(diff['added'])} new</span> | <span style='color:#f85149'>-{len(diff['removed'])} removed</span> | <span style='color:#d29922'>~{len(diff['changed'])} changed</span></p>"
                        if diff['added']:
                            report += "<h3 style='color:#3fb950'>🆕 New Repos</h3><ul>"
                            for n in diff['added']:
                                report += f"<li>{n}</li>"
                            report += "</ul>"
                        if diff['removed']:
                            report += "<h3 style='color:#f85149'>🗑️ Removed Repos</h3><ul>"
                            for n in diff['removed']:
                                report += f"<li>{n}</li>"
                            report += "</ul>"
                        if diff['changed']:
                            report += "<h3 style='color:#d29922'>📝 Changed</h3><ul>"
                            for item in diff['changed'][:20]:
                                report += f"<li><b>{item['repo']}</b><ul>"
                                for f, v in item['changes'].items():
                                    report += f"<li>{f}: {v['from']} → {v['to']}</li>"
                                report += "</ul></li>"
                            report += "</ul>"

        for owner, repos in self.repos_data.items():
            report += f"<h2>Summary for {owner}</h2>"
            total = len(repos)
            public = sum(1 for r in repos if not r.get('private'))
            active = sum(1 for r in repos if not r.get('archived'))
            report += f"<b>Total:</b> {total} | <span style='color:green'>Public: {public}</span> | <span style='color:red'>Private: {total - public}</span><br>"
            report += f"<span style='color:green'>Active: {active}</span> | <span style='color:orange'>Archived: {total - active}</span><br><hr>"
            
            langs = {}
            for r in repos:
                lang = r.get('language', 'None') or 'None'
                langs[lang] = langs.get(lang, 0) + 1
            report += "<h3>Languages</h3><ul>"
            for k, v in sorted(langs.items(), key=lambda item: item[1], reverse=True):
                report += f"<li>{k}: {v}</li>"
            report += "</ul>"

            if self.chk_stale.isChecked():
                stale_repos = [r for r in repos if r.get('is_stale')]
                report += f"<h3 style='color:magenta'>Stale Repos (>{self.stale_days.value()} days)</h3><ul>"
                if stale_repos:
                    for r in stale_repos:
                        report += f"<li>{r.get('full_name')} (Last: {r.get('updated_at', '').split('T')[0]})</li>"
                else:
                    report += "<li>No stale repos!</li>"
                report += "</ul>"

            if self.chk_unprotected.isChecked():
                unprot = [r for r in repos if not r.get('branch_protected') and not r.get('archived')]
                report += f"<h3 style='color:red'>Unprotected Default Branches</h3><ul>"
                if unprot:
                    for r in unprot:
                        report += f"<li>{r.get('full_name')} ({r.get('checked_branch', 'main')})</li>"
                else:
                    report += "<li>All protected!</li>"
                report += "</ul>"

        self.reports_text.setHtml(report)

    def compute_diff(self, old_repos, new_repos):
        old_map = {r['full_name']: r for r in old_repos}
        new_map = {r['full_name']: r for r in new_repos}
        old_names = set(old_map.keys())
        new_names = set(new_map.keys())
        
        changed = []
        for name in old_names & new_names:
            old_r, new_r = old_map[name], new_map[name]
            changes = {}
            for field in ['stargazers_count', 'forks_count', 'open_issues_count', 'private', 'archived', 'language', 'size', 'updated_at']:
                if old_r.get(field) != new_r.get(field):
                    changes[field] = {'from': old_r.get(field), 'to': new_r.get(field)}
            if changes:
                changed.append({'repo': name, 'changes': changes})
                
        return {
            'added': sorted(new_names - old_names),
            'removed': sorted(old_names - new_names),
            'changed': changed
        }

    # ============================================
    # EXPORTS
    # ============================================
    def export_data(self, fmt):
        if not self.repos_data:
            QMessageBox.warning(self, "No Data", "Fetch repositories first.")
            return

        if fmt == "csv":
            path, _ = QFileDialog.getSaveFileName(self, "Save CSV", "repos.csv", "CSV Files (*.csv)")
            if path:
                with open(path, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow([self.model.horizontalHeaderItem(c).text() for c in range(self.model.columnCount())])
                    for r in range(self.proxy_model.rowCount()):
                        writer.writerow([str(self.proxy_model.data(self.proxy_model.index(r, c))) for c in range(self.proxy_model.columnCount())])
                QMessageBox.information(self, "Exported", f"Saved to {path}")

        elif fmt == "json":
            path, _ = QFileDialog.getSaveFileName(self, "Save JSON", "repos.json", "JSON Files (*.json)")
            if path:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(self.repos_data, f, indent=2, ensure_ascii=False)
                QMessageBox.information(self, "Exported", f"Saved to {path}")

        elif fmt == "markdown":
            path, _ = QFileDialog.getSaveFileName(self, "Save Markdown", "repos.md", "Markdown (*.md)")
            if path:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write("# Repository Report\n\n")
                    for owner, repos in self.repos_data.items():
                        f.write(f"## {owner}\n\n| Repo | Description | Visibility | Language | Stars | Updated |\n|------|-------------|-----------|----------|-------|--------|\n")
                        for r in repos:
                            vis = "🔒" if r.get('private') else "🌐"
                            desc = (r.get('description') or '').replace('|', '\\|')
                            f.write(f"| [{r['name']}]({r['html_url']}) | {desc} | {vis} | {r.get('language', '—')} | {r.get('stargazers_count', 0)} | {r.get('updated_at', '')[:10]} |\n")
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
        QGroupBox { border: 1px solid #30363d; border-radius: 6px; margin-top: 1em; padding-top: 10px; font-weight: bold; }
        QGroupBox::title { subcontrol-origin: margin; padding: 0 3px; }
        QLineEdit, QComboBox, QSpinBox { background-color: #161b22; border: 1px solid #30363d; border-radius: 4px; padding: 4px; color: #c9d1d9; }
        QPushButton { background-color: #21262d; border: 1px solid #30363d; border-radius: 6px; padding: 5px; color: #c9d1d9; }
        QPushButton:hover { background-color: #30363d; }
        QTableView { background-color: #161b22; alternate-background-color: #0d1117; border: none; selection-background-color: #1f6feb; }
        QHeaderView::section { background-color: #161b22; color: #c9d1d9; border: 1px solid #30363d; padding: 4px; }
        QTextEdit { background-color: #161b22; border: 1px solid #30363d; color: #c9d1d9; }
        QTabWidget::pane { border: 1px solid #30363d; }
        QTabBar::tab { background-color: #161b22; color: #8b949e; padding: 8px; border: 1px solid #30363d; border-bottom: none; border-top-left-radius: 4px; border-top-right-radius: 4px; }
        QTabBar::tab:selected { background-color: #0d1117; color: #c9d1d9; }
        QProgressBar { border: 1px solid #30363d; border-radius: 4px; text-align: center; background-color: #161b22; color: #c9d1d9; }
        QProgressBar::chunk { background-color: #238636; }
        QStatusBar { background-color: #161b22; color: #8b949e; }
        QMenu { background-color: #161b22; color: #c9d1d9; border: 1px solid #30363d; }
        QMenu::item:selected { background-color: #1f6feb; }
        QScrollBar:vertical { border: none; background: #0d1117; width: 10px; margin: 0px 0px 0px 0px; }
        QScrollBar::handle:vertical { background: #30363d; min-height: 20px; border-radius: 5px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { border: none; background: none; }
        QDialog { background-color: #0d1117; color: #c9d1d9; }
    """)

    window = RepoExplorer()
    window.show()
    sys.exit(app.exec())