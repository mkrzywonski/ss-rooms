# Stellar Skies Lodge — Design Document

This design proposes a small, members-only web application for reserving Lodge bedrooms, reporting property problems, and coordinating consumable supplies. It will run through Docker Compose on your VPS behind an existing HTTPS reverse proxy.

The design prioritizes transparent coordination among approximately a dozen members, reliable reservations, and straightforward maintenance.

## 1. Scope and proposed defaults

The initial release includes:

- Email registration, email verification, and administrator approval.
- A shared reservation calendar for the three bedrooms.
- One bedroom per new reservation; reserve additional rooms separately. Changes and cancellations are supported. Existing multi-room bookings remain manageable.
- Optional sharing of bedrooms with multiple sleeping places.
- Problem reports with photos, comments, and status history.
- Consumable stock levels and “I’ll bring some” commitments.
- Email notifications and an administration interface.

The data model will anticipate future resource types, but the initial interface will deal exclusively with bedrooms.

The following are proposed defaults where you have not yet chosen a policy:

| Decision | Proposed default |
|---|---|
| Registration | Public registration requests; verified email and administrator approval required |
| Authentication | Email address and password, with password reset |
| Administrators | Multiple administrators supported |
| Reservation approval | Immediate confirmation when availability permits |
| Stay limits | No maximum stay or advance-booking limit initially |
| Date convention | Arrival date inclusive; departure date exclusive |
| Sharing | Private by default; explicitly opt into sharing |
| Site timezone | `America/Chicago`, configurable |
| Member visibility | Names, contact email, occupancy, dates, and reservation notes visible to approved members |
| Administrative authority | Manage any reservation, with history and notification to the affected member |

Payments, waitlists, recurring reservations, and guest accounts are outside the initial scope.

## 2. Membership and access

An account has one of these states: **email unverified, pending approval, approved, rejected, or suspended**. Administrator privileges are separate from account status.

Registration proceeds as follows:

1. The applicant supplies their name, email address, password, and a short explanation of their connection to Stellar Skies.
2. The system sends an expiring email-verification link.
3. After verification, the account enters the approval queue and administrators receive an email.
4. An administrator approves or rejects the request.
5. The applicant receives the decision by email. Approved members can immediately use the application.

Unapproved users can view their own registration status but cannot see reservations, member information, reports, or supplies.

To address spam without requiring a complete membership list:

- Rate-limit registration, login, verification emails, and password resets.
- Add a hidden bot-detection field to registration.
- Notify administrators only after email verification.
- Expire abandoned, unverified registrations.
- Allow administrators to disable registration or switch to invitation-required registration.
- Add a CAPTCHA only if actual abuse warrants it.

An invitation, if enabled, permits registration but still requires approval. Administrator-created accounts should receive an invitation to establish their own password.

The first administrator is created through a deployment command. Administrators can approve members, assign administrator privileges, and suspend accounts. Suspension invalidates existing sessions; upcoming reservations remain visible for administrators to review rather than being silently canceled.

## 3. Bedroom reservations

Each bedroom has a name, description, number of reservable sleeping places, sharing setting, and active status. Administrators configure the three rooms and can block dates for maintenance.

A reservation belongs to one member, who is responsible for any accompanying guests. Guests do not need accounts. The reservation records dates, rooms, party size, sleeping places required, sharing preferences, and notes.

A single booking may contain several bedrooms. Submission is atomic: either all selected bedrooms are reserved or none are. The member may subsequently change or cancel individual rooms.

**Dates and overnight stays.** A reservation from October 10 to October 13 occupies the nights of October 10, 11, and 12. Another reservation may begin October 13.

The interface should ask for “first night” and “departure date” to reduce ambiguity for astronomers arriving after midnight. A member who arrives at 1 a.m. on October 11 but needs accommodation for the October 10 observing night must book October 10. Arrival notes can explain unusual timing.

All stay dates use the site’s timezone, regardless of where the member lives. Event timestamps are stored in UTC.

**Private and shared reservations.** A private reservation occupies the whole bedroom. A shared reservation occupies a specified number of sleeping places.

Overlapping reservations are permitted only when:

- The bedroom supports sharing.
- Every reservation overlapping on a given night explicitly permits sharing.
- The total sleeping places reserved does not exceed the bedroom’s capacity on any night.
- No maintenance block applies.

For example, in a two-place room, Alice can reserve one place and permit sharing. Bob can reserve the other place for overlapping nights if he also permits sharing. If Alice reserves privately, the room is unavailable to Bob.

Sleeping-place capacity must be configured deliberately: a double bed should not automatically be treated as two independently shareable places. Guest count and sleeping places are recorded separately.

Members cannot change a shared reservation to private while another reservation overlaps. Similarly, administrators cannot reduce room capacity or disable sharing if that would invalidate existing bookings; those conflicts must first be resolved.

