# -*- coding: utf-8 -*-
"""
Facebook Page Follower Tracker v6.1 - PyQt6 modern UI

Features
- Normal visible Edge/Chrome login with a persistent local browser profile.
- Automatic reading of visibly rendered Facebook Page follower counts.
- SQLite history and growth calculation.
- Growth alert uses the same time interval as collection; only the growth threshold is configured separately.
- Built-in bell or user-selected local audio file.
- Modern PyQt6 interface with system-tray notification.
- Live result sync with the Page ID list, per-monitoring-run Page alert muting, and double-click copy.

Notes
- The app does not bypass Facebook login/security challenges or access controls.
- Facebook can change its page layout/text; parser updates may occasionally be needed.
- If Facebook shows an abbreviated number such as 38万 / 17K, the stored count is approximate.
"""

import csv
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from PyQt6.QtCore import QObject, QSettings, QThread, QTimer, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QCloseEvent, QFont, QIcon, QPalette
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QPushButton,
    QSpinBox,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

APP_TITLE = "Facebook 专页粉丝增长监控"
APP_VERSION = "6.0 PyQt6"
FACEBOOK_HOME = "https://www.facebook.com/"
MIN_INTERVAL_SECONDS = 60
PAGE_GAP_SECONDS = 1.5
PAGE_LOAD_TIMEOUT_MS = 45000


def ensure_private_directory(path: Path) -> Path:
    """Protect browser sessions/history from other POSIX users; Windows uses ACLs."""
    if path.is_symlink():
        raise RuntimeError("数据目录不能是符号链接")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        path.chmod(0o700)
    return path


def safe_csv_cell(value):
    """Keep untrusted text literal when imported by spreadsheet applications."""
    if isinstance(value, str):
        probe = value.lstrip("\ufeff \t\r\n\v\f")
        if probe.startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
            return "'" + value
    return value


def diagnostic_url(value):
    """Never include credentials, query parameters or fragments in diagnostics."""
    url = urlsplit(value)
    return urlunsplit((url.scheme, url.hostname or "", url.path, "", ""))


def app_data_dir() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "FacebookFollowerTracker"
    else:
        base = Path.home() / ".facebook_follower_tracker"
    return ensure_private_directory(base)


DATA_DIR = app_data_dir()
DB_PATH = DATA_DIR / "fb_follower_tracker.db"
BROWSER_PROFILE_DIR = DATA_DIR / "browser_profile"
ensure_private_directory(BROWSER_PROFILE_DIR)


def resource_path(relative: str) -> Path:
    """Works from source and from a PyInstaller bundle."""
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS) / relative
    return Path(__file__).resolve().parent / relative


BUILTIN_SOUND = resource_path("assets/bell.wav")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_page_ids(text: str):
    parts = re.split(r"[\s,;，；]+", text.strip())
    ids = []
    seen = set()
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if not re.fullmatch(r"\d+", p):
            raise ValueError(f"专页 ID 必须是纯数字：{p}")
        if p not in seen:
            ids.append(p)
            seen.add(p)
    return ids


def unit_seconds(value: int, unit: str) -> int:
    mapping = {"秒": 1, "分钟": 60, "小时": 3600, "天": 86400}
    return int(value) * mapping[unit]


def format_delta(value):
    if value is None or value == "":
        return "—"
    value = int(value)
    return f"+{value:,}" if value > 0 else f"{value:,}"


def parse_human_number(raw):
    s = str(raw).strip().lower().replace("\u00a0", " ").replace("，", ",")
    multiplier = 1
    suffix_patterns = [
        (r"\s*(?:k|千)$", 1_000),
        (r"\s*(?:m|million|millions|millón|millones|mio\.?|jt)$", 1_000_000),
        (r"\s*(?:b|billion|billions)$", 1_000_000_000),
        (r"\s*万$", 10_000),
        (r"\s*亿$", 100_000_000),
        (r"\s*(?:rb|ribu)$", 1_000),
        (r"\s*mil$", 1_000),
    ]
    for pat, mult in suffix_patterns:
        if re.search(pat, s, flags=re.I):
            multiplier = mult
            s = re.sub(pat, "", s, flags=re.I).strip()
            break

    s = re.sub(r"[^0-9.,]", "", s)
    if not s:
        raise ValueError("未识别到数字")

    if multiplier == 1:
        digits = re.sub(r"[.,]", "", s)
        if not digits.isdigit():
            raise ValueError(f"无法解析数字：{raw}")
        return int(digits), False

    if "," in s and "." in s:
        decimal_sep = "," if s.rfind(",") > s.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        s = s.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif "," in s:
        tail = s.split(",")[-1]
        s = s.replace(",", "." if len(tail) <= 2 else "")
    elif "." in s:
        tail = s.split(".")[-1]
        if len(tail) > 2:
            s = s.replace(".", "")

    return int(round(float(s) * multiplier)), True


FOLLOWER_PATTERNS = [
    r"(?P<num>[0-9][0-9.,\s]*(?:[KkMmBb])?)\s+(?:followers?|Follower)",
    r"(?P<num>[0-9][0-9.,\s]*(?:万|亿|[KkMm])?)\s*(?:位)?粉丝",
    r"(?P<num>[0-9][0-9.,\s]*(?:rb|ribu|jt|[KkMm])?)\s+(?:pengikut)",
    r"(?P<num>[0-9][0-9.,\s]*(?:mil|millones?|[KkMm])?)\s+(?:seguidores?)",
    r"(?P<num>[0-9][0-9.,\s]*(?:k|m|millions?)?)\s+(?:abonnés?)",
    r"(?P<num>[0-9][0-9.,\s]*(?:Tsd\.?|Mio\.?|[KkMm])?)\s+(?:Follower)",
    r"(?P<num>[0-9][0-9.,\s]*(?:mila|milioni?|[KkMm])?)\s+(?:follower)",
    r"(?P<num>[0-9][0-9.,\s]*(?:bin|milyon|[KkMm])?)\s+(?:takipçi)",
]


def normalize_locale_suffix(num_text):
    s = num_text.strip()
    replacements = {
        "Tsd.": "K", "Tsd": "K", "mila": "K", "bin": "K",
        "Mio.": "M", "Mio": "M", "milyon": "M", "milioni": "M", "milione": "M",
    }
    for old, new in replacements.items():
        if s.lower().endswith(old.lower()):
            return s[: -len(old)] + new
    return s


def extract_followers_from_text(text):
    candidates = []
    for pattern in FOLLOWER_PATTERNS:
        for m in re.finditer(pattern, text, flags=re.I):
            raw = normalize_locale_suffix(m.group("num"))
            try:
                count, approx = parse_human_number(raw)
            except Exception:
                continue
            if 0 <= count <= 10_000_000_000:
                candidates.append((m.start(), count, approx, m.group(0).strip()))
    if not candidates:
        raise RuntimeError("网页中没有识别到粉丝数。可能是未登录、页面未完整加载，或 Facebook 页面文字格式已变化。")
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1], candidates[0][2], candidates[0][3]


def clean_page_name(raw):
    if not raw:
        return ""
    s = re.sub(r"\s+", " ", str(raw)).strip()
    s = re.sub(r"^\(\d+\)\s*", "", s).strip()
    s = re.sub(r"\s*[|\-–—·]\s*Facebook\s*$", "", s, flags=re.I).strip()
    s = re.sub(r"\s*\|\s*Meta\s*$", "", s, flags=re.I).strip()
    s = re.sub(
        r"\s*[|\-–—·]\s*(?:Home|Posts|About|Photos|Videos|Reels|主页|首页|帖子|关于|照片|视频|短视频)\s*$",
        "", s, flags=re.I,
    ).strip()
    return s if len(s) <= 180 else ""


GENERIC_UI_NAMES = {
    "facebook", "meta", "home", "首页", "主页", "通知", "消息", "菜单", "搜索", "好友", "视频", "游戏", "群组",
    "动态消息", "动态", "创建", "更多", "个人主页", "设置", "设置和隐私", "帮助和支持", "查看全部", "通知中心",
    "赞", "点赞", "关注", "已关注", "正在关注", "发消息", "简介", "关于", "帖子", "照片", "短视频", "提及", "点评", "社区",
    "直播", "分享", "管理", "切换", "搜索 facebook", "notifications", "notification", "menu", "search", "friends",
    "watch", "marketplace", "groups", "gaming", "messenger", "messages", "settings", "like", "liked", "follow",
    "following", "message", "about", "posts", "photos", "videos", "reels", "mentions", "reviews", "community", "live",
    "share", "log in", "login", "sign up", "register", "详细信息", "详情", "更多信息", "专页详情", "页面详情",
    "个人资料详情", "个人资料详细信息", "公开详情", "基本信息", "信息", "介绍", "联系方式", "details", "detail",
    "page details", "profile details", "public details", "more information", "additional information", "contact info", "information",
    "facebook - log in or sign up", "facebook – log in or sign up", "facebook - 登录或注册", "facebook – 登录或注册",
    "全部", "所有", "全部内容", "查看全部", "显示全部", "全部帖子", "全部动态", "all", "all posts",
    "see all", "show all", "overview", "timeline", "精选", "筛选", "排序", "最近", "最相关", "热门",
    "posts by", "more", "更多内容", "管理帖子", "筛选条件",
}

BAD_NAME_FRAGMENTS = [
    "followers", "follower", "粉丝", "pengikut", "seguidores", "abonn", "following", "likes", "people like this",
    "people follow this", "登录", "注册", "log in", "sign up", "notification", "通知中心", "详细信息", "个人资料详细信息",
    "公开详情", "page details", "profile details",
]

