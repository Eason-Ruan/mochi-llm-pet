"""Allowlisted document names from native foreground metadata."""
from __future__ import annotations

from collections.abc import Iterable
from ipaddress import ip_address
import re
import unicodedata
from urllib.parse import urlsplit

from .vision import redact_text


_APP_ALIASES = {
    "microsoft-word": frozenset({"microsoft-word", "microsoft word", "word", "winword", "com.microsoft.word"}),
    "microsoft-excel": frozenset({"microsoft-excel", "microsoft excel", "excel", "com.microsoft.excel"}),
    "vscode": frozenset({
        "vscode", "vs code", "visual studio code", "visual studio code - insiders",
        "code", "code - insiders", "code-insiders", "com.microsoft.vscode", "com.microsoft.vscodeinsiders",
    }),
}
_APP_KEYS = {alias: key for key, aliases in _APP_ALIASES.items() for alias in aliases}
_MOCHI_NAMES = frozenset({"mochi", "mochi m0", "deskpet", "io.github.mochi.deskpet"})
_WEB_BRANDING = re.compile(
    r"\s+[-–—|]\s+(?:(?:google |microsoft )?(?:chrome|edge)|chromium|brave(?: browser)?|vivaldi|"
    r"safari|firefox|google (?:docs|sheets|slides|drive)|notion|(?:feishu|lark)(?: docs)?|飞书(?:云?文档)?)$",
    re.IGNORECASE,
)
_OFFICE_BRANDING = {
    f"app:microsoft-{app}": re.compile(
        rf"\s+[-–—|]\s+(?:microsoft )?{app}(?:\s*\([^()]*\))?$|\s+\((?:microsoft )?{app}\)$",
        re.IGNORECASE,
    )
    for app in ("word", "excel")
}
_OFFICE_STATUS = re.compile(
    r"\s+[-–—|]\s+(?:saved(?: to .*)?|last saved by .+|autosaved|已保存(?:到.*)?)$|"
    r"\s*[\[(](?:compatibility mode|read.only|protected view|兼容模式|只读)[\])]$",
    re.IGNORECASE,
)
_OFFICE_PREFIX = re.compile(r"^(?:microsoft )?(?:word|excel)\s+[-–—|]\s+", re.I)
_CODE_BRANDING = re.compile(r"\s+[-–—]\s+visual studio code(?:\s+-\s+insiders)?$", re.IGNORECASE)
_CODE_UNSAVED_MARKER = "● "
_TITLE_SEPARATOR = re.compile(r"\s+[-–—|]\s+")
_FILE_EXTENSION = re.compile(r"\.[a-zA-Z0-9][a-zA-Z0-9_-]{0,15}$")
_CODE_EXTENSIONLESS = frozenset({"dockerfile", "gemfile", "license", "makefile", "procfile", "rakefile", "readme"})
_GENERIC_TITLE = re.compile(
    r"^(?:untitled(?:[- ]\d+| (?:document|spreadsheet|presentation))?|"
    r"(?:document|book)\s*\d*|new (?:tab|document|file|page|window)|(?:blank )?page|"
    r"welcome(?: page)?|start page|home(?: page)?|loading(?:…|\.\.\.)?|dashboard|workspace|"
    r"recent(?:ly (?:opened|viewed))?(?: files| documents| pages)?|all (?:files|documents)|"
    r"my (?:files|documents)|document list|templates?|projects?|"
    r"(?:microsoft )?(?:word|excel)|visual studio code|vscode|code|google (?:docs|drive|sheets|slides)|"
    r"notion|(?:feishu|lark)(?: docs)?|飞书(?:云?文档)?|文档\d*|未命名(?:文档)?|无标题(?:文档)?|"
    r"新建(?:文档|标签页)?|最近(?:打开|访问|使用|文档)?|我的文档|工作台|首页|欢迎)$",
    re.IGNORECASE,
)
_UNSAFE_TITLE = re.compile(
    r"://|\bwww\.|[?&][\w.-]+=|\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b|"
    r"\b(?:api[-_ ]?key|secret|access[-_]token|authorization|bearer|password|passcode|credential)\b|"
    r"\b(?:login|sign[- ]in|bank(?:ing)?|incognito|inprivate|private (?:browsing|window))\b|"
    r"\b(?:ignore|disregard) (?:all |any )?(?:previous|prior|earlier|above)\b|"
    r"忽略.{0,35}(?:指令|提示词|规则)|密码|银行|支付|无痕(?:浏览|窗口)|隐私浏览|"
    r"無痕(?:瀏覽|視窗)|私密(?:浏览|瀏覽)",
    re.IGNORECASE,
)
_WEB_ADDRESS = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:app|cn|co|com|dev|edu|io|me|net|org|site|so)\b", re.I)
_GOOGLE_DOCUMENT = re.compile(r"^/(?:document|spreadsheets|presentation|forms)/(?:u/\d+/)?d/(?:e/)?[\w-]+(?:/|$)")
_NOTION_DOCUMENT = re.compile(r"(?:^|/|-)\w*?(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})/?$", re.I)
_DOCUMENT_ROUTE = re.compile(r"^/(?:docs?|docx|documents|sheets|slides|wiki|base)/[a-zA-Z0-9_-]+(?:/|$)")
_GENERIC_ROUTE = re.compile(r"^/(?:docs?|docx|documents|sheets|slides|wiki|base)/(?:home|recent|new|templates|shared|list)(?:/|$)", re.I)
_PATH_ROOT = re.compile(r"^(?:[A-Za-z]:[/\\]|[/\\.~])")


