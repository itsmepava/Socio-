# Socio

Socio helps clubs and other member organizations manage the people who have already joined them. An organization records a member and sends a secure invitation; the member activates their account, completes their profile, consent, and any club onboarding form, then sees all their Socio clubs and receives club updates in one place.

This is the first alpha for the fresh Socio repository. It is a locally hosted web demo for team testing. The intended service has a GCP-hosted backend and organization web workspaces for managing rosters, staff roles, attendance, events, and offers. Members will use native Android and iOS apps for their clubs, notifications, and digital passes, distributed through Google Play and Apple’s App Store. Those deployment and mobile apps are future stages, not included in this alpha. Payments are not processed by Socio.

## What this alpha includes

- Organization registration and platform-admin approval.
- Organization staff roles with scoped permissions.
- Member tiers for organizing existing club members.
- CSV roster import and individual member invitations.
- One-time invitation links; members can activate an existing account or create a password.
- Basic member profile, consent, and organization-specific onboarding questions.
- A private member gallery showing only the clubs the signed-in member belongs to.
- Member search for organization staff, with onboarding responses visible to authorized staff.
- Member requests to change membership tier, with organization review and an in-app decision notice.
- Member controls to request a membership upgrade or tier change and schedule or undo cancellation.
- Digital membership QR passes for staff-verified check-in, plus rotating organization QR codes for member self check-in.
- Attendance history scoped to each organization; QR passes are short-lived and single-use.
- A clickable Android/iPhone-style member app concept with clubs, updates, passes, and profile screens. Signed-in members see their own memberships and notifications; signed-out visitors see clearly labeled sample content.
- In-app notification inbox plus optional email delivery for club updates.
- Events, posters, and offers that members can open for full details.
- SMTP email delivery when configured; local email preview is available for development.
- Simulated payments are not part of the member experience; club admission and fees happen through the organization.

## Run locally on Windows

1. Install Python 3.12.
2. Extract this ZIP to a short folder path, for example `C:\Socio\alpha1`. Avoid deeply nested paths on Windows.
3. Open PowerShell in the extracted folder.
4. Run `python -m venv .venv`.
5. Run `Set-ExecutionPolicy -Scope Process Bypass` if PowerShell blocks the setup script.
6. Run `./start-local.ps1` and follow its prompts. The script installs requirements, prepares the local database, and starts Socio.
7. Open the local address printed in the terminal. Socio opens on a sign-in choice page for members, organization administrators, and platform staff. Keep that terminal window open while using the app.

For manual setup, install `requirements.txt`, set a private `SECRET_KEY`, run the Flask database setup command documented in `start-local.ps1`, create the first platform administrator, then start the Flask app. Never use sample credentials for a public server.

To scan a local demo QR code from a separate phone, set `PUBLIC_BASE_URL` in `.env` to the host computer’s private Wi-Fi address, such as `http://192.168.1.20:5001`, then start with `./start-local.ps1 -BindAddress 0.0.0.0`. Keep both devices on the same trusted Wi-Fi network and allow Windows Firewall access only on the private network if prompted. Do not port-forward this local demo to the internet. The GCP-hosted production environment will use its HTTPS URL.

## SMTP email setup

SMTP is optional for local work. Without SMTP, development mode can display confirmation or invitation links in the server terminal. To send real email, create a local `.env` file from `.env.example` and set:

- `MAIL_SERVER`
- `MAIL_PORT`
- `MAIL_USERNAME`
- `MAIL_PASSWORD`
- `MAIL_FROM`
- `MAIL_USE_TLS`
- `MAIL_USE_SSL`
- `PUBLIC_BASE_URL`
- `DEV_EMAIL_PREVIEW=0`
- `SECRET_KEY`

For Gmail, use an App Password after enabling 2-Step Verification; do not use your normal Gmail password. Never commit `.env`, SMTP passwords, or secret keys to GitHub. On Codespaces or another host, configure these as environment secrets on that host and restart the app. In-app notifications are saved in Socio’s database and work independently of SMTP; SMTP sends an accompanying email when configured.

For GitHub Codespaces, follow [the Codespaces SMTP setup guide](docs/CODESPACES_SMTP_SETUP.md). It covers secure secret storage, the Codespaces HTTPS URL required for invitation links, and the local-only sample-data setup step.

## Main member flow

1. An organization administrator records someone already admitted by the club, individually or through the roster CSV.
2. Socio emails a one-time invitation link to that member’s address.
3. The member opens the link, signs into an existing Socio account or chooses a password, and completes profile, consent, and the organization’s onboarding form.
4. The member’s private Socio home shows the clubs they belong to, along with club events, offers, and notifications.
5. Organization staff search their own roster and send club-wide or member-specific updates. Updates appear in the Socio inbox and are also emailed when SMTP is configured and the member can receive mail.
6. Members can request a different tier from a club they belong to. The organization approves or declines the request; admission criteria and any fees are handled directly by the club.

## Product destination

The local web app is a temporary way to validate workflows with the team. The full product is planned as a GCP-hosted service: organizations use web workspaces for administration, while members use native Android and iOS apps. Before that launch, the backend, data storage, authentication, privacy, operational monitoring, and mobile app store requirements must be completed and reviewed.

## App and QR demo

Open **About Socio** in the footer and choose **Preview the mobile app concept** to switch between the Android and iPhone mockups. The preview links to the live local QR flows when you are signed in: a member can open a one-time membership pass, while organization staff with member-management permission can open the club attendance desk. At the desk, staff can display the rotating club code for members to scan, view attendance history, and confirm a scanned member pass. A successful check-in is stored against the organization and its member. The static QR illustration shown to signed-out visitors is explicitly a non-valid sample.

For phone scanning on the same Wi-Fi, follow the local QR setup instructions above. The phone app visual is a clickable demo concept; it is not a native Android/iOS build or an app-store package.

## Notes for this alpha

Use test accounts and non-sensitive sample data. Invitation links are private credentials; share them only with the intended member. The app is not yet ready for public consumer use. Before GCP deployment, configure a supported database, persistent private file storage, backups, HTTPS, monitoring, account recovery, and complete an independent security and privacy review. Native Android and iOS apps for Google Play and Apple’s App Store are part of the intended consumer product and will be built after the core workflows are validated.
