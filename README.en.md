# Discord Bot

**English** · [繁體中文](README.md)

## Overview

This is a Discord bot project written with `discord.py` and `aiohttp`.

Supported features:

- `/ping`: check bot latency
- `/choice`: randomly select one option from user input
- `/quotes`: show random quotes or jokes with interaction buttons
- `/steal emoji:<emoji>`: download a Discord emoji reference or mention directly from the CDN and add it to the current guild as a custom emoji; supports multiple emoji in one call and replies with a friendly message when a source exceeds the 256 KB Discord limit
- `/weather <city>`: query real-time weather for Taiwan cities
- `/bus`: query Taiwan bus arrivals through dropdown menus
- `/server_info`: display detailed server information
- `/welcome_active`: set up a server welcome message (welcome channel required; rules and role channels optional)
- `/welcome_inactive`: disable the server welcome message feature
- `/reaction_roles_create`: post a reaction-role message with emoji + role pairs (multiple messages per server); members gain the role by reacting and lose it when un-reacting, and reactions that grant nothing are removed automatically. Requires **Manage Server** permission
- `/reaction_roles_edit`: edit a posted reaction-role message interactively — pick it from a dropdown menu, then edit the message text or emoji/role pairs in pop-up inputs, or toggle strict mode with one click. Requires **Manage Server** permission
- `/music_join`: join the voice channel where the user is currently connected
- `/music_leave`: leave the current voice channel
- `/music_play <query>`: search and play music; automatically queues the track if something is already playing (accepts keywords, or a YouTube/SoundCloud URL)
- `/music_play_next <query>`: play next (jump the queue), search a track and insert it at the front of the queue
- `/music_queue`: show the current server's playback queue (now playing + upcoming tracks)
- `/music_queue_clear`: clear the current playback queue (does not affect the currently playing track)
- `/music_pause`: pause the currently playing track
- `/music_resume`: resume a paused track
- `/music_skip`: skip the current track and play the next one in the queue
- `/music_seek <time>`: seek to a position (seconds, `mm:ss`, `hh:mm:ss`, or relative `+10` / `-15`)
- `/music_stop`: stop playback and clear the queue (requires a group vote)
- `/music_set_channel [channel]`: restrict music commands to a single text channel; run without arguments to lift the restriction. Requires **Manage Server** permission
- `/music_node_status`: view the connection status of all registered Lavalink nodes (URI, status, Session ID, and connected guild count)
- `/auto_reply_add`: add a keyword auto-reply rule (rule name, trigger keyword, bot reply text, active scope). Requires **Manage Server** permission
- `/auto_reply_edit`: edit an auto-reply rule (edit keywords / reply text in modals, leaving a field empty deletes that entry; can also pause or resume the whole rule). Requires **Manage Server** permission
- `/auto_reply_list`: list all auto-reply rules (one rule per page, with paging: rule name, status, keywords, reply text, active scope). Requires **Manage Server** permission
- `/auto_reply_remove`: delete an auto-reply rule (pick from a menu of the currently active rules, with a confirmation step). Requires **Manage Server** permission
- `/tts`: convert text to speech using Google Text-to-Speech (gTTS) and output as an MP3 audio file (supports custom filename and language code, defaults to English `en`)

### Requester control lock

The five commands `/music_pause`, `/music_resume`, `/music_skip`, `/music_stop`, and `/music_seek` are protected by a requester control lock:

- While the current track's requester stays in the bot's voice channel, only they can control that track; once the requester leaves the voice channel, anyone may control it.
- **Pause/Resume**: when used by someone else, the bot posts a public request in the original text channel with "✅ Allow / ❌ Deny" buttons. Only the requester can press them; the buttons are one-shot (disabled once decided) and unanswered requests are denied after 30 seconds.
- **Stop**: any use starts a group vote with agree/disagree buttons showing live tallies; a single disagreement vetoes the request, and only unanimous agreement stops playback. Buttons are valid for 30 seconds. If the channel only has one human, the command executes directly without a vote. Once vetoed, no new stop vote can be started for that track until the next one begins.
- **Rejection lock**: once a request for a track is denied (or times out) — including pause/resume button requests and a vetoed (or timed-out-as-denied) stop vote — no new request or vote can be started for that track until the next one starts playing; the lock clears when the track changes.

The project also includes `cogs/web_server.py`, which starts a Flask web server in the background (backing a `templates/index.html` dashboard) that exposes bot status APIs (guild count, latency, uptime, etc.) for cloud deployments that require a keep-alive endpoint.