**Changes and cancellations.** Members manage their own reservations. Administrators can manage all reservations, recording a reason for changes made on someone’s behalf.

Changes are validated and committed as one operation. If new dates or rooms are unavailable, the original reservation remains intact. Cancellations release availability immediately and retain history.

Administrators follow the same capacity and consent rules. They can explicitly rearrange or cancel reservations, but cannot silently overbook a room.

**First-come, first-served.** Availability displayed in the calendar is provisional until submission succeeds. The first valid booking committed by the server receives the space. Opening a form does not hold a room.

## 4. Shared calendar and member experience

The home page brings together:

- Upcoming stays and the member’s own bookings.
- Open property problems.
- Supplies marked low or out.
- Upcoming restocking commitments.

The reservation calendar shows all three bedrooms, member names, party sizes, and private or shared occupancy. It distinguishes **available**, **sharing space available**, **full**, and **maintenance blocked**.

Selecting a reservation shows contact email, dates, guests, and notes. Members can coordinate directly as they do through the current distribution list.

Provide month, week, and accessible list views, with a phone-friendly layout. Calendar navigation should preserve selected dates when opening the booking form.

Member content and uploaded photos require an approved account. Openness applies within the group.

## 5. Property problem reports

Any approved member can submit a report with:

- Title and description.
- Location, optionally linked to a bedroom.
- Suggested urgency.
- One or more photos.

Reports use **open, in progress, resolved, and closed** statuses. Any member can comment, change status, or reopen a problem. Resolving a report requires a short explanation of what was done. Members may optionally identify themselves as working on a problem.

Every status transition records who made it, when, and any explanation. Administrators can correct or archive duplicate reports.

New reports generate email to all active administrators. Reporters and participating commenters receive subsequent comments and status changes, with an unsubscribe option. Administrators receive resolution and reopening notifications; routine comments need not generate additional administrator mail unless they are participating.

A report linked to a bedroom does not automatically block reservations. Administrators create an explicit maintenance block when accommodation is affected. If the block intersects existing reservations, the system presents the conflicts and requires their resolution before applying it.

Photos are validated, resized, and stored in a persistent upload volume. Set limits on upload size, image dimensions, and photos per report. Use generated filenames and remove location metadata when processing images.

## 6. Consumables and restocking

Administrators manage the list of consumables, including name, description, storage location, and active status.

Each item has a stock level of **plenty, low, out, or unknown**, together with the last update’s author, time, and optional note. Any member can update stock levels. History remains available so members can judge how current an observation is.

Members can record a restocking commitment containing:

- The item they will bring.
- Expected delivery date.
- Optional quantity or description.
- Optional link to a reservation.
- Status: planned, delivered, or canceled.

Several members may make commitments for the same item. All commitments are visible to help avoid unnecessary duplication.

A planned delivery does not change the stock level. Marking a commitment delivered prompts the member to record the resulting stock level. Overdue commitments remain visible as overdue; the system does not assume delivery occurred.

If a linked reservation is canceled, the member is prompted to update the commitment, which is flagged for review until addressed. A reservation date change similarly prompts review of the delivery date.

The initial release shows low supplies prominently in the application. Optional supply-summary emails can be added later.

## 7. Email delivery

| Event | Recipients |
|---|---|
| Email verification or password reset | Applicant or account owner |
| Verified registration awaiting approval | Active administrators |
| Registration approved or rejected | Applicant |
| Reservation created, changed, or canceled | Booking owner |
| Administrator changes a reservation | Booking owner, including reason |
| New problem report | Active administrators; confirmation to reporter |
| Problem comments and status changes | Relevant participants, according to preferences |
| Problem resolved or reopened | Active administrators |

Use an external SMTP service configured through deployment secrets. The provider and sending address remain deployment choices.

Application changes and their notification records are saved in the same database transaction. A separate worker sends pending email and retries temporary failures. Consequently, a mail outage does not lose a reservation or problem report.

The administration interface shows failed deliveries and allows retrying them. Retries should minimize duplicate messages, although SMTP cannot guarantee exactly-once delivery.

Approval links in email open the authenticated review page; simply following a link never approves an account.

## 8. Application architecture

