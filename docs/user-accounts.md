# User accounts: sign-up, exchange keys and trading for other people

The dashboard can host other users. Each user signs up with an email and password, connects
their own Binance API key, and the agent trades the platform's strategies on **their** account,
sized from their own balance. You (the admin, the `AITRADER_WEB_USER` login) approve every
account and decide who may use real money.

> **Check the law first.** Holding other people's exchange keys and trading their money can
> make you an investment manager, broker or portfolio manager under your country's rules. That
> may need a licence or registration, client agreements, KYC/AML checks and tax reporting. In
> India, look at SEBI and FIU-IND rules and get advice from a lawyer before inviting anyone
> beyond yourself. Until then, keep sign-up closed and use accounts only for yourself or for
> testnet demos.

## Turn it on

1. In `.env`:

   ```
   AITRADER_KEYS_SECRET=<output of: openssl rand -hex 32>
   AITRADER_WEB_SIGNUP=open        # or closed: then create accounts with `aitrader users add`
   ```

   Back up `AITRADER_KEYS_SECRET` somewhere safe and never change it while users have keys. If
   you lose it, every user has to connect their keys again.
2. Restart the dashboard: `docker compose up -d web`.
3. Start the per-user trading service: `docker compose --profile users up -d users`.

## What a user does

1. **Sign up** at `/signup` with an email and a password of 12+ characters, and confirm they
   understand the risk. The account starts as *waiting for approval*.
2. After you approve it, they log in and see **My account**:
   - **Exchange keys.** They paste a Binance **testnet** key first. A real-account key is
     accepted only if Binance itself reports that withdrawals, transfers, margin and futures are
     **off**, spot trading is on, and the key is restricted to the server's IP. Keys that fail
     are not stored. Stored keys are encrypted and never shown again (only their last 4
     characters).
   - **Pause, resume and kill switch** for their own account. The kill switch sells everything
     the agent bought for them.
   - Their **equity curve, open positions and trade log**.
   - **Password change.**
3. **The live trading switch** (the "Real money" card). It is **off** for every account to start
   with. The page says why it can't be turned on yet. Turning it **on** needs all of these:
   - you allowed live for that account (Users page or `users live-on`);
   - the platform's live lock in `.env` is open (only you can do that, on the server);
   - the go/no-go gate passed and you approved at least one strategy (`golive approve`), and
     that strategy and the AI model haven't changed since;
   - a real-account key that passed the no-withdrawal check;
   - the user re-enters their password, sets a budget cap (the most USDT the agent may use)
     and types `I accept real-money risk`.

   Turning it **off** is one click and instant. No new real-money trades, the agent sells
   everything it bought on the real account, then the account goes back to the testnet. The
   account's kill switch, removing the real-account key, your "Stop live" and disabling the
   account all switch it off the same way.

   Every switch, on or off, refused attempts included, goes into the account log (shown on the
   user's page and on your Users page) and the event log, so Telegram tells you.

## What you do (admin)

**Users** in the dashboard menu, or the command line:

```bash
docker compose run --rm collector users list
docker compose run --rm collector users approve someone@example.com
docker compose run --rm collector users disable someone@example.com
docker compose run --rm collector users live-on someone@example.com     # allow real money
docker compose run --rm collector users live-off someone@example.com
docker compose run --rm collector users add you@example.com             # create an active account
```

- **Disable** sells what the agent bought for that account, then stops it. Disabled users can't
  log in.
- Your platform pause and kill switch (Trading page, Telegram, `trade kill`) stop **every** user.

## When does a user trade real money?

All of these must be true. Each one is checked before every order:

1. The platform's live lock in `.env` is open (`AITRADER_MODE=live`, `AITRADER_LIVE_UNLOCKED=true`,
   confirmation phrase). While it is closed, every user trades on the testnet, whatever they chose.
2. The go/no-go gate passed and you approved the strategy (`golive approve`); approval breaks
   if the strategy or the AI model changes.
3. You allowed live for that user.
4. The user turned their live switch on (password, budget cap, confirmation phrase).
5. Their real-account key passed the no-withdrawal check.

If you close the platform's live lock while accounts still hold live positions, the agent can't
sell them any more. Use the kill switch first, then close the lock.

The same risk engine applies to every user: 0.5% risk per trade (never above 1%), daily and
weekly loss caps that pause their account, and the exposure limits.

## Security notes

- Passwords are stored as salted scrypt hashes. Five wrong logins from one address lock it out
  for 15 minutes. Sign-up is limited to 3 accounts per address per hour.
- Each user only ever sees their own account. Users can't reach the admin pages (Overview,
  Trading, Books, Users).
- Serve the dashboard over HTTPS before inviting anyone (docs/dashboard.md), and set
  `AITRADER_WEB_SECURE_COOKIE=true`.
- Backups (`aitrader backup`, and the update script) include `accounts.db`. Keys inside stay
  encrypted, so keep the backup and `AITRADER_KEYS_SECRET` in different places.
