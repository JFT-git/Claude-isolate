# Firefox: reviewed Ubuntu advisory mismatch

Reviewed 2026-10-02. Expires 2026-11-01; renewal requires a fresh review.

CI run [37016931439](https://github.com/JFT-git/Claude-isolate/actions/runs/37016931439)
reported CVE-2022-25235 and CVE-2022-25236 for Mozilla's `firefox 157.0~build1`
on both amd64 and arm64. Its proposed fix, `1:1snap1-0ubuntu1`, belongs to
Ubuntu's transition to Snap. Comparing that Debian epoch to a Mozilla version
incorrectly treats the current Mozilla package as older than the transition.

Mozilla's upstream investigation states that Firefox does not use the affected
UTF-8 Expat mode for CVE-2022-25235, and its namespace separator avoids the
condition involved in CVE-2022-25236. The final security assessment says the
issues did not apply; defensive fixes were also taken.

Primary evidence:

- [Mozilla bug 1754724, comment 14 (UTF-8)](https://bugzilla.mozilla.org/show_bug.cgi?id=1754724#c14)
- [Mozilla bug 1754724, comment 3 (namespace separator)](https://bugzilla.mozilla.org/show_bug.cgi?id=1754724#c3)
- [Mozilla bug 1754724, comment 22 (security assessment)](https://bugzilla.mozilla.org/show_bug.cgi?id=1754724#c22)
- [Mozilla bug 1764170, follow-up and shipped fixes](https://bugzilla.mozilla.org/show_bug.cgi?id=1764170)
- [Trivy: third-party packages can be mismatched to OS advisories](https://trivy.dev/docs/latest/guide/scanner/vulnerability/#third-party-packages)

## Scope and audit trail

`security/trivy-guest-reviewed.yaml` scopes the exceptions to the **exact PURL
and version** `pkg:deb/ubuntu/firefox@157.0~build1`, and these two CVEs only.
It does not suppress libexpat, another Firefox version, other CVEs, or any other
package. It is passed only to the guest release-gating scan, not source scans.
The repositories use pinned signing-key fingerprints and Mozilla APT preferences.

Both raw and filtered JSON, the effective exception file, dpkg package metadata
and CycloneDX SBOM are retained in `sca-guest-*` artifacts. Scanner errors remain
failures. HIGH/CRITICAL findings outside this scoped review still fail the gate,
including unfixed vulnerabilities. No `continue-on-error` or global CVE ignore
list is used. A new Firefox version is not automatically exempted.

An independent raw-vs-filtered report check rejects missing PURLs, different
packages/versions/CVEs, non-Ubuntu results and expired reviews. This also protects
against Trivy's fallback match when a vulnerability has no package PURL.