## Requirements

Install dependencies first:

```bash
pip install -r requirements.txt
```

`requirements.txt` contains:

- discord.py
- python-dotenv
- Flask
- waitress
- certifi
- aiohttp
- wavelink
- gTTS

`wavelink` is already included in `requirements.txt`, so running `pip install -r requirements.txt` installs it together with all other dependencies. The music features also require a reachable Lavalink node configured via `LAVALINK_URI` and `LAVALINK_PASSWORD`. If Lavalink is unavailable, the bot still starts normally but music commands will not work.

The web dashboard runs on the production WSGI server `waitress` (also listed in `requirements.txt`), listening on `0.0.0.0` and the `PORT` environment variable (defaults to `20198` when unset).

## Environment Variables

Create a `.env` file in the project root, or set these variables in your environment:

```env
DISCORD_TOKEN=your Discord bot token
CWA_API_KEY=your Central Weather Administration API key
TDX_CLIENT_ID=your TDX Client ID
TDX_CLIENT_SECRET=your TDX Client Secret
DISCORD_OWNER_ID=your Discord user ID
LAVALINK_URI=your Lavalink node URL
LAVALINK_PASSWORD=your Lavalink node password
EMPTY_VOICE_CHANNEL_TIMEOUT=seconds of an empty voice channel before auto-leaving (optional, default 60)
LAVALINK_CONNECT_TIMEOUT=seconds before the Lavalink connection attempt times out (optional, default 15)
NODE_HEALTH_CHECK_INTERVAL=interval in seconds between Lavalink node health checks (optional, default 30)
LAVALINK_RECONNECT_COOLDOWN=cooldown in seconds before an automatic reconnect when the node is not in the pool (optional, default 60)
```

`TDX_CLIENT_ID` and `TDX_CLIENT_SECRET` are required for `/bus`. `DISCORD_OWNER_ID` is optional and is used to receive bot error notifications. If it is not set, error notifications will be skipped.
`OWNER_ID` can also be used as an alternative name for `DISCORD_OWNER_ID`. `LAVALINK_URI` and `LAVALINK_PASSWORD` configure the Lavalink connection for the music features. If either is missing, the bot skips the Lavalink connection.
`EMPTY_VOICE_CHANNEL_TIMEOUT` is optional: how many seconds a voice channel can have no human members (bot only) before the bot auto-leaves and clears its queue. Defaults to 60 seconds.
`LAVALINK_CONNECT_TIMEOUT` is optional: timeout in seconds for the initial Lavalink connection attempt; if exceeded, the owner receives a DM notification. Defaults to 15 seconds.
`NODE_HEALTH_CHECK_INTERVAL` is optional: interval in seconds between periodic Lavalink node health checks; the owner is notified once when a node goes offline and the flag resets upon recovery. Defaults to 30 seconds.
`LAVALINK_RECONNECT_COOLDOWN` is optional: cooldown in seconds before the health check retries the connection when no node is registered in the pool (initial connection failed, or the node was ejected). Defaults to 60 seconds.
When the Lavalink connection fails, the bot first probes the node's `/version` endpoint and turns the real cause that wavelink swallows (for example `LAVALINK_URI` using `https://` against a plain-HTTP node, a wrong `LAVALINK_PASSWORD`, or an offline node) into an actionable Chinese message and DMs it to `DISCORD_OWNER_ID`; the message never contains the password, and only one DM is sent per outage until the connection recovers.

To enable the music features, ensure `wavelink` is installed (already included in `requirements.txt`) and set up a reachable Lavalink node with `LAVALINK_URI` and `LAVALINK_PASSWORD`.

## Run

```bash
python main.py
```

When launched, the bot starts the Flask background server from `cogs/web_server.py`, checks required environment variables, and then connects to Discord with `bot.run(TOKEN)`.

## Commands

