# PrismSync

检查、下载、缓存并同步游戏资源的 Python 库，同时提供命令行工具。

- **Python 包名**：`prism_sync`
- **CLI 命令**：`prism-sync`
- **项目形态**：可独立使用的 Python 库 + 独立 CLI
- **当前版本**：0.2.0

PrismSync 首先是一个 Python 库，其次才是 CLI 工具。两者共享同一套业务实现，
不维护两套同步逻辑。

## 项目定位

PrismSync 负责「拿到资源」这一件事的完整链路：解析远端版本、获取并解码
Catalog、按条件筛选资源、并发下载、校验完整性、原子写入，并把同步状态
持久化下来供下次增量比对。

它**不内置任何游戏版本或 VersionHash**——这两项随客户端更新变化，硬编码在
库里只会让调用方拿到过期配置。调用方必须显式提供版本配置；如果需要从
Android 设备读取，PrismSync 也提供了配套的调用入口（详见使用文档）。

## 主要能力

| 能力 | 入口 |
|---|---|
| 完整资源同步（增量、续传、校验、原子写入） | `prism_sync.sync_if_needed()` / `prism-sync sync` |
| 只读版本检查 | `--check` |
| 同步预演（预计下载量与磁盘余量） | `--dry-run` |
| 按资源类型统计 | `--list-types` |
| 从 Android 设备读取 `versionHash` 并写入 profile | `profile import-device` |

只读命令不会下载实际资源，也不会写入 `.prism-version.json`、`sync-state.json`
或 Catalog 缓存。

## 环境要求

- Python >= 3.11
- [uv](https://docs.astral.sh/uv/)（依赖管理与命令执行）
- Git

## 获取与初始化

克隆后在**项目根目录**执行初始化：

```bash
git clone https://github.com/DonTenD/PrismSync.git
cd PrismSync
uv sync
```

`uv sync` 会创建 `.venv`、安装依赖，并以可编辑模式安装本项目本身，
因此在项目根目录即可使用 `uv run prism-sync`。

如需在任意目录直接调用 CLI，激活虚拟环境后即可使用 `prism-sync`：

```bash
# Windows PowerShell
.venv/Scripts/Activate.ps1

# macOS / Linux
source .venv/bin/activate
```

如需在其他项目中把 PrismSync 作为库引入，请引用本地路径：

```bash
uv add --editable ../PrismSync
```

## 快速开始

最小可用流程：读取 Profile → 查询远端资源版本 → 同步 → 检查结果。

```python
from pathlib import Path

from prism_sync import HttpClient, load_profiles, resolve_remote_version, sync_if_needed

profiles, default_name = load_profiles("./version_profiles.json")
profile = profiles[default_name]

remote_version = resolve_remote_version(profile)

with HttpClient() as client:
    outcome = sync_if_needed(
        client,
        remote_version,
        game_version=profile.game_version,
        output=Path("./assets"),
    )

if outcome.result is None:
    print("已经是最新版本")
elif outcome.result.failed == 0 and outcome.marker_written:
    print(f"完成：下载 {outcome.result.downloaded}，跳过 {outcome.result.skipped}")
```

对应的 CLI 等价写法：

```bash
prism-sync sync --output ./assets --profile ./version_profiles.json
```

## 文档

完整的安装说明、Python 库用法、CLI 参数、Android 版本接入、退出码与真实环境
验证数据，请见 **[使用指南（usage.md）](usage.md)**。

变更历史见 [CHANGELOG](CHANGELOG.md)。

## 项目状态

当前版本 **0.2.0**。Python 库与 `prism-sync` 命令行工具已能完成完整资源同步，
并提供只读诊断（`--check` / `--dry-run` / `--list-types`）与 Android 版本接入
（`profile import-device`）。

尚未迁移：剪裁（`--prune`）、MasterData 与本地化。

## 许可证

本项目采用 [MIT License](LICENSE)，版权归 DonTenD 所有（2026）。

完整授权文本见仓库根目录的 [`LICENSE`](LICENSE) 文件。

请注意：MIT License 只覆盖本仓库自身的代码。游戏资源、游戏客户端以及相关第三方
内容的授权与使用条件由其各自的权利人决定，与本项目的许可证无关。
