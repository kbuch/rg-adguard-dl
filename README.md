# Microsoft ISO Downloader

A Python script that searches and downloads Microsoft ISOs from [rg-adguard.net](https://files.rg-adguard.net), handling everything automatically: search, file selection, download via aria2c, password-protected archive extraction, SVF delta reconstruction, and SHA-256 verification.

---

## How it works

rg-adguard.net is a Microsoft ISO mirror that serves files through Cloudflare-protected CDN endpoints. This script:

1. Uses a headless browser (Playwright) to search the catalog and scrape file hashes — required because search results and file detail pages are JavaScript-rendered
2. Downloads archives via **aria2c**, which is the site's intended download client and bypasses Cloudflare natively
3. Extracts password-protected `.7z` archives using py7zr — the archive password is fetched dynamically from rg-adguard's own `tools.cab` on every run
4. Reconstructs ISOs from `.svf` delta files using `smv.exe` (bundled in `tools.cab`)
5. Verifies the final ISO against the SHA-256 hash scraped from the file page

---

## Prerequisites

### Python

Python 3.11 or newer is required.

- **Windows**: Download from [python.org](https://python.org) or `winget install Python.Python.3`
- **macOS**: `brew install python` or download from [python.org](https://python.org)

### Python packages

```bash
pip install curl_cffi tqdm playwright py7zr
playwright install chromium
```

### aria2c

aria2c is required for downloading. It is the site's intended download client and handles Cloudflare CDN authentication natively.

**macOS:**
```bash
brew install aria2
```

**Windows:**

`winget install aria2` may install aria2c but not add it to `PATH`, causing the script to not find it. The reliable approach is a manual install:

1. Download the latest Windows release from [github.com/aria2/aria2/releases](https://github.com/aria2/aria2/releases) — grab the `aria2-*-win-64bit*.zip`
2. Extract the zip and locate `aria2c.exe`
3. Move `aria2c.exe` to `C:\Windows\System32\`
4. Verify it works: open a new terminal and run `aria2c --version`

### Wine *(macOS only, required for Enterprise LTSC and other SVF-based ISOs)*

Some ISOs (notably Windows 11 Enterprise LTSC) are packaged as SVF delta files that must be reconstructed using `smv.exe`, a Windows binary bundled by rg-adguard. On macOS this requires Wine.

```bash
brew install --cask wine-stable
```

**Gatekeeper quarantine:** macOS will block Wine Stable from running the first time with a popup saying *"Wine Stable.app Not Opened"*. This is Apple's Gatekeeper flagging an app from an unnotarized developer — Wine is safe. Remove the quarantine flag with:

```bash
sudo xattr -rd com.apple.quarantine /Applications/Wine\ Stable.app
```

Alternatively, go to **System Settings → Privacy & Security**, scroll down, and click **Allow Anyway** for Wine Stable (the button appears after the first blocked attempt).

---

## Usage

```bash
# Interactive mode — prompts for search term or UUID
python get_microsoft_iso.py

# Search by name
python get_microsoft_iso.py -s "Win 11 Enterprise LTSC"

# Direct UUID (skip search)
python get_microsoft_iso.py -u 9f118776-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# Skip the file selection menu by matching a name substring
python get_microsoft_iso.py -u <uuid> -f "ltsc"
python get_microsoft_iso.py -s "Win 11 24H2" -f "consumer"

# Custom output directory
python get_microsoft_iso.py -s "Win 11 Enterprise LTSC" -o ~/ISOs

# Verbose debug output
python get_microsoft_iso.py -u <uuid> --debug
```

---

## Gotchas

### Some ISOs require two downloads (SVF delta)

rg-adguard packages certain ISOs as SVF delta files to save storage — the ISO is stored as a binary diff against another (larger) ISO. Windows 11 Enterprise LTSC, for example, is a delta on top of the Consumer Editions ISO.

When this happens the script automatically downloads both archives, extracts everything, reconstructs the target ISO, then cleans up all intermediate files (`.7z`, `.svf`, `.hash`, and the base ISO). You will see a message like:

```
Package has 1 additional archive(s) needed for SVF reconstruction.
```

This is expected — you cannot get the LTSC ISO without the Consumer Editions archive as a base. The original rg-adguard batch script does the same thing.

### Archive password

rg-adguard's `.7z` archives are password-protected. The script fetches the password dynamically from rg-adguard's own `tools.cab` on every run, so if the password ever changes it will be picked up automatically without any code changes.

### aria2c on Windows not found after winget install

`winget install aria2` installs aria2c as a portable app in a per-user directory that is not on `PATH`. Opening a new terminal after install does not help — the directory is simply not in the system `PATH`. The fix is to copy `aria2c.exe` to `C:\Windows\System32\` as described in the prerequisites above.

### Wine Gatekeeper block on macOS

Covered in the Wine section above. The quarantine attribute is set by macOS on anything downloaded from the internet that isn't notarized by Apple. The `xattr` command removes it permanently for that app — you only need to do this once.

### macOS: SVF reconstruction requires Wine

Direct ISO downloads (most standard Windows editions) work fully on macOS without Wine. SVF-based ISOs (Enterprise LTSC and similar) require `smv.exe` to reconstruct, which is a Windows binary. Without Wine the script will complete the download and extraction but stop at reconstruction, leaving the `.svf` file in place with instructions.

### Script requires Python 3.11+

The `playwright` package pulls in `greenlet` as a dependency, which requires compilation on Python versions below 3.11 and will fail without MSVC build tools on Windows. Python 3.11+ ships pre-built wheels that install without a compiler.

---

## Platform support

| Feature | Windows | macOS |
|---|---|---|
| Search & hash scraping | ✅ | ✅ |
| Download via aria2c | ✅ | ✅ |
| Archive extraction | ✅ | ✅ |
| Direct ISO packages | ✅ | ✅ |
| SVF delta reconstruction | ✅ | ✅ with Wine |

---

## Credits

ISOs are served by [rg-adguard.net](https://files.rg-adguard.net). The `tools.cab` bundled by that site contains `aria2c.exe`, `7z.exe`, and `smv.exe` (SmartVersion) used for delta reconstruction.
