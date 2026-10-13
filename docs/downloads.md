# Downloads

Calibre-Web NextGen+ can find books that are not in your library and download them into it. The downloader is built in: it runs inside the server, with no separate service to install or expose.

## For readers

Search as usual. When downloads are on and you may use them, the results page has two tabs: **In your library** and **Get more books**.

**Get more books** searches a book catalogue (Open Library by default) and lists matching books:

- A book you already have is marked **In your library** and links to it.
- **Find downloads** searches the download sources for that book and lists the files found, with format, size, source and language.
- **Find audiobooks** appears when an audiobook source is on.
- **Download** (or **Request**, if your requests need approval) fetches the file. When it has been imported, the row links to the book.

Only formats the library accepts can be downloaded: EPUB, PDF, MOBI (if allowed), and M4B/M4A audiobooks. A file whose format is only known after download is checked once it arrives. An error page, or the wrong kind of file, is never imported.

## For administrators

1. **Administration → Book sources.** Switch on **Allow requests from book sources**. Under **Who can use it**, choose who may download. Add **No approval needed** for people who should not wait for you.
2. **Administration → Downloads.** Switch on **Allow downloads**. Choose which catalogue search uses (Open Library needs no key; Hardcover uses the token from your metadata settings).
3. Under **Sources and clients**, turn on at least one source:
   - **Download Sources**: Anna's Archive (direct download). A donator key speeds it up.
   - **Libgen Search**: LibGen.
   - **AudiobookBay**: audiobooks. These are torrents, so they also need a client under **Download Clients**: qBittorrent, Transmission, Deluge or rTorrent, or a debrid service (Real-Debrid, AllDebrid, TorBox, Debrid-Link).
   - **Cloudflare Bypass**: many direct-download mirrors sit behind a Cloudflare check. The built-in bypass runs a hidden Chromium inside the container. Alternatively, point it at a FlareSolverr instance you run.
   - **Mirrors** and **Network**: alternative mirror addresses, DNS, proxies.

**Downloading now** on the same page shows what the downloader is working on.

### What happens to a download

1. A request becomes a normal book-source job. Access, approval, format rules, cancel and retry all apply.
2. The downloader fetches the release. Torrents go through the configured client; direct downloads go straight to the server.
3. When it finishes, the file is copied into private staging and its bytes are checked. The downloader's copy is then deleted.
4. The book is imported through ingest, with your normal conversion and metadata settings, and credited to the requester.

The downloader keeps its settings and work files in `/config/downloads`. The `CWNG_DOWNLOADS_*` variables in `examples/.env.example` can move them.

### Audiobooks

The library imports one audio file per book (M4B or M4A). A release that turns out to be a folder of chapters, or MP3 files, cannot be imported as a single book and fails with `audiobook_needs_single_file` or `no_usable_book`. Pick another release.

### Troubleshooting

| What you see | What it means |
| --- | --- |
| *No ebook source is switched on yet* | Turn on Anna's Archive or LibGen under **Sources and clients**. |
| A request ends **Failed** with `client_job_failed` | The source could not deliver that release, often a Cloudflare check that failed. Try another release, or check the bypass. |
| `no_usable_book` | The download was not a readable book of that kind. |
| `format_not_allowed` | The file's real format is not one the library accepts (see the upload formats in the basic configuration). |
| `client_job_stalled` | The download made no progress for a week and was given up. |

## Where it comes from

The downloader is [Shelfmark](https://github.com/calibrain/shelfmark)'s download engine (MIT License), vendored into `cps/services/downloader/engine` by `scripts/sync_shelfmark_engine.py`. Its web interface, accounts and notifications are not included; Calibre-Web NextGen+ provides those. Re-running the script against a newer Shelfmark checkout takes upstream fixes.
