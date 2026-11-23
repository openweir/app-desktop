# Linux 安装包

本目录是 `.deb` 和 `.rpm`（D61）除 `/opt/openweir` 以外装进系统的文件，以及 nfpm 的配置。安装包由
`scripts/build_linux.py` 构建，设计见 [architecture.md](../../docs/architecture.md)"Linux"和决策 D60–D67。

```sh
uv run --group build python scripts/build_linux.py                   # 目录树和两种安装包
uv run --group build python scripts/build_linux.py --skip-packages   # 只到目录树
uv run python scripts/build_linux.py --packages-only --format deb    # 用已有的目录树打包
```

产物在 `build/linux-<x64|arm64>/`：`openweir/`（装成 `/opt/openweir`）、`symbols/`（从 `openweir-ui` 和
`openweir-service` 分出的 DWARF，`build-ids.json` 记录 GNU build ID）和 `OpenWeir-<版本>-linux-<arch>.<deb|rpm>`。
只为本机架构构建：x64 在 `ubuntu:22.04` 里（glibc 2.35），arm64 在 Ubuntu 24.04 上（glibc 2.39，PySide6 aarch64
轮子的下限）。构建机需要 C 编译器、`patchelf`（Nuitka 用）、binutils（`readelf`、`objcopy`）和 Go；nfpm 以
`go run github.com/goreleaser/nfpm/v2/cmd/nfpm@v2.48.0` 运行，也可以用 `--nfpm` 指定一个。

## 装了什么

| 位置 | 内容 |
| --- | --- |
| `/opt/openweir/` | Nuitka standalone 的界面 `openweir-ui`（包的数据在旁边的 `openweir/` 里，所以可执行文件不叫 `openweir`）、`openweir-service`、`libcronet.so`、`geodata/`、`LICENSE` 和 `openweir/licenses/`；`package-format` 写着 `deb` 或 `rpm`，更新时下载同一种包 |
| `/usr/bin/openweir` | 指向 `/opt/openweir/openweir-ui` 的链接 |
| `/usr/lib/systemd/system/` | `openweir.socket`、`openweir.service` |
| `/usr/share/polkit-1/actions/org.openweir.app.policy` | `org.openweir.app.take-over`、`org.openweir.app.install-update`：pkexec 以 root 运行 `/opt/openweir/openweir-service -take-over` 或 `-install-update`，都要管理员验证（`auth_admin`），只对本机的活动会话 |
| `/usr/share/applications/org.openweir.app.desktop` | 由 `org.openweir.app.desktop.in` 生成，`MimeType` 是 `product.json` 的 `urlSchemes` |
| `/usr/share/icons/hicolor/` | `scripts/gen_icons.py` 生成的 `org.openweir.app` 图标 |
| `/usr/lib/NetworkManager/conf.d/90-openweir.conf` | NetworkManager 不管 `openweir*` 设备；用 `+=` 追加，不覆盖别的配置（如 Ubuntu 的 `10-globally-managed-devices.conf`）设的值 |

root 拥有所有文件和目录，`/opt/openweir` 里只有 root 可写：服务只接受这样的目录里的界面。

## 构建时的检查

- 每个 ELF 文件的架构（`e_machine`）都是目标架构；需要的 `GLIBC_`、`GLIBCXX_`、`CXXABI_` 版本不超过构建镜像的
  （x64：2.35、3.4.30、1.3.13；arm64：2.39、3.4.33、1.3.15）。
- 目录树里只有目录、普通文件和不出树的相对链接；模式统一为 0755/0644，不允许组或他人可写、setuid/setgid。
- 窗口离不开的 Qt 插件都在：`platforms` 的 `libqwayland.so`、`libqxcb.so`，Wayland 的 `libxdg-shell.so` 和标题栏装饰
  `libadwaita.so`（要 QtSvg、QtDBus）、`libbradient.so`（D64）。
- 依赖：读每个 ELF 的 `readelf -d`（NEEDED），树里带的库之外，只能是 `system-libraries.toml` 列出的系统库，
  否则构建失败。列出的库不进 Nuitka 的产物；只被这些系统库需要、树里没人用的库（如 libdbus 带来的 libsystemd）
  构建时删掉并打印出来。界面用 ctypes 按 soname 加载的库（X11 窗口激活用的 `libxcb.so.1`）readelf 看不到，在列表里标
  `loaded_by_name`，总是依赖。`.deb` 依赖 Debian 的包名（libc6 带最低的 glibc 版本），`.rpm` 依赖 soname 本身
  （`libfoo.so.1()(64bit)` 和最高的 `GLIBC_` 等版本），再加 `systemd` 和 pkexec。推荐安装彩色 emoji 字体
  （`fonts-noto-color-emoji` / `google-noto-color-emoji-fonts`），Linux 上界面用系统的字体画 emoji。
