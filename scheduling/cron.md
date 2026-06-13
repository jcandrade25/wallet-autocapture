# Scheduling on macOS / Linux (cron)

Run `wallet-autocapture` once a day with a single crontab line. The entry point is
the `src.run_daily` module, run from the repo root so the `src` package resolves.

## TL;DR

Edit your crontab:

```bash
crontab -e
```

Add one line (runs every day at 07:30, repo at `~/code/wallet-autocapture`):

```cron
30 7 * * *  cd "$HOME/code/wallet-autocapture" && /usr/bin/python3 -m src.run_daily >> logs/run_daily.log 2>&1
```

Cron field order is `minute hour day-of-month month day-of-week`. `30 7 * * *`
= 07:30 every day. Change `30 7` to retime it.

## Use the repo's own Python (recommended: a venv)

cron runs with a minimal `PATH`, so always use an **absolute** interpreter path.
If the project has a virtualenv, point at the venv's python — no need to activate
it, the venv interpreter sets up its own `sys.path`:

```cron
30 7 * * *  cd "$HOME/code/wallet-autocapture" && ./.venv/bin/python -m src.run_daily >> logs/run_daily.log 2>&1
```

Create that venv once:

```bash
cd ~/code/wallet-autocapture
python3 -m venv .venv
# wallet-autocapture is stdlib-only by default, so there is usually nothing to
# install. If you enabled an optional extra (e.g. requests), install it here:
# ./.venv/bin/pip install -r requirements.txt
```

## Logs

The redirect `>> logs/run_daily.log 2>&1` appends stdout+stderr to a log file in
the repo (`run_daily` also writes its own structured log + `capturas_log.md`).
Make sure the folder exists:

```bash
mkdir -p ~/code/wallet-autocapture/logs
```

Rotate it occasionally so it does not grow forever, e.g. a logrotate rule or a
second cron line:

```cron
0 4 1 * *  : > "$HOME/code/wallet-autocapture/logs/run_daily.log"   # truncate monthly
```

## Secrets (IMAP password) in cron

cron does **not** load your shell profile, so env vars you set in `~/.zshrc` /
`~/.bashrc` are not visible to the job. Pick one:

1. **Password file (simplest).** Put the IMAP app-password in the file named by
   `email.password_file` in `config.json` (e.g. `~/.wallet_imap`), one line, and
   lock it down:

   ```bash
   printf '%s' 'your-app-password' > ~/.wallet_imap
   chmod 600 ~/.wallet_imap
   ```

   `run_daily` resolves the secret from that file; no env var needed.

2. **Env var in the crontab itself.** Define it at the top of the crontab (it
   applies to all jobs below it). Note crontab does not expand `$HOME` in
   assignments, so use literal paths:

   ```cron
   WALLET_IMAP_PASSWORD=your-app-password
   30 7 * * *  cd /home/youruser/code/wallet-autocapture && /usr/bin/python3 -m src.run_daily >> logs/run_daily.log 2>&1
   ```

   This stores the password in plaintext in your crontab — prefer the file +
   `chmod 600` approach. Match the env var name to `email.password_env` in your
   config.

## macOS notes

- `crontab` works on macOS, but the job's process needs Full Disk Access if your
  mail/secret files live in a protected location. The cleaner macOS-native option
  is a **launchd** agent (`~/Library/LaunchAgents/`), which runs in your logged-in
  GUI session — analogous to the Interactive-logon Windows task. Minimal plist:

  ```xml
  <?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0">
  <dict>
    <key>Label</key>            <string>com.local.walletautocapture</string>
    <key>ProgramArguments</key>
    <array>
      <string>/Users/youruser/code/wallet-autocapture/.venv/bin/python</string>
      <string>-m</string>
      <string>src.run_daily</string>
    </array>
    <key>WorkingDirectory</key> <string>/Users/youruser/code/wallet-autocapture</string>
    <key>StartCalendarInterval</key>
    <dict>
      <key>Hour</key>    <integer>7</integer>
      <key>Minute</key>  <integer>30</integer>
    </dict>
    <key>StandardOutPath</key>   <string>/Users/youruser/code/wallet-autocapture/logs/run_daily.log</string>
    <key>StandardErrorPath</key> <string>/Users/youruser/code/wallet-autocapture/logs/run_daily.log</string>
  </dict>
  </plist>
  ```

  Load it:

  ```bash
  launchctl load ~/Library/LaunchAgents/com.local.walletautocapture.plist
  ```

## Verify the schedule

```bash
crontab -l                                   # show your crontab
cd ~/code/wallet-autocapture && python3 -m src.run_daily --dry   # one manual dry run
tail -n 40 ~/code/wallet-autocapture/logs/run_daily.log
```

`--dry` parses and reports without writing anything to Wallet — use it to confirm
the path, interpreter and credentials line up before trusting the daily job.
