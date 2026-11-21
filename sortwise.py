#!/usr/bin/env python3
"""
SortWise - sorts any folder on your computer by what the files are about.

  SortWise                 start and open the SortWise window
  SortWise --background    start quietly in the tray / menu bar (used when signing in)

Runs locally; nothing leaves the computer. Nothing is deleted except duplicates
you choose, and those go to the Recycle Bin (Windows) or the Bin (Mac).
"""
__version__ = "1.0.0"
REPO = "theganeshlife/SortWise"
import hashlib
import html
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, unquote

FROZEN = getattr(sys, "frozen", False)
RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
UI_PATH = os.path.join(RES_DIR, "ui.html")
ICON_PATH = os.path.join(RES_DIR, "assets", "icon.png")
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
if IS_WIN:
    DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "SortWise")
elif IS_MAC:
    DATA_DIR = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "SortWise")
else:
    DATA_DIR = os.path.join(os.path.expanduser("~"), ".sortwise")
LEGACY_DIR = os.path.join(os.path.dirname(DATA_DIR), "Tidy" if (IS_WIN or IS_MAC) else ".tidy")
if not os.path.exists(DATA_DIR) and os.path.isdir(LEGACY_DIR):
    # carry connected folders, settings, tags and history over from an earlier install
    import shutil
    try:
        shutil.copytree(LEGACY_DIR, DATA_DIR, ignore=shutil.ignore_patterns("update", "*.log"))
        for old, new in (("tidy.db", "sortwise.db"), ("tidy.db-wal", "sortwise.db-wal"), ("tidy.db-shm", "sortwise.db-shm")):
            if os.path.exists(os.path.join(DATA_DIR, old)):
                os.replace(os.path.join(DATA_DIR, old), os.path.join(DATA_DIR, new))
    except OSError:
        pass
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "sortwise.db")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
LOG_PATH = os.path.join(DATA_DIR, "sortwise.log")
PORT = int(os.environ.get("SORTWISE_PORT", "8765"))
TOKEN = secrets.token_hex(16)
try:
    with open(os.path.join(RES_DIR, "build.txt"), encoding="utf-8") as _f:
        BUILD = _f.read().strip() or "dev"
except OSError:
    BUILD = "dev"

try:
    import logging
    logging.getLogger("pypdf").setLevel(logging.CRITICAL)
    import pypdf  # type: ignore
except Exception:
    pypdf = None

CLOUD_MASK = 0x1000 | 0x40000 | 0x400000  # offline / recall-on-open / recall-on-data-access

# ------------------------------------------------------------------ file kinds
KINDS = [  # (folder name, extensions)
    ("Documents", "doc docx odt rtf txt md pages wpd"),
    ("PDFs", "pdf"),
    ("Spreadsheets", "xls xlsx xlsm csv ods numbers tsv"),
    ("Presentations", "ppt pptx pptm key odp"),
    ("Photos", "heic heif cr2 cr3 nef arw dng raw orf rw2 raf"),
    ("Images", "jpg jpeg png gif bmp webp svg tif tiff ico avif"),
    ("Videos", "mp4 mov avi mkv wmv m4v webm 3gp mts"),
    ("Audio", "mp3 wav m4a aac flac ogg wma opus aiff"),
    ("Design", "ai psd indd afdesign afphoto xd fig eps sketch vsdx cdr"),
    ("eBooks", "epub mobi azw azw3 djvu cbz cbr"),
    ("Code", "py ipynb js ts jsx tsx html htm css json xml yaml yml sql r sh bat ps1 java c cpp cs go rs php rb"),
    ("Archives", "zip rar 7z tar gz tgz bz2 xz"),
    ("Installers", "exe msi msix appx dmg pkg apk iso"),
    ("Fonts", "ttf otf woff woff2"),
    ("Email", "pst ost msg eml mbox"),
]
EXT_KIND = {e: k for k, exts in KINDS for e in exts.split()}
KIND_NAMES = [k for k, _ in KINDS] + ["Other"]
KIND_ALIASES = {"Photos": ["Photos", "Pictures"], "Images": ["Images", "Pictures"], "Videos": ["Videos", "Movies"],
                "Audio": ["Audio", "Music"], "Documents": ["Documents"]}
DUP_GROUP = {"Photos": "photos", "Images": "photos", "Videos": "videos", "Documents": "documents", "PDFs": "documents",
             "Spreadsheets": "documents", "Presentations": "documents", "eBooks": "documents"}

SMART = [  # cross-type categories, matched on whole words in the file name
    ("Screenshots", ["screenshot", "screen shot", "screen capture", "snip", "snapshot"], {"Images", "Photos"}),
    ("Invoices & Receipts", ["invoice", "receipt", "bill", "tax invoice", "payslip", "pay slip", "statement", "remittance"], None),
    ("CVs & Job Applications", ["cv", "resume", "résumé", "cover letter", "selection criteria", "job application"], None),
    ("Tickets & Bookings", ["ticket", "e ticket", "eticket", "booking", "boarding pass", "itinerary", "reservation", "confirmation"], None),
]
CAMERA_NAME = re.compile(r"^(img|dsc|dscn|dcim|pxl|mvimg|photo|whatsapp image|signal-|vid|mov|pict)[ _-]?|^\d{8}[_-]\d{6}", re.I)
SKIP_NAME = re.compile(r"^(~\$|\.|desktop\.ini$|thumbs\.db$|\.ds_store$)", re.I)
SKIP_EXT = {"lnk", "url", "ini", "tmp", "crdownload", "part", "partial", "download", "opdownload", "!ut", "temp", "lock", "sys"}
STOP = set("""the and for with from into about your you our this that these are was were not but all any new final draft copy
version ver doc docx document documents file files untitled image img photo pic pics scan scanned edited edit updated update
revised rev jan feb mar apr may jun jul aug sep sept oct nov dec january february march april june july august september
october november december mon tue wed thu fri sat sun track changes pdf part page pages presentation slides sheet sheets book workbook
png jpg jpeg screenshot whatsapp download downloaded www com http https org net html one two three his her its how why what
when who via per vs etc using use used get got have has had will can could should would also more most other others some
microsoft word excel powerpoint office template templates sample test temp misc stuff things final2 last latest old""".split())
GENERIC_DIR = re.compile(r"^(new folder( \(\d+\))?|untitled folder|folder\d*|temp|tmp|misc|stuff|_review|_deleted)$", re.I)

LOCK = threading.RLock()
STATE = {"scanning": False, "progress": "", "last_scan": None}
EVENTS = []  # live activity for the UI


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def push_event(kind, text, **kw):
    e = {"id": (EVENTS[-1]["id"] + 1) if EVENTS else 1, "ts": time.time(), "kind": kind, "text": text, **kw}
    EVENTS.append(e)
    del EVENTS[:-300]
    con = db()
    con.execute("INSERT INTO activity(ts,kind,text,data) VALUES(?,?,?,?)", (e["ts"], kind, text, json.dumps(kw)))
    con.commit(); con.close()


# ------------------------------------------------------------------ settings
DEFAULT_SETTINGS = {"folders": [], "min_group": 3, "settle_seconds": 4, "start_with_windows": False}


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    for k, v in DEFAULT_SETTINGS.items():
        s.setdefault(k, v if not isinstance(v, list) else list(v))
    return s


SETTINGS = load_settings()


