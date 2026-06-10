# Security policy

## Reporting a vulnerability

If you find a security issue in birdfy-bridge — credential leak, code-injection vector, anything that lets one user reach another user's Birdfy account — please **do not** open a public GitHub issue.

Instead, open a private security advisory via GitHub's "Security" tab → "Report a vulnerability".

Please include:
- A description of the issue and impact.
- Reproduction steps or a proof-of-concept (if applicable).
- Whether you've disclosed it elsewhere.

I'll acknowledge within a few days and aim to publish a fix or mitigation within 30 days for high-severity issues.

## Out of scope

- Vulnerabilities in **Netvue's cloud API itself**. This bridge is an unaffiliated interop client; report those to Netvue.
- Vulnerabilities in upstream dependencies (`aiortc`, `aiohttp`, `mediamtx`, `ffmpeg`). Please report them to the relevant project. If a CVE in a dependency means birdfy-bridge needs a pin bump, that *is* in scope here — please open an advisory.

## Supported versions

There is one supported version: the latest tagged release on `main`. There is no LTS branch.
