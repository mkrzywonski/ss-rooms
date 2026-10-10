# Validation — 2026-10-09

Development and verification were performed on `vps.wg` in
`/home/mike/room-reservation`, through the authorized aish MCP session.

- Built the application image using the pinned dependencies in `requirements.lock`.
- Passed 50 tests against PostgreSQL 17, including simultaneous private/shared
  booking attempts from independent database connections, multi-room rollback,
  failed/stale edits, individual room cancellation, account approval/suspension,
  administrator permissions, authenticated photo access, stock history,
  restocking ownership, email retries, and verification/reset token log redaction.
- Django system checks passed; no missing model migrations were detected.
- Ruff checks passed with the project's configured lint rules.
- HTTP health, login page, and CSS requests succeeded on the loopback service.
- Verified the upload volume is writable by the non-root application user.
- Executed the coordinated backup script. Restored the database archive into a
  separate temporary database and verified the three bedroom and three supply
  records. Removed that temporary database after verification. The uploads
  archive was readable; at this point there were no real user photos to restore.
- Web and database containers reported healthy; the email worker was running.

The first administrator is `mike@krzywonski.me`. Its password is intentionally
unset. Set it interactively on the VPS:

```sh
docker compose exec web python manage.py changepassword mike@krzywonski.me
```

## Historical calendar availability labels

- Past room-nights now use a neutral Past label instead of Available or shared
  availability. Historical reservation links and maintenance details remain.
- Month/week regression checks cover empty and previously shared past nights,
  retained booking history, and unchanged current/future availability.
- All 14 page tests passed.

## Preventing past reservations

- Retained transactional server-side rejection of new or backdated reservations.
- New-booking date inputs have minimum dates based on the Lodge timezone.
  Old links prefill today or later; past calendar nights omit booking links.
- Added coverage for forged past-date POST rejection, today's allowed arrival,
  minimum-date attributes, and past-calendar link suppression.
- All 158 PostgreSQL tests and Ruff checks passed.

## Sharing checkbox visibility

- New and edited bookings, including individual room edits, show sharing
  checkboxes only when sharing is enabled for that bedroom.
- Disabled rooms omit the field entirely and save private occupancy even if
  a forged submission includes a sharing value.
- Regression coverage checks enabled/disabled room forms and submissions.
- All 155 PostgreSQL tests and Ruff checks passed.

## Single-bedroom reservation forms

- New reservations begin with a bedroom picker, or directly with the bedroom
  selected in a calendar cell. The form renders only that bedroom's occupancy
  and sharing fields. Additional rooms require separate bookings.
- Edit forms include only rooms already booked, retaining compatibility with
  historical multi-room bookings. No database migration or data rewrite.
- Server-side validation rejects missing, invalid, or inactive room selections;
  injected occupancy fields for other bedrooms cannot add them to a booking.
- All 154 PostgreSQL tests passed; Ruff formatting and lint checks passed.

## Email-confirmation CSRF repair

- Production logs showed Origin null on confirmation and logout submissions.
  The confirmation response's no-referrer policy caused this browser behavior.
- Changed that response to strict-origin: Referer never includes the signed
  token path, while same-origin form submissions retain CSRF-compatible origin
  information. CSRF protection remains enabled; null and foreign origins remain
  rejected, even with a valid form token.
- All 19 account self-service tests passed, including CSRF-enforced HTTPS
  confirmation with Origin and the Referer-only fallback, and error-page policy.
- The complete 151-test PostgreSQL suite passed before deployment.

## Email-confirmation account mismatch clarification

- Diagnosed the current emailed link without exposing its token: signature,
  pending address, nonce, and password binding were valid. Older links had been
  superseded by later requests.
- Read-only HTTPS checks reproduced success for the requesting account and the
  misleading invalid/expired error for a different signed-in account. Temporary
  diagnostic sessions were deleted; no email addresses were changed.
- Added a specific wrong-account message without disclosing the requesting
  account's address. Added a regression test for signing into the correct
  account and reusing the same confirmation link.
- All 16 account self-service tests passed.

## Member account self-service update

- Replaced the separate My profile navigation item with a link on the member's
  name, retaining visibility on mobile.
- Added observatory-number editing, current-password-verified password changes,
  and verified email changes with cancellation and 48-hour owner-bound links.
- Password changes keep the current session and invalidate other sessions.
  Email sign-in remains unchanged until a CSRF-protected confirmation POST.
  Old addresses get request/completion notices; new addresses get verification
  and completion messages. Tokens and password fields are redacted.
- All 146 PostgreSQL tests passed, including hashing, session behavior,
  address uniqueness/expiry/replacement/cancellation, rollback, CSRF, and logs.
- Ruff checks passed; no missing model migrations detected.
- Created backup `backups/lodge-20261009T194347Z-W9G4Uc`, applied migration
  `0007_user_email_change_token_user_pending_email`, and deployed rebuilt services.