def save_settings():
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(SETTINGS, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def folder_entry(base):
    nb = os.path.normcase(os.path.normpath(base))
    for f in SETTINGS["folders"]:
        if os.path.normcase(os.path.normpath(f["path"])) == nb:
            return f
    return None


def bases():
    return [f["path"] for f in SETTINGS["folders"] if os.path.isdir(f["path"])]


def base_of(path):
    p = os.path.normcase(os.path.abspath(path))
    best = None
    for b in bases():
        bc = os.path.normcase(os.path.abspath(b))
        if p == bc or p.startswith(bc.rstrip(os.sep) + os.sep):
            if best is None or len(bc) > len(os.path.normcase(best)):
                best = b
    return best


def inside(path, base):
    p = os.path.normcase(os.path.abspath(path))
    b = os.path.normcase(os.path.abspath(base)).rstrip(os.sep)
    return p == b or p.startswith(b + os.sep)


# ------------------------------------------------------------------ db
def db():
    con = sqlite3.connect(DB_PATH, timeout=60, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, base TEXT, rel TEXT, dir TEXT, name TEXT,
        ext TEXT, kind TEXT, size INTEGER, mtime REAL, cloud INTEGER DEFAULT 0, hash TEXT, content TEXT,
        content_done INTEGER DEFAULT 0, seen INTEGER DEFAULT 0);
    CREATE INDEX IF NOT EXISTS ix_base ON files(base, dir);
    CREATE INDEX IF NOT EXISTS ix_size ON files(size);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, tags TEXT DEFAULT '', note TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS journal(id INTEGER PRIMARY KEY, batch TEXT, ts REAL, action TEXT, src TEXT, dst TEXT,
        label TEXT, undone INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, text TEXT, data TEXT);
    """)
    cols = {r[1] for r in con.execute("PRAGMA table_info(files)")}
    if "kw" not in cols:
        con.execute("ALTER TABLE files ADD COLUMN kw TEXT")
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(name, dir, content, tags, tokenize='unicode61 remove_diacritics 2')")
        STATE["fts"] = True
    except sqlite3.OperationalError:
        STATE["fts"] = False
    con.commit(); con.close()


def forget_path(con, path, keep_id):
    """Drop an out-of-date index entry for a path before another entry takes it over."""
    for r in con.execute("SELECT id FROM files WHERE path=? AND id<>?", (path, keep_id)).fetchall():
        con.execute("DELETE FROM files WHERE id=?", (r["id"],))
        if STATE.get("fts"):
            con.execute("DELETE FROM fts WHERE rowid=?", (r["id"],))


def meta_key(name, size):
    return f"{name.lower()}|{size}"


# ------------------------------------------------------------------ helpers
def split_ext(name):
    base, dot, ext = name.rpartition(".")
    return (name, "") if not dot or not base else (base, ext.lower())


def kind_of(name):
    return EXT_KIND.get(split_ext(name)[1], "Other")


def is_cloud(st):
    if (getattr(st, "st_file_attributes", 0) or 0) & CLOUD_MASK:
        return True
    return bool((getattr(st, "st_flags", 0) or 0) & 0x40000000)  # macOS SF_DATALESS: online-only placeholder


def skippable(name):
    return bool(SKIP_NAME.match(name)) or split_ext(name)[1] in SKIP_EXT


def norm(s):
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    return re.sub(r"[_\-.,+()\[\]{}#@&'’!]+", " ", s.lower())


def tokens(name):
    base = split_ext(name)[0]
    raw = re.findall(r"[^\W\d_]{3,}", norm(base), re.U)
    return [t for t in raw if t not in STOP]


def orig_forms(names):
    forms = defaultdict(Counter)
    for n in names:
        b = re.sub(r"([a-z])([A-Z])", r"\1 \2", split_ext(n)[0])
        for w in re.findall(r"[^\W\d_]{3,}", b, re.U):
            forms[w.lower()][w] += 1
    return forms


def pretty(words, forms):
    out = []
    for w in words:
        f = forms.get(w)
        cand = f.most_common(1)[0][0] if f else w
        out.append(cand if (cand.isupper() or (cand[0].isupper() and cand[1:].lower() != cand[1:])) else w.capitalize())
    return " ".join(out)


def has_phrase(name, phrase):
    return re.search(r"(?<![a-z0-9])" + r"\s+".join(map(re.escape, phrase.split())) + r"(?![a-z0-9])", norm(split_ext(name)[0])) is not None


def year_of(name, mtime):
    m = re.search(r"(?<!\d)((?:19|20)\d\d)[-_ .]?(0[1-9]|1[0-2])[-_ .]?([0-2]\d|3[01])(?!\d)", name)
    if m and 1990 <= int(m.group(1)) <= datetime.now().year:
        return m.group(1)
    return str(datetime.fromtimestamp(mtime).year) if mtime else str(datetime.now().year)


def unique_dest(path):
    if not os.path.exists(path):
        return path
    b, e = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{b} ({i}){e}"):
        i += 1
    return f"{b} ({i}){e}"


def listdir(path):
    try:
        return list(os.scandir(path))
    except OSError:
        return []


def subdirs(path):
    return [e.name for e in listdir(path) if e.is_dir(follow_symlinks=False) and not e.name.startswith((".", "$"))]


def loose_files(path):
    out = []
    for e in listdir(path):
        try:
            if e.is_file(follow_symlinks=False) and not skippable(e.name) and os.path.normcase(e.path) != os.path.normcase(os.path.join(APP_DIR, e.name)):
                st = e.stat(follow_symlinks=False)
                out.append({"name": e.name, "path": e.path, "size": st.st_size, "mtime": st.st_mtime, "cloud": is_cloud(st)})
        except OSError:
            pass
    return out


# ------------------------------------------------------------------ text extraction & hashing
TEXT_EXT = {"txt", "md", "csv", "tsv", "py", "js", "ts", "html", "htm", "css", "json", "xml", "sql", "r", "sh", "bat", "ps1", "yaml", "yml"}


def extract_text(path, ext, size, limit=300_000):
    if size > 40 * 1024 * 1024:
        return ""
    try:
        if ext in ("docx", "pptx", "pptm", "xlsx", "xlsm"):
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                if ext == "docx":
                    parts = [n for n in names if re.match(r"word/(document|header\d*|footer\d*|footnotes)\.xml$", n)]
                elif ext.startswith("ppt"):
                    parts = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)), key=lambda n: int(re.findall(r"\d+", n)[0]))
                else:
                    parts = [n for n in names if n == "xl/sharedStrings.xml"]
                out = []
                for n in parts:
                    xml = z.read(n).decode("utf-8", "ignore")
                    xml = re.sub(r"</w:p>|</a:p>|</si>", "\n", xml)
                    out.append(html.unescape(re.sub(r"<[^>]+>", "", xml)))
                return "\n".join(out)[:limit]
        if ext == "pdf" and pypdf:
            r = pypdf.PdfReader(path)
            return "\n".join((p.extract_text() or "") for p in r.pages[:40])[:limit]
        if ext in TEXT_EXT:
            with open(path, "rb") as f:
                return f.read(limit).decode("utf-8", "ignore")
    except Exception:
        return ""
    return ""


def file_hash(path, size):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        if size <= 64 << 20:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        else:
            for off in (0, size // 2, max(0, size - (1 << 20))):
                f.seek(off); h.update(f.read(1 << 20))
            h.update(str(size).encode())
    return h.hexdigest()


# ------------------------------------------------------------------ indexing
def upsert(con, base, path, st=None, stamp=0):
    try:
        st = st or os.stat(path)
    except OSError:
        return None
    name = os.path.basename(path)
    rel = os.path.relpath(path, base)
    cloud = 1 if is_cloud(st) else 0
    row = con.execute("SELECT id,size,mtime FROM files WHERE path=?", (path,)).fetchone()
    if row and row["size"] == st.st_size and abs(row["mtime"] - st.st_mtime) < 2:
        con.execute("UPDATE files SET seen=?, cloud=?, base=?, rel=?, dir=? WHERE id=?",
                    (stamp, cloud, base, rel, os.path.dirname(rel), row["id"]))
        return row["id"]
    vals = (base, rel, os.path.dirname(rel), name, split_ext(name)[1], kind_of(name), st.st_size, st.st_mtime, cloud, stamp)
    if row:
        con.execute("""UPDATE files SET base=?,rel=?,dir=?,name=?,ext=?,kind=?,size=?,mtime=?,cloud=?,seen=?,hash=NULL,
                       content=NULL,content_done=0,kw=NULL WHERE id=?""", vals + (row["id"],))
        return row["id"]
    try:
        cur = con.execute("INSERT INTO files(base,rel,dir,name,ext,kind,size,mtime,cloud,seen,path) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          vals + (path,))
        return cur.lastrowid
    except sqlite3.IntegrityError:  # recorded by another part of the app a moment ago
        row = con.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
        if row:
            con.execute("""UPDATE files SET base=?,rel=?,dir=?,name=?,ext=?,kind=?,size=?,mtime=?,cloud=?,seen=? WHERE id=?""",
                        vals + (row["id"],))
            return row["id"]
        return None


def walk(base):
    others = {os.path.normcase(os.path.abspath(b)) for b in bases() if os.path.normcase(b) != os.path.normcase(base)}
    stack = [base]
    while stack:
        d = stack.pop()
        for e in listdir(d):
            try:
                if e.is_dir(follow_symlinks=False):
                    n = e.name
                    if n.startswith((".", "$")) or n in ("node_modules", "AppData", "__pycache__", "System Volume Information"):
                        continue
                    if os.path.normcase(os.path.abspath(e.path)) in others or os.path.normcase(e.path) == os.path.normcase(APP_DIR):
                        continue
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False) and not SKIP_NAME.match(e.name):
                    yield e.path, e.stat(follow_symlinks=False)
            except OSError:
                continue


QUEUED = set()


def scan(only=None):
    """Index connected folders. If a scan is already running, this one is queued and runs right after."""
    with LOCK:
        if STATE["scanning"]:
            QUEUED.add(only or "*")
            return
        STATE["scanning"] = True
    try:
        _scan(only)
    finally:
        with LOCK:
            STATE["scanning"] = False
            nxt = QUEUED.copy()
            QUEUED.clear()
    if nxt:
        scan(None if "*" in nxt or len(nxt) > 1 else next(iter(nxt)))


def _scan(only=None):
    if True:
        con = db()
        stamp = int(time.time() * 1000)
        targets = [only] if only else bases()
        n = 0
        last_commit = time.time()
        for b in targets:
            STATE["progress"] = f"Reading {os.path.basename(b) or b}"
            for path, st in walk(b):
                upsert(con, b, path, st, stamp)
                n += 1
                if time.time() - last_commit > 0.3:  # keep write locks short so the window stays responsive
                    STATE["progress"] = f"Found {n:,} files"
                    con.commit()
                    last_commit = time.time()
            con.execute("DELETE FROM files WHERE base=? AND seen<>?", (b, stamp))
            con.commit()
        # forget folders that were disconnected
        known = bases()
        if not only:
            q = "DELETE FROM files WHERE base NOT IN (%s)" % ",".join("?" * len(known)) if known else "DELETE FROM files"
            con.execute(q, known); con.commit()
        todo = con.execute("SELECT id,path,ext,size FROM files WHERE content_done=0 AND cloud=0").fetchall()
        for i, r in enumerate(todo):
            if i % 20 == 0:
                STATE["progress"] = f"Reading inside files {i:,} of {len(todo):,}"
            txt = extract_text(r["path"], r["ext"], r["size"])  # read with no database lock held
            con.execute("UPDATE files SET content=?, content_done=1, kw=? WHERE id=?", (txt, keywords_json(txt), r["id"]))
            con.commit()
        STATE["progress"] = "Looking for duplicates"
        rows = con.execute("""SELECT id,path,size FROM files WHERE cloud=0 AND hash IS NULL AND size>0 AND size IN
                              (SELECT size FROM files WHERE size>0 GROUP BY size HAVING COUNT(*)>1)""").fetchall()
        for r in rows:
            try:
                h = file_hash(r["path"], r["size"])
            except OSError:
                continue
            con.execute("UPDATE files SET hash=? WHERE id=?", (h, r["id"]))
            con.commit()
        rebuild_fts(con)
        con.close()
        STATE["last_scan"] = time.time()
        STATE["progress"] = ""
        log(f"scan done ({n} files)")


def rebuild_fts(con):
    if not STATE.get("fts"):
        return
    con.execute("DELETE FROM fts")
    con.execute("""INSERT INTO fts(rowid,name,dir,content,tags) SELECT f.id, f.name, f.dir, COALESCE(f.content,''),
                   COALESCE(m.tags,'')||' '||COALESCE(m.note,'') FROM files f LEFT JOIN meta m ON m.key=lower(f.name)||'|'||f.size""")
    con.commit()


def fts_row(con, fid):
    if not STATE.get("fts"):
        return
    f = con.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    con.execute("DELETE FROM fts WHERE rowid=?", (fid,))
    if f:
        m = con.execute("SELECT tags,note FROM meta WHERE key=?", (meta_key(f["name"], f["size"]),)).fetchone()
        con.execute("INSERT INTO fts(rowid,name,dir,content,tags) VALUES(?,?,?,?,?)",
                    (fid, f["name"], f["dir"], f["content"] or "", (m["tags"] + " " + m["note"]) if m else ""))


# ------------------------------------------------------------------ the organiser
WORDS = set("""also which their there them they been being would could should about after before between through during
under over more most such than then when where while what whose with within without into onto upon very much many these those
each other another same different same however therefore thus hence because since although though whether either neither
both only just even still already often always never sometimes here well first second third last next page pages figure table
chapter section introduction conclusion abstract references appendix university department http https www com org doi pdf
journal vol volume issue press published publication author authors edited edition et al using used use make made way ways
new one two three four five six seven eight nine ten year years time times part may might must shall will can cannot like
including include includes include based study studies paper research data analysis results result findings finding discussion
case cases example examples number numbers level levels form forms general specific various several important however""".split())
CONTENT_KINDS = {"Documents", "PDFs", "Presentations", "eBooks"}
MAX_DEPTH = 4
_CONTENT = {}


def content_for(path, ext, size, cloud):
    """Text inside a file for grouping: from the index if we have it, else read it now (never downloads online-only files)."""
    if path in _CONTENT:
        return _CONTENT[path]
    txt = ""
    try:
        con = db()
        r = con.execute("SELECT content, content_done FROM files WHERE path=?", (path,)).fetchone()
        con.close()
        if r and r["content_done"]:
            txt = r["content"] or ""
        elif not cloud:
            txt = extract_text(path, ext, size, 60_000)
    except Exception:
        txt = ""
    _CONTENT[path] = txt
    return txt


def terms_for(f):
    """Words that describe a file: words in its name, plus how often each word appears in the text inside it."""
    if "_name" in f:
        return f["_terms"]
    nt = tokens(f["name"])
    f["_name"] = set(nt)
    f["_nameph"] = {(a, b) for a, b in zip(nt, nt[1:]) if a != b}
    f["_wc"], f["_bc"] = Counter(), Counter()
    if kind_of(f["name"]) in CONTENT_KINDS:
        txt = content_for(f["path"], split_ext(f["name"])[1], f.get("size", 0), f.get("cloud", False))[:60_000]
        if txt:
            words = [w for w in re.findall(r"[^\W\d_]{3,}", norm(txt), re.U) if w not in STOP and w not in WORDS]
            f["_wc"] = Counter(words)
            f["_bc"] = Counter((a, b) for a, b in zip(words, words[1:]) if a != b)
    # a word from inside the file only counts if it's one of the file's main words, not a passing mention
    top_w = max(f["_wc"].values(), default=0)
    top_b = max(f["_bc"].values(), default=0)
    f["_terms"] = f["_name"] | {w for w, c in f["_wc"].items() if c >= max(2, 0.25 * top_w)}
    f["_phrases"] = f["_nameph"] | {p for p, c in f["_bc"].items() if c >= max(2, 0.25 * top_b)}
    return f["_terms"]


def has_term(f, words):
    terms_for(f)
    return (words[0] in f["_terms"]) if len(words) == 1 else (tuple(words) in f["_phrases"])


def content_words(txt):
    return [w for w in re.findall(r"[^\W\d_]{3,}", norm(txt[:60_000]), re.U) if w not in STOP and w not in WORDS]


def keywords_json(txt):
    """The 40 most frequent meaningful words inside a file, stored so folders can be compared by what's in them."""
    if not txt:
        return None
    c = Counter(content_words(txt))
    return json.dumps(dict(c.most_common(40))) if c else None


def file_vec(f):
    import math
    terms_for(f)
    v = Counter()
    for w, c in f["_wc"].most_common(40):
        v[w] += 1 + math.log(c)
    for w in f["_name"]:
        v[w] += 3
    return v


FALLBACK_DIRS = ({k.lower() for k in KIND_NAMES} | {a.lower() for al in KIND_ALIASES.values() for a in al}
                 | {x[0].lower() for x in SMART} | {"desktop", "downloads", "apps", "attachments", "inbox"})


class Context:
    """What's already in a folder: its subfolders and what the files inside them are about."""

    def __init__(self, base):
        self.base = base
        self.top = subdirs(base)
        self.top_l = {d.lower(): d for d in self.top}
        self._prof = None

    def kind_folder(self, kind):
        for alias in KIND_ALIASES.get(kind, [kind]):
            if alias.lower() in self.top_l:
                return self.top_l[alias.lower()]
        return kind

    def profiles(self):
        """For every existing folder (a few levels deep): the words that describe the files inside it."""
        import math
        if self._prof is not None:
            return self._prof
        prof = defaultdict(lambda: {"vec": Counter(), "n": 0})
        df, n_files = Counter(), 0
        con = db()
        pref = self.base.rstrip(os.sep) + os.sep
        rows = con.execute("SELECT path, name, kw FROM files WHERE substr(path, 1, ?) = ?", (len(pref), pref)).fetchall()
        con.close()
        for r in rows:
            parts = os.path.relpath(r["path"], self.base).split(os.sep)[:-1]
            if not parts:
                continue
            try:
                kw = json.loads(r["kw"]) if r["kw"] else {}
            except ValueError:
                kw = {}
            for t in tokens(r["name"]):
                kw[t] = kw.get(t, 0) + 3
            if not kw:
                continue
            n_files += 1
            df.update(kw.keys())
            for i in range(1, min(len(parts), MAX_DEPTH + 2) + 1):
                p = prof[os.sep.join(parts[:i])]
                p["n"] += 1
                for w, c in kw.items():
                    p["vec"][w] += 1 + math.log(c)
        for d in all_dirs(self.base, MAX_DEPTH + 2):
            prof[d]  # folders with nothing indexed yet can still match by name
        self.df, self.N = df, max(n_files, 1)
        out = {}
        for d, p in prof.items():
            parts = d.split(os.sep)
            if any(x.startswith(("_", ".")) or GENERIC_DIR.match(x) or re.fullmatch(r"(19|20)\d\d", x) for x in parts):
                continue
            if os.path.normcase(os.path.join(self.base, d)) == os.path.normcase(APP_DIR):
                continue
            fallback = len(parts) == 1 and parts[0].lower() in FALLBACK_DIRS
            w = {t: c * self.idf(t) for t, c in p["vec"].most_common(250)}
            norm_ = math.sqrt(sum(x * x for x in w.values())) or 1
            out[d] = {"w": w, "norm": norm_, "n": p["n"], "name": set(tokens(parts[-1])), "fallback": fallback}
        self._prof = out
        return out

    def idf(self, t):
        import math
        return math.log((self.N + 2) / (self.df.get(t, 0) + 1)) + 0.5

    def best_folder(self, f):
        """The existing folder this file belongs in, judged by its name and what it's about. Picks the deepest good match."""
        import math
        prof = self.profiles()
        ft = terms_for(f)
        fv = {t: c * self.idf(t) for t, c in file_vec(f).items()}
        fn = math.sqrt(sum(x * x for x in fv.values())) or 1
        found = []
        for d, p in prof.items():
            if p["fallback"]:
                continue
            name_hit = bool(p["name"]) and p["name"] <= ft
            if p["n"] < 2 and not name_hit:
                continue
            cos = sum(v * p["w"].get(t, 0) for t, v in fv.items()) / (fn * p["norm"]) if p["w"] else 0
            score = cos + (0.45 if name_hit else 0)
            if score >= 0.28:
                found.append((score, d, name_hit, cos))
        if not found:
            return None
        top = max(found)
        family = [x for x in found if (x[1] == top[1] or x[1].startswith(top[1] + os.sep)) and x[0] >= 0.8 * top[0]]
        return max(family, key=lambda x: (x[1].count(os.sep), x[0]))


TOPIC_KINDS = {"Documents", "PDFs", "Presentations", "Spreadsheets", "eBooks", "Images", "Design", "Audio", "Videos"}


def type_route(f, ctx):
    """Where a file goes when nothing about its content places it: a folder for its type."""
    name, kind, ext = f["name"], kind_of(f["name"]), split_ext(f["name"])[1]
    if kind == "Images" and ext in ("jpg", "jpeg", "png", "webp") and CAMERA_NAME.search(name):
        kind = "Photos"
    kf = ctx.kind_folder(kind)
    if kind in ("Photos", "Videos"):
        years = [d for d in subdirs(os.path.join(ctx.base, kf)) if re.fullmatch(r"(19|20)\d\d", d)]
        if years or f.get("_year_split"):
            return os.path.join(kf, year_of(name, f["mtime"])), f"{kind.lower()} sorted by year", False
        return kf, f"{kind[:-1].lower()} with nothing else to go on", False
    return kf, (f"no topic found, so filed by type (.{ext})" if ext else "no topic found"), kind in TOPIC_KINDS


def route(f, ctx):
    """First decision for one file: (folder, reason, mode). mode: 'final', 'topic' (group with other loose files
    by what they're about), or 'here' (may start a subfolder where it lands)."""
    name, kind = f["name"], kind_of(f["name"])
    match = ctx.best_folder(f)
    if match and (match[2] or match[3] >= 0.45):
        why = f'name matches your folder "{match[1].split(os.sep)[-1]}"' if match[2] else f'its content matches your folder "{match[1].split(os.sep)[-1]}"'
        return match[1], why, "here"
    for cat, words, kinds in SMART:
        if kinds and kind not in kinds:
            continue
        for w in words:
            if has_phrase(name, w):
                return ctx.top_l.get(cat.lower(), cat), f'name says "{w}"', "final"
    if match:
        return match[1], f'what it\'s about is close to your folder "{match[1].split(os.sep)[-1]}"', "here"
    if kind in TOPIC_KINDS and not CAMERA_NAME.search(name) and terms_for(f):
        return None, "", "topic"
    d, why, here = type_route(f, ctx)
    return d, why, "here" if here else "final"


def best_groups(files, min_group, exclude, top_level):
    """Group files by the word or two-word phrase that best sets them apart from the rest. Returns [(words, members)]."""
    import math
    n = len(files)
    for f in files:
        terms_for(f)
    df_u, df_b = Counter(), Counter()
    for f in files:
        df_u.update(t for t in f["_terms"] if t not in exclude)
        df_b.update(p for p in f["_phrases"] if p[0] not in exclude and p[1] not in exclude)
    limit = 0.8 * n if n >= 5 else n - 1   # a word on (nearly) every file doesn't set a group apart
    ok_u = {t for t, c in df_u.items() if min_group <= c <= limit}
    ok_b = {p for p, c in df_b.items() if min_group <= c <= limit}
    # each file nominates the words that are most characteristic of it within this group (tf-idf)
    nominated_u, nominated_b = set(), set()
    for f in files:
        su = sorted(((f["_wc"][t] + 5 * (t in f["_name"])) * math.log((n + 1) / df_u[t]), t) for t in f["_terms"] & ok_u)
        nominated_u.update(t for _, t in su[-6:])
        sb = sorted(((f["_bc"][p] + 5 * (p in f["_nameph"])) * math.log((n + 1) / df_b[p]), p) for p in f["_phrases"] & ok_b)
        nominated_b.update(p for _, p in sb[-3:])
    cands = [((a, b), df_b[(a, b)]) for (a, b) in nominated_b if df_b[(a, b)] >= 0.7 * min(df_u[a] or 1, df_u[b] or 1)]
    cands += [((t,), df_u[t]) for t in nominated_u]
    in_name = Counter(t for f in files for t in f["_name"])
    # bigger groups first; a two-word subject is more specific than one word, so it counts extra
    def strength(words):
        """How central the subject is to the files that mention it (1 = it's what they're mostly about)."""
        vals = []
        for f in files:
            if not has_term(f, words):
                continue
            if (len(words) == 1 and words[0] in f["_name"]) or (len(words) > 1 and tuple(words) in f["_nameph"]):
                vals.append(1.0)
                continue
            tf = f["_wc"][words[0]] if len(words) == 1 else f["_bc"][tuple(words)]
            top = max(f["_wc"].values(), default=1)  # compared with the file's most-used word, for words and phrases alike
            vals.append(min(1.0, tf / (top or 1)))
        return sum(vals) / len(vals) if vals else 0

    def rank(c):
        words, n_files = c
        named = min(in_name[w] for w in words) >= max(2, n_files / 2)  # most of the group say it in their file names
        return -(n_files * (1.2 if len(words) > 1 else 1) * (1.6 if named else 1) * (0.15 + strength(words)))
    cands.sort(key=lambda x: (rank(x), -sum(in_name[w] for w in x[0]), -len(x[0]), x[0]))
    groups, taken, used = [], set(), set()
    for words, _c in cands:
        if set(words) & used and len(words) == 1:
            continue  # a single word that is already part of a two-word folder
        members = [f for f in files if id(f) not in taken and has_term(f, words)]
        if len(members) >= min_group:
            groups.append((words, members))
            taken.update(id(m) for m in members)
            used.update(words)
    return groups


def cluster_tree(files, min_group, exclude=frozenset(), depth=1, moving=None):
    """Folders inside folders: returns {file id: [folder, subfolder, ...]} for files that belong in a topic folder.
    A folder is only made if at least one of `moving` (files being sorted now) goes into it, so files already
    in place aren't reshuffled when something new arrives."""
    out = {}
    if len(files) < min_group or depth > MAX_DEPTH:
        return out
    forms = orig_forms([f["name"] for f in files])
    for words, members in best_groups(files, min_group, exclude, depth == 1):
        if moving is not None and not any(id(m) in moving for m in members):
            continue
        label = pretty(words, forms)
        for m in members:
            out[id(m)] = [label]
        if len(members) >= 2 * min_group:
            sub = cluster_tree(members, min_group, frozenset(exclude | set(words)), depth + 1, moving)
            for k, path in sub.items():
                out[k] = [label] + path
    return out


def make_plan(base, files=None):
    """Work out where every loose file in `base` goes. `files` limits it to some files (used when new files arrive)."""
    _CONTENT.clear()
    ctx = Context(base)
    minimum = int(SETTINGS.get("min_group", 3))
    loose = files if files is not None else loose_files(base)
    photo = lambda f: "Photos" if (kind_of(f["name"]) == "Images" and CAMERA_NAME.search(f["name"])) else kind_of(f["name"])
    by_kind = Counter(photo(f) for f in loose)
    for f in loose:
        k = photo(f)
        existing = len(loose_files(os.path.join(base, ctx.kind_folder(k)))) if k in ("Photos", "Videos") else 0
        f["_year_split"] = by_kind[k] + existing >= 15

    def mv(f, dest, reason):
        return {"src": os.path.relpath(f["path"], base), "name": f["name"], "dest": dest, "reason": reason,
                "kind": kind_of(f["name"]), "size": f["size"], "cloud": f.get("cloud", False)}

    moves, here, topic = [], defaultdict(list), []
    for f in loose:
        dest, reason, mode = route(f, ctx)
        if mode == "topic":
            topic.append(f)
            continue
        m = mv(f, dest, reason)
        moves.append(m)
        if mode == "here":
            here[dest].append((f, m))

    # 1. loose files that share a subject get new topic folders (and folders inside them), whatever their type
    tree = cluster_tree(topic, minimum) if topic else {}
    for f in topic:
        path = tree.get(id(f))
        if path:
            m = mv(f, os.path.join(*path), f'about "{path[-1]}"' + (f" (inside {' › '.join(path[:-1])})" if len(path) > 1 else "")
                   + ", like other files here")
            moves.append(m)
        else:
            d, why, _ = type_route(f, ctx)
            m = mv(f, d, why)
            moves.append(m)
            here[d].append((f, m))

    # 2. where files land in an existing folder, related files can start a subfolder there
    extra = []
    for d, items in here.items():
        existing = loose_files(os.path.join(base, d)) if os.path.isdir(os.path.join(base, d)) else []
        everyone = [f for f, _ in items] + existing
        exclude = frozenset(t for part in d.split(os.sep) for t in tokens(part))
        by_id = {id(f): m for f, m in items}
        sub = cluster_tree(everyone, minimum, exclude, moving=set(by_id))
        for f in everyone:
            path = sub.get(id(f))
            if not path:
                continue
            target = os.path.join(d, *path)
            why = f'about "{path[-1]}"' + (f" (inside {' › '.join(path[:-1])})" if len(path) > 1 else "")
            if id(f) in by_id:
                by_id[id(f)]["dest"], by_id[id(f)]["reason"] = target, why
            else:
                extra.append(mv(f, target, why))
    moves += extra
    for m in moves:
        m["dest_kind"] = top_kind(m["dest"].split(os.sep)[0])
    existing_dirs = {os.path.normcase(x) for x in all_dirs(base, MAX_DEPTH + 2)}
    folders = Counter(m["dest"] for m in moves)
    out_folders = [{"rel": d, "top": d.split(os.sep)[0], "count": c, "new": os.path.normcase(d) not in existing_dirs,
                    "kind": top_kind(d.split(os.sep)[0])}
                   for d, c in sorted(folders.items(), key=lambda x: x[0].lower())]
    return {"moves": moves, "folders": out_folders}


def plan_when_ready(base):
    """Plans need to know what's inside existing folders, so finish reading the folder first."""
    for _ in range(240):
        if not STATE["scanning"] and not QUEUED:
            break
        time.sleep(0.5)
    root = base_of(base) or base
    con = db()
    known = con.execute("SELECT COUNT(*) FROM files WHERE base=?", (root,)).fetchone()[0]
    con.close()
    if not known:
        scan(root)
    return make_plan(base)


def all_dirs(base, depth=3):
    out = []
    stack = [("", 0)]
    while stack:
        rel, d = stack.pop()
        for n in subdirs(os.path.join(base, rel)):
            r = os.path.join(rel, n) if rel else n
            out.append(r)
            if d + 1 < depth:
                stack.append((r, d + 1))
    return out


def top_kind(top):
    t = top.lower()
    for k in KIND_NAMES:
        if t == k.lower():
            return k
    for k, al in KIND_ALIASES.items():
        if t in (a.lower() for a in al):
            return k
    for s in SMART:
        if t == s[0].lower():
            return s[0]
    return "Folder"


# ------------------------------------------------------------------ moving, undo, delete
def new_batch():
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def move_one(con, base, src_rel, dest_rel, batch, label):
    src = os.path.normpath(os.path.join(base, src_rel))
    dest_dir = os.path.normpath(os.path.join(base, dest_rel))
    if ".." in dest_rel.replace("\\", "/").split("/") or not inside(src, base) or not inside(dest_dir, base):
        raise ValueError("outside the connected folder")
    if not os.path.exists(src):
        raise FileNotFoundError("file is gone")
    if os.path.normcase(os.path.dirname(src)) == os.path.normcase(dest_dir):
        return src
    os.makedirs(dest_dir, exist_ok=True)
    dest = unique_dest(os.path.join(dest_dir, os.path.basename(src)))
    os.rename(src, dest)  # same drive: instant, and online-only files stay online-only
    con.execute("INSERT INTO journal(batch,ts,action,src,dst,label) VALUES(?,?,?,?,?,?)", (batch, time.time(), "move", src, dest, label))
    row = con.execute("SELECT id FROM files WHERE path=?", (src,)).fetchone()
    if row:
        rel = os.path.relpath(dest, base)
        forget_path(con, dest, row["id"])
        con.execute("UPDATE files SET path=?, rel=?, dir=?, name=? WHERE id=?", (dest, rel, os.path.dirname(rel), os.path.basename(dest), row["id"]))
        fts_row(con, row["id"])
    else:
        fid = upsert(con, base, dest)
        if fid:
            fts_row(con, fid)
    return dest


def apply_moves(base, moves, batch=None, label="Sorted"):
    batch = batch or new_batch()
    con = db()
    done, errors = [], []
    for m in moves:
        try:
            dest = move_one(con, base, m["src"], m["dest"], batch, label)
            done.append({"src": m["src"], "dest": os.path.relpath(dest, base)})
        except Exception as e:
            errors.append({"src": m["src"], "error": str(e)})
    con.commit(); con.close()
    return {"batch": batch, "moved": done, "errors": errors}


def undo(batch):
    con = db()
    rows = con.execute("SELECT * FROM journal WHERE batch=? AND undone=0 ORDER BY id DESC", (batch,)).fetchall()
    ok, errs = 0, []
    for r in rows:
        if r["action"] not in ("move", "rename"):
            continue
        if not os.path.exists(r["dst"]):
            errs.append(os.path.basename(r["dst"])); continue
        src = r["src"] if not os.path.exists(r["src"]) else unique_dest(r["src"])
        try:
            os.makedirs(os.path.dirname(src), exist_ok=True)
            os.rename(r["dst"], src)
        except OSError as e:
            errs.append(str(e)); continue
        b = base_of(src)
        row = con.execute("SELECT id FROM files WHERE path=?", (r["dst"],)).fetchone()
        if row and b:
            rel = os.path.relpath(src, b)
            forget_path(con, src, row["id"])
            con.execute("UPDATE files SET path=?,rel=?,dir=?,name=? WHERE id=?", (src, rel, os.path.dirname(rel), os.path.basename(src), row["id"]))
            fts_row(con, row["id"])
        con.execute("UPDATE journal SET undone=1 WHERE id=?", (r["id"],))
        ok += 1
        # remove folders this batch created and left empty
        d = os.path.dirname(r["dst"])
        while b and inside(d, b) and os.path.normcase(d) != os.path.normcase(b):
            try:
                os.rmdir(d)
            except OSError:
                break
            d = os.path.dirname(d)
    con.commit(); con.close()
    return {"reverted": ok, "errors": errs}


def to_recycle_bin(path):
    if IS_WIN:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                        ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                        ("lpszProgressTitle", wintypes.LPCWSTR)]
        op = SHFILEOPSTRUCTW(None, 3, os.path.abspath(path) + "\0", None, 0x40 | 0x10 | 0x4 | 0x400, False, None, None)
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if rc != 0 or op.fAnyOperationsAborted:
            raise OSError(f"Windows couldn't move it to the Recycle Bin (code {rc})")
    elif IS_MAC:
        import shutil
        trash = os.path.join(os.path.expanduser("~"), ".Trash")
        dest = unique_dest(os.path.join(trash, os.path.basename(path)))
        shutil.move(path, dest)
    else:  # other systems: park in a _Deleted folder next to the connected folder
        b = base_of(path) or os.path.dirname(path)
        dest = unique_dest(os.path.join(b, "_Deleted", os.path.basename(path)))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(path, dest)