def _app_key(value: str) -> str:
    return value.casefold().strip().removesuffix(".exe")


def is_mochi_app(app_name: str | None, app_id: str | None = None) -> bool:
    return any(_app_key(value) in _MOCHI_NAMES for value in (app_name, app_id) if value)


def native_document_source(app_name: str | None, app_id: str | None) -> str | None:
    value = app_id or app_name
    app = _APP_KEYS.get(_app_key(value)) if value else None
    return f"app:{app}" if app else None


def _host(value: str) -> str | None:
    try:
        host = value.rstrip(".").lower().encode("idna").decode("ascii")
        ip_address(host)
    except UnicodeError:
        return None
    except ValueError:
        pass
    else:
        return None
    if len(host) > 253 or host.endswith((".local", ".internal", ".localhost", ".lan", ".home", ".arpa")):
        return None
    labels = host.split(".")
    if len(labels) < 2 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                  for label in labels):
        return None
    return host


def page_domain(url: str | None) -> str | None:
    if url:
        try:
            parsed = urlsplit(url)
            if parsed.scheme in {"http", "https"} and parsed.hostname is not None:
                return _host(parsed.hostname)
        except ValueError:
            pass
    return None


def normalize_document_sources(values: Iterable[str]) -> tuple[str, ...]:
    """Return exact source IDs suitable for local settings and permission checks."""
    if isinstance(values, (str, bytes)):
        raise ValueError("Document sources must be a list of supported apps or exact domains")
    normalized = {}
    for value in values:
        source = value.strip().casefold() if isinstance(value, str) else ""
        if source.startswith("app:") or _app_key(source) in _APP_KEYS:
            key = native_document_source(source.removeprefix("app:"), None)
        else:
            host = _host(source.removeprefix("domain:"))
            key = f"domain:{host}" if host else None
        if key is None:
            raise ValueError("Document sources must be supported apps or exact domains")
        normalized[key] = None
    return tuple(normalized)


def document_source_allowed(source: str | None, sources: Iterable[str]) -> bool:
    return bool(source and source in sources)


def browser_document_source(url: str | None) -> str | None:
    """Require a document route as well as the active tab's exact site."""
    domain = page_domain(url)
    if domain is None:
        return None
    try:
        parsed = urlsplit(url)
        if parsed.username is not None or parsed.password is not None or parsed.port not in (None, 80, 443):
            return None
    except ValueError:
        return None
    path = parsed.path
    if domain == "docs.google.com":
        document = _GOOGLE_DOCUMENT.match(path)
    elif domain == "notion.so" or domain.endswith((".notion.so", ".notion.com", ".notion.site")):
        document = _NOTION_DOCUMENT.search(path)
    else:
        document = _DOCUMENT_ROUTE.match(path) and not _GENERIC_ROUTE.match(path)
    return f"domain:{domain}" if document else None


