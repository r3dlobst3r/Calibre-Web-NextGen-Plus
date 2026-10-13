# Downloads with Shelfmark

[Shelfmark](https://github.com/calibrain/shelfmark) searches book metadata
providers and download sources (direct download, Prowlarr, IRC, Newznab, …) and
downloads releases. In this setup it runs as a private companion to CWNG:

- **Users never see Shelfmark.** They search in CWNG. Search results get a
  second tab, **Get more books**, beside **In your library**.
- **Administrators manage it in CWNG**, under **Administration → Downloads**.
  Shelfmark's own web interface has no published port.
- **Requests go through CWNG's book sources rules.** That covers who may download,
  who needs approval, allowed formats, cancel and retry. A downloaded file is
  imported through CWNG's normal ingest, credited to the account that asked for it.

## How a download works

1. A user searches. CWNG asks Shelfmark's metadata search for books. Results that
   are already in the library are marked **In your library**, matched by ISBN
   first and then by title and author.
2. **Find downloads** asks Shelfmark to search its sources for that book. Each
   EPUB, PDF, opted-in MOBI or not-yet-known file becomes a request button. Other
   formats are listed but can't be requested.
3. The request is a normal CWNG acquisition job. The worker queues the release in
   Shelfmark, then checks its progress every 15 seconds.
4. When Shelfmark finishes, CWNG streams the file into its private staging area
   over Shelfmark's API, then checks the file's bytes. An HTML error page or the
   wrong format never reaches the library. CWNG then asks Shelfmark to delete
   its copy.
5. The file is handed to CWNG's ingest service. The job reaches **In your library**
   only when the import receipt names the new book.

Shelfmark names each queue entry after the release itself and refuses
duplicates. So if a request is retried after a lost response, it can't start a
second download. Cancelling a request also cancels it in Shelfmark.

## Set up

Use `docker-compose.shelfmark.yml`. It builds CWNG from this checkout and
Shelfmark from `../shelfmark`. Both containers get the same `SHELFMARK_API_KEY`,
and CWNG gets `SHELFMARK_URL=http://shelfmark:8084`. Shelfmark runs with local
sign-in (`AUTH_METHOD=builtin`), so it only answers requests that carry that key.

Then, in CWNG:

1. Open **Administration → Book sources** and switch on **Allow requests from
   book sources**. Under **Who can use it**, grant the accounts that may
   download. Add **No approval needed** for accounts that should not wait for an
   administrator.
2. Open **Administration → Downloads** and choose **Connect**. CWNG checks the
   address and key, adds Shelfmark as a book source, and skips Shelfmark's
   first-run wizard. If no metadata provider is enabled, it also switches on
   Open Library, which needs no key, so search works straight away.
3. Under **Sources and search**, configure Shelfmark's sources, for example
   download sources, mirrors, Prowlarr, IRC or the Cloudflare bypass. The forms
   are Shelfmark's own settings, drawn by CWNG. Stored passwords are never shown;
   leave a password blank to keep it.

Without the environment variables, choose **Enter address and key** on the
Downloads page instead.

## What CWNG keeps for itself

The Downloads page does not show these Shelfmark settings tabs: users, sign-in
and security, notifications, and the output folder and file naming. CWNG owns
those concerns, and changing them in Shelfmark would break the hand-off.

## Troubleshooting

| Message | Meaning |
| --- | --- |
| *The download service is not responding* | The `shelfmark` container is down or still starting. `docker logs shelfmark`. |
| *rejected the API key* | `SHELFMARK_API_KEY` differs between the two containers, or Shelfmark is not using local sign-in. |
| *No metadata provider configured* | Choose **Connect** again, or enable a provider under **Sources and search**. |
| A request ends **Failed** with `client_job_failed` | Shelfmark could not download that release. Try another release, or retry later. |
| `no_usable_book` | The release was not a readable EPUB, PDF or MOBI. |

Shelfmark's log warns at startup that *no local admin account with a password exists*. That is expected here: CWNG signs in with the API key, and nobody signs in to Shelfmark directly.