def delete_paths(paths):
    con = db()
    batch = new_batch()
    done, errs = [], []
    for p in paths:
        if not base_of(p) or os.path.normcase(p) in {os.path.normcase(b) for b in bases()}:
            errs.append({"path": p, "error": "not inside a connected folder"}); continue
        try:
            to_recycle_bin(p)
        except OSError as e:
            errs.append({"path": p, "error": str(e)}); continue
        con.execute("INSERT INTO journal(batch,ts,action,src,dst,label) VALUES(?,?,?,?,?,?)",
                    (batch, time.time(), "recycle", p, "", ("Sent to Bin" if IS_MAC else "Sent to Recycle Bin")))
        like = p.rstrip(os.sep) + os.sep + "%"
        for r in con.execute("SELECT id FROM files WHERE path=? OR path LIKE ?", (p, like)).fetchall():
            con.execute("DELETE FROM files WHERE id=?", (r["id"],))
            if STATE.get("fts"):
                con.execute("DELETE FROM fts WHERE rowid=?", (r["id"],))
        done.append(p)
    con.commit(); con.close()
    if done:
        push_event("delete", f"Sent {len(done)} item{'s' if len(done) > 1 else ''} to the " + ("Bin" if IS_MAC else "Recycle Bin"))
    return {"deleted": done, "errors": errs}