- Read-only live checks confirmed the name/profile link, all account forms, hash
  non-disclosure, HTTPS health, and Django system checks. No real account details
  were changed and no verification emails were generated by these smoke checks.

## SMTP-only configuration and dedicated encryption key update

- Removed delivery modes from the form, transport selection, and schema.
- User reported successful SMTP delivery before this update.
- Replaced Django-secret-derived encryption for new credentials with a dedicated
  random Fernet key in private .env (mode 0600), outside database backups.
- Added explicit, atomic legacy migration and planned rotation with retired keys.
  Commands verify plaintext equality in memory without printing secrets.
- Encryption/decryption/rotation and configuration saving redact sensitive local
  variables from Django error reports. Missing/invalid keys fail closed.
- All 131 PostgreSQL tests passed, covering mode-free UI, encryption randomness,
  tampering/wrong keys, application-key independence, legacy migration,
  idempotence, planned rotation/retirement, and failure rollback.
- Created backup `backups/lodge-20261009T193111Z-wzXCOA`, applied migration
  `0006_remove_emailconfiguration_mode`, and re-encrypted the live credential
  with `reencrypt_smtp_password --legacy`. Verified equality in memory without
  exposing plaintext or changing server/account settings.
- Live SMTP TLS connection and authentication succeeded after deployment; no
  test email was sent. Verified mode-free HTML, current-key-only decryption,
  private .env permissions (0600), and the public HTTPS health endpoint.

## Owner-controlled contact sharing update

- Added independent address/phone sharing checkboxes, both defaulting to false
  for new and existing accounts, an owner-only My profile editor, and an
  approved-members directory with private contact values redacted in its query.
- Administrators retain access in user administration, but cannot change another
  owner's consent. Their regular directory uses the same privacy rules.
- Profile edits ignore forged user IDs/privileged fields, enforce CSRF and
  freshness, and audit field names/preferences without copying private values.
- All 121 PostgreSQL tests passed; Ruff checks and migration checks passed.
  Coverage includes independent sharing, revocation, private response contents,
  admin visibility/consent protections, status restrictions, and stale edits.
- Created backup `backups/lodge-20261009T190302Z-soZREy`, applied migration
  `0005_user_share_address_user_share_phone_number`, and deployed rebuilt services.
- Live read-only profile/directory checks and public HTTPS health succeeded.
  Verified that all existing accounts remained private after migration.

## Admin navigation and email configuration update

- Grouped administrative entry points under the Admin dropdown, and moved
  supply-item configuration and other-member booking controls off regular pages.
- Added live database-backed SMTP configuration with verified STARTTLS/implicit
  TLS, encrypted credentials, blank-password retention/explicit clearing,
  stale-edit protection, and test messages to the acting administrator.
- Added tests for admin-only endpoints/CSRF, secret non-disclosure, encrypted
  storage, credential replacement, invalid settings, mode switching, live worker
  transport, queue rollback, and ordinary versus administrative navigation.
- All 101 PostgreSQL tests passed; Django system and migration checks passed,
  and Ruff checks passed.
- Created backup `backups/lodge-20261009T185722Z-RYGLzi`, applied migration
  `0004_emailconfiguration`, and deployed rebuilt web/worker services.
- Read-only administrator requests to the live dashboard and new administrative
  pages succeeded; HTTPS health/login checks succeeded. Confirmed that email
  remains in its existing private-file mode without sending test mail.
- SMTP-provider delivery remains unverified until credentials are entered and
  a test is sent from the email configuration page.

## User contact fields update

- Added optional address, phone number, and observatory number to User and the
  administrator Add/Edit user forms. Existing accounts retain blank values.
- Passed all 80 PostgreSQL tests, including field defaults, create/edit/clear,
  form initialization and POST persistence, length limits, and preservation
  when fields are omitted from service calls.
- Ruff checks passed; no missing migrations were detected.
- Created backup `backups/lodge-20261009T173627Z-vL5jv3`, applied migration
  `0003_user_address_user_observatory_number_and_more`, and deployed rebuilt services.
- Verified all three fields appear on live Add/Edit user forms with read-only
  administrator requests; the public HTTPS database health check succeeded.

## User administration update

- Passed all 75 tests, including account creation/edit/deletion, password setup,
  permissions, stale edits, retained history, session handling, transactional
  rollback, and deletion racing against a new reservation.
- Ruff and Django checks passed; no missing migrations were detected.
- Applied migration `0002_user_admin_revision_user_deleted_at` and rebuilt and
  deployed web/worker containers.
- Created coordinated backup `backups/lodge-20261009T154625Z-0I2G6x` before deployment.
- Checked Members, Add user, Edit user, and Delete confirmation pages against the
  running image using read-only requests with an administrator request context.
- Public HTTPS login and database-backed health checks succeeded.

The public HTTPS reverse proxy is now configured; see DEPLOYMENT.md. The
password-unset note above records the initial setup, not current account state.
Real SMTP delivery, a scheduled off-VPS backup destination, and a visual
browser/device review remain deployment tasks. Email currently goes to private
files, not external recipients.
