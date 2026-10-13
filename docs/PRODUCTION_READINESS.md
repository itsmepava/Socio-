# Socio Alpha 1 readiness

Socio Alpha 1 is a local development build for an organization-managed membership hub. Organizations add people who are already members; invitation links let those members activate a Socio account and complete onboarding. This is not a public membership marketplace and Socio does not collect club dues.

## Included in the alpha

- Platform approval of organization accounts.
- Organization owner dashboard and invitation of scoped staff administrators.
- Custom staff workspace titles, separate from server-enforced permission roles.
- Existing member entry by form or CSV, invitation activation, consent, and organization-specific onboarding.
- Member club gallery, event and offer details, and database-backed in-app notifications.
- SMTP email for invitations and notification copies when configured.
- Member-requested tier changes reviewed by the organization; fees and eligibility remain with the club.
- Member upgrade/tier-change requests and scheduled cancellation controls.
- Two-way QR check-in: organization staff confirm a member’s one-time pass, or a member checks in by scanning the organization’s expiring code. Attendance records are organization-scoped.

## Intended consumer architecture

The current local web app is only the team-testing demo. The target consumer product is a GCP-hosted backend used by native Android and iOS applications distributed through Google Play and Apple’s App Store. The web interface can remain useful for organization administration, but it is not the planned final member experience. GCP deployment and store apps are future delivery stages, not included in Alpha 1.

## Required before a consumer launch

1. Deploy behind HTTPS with a production WSGI server, managed secrets, secure cookies, health monitoring, logs, and a rollback plan.
2. Use a managed database with versioned migrations, automated backups, and a rehearsed recovery process.
3. Use durable private object storage for logos and event posters before running multiple app instances.
4. Replace in-memory rate limits with shared storage and review all tenant-specific authorization paths.
5. Add account recovery, platform-admin multi-factor authentication, session/device management, and security alerts.
6. Verify production SMTP delivery, domain authentication, bounce handling, and email retention. Add SMS only after choosing a provider and consent/abuse controls.
7. Complete privacy terms, retention and deletion controls, incident response, and an independent security review.
8. Validate responsive layouts and key workflows on current desktop and phone browsers, then build and test native Android and iOS apps and complete Google Play and Apple App Store review requirements.

QR check-in tokens are short-lived, stored as hashes, and scoped to an organization and active membership. Before consumer launch, review operational controls for staff attendance corrections, audit retention, member consent, and abuse/rate limits.

SMTP sends email; in-app notices are stored in the database and do not depend on SMTP. Local SQLite and local uploads are suitable only for development. Test with synthetic data; do not use this build for production consumers.