# ------------------------------------------------------------------ watcher
PENDING = {}  # path -> (size, mtime, first_seen, stable_count, tries)


def watcher():
    while True:
        time.sleep(3)
        try:
            watch_once()
        except Exception as e:  # keep watching whatever happens
            log(f"watcher error: {e!r}")


def watch_once():
    settle = float(SETTINGS.get("settle_seconds", 4))
    now = time.time()
    seen = set()
    for fe in list(SETTINGS["folders"]):
        if not (fe.get("managed") and fe.get("watch")) or not os.path.isdir(fe["path"]):
            continue
        base = fe["path"]
        ready = []
        for f in loose_files(base):
            p = f["path"]
            seen.add(p)
            prev = PENDING.get(p)
            if not prev or prev[0] != f["size"] or prev[1] != f["mtime"]:
                PENDING[p] = (f["size"], f["mtime"], now, 0, prev[4] if prev else 0)
                continue
            size, mt, first, stable, tries = prev
            PENDING[p] = (size, mt, first, stable + 1, tries)
            if stable + 1 >= 1 and now - max(mt, first) >= settle and tries < 20:
                ready.append(f)
        if ready:
            file_new(base, ready)
    for p in list(PENDING):
        if p not in seen:
            PENDING.pop(p, None)


def file_new(base, ready):
    plan = make_plan(base, ready)
    if not plan["moves"]:
        return
    batch = new_batch()
    res = apply_moves(base, plan["moves"], batch, "Sorted automatically")
    for e in res["errors"]:  # probably still open in another app; try again shortly
        p = os.path.join(base, e["src"])
        if p in PENDING:
            s = PENDING[p]
            PENDING[p] = (s[0], s[1], time.time(), 0, s[4] + 1)
    newdirs = {f["rel"] for f in plan["folders"] if f["new"]}
    for mv in res["moved"]:
        PENDING.pop(os.path.join(base, mv["src"]), None)
        name = os.path.basename(mv["src"])
        dest_dir = os.path.dirname(mv["dest"])
        push_event("filed", f"{name} → {dest_dir}", base=base, name=name, dest=dest_dir,
                   new_folder=dest_dir in newdirs, batch=batch)
    if res["moved"]:
        log(f"auto-filed {len(res['moved'])} in {base}")
        threading.Thread(target=scan, args=(base,), daemon=True).start()  # read what's inside the new files


