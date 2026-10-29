# Verifying a Yunshu download

[中文](VERIFY.md)

Starting with 4.4.0-beta.1, every release is built on its tag by the GitHub Actions release pipeline (`.github/workflows/release.yml`), which does three things:

- **Image signature**: the digest of the multi-arch image is signed keylessly with [cosign](https://github.com/sigstore/cosign) (a GitHub OIDC certificate, recorded in the public Sigstore transparency log).
- **Provenance**: the image and every release file get a GitHub build provenance attestation stating which repository, workflow and commit built it. The image also has an SPDX SBOM attestation bound to its final digest.
- **Checksums**: `SHA256SUMS` on the release page lists the SHA-256 of every release file, and `SHA256SUMS.cosign.bundle` is the cosign signature over it.

The identity in the signing certificate is always `https://github.com/kongbai9420/YunShu/.github/workflows/release.yml@refs/tags/v…`. Anything built by another repository, workflow or branch fails the commands below.

The Windows packages (MSI and portable ZIP) are **not** Authenticode-signed, so SmartScreen warns about an "unknown publisher". Check the checksum and provenance as in section 2 before choosing "Run anyway".

## Tools

| Tool | Used for | Version |
| --- | --- | --- |
| [cosign](https://docs.sigstore.dev/cosign/system_config/installation/) | image signature and `SHA256SUMS` signature | 3.x |
| [GitHub CLI `gh`](https://cli.github.com/) | provenance attestations | 2.49 or newer; run `gh auth login` first |
| `sha256sum` (Linux) or PowerShell `Get-FileHash` (Windows) | file checksums | built in |

The examples use `4.4.0-beta.2`; substitute the version you downloaded.

## 1. Verify the image

Look up the digest of the multi-arch index for the version tag (it is also printed at the end of the release notes):

```bash
docker buildx imagetools inspect ghcr.io/kongbai9420/yunshu:4.4.0-beta.2
# "Digest: sha256:…" in the output is <digest>
```

**Signature**:

```bash
cosign verify ghcr.io/kongbai9420/yunshu@<digest> \
  --certificate-identity-regexp '^https://github.com/kongbai9420/YunShu/.github/workflows/release.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

**Provenance and SBOM**:

```bash
gh attestation verify oci://ghcr.io/kongbai9420/yunshu@<digest> --repo kongbai9420/YunShu
# SBOM attestation only
gh attestation verify oci://ghcr.io/kongbai9420/yunshu@<digest> --repo kongbai9420/YunShu \
  --predicate-type https://spdx.dev/Document/v2.3
```

Once verified, consider deploying by digest rather than by tag, e.g. `image: ghcr.io/kongbai9420/yunshu:4.4.0-beta.2@sha256:…`, so a later tag move cannot swap your image.

## 2. Verify release files

Download the files you need from the GitHub Release page **plus** `SHA256SUMS` and `SHA256SUMS.cosign.bundle` into one directory.

**Step 1: make sure `SHA256SUMS` itself is authentic**

```bash
cosign verify-blob --bundle SHA256SUMS.cosign.bundle \
  --certificate-identity-regexp '^https://github.com/kongbai9420/YunShu/.github/workflows/release.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  SHA256SUMS
# prints Verified OK
```

**Step 2: check your downloads against `SHA256SUMS`**

Linux / macOS:

```bash
sha256sum -c SHA256SUMS --ignore-missing
# every downloaded file should report OK
```

Windows PowerShell (run in the download directory):

```powershell
Get-Content .\SHA256SUMS | ForEach-Object {
  $hash, $name = $_ -split '\s+\*?', 2
  if (Test-Path -LiteralPath $name) {
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $name).Hash.ToLower()
    if ($actual -eq $hash) { "OK      $name" } else { "FAILED  $name" }
  }
}
```

For a single file: `(Get-FileHash .\Yunshu-4.4.0-beta.2-windows-amd64.msi).Hash`, compared with the matching line in `SHA256SUMS` (case does not matter).

**Step 3 (optional): provenance**

Each file can be verified on its own, without `SHA256SUMS`:

```bash
gh attestation verify yunshu-4.4.0-beta.2-linux-amd64.tar.gz --repo kongbai9420/YunShu
gh attestation verify Yunshu-4.4.0-beta.2-windows-amd64.msi --repo kongbai9420/YunShu
```

Add `--signer-workflow kongbai9420/YunShu/.github/workflows/release.yml` to also pin the workflow that built it.

## 3. Offline image archives

For networks that cannot reach the registry: `yunshu-4.4.0-beta.2-image-amd64.tar.gz` / `-arm64.tar.gz`. Pick the one matching the server (`uname -m`: `x86_64` → amd64, `aarch64` → arm64).

1. Check the checksum as in section 2 (or run `gh attestation verify`).
2. Load it:

   ```bash
   docker load -i yunshu-4.4.0-beta.2-image-amd64.tar.gz
   # Loaded image: ghcr.io/kongbai9420/yunshu:4.4.0-beta.2
   ```

3. Then follow the [deployment guide](README.en.md) with `image: ghcr.io/kongbai9420/yunshu:4.4.0-beta.2`; Docker uses the loaded local image and does not pull.

The pipeline produces each archive by pulling that architecture from the signed multi-arch image by digest and running `docker save`, so the content is the same as the online image. Its SBOM is the `.spdx.json` file with the same name.

## 4. SBOMs

SBOMs (SPDX JSON) attached to the release:

| File | Content |
| --- | --- |
| `yunshu-<version>-image-<arch>.spdx.json` | the published image (the same SBOM is attested on the image digest) |
| `yunshu-<version>-linux-<arch>.spdx.json` | scan of the unpacked Linux tarball |
| `<portable ZIP name>.spdx.json` | scan of the unpacked Windows portable ZIP |

The MSI has no separate SBOM yet. The SBOM files are listed in `SHA256SUMS` too and can be checked the same way.

## If verification fails

Do not use the file or image. Download it again from [GitHub Releases](https://github.com/kongbai9420/YunShu/releases); if it still fails, open an issue with the file name, version and command output.
