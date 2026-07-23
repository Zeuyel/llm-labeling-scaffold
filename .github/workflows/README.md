# Workflows

## CI

Pushes to `main` and `integration/multiuser-control-plane` run the Python test suite, frontend build, and Compose configuration checks. Pull requests and manual runs use the same checks.

The Compose job validates the base stack and the loopback and rclone overrides with CI-only database passwords. It does not start services or publish ports. Production and Tunnel overlays require deployment secrets and are validated on the server during the deployment preflight.

## Docker image

The Docker workflow publishes the panel image to GHCR for `main`, `integration/multiuser-control-plane`, and Git tags:

- `main` publishes `latest` and `main`.
- `integration/multiuser-control-plane` publishes `ghcr.io/zeuyel/llm-labeling-scaffold/panel:integration` and `ghcr.io/zeuyel/llm-labeling-scaffold/panel:sha-<short SHA>`.
- Git tag pushes publish the existing tag-derived image tag.

The `integration/multiuser-control-plane` branch never enables the `latest` or `main` tags.
