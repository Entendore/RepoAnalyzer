# GitHub Repo State Lister

A PySide6 desktop GUI to batch-query, filter, annotate, and analyze GitHub repositories via the `gh` CLI API.

## Prerequisites

| Requirement | Version / Note |
|---|---|
| Python | 3.9+ |
| GitHub CLI (`gh`) | Installed and authenticated (`gh auth login`) |
| Git | Required for SSH/HTTPS clone context menu actions |

## Installation

```bash
pip install PySide6 matplotlib
```
*`matplotlib` is optional; the Charts tab is disabled without it.*

## Usage

```bash
python app.py
```
The app auto-detects the authenticated `gh` user on startup. Enter a Username and/or Organization and click **Fetch Repos**.

## Architecture & Features

### Data Acquisition
- Executes `gh api` via `subprocess` in a `QThread` to prevent UI blocking.
- Supports concurrent fetching for multiple users/orgs.
- Paginated requests (`?per_page=100`).
- Silently handles HTTP 404/409 errors; skips API calls for empty repos (`size == 0`) to avoid conflicts.

### Enrichment Flags (Optional)
Appends specific API queries per repo if enabled:

| Flag | API Endpoint(s) | Table Column(s) Added |
|---|---|---|
| **Popularity** | Base repo data | Stars, Forks |
| **Health** | `commits?per_page=1`, `search/issues` | Issues, PRs, Last Commit |
| **License** | `repos/{full_name}` | License (SPDX ID) |
| **CI** | `actions/workflows` | CI (True/False) |
| **Branch Protection** | `branches/{branch}/protection` | Protected (Yes/No) |
| **Stale** | Compares `updated_at` | Stale (Threshold in days) |
| **Release** | `releases/latest` | Release, Release Date |

### Filtering
Applies client-side `QSortFilterProxyModel` filters:
- **Visibility**: Public / Private
- **Status**: Active / Archived
- **Type**: Source / Fork
- **Min Stars**: Integer threshold
- **Search Bar**: Real-time text filter on the Description column.

### Local Storage

| Component | Format | Path | Details |
|---|---|---|---|
| **Cache** | Pickle | `.repo_cache/*.pkl` | 24h TTL. Keyed by MD5(owner + flags). Toggled via UI. |
| **Annotations** | SQLite | `.repo_annotations.db` | Schema: `repo_full_name` (PK), `note`, `tags`. |
| **Profiles** | JSON | `.repo_profiles/*.json` | Saves current UI filter/enrichment state. |
| **App Settings** | QSettings | OS-specific registry/conf | Saves UI state on `closeEvent`. |
| **Logs** | Plain Text | `repo_explorer.log` | File-based log. |

### Profiles & Presets
- **Save/Load/Delete**: Custom UI configurations.
- **Built-in Presets**:
  - `security_audit`: Active, Source, License + Protection + Stale.
  - `spring_cleaning`: All, Health + Popularity + Stale.
  - `quick_overview`: Active, Source, Popularity + Release.

### Export Formats
CSV, JSON, Markdown.

### Context Menu Actions (Right-Click)
Open in Browser | Copy URL | Copy Full Name | Clone (SSH) | Clone (HTTPS) | Add Note / Tags

### UI Tabs
1. **Repositories**: `QTableView` with sorting, filtering, and inline annotation previews.
2. **Reports**: HTML summary (totals, active/archived, language breakdown) and diff tracking (added/removed/changed repos since last fetch).
3. **Charts**: Matplotlib `FigureCanvas` (Language pie, Visibility pie, Top 10 Stars bar).
4. **Logs**: Live `QTextEdit` feed routed from `logging.Handler`.