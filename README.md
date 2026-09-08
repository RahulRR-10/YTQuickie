<div align="center">

# 🎵 YTQuickie

**A simple desktop app for ripping audio from YouTube and Spotify.**

Paste a link, pick your tracks, and get clean 192 kbps MP3s — no Python, no Node.js, no API keys, no setup. Download a single song or grab an entire playlist or album in one shot.

[![Platform](https://img.shields.io/badge/platform-Windows-blue)](#download)
[![License](https://img.shields.io/badge/license-MIT-green)](#license)
[![Made with](https://img.shields.io/badge/backend-FastAPI-009688)](#tech-stack)
[![Made with](https://img.shields.io/badge/frontend-React-61DAFB)](#tech-stack)

[Download](#download) · [Usage](#how-to-use) · [FAQ](#frequently-asked-questions) · [Build from Source](#running-from-source)

</div>

---

## Table of Contents

- [Download](#download)
- [How to Use](#how-to-use)
- [Downloads Folder](#downloads-folder)
- [Features](#features)
- [How It Works](#how-it-works)
- [Frequently Asked Questions](#frequently-asked-questions)
- [Project Architecture](#project-architecture)
- [Running From Source](#running-from-source)
- [Limitations](#limitations)
- [Privacy](#privacy)
- [Tech Stack](#tech-stack)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

---

## Download

1. Grab the latest Windows build from **[GitHub Releases](https://github.com/RahulRR-10/YTQuickie/releases)** — look for `YTQuickie-windows-x64.zip`.
2. Right-click the ZIP → **Extract All**.
3. Open the extracted folder and run `YTQuickie.exe`.

No additional dependencies are needed for the Windows release — FFmpeg is bundled in.

> **Seeing "Windows protected your PC"?**
> That's Windows SmartScreen, and it shows up because the executable isn't code-signed (code-signing certificates cost money independent developers often can't justify for a free tool). If you downloaded YTQuickie from the official [Releases page](https://github.com/RahulRR-10/YTQuickie/releases), it's safe to proceed: click **More info → Run anyway**.

---

## How to Use

### 1. Paste a link

Drop a YouTube or Spotify URL into YTQuickie and click **FETCH TRACKS**. Paste a playlist or album link and YTQuickie pulls in every track at once — no need to add songs one by one.

| Platform | Supported |
|----------|-----------|
| YouTube  | Single videos, full playlists |
| Spotify  | Tracks, full albums, full playlists |

### 2. Select your tracks

YTQuickie lists everything it finds. Select individual tracks or hit **SELECT ALL**. Batches are capped at **50 tracks**.

### 3. Convert

Click **CONVERT MP3s**. YTQuickie finds the matching audio, downloads it, and converts it to a **192 kbps MP3** — up to **4 tracks at once**.

---

## Downloads Folder

Files land in your Windows `Downloads` folder by default. Change this anytime from **Settings → Output Folder**.

- **Single track** → saved directly as `Artist - Song.mp3`
- **Multiple tracks** → bundled into `Playlist Name.zip`

---

## Features

- **Download entire playlists and albums in one go** — not just single tracks
- YouTube — single videos and full playlists
- Spotify — tracks, albums, and playlists
- Track preview with individual selection
- Batches of up to 50 tracks, 4 processed concurrently
- 192 kbps MP3 output
- Automatic ZIP bundling for multi-track downloads
- Custom output directory
- Automatic temp-file cleanup
- Live conversion progress
- Retro CRT-inspired desktop interface

---

## How It Works

For **Spotify** links:

```text
Spotify Link → Public Embed → Track Metadata → YouTube Search
→ Title + Duration Matching → Best Match → Download → FFmpeg → 192 kbps MP3
```

For **YouTube** links, the process starts directly from the video or playlist — no matching step needed.

Multiple tracks are processed concurrently through a small worker pool, so a full playlist doesn't download one file at a time.

---

## Frequently Asked Questions

<details>
<summary><strong>Do I need a Spotify account or API key?</strong></summary><br>

No. YTQuickie doesn't require Spotify login credentials or a developer account — it reads track metadata from Spotify's public embed.
</details>

<details>
<summary><strong>Do I need a YouTube API key?</strong></summary><br>

No. YTQuickie uses <code>yt-dlp</code> to search for and retrieve the matching YouTube audio.
</details>

<details>
<summary><strong>Do I need FFmpeg installed?</strong></summary><br>

Not for the Windows release — it's bundled in. If you're running from source, you'll need FFmpeg on your system <code>PATH</code>.
</details>

<details>
<summary><strong>Why doesn't my large Spotify playlist show every track?</strong></summary><br>

YTQuickie intentionally avoids requiring authenticated Spotify API access, relying instead on the public embed for metadata. That method caps out at roughly the first 100 tracks on very large playlists.
</details>

<details>
<summary><strong>Why did it download a different version of my song?</strong></summary><br>

For Spotify tracks, YTQuickie searches YouTube by title, artist, and duration, then scores the results to find the closest match. Since YouTube often hosts multiple versions of the same song — live performances, remixes, covers, music videos, mislabeled reuploads — the match isn't always guaranteed to be the original studio version.
</details>

<details>
<summary><strong>Where are temporary files stored?</strong></summary><br>

Under <code>~/.ytquickie/downloads</code>, and they're automatically cleaned up roughly an hour after conversion finishes.
</details>

---

## Project Architecture

```text
YTQuickie/
│
├── backend/
│   ├── main.py            # FastAPI server, metadata scraping, download jobs, SSE streams
│   ├── desktop.py         # Desktop window and file-system bridge
│   ├── YTQuickie.spec     # PyInstaller configuration
│   └── requirements.txt
│
├── frontend/
│   ├── src/                # React application and UI
│   ├── vite.config.js
│   └── package.json
│
└── screenshots/
```

---

## Running From Source

For developers who want to modify or contribute to YTQuickie.

**Requirements**

- Python 3.11+
- Node.js 22+
- FFmpeg
- Microsoft Edge WebView2 *(pre-installed on most Windows 10/11 systems)*

**Setup**

```bash
git clone https://github.com/RahulRR-10/YTQuickie.git
cd YTQuickie

# Build the frontend
cd frontend
npm install
npm run build
cd ..

# Install backend dependencies
cd backend
pip install -r requirements.txt

# Run the app
python desktop.py
```

**Development mode** (frontend hot-reload)

```bash
# Terminal 1 — frontend
cd frontend
npm run dev

# Terminal 2 — backend
cd backend
uvicorn main:app --reload
```

---

## Limitations

- **50-track batch limit** — keeps consecutive YouTube search requests in check.
- **Large Spotify playlists** — the public embed exposes roughly the first 100 tracks only.
- **Internet required** — for both searching and downloading audio.
- **Imperfect matching** — Spotify tracks are matched to YouTube by metadata, which works well but isn't foolproof.

---

## Privacy

YTQuickie never asks for:

- Spotify API credentials
- YouTube API credentials
- Spotify login credentials

All downloading and audio processing happens locally on your machine — nothing is uploaded anywhere.

---

## Tech Stack

| Component | Technology |
|---|---|
| Frontend | React + Vite |
| Backend | FastAPI |
| Desktop shell | pywebview |
| YouTube extraction | yt-dlp |
| Audio processing | FFmpeg |
| Packaging | PyInstaller |
| Progress updates | Server-Sent Events |

---

## Disclaimer

YTQuickie is an open-source project intended for educational, archival, and personal use. Downloading or converting copyrighted material may be restricted by copyright law or by the terms of the platform hosting the content. Please respect copyright and support the artists, musicians, and creators behind the music you enjoy.

---

## Contributing

Found a bug or have an idea?

- [Open an issue](https://github.com/RahulRR-10/YTQuickie/issues)
- Suggest a feature
- Submit a pull request

Check out the [GitHub repository](https://github.com/RahulRR-10/YTQuickie) to get started.

---

## License

MIT — see [LICENSE](LICENSE) for details.

<p align="center">
<strong>YTQuickie</strong><br>
<code>PASTE → PICK → RIP → ENC → OK</code>
</p>