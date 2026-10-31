# axbot

[Русская версия](README.md)

A Telegram bot that keeps spam bots out of group chats. It was written for a Linux chat, so the captcha questions have some Linux humor in them. You don't need to know anything about Linux to answer them, just read carefully and use basic logic.

Note: the bot's messages and questions are in Russian.

## What it does

When someone joins the chat, the bot mutes them and posts a question with buttons. There are two kinds of questions. Some have one correct button, for example "The kernel takes 40 minutes to compile and the build started at 13:30. When will it finish?". Others need several buttons pressed in the right order, for example sorting distro names alphabetically.

If the answer is right, the bot unmutes the user and greets them. A wrong answer gives them a different question until they run out of attempts. When the attempts run out or time is up, the bot removes the user from the chat. You can choose between a kick (they can join again) and a ban.

The bot also:

- doesn't let anyone press buttons on someone else's captcha;
- kicks bots added by regular members and skips the check for anyone added by an admin;
- deletes "joined the group" and "left the group" messages if that is enabled;
- removes the captcha if the user leaves without answering;
- keeps everything in SQLite, so pending checks and timers survive a restart.

## Who can manage the bot

The owner, which is whoever added the bot to the chat. If the bot is removed and added again, the owner only changes if the person who re-added it is a chat admin.

The chat creator can always manage the bot.

Managers, appointed by the owner.

Superadmins listed in `SUPERADMINS`. They can see and configure every chat.

## Settings in private chat

Send `/start` to the bot and open "Мои чаты" (My chats). For each chat you can turn the captcha on or off, set the time limit, the number of attempts, what happens to users who fail, and the question type. The same panel has the greeting text and its auto-delete timer, deletion of service messages, and the list of managers. The "Попробовать капчу" (Try the captcha) button sends you a test question.

## Commands in the group

Only the owner and managers can use these. If anyone else sends one, the bot just deletes it. The bot deletes the command and its reply after a minute to keep the chat clean.

| Command | What it does |
|---|---|
| `/settings` | shows the settings and a link to the private panel |
| `/help` | list of commands |
| `/captcha on` or `off` | turns the captcha on or off |
| `/timeout 120` | time to answer in seconds, 30 to 3600 |
| `/attempts 2` | number of attempts, 1 to 5 |
| `/action kick` or `ban` | kick or ban users who fail |
| `/mode mixed`, `choice` or `order` | mixed questions, single-choice only, or order only |
| `/greeting text` | custom greeting. You can also reply to a message with the command. `/greeting reset` restores the default |
| `/greetttl 300` | delete the greeting after this many seconds, 0 means keep it |
| `/clean on` or `off` | delete join and leave messages |
| `/pass` | let a user in without the captcha. Reply to their captcha or message, or pass their ID |
| `/managers` | who manages the bot |
| `/addmanager`, `/delmanager` | add or remove a manager by replying to their message or by ID. Owner only |

The greeting text supports `{mention}` (a link to the user), `{name}` (their name) and `{chat}` (the chat title). HTML formatting works too.

## Running

Create a bot with [@BotFather](https://t.me/BotFather) and copy the token. Copy `.env.example` to `.env` and put the token in `BOT_TOKEN`.

With Docker:

```sh
docker compose up -d --build
```

Without Docker, Python 3.11 or newer:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m axbot
```

Then add the bot to your group and make it an admin with the "Ban users" and "Delete messages" permissions. Telegram doesn't tell bots about new members unless they are admins, so the captcha won't work without this.

## Development

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The questions live in [`axbot/questions.py`](axbot/questions.py). If you add your own, make sure the question itself contains everything needed to answer it.

## License

[GNU GPL v3](LICENSE) or any later version.
