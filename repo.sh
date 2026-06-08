#!/usr/bin/env bash

# ============================================================
# GitHub Repo State Lister — Full-Featured + Logging
# Usage: ./repo-states.sh [options] [organization]
# ============================================================

set -euo pipefail

# ============================================================
# DEFAULT CONFIG
# ============================================================
STALE_DAYS=180
MIN_STARS_FILTER=0
SORT_BY="name"
FILTER_VISIBILITY=""
FILTER_STATUS=""
FILTER_TYPE=""
FILTER_LANG=""
OUTPUT_FORMAT="table"       # table, csv, json, markdown
SHOW_HEALTH=false
SHOW_LANGUAGES=false
SHOW_LICENSE=false
SHOW_CI=false
SHOW_STALE=false
SHOW_POPULARITY=false
SHOW_FEATURES=false
SHOW_UNPROTECTED=false
INTERACTIVE=false
ORG=""
DRY_RUN=false

# Logging Config
LOG_DIR="./logs"
LOG_FILE=""
NO_LOG=false
VERBOSE_LOG=false

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ============================================================
# LOGGING SYSTEM
# ============================================================

# Initialize logging directory and file
init_logging() {
    if [[ "$NO_LOG" == true ]]; then
        return
    fi

    mkdir -p "$LOG_DIR" 2>/dev/null || {
        echo "Warning: Cannot create log directory $LOG_DIR. Disabling logging."
        NO_LOG=true
        return
    }

    local timestamp
    timestamp=$(date +"%Y%m%d_%H%M%S")
    LOG_FILE="${LOG_DIR}/repo-states_${timestamp}.log"

    touch "$LOG_FILE" 2>/dev/null || {
        echo "Warning: Cannot create log file $LOG_FILE. Disabling logging."
        NO_LOG=true
        return
    }

    log_msg "INFO" "============================================================"
    log_msg "INFO" " GitHub Repo State Lister - Session Started"
    log_msg "INFO" "============================================================"
    log_msg "INFO" " Timestamp : $(date)"
    log_msg "INFO" " User      : ${GH_USER}"
    log_msg "INFO" " Org       : ${ORG:-none}"
    log_msg "INFO" " Arguments : $*"
    log_msg "INFO" " Log File  : ${LOG_FILE}"
    log_msg "INFO" "------------------------------------------------------------"
}

# Core logging function (writes to log file, optionally to console)
log_msg() {
    local level="$1"
    shift
    local message="$*"
    local timestamp
    timestamp=$(date +"%Y-%m-%d %H:%M:%S")

    # Always write to log file (strip ANSI colors)
    if [[ "$NO_LOG" == false && -n "${LOG_FILE:-}" && -w "${LOG_FILE:-}" ]]; then
        local clean_message
        clean_message=$(echo -e "$message" | sed 's/\x1b\[[0-9;]*m//g')
        echo "[${timestamp}] [${level}] ${clean_message}" >> "$LOG_FILE"
    fi

    # Write to console for specific levels (unless suppressed)
    if [[ "$level" == "ERROR" ]]; then
        echo -e "${RED}[ERROR] ${message}${NC}" >&2
    elif [[ "$level" == "WARN" ]]; then
        echo -e "${YELLOW}[WARN] ${message}${NC}" >&2
    elif [[ "$VERBOSE_LOG" == true && "$level" == "DEBUG" ]]; then
        echo -e "${DIM}[DEBUG] ${message}${NC}" >&2
    fi
}

# Capture command output and log it
log_cmd() {
    local description="$1"
    shift
    local cmd_output
    local exit_code=0

    log_msg "DEBUG" "Executing: $*"
    
    cmd_output=$("$@" 2>&1) || exit_code=$?

    if [[ "$exit_code" -ne 0 ]]; then
        log_msg "ERROR" "${description} failed (exit code ${exit_code}): ${cmd_output}"
    else
        log_msg "DEBUG" "${description} succeeded: $(echo "$cmd_output" | head -c 500)"
    fi

    echo "$cmd_output"
    return $exit_code
}

# Log rate limit status
log_rate_limit() {
    if [[ "$NO_LOG" == true ]]; then return; fi
    
    local limit_data
    limit_data=$(gh api rate_limit -q '.resources.core | {remaining, limit, reset}' 2>/dev/null || echo "{}")
    
    local remaining limit reset_epoch reset_time
    remaining=$(echo "$limit_data" | jq -r '.remaining // "unknown"')
    limit=$(echo "$limit_data" | jq -r '.limit // "unknown"')
    reset_epoch=$(echo "$limit_data" | jq -r '.reset // "0"')
    
    if [[ "$reset_epoch" != "0" ]]; then
        reset_time=$(date -r "$reset_epoch" 2>/dev/null || date -d "@$reset_epoch" 2>/dev/null || echo "unknown")
    else
        reset_time="unknown"
    fi
    
    log_msg "INFO" "API Rate Limit: ${remaining}/${limit} remaining (resets: ${reset_time})"
}

# Log summary stats of fetched repos
log_repo_stats() {
    local repos="$1"
    local label="$2"
    
    if [[ "$NO_LOG" == true ]]; then return; fi
    
    local total public private active archived
    total=$(echo "$repos" | jq 'length')
    public=$(echo "$repos" | jq '[.[] | select(.private == false)] | length')
    private=$(echo "$repos" | jq '[.[] | select(.private == true)] | length')
    active=$(echo "$repos" | jq '[.[] | select(.archived == false)] | length')
    archived=$(echo "$repos" | jq '[.[] | select(.archived == true)] | length')
    
    log_msg "INFO" "${label} - Fetched: ${total} total, ${public} public, ${private} private, ${active} active, ${archived} archived"
}

# Tee output to both stdout and log file
tee_to_log() {
    if [[ "$NO_LOG" == true ]]; then
        cat
    else
        tee >(sed 's/\x1b\[[0-9;]*m//g' >> "$LOG_FILE")
    fi
}

# ============================================================
# HELP
# ============================================================
usage() {
    cat <<EOF
 ${BOLD}GitHub Repo State Lister${NC} — Full-Featured with Logging

 ${BOLD}USAGE${NC}
    ./repo-states.sh [options] [organization]

 ${BOLD}OPTIONS${NC}
    ${CYAN}Filtering${NC}
    --visibility <public|private>   Filter by visibility
    --status <active|archived>      Filter by status
    --type <source|fork>            Filter by repo type
    --lang <language>               Filter by primary language
    --min-stars <N>                 Filter by minimum stars

    ${CYAN}Sorting${NC}
    --sort <field>                  Sort by: name, updated, stars, forks, size, issues, language

    ${CYAN}Feature Flags${NC}
    --health                        Show health metrics (open issues, PRs, last commit)
    --languages                     Show language breakdown statistics
    --license                       Show license detection for each repo
    --ci                            Show CI/CD detection (GitHub Actions)
    --stale                         Highlight stale repos (no update in ${STALE_DAYS} days)
    --stale-days <N>                Set stale threshold in days
    --popularity                    Show stars, forks, watchers
    --features                      Show enabled features (wiki, issues, pages)
    --unprotected                   Show repos without branch protection
    --all                           Enable all feature flags

    ${CYAN}Output${NC}
    --format <table|csv|json|md>    Output format (default: table)
    --interactive                   Interactive menu mode

    ${CYAN}Logging${NC}
    --log-dir <path>                Set log directory (default: ./logs)
    --no-log                        Disable file logging entirely
    --verbose                       Show DEBUG logs in console

    ${CYAN}General${NC}
    --dry-run                       Show what would be fetched (no API calls)
    -h, --help                      Show this help

 ${BOLD}EXAMPLES${NC}
    ./repo-states.sh myorg
    ./repo-states.sh --stale --stale-days 90 --log-dir /var/log/gh-reports myorg
    ./repo-states.sh --health --popularity --format md myorg > report.md
    ./repo-states.sh --visibility private --sort stars --all
    ./repo-states.sh --interactive --verbose
    ./repo-states.sh --languages --no-log myorg
EOF
    exit 0
}

