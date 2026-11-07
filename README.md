# Dropdrive

Copy big files from a **Dropbox link** straight into a **Google Drive folder**, on a Mac.

> **One direction only: Dropbox → Google Drive.** It can't copy from Google Drive to Dropbox (see [Possible improvements](#possible-improvements)).

- **No disk space needed.** Files stream through your Mac without being saved, so a 200 GB video works on a laptop with 20 GB free.
- **Survives interruptions.** Wi-Fi drops, Google hiccups, or a closed window don't lose your progress. Run Dropdrive again and it continues from where Google Drive left off.
- **Checks every file.** When Dropbox provides a fingerprint for a file, Dropdrive confirms the copy in Google Drive matches it exactly.
- **Copies whole folders.** Paste a Dropbox folder link, pick the files you want (or all of them), and subfolders are recreated in Google Drive. Files already there are skipped.

---

## Part 1: For everyone

### What you need

- A Mac, plugged in to power.
- A Dropbox account to sign in with. A free one is fine. Nothing gets saved into your Dropbox, so its size doesn't matter.
- A Google account with **Editor** access to the destination folder, and **enough Google storage for the files** (see [Storage](#storage-read-this-first)).

### Storage: read this first

When you upload into a Google Drive folder, the files belong to **you** and use **your** Google storage, even if someone else owns the folder and has plenty of space. The only exception is a **Shared Drive** (a Google Workspace feature; the folder's path starts with "Shared drives"), which uses the organisation's storage.

So if you're uploading 300 GB into a friend's folder, your own Google account needs 300 GB free. Dropdrive checks this before starting and warns you. Extra space is at [one.google.com](https://one.google.com) and can be cancelled afterwards, but the files stay yours, so the folder owner should make their own copy before you delete anything.

### Install (one time)

1. On this GitHub page, click the green **Code** button, then **Download ZIP**.
2. Open your **Downloads** folder and double-click the ZIP to unzip it. Move the `dropdrive` folder wherever you like (Documents is fine).
3. Open the folder and double-click **Dropdrive.command**.
4. macOS will probably block it the first time, because it came from the internet:
   - Click **Done** (or **OK**) on the warning.
   - Open **System Settings → Privacy & Security**, scroll down, and click **Open Anyway** next to the message about Dropdrive.command. Confirm with your password or Touch ID.
   - On older macOS versions you can instead right-click Dropdrive.command, choose **Open**, then **Open** again.
5. If a window asks to install **Command Line Tools**, click **Install**, wait for it to finish, then double-click Dropdrive.command again.

### Use it

Double-click **Dropdrive.command**. A Terminal window opens and asks you questions. Type your answer and press **Return**. Pressing Return on its own picks the suggestion in [brackets].

**The first time**, it sets itself up:

1. It offers to download **rclone**, a free tool it uses to sign in. Press Return.
2. **Connect Dropbox:** a browser window opens. Sign in to Dropbox and click **Allow**. Come back to the Terminal window.
3. **Connect Google Drive:** a browser window opens. Sign in with the Google account that has access to the destination folder and click **Allow**. If Google shows a "Google hasn't verified this app" screen, click **Advanced**, then **Go to rclone (unsafe)**. That screen is about rclone's sign-in, and it's normal.

**Then, each time:**

1. Choose **1. Copy from a Dropbox link**.
2. Paste the Dropbox link (press ⌘V) and press Return.
3. If it's a folder, you'll see a numbered list of files. Press Return for all of them, or type which ones you want, for example `3`, `1,4,7`, or `2-5`.
4. Paste the Google Drive folder link. To get it, open the folder in Google Drive in your browser and copy the address from the address bar.
5. Press Return to start.

You'll see a progress bar with speed and time left. **Leave the Mac plugged in and the Terminal window open.** You can lock the screen, but don't close the laptop lid. Dropdrive keeps the Mac awake on its own and shows a notification when it finishes.

### If something stops it

Just double-click **Dropdrive.command** again and choose **Resume unfinished transfers**. It asks Google how much it already has and continues from there.

To stop it on purpose, press **Control-C** in the Terminal window. Resume works the same way afterwards.

### Common messages

| Message | What to do |
|---|---|
| **Google can't find that folder from the account …** | The folder isn't shared with that Google account. Ask the owner to share it with that email as **Editor**, or choose **Accounts** in the menu and sign in to Google with the account it was shared with. |
| **Your Google account … can only view that folder** | You have Viewer access. Ask the owner to make you an **Editor**. |
| **Not enough Google storage** / **Your Google storage is full** | See [Storage](#storage-read-this-first). Get more space, then Resume. |
| **Google's daily upload limit was reached** | Google allows about 750 GB of uploads per account per day. Wait until tomorrow, then Resume. |
| **Your Dropbox account isn't allowed to open that link** | Ask the sender to share the folder with your Dropbox email, or set the link to "anyone with the link". |
| **Connection problem … Retrying** | Nothing to do. It retries on its own. |
| **Sign-in expired** or Google sign-in stops working | Choose **Accounts** in the menu and sign in again. If Google still refuses, see "Google sign-in stopped working" below. |

### How long will it take?

Everything passes through your internet connection twice (down from Dropbox, up to Google), so your **upload** speed is the limit. At 20 MB/s, 100 GB takes about 1.5 hours. At 5 MB/s, about 6 hours. The progress bar shows a live estimate.

---

## Part 2: Technical details

### How it works

- `get_shared_link_file` on the Dropbox API streams the file using HTTP `Range` requests, so it can restart at any byte.
- The stream is uploaded with a Google Drive **resumable upload** session in 64 MiB chunks. After any failure, Dropdrive asks Drive how many bytes it has committed and reopens Dropbox at that offset. Partial commits and lost sessions are handled too.
- The session URL and per-4 MiB block digests are saved, so a restart, crash, or reboot resumes the same session instead of starting over. Drive keeps unfinished sessions for about a week.
- When Dropbox lists a `content_hash` for a file (folder links do; single-file links don't), Dropdrive computes the same [Dropbox content hash](https://www.dropbox.com/developers/reference/content-hash) while streaming and compares at the end. Without a hash, it checks the size only.
- **rclone is only used to sign in.** It runs the OAuth browser flow, stores and renews tokens, and Dropdrive borrows the current access token. All transfers happen in `dropdrive.py`, which uses only the Python standard library.
- `caffeinate -i -s` holds off sleep while transfers run.

### Command line

```bash
python3 dropdrive.py                                   # guided menu
python3 dropdrive.py --from "DROPBOX_LINK" --to "DRIVE_FOLDER_LINK" --files all -y
python3 dropdrive.py --resume -y                       # resume everything unattended
python3 dropdrive.py --accounts                        # sign in / switch accounts
```

`--to` accepts a folder link, a folder ID, or `root` for My Drive. If you leave it out with `--from`, it defaults to My Drive. `--files` uses the numbers from the listing.

### Files it keeps

| Path | Contents |
|---|---|
| `~/Library/Application Support/Dropdrive/rclone.conf` | Dropbox and Google sign-in tokens (mode 600). Separate from any rclone config you already have. |
| `~/Library/Application Support/Dropdrive/transfers/` | Unfinished transfer state (session URLs and block digests). |
| `~/Library/Application Support/Dropdrive/bin/rclone` | rclone, if Dropdrive installed it. It's the official build, and its SHA-256 is checked against `downloads.rclone.org/<version>/SHA256SUMS`. An rclone already on your `PATH` or from Homebrew is used instead if present. |
| `~/Library/Logs/Dropdrive.log` | Activity and error log. |

To uninstall, delete the `dropdrive` folder and the two `Dropdrive` paths above.

### Security notes

- Sign-in uses rclone's OAuth apps. The Dropbox app has broad access to your Dropbox. The Google sign-in uses the full `drive` scope, because writing into a folder someone else shared with you isn't possible with the narrower `drive.file` scope. Dropdrive only reads Dropbox links you give it and only creates files and folders in the destination you choose.
- Tokens stay on your Mac in `rclone.conf`. To revoke access when you're done, use [Dropbox connected apps](https://www.dropbox.com/account/connected_apps) and [Google third-party access](https://myaccount.google.com/connections).
- Session URLs in `transfers/` grant upload access to that one in-progress file until it completes or expires.

### Google sign-in stopped working

rclone's shared Google client ID is being retired by Google, and rclone itself warns about this. If Google sign-in fails, create your own client ID ([rclone's guide](https://rclone.org/drive/#making-your-own-client-id), about 10 minutes, free). Then in Dropdrive choose **Accounts → Use your own Google client ID** and paste the client ID and secret.

### Limits

- macOS only, Python 3.8+ (the version Apple's Command Line Tools install works).
- One file at a time. Throughput is bounded by your uplink.
- Google: about 750 GB/day upload per account, 5 TB max per file.
- The Dropbox source has to be a shared link, and your Dropbox account has to be allowed to open it.

### Tests

```bash
python3 tests/test_engine.py
```

The tests run the transfer engine against local mock Dropbox and Google servers. The mocks inject dropped connections, partial chunk commits, 503s, a server that ignores `Range`, a lost upload session, a hash mismatch, and a give-up-then-resume cycle, and the tests check the uploaded bytes and content hash. No network or accounts are needed.

### Possible improvements

**Google Drive → Dropbox (reverse direction).** Not built yet. The transfer engine would carry over with a new source and destination:

- **Source:** Drive's `files.get?alt=media` supports `Range`, so downloads can restart at any byte. Drive provides `md5Checksum`, so files would be MD5-checked instead of using the Dropbox content hash.
- **Destination:** Dropbox upload sessions (`upload_session/start`, `append_v2`, `finish`). When a chunk fails, Dropbox's `incorrect_offset` error returns the offset it actually has, so resuming works the same way it does with Drive's resumable upload today.
- **Storage caveat to surface in the app:** files uploaded into a Dropbox shared folder count against the uploader's quota, and against every member's. Uploading through a Dropbox **File request** link counts against the requester's quota instead, which is usually what people want. Free Dropbox accounts have 2 GB.
- **Limits:** Dropbox caps uploads at 350 GB per file. Google may temporarily block downloads of a file that's been downloaded heavily.
- **UI:** add a direction choice to the start menu. The progress, resume and verification code stay shared.

Other ideas:

- Copy several files at once, for faster total time on fast connections.
- A small double-clickable app instead of the Terminal window.
- Support for Dropbox folders in your own account, not just shared links.

### Publishing

`Dropdrive.command` needs to stay executable. If you add it from a Mac where it's already executable, git records that. Otherwise run `git update-index --chmod=+x Dropdrive.command`.
