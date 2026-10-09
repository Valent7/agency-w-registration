# Agency W — engineering guardrails

These rules are part of the working contract for changes to Agency W.

## 1. Do not fix one feature by breaking another
Before changing an existing page, worker, publisher, dialogue flow, statistics block, or button:
- preserve all existing user-facing functions unless the change explicitly requires otherwise;
- check the surrounding flow for regressions;
- do not silently remove or rename useful controls;
- prefer a small isolated change over replacing a whole working block.

## 2. Telegram polling must stay economical
This problem has already happened before and must not be reintroduced.

- Neona background polling target: **300 seconds**.
- Hard safety floor: **120 seconds**. Never return to 15–30 second polling.
- Telegram Scout remains a low-frequency task; do not couple it to Neona's dialogue loop.
- Do not deeply reread dormant private chats every cycle. A new incoming Telegram message updates the dialog timestamp and makes that dialog active again.
- Manual "catch up now" / explicit retry may perform a full scan because the owner asked for it.

## 3. FloodWait is a stop signal
When Telegram returns FloodWait:
- stop requests for that Telegram owner for the full wait interval plus a small safety buffer;
- do not keep checking the rest of that owner's chats during the wait;
- show the wait in human-readable hours/minutes;
- do not tell the user that Telegram blocked "sending" when the blocked operation was only reading/checking.

## 4. Manual owner intervention has priority
If the owner wrote to a contact manually after an incoming message:
- Neona must not answer that older incoming automatically;
- the UI should show that the owner is leading the dialogue instead of "waiting for reply".

## 5. Regression check before merging Telegram changes
At minimum verify:
- first message sending still works;
- incoming replies are detected;
- Neona can continue an existing dialogue;
- owner manual intervention prevents duplicate automatic replies;
- "Show conversation" still works when Telegram allows requests;
- FloodWait produces backoff instead of repeated requests;
- no polling interval was accidentally reset to 15–30 seconds.

These guardrails should be reviewed whenever Telegram, Neona, Neonia, Publisher, or shared Streamlit dialogue code is changed.


## 6. Mandatory post-change regression control
No change is considered finished immediately after code is committed or deployed.

After **every** modification to Agency W, perform a post-change control to make sure partners do not lose existing functionality.

Mandatory checks:
- open every page directly affected by the change;
- confirm that previously useful buttons, forms, tabs, counters, filters, media controls, publication controls and statistics are still present;
- verify that the changed function itself works;
- verify at least the neighboring functions that share the same page, state, worker, storage or API;
- check that existing partner workflows still make sense from start to finish;
- confirm that a fix did not silently rename, hide, disable or remove a working function;
- if Telegram/Neona/Neonia is involved, check both automation and manual controls;
- if content/publishing is involved, check text, image/video attachment, edit/delete, preview and send/publish controls;
- if statistics are involved, compare what the UI shows with the actual current batch/data;
- record any discovered regression and fix it before calling the task complete.

Product principle:
**Agency W must become more reliable after every change, not merely different.**
The goal is that partners feel confident using the Agency and can recommend it to others without fear that a familiar function disappeared after an update.
