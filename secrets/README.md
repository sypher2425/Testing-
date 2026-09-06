# secrets/

Drop an optional `cookies.txt` here (exported from your browser) to let
yt-dlp fetch URL-ingested jobs using a logged-in session — mainly useful for
Instagram, where view counts and some content are hidden from anonymous
requests.

**Treat `cookies.txt` like a password.** It contains live session tokens for
whatever account you exported it from — anyone with the file can access that
account. Never commit it, never share it, never paste its contents anywhere.
This directory is `.gitignore`d (except this README) specifically so it's
never accidentally committed.

## How to get one

Use a browser extension that exports cookies in Netscape format, e.g.
["Get cookies.txt LOCALLY"](https://chromewebstore.google.com/) for
Chrome/Edge (search the Chrome Web Store — links change over time, so this
intentionally isn't hardcoded). While logged into the platform, export
cookies for that site and save the file here as `secrets/cookies.txt`.

## Enabling it

In your `.env`:

```
COOKIES_FILE=/run/secrets/cookies.txt
```

That's the path the file appears at *inside* the worker container (this
directory is bind-mounted there by `docker-compose.yml`) — not a path on your
own machine. No rebuild needed; `docker-compose.yml` mounts this directory
live, so dropping the file in and restarting the `worker` service is enough:

```
docker compose restart worker
```

Leave `COOKIES_FILE` blank (the default) if you don't need this — nothing
requires it.
