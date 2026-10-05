# Security policy

This project is experimental. Isolation reduces access to host resources; it does
not guarantee anonymity, prevent account bans, or eliminate hypervisor escapes.
Do not publish VM images, browser profiles, tokens, network leases or user configs.

## Automated checks

The same reusable SAST/SCA workflows run on pull requests, main pushes, version
tags, manual dispatch and weekly schedules. Release publication depends on all
scan jobs, tests and builds succeeding; scanner errors are failures, not approvals.
No blanket vulnerability allowlist and no `continue-on-error` security gates.

- Bandit: application and build Python, medium/high severity.
- CodeQL security-extended: Python, compiled Swift and Actions. SARIF gate rejects
  security-severity >=4 or error-level results. Raw reports are retained even if
  GitHub code scanning upload is unavailable. CodeQL licensing/features may require
  GitHub Code Security for private repositories; never bypass the failed gate.
- pip-audit: hash-locked CI/security-tool dependencies and the separate Windows
  build/runtime closure, including the bundled pycdlib ISO writer.
- Trivy: current source dependencies/secrets and both architectures of a disposable
  Ubuntu package-inventory container. A test keeps its explicit package list in
  sync with the actual guest installer. Both use identical pinned APT signing-key
  fingerprints and signed repositories. The inventory container is never shipped.
- Guest scan exports CycloneDX plus dpkg versions. All HIGH/CRITICAL findings,
  including unfixed advisories, prevent automatic release. Review reports, update
  affected packages and rerun. Do not silently ignore findings to obtain a release.

## What these scans do NOT establish

The inventory image represents packages resolved **at scan time**, not an existing
user's VM, nor every base package in the Ubuntu cloud image. First-run downloads
can resolve newer versions. QEMU, host Python/GnuPG/Homebrew, macOS frameworks and
the installed user's host OS are external dependencies and are **not fully covered
by the guest SBOM**. Keep them updated using the platform's signed package manager.
Trivy may not have advisories for proprietary Claude/Electron components or detect
all bundled JavaScript/native libraries. CodeQL/Bandit do not analyze upstream
Claude, Firefox or QEMU source. No test logs into a Claude account in CI.

A green CI result proves the listed checks passed, not end-to-end VPN isolation on
all platforms. Windows CI tests the packaged GUI/worker, installer/uninstaller,
process-tree ownership and a disposable QEMU guest's gateway. It does not test
every desktop/VPN/hardware combination. Linux/Windows lack the Darwin route
watcher and need manual routing/disconnection testing. A periodic public-IP probe
cannot prevent every race or establish every site's egress under split tunneling.

Windows uses a private virtio-serial/named-pipe gateway instead of libslirp's
Windows-incompatible command spawning. SSH only multiplexes filtered proxy
channels over this device: no host TCP SSH listener, shell, sessions or SFTP.
Authentication is scoped to the private VM device, without account passwords or
host SSH keys. Only the fixed gateway destination is accepted, with up to 64
channels; every destination request still requires validation and a current lease.

## Releases and supply chain

Actions are pinned to full commits; Dependabot proposes updates. Python tools have
hash-locked requirements. Ubuntu downloads require a signature by the pinned Ubuntu
key plus matching SHA256. Claude/Mozilla APT keys have pinned fingerprints. Packages
and external services remain trust dependencies. macOS apps are ad-hoc signed,
not Developer ID signed/notarized. Release checksums establish integrity, not a
separate publisher identity. No signing certificate is committed or required.
The Windows app/installer has no Authenticode certificate. Its first-run QEMU
installation uses Microsoft's winget source and installer hash checks;
these host dependencies are not bundled in the application. The CI-only QEMU
installer has a fixed vendor URL and pinned SHA512 before execution. Private GnuPG
uses the official WiX CAB payload pinned by SHA256, extracted by Windows expand.exe
without running its interactive installer or modifying global GnuPG configuration.

For a suspected vulnerability, use GitHub's private vulnerability reporting if
enabled on this repository. Do not put credentials or exploit details against live
accounts in a public issue. Otherwise open a minimal issue requesting private contact.

## Reviewed scanner false positives

The two Mozilla Firefox/Ubuntu advisory mismatches documented in
[the Firefox review](docs/security-review-firefox.md) have exact-version PURL
exceptions expiring 2026-11-01. These apply only to the guest gate. Raw unfiltered
findings and the applied review are retained alongside the filtered report.
All other HIGH/CRITICAL findings and scanner failures continue to block releases.
