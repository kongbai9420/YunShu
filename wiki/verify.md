# 校验下载

从 4.4.0-beta.1 起，每个版本都由 GitHub Actions 的发布流水线在版本标签上构建，并提供三种可以核对的东西：

| 内容 | 作用 |
| --- | --- |
| 镜像签名 | 多架构镜像用 cosign 无密钥签名，记录在 Sigstore 公开透明日志里 |
| 来源证明 | 镜像和每个发布文件都有 GitHub 构建来源证明，说明由哪个仓库、哪个工作流、哪个提交构建；镜像另有 SPDX SBOM 证明 |
| 校验文件 | 发布页的 `SHA256SUMS` 列出全部文件的 SHA-256，`SHA256SUMS.cosign.bundle` 是对它的签名 |

完整命令见仓库里的 [校验说明](https://github.com/kongbai9420/YunShu/blob/main/deploy/VERIFY.md)。

## 最简单的核对：SHA-256

从 [Releases](https://github.com/kongbai9420/YunShu/releases) 下载文件和 `SHA256SUMS`，放在同一个目录：

```bash
sha256sum -c SHA256SUMS --ignore-missing     # Linux / macOS，每个文件都应显示 OK
```

```powershell
# Windows：与 SHA256SUMS 里同名那一行比较（大小写不影响）
(Get-FileHash .\Yunshu-4.4.0-beta.2-windows-amd64.msi).Hash
```

想确认 `SHA256SUMS` 本身没被改过，再用 cosign 校验它的签名（需要 cosign 3.x）：

```bash
cosign verify-blob --bundle SHA256SUMS.cosign.bundle \
  --certificate-identity-regexp '^https://github.com/kongbai9420/YunShu/.github/workflows/release.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  SHA256SUMS
```

## 来源证明

用 GitHub CLI（2.49 或更新，先 `gh auth login`）：

```bash
gh attestation verify yunshu-4.4.0-beta.2-linux-amd64.tar.gz --repo kongbai9420/YunShu
gh attestation verify Yunshu-4.4.0-beta.2-windows-amd64.msi --repo kongbai9420/YunShu
gh attestation verify oci://ghcr.io/kongbai9420/yunshu@<digest> --repo kongbai9420/YunShu
```

镜像的 `<digest>` 用 `docker buildx imagetools inspect ghcr.io/kongbai9420/yunshu:4.4.0-beta.2` 查。校验通过后，部署时可以写 `image: ghcr.io/kongbai9420/yunshu:4.4.0-beta.2@sha256:…`，以后标签被移动也不会换掉你的镜像。

## Windows 安装包没有代码签名

MSI 和免安装 ZIP **没有** Authenticode 签名，运行时 SmartScreen 会提示“未知发布者”。请先按上面核对校验值与来源证明，再点“更多信息 → 仍要运行”。

## 离线镜像包

不能访问镜像仓库的内网环境，用发布页的 `yunshu-4.4.0-beta.2-image-amd64.tar.gz` 或 `-arm64.tar.gz`（`uname -m` 显示 `x86_64` 选 amd64，`aarch64` 选 arm64）：

```bash
docker load -i yunshu-4.4.0-beta.2-image-amd64.tar.gz
```

之后按正常方式部署，`image:` 写 `ghcr.io/kongbai9420/yunshu:4.4.0-beta.2`，Docker 会直接用本地导入的镜像。

## 校验失败怎么办

不要使用该文件或镜像，重新从 [Releases](https://github.com/kongbai9420/YunShu/releases) 下载；仍然失败请在仓库提 issue，写明文件名、版本和命令输出。
