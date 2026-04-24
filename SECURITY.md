# Security

Please report security issues privately through the repository maintainers.

Do not file public issues or public PR comments for vulnerabilities, leaked
credentials, private endpoints, or exploit details.

## Sensitive Material

Do not commit:

- secrets or API keys;
- private hostnames or workspace paths;
- private repository links;
- credential-bearing logs;
- production data exports.

## Runtime And Dependency Notes

Some OmniMarket nodes use network, repository, Docker, secret, or deployment
capabilities. Declare those requirements in the node's `metadata.yaml` so
wrappers and runtimes can fail clearly when prerequisites are unavailable.
