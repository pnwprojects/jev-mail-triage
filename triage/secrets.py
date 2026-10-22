"""Secret access, cached for the life of the container.

Resolution order for each secret:
1. An environment variable of the same name upper-snaked (e.g. GMAIL_OAUTH),
   which is what `gcloud run deploy --set-secrets` mounts, or what you set locally.
2. Secret Manager `projects/<project>/secrets/<name>/versions/latest`.
"""

from __future__ import annotations

import functools
import os


def _env_name(secret_name: str) -> str:
    return secret_name.upper().replace("-", "_")


@functools.lru_cache(maxsize=None)
def get_secret(project_id: str, secret_name: str) -> str:
    from_env = os.environ.get(_env_name(secret_name))
    if from_env:
        return from_env

    from google.cloud import secretmanager  # imported lazily; unused when env-mounted

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    return client.access_secret_version(name=name).payload.data.decode("utf-8")
