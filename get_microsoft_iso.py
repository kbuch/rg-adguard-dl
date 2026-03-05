#!/usr/bin/env python3
"""
Microsoft ISO Downloader
Uses Playwright for JS-rendered search/hash scraping, curl_cffi for downloads
(Chrome TLS impersonation to bypass CDN fingerprint checks).
Automatically handles 7z extraction and SHA-256 verification.

Usage:
    python get_microsoft_iso.py                          # interactive
    python get_microsoft_iso.py -s "Win_Pro_11_22H2"    # search
    python get_microsoft_iso.py -u "9f118776-..."        # direct UUID
    python get_microsoft_iso.py -s "..." -o ~/ISOs       # custom output dir
    python get_microsoft_iso.py -s "..." --debug         # verbose output

Requirements:
    pip install curl_cffi tqdm playwright py7zr
    playwright install chromium
"""

from __future__ import annotations

import platform
import re
import sys
import shutil
import hashlib
import argparse
import subprocess
from pathlib import Path
from urllib.parse import quote

IS_WINDOWS = platform.system() == "Windows"

# Known archive password (from rg-adguard tools.cab → bin/7z.cmd)
# Used as fallback if tools.cab fetch/extraction fails.
RG_ARCHIVE_PASSWORD = "ms_by_rgadguard"

# ── Dependency check ──────────────────────────────────────────────────────────