# ============================================================
# ARGUMENT PARSING
# ============================================================
while [[ $# -gt 0 ]]; do
    case "$1" in
        --visibility)   FILTER_VISIBILITY="$2"; shift 2 ;;
        --status)       FILTER_STATUS="$2"; shift 2 ;;
        --type)         FILTER_TYPE="$2"; shift 2 ;;
        --lang)         FILTER_LANG="$2"; shift 2 ;;
        --min-stars)    MIN_STARS_FILTER="$2"; shift 2 ;;
        --sort)         SORT_BY="$2"; shift 2 ;;
        --health)       SHOW_HEALTH=true; shift ;;
        --languages)    SHOW_LANGUAGES=true; shift ;;
        --license)      SHOW_LICENSE=true; shift ;;
        --ci)           SHOW_CI=true; shift ;;
        --stale)        SHOW_STALE=true; shift ;;
        --stale-days)   STALE_DAYS="$2"; shift 2 ;;
        --popularity)   SHOW_POPULARITY=true; shift ;;
        --features)     SHOW_FEATURES=true; shift ;;
        --unprotected)  SHOW_UNPROTECTED=true; shift ;;
        --all)          SHOW_HEALTH=true; SHOW_LANGUAGES=true; SHOW_LICENSE=true
                        SHOW_CI=true; SHOW_STALE=true; SHOW_POPULARITY=true
                        SHOW_FEATURES=true; SHOW_UNPROTECTED=true; shift ;;
        --format)       OUTPUT_FORMAT="$2"; shift 2 ;;
        --interactive)  INTERACTIVE=true; shift ;;
        --log-dir)      LOG_DIR="$2"; shift 2 ;;
        --no-log)       NO_LOG=true; shift ;;
        --verbose)      VERBOSE_LOG=true; shift ;;
        --dry-run)      DRY_RUN=true; shift ;;
        -h|--help)      usage ;;
        *)              ORG="$1"; shift ;;
    esac
done

