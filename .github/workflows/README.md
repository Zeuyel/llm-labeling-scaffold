# Workflows

## CI

Pushes to `main`, `integration`, and `integration/multiuser-control-plane` run the Python test suite, frontend build, and Compose configuration checks. Pull requests and manual runs use the same checks.

The Compose job validates the base stack and the loopback and rclone overrides with CI-only database passwords. It does not start services or publish ports.

## Docker image

The Docker workflow publishes the panel image to GHCR for `main`, `integration`, and Git tags:

- `main` publishes `latest` and `main`.
- `integration` publishes `integration` and `sha-<commit>`.
- Git tag pushes publish the existing tag-derived image tag.

The integration branch never enables the `latest` or `main` tags.