# Facebook 的页头区域同时会出现“关注 360 人 / 关注 0 人 / 1 人关注”等统计或操作文字。
# 这些文字字体较大，旧版本有时会把它们误判成专页名称。
STAT_OR_ACTION_PATTERNS = [
    r"^(?:已?关注|正在关注|关注了|关注中)\s*[0-9０-９][0-9０-９.,，\s万亿千kmbKMB]*\s*(?:人|位|个)?$",
    r"^[0-9０-９][0-9０-９.,，\s万亿千kmbKMB]*\s*(?:人|位|个)?\s*(?:已?关注|正在关注|关注中)$",
    r"^(?:following|followed by)\s*[0-9][0-9.,\sKkMmBb]*\s*(?:people|persons?)?$",
    r"^[0-9][0-9.,\sKkMmBb]*\s*(?:people\s+)?following$",
    r"^[0-9０-９][0-9０-９.,，\s万亿千kmbKMB]*\s*(?:人|位|people|persons?)$",
    r"^(?:赞|点赞|likes?)\s*[0-9０-９][0-9０-９.,，\s万亿千kmbKMB]*\s*(?:人|位|个)?$",
]


def looks_like_stat_or_action(value: str) -> bool:
    s = clean_page_name(value)
    if not s:
        return True
    compact = re.sub(r"\s+", " ", s).strip()
    for pat in STAT_OR_ACTION_PATTERNS:
        if re.fullmatch(pat, compact, flags=re.I):
            return True

    # 只要包含明显的数字统计 + UI 关键词，也不要作为名称。
    has_number = bool(re.search(r"[0-9０-９]", compact))
    if has_number and re.search(
        r"(?:关注|正在关注|已关注|粉丝|点赞|赞|followers?|following|likes?|people\s+follow)",
        compact,
        flags=re.I,
    ):
        return True
    return False


def looks_like_page_name(value, page_id=""):
    s = clean_page_name(value)
    if not s:
        return False
    low = s.lower().strip()
    if low in {x.lower() for x in GENERIC_UI_NAMES}:
        return False
    if any(x in low for x in BAD_NAME_FRAGMENTS):
        return False
    if looks_like_stat_or_action(s):
        return False
    if page_id and s == str(page_id):
        return False
    if re.search(r"https?://|www\.", low):
        return False
    if re.fullmatch(r"[\d\s.,:+\-/%万亿千kmb]+", low, flags=re.I):
        return False
    if not re.search(r"[A-Za-z\u00C0-\u024F\u4e00-\u9fff\u3040-\u30ff\u0E00-\u0E7F]", s):
        return False
    return True

def _meta_content(page, selector):
    try:
        return page.locator(selector).first.get_attribute("content", timeout=1200) or ""
    except Exception:
        return ""


def _clean_profile_alt(raw):
    s = clean_page_name(raw)
    if not s:
        return ""
    suffixes = [
        r"'s\s+profile\s+picture$", r"profile\s+picture$", r"'s\s+profile\s+photo$",
        r"profile\s+photo$", r"的头像$", r"的大头贴$", r"的个人头像$", r"头像$",
        r"foto de perfil$", r"photo de profil$", r"profilbild$", r"immagine del profilo$",
    ]
    for pat in suffixes:
        s = re.sub(pat, "", s, flags=re.I).strip()
    return clean_page_name(s)


def _strict_h1_candidates(page):
    """Return visible h1 text outside navigation/tab/dialog chrome, top first."""
    try:
        rows = page.locator("h1").evaluate_all(
            r"""
            (els) => {
              const out = [];
              for (const el of els) {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                if (!r || r.width < 2 || r.height < 2 || r.bottom < 0 || r.top > innerHeight * 2) continue;
                if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity || 1) === 0) continue;
                if (el.closest('[role="navigation"],[role="tablist"],[role="menu"],[role="dialog"]')) continue;
                const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
                if (!text || text.length > 180) continue;
                out.push({text, top:r.top, left:r.left, fs:parseFloat(st.fontSize || '0') || 0});
              }
              out.sort((a,b) => (a.top-b.top) || (b.fs-a.fs) || (a.left-b.left));
              return out.slice(0, 20);
            }
            """
        )
        return rows or []
    except Exception:
        return []


def _profile_alt_candidates(page):
    try:
        rows = page.locator('img[alt]').evaluate_all(
            r"""
            (els) => {
              const out = [];
              for (const el of els.slice(0, 300)) {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                if (!r || r.width < 28 || r.height < 28 || r.bottom < 0 || r.top > 1200) continue;
                if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity || 1) === 0) continue;
                if (el.closest('[role="navigation"],[role="menu"],[role="dialog"]')) continue;
                const alt = (el.getAttribute('alt') || '').replace(/\s+/g, ' ').trim();
                if (!alt || alt.length > 220) continue;
                const isProfile = /(?:'s\s+profile\s+(?:picture|photo)|profile\s+(?:picture|photo)|的头像|的大头贴|的个人头像|头像|foto de perfil|photo de profil|profilbild|immagine del profilo)$/i.test(alt);
                if (!isProfile) continue;
                out.push({text:alt, top:r.top, size:Math.max(r.width, r.height)});
              }
              out.sort((a,b) => (a.top-b.top) || (b.size-a.size));
              return out.slice(0, 30);
            }
            """
        )
        return rows or []
    except Exception:
        return []


def _self_link_candidates(page, page_id):
    try:
        return page.locator('a[href]').evaluate_all(
            r"""
            (els, pageId) => {
              const out = [];
              const id = String(pageId || '');
              const cur = new URL(location.href);
              const curPath = cur.pathname.replace(/\/+$/, '');
              for (const el of els.slice(0, 1200)) {
                if (el.closest('[role="navigation"],[role="tablist"],[role="menu"],[role="dialog"]')) continue;
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                if (!r || r.width < 2 || r.height < 2 || r.bottom < 0 || r.top > 1100) continue;
                if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity || 1) === 0) continue;
                const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
                if (!text || text.length > 180) continue;
                let u; try { u = new URL(el.href, location.href); } catch (_) { continue; }
                const path = u.pathname.replace(/\/+$/, '');
                const idHit = id && (path === '/' + id || path.startsWith('/' + id + '/'));
                const profileId = id && u.searchParams.get('id') === id;
                const samePath = curPath && path === curPath;
                if (!(idHit || profileId || samePath)) continue;
                out.push({
                  text,
                  top:r.top,
                  fs:parseFloat(st.fontSize || '0') || 0,
                  fw:parseInt(st.fontWeight || '400', 10) || 400,
                  strong:idHit || profileId
                });
              }
              out.sort((a,b) => (Number(b.strong)-Number(a.strong)) || (a.top-b.top) || (b.fs-a.fs) || (b.fw-a.fw));
              return out.slice(0, 60);
            }
            """,
            str(page_id or ""),
        ) or []
    except Exception:
        return []


def _near_follower_candidates(page, matched_text):
    """Find visible text just above/alongside the actual follower-count element."""
    if not matched_text:
        return []
    try:
        return page.locator('body').evaluate(
            r"""
            (body, needle) => {
              needle = String(needle || '').replace(/\s+/g, ' ').trim();
              if (!needle) return [];
              const all = Array.from(document.querySelectorAll('a,span,div'));
              const hits = [];
              for (const el of all.slice(0, 5000)) {
                if (el.closest('[role="navigation"],[role="tablist"],[role="menu"],[role="dialog"]')) continue;
                const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
                if (!text || text.length > 220) continue;
                if (!(text === needle || text.includes(needle))) continue;
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                if (!r || r.width < 2 || r.height < 2 || r.bottom < 0 || r.top > 1400) continue;
                if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity || 1) === 0) continue;
                hits.push({el, r});
              }
              if (!hits.length) return [];
              hits.sort((a,b) => (a.r.top-b.r.top) || (a.r.left-b.r.left));
              const target = hits[0].r;
              const out = [];
              const candidates = Array.from(document.querySelectorAll('h1,h2,[role="heading"],a,span,strong'));
              for (const el of candidates.slice(0, 5000)) {
                if (el.closest('[role="navigation"],[role="tablist"],[role="menu"],[role="dialog"]')) continue;
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                if (!r || r.width < 2 || r.height < 2) continue;
                if (r.top < target.top - 320 || r.top > target.bottom + 90) continue;
                if (Math.abs(r.left - target.left) > 900) continue;
                if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity || 1) === 0) continue;
                const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
                if (!text || text.length > 180 || text === needle) continue;
                const fs = parseFloat(st.fontSize || '0') || 0;
                const fw = parseInt(st.fontWeight || '400', 10) || 400;
                const distance = Math.max(0, target.top - r.bottom);
                out.push({text, distance, fs, fw, top:r.top});
              }
              out.sort((a,b) => (a.distance-b.distance) || (b.fs-a.fs) || (b.fw-a.fw));
              return out.slice(0, 80);
            }
            """,
            str(matched_text),
        ) or []
    except Exception:
        return []