# ============================================================
# DEPENDENCY CHECKS
# ============================================================
command -v gh >/dev/null 2>&1 || { echo "Error: gh CLI required. https://cli.github.com"; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "Error: jq required."; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Error: Run 'gh auth login' first."; exit 1; }

GH_USER=$(gh api user -q .login)

# Initialize Logging
init_logging "$@"

# ============================================================
# API HELPERS
# ============================================================

# Check rate limit and log
check_rate_limit() {
    local remaining
    remaining=$(gh api rate_limit -q '.resources.core.remaining' 2>/dev/null || echo "unknown")
    local reset
    reset=$(gh api rate_limit -q '.resources.core.reset' 2>/dev/null || echo "unknown")
    
    log_rate_limit
    
    if [[ "$remaining" != "unknown" ]]; then
        local reset_fmt
        reset_fmt=$(date -r "$reset" 2>/dev/null || date -d "@$reset" 2>/dev/null || echo "$reset")
        echo -e "  ${DIM}API calls remaining: ${remaining} (resets: ${reset_fmt})${NC}"
    fi
}

# Fetch all repos with pagination
fetch_repos() {
    local owner="$1"
    local type="$2"
    local page=1
    local combined="[]"

    local endpoint
    if [[ "$type" == "org" ]]; then
        endpoint="orgs/${owner}/repos"
    else
        endpoint="users/${owner}/repos"
    fi

    log_msg "INFO" "Fetching repos from endpoint: ${endpoint}"

    while true; do
        local response
        log_msg "DEBUG" "Fetching page ${page}..."
        response=$(gh api "${endpoint}?per_page=100&page=${page}" 2>/dev/null) || {
            log_msg "WARN" "Failed to fetch page ${page} from ${endpoint}"
            break
        }

        local count
        count=$(echo "$response" | jq 'length')
        [[ "$count" -eq 0 ]] && break

        combined=$(echo "$combined" "$response" | jq -s 'add')
        log_msg "DEBUG" "Fetched ${count} repos from page ${page}"

        [[ "$count" -lt 100 ]] && break
        page=$((page + 1))
    done

    log_repo_stats "$combined" "$owner"
    echo "$combined"
}

# Fetch additional repo details (health, CI, license, protection)
fetch_repo_details() {
    local full_name="$1"
    local details="{}"

    # License
    if [[ "$SHOW_LICENSE" == true ]]; then
        local license
        license=$(gh api "repos/${full_name}" -q '.license.spdx_id // "NONE"' 2>/dev/null || echo "NONE")
        details=$(echo "$details" | jq --arg lic "$license" '. + {license: $lic}')
        log_msg "DEBUG" "[${full_name}] License: ${license}"
    fi

    # CI detection
    if [[ "$SHOW_CI" == true ]]; then
        local has_ci=false
        local workflows
        workflows=$(gh api "repos/${full_name}/actions/workflows" -q '.total_count' 2>/dev/null || echo "0")
        [[ "$workflows" -gt 0 ]] && has_ci=true
        details=$(echo "$details" | jq --argjson ci "$has_ci" '. + {has_ci: $ci}')
        log_msg "DEBUG" "[${full_name}] CI (GitHub Actions): ${has_ci}"
    fi

    # Branch protection
    if [[ "$SHOW_UNPROTECTED" == true ]]; then
        local default_branch
        default_branch=$(gh api "repos/${full_name}" -q '.default_branch' 2>/dev/null || echo "main")
        local protected=false
        gh api "repos/${full_name}/branches/${default_branch}/protection" >/dev/null 2>&1 && protected=true
        details=$(echo "$details" | jq --argjson prot "$protected" --arg db "$default_branch" '. + {branch_protected: $prot, checked_branch: $db}')
        log_msg "DEBUG" "[${full_name}] Branch '${default_branch}' protected: ${protected}"
    fi

    # Health: last commit, open issues count, open PRs count
    if [[ "$SHOW_HEALTH" == true ]]; then
        local last_commit_date
        last_commit_date=$(gh api "repos/${full_name}/commits?per_page=1" -q '.[0].commit.author.date // "never"' 2>/dev/null || echo "never")

        local open_prs
        open_prs=$(gh api "search/issues?q=repo:${full_name}+type:pr+state:open&per_page=1" -q '.total_count' 2>/dev/null || echo "0")

        details=$(echo "$details" | jq \
            --arg lcd "$last_commit_date" \
            --argjson prs "$open_prs" \
            '. + {last_commit: $lcd, open_prs: $prs}')
        log_msg "DEBUG" "[${full_name}] Last commit: ${last_commit_date}, Open PRs: ${open_prs}"
    fi

    echo "$details"
}

# ============================================================
# DATA ENRICHMENT
# ============================================================
enrich_repos() {
    local repos="$1"
    local owner="$2"

    if [[ "$DRY_RUN" == true ]]; then
        log_msg "INFO" "Dry run: Skipping enrichment for ${owner}"
        echo "$repos" | jq '[.[] | . + {license: "DRY_RUN", has_ci: false, branch_protected: false}]'
        return
    fi

    local total
    total=$(echo "$repos" | jq 'length')
    local count=0

    local needs_details=false
    [[ "$SHOW_LICENSE" == true ]] && needs_details=true
    [[ "$SHOW_CI" == true ]] && needs_details=true
    [[ "$SHOW_UNPROTECTED" == true ]] && needs_details=true
    [[ "$SHOW_HEALTH" == true ]] && needs_details=true

    if [[ "$needs_details" == false ]]; then
        log_msg "INFO" "No enrichment flags set, skipping detail fetching for ${owner}"
        echo "$repos"
        return
    fi

    log_msg "INFO" "Enriching ${total} repos with additional data for ${owner}..."
    echo -e "  ${DIM}Enriching ${total} repos with additional data...${NC}" >&2

    local enriched="[]"
    while IFS= read -r repo_json; do
        local full_name
        full_name=$(echo "$repo_json" | jq -r '.full_name')
        count=$((count + 1))

        printf "\r  ${DIM}[%d/%d] Fetching: %-60s${NC}" "$count" "$total" "$full_name" >&2
        log_msg "DEBUG" "Enriching [${count}/${total}]: ${full_name}"

        local details
        details=$(fetch_repo_details "$full_name")

        local merged
        merged=$(echo "$repo_json" "$details" | jq -s '.[0] * .[1]')
        enriched=$(echo "$enriched" "$merged" | jq -s 'add')
    done < <(echo "$repos" | jq -c '.[]')

    echo "" >&2
    log_msg "INFO" "Enrichment completed for ${owner}"
    echo "$enriched"
}

# ============================================================
# FILTERING & SORTING
# ============================================================
apply_filters() {
    local repos="$1"
    local filtered="$repos"

    if [[ -n "$FILTER_VISIBILITY" ]]; then
        local is_private=false
        [[ "$FILTER_VISIBILITY" == "private" ]] && is_private=true
        filtered=$(echo "$filtered" | jq --argjson priv "$is_private" '[.[] | select(.private == $priv)]')
        log_msg "INFO" "Filtered by visibility '${FILTER_VISIBILITY}': $(echo "$filtered" | jq 'length') repos remain"
    fi

    if [[ -n "$FILTER_STATUS" ]]; then
        local is_archived=false
        [[ "$FILTER_STATUS" == "archived" ]] && is_archived=true
        filtered=$(echo "$filtered" | jq --argjson arch "$is_archived" '[.[] | select(.archived == $arch)]')
        log_msg "INFO" "Filtered by status '${FILTER_STATUS}': $(echo "$filtered" | jq 'length') repos remain"
    fi

    if [[ -n "$FILTER_TYPE" ]]; then
        local is_fork=false
        [[ "$FILTER_TYPE" == "fork" ]] && is_fork=true
        filtered=$(echo "$filtered" | jq --argjson fork "$is_fork" '[.[] | select(.fork == $fork)]')
        log_msg "INFO" "Filtered by type '${FILTER_TYPE}': $(echo "$filtered" | jq 'length') repos remain"
    fi

    if [[ -n "$FILTER_LANG" ]]; then
        filtered=$(echo "$filtered" | jq --arg lang "$FILTER_LANG" '[.[] | select((.language // "none") | ascii_downcase == ($lang | ascii_downcase))]')
        log_msg "INFO" "Filtered by language '${FILTER_LANG}': $(echo "$filtered" | jq 'length') repos remain"
    fi

    if [[ "$MIN_STARS_FILTER" -gt 0 ]]; then
        filtered=$(echo "$filtered" | jq --argjson min "$MIN_STARS_FILTER" '[.[] | select(.stargazers_count >= $min)]')
        log_msg "INFO" "Filtered by min stars ${MIN_STARS_FILTER}: $(echo "$filtered" | jq 'length') repos remain"
    fi

    echo "$filtered"
}

sort_repos() {
    local repos="$1"
    local field="$2"
    log_msg "DEBUG" "Sorting repos by: ${field}"
    case "$field" in
        name)       echo "$repos" | jq 'sort_by(.name)' ;;
        updated)    echo "$repos" | jq 'sort_by(.updated_at) | reverse' ;;
        stars)      echo "$repos" | jq 'sort_by(.stargazers_count) | reverse' ;;
        forks)      echo "$repos" | jq 'sort_by(.forks_count) | reverse' ;;
        size)       echo "$repos" | jq 'sort_by(.size) | reverse' ;;
        issues)     echo "$repos" | jq 'sort_by(.open_issues_count) | reverse' ;;
        language)   echo "$repos" | jq 'sort_by(.language // "zzz")' ;;
        *)          echo "$repos" | jq 'sort_by(.name)' ;;
    esac
}

mark_stale() {
    local repos="$1"
    local stale_secs=$((STALE_DAYS * 86400))
    local now_epoch
    now_epoch=$(date +%s)

    log_msg "INFO" "Marking stale repos (threshold: ${STALE_DAYS} days)"
    echo "$repos" | jq --argjson threshold "$stale_secs" --argjson now "$now_epoch" '
        .[] |
        .updated_epoch = ((.updated_at | split("T")[0] | split("-") | map(tostring) | join("-")) | strptime("%Y-%m-%d") | mktime) |
        .is_stale = (($now - .updated_epoch) > $threshold) |
        del(.updated_epoch)
    ' | jq -s '.'
}

# ============================================================
# FORMATTERS
# ============================================================
format_size() {
    local kb="$1"
    if [[ "$kb" -ge 1048576 ]]; then echo "$(echo "scale=1; $kb/1048576" | bc)GB"
    elif [[ "$kb" -ge 1024 ]]; then echo "$(echo "scale=1; $kb/1024" | bc)MB"
    else echo "${kb}KB"; fi
}

