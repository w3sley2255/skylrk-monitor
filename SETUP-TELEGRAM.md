# Telegram setup — finish in ~5 minutes

Everything else is already built, tested, and seeded. Only these steps need you,
because they involve secrets that must never pass through a chat window.

---

## Step 1 — Create the bot (2 min)

1. Open Telegram, search for **@BotFather**, open the chat, press **Start**.
2. Send: `/newbot`
3. It asks for a **name** → anything, e.g. `SKYLRK Restock`
4. It asks for a **username** → must end in `bot`, e.g. `skylrk_restock_wes_bot`
5. BotFather replies with a line like:

   ```
   Use this token to access the HTTP API:
   1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```

   That whole string is your **bot token**. Keep it private — anyone with it
   controls the bot.

## Step 2 — Start a chat with your own bot (30 sec)

Click the `t.me/<your_bot_username>` link BotFather sent, press **Start**, and
send it any message (e.g. `hi`).

**This step is mandatory.** A Telegram bot cannot message you first — if you skip
it, sends fail with `403: bot can't initiate conversation with a user`.

## Step 3 — Get your chat ID (1 min)

Easiest way — put the token in `.env` first (Step 4), then run:

```powershell
python get_chat_id.py --write
```

It asks Telegram which chats your bot can see and writes the ID into `.env` for
you. If it says the bot has received no messages, you skipped Step 2.

> Manual alternatives: message **@userinfobot**, which replies with your numeric
> `Id`; or open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a
> browser and read `"chat":{"id":...}`.

## Step 4 — Put the values in `.env`

Open `.env` in this folder (Notepad is fine) and fill in the blank lines
(`get_chat_id.py --write` can fill the second one for you):

```
TELEGRAM_BOT_TOKEN=1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=987654321
```

No quotes, no spaces around the `=`. Save.

`.env` is gitignored — it will never be committed or pushed.

## Step 5 — Send yourself a real test alert

```powershell
cd "$env:USERPROFILE\OneDrive\桌面\skylrk-monitor"
python send_test_alert.py
```

A sample "XS/S RESTOCK" message with a product photo should arrive in Telegram
within a couple of seconds. If it does, the channel works and you are done
locally — the monitor will use the same path for real alerts.

Troubleshooting:

| Error | Fix |
|---|---|
| `Missing required environment variable` | A line in `.env` is still blank |
| `401 Unauthorized` | Token is wrong/truncated — re-copy from BotFather |
| `400 chat not found` | Chat ID is wrong, or you skipped Step 2 |
| `403 bot can't initiate` | You never messaged the bot — do Step 2 |

---

# GitHub Actions — run it 24/7 without your PC

The workflow at `.github/workflows/monitor.yml` is already written (15-min cron,
manual trigger, commits state back). You just need a repo.

## Step 6 — Push to a private GitHub repo

Create an **empty private** repo on github.com (no README, no .gitignore), then:

```powershell
cd "$env:USERPROFILE\OneDrive\桌面\skylrk-monitor"
git init
git add -A
git commit -m "SKYLRK clothing monitor: initial setup, state seeded"
git branch -M main
git remote add origin https://github.com/<YOUR-USERNAME>/skylrk-monitor.git
git push -u origin main
```

Confirm `.env` is **not** in the pushed files — `git status` should never list it.

## Step 7 — Add the two secrets

Repo → **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**. Add these two, pasting the same values as in `.env`:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Paste them directly into GitHub's form — encrypted at rest, never visible again,
and masked in logs.

## Step 8 — Kick off the first run

Repo → **Actions** tab → **SKYLRK monitor** → **Run workflow**.

The run should finish green in under a minute. Because state was already seeded
with the current 26 clothing products, this run sends **no alerts** — that is
correct. From then on the cron fires every 15 minutes and you only hear from it
when something actually changes.

---

## Everyday commands

```powershell
python tests/test_diff.py                      # 8/8 detection tests
python skylrk_monitor.py --dry-run             # check live, print findings, send/save nothing
python skylrk_monitor.py                       # a normal run (sends to Telegram)
python skylrk_monitor.py --seed                # re-baseline, no alerts
```

## Pausing / stopping

- **Pause:** Actions tab → SKYLRK monitor → **⋯** → **Disable workflow**
- **Resume:** same menu → **Enable workflow**
- **Kill the bot entirely:** @BotFather → `/deletebot`
