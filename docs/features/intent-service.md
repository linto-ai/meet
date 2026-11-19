# Intent service page (`/intents`)

Meet can be the *service* of a [cozy intent](https://docs.cozy.io/en/cozy-stack/intents/):
cozy-stack frames `/intents?intent=<id>` in an iframe and the host application
talks to the page through `cozy-interapp`. The host gets
the call through the data and the results of the intent (join, leave, idle,
one error alone).

## How the page starts

1. It reads `intent` from the URL. Without it, it does nothing.
2. `POST /api/v1.0/stack-token/` (the Meet session) gives a stack access token
   and the `platform_url` of the user. The token is only sent to that
   platform, as a Bearer token, by the `fetch` given to `cozy-interapp`. It is
   never logged, stored or put in a URL.
3. `createService(intent)` opens the service. Without it in 10 s the page
   shows an error.
4. `getData()` must answer the data below, else the host gets
   `{ event: 'error', code: 'unsupported' }`. The page then joins the call and
   calls `notifyReadyToUse()` once.

## Data of the host

```json
{ "room": "<uuid>", "token": "<LiveKit token>", "audio": true, "video": false, "displayName": "optional" }
```

`room` is a lowercase UUID, `token` a non blank string, `audio` and `video`
booleans.

## Results to the host

Sent with `sendResult`, then `terminate()` after `left` or `error`:

| Result | When |
| --- | --- |
| `{ event: 'joined' }` | LiveKit is connected |
| `{ event: 'left', reason }` | `user`, `idle`, `removed` or `disconnected`, only after `joined` |
| `{ event: 'error', code }` | the join failed: `token_rejected`, `unreachable`, `unsupported` or `unknown` |

The host leaves with `{ leave: true }` (`service.onData`).

## No session

A 401 on the stack token shows a "Sign in to Meet" button (a popup needs a
click). It opens the Meet sign-in in a popup that comes back to
`/intents/signed-in`, which tells the page (`postMessage` to its own origin)
and closes itself. The page then asks for the stack token once more; a second
401 is the error `unauthenticated`. A 409 `no_twake_instance` (a session from
before the `workplaceFqdn` scope: signing in again fills `twake_instance`)
shows the same button as a 401; a second 409 is `unreachable`. A blocked popup is `popup_blocked`. These
errors, and `unreachable` (any other 409, 400, 5xx, network), are shown by the page
itself: no service exists yet to tell the host.

## Framing

`/intents` is framed by the page that opens the intent (cozy-interapp puts the
iframe in that page, with the URL the stack gave), not by cozy-stack: Meet must
allow the origin of that page (Twake Chat, or whichever app opens the intent)
in `Content-Security-Policy: frame-ancestors` for `/intents`. `/intents/signed-in` is a popup and must **not** be framable.
This repository sets no framing header itself (the Django
`XFrameOptionsMiddleware` and the nginx of `src/frontend/default.conf` add
none): they come from the deployment (ingress), where `/intents` is allowed.

## Deployment settings

All of these are deployment settings, none is in this repository:

- **Scope and claim**: the SSO client of Meet asks the scope `workplaceFqdn` (`OIDC_RP_SCOPES`) and Meet reads it into `user.twake_instance` (`OIDC_USERINFO_TWAKE_INSTANCE_FIELD`). Users get it at their next sign-in. Without it `stack-token` answers 409.
- **Id token**: `OIDC_STORE_ID_TOKEN` stays `True` (the default): the exchange reads the id token of the session.
- **Stack**: in the context of the instance, `allow_app_token_exchange: true` and an `app_token_exchange` entry keyed by the SSO client of Meet (the `aud` of its id token), with `software_id: registry://visio` (the app must be installed from a registry) and `instance_claim: workplaceFqdn`. The id token must carry `sid`.
- **Flag**: `visio.embedded-app-url` of the instance (the flag Visio already uses for the root of Meet) is the origin of Meet, not the URL of `/intents`; the stack appends the `href` itself. Without it the stack serves the intent from the Visio subdomain, so it must be set on each instance. The manifest of Visio needs no permission on a doctype for this intent.
- **Framing**: see above.
- **Sign-in popup**: the Meet session cookie must reach the framed page, so Chat and Meet are on the same site (for example both under `*.example.org`). On different sites the frame has no session and the popup cannot give it one.
- **Intent id**: the page ignores an `intent` that is not a plain identifier (letters, digits, `-` and `_`, 128 characters at most), and never sends the stack token outside the platform URL.