format_duration() {
    local last_date="$1"
    if [[ "$last_date" == "never" || -z "$last_date" ]]; then echo "never"; return; fi
    local last_epoch now_epoch
    now_epoch=$(date +%s)
    if date -d "$last_date" +%s >/dev/null 2>&1; then last_epoch=$(date -d "$last_date" +%s)
    elif date -j -f "%Y-%m-%dT%H:%M:%SZ" "$last_date" +%s >/dev/null 2>&1; then last_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$last_date" +%s)
    else
        local date_only="${last_date%%T*}"
        last_epoch=$(date -d "$date_only" +%s 2>/dev/null || date -j -f "%Y-%m-%d" "$date_only" +%s 2>/dev/null || echo "$now_epoch")
    fi
    local days=$(( (now_epoch - last_epoch) / 86400 ))
    if [[ "$days" -gt 365 ]]; then echo "$(( days / 365 ))y ago"
    elif [[ "$days" -gt 30 ]]; then echo "$(( days / 30 ))mo ago"
    else echo "${days}d ago"; fi
}

# -------------------------------------------------------------------
# TABLE OUTPUT
# -------------------------------------------------------------------
print_table() {
    local repos="$1"
    local title="$2"

    if [[ -z "$repos" ]] || [[ "$(echo "$repos" | jq 'length')" -eq 0 ]]; then
        log_msg "WARN" "No repositories found for ${title}"
        echo -e "  ${DIM}No repositories found.${NC}"
        return
    fi

    local count
    count=$(echo "$repos" | jq 'length')
    log_msg "INFO" "Generating table for ${title} (${count} repos)"

    echo ""
    echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${CYAN}║  ${title} (${count} repos)${NC}"
    echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════════════╝${NC}"

    # Build header
    local header="REPO"; local hfmt="  %-30s"
    header+="$(printf ' %-10s' 'VIS')"; hfmt+=" %-10s"
    header+="$(printf ' %-10s' 'STATUS')"; hfmt+=" %-10s"
    header+="$(printf ' %-8s' 'TYPE')"; hfmt+=" %-8s"

    if [[ "$SHOW_POPULARITY" == true ]]; then
        header+="$(printf ' %-6s' '★')"; hfmt+=" %-6s"
        header+="$(printf ' %-6s' '⑂')"; hfmt+=" %-6s"
        header+="$(printf ' %-6s' '👁')"; hfmt+=" %-6s"
    fi

    header+="$(printf ' %-10s' 'LANG')"; hfmt+=" %-10s"
    header+="$(printf ' %-8s' 'SIZE')"; hfmt+=" %-8s"

    if [[ "$SHOW_HEALTH" == true ]]; then
        header+="$(printf ' %-10s' 'ISSUES')"; hfmt+=" %-10s"
        header+="$(printf ' %-8s' 'PRs')"; hfmt+=" %-8s"
        header+="$(printf ' %-12s' 'LAST PUSH')"; hfmt+=" %-12s"
    fi

    if [[ "$SHOW_LICENSE" == true ]]; then header+="$(printf ' %-12s' 'LICENSE')"; hfmt+=" %-12s"; fi
    if [[ "$SHOW_CI" == true ]]; then header+="$(printf ' %-6s' 'CI')"; hfmt+=" %-6s"; fi
    if [[ "$SHOW_UNPROTECTED" == true ]]; then header+="$(printf ' %-10s' 'PROTECT')"; hfmt+=" %-10s"; fi
    if [[ "$SHOW_FEATURES" == true ]]; then header+="$(printf ' %-10s' 'FEATURES')"; hfmt+=" %-10s"; fi

    header+="$(printf ' %-12s' 'UPDATED')"; hfmt+=" %-12s"

    printf "${DIM}${hfmt}${NC}\n" "${header[@]}"
    printf "${DIM}${hfmt}${NC}\n" "${header[@]//[^ ]/-}"

    # Rows
    echo "$repos" | jq -c '.[]' | while IFS= read -r repo; do
        local name vis vis_color status status_color type lang size_str updated
        name=$(echo "$repo" | jq -r '.name')
        local private
        private=$(echo "$repo" | jq -r '.private')
        if [[ "$private" == "true" ]]; then vis="🔒 priv"; vis_color="$RED"; else vis="🌍 pub"; vis_color="$GREEN"; fi

        local archived
        archived=$(echo "$repo" | jq -r '.archived')
        if [[ "$archived" == "true" ]]; then status="📦 arch"; status_color="$YELLOW"; else status="✅ actv"; status_color="$GREEN"; fi

        local is_fork
        is_fork=$(echo "$repo" | jq -r '.fork')
        [[ "$is_fork" == "true" ]] && type="🍴fork" || type="📄src"

        lang=$(echo "$repo" | jq -r '.language // "—"' | cut -c1-9)
        local size_kb; size_kb=$(echo "$repo" | jq -r '.size'); size_str=$(format_size "$size_kb")
        updated=$(echo "$repo" | jq -r '.updated_at | split("T")[0]')

        local is_stale=false
        if [[ "$SHOW_STALE" == true ]]; then is_stale=$(echo "$repo" | jq -r '.is_stale // false'); fi
        local name_color="$NC"; [[ "$is_stale" == true ]] && name_color="$MAGENTA"

        [[ ${#name} -gt 28 ]] && name="${name:0:25}..."

        local row=""
        row+="$(printf "${name_color}%-30s${NC}" "$name")"
        row+="$(printf " ${vis_color}%-10s${NC}" "$vis")"
        row+="$(printf " ${status_color}%-10s${NC}" "$status")"
        row+="$(printf " %-8s" "$type")"

        if [[ "$SHOW_POPULARITY" == true ]]; then
            local stars forks watchers
            stars=$(echo "$repo" | jq -r '.stargazers_count')
            forks=$(echo "$repo" | jq -r '.forks_count')
            watchers=$(echo "$repo" | jq -r '.watchers_count')
            row+="$(printf " %-6s" "${stars}")"; row+="$(printf " %-6s" "${forks}")"; row+="$(printf " %-6s" "${watchers}")"
        fi

        row+="$(printf " %-10s" "$lang")"; row+="$(printf " %-8s" "$size_str")"

        if [[ "$SHOW_HEALTH" == true ]]; then
            local issues open_prs last_commit
            issues=$(echo "$repo" | jq -r '.open_issues_count')
            open_prs=$(echo "$repo" | jq -r '.open_prs // "?"')
            last_commit=$(echo "$repo" | jq -r '.last_commit // .pushed_at // .updated_at')
            row+="$(printf " %-10s" "${issues}")"; row+="$(printf " %-8s" "${open_prs}")"
            row+="$(printf " %-12s" "$(format_duration "$last_commit")")"
        fi

        if [[ "$SHOW_LICENSE" == true ]]; then row+="$(printf " %-12s" "$(echo "$repo" | jq -r '.license // "NONE"' | cut -c1-10)")"; fi
        if [[ "$SHOW_CI" == true ]]; then
            local has_ci; has_ci=$(echo "$repo" | jq -r '.has_ci // false')
            if [[ "$has_ci" == true ]]; then row+="$(printf " ${GREEN}%-6s${NC}" "✅")"; else row+="$(printf " ${RED}%-6s${NC}" "❌")"; fi
        fi
        if [[ "$SHOW_UNPROTECTED" == true ]]; then
            local protected; protected=$(echo "$repo" | jq -r '.branch_protected // false')
            if [[ "$protected" == true ]]; then row+="$(printf " ${GREEN}%-10s${NC}" "🛡 yes")"; else row+="$(printf " ${RED}%-10s${NC}" "⚠️ no")"; fi
        fi
        if [[ "$SHOW_FEATURES" == true ]]; then
            local feats=""
            [[ $(echo "$repo" | jq -r '.has_issues') == true ]] && feats+="I"
            [[ $(echo "$repo" | jq -r '.has_wiki') == true ]] && feats+="W"
            [[ $(echo "$repo" | jq -r '.has_pages // false') == true ]] && feats+="P"
            [[ $(echo "$repo" | jq -r '.has_projects') == true ]] && feats+="X"
            row+="$(printf " %-10s" "$feats")"
        fi

        row+="$(printf " %-12s" "$updated")"
        echo -e "$row"
    done

    if [[ "$SHOW_STALE" == true ]]; then echo -e "\n  ${MAGENTA}■${NC} Magenta name = stale (no update in ${STALE_DAYS}+ days)"; fi
    if [[ "$SHOW_FEATURES" == true ]]; then echo -e "  Features: I=Issues W=Wiki P=Pages X=Projects"; fi
}

# -------------------------------------------------------------------
# CSV OUTPUT
# -------------------------------------------------------------------
print_csv() {
    local repos="$1"
    local owner="$2"
    local filename="repos_${owner}.csv"
    log_msg "INFO" "Exporting CSV to ${filename}"

    echo "$repos" | jq -r '.[] | [
        .name, .full_name,
        (if .private then "private" else "public" end),
        (if .archived then "archived" else "active" end),
        (if .fork then "fork" else "source" end),
        (.language // "none"), .size,
        (.default_branch // "none"),
        (.updated_at | split("T")[0]), .html_url,
        (.stargazers_count // ""), (.forks_count // ""), (.watchers_count // ""),
        (.open_issues_count // ""), (.open_prs // ""), (.last_commit // ""),
        (.license // ""), (.has_ci // ""), (.branch_protected // ""), (.is_stale // "")
    ] | @csv' > "$filename"

    echo -e "  ${GREEN}✅ CSV exported to ${filename}${NC}"
}

# -------------------------------------------------------------------
# JSON OUTPUT
# -------------------------------------------------------------------
print_json() {
    local repos="$1"
    local owner="$2"
    local filename="repos_${owner}.json"
    log_msg "INFO" "Exporting JSON to ${filename}"

    echo "$repos" | jq '.' > "$filename"
    echo -e "  ${GREEN}✅ JSON exported to ${filename}${NC}"
    echo "$repos" | jq '{total: length, repos: [.[] | {name, full_name, visibility: (if .private then "private" else "public" end), status: (if .archived then "archived" else "active" end), language: (.language // "none"), updated: (.updated_at | split("T")[0]), url: .html_url}]}'
}

# -------------------------------------------------------------------
# MARKDOWN OUTPUT
# -------------------------------------------------------------------
print_markdown() {
    local repos="$1"
    local title="$2"
    local owner="$3"
    local filename="repos_${owner}.md"
    log_msg "INFO" "Exporting Markdown to ${filename}"

    echo "# ${title}" > "$filename"
    echo "" >> "$filename"
    echo "_Generated: $(date)_ | _Owner: ${owner}_" >> "$filename"
    echo "" >> "$filename"

    local total public private active archived forks sources
    total=$(echo "$repos" | jq 'length')
    public=$(echo "$repos" | jq '[.[] | select(.private == false)] | length')
    private=$(echo "$repos" | jq '[.[] | select(.private == true)] | length')
    active=$(echo "$repos" | jq '[.[] | select(.archived == false)] | length')
    archived=$(echo "$repos" | jq '[.[] | select(.archived == true)] | length')
    forks=$(echo "$repos" | jq '[.[] | select(.fork == true)] | length')
    sources=$(echo "$repos" | jq '[.[] | select(.fork == false)] | length')

    echo "## Summary" >> "$filename"
    echo "" >> "$filename"
    echo "| Metric | Count |" >> "$filename"
    echo "|--------|-------|" >> "$filename"
    echo "| Total | ${total} |" >> "$filename"
    echo "| Public / Private | ${public} / ${private} |" >> "$filename"
    echo "| Active / Archived | ${active} / ${archived} |" >> "$filename"
    echo "| Source / Fork | ${sources} / ${forks} |" >> "$filename"
    echo "" >> "$filename"

    echo "## Repositories" >> "$filename"
    echo "" >> "$filename"

    local md_header="| Repo | Visibility | Status | Type | Language | Updated |"
    local md_separator="|------|-----------|--------|------|----------|---------|"

    if [[ "$SHOW_POPULARITY" == true ]]; then
        md_header="| Repo | Visibility | Status | Type | ★ | ⑂ | Language | Updated |"
        md_separator="|------|-----------|--------|------|---|----|----------|---------|"
    fi

    echo "$md_header" >> "$filename"
    echo "$md_separator" >> "$filename"

    echo "$repos" | jq -c '.[]' | while IFS= read -r repo; do
        local name url vis status rtype lang updated stars forks_count
        name=$(echo "$repo" | jq -r '.name')
        url=$(echo "$repo" | jq -r '.html_url')
        vis=$(echo "$repo" | jq -r 'if .private then "🔒 Private" else "🌍 Public" end')
        status=$(echo "$repo" | jq -r 'if .archived then "📦 Archived" else "✅ Active" end')
        rtype=$(echo "$repo" | jq -r 'if .fork then "🍴 Fork" else "📄 Source" end')
        lang=$(echo "$repo" | jq -r '.language // "—"' | cut -c1-12)
        updated=$(echo "$repo" | jq -r '.updated_at | split("T")[0]')

        if [[ "$SHOW_POPULARITY" == true ]]; then
            stars=$(echo "$repo" | jq -r '.stargazers_count')
            forks_count=$(echo "$repo" | jq -r '.forks_count')
            echo "| [${name}](${url}) | ${vis} | ${status} | ${rtype} | ${stars} | ${forks_count} | ${lang} | ${updated} |" >> "$filename"
        else
            echo "| [${name}](${url}) | ${vis} | ${status} | ${rtype} | ${lang} | ${updated} |" >> "$filename"
        fi
    done

    echo -e "  ${GREEN}✅ Markdown exported to ${filename}${NC}"
    cat "$filename"
}

# ============================================================
# ANALYSIS SECTIONS
# ============================================================

print_language_breakdown() {
    local repos="$1"
    local title="$2"

    echo ""
    echo -e "${BOLD}${CYAN}── Language Breakdown: ${title} ──${NC}" | tee_to_log
    echo ""

    echo "$repos" | jq -r '
        group_by(.language // "None") |
        .[] |
        {language: .[0].language // "None", count: length, repos: [.[] | .name]}
    ' | jq -s 'sort_by(-.count)' | jq -r '.[] | "  \(.language): \(.count) repos"' | tee_to_log

    echo ""
    local max_count
    max_count=$(echo "$repos" | jq '[group_by(.language // "None") | .[] | length] | max')

    echo "$repos" | jq -r '
        group_by(.language // "None") |
        .[] |
        {language: .[0].language // "None", count: length}
    ' | jq -s 'sort_by(-.count)' | while IFS= read -r lang_obj; do
        local lang count
        lang=$(echo "$lang_obj" | jq -r '.language')
        count=$(echo "$lang_obj" | jq -r '.count')
        local bar_width=$(( count * 40 / max_count ))
        local bar; bar=$(printf '█%.0s' $(seq 1 "$bar_width" 2>/dev/null) || echo "")
        printf "  %-14s %3d %s\n" "$lang" "$count" "$bar"
    done | tee_to_log
}

print_stale_report() {
    local repos="$1"
    local title="$2"

    local stale_repos
    stale_repos=$(echo "$repos" | jq '[.[] | select(.is_stale == true)]')
    local stale_count; stale_count=$(echo "$stale_repos" | jq 'length')

    log_msg "INFO" "Stale report for ${title}: ${stale_count} stale repos detected"

    echo ""
    echo -e "${BOLD}${MAGENTA}── Stale Repos (>${STALE_DAYS} days): ${title} ──${NC}" | tee_to_log
    echo ""

    if [[ "$stale_count" -eq 0 ]]; then
        echo -e "  ${GREEN}No stale repos found! All repos are active.${NC}" | tee_to_log
        return
    fi

    echo -e "  ${YELLOW}${stale_count} stale repos detected:${NC}" | tee_to_log
    echo ""

    echo "$stale_repos" | jq -c '.[]' | while IFS= read -r repo; do
        local name updated visibility
        name=$(echo "$repo" | jq -r '.name')
        updated=$(echo "$repo" | jq -r '.updated_at | split("T")[0]')
        visibility=$(echo "$repo" | jq -r 'if .private then "private" else "public" end')
        printf "  ${MAGENTA}%-35s${NC} last updated: ${YELLOW}%s${NC} (%s)\n" "$name" "$updated" "$visibility"
    done | tee_to_log

    local active_stale
    active_stale=$(echo "$stale_repos" | jq '[.[] | select(.archived == false)]')
    local active_stale_count; active_stale_count=$(echo "$active_stale" | jq 'length')

    if [[ "$active_stale_count" -gt 0 ]]; then
        echo ""
        echo -e "  ${DIM}${active_stale_count} of these are NOT yet archived. Consider archiving:${NC}" | tee_to_log
        echo "$active_stale" | jq -r '.[] | "    gh repo archive \(.full_name) --yes"' | head -5 | tee_to_log
        [[ "$active_stale_count" -gt 5 ]] && echo "    ... and $((active_stale_count - 5)) more" | tee_to_log
    fi
}

print_unprotected_report() {
    local repos="$1"
    local title="$2"

    local unprotected
    unprotected=$(echo "$repos" | jq '[.[] | select(.branch_protected == false and .archived == false)]')
    local count; count=$(echo "$unprotected" | jq 'length')

    log_msg "WARN" "Unprotected branches report for ${title}: ${count} repos unprotected"

    echo ""
    echo -e "${BOLD}${RED}── Unprotected Default Branches: ${title} ──${NC}" | tee_to_log
    echo ""

    if [[ "$count" -eq 0 ]]; then
        echo -e "  ${GREEN}All active repos have branch protection! 🛡️${NC}" | tee_to_log
        return
    fi

    echo -e "  ${RED}${count} repos without branch protection:${NC}" | tee_to_log
    echo ""

    echo "$unprotected" | jq -c '.[]' | while IFS= read -r repo; do
        local name branch
        name=$(echo "$repo" | jq -r '.full_name')
        branch=$(echo "$repo" | jq -r '.checked_branch // .default_branch // "main"')
        printf "  ${RED}⚠️  %-40s${NC} branch: %s\n" "$name" "$branch"
    done | tee_to_log

    echo ""
    echo -e "  ${DIM}To enable protection:${NC}" | tee_to_log
    echo "$unprotected" | jq -r '.[] | "    gh api -X PUT repos/\(.full_name)/branches/\(.checked_branch // .default_branch // "main")/protection -f required_status_checks=null -f enforce_admins=true -f required_pull_request_reviews=null -f restrictions=null"' | head -3 | tee_to_log
    [[ "$count" -gt 3 ]] && echo "    ... and $((count - 3)) more" | tee_to_log
}

print_ci_report() {
    local repos="$1"
    local title="$2"

    local no_ci
    no_ci=$(echo "$repos" | jq '[.[] | select(.has_ci == false and .archived == false and .fork == false)]')
    local count; count=$(echo "$no_ci" | jq 'length')

    log_msg "INFO" "CI report for ${title}: ${count} source repos without GitHub Actions"

    echo ""
    echo -e "${BOLD}${BLUE}── CI/CD Detection (GitHub Actions): ${title} ──${NC}" | tee_to_log
    echo ""

    if [[ "$count" -eq 0 ]]; then
        echo -e "  ${GREEN}All source repos have GitHub Actions! 🚀${NC}" | tee_to_log
        return
    fi

    echo -e "  ${YELLOW}${count} source repos without GitHub Actions workflows:${NC}" | tee_to_log
    echo ""

    echo "$no_ci" | jq -c '.[]' | while IFS= read -r repo; do
        local name lang
        name=$(echo "$repo" | jq -r '.full_name')
        lang=$(echo "$repo" | jq -r '.language // "unknown"')
        printf "  %-45s %s\n" "$name" "$lang"
    done | tee_to_log
}

print_license_report() {
    local repos="$1"
    local title="$2"

    echo ""
    echo -e "${BOLD}${CYAN}── License Report: ${title} ──${NC}" | tee_to_log
    echo ""

    local no_license
    no_license=$(echo "$repos" | jq '[.[] | select(.license == "NONE" or .license == "NOASSERTION" or .license == null)]')
    local count; count=$(echo "$no_license" | jq 'length')

    log_msg "INFO" "License report for ${title}: ${count} repos without a clear license"

    echo "  License distribution:" | tee_to_log
    echo "$repos" | jq -r '
        group_by(.license // "NONE") |
        .[] |
        {license: .[0].license // "NONE", count: length}
    ' | jq -s 'sort_by(-.count)' | jq -r '.[] | "    \(.license): \(.count)"' | tee_to_log

    if [[ "$count" -gt 0 ]]; then
        echo ""
        echo -e "  ${YELLOW}${count} repos without a clear license:${NC}" | tee_to_log
        echo "$no_license" | jq -r '.[] | "    \(.full_name) (\(.visibility // (if .private then "private" else "public" end)))"' | head -10 | tee_to_log
        [[ "$count" -gt 10 ]] && echo "    ... and $((count - 10)) more" | tee_to_log
    fi
}

print_summary() {
    local repos="$1"
    local title="$2"

    local total
    total=$(echo "$repos" | jq 'length')

    if [[ "$total" -eq 0 ]]; then
        log_msg "WARN" "No repositories to summarize for ${title}"
        echo -e "  ${DIM}No repositories to summarize.${NC}"
        return
    fi

    local public private active archived forks sources
    public=$(echo "$repos" | jq '[.[] | select(.private == false)] | length')
    private=$(echo "$repos" | jq '[.[] | select(.private == true)] | length')
    active=$(echo "$repos" | jq '[.[] | select(.archived == false)] | length')
    archived=$(echo "$repos" | jq '[.[] | select(.archived == true)] | length')
    forks=$(echo "$repos" | jq '[.[] | select(.fork == true)] | length')
    sources=$(echo "$repos" | jq '[.[] | select(.fork == false)] | length')

    log_msg "INFO" "Summary for ${title}: Total=${total}, Public=${public}, Private=${private}, Active=${active}, Archived=${archived}, Source=${sources}, Forks=${forks}"

    echo ""
    echo -e "  ${BOLD}📊 Summary for ${title}:${NC}" | tee_to_log
    echo -e "  ┌─────────────────────────────────────┐" | tee_to_log
    echo -e "  │ Total repos:       ${BOLD}${total}${NC}" | tee_to_log
    echo -e "  │ Public:            ${GREEN}${public}${NC}  │  Private:  ${RED}${private}${NC}" | tee_to_log
    echo -e "  │ Active:            ${GREEN}${active}${NC}  │  Archived: ${YELLOW}${archived}${NC}" | tee_to_log
    echo -e "  │ Source:            ${sources}  │  Forks:    ${forks}" | tee_to_log
    echo -e "  └─────────────────────────────────────┘" | tee_to_log

    local total_size avg_size
    total_size=$(echo "$repos" | jq '[.[].size] | add // 0')
    avg_size=$(echo "$repos" | jq --argjson t "$total_size" --argjson n "$total" 'if $n > 0 then ($t / $n | round) else 0 end')
    echo -e "  Total size: $(format_size "$total_size") | Avg: $(format_size "$avg_size")" | tee_to_log

    if [[ "$SHOW_POPULARITY" == true && "$total" -gt 0 ]]; then
        local top_starred
        top_starred=$(echo "$repos" | jq -r 'sort_by(-.stargazers_count) | .[0:5] | .[] | "\(.name) (\(.stargazers_count)★)"')
        echo -e "  ${BOLD}⭐ Top Starred:${NC}" | tee_to_log
        echo "$top_starred" | while IFS= read -r line; do echo -e "    $line" | tee_to_log; done

        local top_forked
        top_forked=$(echo "$repos" | jq -r 'sort_by(-.forks_count) | .[0:5] | .[] | "\(.name) (\(.forks_count) ⑂)"')
        echo -e "  ${BOLD}⑂ Most Forked:${NC}" | tee_to_log
        echo "$top_forked" | while IFS= read -r line; do echo -e "    $line" | tee_to_log; done
    fi
}

# ============================================================
# INTERACTIVE MODE
# ============================================================
interactive_menu() {
    local user_repos="$1"
    local org_repos="${2:-[]}"

    while true; do
        echo ""
        echo -e "${BOLD}${CYAN}╔══════════════════════════════════════╗${NC}"
        echo -e "${BOLD}${CYAN}║     📊 Interactive Repo Explorer     ║${NC}"
        echo -e "${BOLD}${CYAN}╚══════════════════════════════════════╝${NC}"
        echo ""
        echo "  1) View personal repos"
        [[ -n "$ORG" ]] && echo "  2) View org repos (${ORG})"
        echo "  3) Filter by visibility"
        echo "  4) Filter by language"
        echo "  5) Sort repos"
        echo "  6) Toggle: popularity (current: ${SHOW_POPULARITY})"
        echo "  7) Toggle: health (current: ${SHOW_HEALTH})"
        echo "  8) Toggle: stale detection (current: ${SHOW_STALE})"
        echo "  9) Toggle: license (current: ${SHOW_LICENSE})"
        echo "  10) Toggle: CI detection (current: ${SHOW_CI})"
        echo "  11) Toggle: branch protection (current: ${SHOW_UNPROTECTED})"
        echo "  12) Language breakdown"
        echo "  13) Stale report"
        echo "  14) Export CSV"
        echo "  15) Export JSON"
        echo "  16) Export Markdown"
        echo "  0) Exit"
        echo ""
        read -rp "  Select option: " choice

        log_msg "INFO" "Interactive mode: User selected option ${choice}"

        case "$choice" in
            1)
                print_table "$(apply_filters "$user_repos")" "Personal Repos (${GH_USER})"
                print_summary "$(apply_filters "$user_repos")" "$GH_USER"
                ;;
            2)
                [[ -n "$ORG" ]] && print_table "$(apply_filters "$org_repos")" "Org Repos (${ORG})" && print_summary "$(apply_filters "$org_repos")" "$ORG"
                ;;
            3)
                echo "  1) Public  2) Private  3) All"
                read -rp "  Select: " v
                [[ "$v" == "1" ]] && FILTER_VISIBILITY="public"
                [[ "$v" == "2" ]] && FILTER_VISIBILITY="private"
                [[ "$v" == "3" ]] && FILTER_VISIBILITY=""
                log_msg "INFO" "Interactive: Visibility filter set to ${FILTER_VISIBILITY:-all}"
                ;;
            4)
                read -rp "  Language: " FILTER_LANG
                log_msg "INFO" "Interactive: Language filter set to ${FILTER_LANG}"
                ;;
            5)
                echo "  Sort by: name, updated, stars, forks, size, issues, language"
                read -rp "  Field: " SORT_BY
                log_msg "INFO" "Interactive: Sort set to ${SORT_BY}"
                ;;
            6) SHOW_POPULARITY=$( [[ "$SHOW_POPULARITY" == true ]] && echo false || echo true ); log_msg "DEBUG" "Toggled popularity to ${SHOW_POPULARITY}" ;;
            7) SHOW_HEALTH=$( [[ "$SHOW_HEALTH" == true ]] && echo false || echo true ); log_msg "DEBUG" "Toggled health to ${SHOW_HEALTH}" ;;
            8) SHOW_STALE=$( [[ "$SHOW_STALE" == true ]] && echo false || echo true ); log_msg "DEBUG" "Toggled stale to ${SHOW_STALE}" ;;
            9) SHOW_LICENSE=$( [[ "$SHOW_LICENSE" == true ]] && echo false || echo true ); log_msg "DEBUG" "Toggled license to ${SHOW_LICENSE}" ;;
            10) SHOW_CI=$( [[ "$SHOW_CI" == true ]] && echo false || echo true ); log_msg "DEBUG" "Toggled CI to ${SHOW_CI}" ;;
            11) SHOW_UNPROTECTED=$( [[ "$SHOW_UNPROTECTED" == true ]] && echo false || echo true ); log_msg "DEBUG" "Toggled unprotected to ${SHOW_UNPROTECTED}" ;;
            12)
                print_language_breakdown "$(apply_filters "$user_repos")" "$GH_USER"
                [[ -n "$ORG" ]] && print_language_breakdown "$(apply_filters "$org_repos")" "$ORG"
                ;;
            13)
                SHOW_STALE=true
                user_repos=$(mark_stale "$user_repos")
                print_stale_report "$(apply_filters "$user_repos")" "$GH_USER"
                ;;
            14)
                print_csv "$(apply_filters "$user_repos")" "${GH_USER}"
                [[ -n "$ORG" ]] && print_csv "$(apply_filters "$org_repos")" "$ORG"
                ;;
            15)
                print_json "$(apply_filters "$user_repos")" "${GH_USER}"
                [[ -n "$ORG" ]] && print_json "$(apply_filters "$org_repos")" "$ORG"
                ;;
            16)
                print_markdown "$(apply_filters "$user_repos")" "Personal Repos" "${GH_USER}"
                [[ -n "$ORG" ]] && print_markdown "$(apply_filters "$org_repos")" "Org Repos" "$ORG"
                ;;
            0) log_msg "INFO" "Interactive mode: User exited"; echo "Bye!"; exit 0 ;;
            *) log_msg "WARN" "Invalid interactive option: ${choice}"; echo "Invalid option" ;;
        esac
    done
}