def check_deps():
    missing = []
    for pkg in ("curl_cffi", "tqdm"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        missing.append("playwright")
    if missing:
        print(f"Missing dependencies: {', '.join(missing)}")
        print(f"Install with:")
        print(f"  pip install {' '.join(missing)}")
        if "playwright" in missing:
            print(f"  playwright install chromium")
        sys.exit(1)

check_deps()

from curl_cffi import requests          # Chrome TLS impersonation
from tqdm import tqdm
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL = "https://files.rg-adguard.net"
UUID_RE  = re.compile(
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", re.I
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language":         "en-US,en;q=0.9",
    "Accept-Encoding":         "gzip, deflate, br",
    "Cache-Control":           "max-age=0",
    "Connection":              "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Referer":                 BASE_URL + "/",
    # Chrome client hints — must NOT say "HeadlessChrome" or CDNs block the request
    "sec-ch-ua":          '"Google Chrome";v="120", "Chromium";v="120", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile":   "?0",
    "sec-ch-ua-platform": '"Windows"',
    # Navigation-style fetch metadata
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "cross-site",
    "sec-fetch-user": "?1",
}

# Headers used in Playwright contexts — override Chromium's automatic HeadlessChrome value
PW_EXTRA_HEADERS = {
    "sec-ch-ua":          HEADERS["sec-ch-ua"],
    "sec-ch-ua-mobile":   HEADERS["sec-ch-ua-mobile"],
    "sec-ch-ua-platform": HEADERS["sec-ch-ua-platform"],
}

# ── UI helpers ────────────────────────────────────────────────────────────────

def banner():
    print("\n" + "=" * 70)
    print("  Microsoft ISO Downloader  —  rg-adguard.net")
    print("=" * 70)


def section(title: str):
    print(f"\n── {title} {'─' * (65 - len(title))}")


def pick(items: list[dict], label_key: str, prompt: str) -> int:
    """Print numbered list, return chosen 0-based index."""
    print("-" * 70)
    for i, item in enumerate(items, 1):
        label = item[label_key]
        size  = f"  ({item['size']})" if "size" in item else ""
        print(f"  [{i:>2}]  {label}{size}")
    print("-" * 70)
    while True:
        choice = input(f"\n{prompt} (Q to quit): ").strip()
        if choice.lower() == "q":
            print("Cancelled.")
            sys.exit(0)
        if choice.isdigit() and 1 <= int(choice) <= len(items):
            return int(choice) - 1
        print("  Invalid selection — try again.")

# ── Playwright: search ────────────────────────────────────────────────────────

def search_catalog(query: str) -> list[dict]:
    """
    Search rg-adguard using a real headless browser (Playwright).
    Waits for JS to render results before scraping /file/{uuid} links.
    """
    # Strip surrounding quotes the user may have typed
    query = query.strip().strip('"\'')

    url = f"{BASE_URL}/search?q={quote(query)}&lang=en-us"
    section("Search")
    print(f"Query : {query}")
    print(f"URL   : {url}")
    print("Launching headless browser...")

    results = []
    uuid_path_re = re.compile(
        r"^/file/(" + UUID_RE.pattern + r")$", re.I
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx     = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
            extra_http_headers=PW_EXTRA_HEADERS,
        )
        page = ctx.new_page()

        # Use "load" instead of "networkidle" — the site keeps background
        # connections open so networkidle never fires.
        page.goto(url, wait_until="load", timeout=30_000)

        # Give JS a moment to render results after the initial load
        page.wait_for_timeout(3_000)

        # Wait for at least one /file/{uuid} link to appear
        try:
            page.wait_for_selector('a[href*="/file/"]', timeout=10_000)
        except PWTimeout:
            print("No results rendered — search returned empty.")
            browser.close()
            return []

        seen = set()
        for a in page.query_selector_all('a[href*="/file/"]'):
            href = a.get_attribute("href") or ""
            m    = uuid_path_re.match(href)
            if not m:
                continue
            uuid = m.group(1).lower()
            name = (a.inner_text() or "").strip()
            if uuid not in seen and name:
                seen.add(uuid)
                results.append({"name": name, "uuid": uuid})

        browser.close()

    print(f"Found {len(results)} result(s).")
    return results

# ── Fetch archive password from rg-adguard tools ─────────────────────────────

def fetch_rg_tools() -> dict | None:
    """
    Download rg-adguard tools.cab and extract it.
    Returns {'password': str, 'smv_exe': Path | None, 'tools_dir': Path}
    Caller must call shutil.rmtree(tools_dir) when done.
    """
    import tempfile
    tools_url = "https://files.rg-adguard.net/tools"
    try:
        print("  Fetching tools.cab...", end=" ", flush=True)
        r = requests.get(tools_url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        print(f"{len(r.content) // 1024} KB received.")

        with tempfile.NamedTemporaryFile(suffix=".cab", delete=False) as f:
            cab_path = Path(f.name)
            f.write(r.content)

        out_dir = cab_path.parent / "rg_tools"
        out_dir.mkdir(exist_ok=True)

        # Extract .cab — platform-specific
        extracted_ok = False
        if IS_WINDOWS:
            expand_exe = Path(r"C:\Windows\System32\expand.exe")
            result = subprocess.run(
                [str(expand_exe), str(cab_path), "-f:*", str(out_dir)],
                capture_output=True, text=True,
            )
            extracted_ok = result.returncode == 0
        else:
            # macOS / Linux: try cabextract, then 7z
            for tool, cmd in [
                ("cabextract", ["cabextract", "-d", str(out_dir), str(cab_path)]),
                ("7z",         ["7z", "x", str(cab_path), f"-o{out_dir}", "-y"]),
            ]:
                if shutil.which(tool):
                    result = subprocess.run(cmd, capture_output=True, text=True)
                    extracted_ok = result.returncode == 0
                    break
            if not extracted_ok:
                print("  No cab extractor found. Install one of:")
                print("    brew install cabextract")
                print("    brew install p7zip")

        cab_path.unlink(missing_ok=True)

        # Extract password from 7z.cmd
        password = None
        for f in out_dir.rglob("*.cmd"):
            text = f.read_text(errors="ignore")
            m = re.search(r'(?i)set\s+passwd\s*=\s*"?([^"\r\n^&%]+)"?', text)
            if m:
                password = m.group(1).strip('"\'').strip()
                print(f"  Found password in {f.name}: {password}")
                break

        # Find smv.exe and print smv.cmd for debugging
        smv_exe = next(out_dir.rglob("smv.exe"), None)
        if smv_exe:
            print(f"  Found smv.exe: {smv_exe}")
        return {"password": password, "smv_exe": smv_exe, "tools_dir": out_dir}

    except Exception as e:
        print(f"  fetch_rg_tools failed: {e}")
    return None


def apply_svf(smv_exe: Path, svf_file: Path, dest_dir: Path) -> Path | None:
    """
    Run rg-adguard's smv.exe to reconstruct an ISO from a .svf file.
    Syntax from rg-adguard smv.cmd: smv.exe x <file.svf> -br <base_dir>
    The base_dir must contain any base ISO required for delta reconstruction.
    Returns path to the reconstructed ISO, or None on failure.
    """
    iso_name = svf_file.name.replace(".svf", "")
    iso_path = dest_dir / iso_name
    print(f"  Input : {svf_file.name}")
    print(f"  Output: {iso_name}")

    def build_cmd(verb_args: list[str]) -> list[str]:
        base = [str(smv_exe)] + verb_args
        if not IS_WINDOWS:
            wine = shutil.which("wine")
            if wine:
                return [wine] + base
            print("  Wine not found — cannot run smv.exe on this platform.")
            print("  Install with:  brew install --cask wine-stable")
            return []
        return base

    cmd = build_cmd(["x", str(svf_file), "-br", str(dest_dir)])
    if not cmd:
        return None
    try:
        result = subprocess.run(cmd, cwd=str(dest_dir), capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
        output = result.stdout + result.stderr
        if output.strip():
            # Print first meaningful line for status
            first = next((l for l in output.splitlines() if l.strip()), "")
            print(f"  {first.strip()[:120]}")

        if iso_path.exists() and iso_path.stat().st_size > 100 * 1024 * 1024:
            print(f"  Reconstructed: {iso_path} ({iso_path.stat().st_size / 1024**3:.2f} GB)")
            return iso_path
        for iso in sorted(dest_dir.glob("*.iso")):
            if iso.stat().st_size > 100 * 1024 * 1024:
                return iso
    except Exception as e:
        print(f"  smv.exe invocation failed: {e}")

    return None


# ── Playwright: hashes ────────────────────────────────────────────────────────

def scrape_hashes(uuid: str) -> dict[str, str]:
    """
    Scrape MD5 / SHA-1 / SHA-256 / SHA-512 and archive password from the
    file detail page. Returns a dict keyed by algorithm name plus optionally
    'password' if the archive requires one, e.g. {'sha256': '...', 'password': '...'}.
    """
    url = f"{BASE_URL}/file/{uuid}"
    section("Hash info")
    print(f"Fetching: {url}")

    algo_patterns: dict[str, re.Pattern] = {
        "md5":    re.compile(r"\b([a-f0-9]{32})\b",  re.I),
        "sha1":   re.compile(r"\b([a-f0-9]{40})\b",  re.I),
        "sha256": re.compile(r"\b([a-f0-9]{64})\b",  re.I),
        "sha512": re.compile(r"\b([a-f0-9]{128})\b", re.I),
    }
    # Labels as they appear on the page
    algo_labels = {
        "md5":    ["md5"],
        "sha1":   ["sha-1", "sha1"],
        "sha256": ["sha-256", "sha256"],
        "sha512": ["sha-512", "sha512"],
    }

    hashes: dict[str, str] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx     = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
            extra_http_headers=PW_EXTRA_HEADERS,
        )
        page    = ctx.new_page()

        page.goto(url, wait_until="load", timeout=30_000)
        page.wait_for_timeout(2_000)

        # Strategy 1: iterate table rows / definition lists for label+value pairs
        for row in page.query_selector_all("tr, dl dt, dl dd, .hash, [class*='hash']"):
            text = (row.inner_text() or "").strip().lower()
            for algo, labels in algo_labels.items():
                if algo in hashes:
                    continue
                if any(lbl in text for lbl in labels):
                    m = algo_patterns[algo].search(text)
                    if m:
                        hashes[algo] = m.group(1).lower()

        # Strategy 2: fallback — scan full page text for hash-length hex strings
        # anchored by the algorithm label appearing nearby (within 200 chars)
        if len(hashes) < len(algo_patterns):
            full_text = page.inner_text("body").lower()
            for algo, labels in algo_labels.items():
                if algo in hashes:
                    continue
                for label in labels:
                    idx = full_text.find(label)
                    if idx == -1:
                        continue
                    snippet = full_text[idx: idx + 200]
                    m = algo_patterns[algo].search(snippet)
                    if m:
                        hashes[algo] = m.group(1).lower()
                        break

        # Strategy 3: look for archive password label on the page
        full_text = page.inner_text("body") if "full_text" not in dir() else full_text
        try:
            full_text = page.inner_text("body")
        except Exception:
            full_text = ""
        pw_match = re.search(r'[Pp]assword[:\s]+([^\s\n<]{3,40})', full_text)
        if pw_match:
            candidate = pw_match.group(1).strip().rstrip(".")
            # Exclude hash-like strings (they'd be caught by algo patterns instead)
            if not re.fullmatch(r'[a-f0-9]{32,}', candidate, re.I):
                hashes["password"] = candidate

        browser.close()

    if hashes:
        for key, val in hashes.items():
            label = "PASSWORD" if key == "password" else key.upper()
            print(f"  {label:8}: {val}")
    else:
        print("  No hashes found on page.")

    return hashes

# ── Playwright: download links (aggressive browser interception approach) ──────

def get_download_links(uuid: str, debug: bool = False) -> dict | None:
    """
    Fetch file listing and parse the aria2c-format /dl/free/ endpoint.
    The endpoint returns one URL per file in aria2c input format:
        https://cdn/.../uuid.7z
          out=filename.7z
          checksum=sha-1=<hash>
    Returns dict with keys: files, links, aria2c_input, checksums, cookies.
    """
    section("Download links")
    file_url = f"{BASE_URL}/file/{uuid}"
    dl_url   = f"{BASE_URL}/dl/free/{uuid}"

    # ── File listing (plain requests, no JS needed) ────────────────────────────
    files = []
    try:
        r = requests.get(f"{BASE_URL}/file/{uuid}/list", headers=HEADERS, timeout=30)
        r.raise_for_status()
        for line in r.text.strip().splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2:
                files.append({
                    "name": parts[1],
                    "size": parts[2] if len(parts) > 2 else "unknown",
                })
    except Exception as e:
        print(f"File list request failed: {e}")

    # ── Fetch the aria2c input from /dl/free/ ─────────────────────────────────
    print(f"Fetching download links from: {dl_url}")
    aria2c_input = ""
    try:
        r2 = requests.get(dl_url, headers={**HEADERS, "Referer": file_url}, timeout=30)
        r2.raise_for_status()
        aria2c_input = r2.text
        if debug:
            print(f"  [DEBUG] /dl/free/ response ({r2.status_code}):")
            print(f"    {aria2c_input[:400]!r}")
    except Exception as e:
        print(f"  /dl/free/ request failed: {e}")

    # ── Parse aria2c format ────────────────────────────────────────────────────
    # Format:  URL\n  out=filename\n  checksum=sha-1=<hash>\n\n  (blank line between entries)
    links: list[str] = []
    checksums: dict[str, str] = {}   # url → sha1 hex

    for block in re.split(r"\n\s*\n", aria2c_input.strip()):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        url = lines[0] if lines[0].startswith("http") else None
        if not url:
            continue
        links.append(url)
        for attr_line in lines[1:]:
            m = re.match(r"checksum\s*=\s*sha-1\s*=\s*([a-f0-9]{40})", attr_line, re.I)
            if m:
                checksums[url] = m.group(1).lower()

    print(f"Found {len(links)} download link(s).")
    if debug:
        for lnk in links:
            chk = checksums.get(lnk, "no checksum")
            print(f"  {lnk}  [{chk}]")

    return {
        "files":        files,
        "links":        links,
        "aria2c_input": aria2c_input,
        "checksums":    checksums,   # url → sha-1 of the .7z archive
        "cdn_headers":  None,
        "cookies":      {},
    }

# ── aria2c download ───────────────────────────────────────────────────────────

def download_via_aria2c(url: str, sha1: str | None, dest_dir: Path) -> Path | None:
    """
    Use aria2c (if installed) to download a single URL.
    aria2c is the intended client for rg-adguard's /dl/free/ endpoint.
    Returns path to the downloaded file, or None if aria2c is unavailable/failed.
    """
    aria2c_bin = shutil.which("aria2c")
    if not aria2c_bin:
        return None

    dest_dir = dest_dir.resolve()  # ensure absolute path for aria2c --input-file / --dir
    section("Download (aria2c)")
    print(f"  aria2c : {aria2c_bin}")
    print(f"  URL    : {url}")

    # Build a minimal aria2c input with just this one URL
    block = url + "\n"
    filename = Path(url.split("/")[-1]).name
    block += f"  out={filename}\n"
    if sha1:
        block += f"  checksum=sha-1={sha1}\n"

    input_file = dest_dir / "_aria2c_input.txt"
    input_file.write_text(block)
    dest_file = dest_dir / filename

    try:
        result = subprocess.run(
            [
                aria2c_bin,
                f"--input-file={input_file}",
                f"--dir={dest_dir}",
                "--continue=true",
                "--max-connection-per-server=1",   # avoid 429 rate limiting
                "--split=1",
                "--max-concurrent-downloads=1",    # matches official rg-adguard script (-j1)
                "--disable-ipv6=true",             # matches official rg-adguard script
                "--file-allocation=none",          # skip disk preallocation
                "--console-log-level=notice",
            ],
            cwd=str(dest_dir),
        )
    finally:
        input_file.unlink(missing_ok=True)

    if result.returncode == 0 and dest_file.exists():
        return dest_file
    # aria2c may have partially downloaded — return partial if it exists
    if dest_file.exists() and dest_file.stat().st_size > 0:
        print(f"  aria2c exited with code {result.returncode} but partial file exists.")
        return dest_file
    print(f"  aria2c exited with code {result.returncode}")
    return None

# ── Download ──────────────────────────────────────────────────────────────────

def download_file(
    url: str,
    dest_dir: Path,
    resume: bool = True,
    cdn_headers: dict | None = None,
    cookies: dict | None = None,
) -> Path | None:
    """
    Stream download with tqdm progress bar and resume support.
    Uses curl_cffi with Chrome TLS impersonation to bypass CDN fingerprint checks.
    If cdn_headers provided, uses those exact headers (from intercepted CDN request).
    Otherwise uses HEADERS + cookies.
    """
    filename = Path(url.split("?")[0].split("/")[-1])
    dest     = dest_dir / filename
    existing = dest.stat().st_size if dest.exists() else 0

    section(f"Download  —  {filename.name}")

    session = requests.Session(impersonate="chrome120")

    if cdn_headers:
        # Use exact headers captured from the browser's CDN request
        session.headers.clear()
        session.headers.update(cdn_headers)
        print("  Using intercepted CDN headers for authentication.")
    else:
        session.headers.update(HEADERS)
        if cookies:
            session.cookies.update(cookies)

    if existing > 0 and resume:
        print(f"Resuming  ({existing / 1024**3:.2f} GB already on disk)")
        session.headers["Range"] = f"bytes={existing}-"

    print(f"Saving to : {dest}")

    try:
        r = session.get(url, stream=True, timeout=60)
        if r.status_code == 416:
            print("File already fully downloaded.")
            return dest
        r.raise_for_status()

        total = int(r.headers.get("content-length", 0)) + existing
        mode  = "ab" if existing > 0 and resume else "wb"

        with open(dest, mode) as f:
            with tqdm(
                total=total, initial=existing,
                unit="B", unit_scale=True, unit_divisor=1024,
                desc=filename.name[:40], ncols=80
            ) as bar:
                for chunk in r.iter_content(65_536):
                    if chunk:
                        f.write(chunk)
                        bar.update(len(chunk))

        final_size = dest.stat().st_size
        print(f"\nSaved  ({final_size / 1024**3:.2f} GB)")
        return dest

    except Exception as e:
        print(f"Download failed: {e}")
        return None


# ── 7z extraction ─────────────────────────────────────────────────────────────

def extract_7z(archive: Path, dest_dir: Path, password: str | None = None) -> Path | None:
    """
    Extract a 7z archive (including multi-part) to dest_dir.
    Returns path to the extracted ISO, or None on failure.
    Tries py7zr first, falls back to system 7z binary.
    """
    section("Extract")
    print(f"Archive : {archive.name}")
    if password:
        print(f"Password: {password}")

    # py7zr handles single and multi-volume archives
    try:
        import py7zr
        kwargs = {"mode": "r"}
        if password:
            kwargs["password"] = password
        with py7zr.SevenZipFile(archive, **kwargs) as z:
            names = z.getnames()
            print(f"Contents: {', '.join(names)}")
            z.extractall(path=dest_dir)
        print("Extraction complete (py7zr).")
        for name in names:
            candidate = dest_dir / name
            if candidate.suffix.lower() == ".iso" and candidate.exists():
                return candidate
        # Return any file if no ISO found by suffix
        for name in names:
            candidate = dest_dir / name
            if candidate.exists():
                return candidate
    except ImportError:
        pass
    except Exception as e:
        print(f"py7zr failed: {e} — trying system 7z...")

    # System 7z fallback
    for cmd in ("7z", "7za", "7zz"):
        try:
            args = [cmd, "x", str(archive), f"-o{dest_dir}", "-y"]
            if password:
                args.append(f"-p{password}")
            result = subprocess.run(args, capture_output=True, text=True)
            if result.returncode == 0:
                print(f"Extraction complete ({cmd}).")
                for iso in sorted(dest_dir.glob("*.iso")):
                    return iso
                return None
        except FileNotFoundError:
            continue

    print("\nExtraction failed. Install one of:")
    print("  pip install py7zr")
    print("  brew install p7zip   (macOS)")
    return None

# ── Hash verification ─────────────────────────────────────────────────────────

def verify_hash(
    filepath: Path,
    expected: str | None = None,
    algorithm: str = "sha256",
) -> bool:
    """
    Compute hash of filepath and optionally compare against expected.
    Shows a progress bar since ISOs are large.
    """
    section(f"Hash verification  —  {algorithm.upper()}")
    h    = hashlib.new(algorithm)
    size = filepath.stat().st_size

    with open(filepath, "rb") as f:
        with tqdm(total=size, unit="B", unit_scale=True,
                  unit_divisor=1024, desc="Hashing", ncols=80) as bar:
            for chunk in iter(lambda: f.read(65_536), b""):
                h.update(chunk)
                bar.update(len(chunk))

    actual = h.hexdigest()
    print(f"\nComputed  : {actual}")

    if expected:
        if actual.lower() == expected.lower():
            print("Expected  : (matches)")
            print("Result    : PASSED — file is authentic.")
            return True
        else:
            print(f"Expected  : {expected}")
            print("Result    : FAILED — file may be corrupted or tampered with.")
            return False

    return True

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Search and download Microsoft ISOs from rg-adguard.net",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-s", "--search", help='Search term, e.g. "Win_Pro_11_22H2"')
    parser.add_argument("-u", "--uuid",   help="Direct file UUID from rg-adguard")
    parser.add_argument("-o", "--output",
                        default=".",
                        help="Output directory  (default: current directory)")
    parser.add_argument("-f", "--file", dest="file_match", default=None,
                        help="Substring to auto-select a file from the package menu (case-insensitive)")
    parser.add_argument("--no-resume", action="store_true",
                        help="Disable download resume")
    parser.add_argument("--debug", action="store_true",
                        help="Enable verbose debug output (headers, selectors, page content)")
    args = parser.parse_args()

    banner()

    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nOutput dir: {output_dir}")

    uuid = args.uuid

    # ── Interactive mode ───────────────────────────────────────────────────────
    if not args.search and not uuid:
        term = input("\nEnter search term or UUID: ").strip()
        if UUID_RE.fullmatch(term):
            uuid = term
        else:
            args.search = term

    # ── Search → UUID ──────────────────────────────────────────────────────────
    if args.search and not uuid:
        results = search_catalog(args.search)

        if not results:
            print(f"\nNo results. Browse manually:")
            print(f"  {BASE_URL}/search?q={quote(args.search)}&lang=en-us")
            print(f"Copy the UUID from the file's URL then re-run:")
            print(f"  python {Path(__file__).name} -u <uuid>")
            sys.exit(1)

        print()
        idx  = pick(results, "name", "Select file number")
        uuid = results[idx]["uuid"]
        print(f"\nSelected : {results[idx]['name']}")
        print(f"UUID     : {uuid}")

    # ── Scrape hashes + fetch rg-adguard tools (password + smv.exe) ───────────
    hashes = scrape_hashes(uuid)

    print("  Fetching rg-adguard tools (password + smv.exe)...")
    rg_tools = fetch_rg_tools()
    smv_exe  = rg_tools["smv_exe"] if rg_tools else None

    if rg_tools and rg_tools.get("password") and "password" not in hashes:
        hashes["password"] = rg_tools["password"]
        print(f"  PASSWORD : {hashes['password']}  (from tools.cab)")
    elif "password" not in hashes:
        hashes["password"] = RG_ARCHIVE_PASSWORD
        print(f"  PASSWORD : {RG_ARCHIVE_PASSWORD}  (hardcoded fallback)")

    # ── Fetch download links ───────────────────────────────────────────────────
    info = get_download_links(uuid, debug=args.debug)

    if not info or not info["links"]:
        print("\nNo download links returned.")
        print("UUID may be invalid, or the site is rate-limiting free downloads.")
        sys.exit(1)

    if info["files"]:
        print("\nPackage contents:")
        for f in info["files"]:
            print(f"  {f['name']:60}  {f['size']}")

    cdn_headers  = info.get("cdn_headers")
    dl_cookies   = info.get("cookies")
    dl_checksums = info.get("checksums", {})   # url → sha-1 of .7z

    # ── Detect split archives ──────────────────────────────────────────────────
    split_re = re.compile(r"\.(7z\.\d{3}|part\d+\.(rar|7z)|zip\.\d{3})$", re.I)
    is_split = len(info["links"]) > 1 and all(
        split_re.search(l.split("?")[0]) for l in info["links"]
    )

    def try_download(url: str) -> Path | None:
        """
        Download priority:
          1. aria2c (intended client — single connection to avoid rate limiting)
          2. curl_cffi with Chrome impersonation + sec-* headers
          3. Playwright browser fallback (passes Cloudflare JS challenge)
        """
        sha1 = dl_checksums.get(url)
        result = download_via_aria2c(url, sha1, output_dir)
        if result:
            return result

        result = download_file(
            url, output_dir,
            resume=not args.no_resume,
            cdn_headers=cdn_headers,
            cookies=dl_cookies,
        )
        return result

    # ── Download archives ──────────────────────────────────────────────────────
    # For split archives: download all parts.
    # For multi-file packages: let user pick the target, then download ALL
    # archives upfront so SVF delta reconstruction has its base ISO ready.
    # For single link: just download it.
    all_downloaded: list[Path] = []
    selected_url = info["links"][0]

    if is_split:
        print(f"\nDetected {len(info['links'])}-part archive — downloading all parts.")
        for link in info["links"]:
            part = try_download(link)
            if not part:
                print("A part failed to download — aborting.")
                sys.exit(1)
            all_downloaded.append(part)

    elif len(info["links"]) > 1:
        # Let user pick which ISO they want, then pull all archives so any
        # SVF delta has its base file ready without a second round-trip.
        link_items = []
        for i, link in enumerate(info["links"]):
            label = info["files"][i]["name"] if i < len(info["files"]) else Path(link.split("/")[-1]).name
            size  = info["files"][i].get("size", "") if i < len(info["files"]) else ""
            link_items.append({"name": label, "size": size, "url": link})
        section("Select file to download")
        if args.file_match:
            match = args.file_match.lower()
            matched = [i for i, item in enumerate(link_items) if match in item["name"].lower()]
            if len(matched) == 1:
                idx = matched[0]
                print(f"Auto-selected: {link_items[idx]['name']}")
            elif len(matched) > 1:
                print(f"'{args.file_match}' matched {len(matched)} files — showing menu.")
                idx = pick(link_items, "name", "Select file number")
            else:
                print(f"No file matched '{args.file_match}' — showing menu.")
                idx = pick(link_items, "name", "Select file number")
        else:
            idx = pick(link_items, "name", "Select file number")
        selected_url = link_items[idx]["url"]
        other_urls   = [item["url"] for item in link_items if item["url"] != selected_url]

        # Download selected first so it's ready immediately
        dl = try_download(selected_url)
        if not dl:
            sys.exit(1)
        all_downloaded.append(dl)

        # Download remaining archives — required as SVF base for delta reconstruction.
        # (e.g. Enterprise LTSC is stored as a delta on top of Consumer Editions)
        if other_urls:
            print(f"\nPackage has {len(other_urls)} additional archive(s) needed for SVF reconstruction.")
            for url in other_urls:
                extra = try_download(url)
                if extra:
                    all_downloaded.append(extra)

    else:
        dl = try_download(info["links"][0])
        if not dl:
            sys.exit(1)
        all_downloaded.append(dl)

    downloaded = all_downloaded[0]

    # ── Extract all downloaded archives ───────────────────────────────────────
    archive_password = hashes.pop("password", None)   # remove from hashes dict
    final_file = downloaded
    extraction_succeeded = False

    # Track files produced by non-selected archives (base ISOs, hash files, etc.)
    # so we can clean them up after reconstruction.
    intermediate_files: set[Path] = set()

    # Extract every downloaded archive so all base ISOs are present before smv.exe runs
    svf_files: list[Path] = []
    for archive in all_downloaded:
        if archive.suffix.lower() not in (".7z", ".zip"):
            continue
        before = set(output_dir.iterdir())
        extracted = extract_7z(archive, output_dir, password=archive_password)
        after = set(output_dir.iterdir())

        # Files created by extracting a non-selected (base) archive are intermediates
        if archive.resolve() != downloaded.resolve():
            intermediate_files.update(after - before)

        if not extracted:
            if archive_password is None:
                print("Extraction failed — archive may be password-protected.")
                print(f"Check the file page for a password: {BASE_URL}/file/{uuid}")
            else:
                print(f"Extraction failed for {archive.name} — you may need to extract manually.")
            continue
        if extracted.suffix.lower() == ".svf":
            # Collect SVF files — reconstruct after all archives are extracted
            if archive.resolve() == downloaded.resolve():
                svf_files.insert(0, extracted)   # selected SVF goes first
            else:
                svf_files.append(extracted)
        elif archive.resolve() == downloaded.resolve():
            # The user's selected archive produced a direct ISO
            final_file = extracted
            extraction_succeeded = True

    # ── Reconstruct ISOs from SVF files ───────────────────────────────────────
    if svf_files:
        if smv_exe:
            section("Reconstruct ISO (smv.exe)")
            # Reconstruct base SVFs first so the target SVF has what it needs
            for svf in svf_files[1:]:
                print(f"  Reconstructing base: {svf.name}")
                apply_svf(smv_exe, svf, output_dir)
            # Reconstruct the target (user-selected) SVF
            target_svf = svf_files[0]
            iso = apply_svf(smv_exe, target_svf, output_dir)
            if iso:
                final_file = iso
                extraction_succeeded = True
            else:
                print("smv.exe failed — .svf file kept for manual processing.")
                final_file = target_svf
        else:
            print("  .svf file extracted but smv.exe not available.")
            print("  Cannot reconstruct ISO without smv.exe from tools.cab.")
            final_file = svf_files[0]
    elif downloaded.suffix.lower() not in (".7z", ".zip"):
        # Downloaded file is already an ISO or other format
        final_file = downloaded
        extraction_succeeded = True

    # ── Clean up intermediate files ───────────────────────────────────────────
    if extraction_succeeded:
        cleanup: set[Path] = set()
        # Archives we downloaded
        cleanup.update(output_dir.glob("*.7z"))
        # SVF delta files and their hash sidecars
        cleanup.update(output_dir.glob("*.svf"))
        cleanup.update(output_dir.glob("*.hash"))
        # Files extracted from non-selected (base) archives (e.g. consumer ISO)
        cleanup.update(intermediate_files)
        # Never delete the final ISO
        cleanup.discard(final_file)
        for p in cleanup:
            try:
                p.unlink()
            except OSError:
                pass

    # Clean up rg-adguard tools directory
    if rg_tools and rg_tools.get("tools_dir"):
        shutil.rmtree(str(rg_tools["tools_dir"]), ignore_errors=True)

    # ── Hash verification ──────────────────────────────────────────────────────
    # Hashes on the page are for the extracted ISO, not the archive.
    # Only verify if we successfully extracted (or the download is already an ISO).
    is_archive = downloaded.suffix.lower() in (".7z", ".zip")

    if is_archive and not extraction_succeeded:
        # Verify archive integrity via sha-1 from aria2c if available
        sha1_from_aria2c = dl_checksums.get(selected_url)
        if sha1_from_aria2c:
            print("\nVerifying archive integrity (SHA-1 from aria2c)...")
            verify_hash(downloaded, sha1_from_aria2c, "sha1")
        print("\nSkipping ISO hash check — extraction did not produce a file.")
    else:
        # Prefer SHA-256 from page scrape; fall back to sha-1
        algo     = next((a for a in ("sha256", "sha1", "md5") if a in hashes), None)
        expected = hashes.get(algo) if algo else None
        if algo and expected:
            verify_hash(final_file, expected, algo)
        else:
            print("\nNo hash available — computing SHA-256 for your records.")
            verify_hash(final_file)

    # ── Done ───────────────────────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print(f"  ISO ready: {final_file}")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
