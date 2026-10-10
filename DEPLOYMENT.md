# VPS deployment

Public address: https://ss-rooms.krzywonski.me

Configured and checked on 2026-10-09.

## Request routing

DNS resolves the hostname to the VPS. Host Apache redirects HTTP to HTTPS,
terminates TLS, and proxies to the Lodge at `127.0.0.1:8092`.

- Enabled virtual host: `/etc/apache2/sites-enabled/ss-rooms.krzywonski.me.conf`
- Installed configuration: `/etc/apache2/sites-available/ss-rooms.krzywonski.me.conf`
- Project copy: `apache-ss-rooms.conf`
- Certificate: `/etc/letsencrypt/live/krzywonski.me-0001/fullchain.pem`
- Certificate coverage: `*.krzywonski.me` and `krzywonski.me`
- Certificate expiry at deployment: 2026-12-04
- Apache logs: `/var/log/apache2/ss-rooms.krzywonski.me_{access,error}.log`

The existing wildcard certificate uses the existing Certbot DNS renewal setup.
No separate certificate was issued. Other virtual hosts were not changed.

The proxy preserves the hostname and overwrites `X-Forwarded-Proto` with
`https`. Upload requests are limited to 55 MiB. Access logs omit request
paths to keep verification and password-reset tokens out of logs.

## Application configuration

The private `.env` now uses:

```dotenv
PUBLIC_URL=https://ss-rooms.krzywonski.me
ALLOWED_HOSTS=ss-rooms.krzywonski.me,localhost,127.0.0.1
SECURE_COOKIES=true
TRUST_PROXY=true
WEB_PORT=8092
```

The previous configuration is saved in the private, ignored
`.env.before-https-20261009` file. Web and worker containers were recreated
to pick up the new settings; the database volume was preserved.

SMTP delivery is configured through Admin → Email configuration and was tested
successfully by the administrator. The saved password is encrypted with a
separate random SMTP_ENCRYPTION_KEY in the private .env file (mode 0600), outside
the database. Retain that key separately from database backups; see README.md
for key rotation and recovery.

## Checks performed

- Apache configuration syntax passes.
- DNS resolves to the VPS.
- HTTP redirects to the HTTPS hostname.
- HTTPS login returns 200 with valid certificate verification.
- Health endpoint returns `{"status": "ok"}`.
- CSRF cookie is marked Secure.
- A real HTTPS login POST passes CSRF validation and rejects invalid credentials.
- CSS is served successfully over HTTPS.

## Maintenance

From the project directory:

```sh
docker compose ps
docker compose logs --tail=50 web worker
curl --fail https://ss-rooms.krzywonski.me/health/
```

After changing the Apache configuration, validate before reloading:

```sh
apache2ctl configtest && systemctl reload apache2
```

Do not expose the uploads volume directly through Apache. Photos must continue
to pass through the application's authenticated views.
