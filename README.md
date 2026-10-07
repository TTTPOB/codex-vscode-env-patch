# Codex VSCode environment patch

发布修复 `DEBUG=release` 环境污染的 Codex VSIX，并提供 Linux x86_64 远端扩展升级脚本。补丁只修正扩展 bundle 中 `debug` 包的 save/load 函数；扩展标识、版本和其他打包内容保持不变。

## 生成指定历史版本

工作流每天检查 stable channel，也支持手动指定 Marketplace 中存在的历史版本：

```bash
gh workflow run patch-release.yml \
  --repo TTTPOB/codex-vscode-env-patch \
  --ref main \
  -f version=26.917.62051
```

也可以在 GitHub Actions 的 **Run workflow → version** 输入版本号。指定版本时忽略 `channel`；留空时选择对应 channel 的最新版本。已完整发布的版本会跳过重复构建。

发布标签为 `<version>-environment-variable-patch`，各平台资产为 `openai.chatgpt-<version>-<platform>-environment-variable-patch.vsix`。手动指定版本的构建不改变 GitHub 的 `latest` 指针，因此补发历史版本不会影响默认升级。

每个平台通过 JavaScript 语法检查、实际 VSIX 中 debug 函数的行为测试、打包内容对比后才发布。无法识别的 bundle 会报错，不做全局字符串替换。

## 升级远端扩展和 NFS 修复版 Codex

在目标 Linux x86_64 主机的 **VSCode integrated terminal** 中执行：

```bash
# Install the latest published patched extension.
bash scripts/update-vscode-codex-ext.sh

# Install a specific historical patched extension.
bash scripts/update-vscode-codex-ext.sh --version 26.917.62051
```

指定版本也可使用 `EXTENSION_VERSION` 环境变量。运行 `--help` 查看目录及 CLI 覆盖参数。

升级脚本从本仓库 Release 下载对应 Linux x64 patched VSIX；指定版本未发布时提示构建命令，不回退到未修补的 Marketplace 包。扩展版本独立于 Codex 运行时版本：两种安装模式都选择 `TTTPOB/codex` 最新可用的正式 `nfs-rust-v*` 发布包，并配套同版本的官方 `codex-code-mode-host`。

运行时安装到 `~/.local/bin`：

- `codex`：NFS 修复版二进制。
- `codex-code-mode-host`：与该二进制同版本的官方 host。
- `codex_`：启动快速失败时重试的 wrapper。

扩展中的 `bin/linux-x86_64/codex` 指向 `codex_`，原始扩展二进制保存在同目录的 `codex-orig`。扩展自带 `codex-package.json` 的版本只作参考，不限制替换版本；旧扩展缺少该元数据不妨碍安装。

升级后重新加载 VSCode 窗口。需在扩展菜单中关闭 Codex 的自动更新，避免 Marketplace 覆盖 patched VSIX。扩展与独立运行时的实际兼容性、模型选择器和 queue/steer 行为需要在 VSCode 中验证。

脚本不修改 VSCode 设置，包括 terminal 环境变量。若已设置 `chatgpt.cliExecutable`，该路径会覆盖扩展默认入口；需要启动重试时可自行将其指向目标机器的 `~/.local/bin/codex_`。

## 本地验证

```bash
python3 patch_release.py --self-test
python3 -m unittest test_patch_release.py
python3 scripts/test_update_vscode_codex_ext.py
```
