# Releasing

Docker images are built and published to GHCR **only when a version tag is
pushed**. The image tag mirrors the git tag, so versions are easy to follow.

Pushing a commit to a branch (e.g. `linto`) **no longer builds images** — only
tags do.

## Cut a release

1. Make sure the branch is up to date and green.
2. Create and push a version tag:

   ```sh
   git tag v1.0
   git push origin v1.0
   ```

3. The **Build and push images to GHCR** workflow runs and publishes, for every
   image, two tags:
   - `ghcr.io/linto-ai/<image>:v1.0` — the immutable release
   - `ghcr.io/linto-ai/<image>:latest` — always the newest release

Images built: `meet-backend`, `meet-frontend`, `meet-frontend-dinum`,
`meet-summary`, `meet-agents`.

## Versioning

Use `vMAJOR.MINOR` (e.g. `v1.0`, `v1.1`, `v2.0`). The next release is just the
next tag.

## Manual build

You can also trigger it by hand: GitHub → **Actions** → *Build and push images
to GHCR* → **Run workflow**, and provide the version (e.g. `v1.0`).
