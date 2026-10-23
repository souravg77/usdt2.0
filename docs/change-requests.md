# Changing the platform by chatting

There are two ways to change the platform without opening a task with Claude elsewhere.

| | Change requests page | Claude Code on the server |
|---|---|---|
| Where | the dashboard, **Change requests** | your phone or browser, through Remote Control |
| Good for | small, clear changes ("show X on the Trading page") | bigger work, debugging, running tests, looking at logs |
| What it can do | read the code on GitHub, write a change, open a pull request | everything you can do in a terminal on the server, with your approval |
| Runs tests | no | yes |
| Changes the server | never: you merge the pull request, then run the installer | only what you approve in the session |

Neither can switch on live trading from the website. The live lock stays in `.env` on the server.

## Change requests page

1. On GitHub: **Settings → Developer settings → Personal access tokens → Fine-grained tokens →
   Generate new token**. Repository access: **only** this repository. Permissions: **Contents**
   and **Pull requests** set to **Read and write**, nothing else. Set an expiry date.
2. On the dashboard: **Settings → GitHub (change requests)**. Paste the token; the repository is
   `souravg77/usdt2.0` unless you renamed it. Press **Check and save**.
3. On **Change requests**, describe the change and press **Send to Claude**. When it is done, the
   page shows a link to the pull request.
4. Read the pull request on GitHub. The tests have **not** been run: run them (or ask Claude Code on
   the server) before merging. Changes to safety-critical files (live lock, orders, keys, login,
   deploy) carry a warning: read those line by line.
5. Merge, then run `bash ~/AI_crypto_trader/deploy/install.sh` on the server.

What it won't do:

- It never sees `.env`, saved keys or anything in `data/`; it reads only the repository.
- It can't write `.env*`, `data/`, `.github/` or outside the repository, touches at most 15 files,
  and never merges.
- It refuses requests that weaken the live lock, the risk limits, keys or the login, and says why.

One limit of GitHub tokens: a token that can push branches can also push to `main`. This code only
ever pushes `change/...` branches. If your GitHub plan offers branch protection or rulesets for
private repositories, turn on "require a pull request" for `main`. Delete the token on GitHub if
you stop using this page.

## Claude Code in its own Docker container (recommended)

The `claude` service runs Claude Code in a container of its own. It can read and change the code
(on a branch) and read `./data` (trades, settings, the event log), but it **cannot see `.env`**, so
no key or password ever reaches it, and it can't run Docker or touch the trading services.

```bash
cd ~/AI_crypto_trader
docker compose --profile claude build claude                 # once, and again after each install
docker compose --profile claude run --rm claude claude       # once: sign in (opens a link), trust /work, then /exit
docker compose --profile claude up -d claude                 # starts Remote Control and keeps it running
```

The session then shows up under **Remote Control** in the Claude app and on claude.ai. Its sign-in is
kept in the `claude_signin` volume, so a restart doesn't ask again. Stop it with
`docker compose --profile claude stop claude`; see what it printed with `docker compose logs claude`.

It can run the tests (`python -m pytest -q`). It can't push to GitHub unless you sign in to GitHub
inside it; without that, it explains what it found and the fix goes through a pull request as usual.

## Claude Code directly on the server, from your phone

The same, without Docker. It can then read `.env` and run any command you approve, so prefer the
container above.

This gives you a full Claude Code session working in `~/AI_crypto_trader`, which you drive from
the Claude app or claude.ai.

```bash
# once, as the shawn user (not root)
curl -fsSL https://claude.ai/install.sh | bash
cd ~/AI_crypto_trader && claude            # first run: sign in with your Claude account, then /exit
sudo apt-get install -y tmux

# each time, or leave it running
tmux new -s claude                         # (tmux attach -t claude to come back)
cd ~/AI_crypto_trader && claude remote-control
```

The session then shows up under **Remote Control** in the Claude app and on claude.ai, and you can
chat with it from anywhere. Detach from tmux with `Ctrl-b d`; it keeps running.

To let it push branches and open pull requests, the server needs write access to GitHub. The
deploy key added earlier is read-only. Either add a second deploy key with **Allow write access**
(GitHub → repository → Settings → Deploy keys), or run `gh auth login` on the server.

Good habits:

- Keep its permission prompts on, so it asks before running commands or editing files.
- Have it work on a branch and open a pull request, then merge it yourself, as before.
- It can read `.env` on the server. Never paste keys into the chat, and don't ask it to change the
  live lock.