def _wait_for_page_identity(page, timeout_ms=7000):
    """Give Facebook SPA a little time to replace generic chrome with Page identity."""
    deadline = time.monotonic() + max(0.5, timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        for row in _strict_h1_candidates(page):
            name = clean_page_name((row or {}).get("text", ""))
            if looks_like_page_name(name):
                return
        try:
            title = clean_page_name(page.title())
            if looks_like_page_name(title):
                return
        except Exception:
            pass
        try:
            page.wait_for_timeout(350)
        except Exception:
            break


def extract_page_name(page, body_text, page_id="", matched_text=""):
    """Extract the Page display name using identity-specific DOM before generic text.

    The old versions used a broad largest-text heuristic. Facebook UI words such as
    “通知”, “详细信息”, “关注 360 人”, and “全部” can be large too, so this version
    only trusts identity-bearing locations first and heavily filters navigation text.
    """
    _wait_for_page_identity(page, timeout_ms=4500)

    # 1) Facebook Pages normally expose the display name as a visible top-level h1.
    # Take the first valid h1 outside navigation/tab/dialog chrome.
    for row in _strict_h1_candidates(page):
        name = clean_page_name((row or {}).get("text", ""))
        if looks_like_page_name(name, page_id):
            return name

    # 2) The profile image accessible name is highly specific to the Page identity.
    for row in _profile_alt_candidates(page):
        name = _clean_profile_alt((row or {}).get("text", ""))
        if looks_like_page_name(name, page_id):
            return name

    # 3) A link that points back to the current numeric Page identity.
    for row in _self_link_candidates(page, page_id):
        name = clean_page_name((row or {}).get("text", ""))
        if looks_like_page_name(name, page_id):
            return name

    # 4) Open Graph/document metadata. These are useful but can remain generic in SPA views.
    for raw in [
        _meta_content(page, 'meta[property="og:title"]'),
        _meta_content(page, 'meta[name="twitter:title"]'),
    ]:
        name = clean_page_name(raw)
        if looks_like_page_name(name, page_id):
            return name
    try:
        name = clean_page_name(page.title())
        if looks_like_page_name(name, page_id):
            return name
    except Exception:
        pass

    # 5) Only search text physically near the follower count that we actually parsed.
    for row in _near_follower_candidates(page, matched_text):
        name = clean_page_name((row or {}).get("text", ""))
        if looks_like_page_name(name, page_id):
            return name

    # 6) Last resort: visible heading roles outside navigation. No generic large-text scan.
    try:
        rows = page.locator('[role="heading"]').evaluate_all(
            r"""
            (els) => {
              const out=[];
              for (const el of els.slice(0, 250)) {
                if (el.closest('[role="navigation"],[role="tablist"],[role="menu"],[role="dialog"]')) continue;
                const r=el.getBoundingClientRect(), st=getComputedStyle(el);
                if (!r || r.width<2 || r.height<2 || r.bottom<0 || r.top>1200) continue;
                if (st.visibility==='hidden' || st.display==='none' || Number(st.opacity||1)===0) continue;
                const text=(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim();
                if (!text || text.length>180) continue;
                const level = Number(el.getAttribute('aria-level') || 99);
                out.push({text, level, top:r.top, fs:parseFloat(st.fontSize||'0')||0});
              }
              out.sort((a,b)=>(a.level-b.level)||(a.top-b.top)||(b.fs-a.fs));
              return out.slice(0,80);
            }
            """
        )
        for row in rows or []:
            name = clean_page_name((row or {}).get("text", ""))
            if looks_like_page_name(name, page_id):
                return name
    except Exception:
        pass

    return ""


def save_name_debug(page, page_id, matched_text=""):
    """Save only selector/candidate diagnostics; never stores cookies or local storage."""
    try:
        if not re.fullmatch(r"[0-9]+", str(page_id)):
            raise ValueError("专页 ID 必须是 ASCII 数字")
        path = DATA_DIR / f"page_name_debug_{page_id}.txt"
        lines = [
            f"time: {now_iso()}",
            f"page_id: {page_id}",
            f"url: {diagnostic_url(page.url)}",
            f"matched_followers: {matched_text}",
        ]
        try:
            lines.append(f"title: {page.title()}")
        except Exception:
            pass
        lines.append(f"og:title: {_meta_content(page, 'meta[property=\"og:title\"]')}")
        lines.append("h1:")
        for row in _strict_h1_candidates(page):
            lines.append(f"  {row}")
        lines.append("profile_alt:")
        for row in _profile_alt_candidates(page):
            lines.append(f"  {row}")
        lines.append("self_links:")
        for row in _self_link_candidates(page, page_id)[:20]:
            lines.append(f"  {row}")
        lines.append("near_followers:")
        for row in _near_follower_candidates(page, matched_text)[:30]:
            lines.append(f"  {row}")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except Exception:
        return None


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collected_at TEXT NOT NULL,
                    page_id TEXT NOT NULL,
                    page_name TEXT,
                    followers_count INTEGER,
                    fan_count INTEGER,
                    source_metric TEXT,
                    api_version TEXT,
                    collected_ts REAL
                )
            """)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)")}
            if "collected_ts" not in cols:
                conn.execute("ALTER TABLE snapshots ADD COLUMN collected_ts REAL")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_page_time ON snapshots(page_id, collected_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_page_ts ON snapshots(page_id, collected_ts)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    page_id TEXT NOT NULL,
                    alerted_at TEXT NOT NULL,
                    alerted_ts REAL NOT NULL,
                    current_count INTEGER NOT NULL,
                    baseline_count INTEGER,
                    growth INTEGER NOT NULL,
                    window_seconds INTEGER NOT NULL,
                    threshold INTEGER NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_page_ts ON alerts(page_id, alerted_ts)")

            # Reuse history created by earlier versions: backfill epoch timestamps
            # from their ISO collected_at values so v5 reminder windows can use them.
            old_rows = conn.execute(
                "SELECT id, collected_at FROM snapshots WHERE collected_ts IS NULL LIMIT 50000"
            ).fetchall()
            for old in old_rows:
                try:
                    ts = datetime.fromisoformat(old["collected_at"]).timestamp()
                    conn.execute("UPDATE snapshots SET collected_ts=? WHERE id=?", (ts, old["id"]))
                except Exception:
                    pass

    def add(self, row):
        ts = float(row.get("collected_ts") or time.time())
        with self.lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO snapshots(
                    collected_at, page_id, page_name, followers_count,
                    fan_count, source_metric, api_version, collected_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["collected_at"], row["page_id"], row.get("page_name"),
                row.get("followers_count"), row.get("fan_count"), row.get("source_metric"),
                row.get("api_version"), ts,
            ))

    def latest_for_page(self, page_id):
        with self.lock, self._connect() as conn:
            return conn.execute("""
                SELECT collected_at, page_name, followers_count, fan_count, source_metric, collected_ts
                FROM snapshots WHERE page_id=? ORDER BY id DESC LIMIT 1
            """, (page_id,)).fetchone()

    def alert_baseline(self, page_id: str, current_ts: float, window_seconds: int, interval_seconds: int):
        target = current_ts - window_seconds
        max_extra = max(interval_seconds * 2, 300)
        with self.lock, self._connect() as conn:
            row = conn.execute("""
                SELECT collected_ts, followers_count, fan_count, collected_at
                FROM snapshots
                WHERE page_id=? AND collected_ts IS NOT NULL AND collected_ts<=?
                ORDER BY collected_ts DESC LIMIT 1
            """, (page_id, target)).fetchone()
        if not row:
            return None
        ts = float(row["collected_ts"] or 0)
        if ts <= 0 or (current_ts - ts) > (window_seconds + max_extra):
            return None
        count = row["followers_count"] if row["followers_count"] is not None else row["fan_count"]
        if count is None:
            return None
        return {"ts": ts, "count": int(count), "collected_at": row["collected_at"]}

    def has_recent_alert(self, page_id: str, current_ts: float, window_seconds: int, threshold: int, not_before_ts: float = 0.0):
        since = max(current_ts - max(window_seconds, 60), float(not_before_ts or 0.0))
        with self.lock, self._connect() as conn:
            row = conn.execute("""
                SELECT id FROM alerts
                WHERE page_id=? AND alerted_ts>=? AND window_seconds=? AND threshold=?
                ORDER BY alerted_ts DESC LIMIT 1
            """, (page_id, since, int(window_seconds), int(threshold))).fetchone()
        return row is not None

    def add_alert(self, page_id, current_count, baseline_count, growth, window_seconds, threshold):
        ts = time.time()
        with self.lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO alerts(page_id, alerted_at, alerted_ts, current_count, baseline_count, growth, window_seconds, threshold)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                page_id, now_iso(), ts, int(current_count), int(baseline_count), int(growth),
                int(window_seconds), int(threshold),
            ))

    def export_csv(self, path):
        with self.lock, self._connect() as conn, open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "collected_at", "page_id", "page_name", "followers_count",
                "fan_count", "source_metric", "api_version",
            ])
            for row in conn.execute("""
                SELECT collected_at, page_id, page_name, followers_count,
                       fan_count, source_metric, api_version
                FROM snapshots ORDER BY id
            """):
                writer.writerow(tuple(safe_csv_cell(value) for value in row))


class BrowserCollector:
    def __init__(self, profile_dir: Path, show_browser=True):
        self.profile_dir = str(profile_dir)
        self.show_browser = bool(show_browser)
        self._pw = None
        self.context = None
        self.channel = None

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError("未安装 Playwright。请先运行 install_windows.bat 或 start_windows.bat。") from e

        self._pw = sync_playwright().start()
        errors = []
        for channel in ["msedge", "chrome", None]:
            try:
                kwargs = dict(
                    user_data_dir=self.profile_dir,
                    headless=not self.show_browser,
                    chromium_sandbox=True,
                    viewport={"width": 1360, "height": 900},
                    locale="en-US",
                    args=["--disable-notifications"],
                )
                if channel:
                    kwargs["channel"] = channel
                self.context = self._pw.chromium.launch_persistent_context(**kwargs)
                self.channel = channel or "chromium"
                break
            except Exception as e:
                errors.append(f"{channel or 'chromium'}: {e}")
                self.context = None

        if self.context is None:
            try:
                self._pw.stop()
            except Exception:
                pass
            raise RuntimeError("无法启动 Edge/Chrome 浏览器。\n" + "\n".join(errors[-2:]))
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    def ensure_page(self):
        if self.context.pages:
            return self.context.pages[0]
        return self.context.new_page()

    def login_interactive(self):
        page = self.ensure_page()
        page.goto(FACEBOOK_HOME, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        page.bring_to_front()
        return page

    def fetch_page(self, page_id):
        if not re.fullmatch(r"[0-9]+", str(page_id)):
            raise ValueError("专页 ID 必须是 ASCII 数字")
        page = self.ensure_page()
        try:
            page.goto(f"https://www.facebook.com/{page_id}", wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        except Exception:
            pass
        page.wait_for_timeout(2600)

        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
            page.keyboard.press("Escape")
        except Exception:
            pass

        current_url = page.url.lower()
        try:
            body_text = page.locator("body").inner_text(timeout=10000)
        except Exception as e:
            raise RuntimeError(f"无法读取网页内容：{e}")

        login_markers = [
            "log into facebook", "log in to facebook", "登录 facebook",
            "masuk ke facebook", "iniciar sesión en facebook",
        ]
        if "/login" in current_url or any(x in body_text.lower() for x in login_markers):
            raise RuntimeError("Facebook 登录状态无效，请先点击“打开登录浏览器”重新登录。")

        count, approximate, matched_text = extract_followers_from_text(body_text)
        name = extract_page_name(page, body_text, page_id=page_id, matched_text=matched_text)

        # If Facebook's current SPA route did not expose a trustworthy identity,
        # try two alternate official Page routes only for the name. The follower
        # count above is kept from the original Page view.
        if not name:
            fallback_urls = [
                f"https://www.facebook.com/profile.php?id={page_id}",
                f"https://www.facebook.com/{page_id}/about",
            ]
            for fallback_url in fallback_urls:
                try:
                    page.goto(fallback_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
                    page.wait_for_timeout(1800)
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                    fallback_body = page.locator("body").inner_text(timeout=8000)
                    name = extract_page_name(
                        page, fallback_body, page_id=page_id, matched_text=matched_text
                    )
                    if name:
                        break
                except Exception:
                    continue

        debug_path = None
        if not name:
            debug_path = save_name_debug(page, page_id, matched_text=matched_text)

        return {
            "page_id": page_id,
            "page_name": name,
            "followers_count": count,
            "fan_count": None,
            "current": count,
            "source_metric": "网页粉丝数(约值)" if approximate else "网页粉丝数",
            "approximate": approximate,
            "matched_text": matched_text,
            "name_debug_path": str(debug_path) if debug_path else "",
        }


class LoginWorker(QObject):
    status = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def run(self):
        try:
            self.status.emit("正在打开 Facebook 登录浏览器……")
            with BrowserCollector(BROWSER_PROFILE_DIR, show_browser=True) as collector:
                page = collector.login_interactive()
                self.status.emit("请在浏览器中正常登录 Facebook；登录成功后关闭整个浏览器窗口。")
                while True:
                    try:
                        if page.is_closed():
                            break
                        page.wait_for_timeout(800)
                    except Exception:
                        break
            self.status.emit("登录浏览器已关闭，登录状态已保存在本机。")
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self.finished.emit()


class CollectionWorker(QObject):
    status = pyqtSignal(str)
    row = pyqtSignal(dict)
    alert = pyqtSignal(dict)
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, ids, show_browser, once, interval_seconds, duration_seconds,
                 alert_enabled, alert_window_seconds, alert_threshold, parent=None):
        super().__init__(parent)
        self.ids = list(ids)
        self.show_browser = bool(show_browser)
        self.once = bool(once)
        self.interval_seconds = int(interval_seconds)
        self.duration_seconds = duration_seconds
        self.alert_enabled = bool(alert_enabled)
        self.alert_window_seconds = int(alert_window_seconds)
        self.alert_threshold = int(alert_threshold)
        # For automatic monitoring, alerts from earlier runs must not suppress this new run.
        self.alert_session_started_ts = time.time() if not self.once else 0.0
        self._stop = threading.Event()
        self._ids_lock = threading.Lock()
        self.store = Store(DB_PATH)
        self.session_baseline = {}
        self.last_current = {}

    def stop(self):
        self._stop.set()

    def set_ids(self, ids):
        # Safe to call from the UI thread.  The next batch (or the remaining
        # not-yet-opened Pages in the current batch) will use the new list.
        with self._ids_lock:
            self.ids = list(dict.fromkeys(str(x) for x in ids if str(x).strip()))

    def run(self):
        started = time.monotonic()
        try:
            self.status.emit("正在启动浏览器……")
            with BrowserCollector(BROWSER_PROFILE_DIR, self.show_browser) as collector:
                while not self._stop.is_set():
                    round_started = time.monotonic()
                    self._collect_batch(collector)
                    if self.once:
                        break

                    if self.duration_seconds is not None:
                        elapsed = time.monotonic() - started
                        if elapsed >= self.duration_seconds:
                            self.status.emit("已达到设定监控时长，自动停止。")
                            break

                    round_elapsed = time.monotonic() - round_started
                    wait_for = max(0, self.interval_seconds - round_elapsed)
                    if self.duration_seconds is not None:
                        wait_for = min(wait_for, max(0, self.duration_seconds - (time.monotonic() - started)))

                    self.status.emit(f"本轮完成，约 {int(wait_for)} 秒后开始下一轮。")
                    if self._stop.wait(wait_for):
                        break
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self.finished.emit()

    def _collect_batch(self, collector):
        with self._ids_lock:
            batch_ids = list(self.ids)
        total = len(batch_ids)
        for idx, page_id in enumerate(batch_ids, start=1):
            if self._stop.is_set():
                break
            self.status.emit(f"正在读取 {idx}/{total}：{page_id}")
            try:
                prev = self.store.latest_for_page(page_id)
                prev_current = None
                if prev:
                    prev_current = prev["followers_count"] if prev["followers_count"] is not None else prev["fan_count"]
                    if prev_current is not None:
                        prev_current = int(prev_current)

                data = collector.fetch_page(page_id)
                current_ts = time.time()
                data["collected_at"] = now_iso()
                data["collected_ts"] = current_ts
                data["api_version"] = "browser-visible"
                self.store.add(data)

                current = int(data["current"])
                if page_id not in self.session_baseline:
                    self.session_baseline[page_id] = current
                previous = self.last_current.get(page_id, prev_current)
                last_delta = None if previous is None else current - previous
                self.last_current[page_id] = current

                baseline = None
                window_delta = None
                if self.alert_enabled:
                    baseline = self.store.alert_baseline(
                        page_id, current_ts, self.alert_window_seconds, self.interval_seconds
                    )
                    if baseline is not None:
                        window_delta = current - int(baseline["count"])

                status = "成功" if data.get("page_name") else "成功（专页名称未识别）"
                if not data.get("page_name") and data.get("name_debug_path"):
                    status += "；已生成名称调试文件"
                if data.get("approximate"):
                    status += f"；网页显示约值：{data.get('matched_text', '')}"
                if self.alert_enabled and baseline is None:
                    status += "；提醒窗口历史不足"

                payload = {
                    "page_id": page_id,
                    "name": data.get("page_name", ""),
                    "current": current,
                    "last_delta": last_delta,
                    "window_delta": window_delta,
                    "last_check": data["collected_at"],
                    "status": status,
                    "alerted": False,
                }

                if (
                    self.alert_enabled
                    and baseline is not None
                    and window_delta is not None
                    and window_delta >= self.alert_threshold
                    and not self.store.has_recent_alert(
                        page_id, current_ts, self.alert_window_seconds, self.alert_threshold,
                        self.alert_session_started_ts,
                    )
                ):
                    self.store.add_alert(
                        page_id, current, baseline["count"], window_delta,
                        self.alert_window_seconds, self.alert_threshold,
                    )
                    payload["alerted"] = True
                    self.alert.emit({
                        "page_id": page_id,
                        "name": data.get("page_name", "") or page_id,
                        "current": current,
                        "growth": int(window_delta),
                        "threshold": self.alert_threshold,
                        "window_seconds": self.alert_window_seconds,
                        "baseline_count": int(baseline["count"]),
                    })
                    payload["status"] = f"🔔 已提醒：窗口增长 {format_delta(window_delta)}"

                self.row.emit(payload)
            except Exception as e:
                self.row.emit({
                    "page_id": page_id,
                    "name": "",
                    "current": None,
                    "last_delta": None,
                    "window_delta": None,
                    "last_check": now_iso(),
                    "status": str(e),
                    "alerted": False,
                })

            if idx < total and not self._stop.is_set():
                if self._stop.wait(PAGE_GAP_SECONDS):
                    break


class Card(QFrame):
    def __init__(self, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(22, 18, 22, 20)
        self.layout_.setSpacing(12)

        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")
        self.layout_.addWidget(title_label)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("cardSubtitle")
            sub.setWordWrap(True)
            self.layout_.addWidget(sub)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_TITLE} · {APP_VERSION}")
        self.resize(1480, 900)
        self.setMinimumSize(1120, 760)

        self.store = Store(DB_PATH)
        self.settings = QSettings("LocalTools", "FacebookFollowerTracker")
        self.background_color = self._valid_background_color(
            self.settings.value("background_color", "#f4f7fb", str)
        )
        self.worker_thread = None
        self.worker = None
        self.login_thread = None
        self.login_worker = None
        self.row_map = {}
        # Active (unacknowledged) alerts are kept in memory.  A triggered Page
        # keeps ringing periodically until the user clicks that Page's
        # "停止提醒" button.
        self.active_alerts = {}
        # Pages muted here stay silent for the remainder of the current automatic-monitoring run.
        # The set is cleared only when a brand-new automatic monitoring run starts.
        self.silenced_pages_this_run = set()
        # Check pending reminders once per second. Each Page keeps its own
        # next-ring timestamp, so the repeat cadence follows the alert window
        # selected by the user (for example: 1 minute -> ring every 1 minute).
        self.repeat_alert_timer = QTimer(self)
        self.repeat_alert_timer.setInterval(1000)
        self.repeat_alert_timer.timeout.connect(self._repeat_active_alert_sound)

        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.85)
        self.media_player = QMediaPlayer(self)
        self.media_player.setAudioOutput(self.audio_output)

        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))
        self.tray.setToolTip(APP_TITLE)
        self.tray.show()

        self._build_ui()
        self._load_settings()
        self._apply_style()
        self._update_alert_header()
        self._update_duration_enabled()
        self._update_sound_controls()

    # ---------- UI ----------
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        main = QVBoxLayout(root)
        main.setContentsMargins(20, 16, 20, 14)
        main.setSpacing(12)

        # Header -------------------------------------------------------------
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel(APP_TITLE)
        title.setObjectName("pageTitle")
        subtitle = QLabel("网页登录采集 · 历史增长 · 自定义增长提醒")
        subtitle.setObjectName("pageSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()

        self.bg_swatch = QFrame()
        self.bg_swatch.setObjectName("bgSwatch")
        self.bg_swatch.setFixedSize(22, 22)
        header.addWidget(self.bg_swatch, 0, Qt.AlignmentFlag.AlignTop)

        self.bg_color_btn = QPushButton("选择背景色")
        self.bg_color_btn.setProperty("variant", "secondary")
        self.bg_color_btn.setToolTip("选择软件主界面的背景颜色")
        self.bg_color_btn.clicked.connect(self.choose_background_color)
        header.addWidget(self.bg_color_btn, 0, Qt.AlignmentFlag.AlignTop)

        self.reset_bg_btn = QPushButton("恢复默认背景")
        self.reset_bg_btn.setProperty("variant", "secondary")
        self.reset_bg_btn.setToolTip("恢复默认浅色背景")
        self.reset_bg_btn.clicked.connect(self.reset_background_color)
        header.addWidget(self.reset_bg_btn, 0, Qt.AlignmentFlag.AlignTop)

        self.state_badge = QLabel("● 就绪")
        self.state_badge.setObjectName("stateBadge")
        header.addWidget(self.state_badge, 0, Qt.AlignmentFlag.AlignTop)
        main.addLayout(header)

        # Main workspace: large results on the LEFT, all settings on the RIGHT.
        workspace = QHBoxLayout()
        workspace.setSpacing(14)
        main.addLayout(workspace, 1)

        # LEFT: current results ------------------------------------------------
        results_card = Card(
            "当前结果",
            "结果区域保持最大显示空间；双击“专页 ID”或“专页名称”即可复制。",
        )
        workspace.addWidget(results_card, 7)

        results_tools = QHBoxLayout()
        results_tools.addStretch()
        self.export_btn = QPushButton("导出 CSV")
        self.export_btn.setProperty("variant", "secondary")
        self.export_btn.clicked.connect(self.export_csv)
        results_tools.addWidget(self.export_btn)

        self.clear_btn = QPushButton("清空当前结果")
        self.clear_btn.setProperty("variant", "secondary")
        self.clear_btn.clicked.connect(self.clear_results)
        results_tools.addWidget(self.clear_btn)
        results_card.layout_.addLayout(results_tools)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([
            "专页 ID", "专页名称", "当前粉丝数", "较上次增长", "提醒窗口增长", "最后采集", "状态"
        ])
        for col in range(7):
            header_item = self.table.horizontalHeaderItem(col)
            if header_item:
                header_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSortingEnabled(False)
        self.table.cellDoubleClicked.connect(self._copy_result_cell)
        self.table.setToolTip("双击“专页 ID”或“专页名称”即可复制")
        self.table.setMinimumHeight(420)
        self.table.verticalHeader().setDefaultSectionSize(38)

        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        results_card.layout_.addWidget(self.table, 1)

        # RIGHT: Page IDs / collection settings + growth alert ----------------
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_holder = QWidget()
        right_holder.setObjectName("rightPanel")
        right_panel = QVBoxLayout(right_holder)
        right_panel.setContentsMargins(0, 0, 4, 0)
        right_panel.setSpacing(12)
        right_scroll.setWidget(right_holder)
        workspace.addWidget(right_scroll, 4)

        monitor_card = Card(
            "专页 ID 与采集设置",
            "登录、专页 ID、采集/提醒间隔和监控时长统一放在这里。",
        )
        right_panel.addWidget(monitor_card, 1)

        browser_row = QHBoxLayout()
        self.login_btn = QPushButton("打开登录浏览器")
        self.login_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirHomeIcon))
        self.login_btn.clicked.connect(self.open_login_browser)
        browser_row.addWidget(self.login_btn)

        self.profile_btn = QPushButton("浏览器资料目录")
        self.profile_btn.setProperty("variant", "secondary")
        self.profile_btn.clicked.connect(self.open_profile_folder)
        browser_row.addWidget(self.profile_btn)
        browser_row.addStretch()
        monitor_card.layout_.addLayout(browser_row)

        self.show_browser_cb = QCheckBox("采集时显示浏览器")
        self.show_browser_cb.setChecked(True)
        monitor_card.layout_.addWidget(self.show_browser_cb)

        id_label = QLabel("专页 ID")
        id_label.setObjectName("fieldLabel")
        monitor_card.layout_.addWidget(id_label)
        self.ids_edit = QTextEdit()
        self.ids_edit.setAcceptRichText(False)
        self.ids_edit.setPlaceholderText("每行一个专页 ID，也可用逗号分隔\n例如：\n556758147527312\n1246518141880089")
        self.ids_edit.setMinimumHeight(120)
        self.ids_edit.setMaximumHeight(180)
        self.ids_edit.textChanged.connect(self._sync_results_with_ids)
        monitor_card.layout_.addWidget(self.ids_edit, 1)

        settings_row = QGridLayout()
        settings_row.setHorizontalSpacing(8)
        settings_row.setVerticalSpacing(6)

        settings_row.addWidget(self._field_label("采集 / 提醒间隔"), 0, 0, 1, 2)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 9999)
        self.interval_spin.setValue(15)
        self.interval_spin.valueChanged.connect(self._update_alert_header)
        settings_row.addWidget(self.interval_spin, 1, 0)
        self.interval_unit = QComboBox()
        self.interval_unit.addItems(["秒", "分钟", "小时"])
        self.interval_unit.setCurrentText("分钟")
        self.interval_unit.currentTextChanged.connect(self._update_alert_header)
        settings_row.addWidget(self.interval_unit, 1, 1)

        settings_row.addWidget(self._field_label("监控时长"), 0, 2, 1, 2)
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(1, 9999)
        self.duration_spin.setValue(24)
        settings_row.addWidget(self.duration_spin, 1, 2)
        self.duration_unit = QComboBox()
        self.duration_unit.addItems(["分钟", "小时", "天", "无限"])
        self.duration_unit.setCurrentText("小时")
        self.duration_unit.currentTextChanged.connect(self._update_duration_enabled)
        settings_row.addWidget(self.duration_unit, 1, 3)
        for c in range(4):
            settings_row.setColumnStretch(c, 1)
        monitor_card.layout_.addLayout(settings_row)

        monitor_actions = QHBoxLayout()
        self.fetch_btn = QPushButton("立即采集一次")
        self.fetch_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.fetch_btn.clicked.connect(self.fetch_once)
        monitor_actions.addWidget(self.fetch_btn)

        self.start_btn = QPushButton("开始自动监控")
        self.start_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.start_btn.clicked.connect(self.start_tracking)
        monitor_actions.addWidget(self.start_btn)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.setProperty("variant", "danger")
        self.stop_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_tracking)
        monitor_actions.addWidget(self.stop_btn)
        monitor_card.layout_.addLayout(monitor_actions)

        alert_card = Card(
            "增长提醒",
            "提醒统计周期与“采集 / 提醒间隔”一致。达到增长条件后按同一间隔重复响铃，直到手动停止。",
        )
        right_panel.addWidget(alert_card, 1)

        self.alert_enabled_cb = QCheckBox("启用增长提醒")
        self.alert_enabled_cb.setChecked(True)
        self.alert_enabled_cb.toggled.connect(self._update_alert_header)
        alert_card.layout_.addWidget(self.alert_enabled_cb)

        rule_box = QFrame()
        rule_box.setObjectName("ruleBox")
        rule_layout = QGridLayout(rule_box)
        rule_layout.setContentsMargins(12, 10, 12, 10)
        rule_layout.setHorizontalSpacing(8)
        rule_layout.setVerticalSpacing(8)
        rule_layout.addWidget(QLabel("每个采集周期，粉丝增长 ≥"), 0, 0)
        self.alert_threshold_spin = QSpinBox()
        self.alert_threshold_spin.setRange(1, 100_000_000)
        self.alert_threshold_spin.setSingleStep(100)
        self.alert_threshold_spin.setValue(500)
        rule_layout.addWidget(self.alert_threshold_spin, 0, 1)
        rule_layout.addWidget(QLabel("时提醒"), 0, 2)
        rule_layout.setColumnStretch(1, 1)
        alert_card.layout_.addWidget(rule_box)

        sound_label = self._field_label("提醒铃声")
        alert_card.layout_.addWidget(sound_label)
        sound_row = QHBoxLayout()
        self.sound_mode = QComboBox()
        self.sound_mode.addItems(["内置铃声", "自定义铃声"])
        self.sound_mode.currentTextChanged.connect(self._update_sound_controls)
        sound_row.addWidget(self.sound_mode)
        self.choose_sound_btn = QPushButton("选择音频")
        self.choose_sound_btn.setProperty("variant", "secondary")
        self.choose_sound_btn.clicked.connect(self.choose_custom_sound)
        sound_row.addWidget(self.choose_sound_btn)
        self.test_sound_btn = QPushButton("试听")
        self.test_sound_btn.setProperty("variant", "secondary")
        self.test_sound_btn.clicked.connect(self.play_alert_sound)
        sound_row.addWidget(self.test_sound_btn)
        alert_card.layout_.addLayout(sound_row)

        self.custom_sound_path = QLineEdit()
        self.custom_sound_path.setReadOnly(True)
        self.custom_sound_path.setPlaceholderText("尚未选择自定义音频")
        alert_card.layout_.addWidget(self.custom_sound_path)

        self.last_alert_label = QLabel("最近提醒：暂无")
        self.last_alert_label.setObjectName("lastAlert")
        self.last_alert_label.setTextFormat(Qt.TextFormat.PlainText)
        self.last_alert_label.setWordWrap(True)
        alert_card.layout_.addWidget(self.last_alert_label)

        self.active_alert_label = QLabel()
        self.active_alert_label.setObjectName("fieldLabel")
        self.active_alert_label.setWordWrap(True)
        alert_card.layout_.addWidget(self.active_alert_label)
        self._update_active_alert_caption()

        self.active_alert_table = QTableWidget(0, 3)
        self.active_alert_table.setObjectName("activeAlertTable")
        self.active_alert_table.setHorizontalHeaderLabels(["专页", "增长", "操作"])
        for col in range(3):
            header_item = self.active_alert_table.horizontalHeaderItem(col)
            if header_item:
                header_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.active_alert_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.active_alert_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.active_alert_table.verticalHeader().setVisible(False)
        self.active_alert_table.setShowGrid(False)
        self.active_alert_table.setMinimumHeight(105)
        self.active_alert_table.setMaximumHeight(160)
        active_header = self.active_alert_table.horizontalHeader()
        for col in range(3):
            active_header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
        active_header.setStretchLastSection(False)
        self._resize_active_alert_columns()
        alert_card.layout_.addWidget(self.active_alert_table, 1)

        # Status line ---------------------------------------------------------
        status_row = QHBoxLayout()
        self.status_label = QLabel(f"就绪 · 数据库：{DB_PATH}")
        self.status_label.setObjectName("statusText")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        status_row.addWidget(self.status_label)
        status_row.addStretch()
        self.version_label = QLabel(APP_VERSION)
        self.version_label.setObjectName("versionText")
        status_row.addWidget(self.version_label)
        main.addLayout(status_row)

    def _field_label(self, text):
        lab = QLabel(text)
        lab.setObjectName("fieldLabel")
        return lab

    @staticmethod
    def _valid_background_color(value):
        color = QColor(str(value or "").strip())
        if not color.isValid():
            return "#f4f7fb"
        return color.name()

    @staticmethod
    def _background_text_colors(bg_hex):
        color = QColor(bg_hex)
        # Relative brightness approximation is sufficient for UI contrast.
        brightness = (color.red() * 299 + color.green() * 587 + color.blue() * 114) / 1000
        if brightness < 145:
            return "#f8fafc", "#d7e0ea", "#cbd5e1", "#94a3b8"
        return "#111827", "#6b7280", "#64748b", "#94a3b8"

    def _refresh_background_swatch(self):
        if hasattr(self, "bg_swatch"):
            self.bg_swatch.setStyleSheet(
                f"QFrame#bgSwatch {{ background: {self.background_color}; "
                "border: 1px solid #94a3b8; border-radius: 7px; }}"
            )

    def choose_background_color(self):
        initial = QColor(self.background_color)
        color = QColorDialog.getColor(initial, self, "选择界面背景颜色")
        if not color.isValid():
            return
        self.background_color = color.name()
        self.settings.setValue("background_color", self.background_color)
        self.settings.sync()
        self._apply_style()

    def reset_background_color(self):
        self.background_color = "#f4f7fb"
        self.settings.setValue("background_color", self.background_color)
        self.settings.sync()
        self._apply_style()

    def _apply_style(self):
        self.setFont(QFont("Microsoft YaHei UI", 10))
        bg = self._valid_background_color(getattr(self, "background_color", "#f4f7fb"))
        self.background_color = bg
        title_color, subtitle_color, status_color, version_color = self._background_text_colors(bg)
        self.setStyleSheet(f"""
            /* Force readable colors even when Windows is using a dark system theme. */
            QWidget {{ color: #1f2937; }}
            QWidget#root {{ background: {bg}; color: #1f2937; }}
            QWidget#rightPanel {{ background: transparent; }}
            QScrollArea {{ background: transparent; border: none; }}
            QLabel#pageTitle {{ font-size: 26px; font-weight: 700; color: {title_color}; }}
            QLabel#pageSubtitle {{ font-size: 13px; color: {subtitle_color}; }}
            QLabel#stateBadge {{ background: #e8f2ff; color: #1558b0; border: 1px solid #cfe2ff; border-radius: 12px; padding: 6px 12px; font-weight: 600; }}
            QFrame#card {{ background: white; border: 1px solid #e6eaf0; border-radius: 14px; }}
            QLabel#cardTitle {{ font-size: 16px; font-weight: 700; color: #111827; }}
            QLabel#cardSubtitle {{ color: #6b7280; font-size: 12px; }}
            QLabel#fieldLabel {{ font-weight: 600; color: #374151; }}
            QFrame#ruleBox {{ background: #f8fbff; border: 1px solid #dceaff; border-radius: 10px; }}
            QLabel#lastAlert {{ background: #fff8e8; border: 1px solid #f6dfaa; color: #7c5a0a; border-radius: 9px; padding: 10px; }}
            QTextEdit, QLineEdit, QSpinBox, QComboBox {{
                background: #ffffff; color: #111827; border: 1px solid #d7dde6; border-radius: 8px; padding: 7px 9px;
                selection-background-color: #2f80ed; selection-color: #ffffff;
            }}
            QTextEdit:disabled, QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
                background: #f1f5f9; color: #94a3b8;
            }}
            QComboBox QAbstractItemView {{
                background: #ffffff; color: #111827; border: 1px solid #d7dde6;
                selection-background-color: #e5f0ff; selection-color: #153e75; outline: 0;
            }}
            QTextEdit:focus, QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid #2f80ed; }}
            QPushButton {{
                background: #2f80ed; color: white; border: none; border-radius: 8px; padding: 9px 16px; font-weight: 600;
            }}
            QPushButton:hover {{ background: #1f6fd1; }}
            QPushButton:pressed {{ background: #195fb6; }}
            QPushButton:disabled {{ background: #cbd5e1; color: #f8fafc; }}
            QPushButton[variant="secondary"] {{ background: #ffffff; color: #334155; border: 1px solid #d5dce6; }}
            QPushButton[variant="secondary"]:hover {{ background: #f7f9fc; }}
            QPushButton[variant="danger"] {{ background: #e45454; color: white; }}
            QPushButton[variant="danger"]:hover {{ background: #c94242; }}
            QCheckBox {{ spacing: 8px; color: #334155; }}
            QTableWidget {{ background: white; color: #1f2937; border: 1px solid #e6eaf0; border-radius: 9px; alternate-background-color: #fafbfd; }}
            QHeaderView::section {{ background: #f8fafc; color: #475569; border: none; border-bottom: 1px solid #e2e8f0; padding: 10px 8px; font-weight: 700; }}
            QTableWidget::item {{ padding: 8px; border-bottom: 1px solid #eef2f7; }}
            QTableWidget::item:selected {{ background: #e5f0ff; color: #153e75; }}
            QTableWidget#activeAlertTable {{ background: #fffdf7; border: 1px solid #f1dfad; border-radius: 8px; alternate-background-color: #fffaf0; }}
            QTableWidget#activeAlertTable QHeaderView::section {{ background: #fff8e8; color: #7c5a0a; padding: 6px 5px; }}
            QPushButton#silenceAlertButton {{
                background: #fff7ed; color: #7c2d12; border: 1px solid #fb923c;
                border-radius: 6px; padding: 0px; margin: 0px; font-size: 11px; font-weight: 700;
                min-width: 0px; min-height: 0px; max-width: 38px; max-height: 24px;
            }}
            QPushButton#silenceAlertButton:hover {{ background: #ffedd5; color: #7c2d12; }}
            QPushButton#silenceAlertButton:pressed {{ background: #fed7aa; color: #7c2d12; }}
            QToolTip {{ background: #111827; color: #f8fafc; border: 1px solid #334155; padding: 5px; }}
            QLabel#statusText {{ color: {status_color}; }}
            QLabel#versionText {{ color: {version_color}; }}
            QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
            QScrollBar::handle:vertical {{ background: #cbd5e1; border-radius: 5px; min-height: 30px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
        """)
        self._refresh_background_swatch()

    # ---------- settings ----------
    def _load_settings(self):
        s = self.settings
        self.background_color = self._valid_background_color(
            s.value("background_color", self.background_color, str)
        )
        self.ids_edit.setPlainText(s.value("page_ids", "", str))
        self.show_browser_cb.setChecked(s.value("show_browser", True, bool))
        self.interval_spin.setValue(s.value("interval_value", 15, int))
        self.interval_unit.setCurrentText(s.value("interval_unit", "分钟", str))
        self.duration_spin.setValue(s.value("duration_value", 24, int))
        self.duration_unit.setCurrentText(s.value("duration_unit", "小时", str))
        self.alert_enabled_cb.setChecked(s.value("alert_enabled", True, bool))
        self.alert_threshold_spin.setValue(s.value("alert_threshold", 500, int))
        self.sound_mode.setCurrentText(s.value("sound_mode", "内置铃声", str))
        self.custom_sound_path.setText(s.value("custom_sound_path", "", str))

    def _save_settings(self):
        s = self.settings
        s.setValue("background_color", self.background_color)
        s.setValue("page_ids", self.ids_edit.toPlainText())
        s.setValue("show_browser", self.show_browser_cb.isChecked())
        s.setValue("interval_value", self.interval_spin.value())
        s.setValue("interval_unit", self.interval_unit.currentText())
        s.setValue("duration_value", self.duration_spin.value())
        s.setValue("duration_unit", self.duration_unit.currentText())
        s.setValue("alert_enabled", self.alert_enabled_cb.isChecked())
        s.setValue("alert_threshold", self.alert_threshold_spin.value())
        s.setValue("sound_mode", self.sound_mode.currentText())
        s.setValue("custom_sound_path", self.custom_sound_path.text())
        s.sync()

    def _update_duration_enabled(self):
        self.duration_spin.setEnabled(self.duration_unit.currentText() != "无限")

    def _update_sound_controls(self):
        custom = self.sound_mode.currentText() == "自定义铃声"
        self.custom_sound_path.setVisible(custom)
        self.choose_sound_btn.setEnabled(custom)

    def _update_alert_header(self):
        try:
            window = f"{self.interval_spin.value()}{self.interval_unit.currentText()}"
        except Exception:
            window = "采集周期"
        if hasattr(self, "table"):
            if self.alert_enabled_cb.isChecked():
                label = f"{window}增长"
            else:
                label = "周期增长"
            self.table.setHorizontalHeaderItem(4, QTableWidgetItem(label))
        self._update_active_alert_caption()

    def _update_active_alert_caption(self):
        if not hasattr(self, "active_alert_label"):
            return
        try:
            window = self._human_window(
                unit_seconds(self.interval_spin.value(), self.interval_unit.currentText())
            )
        except Exception:
            window = "采集周期"
        self.active_alert_label.setText(
            f"待确认提醒（每 {window} 重复响铃；点“停”后该专页本轮不再提醒）"
        )

    def _resize_active_alert_columns(self):
        """Use balanced widths for Page / Growth / Action in the alert table.

        Target ratio is about 58% / 20% / 22%.  Minimum widths keep the
        numeric growth and Stop button readable even when the window is not
        maximized.
        """
        if not hasattr(self, "active_alert_table"):
            return
        viewport_width = self.active_alert_table.viewport().width()
        if viewport_width <= 0:
            viewport_width = self.active_alert_table.width()
        if viewport_width <= 0:
            return

        # Leave a tiny safety margin for frame/scrollbar rounding.
        usable = max(260, viewport_width - 4)
        growth_w = max(95, int(usable * 0.20))
        action_w = max(105, int(usable * 0.22))
        page_w = max(150, usable - growth_w - action_w)

        # If minimums overflow a narrow card, take the excess from Page first.
        total = page_w + growth_w + action_w
        if total > usable:
            page_w = max(120, page_w - (total - usable))

        self.active_alert_table.setColumnWidth(0, page_w)
        self.active_alert_table.setColumnWidth(1, growth_w)
        self.active_alert_table.setColumnWidth(2, action_w)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Defer once so Qt has already recalculated the card/table viewport.
        QTimer.singleShot(0, self._resize_active_alert_columns)

    # ---------- sound / alert ----------
    def choose_custom_sound(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择提醒铃声", "",
            "音频文件 (*.wav *.mp3 *.m4a *.ogg *.flac);;所有文件 (*.*)"
        )
        if path:
            self.custom_sound_path.setText(path)
            self._save_settings()

    def _selected_sound_path(self):
        if self.sound_mode.currentText() == "自定义铃声":
            p = Path(self.custom_sound_path.text().strip())
            if p.exists():
                return p
        return BUILTIN_SOUND

    def play_alert_sound(self):
        path = self._selected_sound_path()
        if not path.exists():
            QMessageBox.warning(self, "铃声不可用", f"找不到铃声文件：\n{path}")
            return
        self.media_player.stop()
        self.media_player.setSource(QUrl.fromLocalFile(str(path)))
        self.media_player.setPosition(0)
        self.media_player.play()

    def on_alert(self, data):
        page_id = str(data.get("page_id", ""))
        # If the Page ID was removed from the monitor list while a collection
        # round was still running, ignore a late alert/result for it.
        if page_id and page_id not in self._current_page_id_set():
            return
        if page_id in self.silenced_pages_this_run:
            return

        alert_data = dict(data)
        repeat_seconds = max(1, int(alert_data.get("window_seconds", 0) or 1))
        alert_data["repeat_seconds"] = repeat_seconds
        alert_data["next_ring_ts"] = time.monotonic() + repeat_seconds
        self.active_alerts[page_id] = alert_data
        self._refresh_active_alert_table()
        if not self.repeat_alert_timer.isActive():
            self.repeat_alert_timer.start()

        # Ring immediately once when the threshold is first reached.
        self.play_alert_sound()
        name = data.get("name") or page_id
        growth = int(data.get("growth", 0))
        current = int(data.get("current", 0))
        window = self._human_window(int(data.get("window_seconds", 0)))
        message = f"{name}\n{window}内增长 {format_delta(growth)}，当前粉丝 {current:,}"
        self.last_alert_label.setText(f"最近提醒：{message.replace(chr(10), ' · ')}")
        self.tray.showMessage("Facebook 粉丝增长提醒", message, QSystemTrayIcon.MessageIcon.Information, 8000)
        self.state_badge.setText("● 已触发提醒")
        QTimer.singleShot(8000, lambda: self.state_badge.setText("● 监控中" if self.worker else "● 就绪"))

    def _repeat_active_alert_sound(self):
        if not self.active_alerts:
            self.repeat_alert_timer.stop()
            return

        now = time.monotonic()
        due = False
        for data in self.active_alerts.values():
            next_ring = float(data.get("next_ring_ts", 0) or 0)
            if now >= next_ring:
                due = True
                repeat_seconds = max(1, int(data.get("repeat_seconds", 1) or 1))
                # Schedule from now so a temporarily blocked UI does not create
                # a burst of catch-up sounds.
                data["next_ring_ts"] = now + repeat_seconds

        # If several Pages become due together, one bell is enough; the table
        # still lists every Page that is waiting for acknowledgement.
        if due:
            self.play_alert_sound()

    def _refresh_active_alert_table(self):
        if not hasattr(self, "active_alert_table"):
            return
        self.active_alert_table.setRowCount(0)
        for page_id, data in self.active_alerts.items():
            row = self.active_alert_table.rowCount()
            self.active_alert_table.insertRow(row)
            name = str(data.get("name") or page_id)
            growth = format_delta(data.get("growth"))
            name_item = QTableWidgetItem(name)
            name_item.setToolTip(f"专页 ID：{page_id}")
            name_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            growth_item = QTableWidgetItem(growth)
            growth_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.active_alert_table.setItem(row, 0, name_item)
            self.active_alert_table.setItem(row, 1, growth_item)
            btn = QPushButton("停")
            btn.setObjectName("silenceAlertButton")
            btn.setFixedSize(38, 24)
            btn.setToolTip("停止提醒：点击后，该专页在本轮自动监控中后续再增长也不再提醒。重新点击“开始自动监控”时恢复。")
            btn.clicked.connect(lambda _checked=False, pid=page_id: self.silence_page_alert(pid))
            button_box = QWidget()
            button_layout = QHBoxLayout(button_box)
            button_layout.setContentsMargins(2, 1, 2, 1)
            button_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            button_layout.addWidget(btn)
            self.active_alert_table.setCellWidget(row, 2, button_box)
            self.active_alert_table.setRowHeight(row, 30)
        QTimer.singleShot(0, self._resize_active_alert_columns)

    def silence_page_alert(self, page_id: str):
        page_id = str(page_id)
        # Silence this Page for the rest of the current automatic-monitoring run.
        self.silenced_pages_this_run.add(page_id)
        data = self.active_alerts.pop(page_id, None)
        self._refresh_active_alert_table()
        if not self.active_alerts:
            self.repeat_alert_timer.stop()
            self.media_player.stop()
        name = (data or {}).get("name") or page_id
        self.last_alert_label.setText(
            f"最近提醒：已停止 {name} 的本轮提醒；重新开始自动监控后才恢复"
        )
        self.set_status(
            f"已停止 {name} 的本轮增长提醒。本轮仍继续采集，但不再响铃或弹出该专页的系统提醒。"
        )

    def _silence_removed_page_alerts(self, valid_ids):
        removed = [pid for pid in self.active_alerts if pid not in valid_ids]
        if not removed:
            return
        for pid in removed:
            self.active_alerts.pop(pid, None)
        self._refresh_active_alert_table()
        if not self.active_alerts:
            self.repeat_alert_timer.stop()
            self.media_player.stop()

    @staticmethod
    def _human_window(seconds: int):
        if seconds % 86400 == 0:
            return f"{seconds // 86400}天"
        if seconds % 3600 == 0:
            return f"{seconds // 3600}小时"
        if seconds % 60 == 0:
            return f"{seconds // 60}分钟"
        return f"{seconds}秒"

    # ---------- browser login ----------
    def open_profile_folder(self):
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(BROWSER_PROFILE_DIR))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(BROWSER_PROFILE_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(BROWSER_PROFILE_DIR)])
        except Exception as e:
            QMessageBox.critical(self, "打开失败", str(e))

    def open_login_browser(self):
        if self.worker is not None:
            QMessageBox.information(self, "提示", "请先停止自动采集，再打开登录浏览器。")
            return
        if self.login_thread is not None:
            QMessageBox.information(self, "提示", "登录浏览器已经打开。")
            return

        self.login_thread = QThread(self)
        self.login_worker = LoginWorker()
        self.login_worker.moveToThread(self.login_thread)
        self.login_thread.started.connect(self.login_worker.run)
        self.login_worker.status.connect(self.set_status)
        self.login_worker.error.connect(lambda m: QMessageBox.critical(self, "登录浏览器错误", m))
        self.login_worker.finished.connect(self.login_thread.quit)
        self.login_worker.finished.connect(self.login_worker.deleteLater)
        self.login_thread.finished.connect(self._login_finished)
        self.login_btn.setEnabled(False)
        self.state_badge.setText("● 登录中")
        self.login_thread.start()

    def _login_finished(self):
        if self.login_thread:
            self.login_thread.deleteLater()
        self.login_thread = None
        self.login_worker = None
        self.login_btn.setEnabled(True)
        self.state_badge.setText("● 就绪")

    # ---------- live Page-ID / result sync ----------
    def _current_page_id_list(self):
        # Lenient parser for live editing: only complete digit tokens count.
        values = re.findall(r"(?<!\d)\d+(?!\d)", self.ids_edit.toPlainText())
        return list(dict.fromkeys(values))

    def _current_page_id_set(self):
        return set(self._current_page_id_list())

    def _rebuild_row_map(self):
        self.row_map.clear()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                self.row_map[item.text().strip()] = row

    def _sync_results_with_ids(self):
        if not hasattr(self, "table"):
            return
        current_ids = self._current_page_id_list()
        valid_ids = set(current_ids)
        if self.worker is not None:
            self.worker.set_ids(current_ids)
        rows_to_remove = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            pid = item.text().strip() if item else ""
            if pid and pid not in valid_ids:
                rows_to_remove.append(row)
        for row in reversed(rows_to_remove):
            self.table.removeRow(row)
        if rows_to_remove:
            self._rebuild_row_map()
        self._silence_removed_page_alerts(valid_ids)

    def _copy_result_cell(self, row: int, column: int):
        if column not in (0, 1):
            return
        item = self.table.item(row, column)
        if not item:
            return
        text = item.text().strip()
        if not text or text == "—":
            return
        QApplication.clipboard().setText(text)
        what = "专页 ID" if column == 0 else "专页名称"
        self.set_status(f"已复制{what}：{text}")

    # ---------- collection ----------
    def _read_inputs(self):
        ids = parse_page_ids(self.ids_edit.toPlainText())
        if not ids:
            raise ValueError("请至少填写一个 Facebook 专页 ID。")

        interval = unit_seconds(self.interval_spin.value(), self.interval_unit.currentText())
        if interval < MIN_INTERVAL_SECONDS:
            raise ValueError(f"采集间隔不能少于 {MIN_INTERVAL_SECONDS} 秒。")

        duration = None
        if self.duration_unit.currentText() != "无限":
            duration = unit_seconds(self.duration_spin.value(), self.duration_unit.currentText())

        # Growth window and repeated bell cadence use exactly the same
        # interval as collection. There is only one time setting in the UI.
        alert_window = interval
        threshold = self.alert_threshold_spin.value()
        return ids, interval, duration, alert_window, threshold

    def fetch_once(self):
        self._start_collection(once=True)

    def start_tracking(self):
        self._start_collection(once=False)

    def _start_collection(self, once):
        if self.login_thread is not None:
            QMessageBox.information(self, "提示", "请先关闭登录浏览器，再开始采集。")
            return
        if self.worker is not None:
            QMessageBox.information(self, "提示", "当前已有采集任务正在执行。")
            return
        try:
            ids, interval, duration, alert_window, threshold = self._read_inputs()
        except Exception as e:
            QMessageBox.warning(self, "设置错误", str(e))
            return

        if not once:
            self.silenced_pages_this_run.clear()
            self.active_alerts.clear()
            self.repeat_alert_timer.stop()
            self.media_player.stop()
            self._refresh_active_alert_table()
            self.last_alert_label.setText("最近提醒：新一轮自动监控已开始，所有专页提醒已重新开启")

        self._save_settings()
        self.worker_thread = QThread(self)
        self.worker = CollectionWorker(
            ids=ids,
            show_browser=self.show_browser_cb.isChecked(),
            once=once,
            interval_seconds=interval,
            duration_seconds=duration,
            alert_enabled=self.alert_enabled_cb.isChecked(),
            alert_window_seconds=alert_window,
            alert_threshold=threshold,
        )
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.status.connect(self.set_status)
        self.worker.row.connect(self.upsert_row)
        self.worker.alert.connect(self.on_alert)
        self.worker.error.connect(lambda m: QMessageBox.critical(self, "采集错误", m))
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self._collection_finished)

        self.fetch_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.login_btn.setEnabled(False)
        self.state_badge.setText("● 采集中" if once else "● 监控中")
        self.worker_thread.start()

    def stop_tracking(self):
        if self.worker:
            self.worker.stop()
            self.set_status("正在停止……")
            self.stop_btn.setEnabled(False)

    def _collection_finished(self):
        if self.worker_thread:
            self.worker_thread.deleteLater()
        self.worker_thread = None
        self.worker = None
        self.fetch_btn.setEnabled(True)
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.login_btn.setEnabled(True)
        self.state_badge.setText("● 就绪")
        self.set_status("采集任务已结束。")

    def upsert_row(self, data):
        page_id = str(data.get("page_id", ""))
        if page_id and page_id not in self._current_page_id_set():
            return
        if page_id in self.silenced_pages_this_run:
            data = dict(data)
            data["alerted"] = False
            status = str(data.get("status", "") or "")
            if status.startswith("🔔"):
                status = "成功"
            if "本轮提醒已停止" not in status:
                status = (status + "；" if status else "") + "本轮提醒已停止"
            data["status"] = status
        if page_id in self.row_map:
            row = self.row_map[page_id]
        else:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.row_map[page_id] = row

        values = [
            page_id,
            data.get("name", "") or "—",
            "—" if data.get("current") is None else f"{int(data['current']):,}",
            format_delta(data.get("last_delta")),
            format_delta(data.get("window_delta")),
            data.get("last_check", ""),
            data.get("status", ""),
        ]
        for col, value in enumerate(values):
            item = self.table.item(row, col) or QTableWidgetItem()
            item.setText(str(value))
            if col in (0, 1, 2, 3, 4):
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, col, item)

        # Brief visual signal for an alert row.
        alert_color = QColor("#fff2cc") if data.get("alerted") else QColor("#ffffff")
        for col in range(self.table.columnCount()):
            self.table.item(row, col).setBackground(alert_color)

    # ---------- misc ----------
    def set_status(self, text):
        self.status_label.setText(text)

    def clear_results(self):
        if self.table.rowCount() == 0:
            self.set_status("当前结果已经是空的。")
            return
        reply = QMessageBox.question(
            self, "确认清空",
            "只清空界面中的当前结果，不会删除已经保存的历史数据。\n\n确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.table.setRowCount(0)
            self.row_map.clear()
            self.set_status("已清空当前结果；历史数据仍然保留。")

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出历史数据", "facebook_follower_history.csv", "CSV 文件 (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        try:
            self.store.export_csv(path)
            QMessageBox.information(self, "导出完成", f"历史数据已导出到：\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def closeEvent(self, event: QCloseEvent):
        self._save_settings()
        self.repeat_alert_timer.stop()
        self.media_player.stop()
        if self.worker:
            self.worker.stop()
        event.accept()


def apply_fixed_light_palette(app: QApplication):
    """Keep form controls readable regardless of the Windows light/dark theme."""
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#f4f7fb"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#1f2937"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#fafbfd"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#111827"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#f8fafc"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#111827"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#334155"))
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#2f80ed"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    try:
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#94a3b8"))
    except Exception:
        pass
    app.setPalette(palette)


def main():
    app = QApplication(sys.argv)
    # Fusion + an explicit palette prevents Windows dark mode from turning
    # input text/labels white while our cards remain light.
    app.setStyle("Fusion")
    apply_fixed_light_palette(app)
    app.setApplicationName(APP_TITLE)
    app.setOrganizationName("LocalTools")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
