# 校验下载的云枢

[English](VERIFY.en.md)

从 4.4.0-beta.1 起，每个版本都由 GitHub Actions 的发布流水线（`.github/workflows/release.yml`）在标签上构建，并做三件事：

- **镜像签名**：多架构镜像的摘要用 [cosign](https://github.com/sigstore/cosign) 无密钥签名（GitHub OIDC 证书，记录在 Sigstore 公开透明日志里）。
- **来源证明**：镜像和每个发布文件都有 GitHub 构建来源证明（build provenance），说明它由哪个仓库、哪个工作流、哪个提交构建；镜像另有绑定到最终摘要的 SPDX SBOM 证明。
- **校验文件**：发布页的 `SHA256SUMS` 列出全部发布文件的 SHA-256，`SHA256SUMS.cosign.bundle` 是对它的 cosign 签名。

签名证书里的身份固定为 `https://github.com/kongbai9420/YunShu/.github/workflows/release.yml@refs/tags/v…`。别的仓库、别的工作流或分支构建的东西，下面的命令都会校验失败。

Windows 安装包（MSI 与免安装 ZIP）**没有** Authenticode 代码签名，运行时 SmartScreen 会提示“未知发布者”。请先按下面第 2 节核对校验值与来源证明，再选择“仍要运行”。

## 需要的工具

| 工具 | 用途 | 版本 |
| --- | --- | --- |
| [cosign](https://docs.sigstore.dev/cosign/system_config/installation/) | 校验镜像签名和 `SHA256SUMS` 签名 | 3.x |
| [GitHub CLI `gh`](https://cli.github.com/) | 校验来源证明 | 2.49 或更新；需要先 `gh auth login` |
| `sha256sum`（Linux）或 PowerShell `Get-FileHash`（Windows） | 核对文件 | 系统自带 |

下面以 `4.4.0-beta.1` 为例，换成你下载的版本号。

## 1. 校验镜像

先查出版本标签对应的多架构索引摘要（发布说明末尾也写着）：

```bash
docker buildx imagetools inspect ghcr.io/kongbai9420/yunshu:4.4.0-beta.1
# 输出里 "Digest: sha256:…" 就是 <digest>
```

**签名**：

```bash
cosign verify ghcr.io/kongbai9420/yunshu@<digest> \
  --certificate-identity-regexp '^https://github.com/kongbai9420/YunShu/.github/workflows/release.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

**来源证明与 SBOM**：

```bash
gh attestation verify oci://ghcr.io/kongbai9420/yunshu@<digest> --repo kongbai9420/YunShu
# 只看 SBOM 证明
gh attestation verify oci://ghcr.io/kongbai9420/yunshu@<digest> --repo kongbai9420/YunShu \
  --predicate-type https://spdx.dev/Document/v2.3
```

校验通过后，部署时建议直接写摘要而不是标签，例如 `image: ghcr.io/kongbai9420/yunshu:4.4.0-beta.1@sha256:…`，这样标签以后被移动也不会换掉你的镜像。

## 2. 校验发布文件

从 GitHub Release 页面下载要用的文件，**以及** `SHA256SUMS` 和 `SHA256SUMS.cosign.bundle`，放在同一个目录。

**第一步：确认 `SHA256SUMS` 本身没有被改过**

```bash
cosign verify-blob --bundle SHA256SUMS.cosign.bundle \
  --certificate-identity-regexp '^https://github.com/kongbai9420/YunShu/.github/workflows/release.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  SHA256SUMS
# 输出 Verified OK
```

**第二步：用 `SHA256SUMS` 核对下载的文件**

Linux / macOS：

```bash
sha256sum -c SHA256SUMS --ignore-missing
# 每个下载的文件都应显示 OK
```

Windows PowerShell（在下载目录里运行）：

```powershell
Get-Content .\SHA256SUMS | ForEach-Object {
  $hash, $name = $_ -split '\s+\*?', 2
  if (Test-Path -LiteralPath $name) {
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $name).Hash.ToLower()
    if ($actual -eq $hash) { "OK      $name" } else { "FAILED  $name" }
  }
}
```

只核对一个文件时：`(Get-FileHash .\Yunshu-4.4.0-beta.1-windows-amd64.msi).Hash`，与 `SHA256SUMS` 里同名那一行比较（大小写不影响）。

**第三步（可选）：来源证明**

每个文件都可以单独校验，不需要 `SHA256SUMS`：

```bash
gh attestation verify yunshu-4.4.0-beta.1-linux-amd64.tar.gz --repo kongbai9420/YunShu
gh attestation verify Yunshu-4.4.0-beta.1-windows-amd64.msi --repo kongbai9420/YunShu
```

想同时限定构建它的工作流，可以加 `--signer-workflow kongbai9420/YunShu/.github/workflows/release.yml`。

## 3. 离线镜像包

给不能访问镜像仓库的内网环境：`yunshu-4.4.0-beta.1-image-amd64.tar.gz` / `-arm64.tar.gz`，按服务器的架构选一个（`uname -m` 显示 `x86_64` 选 amd64，`aarch64` 选 arm64）。

1. 按第 2 节核对校验值（或 `gh attestation verify`）。
2. 导入：

   ```bash
   docker load -i yunshu-4.4.0-beta.1-image-amd64.tar.gz
   # Loaded image: ghcr.io/kongbai9420/yunshu:4.4.0-beta.1
   ```

3. 之后按 [部署指南](README.md) 使用，`image:` 写 `ghcr.io/kongbai9420/yunshu:4.4.0-beta.1`，Docker 会直接用本地导入的镜像，不再联网拉取。

离线包是发布流水线从已签名的多架构镜像里按摘要取出本架构后 `docker save` 得到的，内容与在线镜像相同。对应的 SBOM 是同名的 `.spdx.json`。

## 4. SBOM

发布页附带的 SBOM（SPDX JSON）：

| 文件 | 内容 |
| --- | --- |
| `yunshu-<版本>-image-<架构>.spdx.json` | 实际发布的镜像（同一份也作为证明绑定在镜像摘要上） |
| `yunshu-<版本>-linux-<架构>.spdx.json` | 解开 Linux 安装包后扫描的结果 |
| `<免安装 ZIP 文件名>.spdx.json` | 解开 Windows 免安装 ZIP 后扫描的结果 |

MSI 暂不单独附 SBOM。SBOM 文件本身也在 `SHA256SUMS` 里，同样可以校验。

## 校验失败怎么办

不要使用该文件或镜像，重新从 [GitHub Releases](https://github.com/kongbai9420/YunShu/releases) 下载；仍然失败请在仓库提 issue，写明文件名、版本和命令输出。