# ------------------------------------------------------------------ browsing & search
def fdict(r, con=None):
    d = {k: r[k] for k in ("id", "name", "ext", "kind", "size", "mtime", "dir", "rel", "cloud")}
    d["base"] = r["base"]
    if con is not None:
        m = con.execute("SELECT tags,note FROM meta WHERE key=?", (meta_key(r["name"], r["size"]),)).fetchone()
        d["tags"] = [t for t in (m["tags"].split(",") if m and m["tags"] else []) if t]
        d["note"] = m["note"] if m else ""
    return d


def browse(base, rel=""):
    if not base_of(os.path.join(base, rel)) and not folder_entry(base):
        return {"error": "That folder isn't connected."}
    path = os.path.normpath(os.path.join(base, rel)) if rel else base
    if not inside(path, base) or not os.path.isdir(path):
        return {"error": "That folder no longer exists."}
    con = db()
    stamp = 0
    files = []
    for f in loose_files(path):
        fid = upsert(con, base, f["path"], None, stamp)
        r = con.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if r:
            files.append(fdict(r))
    con.commit()
    # folder stats from the index
    pref = (rel + os.sep) if rel else ""
    stats = defaultdict(lambda: {"n": 0, "s": 0, "kinds": Counter(), "thumbs": []})
    for r in con.execute("SELECT id,rel,kind,size,ext,cloud FROM files WHERE base=? AND rel LIKE ?", (base, pref.replace("%", "") + "%")):
        rest = r["rel"][len(pref):]
        if os.sep not in rest:
            continue
        top = rest.split(os.sep)[0]
        s = stats[top]
        s["n"] += 1; s["s"] += r["size"] or 0; s["kinds"][r["kind"]] += 1
        if r["kind"] in ("Images", "Photos") and not r["cloud"] and r["ext"] in ("jpg", "jpeg", "png", "gif", "webp") and len(s["thumbs"]) < 3:
            s["thumbs"].append(r["id"])
    con.close()
    folders = []
    for d in subdirs(path):
        s = stats.get(d, {"n": 0, "s": 0, "kinds": Counter(), "thumbs": []})
        k = top_kind(d) if not rel else (top_kind(d) if top_kind(rel.split(os.sep)[0]) == "Folder" else top_kind(rel.split(os.sep)[0]))
        folders.append({"name": d, "rel": os.path.join(rel, d) if rel else d, "count": s["n"], "size": s["s"], "kind": k,
                        "thumbs": s["thumbs"], "generic": bool(GENERIC_DIR.match(d)), "subs": sorted(subdirs(os.path.join(path, d)), key=str.lower)[:4]})
    folders.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())
    fe = folder_entry(base) or {}
    return {"base": base, "rel": rel, "name": os.path.basename(path) or path, "folders": folders, "files": files,
            "managed": fe.get("managed", False), "watch": fe.get("watch", False)}


def fts_query(q):
    out = []
    for t in re.findall(r'"[^"]+"|[\w\'’-]+', q, re.U):
        if t.startswith('"'):
            out.append(t)
        elif t.upper() not in ("AND", "OR", "NOT"):
            out.append('"' + t.replace('"', "") + '"*')
    return " ".join(out)


def search(p):
    q = (p.get("q") or "").strip()
    base = p.get("base") or ""
    kind = p.get("kind") or ""
    con = db()
    where, args, join, snip, order = [], [], "", "NULL AS snip", "f.mtime DESC"
    if q and STATE.get("fts") and fts_query(q):
        join = "JOIN fts ON fts.rowid=f.id"
        where.append("fts MATCH ?"); args.append(fts_query(q))
        snip = "snippet(fts, 2, '[[', ']]', ' … ', 12) AS snip"
        order = "bm25(fts, 10.0, 5.0, 1.0, 6.0)"
    elif q:
        for t in q.split():
            where.append("(f.name LIKE ? OR f.dir LIKE ? OR f.content LIKE ?)"); args += [f"%{t}%"] * 3
    if base:
        where.append("f.base=?"); args.append(base)
    if kind:
        where.append("f.kind=?"); args.append(kind)
    known = bases()
    if known:
        where.append("f.base IN (%s)" % ",".join("?" * len(known))); args += known
    sql = f"SELECT f.*, {snip} FROM files f {join}" + (" WHERE " + " AND ".join(where) if where else "")
    try:
        total = con.execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
        rows = con.execute(f"{sql} ORDER BY {order} LIMIT 120", args).fetchall()
    except sqlite3.OperationalError as e:
        con.close()
        return {"total": 0, "results": [], "folders": [], "error": str(e)}
    res = []
    for r in rows:
        d = fdict(r, con)
        d["snippet"] = r["snip"]
        res.append(d)
    # matching folders
    folders = []
    if q:
        seen = set()
        ql = [w.lower() for w in q.split()]
        for r in con.execute("SELECT DISTINCT base, dir FROM files WHERE dir<>''" + (" AND base=?" if base else ""), ([base] if base else [])):
            if r["base"] not in known:
                continue
            parts = r["dir"].split(os.sep)
            for i in range(len(parts)):
                sub = os.sep.join(parts[:i + 1])
                key = (r["base"], sub)
                if key in seen:
                    continue
                if all(w in parts[i].lower() for w in ql):
                    seen.add(key)
                    folders.append({"base": r["base"], "rel": sub, "name": parts[i]})
        folders = folders[:24]
    con.close()
    return {"total": total, "results": res, "folders": folders}


# ------------------------------------------------------------------ duplicates
def keep_score(r):
    s = r["dir"].count(os.sep) + (1 if r["dir"] else 0)
    if re.search(r"\(\d+\)|\bcopy\b|-\w{4,6}-\d{4,6}-w\b|\s-\s\w+-pc\b", r["name"], re.I):
        s -= 3
    return (s, -r["mtime"])


def duplicates():
    con = db()
    known = bases()
    if not known:
        return {"files": [], "folders": []}
    ph = ",".join("?" * len(known))
    rows = con.execute(f"SELECT * FROM files WHERE base IN ({ph}) AND size>0", known).fetchall()
    by_hash, by_namesize = defaultdict(list), defaultdict(list)
    for r in rows:
        if r["hash"]:
            by_hash[r["hash"]].append(r)
        by_namesize[(r["name"].lower(), r["size"])].append(r)
    groups = [("identical", g) for g in by_hash.values() if len(g) > 1]
    for g in by_namesize.values():
        if len(g) > 1 and any(r["cloud"] for r in g) and not all(r["hash"] for r in g):
            if len({r["hash"] for r in g if r["hash"]}) <= 1:
                groups.append(("same name and size (online-only, not compared byte by byte)", g))

    # duplicate folders: same file names and sizes all the way down
    content = defaultdict(list)
    for r in rows:
        parts = r["rel"].split(os.sep)
        for i in range(1, len(parts)):
            content[(r["base"], os.sep.join(parts[:i]))].append((os.sep.join(parts[i:]).lower(), r["size"], r["hash"] or ""))
    sigs = defaultdict(list)
    for key, items in content.items():
        if len(items) >= 1:
            items.sort()
            sigs[hashlib.sha1(repr([(a, b) for a, b, _ in items]).encode()).hexdigest()].append(key)
    dup_dirs = []
    in_dup = set()
    for keys in sigs.values():
        if len(keys) < 2:
            continue
        dup_dirs.append(keys)
        in_dup.update(keys)
    # keep only the highest folders (if two parents match, their children obviously do too)
    def parent_dup(k):
        b, rel = k
        parts = rel.split(os.sep)
        return any((b, os.sep.join(parts[:i])) in in_dup for i in range(1, len(parts)))
    folder_groups = []
    for keys in dup_dirs:
        keys = [k for k in keys if not parent_dup(k)]
        if len(keys) < 2:
            continue
        items = content[keys[0]]
        entries = []
        for b, rel in keys:
            path = os.path.join(b, rel)
            thumbs = [x["id"] for x in con.execute("""SELECT id FROM files WHERE base=? AND rel LIKE ? AND kind IN ('Images','Photos')
                        AND cloud=0 AND ext IN ('jpg','jpeg','png','gif','webp') LIMIT 4""", (b, rel + os.sep + "%"))]
            mt = con.execute("SELECT MAX(mtime) FROM files WHERE base=? AND rel LIKE ?", (b, rel + os.sep + "%")).fetchone()[0]
            entries.append({"path": path, "base": b, "rel": rel, "name": os.path.basename(rel), "thumbs": thumbs, "mtime": mt or 0,
                            "depth": rel.count(os.sep)})
        entries.sort(key=lambda e: (-e["depth"], -e["mtime"]))
        folder_groups.append({"type": "folders", "count": len(items), "size": sum(s for _, s, _ in items),
                              "list": [a for a, _, _ in items[:12]], "items": entries, "keep": entries[0]["path"]})
    covered = {(b, rel) for g in folder_groups for e in g["items"] for b, rel in [(e["base"], e["rel"])]}

    def in_covered(r):
        parts = r["rel"].split(os.sep)
        return any((r["base"], os.sep.join(parts[:i])) in covered for i in range(1, len(parts)))
    file_groups = []
    for label, g in groups:
        g = [r for r in g if not in_covered(r)]
        if len(g) < 2:
            continue
        g.sort(key=keep_score, reverse=True)
        k = g[0]["kind"]
        file_groups.append({"type": DUP_GROUP.get(k, "other"), "kind": k, "how": label, "size": g[0]["size"],
                            "items": [fdict(r) for r in g], "keep": g[0]["id"]})
    con.close()
    file_groups.sort(key=lambda x: -x["size"] * (len(x["items"]) - 1))
    folder_groups.sort(key=lambda x: -x["size"])
    return {"files": file_groups, "folders": folder_groups}


