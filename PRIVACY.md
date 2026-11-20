# Privacy Policy

**Last updated:** 2026-11-20

This Privacy Policy describes how the **botalert** Discord bot and its
companion dashboard (the "Service") handle information. By using the
Service, you acknowledge this Policy.

## 1. What We Collect

The Service is a single-operator personal tool. In normal operation it
collects and stores:

- **Watch list entries** — the product page URLs, labels, stock
  indicators, and recheck cadences that the operator adds. These are
  configured by the operator and are not personal data of any third party.
- **Operational state** — each watched product's last-known stock status,
  consecutive-check-failure counts, and the id of the bot's control
  message. This state is persisted to a local state file so the Service
  survives restarts.
- **Discord user ids** — when you interact with the bot's management
  buttons, menus, or modals, Discord provides the bot with your user id.
  The bot uses your user id solely to verify that you are the designated
  owner before performing any management action. It does not read, store,
  or log the content of your interactions.

The Service does **not**:

- read or store the text of Discord messages;
- collect or store any other users' personal data;
- use analytics, advertising, tracking pixels, or cookies; or
- share any information with any third party other than Discord itself, as
  described below.

## 2. How Information Is Processed

- **Stock checks.** The Service fetches the pages in the watch list over
  HTTPS on a scheduled cadence and inspects the returned page text to
  determine stock status. It does not log in to, or send data to, the
  fetched sites. Your use of the pages you add to the watch list is
  subject to each site's own terms and privacy policy.
- **Discord.** All interaction with Discord occurs through the Discord
  Gateway and Discord's standard bot/webhook APIs, using the credentials
  provided at setup. Discord may process information in connection with
  providing those APIs under [Discord's Privacy
  Policy](https://discord.com/legal/privacy).

## 3. Security

- Secrets (bot token, dashboard token, webhook URL) are held in
  environment configuration only, never in the state file, the dashboard,
  or logs.
- Every management API endpoint on the dashboard requires a shared secret
  token, compared in constant time before any state is accessed.
- All management interactions through Discord are authorized to the
  designated owner's user id; all other users receive no action and no
  state access.

## 4. Data Retention

Watch list entries and operational state are retained until the operator
deletes them, or until the Service is stopped. Corrupt state files are
renamed and preserved, never silently deleted. No other records are kept.

## 5. Your Rights

The Service does not process personal data on your behalf beyond what is
described above. If you believe we are holding information you believe you
are entitled to access, correct, or delete, you may contact the operator
of the Service (see below).

## 6. Children's Privacy

The Service is not directed to children under 13, and we do not knowingly
collect any personal information from children.

## 7. Changes to This Policy

We may update this Privacy Policy from time to time. The "last updated"
date at the top of this page reflects the most recent revision. Material
changes will be posted to this page before they take effect.

## 8. Contact

Questions about this Privacy Policy may be directed to the operator of the
Service via the Discord account or repository through which the Service is
provided.
