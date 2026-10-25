# Codex persona dual-login spike (multi-node-inference-router WP-0)

**Status: PASS 2026-10-25 (user disposition) — (a) measured pass; (b) and (c) waived by the user on longer-running field evidence (below).** Plan: `plans/multi-node-inference-router.md` WP-0. WP-4 (laptop codex cutover) is unblocked.

## Question

Can one ChatGPT account be logged in **independently** on two machines (separate `codex login --device-auth` on each, never a copied `auth.json`) and keep working on both across token refreshes? A copied `auth.json` shares one refresh-token family, and rotation-reuse detection could revoke both copies, so the copy case is out of scope by design.

## Setup (2026-10-24)

- Persona: **codex-a** (user choice at login time; the plan had named codex-b to keep the session auditor's profile out of the test — the user accepted that risk).
- host-a: existing `~/.codex-a` behind `codex-sidecar` → router `:4813`, codex `0.164.0`.
- host-b: codex `0.164.0` installed with `private-config/bin/codex-install.sh 0.164.0` (verified release package, `~/.local/lib/codex/current`); fresh `codex login --device-auth` by the user into `~/.codex-a`; `codex login status` → "Logged in using ChatGPT".
- The agent never reads `auth.json` (deny-enforced, even `stat`); refresh timestamps are read by the user on each machine with `jq -r .last_refresh ~/.codex-a/auth.json` (no token values).

Refresh baselines (`last_refresh`, read by the user):

- host-a: `2026-10-24T00:47:30.711492824Z` (before the laptop login).
- host-b: `2026-10-25T03:12:13.721574344Z` (= the laptop login; read with `grep -o '"last_refresh":"[^"]*"' ~/.codex-a/auth.json`; no jq on the laptop).

## Probes

Every probe is an authenticated model turn, never `/healthz`:

- host-a: `POST 127.0.0.1:4813/v1/messages` with `model: codex-a/gpt-5.6-luna`, prompt "Reply with exactly: PONG".
- host-b: `CODEX_HOME=~/.codex-a codex exec --skip-git-repo-check -s read-only -m gpt-5.6-luna "Reply with exactly: PONG"`.

## Results

| Step | When | host-a | host-b |
|---|---|---|---|
| (a) right after the laptop login | 2026-10-24T21:32-06:00 | 200, `PONG`, 2.27 s | `PONG`, exit 0 |
| (b) after the laptop's copy refreshed ≥1× | waived (user, 2026-10-25) | — | — |
| (c) after host-a's copy refreshed ≥1× | waived (user, 2026-10-25) | — | — |

Pass = all three rows answer on both nodes. Fail-safe: if host-a's login is revoked, the user re-logs it on host-a and the result is recorded as negative (WP-4 then does not cut the laptop over to a local codex sidecar; the chained route via the tower stays).

## Disposition (2026-10-25)

The user waived rows (b) and (c): codex-b has been logged in independently on a Windows machine and on host-a for months, across many refreshes, with no interference with the host-a profile. That is the same question (independent logins of one account on two machines surviving refresh rotation) observed over a far longer window than this spike's refresh wait. Result recorded as **pass on user disposition**, not on a measured (b)/(c). If a codex-a login on either node is ever revoked after a refresh, that falsifies this; the fail-safe above applies (re-login, and move the laptop's codex-a provider back to the chained route).
