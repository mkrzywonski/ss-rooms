# Stellar Skies Lodge

Members-only bedroom reservations, property reports, and shared supply coordination.
Built with Django, PostgreSQL, and Docker Compose. See [design.md](design.md) for
the booking policies and original design.

## Current state

The initial application includes:

- Email verification and administrator approval, membership suspension, and administrator roles.
- Private and shared bedroom reservations, one room per new booking, room
  changes/cancellation, a shared month/week/list calendar, and booking history.
  Existing multi-room bookings remain manageable.
- Maintenance blocks and bedroom capacity configuration.
- Problem reports, authenticated photos, comments, status history, and participant email preferences.
- Stock observations, history, and restocking plans linked to bookings.
- A durable email queue with retries and failed-delivery administration.

Bedroom names and capacities are placeholders. Sharing is disabled until an
administrator confirms the actual sleeping arrangements. The VPS is available at
https://ss-rooms.krzywonski.me through Apache; see [DEPLOYMENT.md](DEPLOYMENT.md).
SMTP delivery is configured and has been tested successfully by the administrator.

## First setup

Create a private configuration from `.env.example`. Set independent, randomly
generated `DJANGO_SECRET_KEY` and `DB_PASSWORD` values, and protect `.env`
with mode 0600. Do not commit it.

```sh
docker compose up -d --build
docker compose exec web python manage.py seed_lodge
docker compose exec web python manage.py createsuperuser
```

The migration service runs before web and worker startup. The seed command
creates three private bedrooms and three supply items; it does not modify
existing records.

The application binds to **127.0.0.1:8092** on the VPS. To access it from your
computer during development:

```sh
ssh -N -L 8092:127.0.0.1:8092 vps.wg
```

Then open http://localhost:8092. Use a different local port if 8092 is occupied;
update `PUBLIC_URL` and `ALLOWED_HOSTS` accordingly if using a different origin.

Create the first administrator interactively with `createsuperuser`; never
pass a password on the command line. Existing administrators can grant or
remove ordinary administrator privileges on the Members page. Ordinary
administrators can inspect the read-only Django admin and retry failed emails;
all domain changes use the application pages, where booking rules are enforced.

## User administration

Administrators use **Admin → Users and approvals** to add, edit, or delete users. The edit form
supports name, email, optional mailing address, phone number, observatory number,
membership status, account access, administrator role, and an optional password
change. Observatory numbers are text identifiers (for example, `07` or `A-3`),
not unique numeric values. Contact fields are editable by administrators and
account owners (address/phone/observatory number), and are not requested during public registration. Leave password fields blank to preserve an
existing password. New approved accounts can receive a password-setup link;
with the file email backend, these links are captured rather than delivered.
Administrators can instead set an initial password in the form.

Deletion requires confirmation, disables login, and hides the account from the
current member list. Shared reservations, problem reports, and audit history
are retained; the email address remains reserved. Current/future reservations
must be canceled or otherwise handled before deletion. Outstanding restocking
plans are canceled and problem assignments cleared. Deleted accounts remain
visible under **Deleted accounts**. Self-deletion and superuser deletion are
blocked; administrators cannot remove their own access through the form.

Account updates are audited and stale edits rejected. Passwords are hashed,
never included in audit records or notification emails.

## Contact sharing and member profiles

Approved members click their name in the header to open **My profile**, where
they can update their address, phone number, and observatory number, and
independently check **Share my address with other members** or **Share my phone
number with other members**. Both options default to off, including existing
accounts. Saving unchecked options revokes sharing without deleting the values.
Only the owner can change consent; admin forms display another member's
preferences as disabled fields, and the service rejects attempted overrides.
Admins can change their own preferences like any member.

The **Members** directory includes approved, active, undeleted members, their
names, existing shared emails and observatory numbers, and explicitly shared
contact details. Private address/phone values are redacted in the database
query, not merely hidden with CSS. Administrators see the same privacy-filtered
directory, but retain access to all contact fields through user administration.
Profile/directory responses are marked private/no-store. Previously viewed
copies cannot be recalled.

Profile edits update only the authenticated owner's contact fields and sharing
preferences, never a posted target ID, role, email, or membership status. Stale
profile/admin edits are rejected; profile audit records describe changed fields
and consent without retaining old contact details.