# ------------------------------------------------------------------ misc actions
def file_row(fid):
    con = db()
    r = con.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    con.close()
    return r


def open_path(path, reveal=False):
    if not base_of(path):
        return {"error": "That isn't inside a connected folder."}
    if IS_WIN:
        if reveal:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path] if reveal else ["open", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path) if reveal else path])
    return {"ok": True}


def rename(fid, new):
    new = new.strip()
    if not new or re.search(r'[<>:"/\\|?*]', new):
        return {"error": 'Names can\'t contain < > : " / \\ | ? *'}
    r = file_row(fid)
    if not r:
        return {"error": "That file isn't in the index any more."}
    ext = split_ext(r["name"])[1]
    if ext and not new.lower().endswith("." + ext):
        new += "." + ext
    dest = unique_dest(os.path.join(os.path.dirname(r["path"]), new))
    os.rename(r["path"], dest)
    con = db()
    con.execute("INSERT INTO journal(batch,ts,action,src,dst,label) VALUES(?,?,?,?,?,?)", (new_batch(), time.time(), "rename", r["path"], dest, "Renamed"))
    con.execute("UPDATE meta SET key=? WHERE key=?", (meta_key(os.path.basename(dest), r["size"]), meta_key(r["name"], r["size"])))
    rel = os.path.relpath(dest, r["base"])
    forget_path(con, dest, fid)
    con.execute("UPDATE files SET path=?,rel=?,name=? WHERE id=?", (dest, rel, os.path.basename(dest), fid))
    fts_row(con, fid)
    con.commit(); con.close()
    return {"ok": True, "name": os.path.basename(dest)}


def set_meta(fid, tags, note):
    r = file_row(fid)
    if not r:
        return {"error": "not found"}
    t = ",".join(sorted({x.strip().lower() for x in tags if x.strip()}))
    con = db()
    con.execute("INSERT OR REPLACE INTO meta(key,tags,note) VALUES(?,?,?)", (meta_key(r["name"], r["size"]), t, note or ""))
    fts_row(con, fid)
    con.commit(); con.close()
    return {"ok": True}


def history():
    con = db()
    rows = con.execute("""SELECT batch,label,MIN(ts) ts,COUNT(*) n,SUM(undone) u,MAX(action) a FROM journal GROUP BY batch
                          ORDER BY ts DESC LIMIT 150""").fetchall()
    out = []
    for r in rows:
        sample = [{"from": x["src"], "to": x["dst"]} for x in con.execute("SELECT src,dst FROM journal WHERE batch=? LIMIT 4", (r["batch"],))]
        out.append({"batch": r["batch"], "label": r["label"], "ts": r["ts"], "n": r["n"], "undone": r["u"] == r["n"],
                    "undoable": r["a"] != "recycle", "sample": sample})
    con.close()
    return out


def preview_text(fid):
    r = file_row(fid)
    if not r:
        return {"text": ""}
    if r["content"]:
        return {"text": r["content"][:6000]}
    if r["cloud"]:
        return {"text": "", "cloud": True}
    return {"text": extract_text(r["path"], r["ext"], r["size"], 6000)}


def _win_pick_folder(title):
    """The standard Windows 'Select folder' dialog (IFileOpenDialog), owned by the SortWise window so it opens in front."""
    import ctypes
    from ctypes import wintypes, byref, c_void_p, c_long, c_ulong, c_wchar_p, POINTER, WINFUNCTYPE

    class GUID(ctypes.Structure):
        _fields_ = [("a", c_ulong), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

    ole32 = ctypes.windll.ole32

    def guid(text):
        g = GUID()
        if ole32.CLSIDFromString(c_wchar_p(text), byref(g)) != 0:
            raise OSError("bad guid")
        return g

    def method(obj, index, *argtypes):
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
        fn = WINFUNCTYPE(c_long, c_void_p, *argtypes)(vtbl[index])
        return lambda *a: fn(obj, *a)

    result = {}

    def worker():
        ole32.CoInitializeEx(None, 0x2)  # apartment threaded, as dialogs require
        dlg = c_void_p()
        try:
            hr = ole32.CoCreateInstance(byref(guid("{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}")), None, 1,
                                        byref(guid("{D57C7288-D4AD-4768-BE02-9D969532D960}")), byref(dlg))
            if hr != 0:
                raise OSError(f"CoCreateInstance {hr:#x}")
            opts = c_ulong()
            method(dlg, 10, POINTER(c_ulong))(byref(opts))                       # GetOptions
            method(dlg, 9, c_ulong)(opts.value | 0x20 | 0x40 | 0x800)            # SetOptions: pick folders, file system, must exist
            method(dlg, 17, c_wchar_p)(title)                                     # SetTitle
            owner = ctypes.windll.user32.GetForegroundWindow()
            hr = method(dlg, 3, wintypes.HWND)(owner)                             # Show
            if hr != 0:
                result["path"] = ""   # cancelled
                return
            item = c_void_p()
            if method(dlg, 20, POINTER(c_void_p))(byref(item)) != 0:             # GetResult
                raise OSError("GetResult failed")
            name = c_wchar_p()
            if method(item, 5, c_ulong, POINTER(c_wchar_p))(0x80058000, byref(name)) != 0:  # GetDisplayName(SIGDN_FILESYSPATH)
                raise OSError("GetDisplayName failed")
            result["path"] = name.value or ""
            ole32.CoTaskMemFree(name)
            method(item, 2)()                                                     # Release
        except Exception as e:
            result["error"] = e
        finally:
            if dlg:
                try:
                    method(dlg, 2)()
                except Exception:
                    pass
            ole32.CoUninitialize()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result.get("path", "")


def pick_folder():
    title = "Choose a folder for SortWise to look after"
    try:
        if IS_WIN:
            try:
                out = _win_pick_folder(title)
            except Exception as e:
                log(f"folder dialog fallback: {e!r}")
                ps = ("Add-Type -AssemblyName System.Windows.Forms;[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                      "$f=New-Object System.Windows.Forms.FolderBrowserDialog;$f.Description='" + title + "';"
                      "$o=New-Object System.Windows.Forms.Form -Property @{TopMost=$true;ShowInTaskbar=$false;WindowState='Minimized'};$o.Show();$o.Hide();"
                      "if($f.ShowDialog($o) -eq 'OK'){$f.SelectedPath}")
                out = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, text=True,
                                     encoding="utf-8", timeout=900, creationflags=0x08000000).stdout.strip()
        elif IS_MAC:
            script = ['-e', 'activate', '-e', f'POSIX path of (choose folder with prompt "{title}")']
            r = subprocess.run(["osascript", *script], capture_output=True, text=True, timeout=900)
            out = r.stdout.strip()
        else:
            code = ("import tkinter as tk\nfrom tkinter import filedialog\nr=tk.Tk();r.withdraw()\n"
                    f"print(filedialog.askdirectory(title='{title}') or '')")
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=900).stdout.strip()
    except Exception as e:
        return {"error": f"Couldn't open the folder dialog ({e}). Paste the folder path instead."}
    return {"path": os.path.normpath(out) if out else ""}


def suggestions():
    home = os.path.expanduser("~")
    out = []
    for n in ("Desktop", "Downloads", "Documents", "Pictures", "Videos", "Music"):
        p = os.path.join(home, n)
        if os.path.isdir(p):
            out.append({"name": n, "path": p})
    try:
        for n in sorted(os.listdir(home)):
            p = os.path.join(home, n)
            if n.lower().startswith("onedrive") and os.path.isdir(p):
                out.append({"name": n, "path": p})
                for sub in ("Desktop", "Documents", "Pictures"):
                    sp = os.path.join(p, sub)
                    if os.path.isdir(sp) and not any(x["name"] == sub and x["path"] == os.path.join(home, sub) and os.path.isdir(os.path.join(home, sub)) for x in out):
                        out.append({"name": f"{sub} (in {n})", "path": sp})
    except OSError:
        pass
    have = {os.path.normcase(f["path"]) for f in SETTINGS["folders"]}
    return [x for x in out if os.path.normcase(x["path"]) not in have]


def connect(path):
    path = os.path.normpath(os.path.expanduser(path.strip().strip('"')))
    if not os.path.isdir(path):
        return {"error": "That folder doesn't exist. Check the path and try again."}
    if os.path.normcase(path) in (os.path.normcase(os.path.splitdrive(path)[0] + os.sep), os.path.normcase(os.path.expanduser("~"))):
        return {"error": "That's a whole drive or your entire user folder. Connect a specific folder instead, like Downloads or Desktop."}
    if inside(APP_DIR, path) and os.path.normcase(path) == os.path.normcase(APP_DIR):
        return {"error": "That's SortWise's own folder."}
    if folder_entry(path):
        return {"ok": True, "path": path}
    SETTINGS["folders"].append({"path": path, "managed": False, "watch": False, "added": time.time()})
    save_settings()
    threading.Thread(target=scan, args=(path,), daemon=True).start()
    return {"ok": True, "path": path}


def disconnect(path):
    SETTINGS["folders"] = [f for f in SETTINGS["folders"] if os.path.normcase(os.path.normpath(f["path"])) != os.path.normcase(os.path.normpath(path))]
    for p in list(PENDING):
        if inside(p, path):
            PENDING.pop(p, None)
    save_settings()
    con = db()
    for r in con.execute("SELECT id FROM files WHERE base=?", (path,)).fetchall():
        if STATE.get("fts"):
            con.execute("DELETE FROM fts WHERE rowid=?", (r["id"],))
    con.execute("DELETE FROM files WHERE base=?", (path,))
    con.commit(); con.close()
    return {"ok": True}


def folders_summary():
    out = []
    con = db()
    for f in SETTINGS["folders"]:
        exists = os.path.isdir(f["path"])
        loose = len(loose_files(f["path"])) if exists else 0
        n, size = con.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE base=?", (f["path"],)).fetchone()
        out.append({**f, "name": os.path.basename(f["path"].rstrip(os.sep)) or f["path"], "exists": exists, "loose": loose,
                    "total": n, "bytes": size})
    con.close()
    return out


