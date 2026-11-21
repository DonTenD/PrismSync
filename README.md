# PrismSync

检查、下载、缓存并同步游戏资源的 Python 库，同时提供命令行工具。

- **Python 包名**：`prism_sync`
- **CLI 命令**：`prism-sync`
- **项目形态**：可独立使用的 Python 库 + 独立 CLI

PrismSync 首先是一个 Python 库，其次才是 CLI 工具。两者共享同一套业务实现。

## 环境要求

- Python >= 3.11（开发环境使用 3.11，见 `.python-version`）
- [uv](https://docs.astral.sh/uv/) 用于依赖管理与命令执行

## 安装

```bash
# 作为依赖安装
uv add prism-sync

# 在本地开发环境中同步依赖
uv sync
```

## 使用

### Python 库

最小可用流程：读取 Profile → 查询远端资源版本 → 同步 → 检查结果。

```python
from pathlib import Path

from prism_sync import HttpClient, load_profiles, resolve_remote_version, sync_if_needed

# 1. 读 Profile（库不内置游戏版本，必须显式提供文件）
profiles, default_name = load_profiles("./version_profiles.json")
profile = profiles[default_name]

# 2. 查询远端资源版本（一次 gRPC 调用）
remote_version = resolve_remote_version(profile)

# 3. 同步
with HttpClient() as client:
    outcome = sync_if_needed(
        client,
        remote_version,
        game_version=profile.game_version,
        output=Path("./assets"),
    )

# 4. 检查结果
if outcome.result is None:
    # 本地已是最新，没有执行下载
    print("已经是最新版本")
else:
    result = outcome.result
    if result.failed:
        # 有资源下载失败：本次不算成功，版本标记不会写入
        print(f"{result.failed} 个资源下载失败")
    if not outcome.marker_written:
        # 本次运行未完成（取消、部分失败等），下次会重试
        print("版本标记未更新")
    if result.failed == 0 and outcome.marker_written:
        print(f"完成：下载 {result.downloaded}，跳过 {result.skipped}")
```

要点：

- `HttpClient` 支持 `with`，异常路径也会关闭连接。
- `sync_if_needed()` **不会**自己查询远端版本，`remote_version` 必须由调用方传入。
- `outcome.result is None` 表示本地已是最新，没有执行下载。
- `outcome.result.failed > 0` 表示有资源下载失败，此时不要当作成功。
- `outcome.marker_written` 为 `False` 说明本次运行没有完成（取消、部分失败等），
  版本标记不会被更新；只有 `failed == 0` 且 `marker_written` 为 `True` 时才是一次
  完整成功的同步。

如果已知资源版本串，可以跳过 gRPC：

```python
with HttpClient() as client:
    outcome = sync_if_needed(
        client,
        "YOUR_RESOURCE_VERSION",  # "<simple>@<base64>" 形式
        game_version="YOUR_GAME_VERSION",
        output=Path("./assets"),
    )
```

也可以直接用 `Selection` 收窄范围：

```python
from prism_sync import Selection

selection = Selection(types=frozenset({"txt"}), max_size=64 * 1024)
```

> `sync_if_needed()` 是推荐的入口；需要跳过版本比较时用 `sync_resources()`，
> 两者参数一致，只是后者不做本地版本判断。

### 版本配置

PrismSync **不内置任何游戏版本或 VersionHash**：这两项随游戏更新变化，硬编码在库里
只会让调用方拿到一个已经过期的配置。调用方必须显式提供 `VersionProfile`。

```python
from prism_sync import VersionProfile

# 版本信息由调用方负责获取；下面的值是占位符，不可直接连接服务端
profile = VersionProfile(
    game_version="YOUR_GAME_VERSION",
    version_hash="YOUR_VERSION_HASH",
)
```

拿到 profile 后即可向 gRPC 服务查询当前资源版本：

```python
from prism_sync import HttpClient, VersionProfile, resolve_remote_version

profile = VersionProfile(
    game_version="YOUR_GAME_VERSION",
    version_hash="YOUR_VERSION_HASH",
)

with HttpClient() as client:
    remote_version = resolve_remote_version(profile)
```

如果已知资源版本串，可以完全跳过 gRPC，直接同步：

```python
from pathlib import Path

from prism_sync import HttpClient, sync_if_needed

with HttpClient() as client:
    outcome = sync_if_needed(
        client,
        "YOUR_RESOURCE_VERSION",  # "<simple>@<base64>" 形式
        game_version="YOUR_GAME_VERSION",
        output=Path("./assets"),
    )
```

`sync_if_needed()` 不使用 `VersionProfile`，`game_version` 单独传入，并同时用于根
Catalog 的 label CRC。

### Profile 文件

`load_profiles(path)` **必须**指定配置文件路径。文件缺失、无法读取或字段不全都会
抛出 `VersionError`；函数不会回退到任何内置默认值。传 `None` 会抛出 `TypeError`。

```json
{
  "default": "YOUR_GAME_VERSION",
  "profiles": {
    "YOUR_GAME_VERSION": {
      "gameVersion": "YOUR_GAME_VERSION",
      "versionHash": "YOUR_VERSION_HASH"
    }
  }
}
```

### 命令行

安装后 `prism-sync` 会出现在 PATH 中；在仓库内也可以用 `uv run prism-sync` 调用。

```bash
prism-sync --help
prism-sync sync --help
```

#### 基本同步

```bash
prism-sync sync --output ./assets --profile ./version_profiles.json
```

`--output` 是必填项。`--profile` 省略时会依次在当前目录、项目根目录和用户配置
目录中查找 `version_profiles.json`。文档里有多个 profile 时需要 `--profile-name`。

#### 全量同步需要显式确认

资源目录非常大（2.20.0 约有 19.5 万项、合计约 24 GB）。没有筛选条件时 CLI
会拒绝执行，避免误触发全量下载：

```bash
# 被拒绝：未提供任何筛选
prism-sync sync --output ./assets --profile ./version_profiles.json

# 确认要全量同步
prism-sync sync --output ./assets --profile ./version_profiles.json --all
```

#### 筛选参数

| 参数 | 说明 |
|---|---|
| `--types LIST` | 逗号分隔的资源类型，如 `txt,png`（大小写不敏感） |
| `--max-size SIZE` | 跳过大于该值的资源，如 `64KB`、`1.5MB` |
| `--include GLOB` | 仅保留匹配的资源（可重复） |
| `--exclude GLOB` | 排除匹配的资源（可重复） |

`--include` / `--exclude` 使用 `fnmatch` 通配符，匹配 `"<label>.<type>"`、
资源的相对路径或裸 label。`--exclude` 优先于 `--include`。

```bash
# 只同步 64 KB 以下的文本资源
prism-sync sync --output ./assets --profile ./version_profiles.json     --types txt --max-size 64KB

# 只要某类资源，并排除备份文件
prism-sync sync --output ./assets --profile ./version_profiles.json     --include 'music_lyrics_*' --exclude '*.bak'
```

#### 同步选项

| 参数 | 说明 |
|---|---|
| `--jobs N` | 并发下载数，取值 1–16，默认 8 |
| `--force` | 即使本地文件可用也重新下载 |
| `--verify` | 对无法从 Catalog 推导大小的资源重新下载并校验 |
| `--quiet` | 关闭进度与例行输出（错误仍会显示） |
| `--verbose` | 输出每个阶段的细节 |

`--quiet` 与 `--verbose` 互斥。

#### 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功，或已经是最新版本 |
| 2 | 参数错误（含未提供筛选且未加 `--all`） |
| 3 | Profile 缺失、格式错误或名称不存在 |
| 4 | 远端版本查询失败 |
| 5 | 同步过程异常 |
| 6 | 有资源下载失败（部分成功） |
| 130 | 用户中断（已下载的部分会保留，可续传） |

## 开发

```bash
uv sync                                  # 同步环境
uv run ruff check .                      # Lint
uv run ruff format --check .             # 格式检查
uv run mypy                              # 类型检查（strict）
uv run python -m pytest                  # 测试
```

所有工具配置集中在 `pyproject.toml`，它是项目的唯一权威配置入口。
开发规范见 [`AGENTS.md`](AGENTS.md)。

## 项目状态

项目处于早期开发阶段（0.1.0）。Python 库与 `prism-sync sync` 已能完成资源同步；剪裁（prune）、dry-run、MasterData 与本地化等功能尚未迁移。

## 许可证

本项目采用 [MIT License](LICENSE)，版权归 DonTenD 所有（2026）。

完整授权文本见仓库根目录的 [`LICENSE`](LICENSE) 文件。

请注意：MIT License 只覆盖本仓库自身的代码。游戏资源、游戏客户端以及相关第三方
内容的授权与使用条件由其各自的权利人决定，与本项目的许可证无关。
