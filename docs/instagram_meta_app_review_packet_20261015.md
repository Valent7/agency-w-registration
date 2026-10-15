# Agency W — Instagram App Review: submission preparation
**Working document · 15 October 2026 · NOT a submitted or approved Meta application**

This file is a review packet and evidence checklist, **not a change to authentication, webhooks, Meta permissions or the running service**. Never store passwords, access tokens, account secrets, reviewer credentials, or personal DM contents in GitHub.

## 1. Verified engineering context (as of PR #13)

- App/product: **Agency W-IG** (Instagram Login client ID `2153768211848484`); related Meta app **Agency W** (ID `1101869308929738`). Confirm in the Meta dashboard **which app actually owns each permission request** before submitting.
- Platform: **Instagram API with Instagram Login** for **professional (Business/Creator)** Instagram accounts. A linked Facebook Page is not required for this login variant.
- Currently requested: `instagram_business_basic`, `instagram_business_manage_messages`, `instagram_business_manage_comments`.
- Partner entry point: Agency W cabinet → Instagram connection → Instagram OAuth with user's consent → callback on the Agency W webhook service.
- Existing code: signed OAuth state, verified partner mapping/native Instagram user ID, webhook signature validation, no falsely "connected" UI if long-lived token exchange fails.
- At least one administrator-owned account has produced working Instagram Direct responses in a previous test; this **does not establish approval for outside partner accounts**.
- PR #13 merged; offline checks passed. **No evidence here of live partner onboarding or Meta approval**.
- Meta Advanced Access / App Review for third-party Instagram professional accounts: **not yet approved**.
- Project is under development and **not yet officially registered as a business**. Do not misstate legal status or submit invented registration data.

Related internal checklist: [instagram_partner_launch.md](./instagram_partner_launch.md).

## 2. Decisions to obtain in Meta UI before submission

For the **Agency W-IG** app/product, record a dated screenshot of the following *without showing secrets*:

1. **Instagram → API setup / Permissions and Features / App Review**: status of EACH of the three permissions (standard/test/advanced; requestable or blocked).
2. **App Review / Requirements**: whether Meta requests Business Verification, Access Verification, Tech Provider status, verified domain, or other prerequisites *for this app*. Copy exact wording rather than inferring.
3. **Business Portfolio**: whether an actual business is linked to the application, portfolio verification status, and whose name is listed.
4. **App Settings → Basic**: public app name, official contact, app domains, public Privacy Policy URL, Terms URL, data-deletion instructions URL, category, and icon. Check links as an unauthenticated user.
5. **Instagram Business Login**: permitted redirect URI, scopes and correct Instagram professional test account roles.
6. **Live-mode and submission availability**: capture actual access state; don't equate 'Ready for testing' with Advanced Access.

**Decision gate:** If Meta explicitly requires documentary verification of a legal business/Tech Provider and the project has no eligible verifiable entity, pause submission. Clarify which permitted legal route applies (e.g. legitimately registered sole proprietorship, other qualifying entity, or a verified partner provider); do not fabricate documents or assume an individual hobby project satisfies that gate.

**Official references**
- Instagram Platform / access levels: https://developers.facebook.com/documentation/instagram-platform/overview
- Instagram App Review: https://developers.facebook.com/documentation/instagram-platform/app-review
- Business Login: https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/business-login
- Business verification: https://developers.facebook.com/documentation/development/release/business-verification
- Tech Provider verification: https://developers.facebook.com/docs/development/release/tech-providers/
- Instagram messaging: https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/messaging-api

## 3. Draft app description for Meta (English; validate against live UI)

> Agency W is an in-development software service for Instagram professional-account owners. It helps an authorized account owner manage incoming Instagram direct messages and comments through an AI-assisted workflow called Neona. The account owner opens Instagram Login from their own Agency W cabinet and grants the permissions requested by Meta. Agency W binds the resulting Instagram account identifier to the correct signed-in Agency W owner so one customer's conversations are not attributed to another. The app uses data solely for the account owner's authorized communication workflow, with technical security checks and a way to request data deletion. Agency W does not request Instagram account passwords, does not send unsolicited bulk direct messages, and does not claim that third-party onboarding is live before Meta approves the required permissions.

**Important:** this is a draft description, not a factual warranty of all privacy controls. Verify against deployed functionality, privacy policy, and Meta policy before copying.

### Proposed justification: `instagram_business_basic`
> The permission is used in the account owner's consent-based Instagram Login flow to identify the Instagram professional account that is connected, display connection status to its owner, and associate incoming Instagram events with the correct Agency W account. Without the account's native identifier, we cannot securely separate partners' conversations or confirm which owner authorized the connection.

**Demo evidence:** owner signs into the Agency W cabinet; clicks Instagram connection; consents in Instagram; returns to cabinet; sees connection owned by the same owner. Record the account name/ID only as appropriate, with no tokens on screen.

### Proposed justification: `instagram_business_manage_messages`
> The account owner authorizes Agency W to receive incoming Instagram Direct messages to the connected professional account and send relevant responses on the owner's behalf, within Meta's applicable messaging rules and allowed conversation windows. Neona assists with these inbound conversations. We do not use this permission to initiate unsolicited outreach to arbitrary Instagram users.

**Demo evidence:** from an independent controlled Instagram test account, send a new DM to the authorized professional test account; show the corresponding event in Agency W; show the account's authorized response and receipt in Instagram. Show that the reply belongs to the correct owner and is not duplicated. Only claim automatic/manual sending behavior that is truly working on screen.

