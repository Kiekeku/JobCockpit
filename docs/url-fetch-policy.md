# URL Fetch Policy

URL ingestion and Gmail redirect resolution must block unsupported protocols, private-network hosts, cloud metadata addresses, over-large responses, and excessive redirects. Localhost is allowed only for explicitly configured mock adapters.

Resolution must not execute JavaScript, submit forms, load remote images, or use a browser session.