# ============================================================
# MAIN
# ============================================================

echo ""
echo -e "${BOLD}${CYAN}╔════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║     📊 GitHub Repo State Lister — v3.0        ║${NC}"
echo -e "${BOLD}${CYAN}╚════════════════════════════════════════════════╝${NC}"

echo -e "  Authenticated as: ${BOLD}${GH_USER}${NC}" | tee_to_log
[[ -n "$ORG" ]] && echo -e "  Organization:     ${BOLD}${ORG}${NC}" | tee_to_log
echo -e "  Output format:    ${OUTPUT_FORMAT}" | tee_to_log
echo -e "  Log File:         ${LOG_FILE:-disabled}" | tee_to_log
echo -e "  Filters:          visibility=${FILTER_VISIBILITY:-all} status=${FILTER_STATUS:-all} type=${FILTER_TYPE:-all} lang=${FILTER_LANG:-all} min_stars=${MIN_STARS_FILTER}" | tee_to_log
echo -e "  Sort:             ${SORT_BY}" | tee_to_log
echo -e "  Features:         health=${SHOW_HEALTH} languages=${SHOW_LANGUAGES} license=${SHOW_LICENSE} ci=${SHOW_CI} stale=${SHOW_STALE} popularity=${SHOW_POPULARITY} features=${SHOW_FEATURES} unprotected=${SHOW_UNPROTECTED}" | tee_to_log

