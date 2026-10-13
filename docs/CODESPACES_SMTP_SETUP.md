# SMTP setup for GitHub Codespaces

This configures Socio to send real confirmation and invitation emails while the alpha runs in GitHub Codespaces. It does not put credentials in the repository. For Gmail, use a dedicated testing account and an App Password, never your normal Google password.

## 1. Create a Gmail App Password

1. Sign in to the Google account Socio will send from.
2. Enable 2-Step Verification in the Google Account security settings.
3. Open **App passwords**, create one for Socio, and copy the generated password. Google may display it in groups; enter it in GitHub without spaces.
4. Keep the App Password private. If it is ever exposed, revoke it in Google and create a replacement.

## 2. Add Codespaces secrets on GitHub

1. Open the Socio repository on GitHub.
2. Select **Settings → Secrets and variables → Codespaces**.
3. Choose **New repository secret** and add these names and values, one at a time:

   | Name | Value for Gmail |
   | --- | --- |
   | `SECRET_KEY` | A unique random value; generate one with the command below. |
   | `MAIL_SERVER` | `smtp.gmail.com` |
   | `MAIL_PORT` | `587` |
   | `MAIL_USERNAME` | The full Gmail address sending Socio mail. |
   | `MAIL_PASSWORD` | The Gmail App Password, without spaces. |
   | `MAIL_FROM` | `Socio <sender@gmail.com>` using that same sender address. |
   | `MAIL_USE_TLS` | `1` |
   | `MAIL_USE_SSL` | `0` |
   | `DEV_EMAIL_PREVIEW` | `0` |

   Generate a random secret in a terminal with:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Copy the generated value directly into the `SECRET_KEY` secret form. Do not put it in a source file or paste it into chat.

4. Save each secret. If you do not have repository Settings access, add them under your personal **GitHub Settings → Codespaces → Secrets** and grant access to the Socio repository. Do not use GitHub Actions secrets; those are a separate store and are not injected into Codespaces.
5. Stop and restart the Codespace so the new secrets are available to its terminal.

Anyone who can run code in a Codespace with access to these credentials could use the sender account. Only grant access to trusted collaborators. The public web app’s visitors do not receive the secrets.

## 3. Install dependencies and initialize the demo database

In the Codespaces terminal, from the repository root, run:

```bash
python -m pip install -r requirements.txt
export PUBLIC_BASE_URL=http://127.0.0.1:5001
python -m flask --app 'app:create_app' init-db
python -m flask --app 'app:create_app' seed-demo
python -m flask --app 'app:create_app' create-admin
```

The two setup commands prompt you to create local demo and platform-admin accounts. Use test email addresses and strong, unique passwords. `seed-demo` must run while `PUBLIC_BASE_URL` is localhost; it intentionally refuses to create sample data using a public address.

If the database is already initialized or the demo data already exists, skip the corresponding setup command. Do not reset a database containing information you want to keep.

## 4. Start Socio using the Codespaces HTTPS address

Run these commands in the same terminal session:

```bash
export PUBLIC_BASE_URL="https://${CODESPACE_NAME}-5001.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
export DEV_EMAIL_PREVIEW=0
python -m flask --app 'app:create_app' run --host 0.0.0.0 --port 5001
```

Open the **Ports** panel in Codespaces and set port `5001` to **Public** if teammates need access without signing in to GitHub. Share the HTTPS URL shown for that forwarded port. Use a public port only for this test demo and test data; anyone with the link can reach the site. The URL is specific to that Codespace and must be updated in `PUBLIC_BASE_URL` if it changes.

## 5. Confirm email delivery

1. Open the forwarded Socio URL and register a test account or invite a test member.
2. Confirm the message arrives and that its link opens the same forwarded HTTPS Socio URL.
3. If it does not arrive, check the spam folder, the Codespaces terminal log, that `MAIL_FROM` matches the Gmail sender, and that the Gmail App Password is current. Never print or share the password while troubleshooting.

Socio’s in-app inbox stores notifications in its database. SMTP provides the accompanying email delivery. Keep credentials in Codespaces secrets or another secret manager; never add `.env`, passwords, or secret keys to GitHub commits or ZIP uploads.