### Password and email changes

My profile has separate password and email forms. Password changes require the
current password and Django's password-strength validation. The current session
is preserved while other sessions are invalidated. Passwords are hashed, never
stored in audit entries or notification messages. A security notification goes
to the existing email address.

Email changes require the current password and verification at the new address.
The existing sign-in address stays active until the owner signs in and confirms
the link with a CSRF-protected POST. Links expire after 48 hours; replacing or
canceling the request, changing/resetting the password, or an administrator
changing the email invalidates old links. Address availability is rechecked at
confirmation. Old and new addresses receive completion notifications; the old
address also receives a request notice. Pending addresses never appear in the
member directory. Confirmation tokens and password fields are redacted from
application logs/error reports.

## Registration and invitations

Registration is enabled by default. Applicants verify their email before
administrators receive an approval request. Until approved they can only see
their account status. Administrators review accounts under **Members**.

To require invitations, set `INVITATION_REQUIRED=true` and recreate web and
worker. Issue an invitation using:

```sh
docker compose exec web python manage.py invite_member member@example.com
```

Invitations expire after 14 days and are tied to the email address.
Approval is still required. Set `REGISTRATION_ENABLED=false` to close registration.

Public authentication endpoints have a database-backed hourly throttle and
registration includes a honeypot. The throttle uses the immediate peer IP:
behind one proxy, it conservatively limits requests across that proxy together.
For this small group, this is intentional. Add per-client edge rate limiting if
public traffic becomes significant; never trust arbitrary forwarded IP headers.

## Administration menu

Approved administrators see the same Reservations, Property, and Supplies pages
as other members, with one additional **Admin** dropdown. It contains users and
approvals, bedrooms and maintenance, reservation management, supply-item
configuration, email configuration, the email queue, and audit history.
Administrative supply configuration and changes to other members' reservations
are accessed here rather than on regular member pages. Own reservations remain
editable normally. All administrative endpoints still enforce permissions.

## Email

Configure outgoing SMTP delivery through **Admin → Email configuration**. Enter
the hostname, port, STARTTLS or TLS/SSL security, account, password/app password,
sender address, and optional display name. There is no delivery-mode selector.
Save settings or choose **Save and send test email** to queue a test to your own
account. Refresh for retry/delivery status and check your inbox. SMTP acceptance
does not guarantee inbox delivery. The administrator has tested the VPS SMTP
configuration successfully.

Settings apply per message without restarting services. Blank password fields
retain the saved password; a separate checkbox clears it. Changing the host or
account requires replacing or explicitly clearing the password. SMTP always
uses certificate-verified STARTTLS (usually port 587) or implicit TLS (usually
port 465). Environment settings are only a bootstrap fallback before the first
SMTP configuration is saved; no capture/default modes are offered in the UI.

### SMTP password encryption