check_rate_limit

# -------------------------------------------------------
# FETCH PERSONAL REPOS
# -------------------------------------------------------
echo "" | tee_to_log
echo -e "${BOLD}📦 Fetching personal repos for ${GH_USER}...${NC}" | tee_to_log
USER_JSON=$(fetch_repos "$GH_USER" "user")
USER_JSON=$(sort_repos "$USER_JSON" "$SORT_BY")
USER_JSON=$(apply_filters "$USER_JSON")

if [[ "$SHOW_STALE" == true ]]; then
    USER_JSON=$(mark_stale "$USER_JSON")
fi

USER_JSON=$(enrich_repos "$USER_JSON" "$GH_USER")

# -------------------------------------------------------
# FETCH ORG REPOS
# -------------------------------------------------------
ORG_JSON="[]"
if [[ -n "$ORG" ]]; then
    echo "" | tee_to_log
    echo -e "${BOLD}🏢 Fetching org repos for ${ORG}...${NC}" | tee_to_log
    ORG_JSON=$(fetch_repos "$ORG" "org")
    ORG_JSON=$(sort_repos "$ORG_JSON" "$SORT_BY")
    ORG_JSON=$(apply_filters "$ORG_JSON")

    if [[ "$SHOW_STALE" == true ]]; then
        ORG_JSON=$(mark_stale "$ORG_JSON")
    fi

    ORG_JSON=$(enrich_repos "$ORG_JSON" "$ORG")
