# Security Policy

Hermes Health Apollo works with health, calendar, Gmail metadata, OAuth, and
local database state. Treat bug reports and examples for this repository as
sensitive by default.

## Private reporting

Use GitHub's private vulnerability reporting / Security Advisory flow for this
repository when it is available. If that flow is not available to you, email
`apollo@ultima.inc` with a short summary and ask for a private intake path.

Do not open a public issue, discussion, pull request, gist, screenshot, log
dump, or chat message that contains any of the following:

- health records, wearable exports, workout details, food logs, or health PII;
- OAuth client secrets, access tokens, refresh tokens, callback URLs, `code`,
  or `state` values;
- Oura, Google, Gmail, Calendar, or future WHOOP account details;
- local database files such as `health.db`;
- route, map, location, timeline, or takeout exports;
- logs that include message metadata, event titles, attendee identities, file
  paths, or machine/user identifiers.

## What to include

For private reports, include only the minimum information needed to reproduce
or assess the issue:

- affected commit, release, or branch;
- operating system and Python/Hermes versions;
- command or workflow that triggered the issue;
- expected result and actual result;
- short, redacted excerpts of errors or logs.

Use placeholders for secrets, account identifiers, event titles, email
subjects, names, and health values unless the maintainer explicitly requests a
specific private artifact through the advisory thread.

## Public issues

Public issues are fine for documentation problems, feature requests, and bugs
that can be described with synthetic data. If the issue involves a vulnerability
or any real health/context data, use private reporting instead.

## Scope

This policy covers this repository's source code, packaging, documentation,
release artifacts, and local plugin behavior. It does not make Hermes Health
Apollo a medical device or a source of medical advice.
