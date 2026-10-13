# Socio Alpha 1

This is the first alpha release for the fresh Socio repository. It resets the product direction around organizations adding people who are already admitted members, rather than members applying through a public club marketplace.

## Included

- Organization registration and platform-admin approval.
- Scoped organization and platform staff roles.
- Organization member tiers and configurable onboarding forms.
- Individual member entry and CSV roster import.
- Secure, one-time member invitations delivered through SMTP when configured.
- Member activation with profile details, consent, and organization form responses.
- Private member home showing only organizations the signed-in member belongs to.
- Organization member search and member-specific or club-wide updates.
- Member tier-change requests that the organization can approve or decline; fees and eligibility stay with the club.
- Persistent in-app notification inbox, with optional parallel email notifications through SMTP.
- Events with optional posters and offers with full detail pages.
- Member admission and fee collection remain managed by the organization; Socio does not process member payments in this alpha.
- Local Windows setup instructions and SMTP environment configuration guidance.
- A focused sign-in landing page for member, organization, and platform accounts; signed-out visits now open there by default.
- Optional “stay signed in” selection. Without it, the login cookie lasts for the browser session.
- Consistent card and form spacing, and a simpler top navigation with About Socio available in the footer.
- Fixed the team roster permission display that caused a server error on the organization team page.
- Member upgrade/tier-change requests and end-of-period cancellation or cancellation undo.
- Digital member QR passes that organization staff can confirm, and expiring club QR codes for member self check-in.
- Organization-scoped attendance history. QR credentials are short-lived and one-time where presented by a member.
- Clickable Android/iPhone-style mobile preview with Clubs, Updates, Passes, and Profile tabs. Signed-in members see their own club and notification data; visitors see content labeled as sample-only.
- Mobile preview links to the working local QR flows: member pass, organization check-in desk, two-way attendance capture, and the attendance history panel.
- Polished phone mockup responsive layout with reduced-motion support.
- Phone navigation stays visible in a swipeable row beneath the logo, while the light/dark mode control remains in the header.
- Fixed mobile feature cards on the About page so text stays in a single readable column without overlap.
- Corrected the onboarding-question list inset and refreshed the static asset cache version so updated styling is picked up.

## Roadmap

Alpha 1 is a locally hosted web demo for team testing. The intended full service will use a GCP-hosted backend and web workspaces for organization administrators, alongside native Android and iOS apps for members. GCP deployment and store-ready mobile apps are not part of this alpha.

## Testing notes

- This is a local development alpha. Use synthetic data and test accounts.
- SMTP secrets belong in local environment settings or the hosting provider’s secret store, never in Git.
- In-app notifications are stored in the application database. SMTP is used only for email delivery.
- Android and iOS applications, production hosting, persistent object storage, account recovery, and independent security review remain future work.