fi

# -------------------------------------------------------
# INTERACTIVE MODE
# -------------------------------------------------------
if [[ "$INTERACTIVE" == true ]]; then
    log_msg "INFO" "Entering interactive mode"
    interactive_menu "$USER_JSON" "$ORG_JSON"
    exit 0
fi

# -------------------------------------------------------
# OUTPUT
# -------------------------------------------------------

case "$OUTPUT_FORMAT" in
    table)
        print_table "$USER_JSON" "Personal Repos (${GH_USER})"
        print_summary "$USER_JSON" "$GH_USER"

        if [[ -n "$ORG" ]]; then
            print_table "$ORG_JSON" "Org Repos (${ORG})"
            print_summary "$ORG_JSON" "$ORG"
        fi
        ;;
    csv)
        print_csv "$USER_JSON" "${GH_USER}"
        [[ -n "$ORG" ]] && print_csv "$ORG_JSON" "$ORG"
        ;;
    json)
        print_json "$USER_JSON" "${GH_USER}"
        [[ -n "$ORG" ]] && print_json "$ORG_JSON" "$ORG"
        ;;
    md|markdown)
        print_markdown "$USER_JSON" "Personal Repositories" "${GH_USER}"
        [[ -n "$ORG" ]] && print_markdown "$ORG_JSON" "Organization Repositories" "$ORG"
        ;;
    *)
        log_msg "ERROR" "Unknown format: ${OUTPUT_FORMAT}"
        echo "Unknown format: $OUTPUT_FORMAT. Use table, csv, json, or md."
        exit 1
        ;;
