#!/usr/bin/env python3
"""
Dropdrive: copy files from a Dropbox link straight into a Google Drive folder.

Nothing is saved to your Mac's disk, so it works for files bigger than your free
space. Transfers survive network drops: if anything stops, run Dropdrive again and
it continues from where Google Drive left off. Each finished file is checked
against Dropbox's fingerprint for it.

Needs macOS and Python 3.8 or newer. Uses rclone (installed automatically) only to
sign in to Dropbox and Google; the copying itself is done here.

Run with no arguments for the guided version, or see:  python3 dropdrive.py --help
"""

import argparse
import hashlib
import http.client
import json
import mimetypes
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import zipfile
from urllib.parse import urlencode, urlsplit

VERSION = "1.0.0"

APP_DIR = os.environ.get("DROPDRIVE_HOME") or os.path.expanduser(
    "~/Library/Application Support/Dropdrive")
BIN_DIR = os.path.join(APP_DIR, "bin")
STATE_DIR = os.path.join(APP_DIR, "transfers")
RCLONE_CONF = os.path.join(APP_DIR, "rclone.conf")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
LOG_FILE = os.environ.get("DROPDRIVE_LOG") or os.path.expanduser("~/Library/Logs/Dropdrive.log")

DBX_REMOTE = "dropdrive-dropbox"
GD_REMOTE = "dropdrive-google"

CHUNK = 64 * 1024 * 1024        # bytes sent to Google per request (multiple of 256 KiB)
BLOCK = 4 * 1024 * 1024         # Dropbox content-hash block size (fixed by Dropbox)
READ = 8 * 1024 * 1024          # socket read size
TIMEOUT = 300                   # seconds without data before a connection counts as dead
SEEK_RATE = 10 * 1024 * 1024    # slowest rate Dropbox skips ahead (bytes/s) before sending a resumed file
GiB = 1024 ** 3
MAX_FAILS = 60                  # consecutive errors before giving up on a file

FOLDER_MIME = "application/vnd.google-apps.folder"

# Test hook: route every https:// request to a local plain-HTTP mock server.
TEST_SERVER = os.environ.get("DROPDRIVE_TEST_SERVER")
TEST_TOKEN = os.environ.get("DROPDRIVE_TEST_TOKEN")


# =============================================================================
# Output helpers
# =============================================================================

TTY = sys.stdout.isatty()


def _c(code, s):
    return "\033[%sm%s\033[0m" % (code, s) if TTY else s


def bold(s):
    return _c("1", s)


def dim(s):
    return _c("2", s)


def say(s=""):
    print(s, flush=True)


def info(s):
    say(_c("36", "› ") + s)


def ok(s):
    say(_c("32", "✓ ") + s)


def warn(s):
    say(_c("33", "! ") + s)


def bad(s):
    say(_c("31", "✗ ") + s)


def heading(s):
    say()
    say(bold(s))
    say(dim("─" * min(len(s), 60)))


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def ask(prompt, default=None):
    suffix = " [%s]" % default if default else ""
    try:
        ans = input(_c("1", "? ") + prompt + suffix + ": ").strip()
    except EOFError:
        raise SystemExit(1)
    return ans or (default or "")