### Proposed justification: `instagram_business_manage_comments`
> With the professional account owner's consent, Agency W supports handling comments on that owner's Instagram content, including reading relevant comment events and preparing or posting permitted replies as implemented in the product. The purpose is to manage inbound audience interactions for that account, not to harvest unrelated users' data.

**Demo evidence:** leave a new comment under content on the controlled professional account; show the comment appearing in Agency W and, **if really implemented**, the review/send-reply action and the resulting reply visible in Instagram. Do not record a fictional capability.

### Messaging-policy constraint
Instagram messaging is not a general-purpose cold-DM API. Meta documents a standard messaging window that is often 24 hours following a user action. Record only responses that comply with the *current* rule and any applicable exception. Do not portray "scouting" or first contact to arbitrary users as available through this permission.

## 4. App Review demonstration: storyboard for a truthful video

Capture only controlled test accounts and mock/sample conversations. Hide usernames, tokens, secrets, and other partners' data.

| Sequence | Capture | Permission being demonstrated | Pass criteria |
|---|---|---|---|
| 1 | Open Agency W login and the Instagram section | Overview | Reviewer knows where to start |
| 2 | Authorized professional account consents to Instagram Login | basic | Account identity tied to correct cabinet |
| 3 | Return to cabinet: actual connection state | basic | No false success when token is missing |
| 4 | Test Instagram user writes a new DM | manage_messages | Inbound event appears under correct owner |
| 5 | Neona/owner replies in a permitted conversation | manage_messages | Exactly one appropriate reply appears in Instagram |
| 6 | Test user adds a comment under the account's post | manage_comments | Comment is visible in intended workflow |
| 7 | Show implemented comment handling/reply | manage_comments | On-screen evidence matches claimed behavior |
| 8 | If Meta permits, show denial/cancellation and safe return | Reliability | No HTTP 400 or phantom green connection |

**Reality gate:** when no authorized test account demonstrates one of these actions, describe the gap honestly and fix it before preparing a final submission. Do not edit video to imply functionality that does not exist.

## 5. Reviewer test instructions — template, NOT final submission

1. Go to **[verified public Agency W sign-in URL]**.
2. Use **[review-only test account credentials submitted via Meta's secure reviewer field, NOT GitHub]**.
3. Open **[exact verified Instagram menu label]** and select **[exact connection button label]**.
4. Use the **[Meta-authorized professional Instagram test account prepared for App Review]** and accept requested permissions.
5. Return to Agency W and observe the professional account connection state.
6. Using a separate controlled Instagram account, send the message **"Hello, review test"**; show its receipt and reply in Agency W and Instagram.
7. Add the comment **"Review test comment"** under **[controlled post URL]**; demonstrate the real comment flow.
8. For access problems contact **[verified app support email]**.

Do not publish passwords or real contacts. Meta reviewer access must be arranged in the actual submission.

## 6. Privacy / GDPR readiness audit — current blockers

Current server code defines **Privacy Policy**, **Terms** and **Data Deletion Instructions** handlers in `instagram_webhook.py`. This is useful, but **their content and external accessibility have not been verified as compliant**. Before submission and before processing public partner traffic:

- Identify the real data controller/owner and reachable contact; confirm the correct name/legal capacity rather than inventing a company.
- Confirm the public HTTPS addresses for Privacy, Terms and Data Deletion load without a login or Render timeout.
- Map actual data fields and purposes (Instagram account ID, inbound messages/comments, event metadata, AI prompts/responses), storage locations, third parties/processors (including infrastructure and AI services), and actual transfer mechanisms if data leaves the EEA.
- Specify lawful basis, recipients, retention periods or precise criteria per data category, and an implementable deletion and access-request workflow. The current phrase "as long as reasonably necessary" is not a specific retention schedule.
- Verify notices, user rights, supervisory authority complaint information, security and incident-response process, partner-controller roles and processors' agreements as applicable under GDPR.
- Confirm if automated replies are sent and explain AI involvement accurately. Partner authorization alone is not a universal substitute for the rights of people who message or comment.
- Validate deletion in storage and backups where applicable; do not claim an automated deletion endpoint exists without testing it.
- Confirm no data of other partners is exposed in screenshots or diagnostic logs.

**No legal conclusion is asserted here:** privacy policy and applicable German/EU requirements need a review against the real deployment.

## 7. Evidence and release gates

**Technical:** OAuth and token issued for a fresh unrelated professional account (not an app role); correct native account identity and owner; webhook delivery; replies within allowed messaging windows; comment flow; no duplicate sending; failure messages and disconnect/deletion path.

**Meta:** necessary permissions actually approved for Advanced Access; any specific Tech Provider / verification requirement completed legitimately; submitted reviewer evidence matches live app.

**Business/legal:** verified status appropriate to the entity required by Meta; public truthful policies, support contact and personal-data handling.

**Product:** only after all three gates pass, enable partner-wide "Instagram available" messaging. All existing integrations stay unchanged until a separately reviewed change is approved.

## 8. Immediate next action (owner screenshot)

Open **Meta for Developers → My Apps → Agency W-IG → Instagram → App Review / Permissions and Features**, and capture the page that lists access levels and any request-blocking banner. Then open the **Business Verification / Tech Provider** requirement page indicated by Meta. Share cropped screenshots without secrets. The next decision is driven by Meta's *actual* blocker, not speculation.

**Status on 15 October 2026:** preparation draft complete; Meta console evidence, legal verification route, publicly reachable policy pages, and real end-to-end reviewer videos remain pending. **Do not press Submit yet.**