def home():
    """Everything the Home page shows: how many files have been sorted, what's waiting, and what happened lately."""
    con = db()
    week = time.time() - 7 * 86400
    moved = con.execute("SELECT COUNT(*) FROM journal WHERE action='move' AND undone=0").fetchone()[0]
    moved_week = con.execute("SELECT COUNT(*) FROM journal WHERE action='move' AND undone=0 AND ts>=?", (week,)).fetchone()[0]
    recent = []
    for r in con.execute("""SELECT batch, label, MAX(ts) ts, COUNT(*) n, SUM(undone) u FROM journal GROUP BY batch
                            ORDER BY ts DESC LIMIT 6""").fetchall():
        names = [os.path.basename(x["dst"] or x["src"]) for x in con.execute("SELECT src,dst FROM journal WHERE batch=? LIMIT 3", (r["batch"],))]
        recent.append({"label": r["label"], "ts": r["ts"], "n": r["n"], "undone": r["u"] == r["n"], "names": names})
    con.close()
    folders = folders_summary()
    try:
        d = duplicates()
        groups = d["files"] + d["folders"]
        dup = {"sets": len(groups), "bytes": sum(g["size"] * (len(g["items"]) - 1) for g in groups)}
    except Exception:
        dup = {"sets": 0, "bytes": 0}
    return {"folders": folders, "sorted": moved, "sorted_week": moved_week, "recent": recent, "duplicates": dup,
            "waiting": sum(f["loose"] for f in folders), "watching": sum(1 for f in folders if f.get("managed") and f.get("watch")),
            "state": STATE}


def launch_cmd(background=False):
    """How to start SortWise again: the app itself when packaged, otherwise Python + this script."""
    args = ["--background"] if background else []
    if FROZEN:
        return sys.executable, args
    exe = sys.executable
    if IS_WIN:
        exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return exe, [os.path.abspath(__file__)] + args


def make_shortcut(lnk, background=False, desc="SortWise"):
    target, args = launch_cmd(background)
    q = lambda x: x.replace("'", "''")
    argstr = " ".join(f'\"{q(a)}\"' if " " in a else q(a) for a in args)
    icon = f"{q(target)},0" if FROZEN else "%SystemRoot%\\System32\\imageres.dll,3"
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(lnk)}');$s.TargetPath='{q(target)}';"
          f"$s.Arguments='{argstr}';$s.WorkingDirectory='{q(APP_DIR)}';$s.IconLocation='{icon}';$s.Description='{desc}';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=0x08000000, timeout=60)


def desktop_shortcut_once():
    """Running from source on Windows: put a SortWise shortcut on the Desktop once. (The installer does this for the app.)"""
    marker = os.path.join(DATA_DIR, ".shortcut")
    if not IS_WIN or FROZEN or os.path.exists(marker):
        return
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                             capture_output=True, text=True, creationflags=0x08000000, timeout=30).stdout.strip()
        if out:
            make_shortcut(os.path.join(out, "SortWise.lnk"), False, "SortWise")
        open(marker, "w").close()
    except Exception as e:
        log(f"couldn't make desktop shortcut: {e!r}")


MAC_AGENT = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", "com.theganeshlife.sortwise.plist")


def startup_path():
    if IS_WIN:
        return os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup", "SortWise.lnk")
    if IS_MAC:
        return MAC_AGENT
    return None


def starts_at_login():
    p = startup_path()
    return bool(p and os.path.exists(p))


def migrate_startup():
    """Move a sign-in entry from an earlier install over to this one."""
    try:
        if IS_WIN:
            old = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup", "Tidy.lnk")
        elif IS_MAC:
            old = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", "com.theganeshlife.tidy.plist")
        else:
            return
        if os.path.exists(old):
            os.remove(old)
            if FROZEN:
                startup_shortcut(True)
    except Exception as e:
        log(f"couldn't move the sign-in setting over: {e!r}")


def startup_shortcut(on):
    p = startup_path()
    if not p:
        return {"error": "Starting at sign-in is available on Windows and Mac."}
    if not on:
        if os.path.exists(p):
            os.remove(p)
    elif IS_WIN:
        make_shortcut(p, True, "SortWise (background)")
    else:
        target, args = launch_cmd(True)
        items = "".join(f"<string>{html.escape(a)}</string>" for a in [target] + args)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
                    '<key>Label</key><string>com.theganeshlife.sortwise</string>'
                    f'<key>ProgramArguments</key><array>{items}</array>'
                    '<key>RunAtLoad</key><true/><key>ProcessType</key><string>Interactive</string></dict></plist>\n')
    SETTINGS["start_with_windows"] = on
    save_settings()
    return {"ok": True}


# ------------------------------------------------------------------ updates
UPDATE = {"state": "idle", "progress": 0, "message": "", "latest": None, "checked": 0}


def build_time(b):
    """Build numbers are HHMMDDMMYYYY (Sydney time of the push), e.g. 145021112026 = 14:50 on 21 Nov 2026."""
    try:
        return datetime.strptime(b, "%H%M%d%m%Y")
    except (TypeError, ValueError):
        return None


def build_label(b):
    t = build_time(b)
    return t.strftime("%d %b %Y, %H:%M") if t else b


def _https(url, accept="application/vnd.github+json", timeout=20):
    import ssl
    import urllib.request
    try:
        import certifi  # type: ignore
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": f"SortWise/{__version__}", "Accept": accept})
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def install_kind():
    if not FROZEN:
        return "source"
    if IS_WIN:
        return "installer" if os.path.exists(os.path.join(APP_DIR, "unins000.exe")) else "portable"
    if IS_MAC:
        return "mac"
    return "other"


def pick_asset(assets):
    import platform
    kind = install_kind()
    for a in assets:
        n = a["name"].lower()
        if kind == "installer" and n.startswith("sortwisesetup") and n.endswith(".exe"):
            return a
        if kind == "portable" and n.endswith("portable.exe"):
            return a
        if kind == "mac" and n.endswith(".dmg") and (("apple-silicon" in n) == (platform.machine() == "arm64")):
            return a
    return None


def check_update():
    UPDATE.update(state="checking", message="")
    try:
        with _https(f"https://api.github.com/repos/{REPO}/releases/latest") as r:
            rel = json.loads(r.read())
    except Exception as e:
        UPDATE.update(state="error", message=f"Couldn't reach GitHub ({e.__class__.__name__}). Check your internet connection.")
        return UPDATE
    m = re.search(r"Build[:\s]*(\d{12})", rel.get("body") or "")
    latest_build = m.group(1) if m else ""
    latest_ver = (rel.get("tag_name") or "").lstrip("v")
    vt = lambda v: tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)
    newer = vt(latest_ver) > vt(__version__) or (
        vt(latest_ver) == vt(__version__) and build_time(latest_build) and build_time(BUILD) and build_time(latest_build) > build_time(BUILD))
    asset = pick_asset(rel.get("assets", []))
    UPDATE.update(checked=time.time(), latest={"version": latest_ver, "build": latest_build, "label": build_label(latest_build),
                                                 "url": rel.get("html_url"), "asset": asset and {"name": asset["name"], "url": asset["browser_download_url"], "size": asset["size"]}})
    if newer and asset:
        UPDATE.update(state="available", message="")
    elif newer:
        UPDATE.update(state="manual", message="A newer build is out. Download it from GitHub.")
    else:
        UPDATE.update(state="none", message="")
    return UPDATE


def install_update():
    """Download the new build and swap it in. Settings and the index live in the data folder, so they're untouched."""
    if UPDATE.get("state") != "available":
        return {"error": "Check for updates first."}
    a = UPDATE["latest"]["asset"]
    UPDATE.update(state="downloading", progress=0, message="")

    def work():
        try:
            folder = os.path.join(DATA_DIR, "update")
            os.makedirs(folder, exist_ok=True)
            dest = os.path.join(folder, a["name"])
            with _https(a["url"], accept="application/octet-stream", timeout=60) as r, open(dest + ".part", "wb") as f:
                total, got = int(r.headers.get("Content-Length") or a["size"] or 0), 0
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    UPDATE["progress"] = int(got * 100 / total) if total else 0
            os.replace(dest + ".part", dest)
            UPDATE.update(state="installing", progress=100, message="Installing. SortWise will reopen in a moment.")
            time.sleep(1.2)
            launch_installer(dest)
        except Exception as e:
            log(f"update failed: {e!r}")
            UPDATE.update(state="error", message=f"The update didn't finish ({e}). Nothing was changed.")

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


def launch_installer(path):
    kind = install_kind()
    pid = os.getpid()
    if kind == "installer":
        subprocess.Popen([path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"], creationflags=0x00000008 | 0x00000200)
    elif kind == "portable":
        exe = sys.executable
        script = os.path.join(DATA_DIR, "update", "swap.cmd")
        with open(script, "w", encoding="utf-8") as f:
            f.write(f'@echo off\r\n:wait\r\ntasklist /FI "PID eq {pid}" | find "{pid}" >nul && (timeout /t 1 >nul & goto wait)\r\n'
                    f'move /y "{path}" "{exe}" >nul\r\nstart "" "{exe}" --background\r\n')
        subprocess.Popen(["cmd", "/c", script], creationflags=0x08000000 | 0x00000008)
    elif kind == "mac":
        app = os.path.abspath(os.path.join(os.path.dirname(sys.executable), "..", ".."))
        script = os.path.join(DATA_DIR, "update", "swap.sh")
        with open(script, "w", encoding="utf-8") as f:
            f.write(f"""#!/bin/sh
while kill -0 {pid} 2>/dev/null; do sleep 0.5; done
MNT=$(mktemp -d /tmp/sortwise-update.XXXX)
hdiutil attach -nobrowse -readonly -mountpoint "$MNT" "{path}" >/dev/null || exit 1
rm -rf "{app}.old"
mv "{app}" "{app}.old" && ditto "$MNT/SortWise.app" "{app}" && rm -rf "{app}.old" || {{ rm -rf "{app}"; mv "{app}.old" "{app}"; }}
hdiutil detach "$MNT" -quiet
xattr -dr com.apple.quarantine "{app}" 2>/dev/null
rm -f "{path}"
open "{app}" --args --background
""")
        os.chmod(script, 0o755)
        subprocess.Popen(["/bin/sh", script], start_new_session=True)
    else:
        UPDATE.update(state="manual", message="Download the new build from GitHub.")
        return
    log("handing over to the updater")
    threading.Timer(0.8, lambda: os._exit(0)).start()


def auto_check():
    """Look for a new build once a day in the background; the About page shows it."""
    time.sleep(20)
    while True:
        if install_kind() != "source":
            try:
                check_update()
            except Exception:
                pass
        time.sleep(24 * 3600)


# ------------------------------------------------------------------ window & tray
def open_ui():
    """Open SortWise in its own app-style window where possible (Edge/Chrome app mode), else the default browser."""
    url = f"http://127.0.0.1:{PORT}/"
    try:
        if IS_WIN:
            for root in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""), os.environ.get("LOCALAPPDATA", "")):
                for exe in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
                    p = os.path.join(root, exe)
                    if root and os.path.exists(p):
                        subprocess.Popen([p, f"--app={url}", "--window-size=1380,900"], creationflags=0x08000000)
                        return
        elif IS_MAC:
            for app in ("/Applications/Google Chrome.app", "/Applications/Microsoft Edge.app", "/Applications/Brave Browser.app"):
                if os.path.exists(app):
                    subprocess.Popen(["open", "-na", app, "--args", f"--app={url}", "--window-size=1380,900"])
                    return
    except Exception as e:
        log(f"app window failed, using the browser: {e!r}")
    webbrowser.open(url)