- 有 `dpkg-deb` 时核对 `.deb` 的控制字段和所有条目归 root。

加新的系统库前，确认支持的最老发行版（x64：Ubuntu 22.04、Debian 12；arm64：Ubuntu 24.04、Debian 13）都有它。

## 维护脚本

`scripts/` 里的四个脚本由 `build_linux.py` 嵌入 `functions.sh` 后交给 nfpm，同时处理 dpkg 的参数（`install`、
`configure <旧版本>`、`remove`、`purge`……）和 rpm 的数字参数。`systemctl` 的 daemon-reload、start、stop、
try-restart 只在 `/run/systemd/system` 存在时运行；enable、disable 在容器和 chroot 里也做（systemctl 离线完成）。

- **安装**：daemon-reload，enable（`openweir.service` 带上 socket），启动 socket 和服务。被删除但未清除的包重装时
  dpkg 也给出旧版本，所以只要两个单元都没启用，就按安装处理；管理员 mask 了单元则不动它们。
- **升级**：替换文件前请运行中的界面退出：按 `/proc/<pid>/exe` 找到它们，以各自的用户
  `runuser -u <用户> -- /opt/openweir/openweir-ui --quit`，最多等 15 秒，剩下的发 SIGTERM；都不断开隧道。
  文件就位后 daemon-reload，`try-restart openweir.service`。
- **删除**：同样请界面退出，停止并禁用两个单元，再 daemon-reload。
- **清除**（只有 deb 有）：删除 `/var/lib/openweir` 和 `/var/cache/openweir`，用户自己的数据目录不动。rpm 删除时
  保留这两个目录。

启动、停止失败不让安装或删除失败（与 debhelper 相同），原因看 systemctl 的输出和日志。

## 测试

`tests/test_build_linux.py` 用假的 ELF 头和 readelf 输出测各项检查和依赖，渲染 nfpm 配置并核对每个条目，
在临时根目录里用假的 `systemctl`、`runuser`、`getent` 运行维护脚本（含一个假装是界面的进程），并在有
`systemd-analyze` 时校验单元文件。真正的安装、启动和升级在 CI 里做：

- `linux-package` job 构建两种架构的安装包（x64 在 `ubuntu:22.04` 容器里，arm64 在 Ubuntu 24.04 runner 上），另用
  `--packages-only` 把同一棵目录树打成下一个修订号的 `.deb`，供升级测试用。构建失败时 `build_linux.py` 把原因
  （每个未知的 soname 各一条）写成 GitHub 的错误注释，不登录也看得到。
- `linux-install` job 运行 `smoke-test.sh`：在 Ubuntu 24.04 虚拟机（x64、arm64，有 systemd）上安装，检查两个单元已启用、
  套接字在监听、服务在运行且日志里有 `service started`、`/opt/openweir` 和 `/var/lib/openweir` 的属主与模式、
  `package-format`、两个 polkit 动作、`.desktop` 文件、`ldd` 找得到 `openweir-ui` 和 Cronet 要的每个库；在测试网段上
  手工造出崩溃的 TUN 留下的 IPv4、IPv6 规则、路由表和设备，`kill -9` 服务后确认 `-cleanup` 恢复原样；以 runner
  用户起一个 offscreen 界面，确认 `--quit` 能让它退出，再起一个 `--background` 的界面，装下一个版本，确认界面按
  要求退出（不是等 15 秒后被 SIGTERM 结束）、服务重启、单元仍启用；最后删除时保留 `/var/lib/openweir`，清除时删掉。
  在 `ubuntu:22.04`、`debian:12`（x64）和 `debian:13`（arm64）容器里装 `.deb`，在 `fedora:41` 容器里装 `.rpm`（`rpm -V`、
  `rpm -q --requires`），都以 root、没有 systemd 运行。每项检查失败都输出一条写明检查名的 `::error::`。
- runner 上做不到的：没有本机活动会话就没有属主，所以不跑真正的 TUN；没有 GNOME/KDE 会话，不测系统代理；
  没有 polkit 代理，不测接管；runner 没有 IPv6 出口。这些在真机上按 [maintenance.md](../../docs/maintenance.md)
  的 Linux 清单检查。
- `tests/test_linux_smoke.py` 用替身命令（apt-get、dpkg、dnf、rpm、systemctl、journalctl、ip、pkaction 和一个假的界面）
  把 `smoke-test.sh` 的三种模式走一遍，并确认失败的检查写出自己的名字。