def yes_no(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            ans = input(_c("1", "? ") + prompt + " [%s]: " % hint).strip().lower()
        except EOFError:
            raise SystemExit(1)
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        say("  Please type y or n.")


def size_str(n):
    n = float(n)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "bytes" else ("%.1f %s" % (n, unit))
        n /= 1000.0


def duration_str(sec):
    sec = int(sec)
    if sec < 60:
        return "under a minute"
    h, m = sec // 3600, (sec % 3600) // 60
    return ("%dh %02dm" % (h, m)) if h else ("%d min" % m)


def notify(title, message):
    def esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(["osascript", "-e", 'display notification "%s" with title "%s" sound name "Glass"'
                        % (esc(message), esc(title))],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        pass


class UserError(Exception):
    """A problem the person needs to act on. The message is shown as is."""


class Transient(Exception):
    """A temporary problem; retry after a pause."""


class SessionGone(Exception):
    """Google discarded the upload session; it has to start again."""


# =============================================================================
# Settings
# =============================================================================

def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(s):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2)
    os.chmod(SETTINGS_FILE, 0o600)


# =============================================================================
# HTTP
# =============================================================================

_ssl_ctx = None


def ssl_context():
    """Default trust store plus the macOS system roots (python.org builds ship without any)."""
    global _ssl_ctx
    if _ssl_ctx:
        return _ssl_ctx
    ctx = ssl.create_default_context()
    pem = os.path.join(APP_DIR, "macos-root-certs.pem")
    try:
        if not os.path.exists(pem) or time.time() - os.path.getmtime(pem) > 30 * 86400:
            out = subprocess.run(
                ["security", "find-certificate", "-a", "-p",
                 "/System/Library/Keychains/SystemRootCertificates.keychain"],
                capture_output=True, timeout=30).stdout
            if out:
                os.makedirs(APP_DIR, exist_ok=True)
                with open(pem, "wb") as f:
                    f.write(out)
        if os.path.exists(pem):
            ctx.load_verify_locations(pem)
    except Exception:
        pass
    _ssl_ctx = ctx
    return ctx


class Response:
    def __init__(self, status, headers, data):
        self.status, self.headers, self.data = status, headers, data

    def header(self, name):
        return self.headers.get(name)

    def json(self):
        try:
            return json.loads(self.data.decode("utf-8") or "null")
        except ValueError:
            return None

    @property
    def text(self):
        return self.data.decode("utf-8", "replace")[:500]


def _open(method, url, body=None, headers=None, timeout=None):
    u = urlsplit(url)
    scheme, netloc = u.scheme, u.netloc
    path = u.path + ("?" + u.query if u.query else "")
    if TEST_SERVER and scheme == "https":
        scheme, path, netloc = "http", "/" + netloc + path, TEST_SERVER
    if scheme == "http":
        conn = http.client.HTTPConnection(netloc, timeout=timeout or TIMEOUT)
    else:
        conn = http.client.HTTPSConnection(netloc, timeout=timeout or TIMEOUT, context=ssl_context())
    h = {"User-Agent": "Dropdrive/" + VERSION}
    h.update(headers or {})
    conn.request(method, path, body=body, headers=h)
    return conn, conn.getresponse()


def request(method, url, body=None, headers=None):
    conn, resp = _open(method, url, body, headers)
    try:
        data = resp.read()
    finally:
        conn.close()
    return Response(resp.status, resp.headers, data)


NET_ERRORS = (OSError, http.client.HTTPException)


def retrying(fn, what, attempts=8):
    """Run fn(); retry on network errors and Transient, with growing pauses."""
    for i in range(attempts):
        try:
            return fn()
        except (Transient,) + NET_ERRORS as e:
            if i == attempts - 1:
                raise UserError("Couldn't %s: %s\nCheck your internet connection and try again." % (what, e))
            log("retry %s: %s" % (what, e))
            time.sleep(min(60, 3 * (i + 1)))


# =============================================================================
# rclone (used for sign-in only)
# =============================================================================

RCLONE = None


def find_rclone():
    for p in (os.path.join(BIN_DIR, "rclone"), shutil.which("rclone"),
              "/opt/homebrew/bin/rclone", "/usr/local/bin/rclone"):
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def install_rclone():
    """Download the official rclone build for this Mac and check its SHA-256 before using it."""
    arch = {"arm64": "arm64", "x86_64": "amd64"}.get(platform.machine())
    if not arch:
        raise UserError("Unsupported processor type: %s" % platform.machine())

    def fetch(url, dest=None):
        cmd = ["curl", "-fsSL", "--retry", "3", url]
        if dest:
            cmd[1:1] = ["-o", dest]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0:
            raise UserError("Download failed (%s). Check your internet connection." % url)
        return r.stdout

    info("Downloading rclone (the sign-in helper)...")
    ver = fetch("https://downloads.rclone.org/version.txt").decode().split()[-1]   # e.g. v1.68.2
    name = "rclone-%s-osx-%s" % (ver, arch)
    base = "https://downloads.rclone.org/%s/" % ver
    tmp = tempfile.mkdtemp(prefix="dropdrive-")
    try:
        zpath = os.path.join(tmp, name + ".zip")
        fetch(base + name + ".zip", zpath)
        sums = fetch(base + "SHA256SUMS").decode()
        want = None
        for line in sums.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == name + ".zip":
                want = parts[0].lower()
        h = hashlib.sha256()
        with open(zpath, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        if not want or h.hexdigest() != want:
            raise UserError("The rclone download didn't match its published checksum, so it was not installed.")
        os.makedirs(BIN_DIR, exist_ok=True)
        dest = os.path.join(BIN_DIR, "rclone")
        with zipfile.ZipFile(zpath) as z, open(dest + ".tmp", "wb") as out:
            out.write(z.read(name + "/rclone"))
        os.chmod(dest + ".tmp", 0o755)
        os.replace(dest + ".tmp", dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok("rclone %s installed (checksum verified)" % ver)
    return dest


def ensure_rclone(auto=False):
    global RCLONE
    RCLONE = find_rclone()
    if RCLONE:
        return
    say("Dropdrive uses a free tool called rclone to sign in to Dropbox and Google.")
    if auto or yes_no("Download and set it up now?", True):
        RCLONE = install_rclone()
    else:
        raise UserError("rclone is needed. Install it with 'brew install rclone' or run Dropdrive again.")


def rclone(*args, capture=True, check=True):
    os.makedirs(APP_DIR, exist_ok=True)
    cmd = [RCLONE, "--config", RCLONE_CONF] + list(args)
    if capture:
        r = subprocess.run(cmd, capture_output=True, text=True)
    else:
        r = subprocess.run(cmd)
    if check and r.returncode != 0:
        detail = (r.stderr or "").strip().splitlines()[-1:] if capture else []
        raise UserError("rclone %s failed. %s" % (args[0], " ".join(detail)))
    if os.path.exists(RCLONE_CONF):
        os.chmod(RCLONE_CONF, 0o600)
    return r


def remotes():
    if not os.path.exists(RCLONE_CONF):
        return set()
    out = rclone("listremotes").stdout
    return {line.strip().rstrip(":") for line in out.splitlines() if line.strip()}


_tokens = {}


def _expiry_ts(s):
    if not s or s.startswith("0001"):
        return None
    s = re.sub(r"\.\d+", "", s).replace("Z", "+00:00")
    try:
        from datetime import datetime
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


# rclone only renews a token in its last ~10 s, so keep using it until then; waiting
# longer than that stalls the open Dropbox download long enough for Dropbox to drop it.
RENEW_AT = 15


def token(remote):
    """A valid access token for a signed-in account, renewed through rclone when close to expiry."""
    if TEST_TOKEN:
        return TEST_TOKEN
    cached = _tokens.get(remote)
    if cached and cached[1] - time.time() > RENEW_AT:
        return cached[0]
    for _ in range(4):
        # Any API call through rclone renews an expired token and writes it back to the config.
        rclone("about", remote + ":", check=False)
        cfg = json.loads(rclone("config", "dump").stdout or "{}")
        if remote not in cfg:
            raise UserError("Not signed in. Run Dropdrive and choose 'Accounts' to sign in again.")
        tok = json.loads(cfg[remote].get("token") or "{}")
        exp = _expiry_ts(tok.get("expiry"))
        if exp is None:
            _tokens[remote] = (tok["access_token"], time.time() + 10 ** 9)
            return tok["access_token"]
        left = exp - time.time()
        if left > RENEW_AT:
            _tokens[remote] = (tok["access_token"], exp)
            return tok["access_token"]
        if left > 0:
            log("waiting %ds for %s token to expire so rclone renews it" % (left, remote))
            time.sleep(left + 5)
    raise UserError("Couldn't renew the sign-in for %s. Choose 'Accounts' and sign in again." % remote)


def forget_token(remote):
    _tokens.pop(remote, None)


# =============================================================================
# Dropbox
# =============================================================================

def dbx_api(endpoint, payload):
    def call():
        r = request("POST", "https://api.dropboxapi.com/2/" + endpoint,
                    body=json.dumps(payload).encode(),
                    headers={"Authorization": "Bearer " + token(DBX_REMOTE),
                             "Content-Type": "application/json"})
        if r.status == 200:
            return r.json()
        if r.status == 401:
            forget_token(DBX_REMOTE)
            raise Transient("Dropbox sign-in needs renewing")
        if r.status == 429 or r.status >= 500:
            raise Transient("Dropbox is busy (%s)" % r.status)
        raise UserError(dropbox_error(r))
    return retrying(call, "reach Dropbox")


def dropbox_error(r):
    j = r.json() or {}
    summary = j.get("error_summary", "") if isinstance(j, dict) else ""
    if "shared_link_not_found" in summary or "shared_link_malformed" in summary:
        return "Dropbox doesn't recognise that link. Copy it again from Dropbox and paste the whole thing."
    if "shared_link_access_denied" in summary:
        return ("Your Dropbox account isn't allowed to open that link. Ask the sender to share it with "
                "your Dropbox email address, or to make it viewable by anyone with the link.")
    if "email_not_verified" in summary:
        return "Dropbox needs you to verify your email address before using shared links."
    if "not_found" in summary:
        return "That file isn't in the Dropbox folder any more."
    return "Dropbox said: %s (%s)" % (summary or r.text, r.status)


def dbx_account():
    j = dbx_api("users/get_current_account", None)
    return j.get("email", "?")


class Item:
    """One file to copy."""

    def __init__(self, name, size, dbx_path, rel_dirs, content_hash):
        self.name, self.size, self.dbx_path = name, int(size), dbx_path
        self.rel_dirs, self.content_hash = list(rel_dirs), content_hash

    @property
    def display(self):
        return "/".join(self.rel_dirs + [self.name])


def resolve_source(link):
    """Returns (title, [Item]) for a Dropbox file or folder link."""
    meta = dbx_api("sharing/get_shared_link_metadata", {"url": link})
    if meta.get(".tag") == "file":
        return meta["name"], [Item(meta["name"], meta.get("size", 0), None, [], None)]
    items = []
    stack = [("", [])]
    while stack:
        path, rel = stack.pop(0)
        j = dbx_api("files/list_folder", {"path": path, "shared_link": {"url": link}})
        while True:
            for e in j.get("entries", []):
                child = path + "/" + e["name"]
                if e[".tag"] == "file":
                    items.append(Item(e["name"], e.get("size", 0), child, rel, e.get("content_hash")))
                elif e[".tag"] == "folder":
                    stack.append((child, rel + [e["name"]]))
            if not j.get("has_more"):
                break
            j = dbx_api("files/list_folder/continue", {"cursor": j["cursor"]})
    items.sort(key=lambda i: [s.lower() for s in i.rel_dirs] + [i.name.lower()])
    return meta.get("name", "Dropbox folder"), items


def dropbox_seek_wait(offset):
    """Longest wait for the first byte when Dropbox resumes a file at `offset`."""
    return TIMEOUT + offset / SEEK_RATE


class DropboxStream:
    """Reads one file from a Dropbox link, starting at a byte offset.

    Dropbox reads through the file up to `offset` before it sends the first byte of a
    resumed download (measured ~1 GiB per 15-70 s), so the first read gets a longer
    timeout that grows with the offset; later reads use the normal TIMEOUT.
    """

    def __init__(self, link, dbx_path, offset):
        arg = {"url": link}
        if dbx_path:
            arg["path"] = dbx_path
        h = {"Authorization": "Bearer " + token(DBX_REMOTE), "Dropbox-API-Arg": json.dumps(arg)}
        if offset:
            h["Range"] = "bytes=%d-" % offset
        self.pending = b""
        self.conn, self.resp = _open("POST", "https://content.dropboxapi.com/2/sharing/get_shared_link_file",
                                     headers=h, timeout=dropbox_seek_wait(offset))
        st = self.resp.status
        if st not in (200, 206):
            r = Response(st, self.resp.headers, self.resp.read(2000))
            self.close()
            if st == 401:
                forget_token(DBX_REMOTE)
                raise Transient("Dropbox sign-in needs renewing")
            if st == 429 or st >= 500:
                raise Transient("Dropbox is busy (%s)" % st)
            raise UserError(dropbox_error(r))
        if offset and st == 200:
            # Server ignored the range request: read and discard what Google already has.
            log("dropbox ignored Range; discarding %d bytes" % offset)
            left = offset
            while left:
                b = self.resp.read(min(left, READ))
                if not b:
                    raise Transient("Dropbox connection ended early")
                left -= len(b)
        elif offset:
            self.pending = self.resp.read(1)
            if not self.pending:
                self.close()
                raise Transient("Dropbox connection ended early")
        if self.conn.sock:
            self.conn.sock.settimeout(TIMEOUT)

    def read(self, n):
        buf = bytearray(self.pending)
        self.pending = b""
        while len(buf) < n:
            b = self.resp.read(min(n - len(buf), READ))
            if not b:
                break
            buf += b
        return bytes(buf)

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


class ContentHasher:
    """Dropbox content hash, computed while streaming. Finished 4 MB block digests go to a
    file so the check survives a restart."""

    def __init__(self, path):
        self.path, self.f = path, None

    def reset_to(self, nblocks):
        """Keep the first nblocks digests; returns how many were actually available."""
        if self.f:
            self.f.close()
        have = os.path.getsize(self.path) // 32 if os.path.exists(self.path) else 0
        keep = min(nblocks, have)
        with open(self.path, "ab") as f:
            f.truncate(keep * 32)
        self.f = open(self.path, "ab")
        self.cur, self.curlen = hashlib.sha256(), 0
        return keep

    def feed(self, data):
        mv, i = memoryview(data), 0
        while i < len(mv):
            take = min(BLOCK - self.curlen, len(mv) - i)
            self.cur.update(mv[i:i + take])
            self.curlen += take
            i += take
            if self.curlen == BLOCK:
                self.f.write(self.cur.digest())
                self.cur, self.curlen = hashlib.sha256(), 0
        self.f.flush()

    def finish(self):
        if self.curlen:
            self.f.write(self.cur.digest())
            self.curlen = 0
        self.f.close()
        self.f = None
        with open(self.path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def close(self):
        if self.f:
            self.f.close()
            self.f = None


# =============================================================================
# Google Drive
# =============================================================================

GAPI = "https://www.googleapis.com"


def gd_error(r):
    j = r.json() or {}
    err = j.get("error", {}) if isinstance(j, dict) else {}
    reasons = [e.get("reason", "") for e in err.get("errors", [])] if isinstance(err, dict) else []
    msg = err.get("message", r.text) if isinstance(err, dict) else r.text
    return reasons, msg


def gd_check(r, what):
    """Raise the right exception for a failed Google request."""
    reasons, msg = gd_error(r)
    rs = " ".join(reasons)
    log("google %s -> %s %s %s" % (what, r.status, rs, msg))
    if r.status == 401:
        forget_token(GD_REMOTE)
        raise Transient("Google sign-in needs renewing")
    if "storageQuotaExceeded" in rs or "quotaExceeded" in rs and "storage" in msg.lower():
        raise UserError(
            "Your Google storage is full, so Google refused the upload.\n"
            "  Files you upload count against YOUR storage, even inside a folder someone else\n"
            "  shared with you (unless it's a Shared Drive). Free up space or get more at\n"
            "  https://one.google.com, then run Dropdrive again. It will continue where it stopped.")
    if r.status == 403 and ("uploadLimit" in rs or "daily" in rs.lower() or "daily" in msg.lower()):
        raise UserError(
            "Google's daily upload limit was reached (about 750 GB per account per day).\n"
            "  Wait until tomorrow and run Dropdrive again. It will continue where it stopped.")
    if r.status in (429, 500, 502, 503, 504) or "ateLimitExceeded" in rs or "backendError" in rs:
        raise Transient("Google is busy (%s)" % r.status)
    if r.status == 403 and ("insufficientFilePermissions" in rs or "insufficientPermissions" in rs):
        raise UserError("Your Google account doesn't have permission to add files there. "
                        "Ask the folder owner for Editor access.")
    raise UserError("Google said: %s (%s)" % (msg, r.status))


def gd_api(method, path, params=None, body=None):
    params = dict(params or {})
    params.setdefault("supportsAllDrives", "true")
    url = GAPI + "/drive/v3/" + path + "?" + urlencode(params)

    def call():
        h = {"Authorization": "Bearer " + token(GD_REMOTE)}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json; charset=UTF-8"
        r = request(method, url, body=data, headers=h)
        if r.status in (200, 201):
            return r.json()
        if r.status == 404:
            return None
        gd_check(r, path)
    return retrying(call, "reach Google Drive")


def gd_account():
    j = gd_api("GET", "about", {"fields": "user(emailAddress),storageQuota(limit,usage)"}) or {}
    q = j.get("storageQuota", {})
    free = None
    if q.get("limit"):
        free = int(q["limit"]) - int(q.get("usage", 0))
    return j.get("user", {}).get("emailAddress", "?"), free


def parse_drive_folder(text):
    text = (text or "").strip()
    if not text or text.lower() in ("root", "my drive", "mydrive"):
        return "root"
    m = re.search(r"/folders/([A-Za-z0-9_-]{10,})", text) or re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", text):
        return text
    raise UserError("That doesn't look like a Google Drive folder link. Open the folder in Google Drive "
                    "and copy the address from the browser's address bar.")


def resolve_dest(text):
    fid = parse_drive_folder(text)
    meta = gd_api("GET", "files/" + fid,
                  {"fields": "id,name,mimeType,driveId,capabilities(canAddChildren),owners(emailAddress)"})
    email, _ = gd_account()
    if meta is None:
        raise UserError(
            "Google can't find that folder from the account %s.\n"
            "  Either it isn't shared with %s, or the link is wrong.\n"
            "  Ask the owner to share it with that email (as Editor), or choose 'Accounts'\n"
            "  to sign in to Google with the account it was shared with." % (email, email))
    if meta.get("mimeType") != FOLDER_MIME:
        raise UserError("That Google link is a file, not a folder.")
    if not meta.get("capabilities", {}).get("canAddChildren", False):
        raise UserError("Your Google account (%s) can only view that folder. Ask the owner for Editor access."
                        % email)
    if fid == "root":
        meta["name"] = "My Drive"
    return meta


def gd_q(s):
    return s.replace("\\", "\\\\").replace("'", "\\'")


def gd_find(parent, name, folder=False):
    q = "'%s' in parents and name = '%s' and trashed = false" % (parent, gd_q(name))
    q += " and mimeType %s '%s'" % ("=" if folder else "!=", FOLDER_MIME)
    j = gd_api("GET", "files", {"q": q, "fields": "files(id,name,size)", "pageSize": "100",
                               "includeItemsFromAllDrives": "true"}) or {}
    return j.get("files", [])


_folder_cache = {}


def gd_ensure_path(parent, dirs):
    for d in dirs:
        key = (parent, d)
        if key not in _folder_cache:
            found = gd_find(parent, d, folder=True)
            if found:
                _folder_cache[key] = found[0]["id"]
            else:
                j = gd_api("POST", "files", {"fields": "id"},
                           {"name": d, "mimeType": FOLDER_MIME, "parents": [parent]})
                _folder_cache[key] = j["id"]
        parent = _folder_cache[key]
    return parent


def gd_new_session(job):
    url = (GAPI + "/upload/drive/v3/files?" +
           urlencode({"uploadType": "resumable", "supportsAllDrives": "true",
                      "fields": "id,name,size,webViewLink"}))
    mime = mimetypes.guess_type(job.name)[0] or "application/octet-stream"

    def call():
        r = request("POST", url, body=json.dumps({"name": job.name, "parents": [job.parent_id]}).encode(),
                    headers={"Authorization": "Bearer " + token(GD_REMOTE),
                             "Content-Type": "application/json; charset=UTF-8",
                             "X-Upload-Content-Type": mime,
                             "X-Upload-Content-Length": str(job.size)})
        if r.status == 200 and r.header("Location"):
            return r.header("Location")
        gd_check(r, "start upload")
    return retrying(call, "start the Google Drive upload")


def gd_put(uri, content_range, data=b""):
    return request("PUT", uri, body=data,
                   headers={"Authorization": "Bearer " + token(GD_REMOTE),
                            "Content-Length": str(len(data)),
                            "Content-Range": content_range})


def gd_upload_result(r, size):
    """Interpret a response to an upload PUT: ('done', json) | ('partial', committed_bytes)."""
    if r.status in (200, 201):
        return "done", (r.json() or {})
    if r.status == 308:
        rng = r.header("Range")
        return "partial", (int(rng.split("-")[1]) + 1 if rng else 0)
    if r.status in (404, 410):
        raise SessionGone()
    gd_check(r, "upload")


def gd_status(uri, size):
    def call():
        return gd_upload_result(gd_put(uri, "bytes */%d" % size), size)
    return retrying(call, "check the Google Drive upload")


# =============================================================================
# Transfer jobs (saved to disk so they can resume)
# =============================================================================

class Job:
    FIELDS = ("id", "link", "dbx_path", "name", "display", "size", "content_hash",
              "parent_id", "dest_name", "session", "created")

    def __init__(self, **kw):
        for k in self.FIELDS:
            setattr(self, k, kw.get(k))

    @staticmethod
    def make_id(link, dbx_path, parent_id, name):
        key = "\n".join([link, dbx_path or "", parent_id, name])
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    @property
    def path(self):
        return os.path.join(STATE_DIR, self.id + ".json")

    @property
    def blocks_path(self):
        return os.path.join(STATE_DIR, self.id + ".blocks")

    def save(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({k: getattr(self, k) for k in self.FIELDS}, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def delete(self):
        for p in (self.path, self.blocks_path):
            try:
                os.remove(p)
            except OSError:
                pass

    @classmethod
    def load(cls, jid):
        try:
            with open(os.path.join(STATE_DIR, jid + ".json")) as f:
                return cls(**json.load(f))
        except (OSError, ValueError):
            return None

    @classmethod
    def all(cls):
        if not os.path.isdir(STATE_DIR):
            return []
        jobs = [cls.load(f[:-5]) for f in sorted(os.listdir(STATE_DIR)) if f.endswith(".json")]
        jobs = [j for j in jobs if j]
        jobs.sort(key=lambda j: j.created or 0)
        return jobs


class Progress:
    def __init__(self, total_files, total_bytes):
        self.total_files, self.total_bytes = total_files, total_bytes
        self.done_bytes = 0
        self.samples = []
        self.last_draw = 0
        self.line_open = False

    def start_file(self, idx, job, already):
        self.close_line()
        say()
        say(bold("[%d/%d] %s" % (idx, self.total_files, job.display)) + dim("  %s" % size_str(job.size)))
        if already:
            info("Resuming: %s already in Google Drive" % size_str(already))
        self.size, self.samples = job.size, [(time.time(), already)]
        self.last_draw = 0
        self.update(already, force=True)

    def update(self, off, force=False):
        now = time.time()
        self.samples.append((now, off))
        while len(self.samples) > 2 and now - self.samples[0][0] > 60:
            self.samples.pop(0)
        if not force and now - self.last_draw < 0.5:
            return
        self.last_draw = now
        t0, o0 = self.samples[0]
        rate = (off - o0) / (now - t0) if now - t0 > 1 else 0
        frac = off / self.size if self.size else 1.0
        width = 24
        filled = int(width * frac)
        bar = "█" * filled + "░" * (width - filled)
        eta = duration_str((self.size - off) / rate) + " left" if rate > 0 else "measuring speed..."
        line = "  %s %5.1f%%  %s of %s  %s  %s" % (
            bar, frac * 100, size_str(off), size_str(self.size),
            ("%.1f MB/s" % (rate / 1e6)) if rate else "", eta)
        if TTY:
            sys.stdout.write("\r" + line.ljust(96)[:96])
            sys.stdout.flush()
            self.line_open = True
        elif force or frac >= 1:
            say(line)

    def note(self, msg, kind=warn):
        self.close_line()
        kind(msg)

    def close_line(self):
        if self.line_open:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.line_open = False


def run_job(job, idx, progress):
    """Copy one file. Returns 'ok', 'unverified' or raises UserError."""
    log("start %s (%d bytes) -> %s" % (job.display, job.size, job.parent_id))
    hasher = ContentHasher(job.blocks_path) if job.content_hash else None
    off, final = None, None
    if job.session:
        try:
            state, val = gd_status(job.session, job.size)
            if state == "done":
                # Finished during an earlier run that stopped before it could record that.
                final = val
                if hasher:
                    hasher = None
            else:
                off = val
        except SessionGone:
            job.session = None
    if final is None and off is None:
        job.session = gd_new_session(job)
        job.save()
        off = 0
        if hasher:
            hasher.reset_to(0)

    progress.start_file(idx, job, off or 0)
    src = None
    fails = 0
    verify = hasher is not None

    def reposition(target):
        """Open Dropbox at the right place and bring the content hash up to `target`."""
        nonlocal verify
        start = target
        if verify:
            nb = hasher.reset_to(target // BLOCK)
            start = nb * BLOCK
            if target - start > 4 * CHUNK:
                progress.note("Can't fingerprint-check this file after the restart; it will still be "
                              "size-checked.")
                verify = False
                hasher.close()
                start = target
        if start > GiB:
            progress.note("Waiting for Dropbox to skip ahead to %s before it sends anything "
                          "(usually a few minutes; giving it up to %s)..."
                          % (size_str(start), duration_str(dropbox_seek_wait(start))), kind=info)
        t = time.time()
        s = DropboxStream(job.link, job.dbx_path, start)
        if time.time() - t > 30:
            log("dropbox took %ds to start sending at %d" % (time.time() - t, start))
        left = target - start
        while left:
            b = s.read(min(left, READ))
            if not b:
                s.close()
                raise Transient("Dropbox connection ended early")
            hasher.feed(b)
            left -= len(b)
        return s

    try:
        while final is None:
            try:
                if src is None:
                    src = reposition(off)
                want = min(CHUNK, job.size - off)
                data = src.read(want) if want else b""
                if len(data) != want:
                    raise Transient("Dropbox connection ended early")
                if verify:
                    hasher.feed(data)
                rng = ("bytes %d-%d/%d" % (off, off + want - 1, job.size)) if want else "bytes */0"
                state, val = gd_upload_result(gd_put(job.session, rng, data), job.size)
                if state == "done":
                    final = val
                    off = job.size
                else:
                    if val != off + want:
                        src.close()
                        src = None
                    off = val
                fails = 0
                progress.update(off)
            except SessionGone:
                progress.note("Google dropped the upload session; starting this file again.")
                log("session gone for %s" % job.display)
                if src:
                    src.close()
                    src = None
                job.session = gd_new_session(job)
                job.save()
                off = 0
            except (Transient,) + NET_ERRORS as e:
                fails += 1
                if src:
                    src.close()
                    src = None
                log("error on %s at %d: %r" % (job.display, off, e))
                if fails > MAX_FAILS:
                    raise UserError("Too many connection problems in a row. Run Dropdrive again "
                                    "and it will continue from %s." % size_str(off))
                wait = min(300, 5 * fails)
                progress.note("Connection problem (%s). Retrying in %ds..." % (str(e)[:80] or type(e).__name__, wait))
                time.sleep(wait)
                try:
                    state, val = gd_status(job.session, job.size)
                    if state == "done":
                        final = val
                    else:
                        off = val
                except SessionGone:
                    job.session = gd_new_session(job)
                    job.save()
                    off = 0
                except UserError:
                    pass  # still offline; loop and retry
                progress.update(off, force=True)
    finally:
        if src:
            src.close()

    progress.close_line()
    got = int(final.get("size", job.size) or 0)
    if got != job.size:
        raise UserError("Google reports %s for %s but Dropbox has %s. The copy is incomplete; "
                        "delete it from Google Drive and run Dropdrive again."
                        % (size_str(got), job.name, size_str(job.size)))
    result = "unverified"
    if verify:
        digest = hasher.finish()
        if digest != job.content_hash:
            log("hash mismatch %s: %s != %s" % (job.display, digest, job.content_hash))
            job.delete()
            raise UserError("%s arrived, but its fingerprint doesn't match Dropbox's, so it may be "
                            "damaged. Delete it from Google Drive and run Dropdrive again." % job.name)
        result = "ok"
    elif hasher:
        hasher.close()
    job.delete()
    log("done %s (%s)" % (job.display, result))
    return result


def run_jobs(jobs):
    """Run a list of jobs one after another. Returns True if everything succeeded."""
    if not jobs:
        return True
    caf = None
    try:
        caf = subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    say()
    info("Keeping your Mac awake while this runs. Leave it plugged in and this window open.")
    info("You can lock the screen. Don't close the laptop lid.")
    progress = Progress(len(jobs), sum(j.size for j in jobs))
    results = []
    t0 = time.time()
    try:
        for i, job in enumerate(jobs, 1):
            try:
                r = run_job(job, i, progress)
                ok("Copied%s" % (" and fingerprint-checked" if r == "ok" else " (size checked)"))
                results.append((job, r))
            except UserError as e:
                progress.close_line()
                bad(str(e))
                log("failed %s: %s" % (job.display, e))
                results.append((job, "failed"))
                if "storage is full" in str(e) or "daily upload limit" in str(e):
                    break
    except KeyboardInterrupt:
        progress.close_line()
        say()
        warn("Stopped. Run Dropdrive again and choose Resume to continue where it left off.")
        notify("Dropdrive stopped", "Run it again to resume.")
        return False
    finally:
        if caf:
            caf.terminate()
    good = [j for j, r in results if r != "failed"]
    failed = [j for j, r in results if r == "failed"]
    remaining = len(jobs) - len(results)
    heading("Summary")
    ok("%d of %d file(s) copied in %s" % (len(good), len(jobs), duration_str(time.time() - t0)))
    for j in failed:
        bad("Not copied: %s" % j.display)
    if remaining:
        warn("%d file(s) not started yet" % remaining)
    if failed or remaining:
        info("Run Dropdrive again and choose Resume to retry.")
        notify("Dropdrive needs attention", "%d file(s) not copied" % (len(failed) + remaining))
        return False
    notify("Dropdrive finished", "%d file(s) copied to Google Drive" % len(good))
    return True


# =============================================================================
# Accounts
# =============================================================================

def connect_dropbox():
    heading("Connect Dropbox")
    say("A browser window will open. Sign in to Dropbox and click Allow.")
    say(dim("Dropdrive only reads the links you give it. Any Dropbox account works, including a free one;"))
    say(dim("nothing is saved into your Dropbox."))
    if not yes_no("Ready?", True):
        raise UserError("Dropbox sign-in skipped.")
    if DBX_REMOTE in remotes():
        rclone("config", "delete", DBX_REMOTE)
    rclone("config", "create", DBX_REMOTE, "dropbox", capture=False)
    forget_token(DBX_REMOTE)
    ok("Dropbox connected: %s" % dbx_account())


def connect_google():
    heading("Connect Google Drive")
    say("A browser window will open. Sign in with the Google account that should own the uploads")
    say("(the one the destination folder is shared with) and click Allow.")
    say(dim("Uploaded files count against this account's storage unless the folder is in a Shared Drive."))
    if not yes_no("Ready?", True):
        raise UserError("Google sign-in skipped.")
    if GD_REMOTE in remotes():
        rclone("config", "delete", GD_REMOTE)
    args = ["config", "create", GD_REMOTE, "drive", "scope=drive"]
    s = load_settings()
    if s.get("google_client_id") and s.get("google_client_secret"):
        args += ["client_id=" + s["google_client_id"], "client_secret=" + s["google_client_secret"]]
    rclone(*args, capture=False)
    forget_token(GD_REMOTE)
    email, free = gd_account()
    ok("Google Drive connected: %s%s" % (email, ("  (%s free)" % size_str(free)) if free is not None else ""))


def ensure_accounts():
    have = remotes()
    if DBX_REMOTE not in have:
        connect_dropbox()
    if GD_REMOTE not in have:
        connect_google()


def accounts_menu():
    while True:
        heading("Accounts")
        have = remotes()
        try:
            d = dbx_account() if DBX_REMOTE in have else "not connected"
        except UserError:
            d = "sign-in expired"
        try:
            if GD_REMOTE in have:
                email, free = gd_account()
                g = email + ("  (%s free)" % size_str(free) if free is not None else "")
            else:
                g = "not connected"
        except UserError:
            g = "sign-in expired"
        say("  Dropbox:      %s" % d)
        say("  Google Drive: %s" % g)
        s = load_settings()
        say(dim("  Google client: %s" % ("your own" if s.get("google_client_id") else "rclone default")))
        say()
        say("  1. Sign in to Dropbox again / switch account")
        say("  2. Sign in to Google again / switch account")
        say("  3. Use your own Google client ID (advanced)")
        say("  4. Back")
        choice = ask("Choose", "4")
        try:
            if choice == "1":
                connect_dropbox()
            elif choice == "2":
                connect_google()
            elif choice == "3":
                set_google_client()
            else:
                return
        except UserError as e:
            bad(str(e))


def set_google_client():
    heading("Your own Google client ID")
    say("rclone's shared Google sign-in is being retired by Google. If Google sign-in stops working,")
    say("create your own client ID (free, about 10 minutes) by following:")
    say("  https://rclone.org/drive/#making-your-own-client-id")
    say("Leave both blank to go back to the default.")
    cid = ask("Client ID")
    secret = ask("Client secret") if cid else ""
    s = load_settings()
    s["google_client_id"], s["google_client_secret"] = cid, secret
    save_settings(s)
    ok("Saved. Signing in to Google again so it takes effect.")
    connect_google()


# =============================================================================
# Flows
# =============================================================================

def parse_selection(text, n):
    text = text.strip().lower()
    if text in ("", "all", "a", "*"):
        return list(range(n))
    picked = []
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            rng = range(a, b + 1)
        elif part.isdigit():
            rng = [int(part)]
        else:
            raise ValueError(part)
        for k in rng:
            if not 1 <= k <= n:
                raise ValueError(str(k))
            if k - 1 not in picked:
                picked.append(k - 1)
    return picked


def choose_items(title, items, preset=None):
    if len(items) == 1:
        return items
    if not items:
        raise UserError("That Dropbox folder is empty.")
    heading("Files in “%s”" % title)
    width = len(str(len(items)))
    for i, it in enumerate(items, 1):
        say("  %s. %s  %s" % (str(i).rjust(width), it.display, dim(size_str(it.size))))
    say(dim("  Total: %d files, %s" % (len(items), size_str(sum(i.size for i in items)))))
    if preset is not None:
        try:
            return [items[k] for k in parse_selection(preset, len(items))]
        except ValueError as e:
            raise UserError("Can't understand file selection '%s'." % e)
    say()
    say("Type the numbers of the files to copy, e.g.  3   or  1,4,7   or  2-5.  Press Enter for all.")
    while True:
        ans = ask("Files to copy", "all")
        try:
            sel = parse_selection(ans, len(items))
            if sel:
                return [items[k] for k in sel]
        except ValueError:
            pass
        say("  Use numbers from the list, like 3 or 1,4 or 2-5.")


def plan_jobs(link, items, dest):
    """Create (or pick up existing) jobs, skipping files already in Drive."""
    jobs, skipped = [], []
    for it in items:
        parent = gd_ensure_path(dest["id"], it.rel_dirs)
        jid = Job.make_id(link, it.dbx_path, parent, it.name)
        existing = Job.load(jid)
        if existing:
            jobs.append(existing)
            continue
        same = [f for f in gd_find(parent, it.name) if int(f.get("size", -1)) == it.size]
        if same:
            skipped.append(it)
            continue
        job = Job(id=jid, link=link, dbx_path=it.dbx_path, name=it.name, display=it.display,
                  size=it.size, content_hash=it.content_hash, parent_id=parent,
                  dest_name=dest["name"], session=None, created=time.time())
        job.save()
        jobs.append(job)
    return jobs, skipped


def check_space(dest, total):
    if dest.get("driveId"):
        return True  # Shared Drive: the organisation's storage is used
    email, free = gd_account()
    if free is None or free >= total:
        return True
    say()
    warn("Not enough Google storage on %s: these files need %s, you have %s free."
         % (email, size_str(total), size_str(free)))
    say("  Files you upload count against your own storage, even in a folder someone else shared")
    say("  with you. Get more space at https://one.google.com, or ask the folder owner to use a")
    say("  Shared Drive. Files that fit will still copy; the rest will stop with an error.")
    return yes_no("Continue anyway?", False)


def new_transfer(src=None, dst=None, files=None, assume_yes=False):
    heading("What to copy")
    while True:
        link = src or ask("Paste the Dropbox link")
        if "dropbox.com" not in link:
            say("  That isn't a Dropbox link. It should start with https://www.dropbox.com/")
            if src:
                raise UserError("Not a Dropbox link.")
            continue
        try:
            info("Looking at the link...")
            title, items = resolve_source(link.strip())
            break
        except UserError as e:
            bad(str(e))
            if src:
                raise
    items = choose_items(title, items, files)
    ok("%d file(s), %s" % (len(items), size_str(sum(i.size for i in items))))

    heading("Where to put it")
    while True:
        text = dst if dst is not None else ask("Paste the Google Drive folder link (Enter for My Drive)")
        try:
            dest = resolve_dest(text)
            break
        except UserError as e:
            bad(str(e))
            if dst is not None:
                raise
    ok("Google Drive folder: %s%s" % (dest["name"], "  (Shared Drive)" if dest.get("driveId") else ""))

    info("Checking what's already there...")
    jobs, skipped = plan_jobs(link.strip(), items, dest)
    for it in skipped:
        ok("Already in Google Drive, skipping: %s" % it.display)
    if not jobs:
        ok("Nothing to do. Everything is already there.")
        return True
    total = sum(j.size for j in jobs)
    if not check_space(dest, total):
        return False
    say()
    say("Ready to copy %d file(s), %s, into “%s”." % (len(jobs), size_str(total), dest["name"]))
    if not assume_yes and not yes_no("Start?", True):
        warn("Cancelled. Nothing was copied. (Choose Resume later to start these.)")
        return False
    return run_jobs(jobs)


def resume_flow(assume_yes=False):
    jobs = Job.all()
    if not jobs:
        ok("No unfinished transfers.")
        return True
    heading("Unfinished transfers")
    for j in jobs:
        say("  • %s  %s  → %s" % (j.display, dim(size_str(j.size)), j.dest_name))
    if not assume_yes:
        say()
        say("  1. Resume all")
        say("  2. Forget them (files already finished in Google Drive are kept)")
        say("  3. Back")
        c = ask("Choose", "1")
        if c == "2":
            if yes_no("Forget %d unfinished transfer(s)?" % len(jobs), False):
                for j in jobs:
                    j.delete()
                ok("Forgotten.")
            return True
        if c != "1":
            return True
    return run_jobs(jobs)


def main_menu():
    while True:
        pending = Job.all()
        heading("Dropdrive %s · Dropbox → Google Drive" % VERSION)
        say("  1. Copy from a Dropbox link")
        say("  2. Resume unfinished transfers" + (bold("  (%d waiting)" % len(pending)) if pending else ""))
        say("  3. Accounts")
        say("  4. Quit")
        c = ask("Choose", "2" if pending else "1")
        try:
            if c == "1":
                new_transfer()
            elif c == "2":
                resume_flow()
            elif c == "3":
                accounts_menu()
            elif c in ("4", "q", "quit", "exit"):
                return
        except UserError as e:
            bad(str(e))


def main():
    p = argparse.ArgumentParser(
        prog="dropdrive",
        description="Copy files from a Dropbox link into a Google Drive folder, without using local "
                    "disk space. Resumes after interruptions. Run with no options for the guided mode.")
    p.add_argument("--from", dest="src", metavar="DROPBOX_LINK", help="Dropbox file or folder link")
    p.add_argument("--to", dest="dst", metavar="DRIVE_FOLDER",
                   help="Google Drive folder link or ID ('root' for My Drive)")
    p.add_argument("--files", metavar="SELECTION",
                   help="for folder links: 'all' or numbers like 1,3,5-7 (as listed)")
    p.add_argument("--resume", action="store_true", help="resume all unfinished transfers")
    p.add_argument("--accounts", action="store_true", help="sign in or switch accounts")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p.add_argument("--version", action="version", version="Dropdrive " + VERSION)
    a = p.parse_args()

    if platform.system() != "Darwin":
        say("Dropdrive is built for macOS.")
        return 1
    if sys.version_info < (3, 8):
        say("Dropdrive needs Python 3.8 or newer.")
        return 1

    try:
        ensure_rclone(auto=a.yes)
        if a.accounts:
            accounts_menu()
            return 0
        ensure_accounts()
        if a.resume:
            return 0 if resume_flow(assume_yes=a.yes) else 2
        if a.src:
            return 0 if new_transfer(a.src, a.dst if a.dst is not None else "", a.files, a.yes) else 2
        main_menu()
        return 0
    except UserError as e:
        bad(str(e))
        return 2
    except KeyboardInterrupt:
        say()
        warn("Stopped. Run Dropdrive again to continue.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