esac

# -------------------------------------------------------
# FEATURE REPORTS
# -------------------------------------------------------

if [[ "$SHOW_LANGUAGES" == true ]]; then
    print_language_breakdown "$USER_JSON" "$GH_USER"
    [[ -n "$ORG" ]] && print_language_breakdown "$ORG_JSON" "$ORG"
fi

if [[ "$SHOW_STALE" == true ]]; then
    print_stale_report "$USER_JSON" "$GH_USER"
    [[ -n "$ORG" ]] && print_stale_report "$ORG_JSON" "$ORG"
fi

if [[ "$SHOW_UNPROTECTED" == true ]]; then
    print_unprotected_report "$USER_JSON" "$GH_USER"
    [[ -n "$ORG" ]] && print_unprotected_report "$ORG_JSON" "$ORG"
fi

if [[ "$SHOW_CI" == true ]]; then
    print_ci_report "$USER_JSON" "$GH_USER"
    [[ -n "$ORG" ]] && print_ci_report "$ORG_JSON" "$ORG"
fi

if [[ "$SHOW_LICENSE" == true ]]; then
    print_license_report "$USER_JSON" "$GH_USER"
    [[ -n "$ORG" ]] && print_license_report "$ORG_JSON" "$ORG"
fi

# -------------------------------------------------------
# COMBINED OVERVIEW
# -------------------------------------------------------
if [[ -n "$ORG" ]]; then
    COMBINED=$(echo "$USER_JSON" "$ORG_JSON" | jq -s 'add')
    print_summary "$COMBINED" "Combined (${GH_USER} + ${ORG})"
fi

echo "" | tee_to_log
check_rate_limit

log_msg "INFO" "============================================================"
log_msg "INFO" " Session completed successfully"
log_msg "INFO" "============================================================"

echo -e "${BOLD}✅ Done!${NC} Log saved to: ${LOG_FILE:-disabled}" | tee_to_log