import sys
import os
import json
import subprocess
import logging
from datetime import datetime, timedelta, timezone

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QFormLayout, QLabel, QLineEdit, QComboBox, QSpinBox,
    QCheckBox, QPushButton, QTabWidget, QTableView, QTextEdit,
    QFileDialog, QMessageBox, QHeaderView, QStatusBar, QProgressBar
)
from PySide6.QtCore import Qt, QThread, Signal, QObject, QSortFilterProxyModel
from PySide6.QtGui import QStandardItemModel, QStandardItem, QColor, QAction, QTextCursor


# ============================================
# HELPER
# ============================================
@staticmethod
def extract_username(text: str) -> str:
    """Accept 'Entendore', 'https://github.com/Entendore', 
    'github.com/Entendore', trailing slashes, etc."""
    text = text.strip().rstrip("/")
    if "/" in text:
        # Take the last path segment
        text = text.rsplit("/", 1)[-1]
    return text

# ============================================================
# LOGGING SETUP
# ============================================================
class QLogHandler(logging.Handler, QObject):
    """Custom logging handler that emits signals to the GUI."""
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

# Also log to file
file_handler = logging.FileHandler("repo_explorer.log")
file_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
logger.addHandler(file_handler)

# ============================================================
# WORKER THREAD
# ============================================================
class GithubWorker(QThread):
    """Background thread to handle gh API calls without freezing the GUI."""
    data_ready = Signal(str, list)  # owner, list_of_dicts
    progress = Signal(str, int)     # message, percentage
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

    def run_gh(self, endpoint, silent_404=False):
        """Helper to run gh api commands."""
        cmd = ["gh", "api", endpoint]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        except subprocess.CalledProcessError as e:
            if silent_404 and "HTTP 404" in e.stderr:
                return None
            logger.error(f"API Error: {e.stderr}")
            return None

    def fetch_repos(self):
        """Fetch all repos with pagination."""
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
        """Fetch additional details based on flags."""
        needs_enrichment = any([self.flags.get('license'), self.flags.get('ci'), 
                                self.flags.get('unprotected'), self.flags.get('health')])
        if not needs_enrichment:
            return

        total = len(repos)
        for i, repo in enumerate(repos):
            full_name = repo['full_name']
            self.progress.emit(f"Enriching [{i+1}/{total}]: {full_name}", int((i/total)*100))

            if self.flags.get('license'):
                res = self.run_gh(f"repos/{full_name}")
                if res:
                    repo['license_det'] = res.get('license', {}).get('spdx_id', 'NONE') if res.get('license') else 'NONE'

            if self.flags.get('ci'):
                res = self.run_gh(f"repos/{full_name}/actions/workflows")
                repo['has_ci'] = res.get('total_count', 0) > 0 if res else False

            if self.flags.get('unprotected'):
                res = self.run_gh(f"repos/{full_name}")
                default_branch = res.get('default_branch', 'main') if res else 'main'
                repo['checked_branch'] = default_branch
                prot_res = self.run_gh(f"repos/{full_name}/branches/{default_branch}/protection", silent_404=True)
                repo['branch_protected'] = prot_res is not None

            if self.flags.get('health'):
                commits = self.run_gh(f"repos/{full_name}/commits?per_page=1")
                repo['last_commit'] = commits[0].get('commit', {}).get('author', {}).get('date', '') if commits and len(commits) > 0 else ''
                
                prs = self.run_gh(f"search/issues?q=repo:{full_name}+type:pr+state:open&per_page=1")
                repo['open_prs'] = prs.get('total_count', 0) if prs else 0

    def mark_stale(self, repos, stale_days):
        """Calculate if a repo is stale."""
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
        super().__init__()
        self.setWindowTitle("📊 GitHub Repo State Lister")
        self.resize(1200, 800)

        self.repos_data = {}  # { owner: [repos] }
        self.workers = []

        self.init_ui()
        self.check_gh_auth()
        self.update_rate_limit()

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # ============================================
        # LEFT SIDEBAR: Controls
        # ============================================
        sidebar = QWidget()
        sidebar.setMaximumWidth(320)
        sidebar_layout = QVBoxLayout(sidebar)

        # Target Group
        target_group = QGroupBox("Target")
        target_layout = QFormLayout()
        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("Auto-detected if empty")
        self.org_input = QLineEdit()
        target_layout.addRow("Username:", self.user_input)
        target_layout.addRow("Organization:", self.org_input)
        target_group.setLayout(target_layout)

        # Filters Group
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

        # Features Group
        feat_group = QGroupBox("Enrichment Features (Slower)")
        feat_layout = QVBoxLayout()
        self.chk_health = QCheckBox("Health (PRs, Last Commit)")
        self.chk_popularity = QCheckBox("Popularity (Stars, Forks)")
        self.chk_license = QCheckBox("License Detection")
        self.chk_ci = QCheckBox("CI Detection (GitHub Actions)")
        self.chk_unprotected = QCheckBox("Branch Protection")
        self.chk_stale = QCheckBox("Highlight Stale Repos")
        
        feat_layout.addWidget(self.chk_popularity)
        feat_layout.addWidget(self.chk_health)
        feat_layout.addWidget(self.chk_license)
        feat_layout.addWidget(self.chk_ci)
        feat_layout.addWidget(self.chk_unprotected)
        feat_layout.addWidget(self.chk_stale)
        feat_group.setLayout(feat_layout)

        # Action Buttons
        self.fetch_btn = QPushButton("🚀 Fetch Repos")
        self.fetch_btn.setStyleSheet("font-weight: bold; padding: 10px; background-color: #238636; color: white;")
        self.fetch_btn.clicked.connect(self.start_fetch)

        self.export_csv_btn = QPushButton("📁 Export CSV")
        self.export_csv_btn.clicked.connect(lambda: self.export_data("csv"))
        self.export_json_btn = QPushButton("📄 Export JSON")
        self.export_json_btn.clicked.connect(lambda: self.export_data("json"))

        sidebar_layout.addWidget(target_group)
        sidebar_layout.addWidget(filter_group)
        sidebar_layout.addWidget(feat_group)
        sidebar_layout.addWidget(self.fetch_btn)
        sidebar_layout.addStretch()
        sidebar_layout.addWidget(self.export_csv_btn)
        sidebar_layout.addWidget(self.export_json_btn)

        # ============================================
        # RIGHT SIDE: Results & Logs
        # ============================================
        right_panel = QTabWidget()

        # Table Tab
        self.table_view = QTableView()
        self.model = QStandardItemModel()
        self.proxy_model = QSortFilterProxyModel()
        self.proxy_model.setSourceModel(self.model)
        self.proxy_model.setSortRole(Qt.UserRole)
        self.table_view.setModel(self.proxy_model)
        self.table_view.setSortingEnabled(True)
        self.table_view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        
        # Reports Tab
        self.reports_text = QTextEdit()
        self.reports_text.setReadOnly(True)

        # Log Tab
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        handler.log_signal.connect(self.append_log)

        right_panel.addTab(self.table_view, "📊 Repositories")
        right_panel.addTab(self.reports_text, "📈 Reports")
        right_panel.addTab(self.log_text, "📜 Logs")

        main_layout.addWidget(sidebar)
        main_panel = QVBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        main_panel.addWidget(self.progress_bar)
        main_panel.addWidget(right_panel)
        main_layout.addLayout(main_panel, stretch=1)

        # Status Bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

    # ============================================
    # LOGIC METHODS
    # ============================================
    def check_gh_auth(self):
        try:
            result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
            if "Logged in" in result.stderr:
                # Extract username
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
            remaining = result.stdout.strip()
            self.status_bar.showMessage(f"API Calls Remaining: {remaining}")
        except:
            pass

    def start_fetch(self):
        self.fetch_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
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
            'stale_days': self.stale_days.value()
        }

        targets = []

        # ✅ Extract just the username from whatever the user typed
        user_raw = self.user_input.text() or self.user_input.placeholderText()
        if user_raw:
            user = self.extract_username(user_raw)
            if user:
                targets.append((user, False))

        org_raw = self.org_input.text()
        if org_raw:
            org = self.extract_username(org_raw)   # same helper works for orgs too
            if org:
                targets.append((org, True))

        if not targets:
            QMessageBox.warning(self, "Warning", "Please enter a Username or Organization.")
            self.fetch_btn.setEnabled(True)
            self.progress_bar.setVisible(False)
            return

        for owner, is_org in targets:
            worker = GithubWorker(owner, is_org, flags)
            worker.data_ready.connect(self.process_data)
            worker.progress.connect(self.update_progress)
            worker.error.connect(self.show_error)
            worker.finished.connect(self.worker_finished)
            self.workers.append(worker)
            worker.start()

    def worker_finished(self):
        self.workers = [w for w in self.workers if w.isRunning()]
        if not self.workers:
            self.fetch_btn.setEnabled(True)
            self.progress_bar.setVisible(False)
            self.update_rate_limit()
            self.generate_reports()

    def update_progress(self, msg, pct):
        self.status_bar.showMessage(msg)
        if pct >= 0:
            self.progress_bar.setValue(pct)

    def show_error(self, msg):
        logger.error(msg)
        QMessageBox.critical(self, "Error", msg)

    def append_log(self, msg):
        self.log_text.append(msg)
        # Auto-scroll
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.log_text.setTextCursor(cursor)

    # ============================================
    # DATA PROCESSING & TABLE
    # ============================================
    def process_data(self, owner, repos):
        self.repos_data[owner] = repos
        self.refresh_table()

    def refresh_table(self):
        self.model.clear()
        
        # Determine columns
        headers = ["Owner", "Repo", "Visibility", "Status", "Type", "Language", "Size(KB)", "Updated"]
        if self.chk_popularity.isChecked(): headers.extend(["★", "⑂"])
        if self.chk_health.isChecked(): headers.extend(["Issues", "PRs", "Last Commit"])
        if self.chk_license.isChecked(): headers.append("License")
        if self.chk_ci.isChecked(): headers.append("CI")
        if self.chk_unprotected.isChecked(): headers.append("Protected")
        if self.chk_stale.isChecked(): headers.append("Stale")
        
        self.model.setHorizontalHeaderLabels(headers)

        # Filter values from UI
        vis_f = self.vis_filter.currentText().lower()
        status_f = self.status_filter.currentText().lower()
        type_f = self.type_filter.currentText().lower()
        stars_f = self.stars_filter.value()

        for owner, repos in self.repos_data.items():
            for r in repos:
                # Apply Filters
                is_private = r.get('private', False)
                vis = "private" if is_private else "public"
                if vis_f != "all" and vis != vis_f: continue

                is_archived = r.get('archived', False)
                status = "archived" if is_archived else "active"
                if status_f != "all" and status != status_f: continue

                is_fork = r.get('fork', False)
                rtype = "fork" if is_fork else "source"
                if type_f != "all" and rtype != type_f: continue

                if r.get('stargazers_count', 0) < stars_f: continue

                # Create Row
                row_items = []
                
                def add_item(text, sort_val=None, color=None):
                    item = QStandardItem(str(text))
                    if sort_val is not None:
                        item.setData(sort_val, Qt.UserRole)
                    if color:
                        item.setForeground(color)
                    row_items.append(item)

                add_item(owner, owner)
                add_item(r.get('name', ''), r.get('name', ''))
                
                vis_color = QColor("red") if is_private else QColor("green")
                add_item(vis.capitalize(), is_private, vis_color)
                
                stat_color = QColor("orange") if is_archived else QColor("green")
                add_item(status.capitalize(), is_archived, stat_color)
                
                add_item(rtype.capitalize(), is_fork)
                add_item(r.get('language', '—'), r.get('language', ''))
                add_item(str(r.get('size', 0)), r.get('size', 0))
                
                updated = r.get('updated_at', '').split('T')[0]
                add_item(updated, updated)

                if self.chk_popularity.isChecked():
                    add_item(str(r.get('stargazers_count', 0)), r.get('stargazers_count', 0))
                    add_item(str(r.get('forks_count', 0)), r.get('forks_count', 0))

                if self.chk_health.isChecked():
                    add_item(str(r.get('open_issues_count', 0)), r.get('open_issues_count', 0))
                    add_item(str(r.get('open_prs', 0)), r.get('open_prs', 0))
                    
                    lc = r.get('last_commit', '')
                    lc_display = self.format_duration(lc) if lc else "N/A"
                    add_item(lc_display, lc)

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

                self.model.appendRow(row_items)

    def format_duration(self, date_str):
        try:
            past = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            days = (now - past).days
            if days > 365: return f"{days//365}y ago"
            if days > 30: return f"{days//30}mo ago"
            return f"{days}d ago"
        except:
            return "N/A"

    # ============================================
    # REPORTS
    # ============================================
    def generate_reports(self):
        report = ""
        for owner, repos in self.repos_data.items():
            report += f"<h2>Summary for {owner}</h2>"
            total = len(repos)
            public = sum(1 for r in repos if not r.get('private'))
            private = total - public
            active = sum(1 for r in repos if not r.get('archived'))
            archived = total - active
            
            report += f"<b>Total:</b> {total} | <span style='color:green'>Public: {public}</span> | <span style='color:red'>Private: {private}</span><br>"
            report += f"<span style='color:green'>Active: {active}</span> | <span style='color:orange'>Archived: {archived}</span><br><hr>"

            # Language Breakdown
            langs = {}
            for r in repos:
                lang = r.get('language', 'None') or 'None'
                langs[lang] = langs.get(lang, 0) + 1
            
            report += "<h3>Languages</h3><ul>"
            for k, v in sorted(langs.items(), key=lambda item: item[1], reverse=True):
                report += f"<li>{k}: {v}</li>"
            report += "</ul>"

            # Stale Report
            if self.chk_stale.isChecked():
                stale_repos = [r for r in repos if r.get('is_stale')]
                report += f"<h3 style='color:magenta'>Stale Repos (>{self.stale_days.value()} days)</h3><ul>"
                for r in stale_repos:
                    report += f"<li>{r.get('full_name')} (Last: {r.get('updated_at', '').split('T')[0]})</li>"
                if not stale_repos: report += "<li>No stale repos!</li>"
                report += "</ul>"

            # Unprotected Report
            if self.chk_unprotected.isChecked():
                unprot = [r for r in repos if not r.get('branch_protected') and not r.get('archived')]
                report += f"<h3 style='color:red'>Unprotected Default Branches</h3><ul>"
                for r in unprot:
                    report += f"<li>{r.get('full_name')} ({r.get('checked_branch', 'main')})</li>"
                if not unprot: report += "<li>All protected!</li>"
                report += "</ul>"

        self.reports_text.setHtml(report)

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
                with open(path, 'w') as f:
                    headers = [self.model.horizontalHeaderItem(c).text() for c in range(self.model.columnCount())]
                    f.write(",".join(headers) + "\n")
                    
                    # Use proxy model to respect current sort/filter
                    for r in range(self.proxy_model.rowCount()):
                        row_data = []
                        for c in range(self.proxy_model.columnCount()):
                            index = self.proxy_model.index(r, c)
                            row_data.append(str(self.proxy_model.data(index)))
                        f.write(",".join(row_data) + "\n")
                QMessageBox.information(self, "Exported", f"Saved to {path}")

        elif fmt == "json":
            path, _ = QFileDialog.getSaveFileName(self, "Save JSON", "repos.json", "JSON Files (*.json)")
            if path:
                with open(path, 'w') as f:
                    json.dump(self.repos_data, f, indent=2)
                QMessageBox.information(self, "Exported", f"Saved to {path}")

# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # Apply a basic dark style
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
    """)

    window = RepoExplorer()
    window.show()
    sys.exit(app.exec())