- `/ping`: reply with current bot latency
- `/choice options:<text>`: enter options separated by spaces and the bot chooses one randomly
- `/quotes`: show buttons for random quote or joke
- `/steal emoji:<emoji string or reference>`: parse a Discord emoji string, download the asset from the CDN, and create it as a custom emoji in the current server; supports multiple emoji at once and prevents failed uploads from exposing raw HTTP error codes by returning a friendly message when the asset is too large
- `/weather city:<city name or English name>`: query weather, supports mappings like `臺北`, `Taichung`, `Matsu`
- `/bus`: select a city, enter a bus route, choose a stop from a dropdown menu, and view real-time arrival information
- `/server_info`: display the current server's details
- `/welcome_active welcome_channel:<channel> [rules_channel:<channel>] [role_channel:<channel>]`: enable welcome messages with a required welcome channel and optional rules/role channels. Requires **Manage Server** permission.
- `/welcome_inactive`: disable welcome messages for this server. Requires **Manage Server** permission.
- `/reaction_roles_create message:<text> pairs:<emoji role emoji role ...> [channel:<channel>] [strict:<true/false>]`: post a reaction-role message to the role channel configured via `/welcome_active` (or the given channel). Emoji + role pairs are space-separated, one-to-one, up to 20 pairs; the bot reacts with every emoji first, and members gain or lose the matching role as they add or remove reactions. Each server keeps up to 10 reaction-role messages at once. `strict` is on by default and auto-removes reactions that grant no role (the bot needs **Manage Messages** in that channel). Requires **Manage Server** permission.
- `/reaction_roles_edit`: edit a posted reaction-role message. Pick the message from a dropdown menu (options show a summary of its emoji/role pairs), then press "Edit message text" or "Edit emoji pairs" to open pop-up inputs pre-filled with the current content — modify and submit. Emoji pairs must be the complete new set (`emoji role ...`, space-separated; a role can be its name, numeric ID or a <@&ID> mention); changing pairs also syncs the reactions on the message (emojis no longer used are cleared along with members' reactions, new ones are reacted by the bot, pairs still in use are kept). The "Strict mode" button toggles it on/off in one click. Everything happens in the same message (no spam when editing repeatedly); buttons expire after 120 seconds of inactivity. Requires **Manage Server** permission.
- `/music_join`: join the user's current voice channel. Requires `wavelink` and a configured Lavalink node.
- `/music_leave`: leave the current voice channel, and clear that server's playback queue.
- `/music_play query:<keywords or URL>`: search and play music; auto-joins your voice channel if the bot isn't connected yet. If something is already playing (or paused), the new track is appended to the FIFO queue instead of replacing it.
- `/music_play_next query:<keywords or URL>`: play next / jump the queue. Same search as `/music_play`, but the track is inserted at the front of the queue and plays right after the current one.
- `/music_queue`: show what's currently playing plus the upcoming tracks in the queue (up to the first 10).
- `/music_queue_clear`: clear the current server's queue. The currently playing track is not affected.
- `/music_pause`: pause the currently playing track. Protected by the requester control lock described above.
- `/music_resume`: resume a paused track. Protected by the requester control lock described above.
- `/music_skip`: skip the current track and play the next one in the queue. Protected by the requester control lock described above.
- `/music_stop`: stop playback and clear the queue (starts a group vote). Protected by the requester control lock described above.
- `/music_seek time:<90 | 1:30 | +10 | -15>`: seek to an absolute position (seconds, `mm:ss`, `hh:mm:ss`) or a relative offset (`+10` / `-15`). Protected by the requester control lock described above.
- `/music_set_channel [channel]`: restrict music commands to a single text channel; run without arguments to lift the restriction. Requires **Manage Server** permission.
- `/music_node_status`: view the connection status of all registered Lavalink nodes (URL, connection state, Session ID, connected guild count). Ephemeral (only visible to you).
- `/auto_reply_add keyword:<keyword> reply:<reply> [name:<rule name>] [channel:<channel>]`: add an auto-reply rule. When a member's message **contains** the keyword (case-insensitive, extra whitespace ignored), the bot quotes that message and auto-replies; without `channel` the rule applies to the whole server, and without `name` the keyword is used as the rule name. Up to 10 rules per server. Requires **Manage Server** permission.
- `/auto_reply_edit`: edit an auto-reply rule. After submission the bot builds a dropdown from the **rule names** of all current rules (older rules fall back to showing their trigger keyword); selecting one shows four buttons: "Edit keywords", "Edit reply text", "⏸️ Pause rule" (becomes "▶️ Resume rule" while paused), and "Cancel". Both edit modals hold 5 input boxes per page (Discord's limit), pre-filled with the current values, and **leaving a box empty deletes that entry**; saving **edits that same message in place** (it never keeps posting new messages) and always leaves an "Edit entries 6-10" button on it (and "Back to entries 1-5" after editing the second page) so you can jump between pages whenever you want. When a rule has more than one reply it automatically switches to **random reply mode** (picks one reply at random on every trigger). Requires **Manage Server** permission.
- `/auto_reply_list`: a quick way to review the current rules. **One rule per page** (paged with "◀️ Previous / Next ▶️"), showing the rule name, status, all keywords, reply text, and active scope; a single reply longer than 300 characters is truncated, and if the content still does not fit it is marked as shortened with a hint to use `/auto_reply_edit`. Tells you to add one with `/auto_reply_add` when there is no rule yet. Requires **Manage Server** permission.
- `/auto_reply_remove`: delete an auto-reply rule. After submission the bot builds a dropdown from the **currently active** trigger keywords; selecting one shows a confirmation message with "Confirm delete / Cancel" buttons that only the person who ran the command can press. Requires **Manage Server** permission.
- `/tts text:<text> [file_name:<filename>] [language:<language_code>]`: convert input text to speech using Google Text-to-Speech (gTTS) and send the generated MP3 file. Defaults to English (`en`) if language is omitted, and defaults to the first 10 characters of the text if filename is omitted.

## Reaction Roles Feature

`/reaction_roles_create` posts a reaction-role message: after sending it, the bot reacts with every configured emoji first, so members can gain the matching role by reacting and lose it when the reaction is removed. Each server can keep multiple reaction-role messages at once (up to 10; the command asks you to remove old ones when full). Settings are stored in `reaction_roles.json` and persist across restarts (the old single-message format is migrated automatically).

`/reaction_roles_edit` edits a posted reaction-role message the same way `/auto_reply_edit` works: pick the message from a dropdown menu, press "Edit message text" or "Edit emoji pairs" to open pop-up inputs pre-filled with the current content, or press the strict button to toggle it in one click; results are updated in place on the same message (no spam when editing repeatedly), and buttons expire after 120 seconds of inactivity (just run the command again). Changing pairs also syncs the reactions on the message: emojis no longer used are cleared together with the members' reactions on them (requires **Manage Messages**), emojis still in use are kept as-is so members do not have to react again; settings and reactions are rolled back if saving fails.

**Only valid emojis (strict mode)**: `strict` is on by default. When a member reacts with an emoji that maps to no role, the bot removes that reaction so only role-granting emojis stay. Removing someone else's reaction requires **Manage Messages** in that channel; the command refuses to send and lists the missing permission if the bot lacks it. Missing permissions, a deleted message, or other failures are only logged in the console and never break the other features. Pass `strict: false` to keep free-form reactions (e.g. to use the message as a comment board). Reaction-role messages stored before this feature (no `strict` key in the settings file) are treated as strict.

**Reaction rate limit**: per (member, emoji) pair, a grant/revoke takes effect at most once every 5 seconds; repeated clicks/un-reacts on the same emoji during the cooldown are silently ignored (other emojis and other members are unaffected), preventing role spam through rapid toggling. Invalid-emoji cleanup shares the same cooldown, so spamming one invalid emoji only triggers a single removal. Cooldown state is stored in `reaction_roles_cooldowns.json` and survives restarts.

How the `pairs` parameter is parsed:

- **Space-separated auto-splitting**: the string is split on whitespace into tokens, then consumed two at a time (emoji → role → emoji → role ...), e.g. `🎉 @Mods 🎮 @Gamer`.
- Both Unicode emoji and custom emoji codes (`<:name:id>`, `<a:name:id>`, with a real 13-20 digit ID) are accepted; plain text (e.g. English words) is never mistaken for an emoji, and CJK role names are safe to use.
- Roles can be given three ways: an @mention (`<@&id>`), a numeric ID, or a role name (case-insensitive). Since names cannot contain spaces, **use a mention or ID for names with spaces**.
- Each emoji may map to only one role; a single message supports up to 20 pairs (Discord's reaction limit).
- On a parse failure the parser stops at the first unresolvable token and replies with a Chinese error message (pair number and reason: unrecognized emoji, duplicate emoji, missing role, role not found, or over the limit); successfully parsed pairs up to that point are still kept for the remaining validation. No message is sent if any error exists.

## Auto Reply Feature

`/auto_reply_add` creates a "keyword auto-reply" rule, which is built from four elements:

- **Rule name**: optional; it is how `/auto_reply_edit` and `/auto_reply_remove` identify rules in their dropdown menus (up to 40 characters, defaults to the keyword when omitted).
- **Trigger keywords**: a member's message fires the rule when it **contains** the keyword (case-insensitive, compared after collapsing repeated whitespace), up to 60 characters each. One rule can hold several keywords (up to 10), and **hitting any one of them triggers the rule**; more can be added later with `/auto_reply_edit`.
- **Bot reply text**: when triggered, the bot quotes the triggering message and replies with this text (it never @-mentions anyone), up to 1500 characters.
- **Active scope**: a single text channel, or the whole server (when `channel` is omitted).

Each server keeps at most **10 rules** at a time (the command asks you to remove old ones when full); a single message only ever triggers the first matching rule; the same member cannot retrigger the same rule within 3 seconds (anti-spam). Settings are stored in `auto_reply.json` and persist across restarts (the old single-keyword / single-reply format is migrated automatically into keyword and reply lists).

**Random reply mode**: a rule can hold several replies (up to 10); once a rule has more than one reply, every trigger picks one of them **at random**.

`/auto_reply_edit` edits an existing rule: after the command is submitted, the bot collects the **rule names** of all current rules into a dropdown (older rules fall back to showing their trigger keyword). Once a rule is selected, the bot shows its details plus four buttons — "Edit keywords", "Edit reply text", "⏸️ Pause rule", and "Cancel":

- **Edit keywords** / **Edit reply text**: open a modal where each input box maps to one entry of the list and is pre-filled with the current value; **leaving a box empty deletes that entry**, typing into a blank box adds a new one, and the whole list is saved in one go (keeping the whitespace collapsing, the 60 / 1500 character limits, and the 10-entry cap; duplicate keywords are skipped automatically).
- **Paging**: Discord allows at most 5 input boxes per modal, so entries 1–5 land on the first page and 6–10 on the second. The confirmation message always carries an "Edit entries 6-10" button regardless of how many entries exist (so a list of exactly 5 entries can still grow), and the second page offers a "Back to entries 1-5" button. **Pages you never edited stay completely untouched** — editing entries 6–10 keeps entries 1–5 exactly as they were, in the same order.
- **Everything empty asks first**: if **every** keyword or reply box is left empty, the bot does not silently store an empty list — it shows a "Do you want to delete this rule entirely?" confirmation (with "🗑️ Confirm delete / Cancel" buttons; cancelling leaves the rule completely untouched).
- **⏸️ Pause rule**: pausing deletes nothing (keywords, reply text, and active scope are all kept) and only stops the rule from replying for now; the button turns into "▶️ Resume rule" and the rule details in the main message update their status too.
- **One message for the whole flow**: picking a rule, editing keywords and reply text, and pausing all happen on the single `/auto_reply_edit` message; every save rewrites that message in place and swaps in the buttons for continuing, so editing never floods the channel. Only if the original message can no longer be edited (e.g. an ephemeral message older than 15 minutes) does the bot send a separate one.
- When replies are deleted down to a single one, the rule automatically returns to fixed-reply mode (no more random picking).

Edit interactions can only be pressed by the person who ran the command, and the buttons can be used consecutively (e.g. change the keywords first, then the reply); every press resets the countdown, so they only expire after **120 seconds without any interaction**. If saving fails, the change is rolled back and reported to the developer.

`/auto_reply_list` is a quick way to review the current setup: **one rule per page**, paged with "◀️ Previous / Next ▶️", where each page shows that rule's name, status, all keywords, reply text, and active scope in full. Because every rule gets its own page instead of sharing one message, a single reply can be displayed up to 300 characters; if it really exceeds the embed character limit, the text is truncated and marked as shortened.

Paused rules are not detected by `on_message`, but the other rules are unaffected, and a message still triggers at most the first matching **enabled** rule.

After `/auto_reply_remove` is submitted, the bot collects all **active** rules into a dropdown for an admin to pick from; selecting one first sends a confirmation message (showing that rule's name, keywords, reply text, and active scope) with "🗑️ Confirm delete / Cancel" buttons, and the rule is only really deleted on confirm. Only the person who ran the command can press these buttons.

## Welcome Message Feature

When enabled, the bot sends an embed to the configured welcome channel whenever a new member joins. The embed includes:

- The new member's avatar and @mention
- Join timestamp and current member count
- A link to the rules channel (if configured)
- A link to the role pickup channel (if configured)

Welcome settings are saved to `welcome_settings.json` and persist across restarts.

> **Important**: Before using the welcome feature, go to the [Discord Developer Portal](https://discord.com/developers/applications) → **Bot** → **Privileged Gateway Intents** and enable **Server Members Intent**, otherwise the `on_member_join` event will not fire.

The bot also enables the `Message Content Intent` and voice-state intents for the current commands and the `/music_join`, `/music_leave`, and `/music_play` voice features. Enable the corresponding intents in the Discord Developer Portal as needed.

## Notes

- `/weather` calls the Taiwan Central Weather Administration API with TLS certificate verification always enabled (via the shared SSL context in `cogs/tls.py`, compatible with legacy government CA chains); if certificate verification fails, it reports a connection error instead of retrying with verification disabled.
- `/bus` calls the Taiwan TDX API for route stops and real-time arrival estimates. Bus query messages are ephemeral, so only the user who started the query can see them.
- `/bus` handles unknown route numbers with a clear not-found message. Unexpected errors trigger an owner DM when `DISCORD_OWNER_ID` is configured.
- `/music_join`, `/music_leave`, `/music_play`, `/music_play_next`, `/music_queue`, `/music_queue_clear`, and `/music_node_status` use Wavelink and require a reachable Lavalink node configured with `LAVALINK_URI` and `LAVALINK_PASSWORD`. The music Cog is loaded without stopping the bot when Wavelink or Lavalink is unavailable.
- On startup, the Music Cog connects to Lavalink in the background within `LAVALINK_CONNECT_TIMEOUT` seconds (default 15) without blocking other bot features. Once connected, a health-check loop runs every `NODE_HEALTH_CHECK_INTERVAL` seconds (default 30), DMing the owner once when a node goes offline and resetting the flag upon recovery to avoid repeated notifications.
- When the Lavalink connection fails (timeout, TLS handshake failure, wrong password, or wavelink's `Pool.connect` silently failing to register the node), the bot probes the node's `/version` endpoint for a diagnosis, discards the failed node (its backoff-retry websocket and aiohttp session), and then DMs `DISCORD_OWNER_ID` an error whose message is no longer empty — one DM per outage. If the pool is empty, the health check automatically reconnects after the `LAVALINK_RECONNECT_COOLDOWN` cooldown (default 60 seconds), so no restart is required.
- The playback queue is a per-server FIFO queue, implemented as `player.song_queue` (a `collections.deque`) in `cogs/music.py`. When a track ends (finishes, is skipped, or errors out), wavelink fires `on_wavelink_track_end`; the listener pops the next track off the front of the queue and plays it automatically, and sends a one-time notice once the queue is empty. `player.autoplay` is set to `disabled` so this listener fully owns the "advance to next track" logic instead of racing with wavelink's built-in autoplay.
- If a voice channel is left with only the bot (no human members) for more than `EMPTY_VOICE_CHANNEL_TIMEOUT` seconds (default 60), the bot automatically leaves and clears its queue, posting a notice to the text channel where it was last used. This listens to discord.py's `on_voice_state_update` event, checking the bot's channel every time someone joins, leaves, or switches channels; the countdown only starts once no humans remain, and is cancelled immediately if someone comes back, to avoid false positives from brief disconnects/reconnects.
- `/music_play` reports search failures (source unreachable, anti-bot blocking, etc.) immediately. If a track is accepted but later fails to load in the background (e.g. YouTube requiring login, region restrictions, or a broken stream link on a public node), the bot reports the failure to the text channel where the command was last used via the `on_wavelink_track_exception` listener, instead of only logging it. Public Lavalink node instability is a known risk here; self-hosting a node is planned for later weeks.
- `/steal` parses Discord emoji references such as `<:pepe_smile:123456789>` and `<a:cat:456789>`, builds the correct CDN path from the extracted `emoji_id`, and creates custom emoji in the current guild via `guild.create_custom_emoji()`. When a source exceeds Discord's 256 KB limit or cannot be fetched, it returns clear user-facing text instead of leaking the raw API error payload.
- `/tts` module converts text into MP3 audio via `gTTS` (Google Text-to-Speech), ported directly from `tts.py` (defaults to English `en` if language is omitted, and uses the first 10 characters of the text if filename is omitted); generation runs in an asynchronous thread to avoid blocking the event loop.
- The bot syncs global slash commands in `setup_hook`. If that fails, it retries in `on_ready` as a fallback.
- `cogs/web_server.py` runs a background Flask web service serving a status dashboard (`templates/index.html`) and the `/api/bot-stats` and `/api/uptime` endpoints.

## Tips

- To add a command, create or update a Cog under `cogs/`, then add its module path to `INITIAL_EXTENSIONS` in `main.py`.
- For cloud deployment, ensure the `PORT` environment variable or default port `20198` is accessible. The dashboard runs on the `waitress` production WSGI server (included in `requirements.txt`).