def _strip_trailing(value: str, suffixes: re.Pattern[str]) -> str:
    while (stripped := suffixes.sub("", value).strip()) != value:
        value = stripped
    return value


def _file_basename(value: str) -> str:
    """Extract an identifiable file basename and discard its parent folders."""
    if "/" not in value and "\\" not in value:
        return value
    basename = re.split(r"[/\\]", value)[-1]
    if _PATH_ROOT.match(value) or _FILE_EXTENSION.search(basename):
        return basename
    return ""


def _office_title(title: str, source: str) -> str:
    branding = _OFFICE_BRANDING[source]
    while (stripped := _strip_trailing(branding.sub("", title).strip(), _OFFICE_STATUS)) != title:
        title = stripped
    return title


def _code_filename(value: str) -> str:
    value = _CODE_BRANDING.sub("", value).lstrip("●◉• \u25cf*")
    # The default title joins the active editor, workspace, and product.
    # Start with the shortest prefix that identifies a file.
    for match in _TITLE_SEPARATOR.finditer(value):
        candidate = _file_basename(value[:match.start()])
        if _FILE_EXTENSION.search(candidate) or candidate.casefold() in _CODE_EXTENSIONLESS:
            return candidate
    candidate = _file_basename(value)
    if _FILE_EXTENSION.search(candidate) or candidate.casefold() in _CODE_EXTENSIONLESS:
        return candidate
    return ""


def document_title(value: object, source: str | None) -> tuple[str | None, str]:
    """Return a safe title and whether missing evidence can use a visual fallback."""
    if not source or value is None or value == "":
        return None, "title_unavailable"
    if not isinstance(value, str) or len(value) > 512 or not all(
        char.isprintable() or char == "\u00a0" for char in value
    ):
        return None, "title_filtered"
    title = " ".join(unicodedata.normalize("NFC", value).split())
    if _UNSAFE_TITLE.search(title):
        return None, "title_filtered"
    raw = title
    if source in _OFFICE_BRANDING:
        title = _office_title(title, source)
        if _GENERIC_TITLE.fullmatch(_OFFICE_PREFIX.sub("", title)):
            return None, "title_unavailable"
        parts = _TITLE_SEPARATOR.split(title, maxsplit=1)
        if len(parts) == 2 and _PATH_ROOT.match(parts[1]) and re.search(r"[/\\]", parts[1]):
            title = parts[0]
        title = _file_basename(title)
    elif source == "app:vscode":
        title = _code_filename(title)
    elif source.startswith("domain:"):
        if _WEB_ADDRESS.search(title):
            return None, "title_filtered"
        title = _strip_trailing(title, _WEB_BRANDING)
    else:
        return None, "title_filtered"
    title = title.strip().strip("•●*").strip()
    if not title and re.search(r"[/\\:?@]", raw):
        return None, "title_filtered"
    if not title or _GENERIC_TITLE.fullmatch(title):
        return None, "title_unavailable"
    if len(title) > 128 or re.search(r"[/\\:?@]", title) or not any(char.isalnum() for char in title):
        return None, "title_filtered"
    normalized = " ".join(unicodedata.normalize("NFKC", title).split())
    if redact_text(title) != normalized:
        return None, "title_filtered"
    return title, "identified"


def sanitize_document_name(value: object, source: str | None) -> str | None:
    """Keep a short title only when an authorized resolver established the source."""
    return document_title(value, source)[0]


def native_document_identity(value: str | None, source: str | None) -> tuple[str, bool | None] | None:
    """Provide a native title for in-memory comparison and any trusted marker."""
    if not value:
        return None
    if source in _OFFICE_BRANDING:
        return _office_title(value, source), None
    if source != "app:vscode":
        return None
    if _CODE_BRANDING.search(value) is None:
        return value, None
    title = _CODE_BRANDING.sub("", value)
    if title.startswith(_CODE_UNSAVED_MARKER):
        return title.removeprefix(_CODE_UNSAVED_MARKER), True
    if title.startswith(("●", "◉", "•", "*")):
        return title, None
    return title, False
