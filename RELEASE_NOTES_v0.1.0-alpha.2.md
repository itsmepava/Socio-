# Socio v0.1.0-alpha.2

This alpha builds on the fresh-repository alpha 1 demo and adds member account quality-of-life and financial record workflows.

## Added

- Members can update their profile name, optional phone number and gender, and profile photo.
- The member club gallery shows each club’s membership tier and marks the highest priced available tier as the club’s top tier.
- Members can view their per-club membership payment records and submit bill or expense details with a receipt photo.
- Organization admins with member-management access can review or decline submitted bill and expense proofs. Members receive an in-app notification with the result.
- Organization approval of an upgrade request now creates a prorated simulated checkout. Successful demo confirmation applies the new tier.
- The simulated checkout includes sample card fields with browser-side checks. Card details are never sent to the server or saved; no real payment is made.

## Important demo limits

- Membership payment history contains only transactions Socio records; offline payments need to be entered by the organization to appear there.
- Receipt acceptance records an organization’s review. Socio does not validate whether money changed hands or reimburse expenses.
- A top-tier badge is based on the highest priced active plan configured by that organization. It is a display label, not a judgment of a member’s status or spending.
- This remains a local alpha for team review, not a public production service.

## Upgrade from alpha 1

The app creates new tables and adds the member profile columns when it starts against its local SQLite database. Keep a backup of any demo database before upgrading. Do not include `.env`, SMTP credentials, or a real card number in a GitHub upload.