def run_tray():
    """Tray icon (Windows) / menu-bar icon (Mac). Blocks on the main thread. Returns False if unavailable."""
    try:
        import pystray  # type: ignore
        from PIL import Image  # type: ignore
    except Exception:
        return False
    try:
        img = Image.open(ICON_PATH)
    except Exception:
        img = Image.new("RGBA", (64, 64), (30, 91, 79, 255))

    def quit_app(icon, _item):
        icon.stop()
        os._exit(0)

    def watching_text(_item):
        n = sum(1 for f in SETTINGS["folders"] if f.get("managed") and f.get("watch"))
        return f"Sorting {n} folder{'s' if n != 1 else ''} automatically" if n else "No folders being sorted yet"

    menu = pystray.Menu(
        pystray.MenuItem("Open SortWise", lambda *_: open_ui(), default=True),
        pystray.MenuItem(watching_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit SortWise", quit_app),
    )
    pystray.Icon("SortWise", img, f"SortWise {__version__} (build {BUILD})", menu).run()
    return True


# ------------------------------------------------------------------ http
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (ConnectionError, OSError):
            pass  # the window closed or moved on before the reply arrived

    def host_ok(self):
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def serve_file(self, fid, q):
        if q.get("t") != TOKEN:
            return self.send(403, {"error": "forbidden"})
        r = file_row(int(fid))
        if not r or not os.path.isfile(r["path"]) or not base_of(r["path"]):
            return self.send(404, {"error": "not found"})
        size = os.path.getsize(r["path"])
        ctype = mimetypes.guess_type(r["name"])[0] or "application/octet-stream"
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "private, max-age=300")
        self.end_headers()
        try:
            with open(r["path"], "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionError, OSError):
            pass

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                with open(UI_PATH, encoding="utf-8") as f:
                    return self.send(200, f.read().replace("__TOKEN__", TOKEN).encode(), "text/html; charset=utf-8")
            if u.path in ("/favicon.png", "/favicon.ico", "/icon.png"):
                try:
                    with open(ICON_PATH, "rb") as f:
                        data = f.read()
                except OSError:
                    return self.send(404, {"error": "not found"})
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/manifest.json":
                return self.send(200, {"name": "SortWise", "short_name": "SortWise", "display": "standalone",
                                       "background_color": "#E4E9F0", "theme_color": "#1E5B4F",
                                       "icons": [{"src": "/icon.png", "sizes": "256x256", "type": "image/png"}]},
                                 "application/manifest+json")
            if u.path == "/api/hello":
                return self.send(200, {"app": "SortWise", "version": __version__, "build": BUILD, "exe": me()})
            if u.path.startswith("/file/"):
                return self.serve_file(u.path.split("/")[2], q)
            if self.headers.get("X-Token") != TOKEN:
                return self.send(403, {"error": "forbidden"})
            routes = {
                "/api/folders": lambda: {"folders": folders_summary(), "suggest": suggestions(), "state": STATE},
                "/api/browse": lambda: browse(q.get("base", ""), q.get("rel", "")),
                "/api/home": home,
                "/api/plan": lambda: plan_when_ready(q["base"]),
                "/api/search": lambda: search(q),
                "/api/duplicates": duplicates,
                "/api/history": history,
                "/api/text": lambda: preview_text(int(q["id"])),
                "/api/file": lambda: fdict(file_row(int(q["id"])), db()),
                "/api/events": lambda: {"events": [e for e in EVENTS if e["id"] > int(q.get("after", 0))], "state": STATE},
                "/api/activity": lambda: [dict(r) for r in db().execute("SELECT * FROM activity ORDER BY id DESC LIMIT 200")],
                "/api/update": lambda: {**UPDATE, "kind": install_kind()},
                "/api/settings": lambda: {**SETTINGS, "start_with_windows": starts_at_login(), "version": __version__,
                                          "build": BUILD, "build_label": build_label(BUILD),
                                          "platform": "mac" if IS_MAC else "windows" if IS_WIN else "other"},
            }
            fn = routes.get(u.path)
            return self.send(200, fn()) if fn else self.send(404, {"error": "not found"})
        except Exception as e:
            log(f"GET {u.path}: {e!r}")
            return self.send(500, {"error": str(e)})

    def do_POST(self):
        if not self.host_ok() or self.headers.get("X-Token") != TOKEN:
            return self.send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        b = json.loads(self.rfile.read(n) or b"{}")
        try:
            if u.path == "/api/connect":
                return self.send(200, connect(b["path"]))
            if u.path == "/api/disconnect":
                return self.send(200, disconnect(b["path"]))
            if u.path == "/api/pick":
                return self.send(200, pick_folder())
            if u.path == "/api/apply":
                res = apply_moves(b["base"], b["moves"], b.get("batch"), "Sorted")
                return self.send(200, res)
            if u.path == "/api/managed":
                fe = folder_entry(b["base"])
                if fe:
                    fe["managed"] = True
                    fe["watch"] = b.get("watch", True)
                    fe["managed_at"] = time.time()
                    save_settings()
                    if b.get("count"):
                        push_event("managed", f"Sorted {b['count']} files in {os.path.basename(b['base'])}", base=b["base"], batch=b.get("batch"))
                return self.send(200, {"ok": True})
            if u.path == "/api/watch":
                fe = folder_entry(b["base"])
                if fe:
                    fe["watch"] = bool(b["on"]); save_settings()
                return self.send(200, {"ok": True})
            if u.path == "/api/undo":
                return self.send(200, undo(b["batch"]))
            if u.path == "/api/delete":
                return self.send(200, delete_paths(b["paths"]))
            if u.path == "/api/open":
                path = b.get("path") or file_row(int(b["id"]))["path"]
                return self.send(200, open_path(path, b.get("reveal", False)))
            if u.path == "/api/rename":
                return self.send(200, rename(int(b["id"]), b["name"]))
            if u.path == "/api/meta":
                return self.send(200, set_meta(int(b["id"]), b.get("tags", []), b.get("note", "")))
            if u.path == "/api/rescan":
                threading.Thread(target=scan, daemon=True).start()
                return self.send(200, {"ok": True})
            if u.path == "/api/settings":
                if "min_group" in b:
                    SETTINGS["min_group"] = max(2, int(b["min_group"]))
                if "start_with_windows" in b:
                    r = startup_shortcut(bool(b["start_with_windows"]))
                    if r.get("error"):
                        return self.send(200, r)
                save_settings()
                return self.send(200, {"ok": True})
            if u.path == "/api/update/check":
                return self.send(200, {**check_update(), "kind": install_kind()})
            if u.path == "/api/update/install":
                return self.send(200, install_update())
            if u.path == "/api/openurl":
                url = b.get("url", "")
                if re.match(r"^https://(gacharya\.com|github\.com|buymemomo\.com)(/|$)", url):
                    webbrowser.open(url)
                    return self.send(200, {"ok": True})
                return self.send(400, {"error": "not allowed"})
            if u.path == "/api/quit":
                self.send(200, {"ok": True})
                threading.Timer(0.3, lambda: os._exit(0)).start()
                return
            return self.send(404, {"error": "not found"})
        except Exception as e:
            log(f"POST {u.path}: {e!r}")
            return self.send(500, {"error": str(e)})


INSTANCE = os.path.join(DATA_DIR, "instance.json")


def me():
    return os.path.normcase(os.path.abspath(sys.executable if FROZEN else __file__))


def take_over_or_join():
    """Another SortWise already owns the port. If it's this same app, use it. If it's a different copy
    (another version or build), ask it to quit so this one takes over."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/hello", timeout=3) as r:
            other = json.loads(r.read())
    except Exception:
        other = {}
    if other.get("exe") == me() and other.get("version") == __version__ and other.get("build") == BUILD:
        return False
    try:
        with open(INSTANCE, encoding="utf-8") as f:
            tok = json.load(f).get("token", "")
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/quit", data=b"{}", method="POST",
                                     headers={"X-Token": tok, "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3).read()
        log(f"asked the other SortWise ({other.get('version', '?')}) to close")
        return True
    except Exception as e:
        log(f"couldn't ask the other SortWise to close: {e!r}")
    stop_port_owner()
    return True


def stop_port_owner():
    """Fallback when the running copy doesn't answer: stop the SortWise process listening on our port."""
    try:
        if IS_WIN:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, creationflags=0x08000000).stdout
            pids = {ln.split()[-1] for ln in out.splitlines() if f"127.0.0.1:{PORT} " in ln and "LISTENING" in ln}
            for pid in pids:
                if not pid.isdigit() or int(pid) == os.getpid():
                    continue
                img = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                     creationflags=0x08000000).stdout.lower()
                if any(x in img for x in ("python", "tidy", "sortwise")):
                    subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, creationflags=0x08000000)
                    log(f"stopped another SortWise (pid {pid})")
        else:
            out = subprocess.run(["lsof", "-nP", f"-iTCP:{PORT}", "-sTCP:LISTEN", "-Fpc"], capture_output=True, text=True).stdout
            pid, cmd = None, ""
            for ln in out.splitlines():
                if ln.startswith("p"):
                    pid = ln[1:]
                elif ln.startswith("c"):
                    cmd = ln[1:].lower()
                    if pid and pid.isdigit() and int(pid) != os.getpid() and any(x in cmd for x in ("python", "tidy", "sortwise")):
                        os.kill(int(pid), 15)
                        log(f"stopped another SortWise (pid {pid})")
    except Exception as e:
        log(f"couldn't stop the other SortWise: {e!r}")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, BrokenPipeError)):
            return  # a closed window, not a problem
        super().handle_error(request, client_address)


def main():
    background = "--background" in sys.argv
    init_db()
    srv = None
    for attempt in range(12):
        try:
            srv = Server(("127.0.0.1", PORT), Handler)
            break
        except OSError:
            if attempt == 0 and take_over_or_join():
                continue
            if attempt == 0:  # the same SortWise is already running: just bring up its window
                if not background:
                    open_ui()
                return
            time.sleep(0.5)
    if srv is None:
        if not background:
            open_ui()
        return
    try:
        with open(INSTANCE, "w", encoding="utf-8") as f:
            json.dump({"port": PORT, "token": TOKEN, "version": __version__, "exe": me(), "pid": os.getpid()}, f)
    except OSError:
        pass
    log(f"SortWise {__version__} (build {BUILD}) running at http://127.0.0.1:{PORT}/")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=scan, daemon=True).start()
    threading.Thread(target=watcher, daemon=True).start()
    threading.Thread(target=desktop_shortcut_once, daemon=True).start()
    threading.Thread(target=migrate_startup, daemon=True).start()
    threading.Thread(target=auto_check, daemon=True).start()
    if not background:
        threading.Timer(0.6, open_ui).start()
    if "--no-tray" in sys.argv or not run_tray():
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