I recommend a single Django application with PostgreSQL and server-rendered pages. Django provides integrated authentication, forms, administration, database migrations, and email support, which suits a small application with substantial administrative workflows. See the [Django documentation](https://docs.djangoproject.com/en/5.2/).

Use modest JavaScript for calendar navigation and photo uploads. Keep validation and authorization on the server.

```text
Member’s browser
       |
       | HTTPS
       v
Existing reverse proxy
       |
       v
Django web application ────── Persistent photo storage
       |
       v
PostgreSQL
       ^
       |
Email worker ──────────────── External SMTP service
```

The web application and email worker use the same application image. The worker polls a database notification queue, avoiding an additional queue service for this scale.

Application modules:

- Accounts and approvals.
- Bedrooms and reservations.
- Problems, comments, and photos.
- Supplies and commitments.
- Notifications and audit history.

No public API is required initially. Business rules should live in reusable application services so a future API can use the same behavior.

## 9. Data model and consistency

| Entity | Main information |
|---|---|
| User | Name, unique normalized email, password hash, account status, administrator role |
| Registration decision | Applicant, reviewer, decision, reason, timestamp |
| Resource | Type, name, description, sleeping capacity, sharing enabled, active status |
| Booking | Owner, overall status, notes, creation time, revision |
| Reservation | Booking, resource, arrival, departure, guest count, sleeping places, sharing consent, status |
| Maintenance block | Resource, dates, reason, creator |
| Problem | Reporter, location/resource, description, urgency, status |
| Problem comment/photo | Parent report, author, content or storage reference |
| Consumable | Name, location, current level, last observer and update time |
| Stock observation | Item, level, author, note, timestamp |
| Restocking commitment | Item, member, expected date, optional booking, description, status |
| Notification | Event, recipient, delivery state, attempts, next retry |
| Audit event | Actor, action, affected record, timestamp, relevant changes |

The resource type is initially always `bedroom`. Future resource types can introduce their own capacity and booking policies without changing member accounts or notification infrastructure.

**Preventing double bookings.** Every booking mutation runs in a database transaction and locks the affected resource rows before checking occupancy. Multiple resources are locked in a consistent order. Creation, modification, cancellation, and maintenance blocking all use this path.

Locking the resource itself handles simultaneous attempts even when it has no existing reservations. PostgreSQL’s [row-level locking](https://www.postgresql.org/docs/current/explicit-locking.html) supplies the synchronization mechanism.

Validation checks capacity and consent throughout the requested date range, excluding the reservation being edited. Database constraints enforce valid dates, positive capacity requirements, and required relationships.

Revision checks detect stale edits: if someone changes a booking or stock level while another member has its form open, the latter sees the current values before overwriting them.

## 10. Docker Compose deployment and operation

Compose defines three long-running services:

| Service | Purpose | Persistent data |
|---|---|---|
| `web` | Serve the application and authenticated photos | Upload volume |
| `worker` | Deliver queued notifications and perform scheduled cleanup | PostgreSQL queue |
| `db` | PostgreSQL | Database volume |

A one-off migration command runs during deployment before the new application starts serving traffic.

The reverse proxy terminates HTTPS and forwards requests to `web`. If it runs on the host, publish the web port only on loopback. If it runs in Docker, connect through a controlled shared network. PostgreSQL is never exposed publicly.

Configure the application’s public URL, allowed hostname, trusted proxy settings, timezone, database credentials, signing secret, and SMTP credentials externally. Secrets stay out of source control and container images.

Containers use restart policies and health checks. Compose can wait for database health during startup; the application should also handle temporary database unavailability. See [Docker’s startup-order guidance](https://docs.docker.com/compose/how-tos/startup-order/).

Operational requirements:

- Back up PostgreSQL and uploaded photos together on a documented schedule.
- Keep encrypted backup copies outside the VPS.
- Test restoration before launch and periodically afterward.
- Pin deployment versions and apply security updates regularly.
- Log application errors, worker failures, and administrative changes without recording passwords or tokens.
- Provide a deployment runbook covering setup, upgrades, backup, restoration, and administrator recovery.

Use secure session cookies, CSRF protection, server-side permission checks, and expiring verification/reset tokens. Member suspension must be enforced on every authenticated request.

## 11. Verification and release criteria

Testing should concentrate on behavior that could disrupt a member’s trip:

- Unverified, pending, rejected, and suspended accounts cannot access member data or book.
- Simultaneous private bookings result in exactly one success.
- Shared bookings never exceed nightly capacity or bypass sharing consent.
- Adjacent stays are allowed; partial overlaps are checked correctly.
- Failed changes preserve the original booking.
- The booking form shows only the selected room; additional rooms require separate reservations. Existing multi-room booking updates remain atomic.
- Maintenance blocks cannot silently displace reservations.
- Members cannot edit another member’s booking.
- Any approved member can resolve or reopen a problem, with history retained.
- Planned restocking does not imply supplies are available.
- Email failures survive container restarts and can be retried.
- Uploaded photos require authentication.
- Database and photo backups can be restored successfully.

Concurrency tests must run against PostgreSQL, using independent database connections.

Before launch, exercise the full workflow with two ordinary accounts and an administrator account on the Compose deployment behind HTTPS.

## 12. Decisions to confirm during implementation

The design can proceed with the proposed defaults. The remaining practical inputs are the three bedroom names, their sleeping arrangements, initial administrators, the SMTP service, and the application hostname.

The main policy decision is whether to enable shared reservations immediately or initially leave every bedroom private. This design supports either configuration. If sharing is enabled, mutual consent and explicit sleeping-place capacity should be part of the first release.
