## What's changed

<!-- Before publishing, write this version's changes here and in the Chinese section below, then delete these comments. GitHub shows line breaks in the body as line breaks, so don't wrap paragraphs. -->

## Downloads

| System | File |
| --- | --- |
| Windows 10 / 11, x64 | `OpenWeir-{{version}}-windows-x64-setup.exe` |
| Windows 11, ARM64 | `OpenWeir-{{version}}-windows-arm64-setup.exe` |
| macOS 26 or later, Apple silicon / Intel x64 | `OpenWeir-{{version}}-macos-universal.dmg` |

`SHA256SUMS` holds the SHA-256 of every file on this page. An installed OpenWeir checks for updates under **About** in Settings, and verifies the checksum of the download before opening the installer.

- **Windows**: the installer is signed by OpenWeir (Azure Artifact Signing), and the administrator prompt names OpenWeir as the publisher. While the signature is still building SmartScreen reputation, opening a downloaded installer may show "Windows protected your PC": click **More info**, check that the publisher is OpenWeir, then click **Run anyway**.
- **macOS**: open the disk image and drag OpenWeir to the Applications folder. On the first connection, macOS asks you to allow OpenWeir's system extension and VPN configuration; allow them in System Settings when prompted.

## Source code and licenses

The source code of this version is the [`v{{version}}`](https://github.com/openweir/app-desktop/tree/v{{version}}) tag of this repository. The mihomo linked into the service and the extension is a commit of [openweir/mihomo](https://github.com/openweir/mihomo), pinned by the `replace` in that tag's [`src/core/go.mod`](https://github.com/openweir/app-desktop/blob/v{{version}}/src/core/go.mod); the versions and checksums of the other Go dependencies are pinned by `go.mod` and `go.sum` in the same directory. Running `go mod download` in `src/core` fetches them and mihomo from the Go module proxy (`proxy.golang.org` by default). The "Source code" archives GitHub attaches to every release contain only this repository, without mihomo or the other Go dependencies.

OpenWeir is released under GPL-3.0; the licenses of third-party components are under **Open-source licenses** in Settings. The data that leaves this computer is described in the [privacy notice](https://github.com/openweir/app-desktop/blob/v{{version}}/PRIVACY.md).

---

## 更新内容

<!-- 写这个版本的变化（中文），再删掉这一行。 -->

## 下载

| 系统 | 文件 |
| --- | --- |
| Windows 10 / 11，x64 | `OpenWeir-{{version}}-windows-x64-setup.exe` |
| Windows 11，ARM64 | `OpenWeir-{{version}}-windows-arm64-setup.exe` |
| macOS 26 及以上，Apple 芯片 / Intel x64 | `OpenWeir-{{version}}-macos-universal.dmg` |

`SHA256SUMS` 是本页每个文件的 SHA-256。已安装的 OpenWeir 在设置页的"关于"里检查更新，下载后核对校验和再打开安装包。

- **Windows**：安装包由 OpenWeir 签名（Azure Artifact Signing），管理员权限确认框里的发布者是 OpenWeir。签名还在积累 SmartScreen 信誉时，从浏览器下载后打开可能仍提示"Windows 已保护你的电脑"：点"更多信息"，确认发布者是 OpenWeir，再点"仍要运行"。
- **macOS**：打开磁盘映像，把 OpenWeir 拖到"应用程序"文件夹。第一次连接时，系统会请求允许 OpenWeir 的系统扩展和 VPN 配置，按提示在"系统设置"里允许。

## 源代码与许可

这个版本的源代码是本仓库的 [`v{{version}}`](https://github.com/openweir/app-desktop/tree/v{{version}}) 标签。服务和扩展链接的 mihomo 是 [openweir/mihomo](https://github.com/openweir/mihomo) 的一个提交，由这个标签的 [`src/core/go.mod`](https://github.com/openweir/app-desktop/blob/v{{version}}/src/core/go.mod) 里的 `replace` 固定；其他 Go 依赖的版本和校验和由同一目录的 `go.mod` 和 `go.sum` 固定，在 `src/core` 里运行 `go mod download` 即可从 Go 模块代理（默认 `proxy.golang.org`）取得它们和 mihomo。GitHub 给每个 Release 自动附带的"Source code"压缩包只有本仓库的代码，不含 mihomo 和其他 Go 依赖。

OpenWeir 以 GPL-3.0 发布，第三方组件的许可证在设置页的"开源许可"里。会离开本机的数据见[隐私说明](https://github.com/openweir/app-desktop/blob/v{{version}}/PRIVACY.md)。