The database stores only authenticated
[Fernet ciphertext](https://cryptography.io/en/latest/fernet/), never the SMTP
password in plaintext. Encryption uses a dedicated, randomly generated
`SMTP_ENCRYPTION_KEY`, independent of `DJANGO_SECRET_KEY`. The key is in the
private, Docker-excluded `.env` file (mode 0600), not the database or its backups.
Protect that configuration separately in an encrypted secret store. Server/root
access can access both the runtime key and password; this protects stored data
and database-only leaks, not a compromised running VPS. Decryption necessarily
occurs in memory to authenticate to SMTP.

Password values and keys are excluded from audit records, form responses,
notification errors, and sensitive Django error-report variables. A missing,
invalid, or wrong key fails closed rather than storing plaintext. Losing the key
requires re-entering the password; rotating Django's signing key does not affect
SMTP credentials.

For planned key rotation, stop web/worker, securely replace
`SMTP_ENCRYPTION_KEY` with a new random Fernet key and put the retired key in
`SMTP_ENCRYPTION_PREVIOUS_KEYS` (comma-separated). Run:

```sh
docker compose run --rm -T --no-deps web python manage.py reencrypt_smtp_password
docker compose up -d --no-deps --force-recreate web worker
```

The command rewraps the credential under the primary key, verifies the plaintext
is unchanged in memory, and commits atomically without printing it. Remove
retired keys from the running configuration after verification and recreate the
containers. Retain retired keys securely for any retained backups encrypted
under them. Do not put keys in command-line arguments or shell history.
The one-time `--legacy` option migrates the original Django-secret-derived
ciphertext; normal runtime never falls back to Django's signing key.

Domain events and queued messages commit in one database transaction. The worker
retries failures, marking them failed after eight attempts. Inspect/retry these
under **Admin → Email delivery queue**. A crash after SMTP acceptance can cause
duplicates. The worker also deactivates unverified registrations after 30 days.

## HTTPS reverse proxy

Set `PUBLIC_URL=https://your-hostname.example`, add that hostname to
`ALLOWED_HOSTS` while retaining `127.0.0.1` for the container health check,
and set `SECURE_COOKIES=true` and `TRUST_PROXY=true`.

A host-based nginx proxy can use:

```nginx
server {
    listen 443 ssl;
    server_name your-hostname.example;
    # Supply your existing certificate configuration here.
    client_max_body_size 55m;

    location / {
        proxy_pass http://127.0.0.1:8092;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

Use the existing proxy's HTTP-to-HTTPS redirect. If the proxy runs in Docker,
attach only web to a shared proxy network and route to port 8000; leave the
database on the private Compose network.

Do **not** expose the uploads volume through nginx or add a public
`/media/` alias. Photos are served through authenticated `/photos/<id>/`
views. Static CSS and Django admin assets are served by WhiteNoise.

## Tests and checks

Tests run on a separate PostgreSQL test database. The suite includes simultaneous
booking attempts from independent connections; SQLite is not supported.

```sh
docker compose run --rm -T --no-deps web python manage.py test lodge --settings=config.test_settings --noinput
docker compose run --rm -T --no-deps web python manage.py check
docker compose run --rm -T --no-deps web python manage.py makemigrations --check --dry-run
```

For tests against edited files before rebuilding, add
`--volume "$PWD:/app"` before `web`. Test settings use a fast password hasher
and simple static storage. Do not use test settings for the running service.

For production security checks, run `python manage.py check --deploy` with the
actual HTTPS settings. HTTPS redirect/HSTS policy is owned by the reverse
proxy; configure it there after confirming HTTPS works.

## Updating and development

Edit sources on the VPS, run the focused tests, then:

```sh
docker compose build
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:8092/health/
```

The migration container must execute successfully before the new application
starts. For releases involving schema changes, stop web/worker before migrating
unless the migration is explicitly compatible with the old version.

`requirements.lock` pins the tested runtime dependencies.
`requirements.txt` defines the permitted direct dependency ranges. Update the
lock deliberately, test the resulting image, and rebuild for dependency updates.

## Backup and restore

Database and uploads are separate persistent volumes. Configuration and signing
keys must also be retained securely. Losing the signing key invalidates existing
sessions and tokens.

Run a coordinated backup during a short maintenance window:

```sh
sh scripts/backup.sh
```

The script stops only this project's web/worker, dumps PostgreSQL and archives
uploads, then restarts web/worker even if the backup fails. Backups are created
in a new private directory under `backups/`. A completion marker is written
only after both archives succeed. Copy completed backups off the VPS using an
encrypted storage or transfer mechanism, and keep `.env` separately in a
secure password/secret store.

Restore into a new, empty deployment with the matching application version.
Do not restore over an existing live database without an explicit recovery plan.

1. Restore the protected configuration and build the application.
2. Start only `db` with `docker compose up -d --wait db`.
3. Restore the dump into the empty database:

   `docker compose exec -T db pg_restore -U lodge -d lodge --no-owner --exit-on-error < backups/SELECTED/database.dump`

4. Restore uploads:

   `docker compose run --rm -T --no-deps --entrypoint tar web -xzf - -C /app/media < backups/SELECTED/uploads.tar.gz`

5. Start the application with `docker compose up -d`. Check database
   migrations, health, login, calendar, and authenticated photo access.

Test restoration in an isolated Compose project with a separate port and
volumes before depending on the backups.

## Remaining deployment inputs

- Real bedroom names and sleeping capacity.
- Initial administrator email and password, entered interactively.
- SMTP provider credentials and verified sender address.
- Public hostname and existing reverse-proxy configuration.
- Backup schedule and encrypted off-VPS destination.
