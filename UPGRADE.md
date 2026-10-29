# Upgrade

All instructions to upgrade this project from one release to the next will be
documented in this file. Upgrades must be run sequentially, meaning you should
not skip minor/major releases while upgrading (fix releases can be skipped).

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For most upgrades, you just need to run the django migrations with
the following command inside your docker container:

`python manage.py migrate`

(Note : in your development environment, you can `make migrate`.)

## [Unreleased]

The external API `POST /external-api/v1.0/rooms/{id}/livekit-token` now needs
the `rooms:livekit_token` scope (it needed `rooms:retrieve`). No migration grants
it: give it by hand to each application that mints LiveKit tokens.
Its tokens live one hour (`APPLICATION_LIVEKIT_TOKEN_TTL_SECONDS`), and
`start-recording` / `stop-recording` with a LiveKit token now need its holder
in the call.

The endpoints authenticated by a LiveKit room token (`start-subtitle`,
`mute-participant`, `toggle-hand`, `rename`) no longer take the user from
the token identity: Meet now sets a `user_id` attribute and its
`user_id_proof` in the token of every authenticated user, and only that proof
names a user. A token minted before the upgrade has no proof, so on these
endpoints its holder is anonymous until they join the room again (reloading
the page is enough) or the token expires (six hours by default). The proof is
keyed by `DJANGO_SECRET_KEY`: all backend replicas must share it, and a
rotation has the same effect as the upgrade on the tokens already minted.

The lobby cookie (`LOBBY_COOKIE_NAME`) is now kept only when it is a UUID that
is not the `sub` of a user. A participant who carries another value gets a new
identifier: one waiting in a lobby at upgrade time enters the queue again.

Twake Drive deliveries no longer build the instance as `{sub}.{TWAKE_INSTANCE_DOMAIN}`
when the `sub` holds an `@` or a dot. Such users get their instance from an
OIDC claim, stored at each sign-in. To set it up:

- Have the IdP return the claim at the userinfo endpoint of Meet's client.
  Meet reads userinfo only, not the ID token.
- The claim is `workplaceFqdn` by default. Set
  `OIDC_USERINFO_TWAKE_INSTANCE_FIELD` if your IdP names it differently.

Existing users get their instance at their next sign-in. Until then, for up to
`SESSION_COOKIE_AGE` (12 hours by default) after the upgrade, their uploads are
skipped with a warning.

## v1.23.0

As part of the 1.23.0 release, the legacy `api/v1` implementation has been removed from the _experimental_ Summary service and Meet has been migrated to the new `api/v2`.

**To avoid a breaking change, the Meet backend continues to use the Summary service's v1-compatible API format by default (`SUMMARY_SERVICE_VERSION` setting defaults to `1`).**

If you are deploying both Meet and Summary from this repository, you must configure the Meet backend to use the v2 API by setting the following environment variable `SUMMARY_SERVICE_VERSION=2`.

If you are upgrading only the Meet deployment while keeping an older Summary v1 compatible deployment, no action is required, as the v1-compatible API remains the default.

Note that we plan on removing the legacy `v1` summary compatibility in a future major version. If you have your own implementation for the summary service, we recommend updating its API contract and setting `SUMMARY_SERVICE_VERSION=2`.
