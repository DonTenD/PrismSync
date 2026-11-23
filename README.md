# PrismSync

检查、下载、缓存并同步游戏资源的 Python 库，同时提供命令行工具。

- **Python 包名**：`prism_sync`
- **CLI 命令**：`prism-sync`
- **项目形态**：可独立使用的 Python 库 + 独立 CLI

PrismSync 首先是一个 Python 库，其次才是 CLI 工具。两者共享同一套业务实现。

## 主要能力

| 能力 | 入口 |
|---|---|
| 完整资源同步（增量、续传、校验、原子写入） | `prism_sync.sync_if_needed()` / `prism-sync sync` |
| 只读版本检查 | `--check` |
| 同步预演（预计下载量与磁盘余量） | `--dry-run` |
| 按资源类型统计 | `--list-types` |
| 从 Android 设备读取 `versionHash` 并写入 profile | `profile import-device` |

只读命令不会下载实际资源，也不会写入 `.prism-version.json`、`sync-state.json` 或 Catalog 缓存。
`--check` 不访问 Catalog；`--dry-run` / `--list-types` 会读取已有 Catalog 缓存，缓存缺失时在线获取但不回写。

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

### 从 Android 设备读取 VersionHash

`versionHash` 是客户端内置的编译期常量，藏在加密的 `global-metadata.dat` 里，
只能从**运行中的游戏进程**内存中读出。这项取证需要 root、Frida 与一台真机，
因此**不在本库的职责范围内**；PrismSync 只通过 `subprocess` 调用一个外部提取器，
并严格校验它的输出。

#### 前置条件

以下条件全部由用户自己准备，PrismSync 不会代劳：

- 单独安装 [PrismVersionHashExtractor](https://github.com/)（或等价工具）。
- Android 设备已用 USB 连接，`adb devices` 能看到它。
- 设备**已 root**，并且 `frida-server` 已在运行。
  **未 root 的设备无法使用**：Frida 在 jailed Android 上只能注入 debuggable 应用。
- 游戏**已启动**并完成了 IL2CPP 初始化（通常需要等 30–40 秒）。

PrismSync 不负责安装 Frida、不负责启动游戏，也不管理设备状态。

#### 调用方式

```python
from prism_sync.hosttools import extract_android_version_hash

result = extract_android_version_hash(
    [
        "uv",
        "run",
        "--project",
        "C:/path/to/PrismVersionHashExtractor",
        "prism-version-hash",
    ],
    timeout=300.0,
)

print(result.version_hash)  # d157435e...（40 位小写十六进制）
print(result.game_version)  # "2.20.0"，读取不到时为 None
print(result.device)  # "Redmi K30 Pro Zoom Edition"，可能为 None
```

拿到哈希后，填进 `VersionProfile` 或 `version_profiles.json` 即可走正常流程：

```python
profile = VersionProfile(
    game_version=result.game_version, version_hash=result.version_hash
)
```

#### 校验规则

只有**同时**满足下列条件，结果才会被接受，否则抛出 `ExtractorInconclusive`：

| 条件 | 说明 |
|---|---|
| `version_hash` 是字符串 | 类型正确 |
| 严格匹配 `^[0-9a-f]{40}$` | 40 位小写十六进制 |
| `confidence == "high"` | 提取器自身的置信度 |
| `candidates` 长度为 1 | 不存在多个候选 |
| 候选 `value` 与 `version_hash` 一致 | 两份数据互相印证 |
| 候选 `format_valid is True` | 格式校验通过 |
| 候选 `nul_terminated is True` | 是独立 NUL 终止字符串 |
| `warnings` 为空 | 提取器没有报告任何告警 |

**退出码 0 不代表结果有效**，仍然会执行上述全部校验。任何一项不满足都会拒绝：

- 启动失败、超时、非零退出码 → `ExtractorUnavailable`
- 输出为空、不是合法 JSON、顶层不是对象、任一校验失败 → `ExtractorInconclusive`

`game_version` 与 `device` 允许缺失或为 `null`，此时返回 `None`，不影响哈希的有效性。

#### 命令行：导入到 profile 文件

除了在 Python 里调用，也可以直接用 CLI 把提取结果写入 profile 文档：

```bash
# 指定提取器项目目录（最简形式）
prism-sync profile import-device     --profile ./version_profiles.json     --extractor-project "C:/path/to/PrismVersionHashExtractor"

# 或显式给出完整命令（每个参数一次 --extractor）
prism-sync profile import-device     --profile ./version_profiles.json     --extractor uv --extractor run     --extractor prism-version-hash
```

执行后依次：

1. 调用提取器（用**真实设备**，不使用 mock）；
2. 校验哈希与游戏版本（`confidence`、`candidates`、`warnings` 等由
   `extract_android_version_hash()` 负责）；
3. 显示设备、游戏版本、版本哈希和将要使用的 profile 名；
4. **询问确认**，只有在回答 `y` 后才写入文件。

常用参数：

| 参数 | 说明 |
|---|---|
| `--profile FILE` | 要更新的 profile 文档（不存在则创建），必填 |
| `--name NAME` | profile 名，默认用提取到的游戏版本 |
| `--extractor-project DIR` | 等价于 `uv run --project DIR prism-version-hash` |
| `--extractor CMD` | 直接给出命令，可重复以传多个参数 |
| `--timeout SECONDS` | 提取器超时，默认 300 |
| `--replace` | 允许覆盖同名 profile |
| `--yes` | 跳过确认提示 |
| `--check-remote` | 额外用新哈希查询一次 gRPC，显示 resource version |

#### 冲突与安全行为

| 情况 | 行为 |
|---|---|
| 同名 profile 已存在 | **拒绝**，提示使用 `--replace`；文件不被修改 |
| 哈希已存在于其他 profile | **警告**但仍写入（不阻断） |
| `game_version` 为 `null` 或空 | **拒绝**，不写入文件、不发起 gRPC |
| 提取器不可用 / 结果不确定 | **拒绝**，退出码 `4` |
| 用户取消确认 | 退出码 `2`，文件不被修改 |
| 写入失败 | 退出码 `3`，`save_profiles()` 使用原子写入，不会留下半截文件 |
| 已有其他 profile | **保留**，只追加或覆盖目标条目；已存在的 `default` 不被改动 |

#### 与 `sync` 的关系

**Profile import 与远端版本查询是两个独立操作**：

- `profile import-device` 只**提取并保存** `VersionProfile`；
- 加 `--check-remote` 才会额外查询一次 gRPC 并显示 `resourceVersion`；
- **`sync` 命令永远不会自动启动提取器**，也不会因为导入而自动下载任何资源。

#### `--check-remote` 的执行顺序与失败语义

命令按以下固定顺序执行：

1. 调用提取器取得 hash；
2. **写入 profile 文件**（原子写入）；
3. 若给了 `--check-remote`，才查询 gRPC。

因此 **profile 的保存早于远端查询**。如果第 3 步失败：

```
prism-sync profile import-device: remote lookup failed: ...
prism-sync profile import-device: warning: profile 'NAME' was already saved
    to PATH; the remote lookup failure does not affect it
```

| 项 | 行为 |
|---|---|
| 退出码 | **4**（远端版本查询失败） |
| profile 文件 | **已保存，不回滚** |
| 是否重复写入 | ❌ 否 |
| 用户该做什么 | 直接检查已保存的 profile 即可，**无需因为查询失败而重新导入** |

远端查询失败**不等于**导入失败：导入已经成功，只是附加的查询没成功。

#### 当前范围

`extract_android_version_hash()` 本身不写任何文件；写入只发生在
`prism-sync profile import-device` 且用户确认之后。二者都**不会**：

- 修改 `.prism-version.json` 或 `sync-state.json`；
- 执行资源下载或同步。

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

#### 资源版本直传

`--resource-version` 直接使用给定的资源版本串（`"<simple>@<base64>"` 形式），
**跳过 gRPC 版本查询**。适合离线环境、复用历史版本，或已经知道版本号时。

```bash
prism-sync sync --output ./assets     --resource-version "YOUR_RESOURCE_VERSION"     --game-version "YOUR_GAME_VERSION"     --types txt --max-size 64KB
```

- 传入非法版本串会以退出码 `2` 结束。
- 该参数本身不要求 Profile 文件存在；不提供 `--profile` 时可以单独工作。
- 但根 Catalog 的路径由 `game_version` 参与计算，**通常必须同时提供
  `--game-version`**。缺少时会在标准错误输出上给出警告，随后很可能因
  Catalog 路径不匹配而失败。
- 如果显式提供了 `--profile` / `--profile-name`，它们仍会被校验，不会被忽略：
  文件不存在或名称未知都会以退出码 `3` 结束。
- `--game-version` 的优先级高于 Profile 中的 `gameVersion`。

#### 连接参数

以下参数覆盖库层的网络默认值；**不提供时完全沿用库默认**。

| 参数 | 说明 | 库默认 |
|---|---|---|
| `--connect-timeout SECONDS` | HTTP 连接超时 | 10.0 |
| `--read-timeout SECONDS` | HTTP 读写超时 | 60.0 |
| `--retries N` | HTTP 请求重试次数（非负整数） | 3 |
| `--base-url URL` | 覆盖 CDN 根地址，必须是 `http`/`https` 绝对地址 | 内置 CDN |
| `--no-proxy` | 忽略代理环境变量（对应 `trust_env=False`） | 使用环境代理 |
| `--grpc-timeout SECONDS` | gRPC 版本查询超时 | 10.0 |
| `--grpc-attempts N` | gRPC 版本查询尝试次数（至少 1） | 3 |

时间类参数必须为正数，`--retries` 不得为负，违反时以退出码 `2` 结束。

**HTTP retries 与 gRPC attempts 是两套独立设置**：前者控制资源下载的重试，
后者控制版本查询的重试，互不影响。

使用 `--resource-version` 时不会发生 gRPC 查询，因此
`--grpc-timeout` / `--grpc-attempts` 在该次运行中不起作用。

#### 预演同步：--dry-run

`--dry-run` 分析远端 Catalog 与本地状态，输出**预计的同步计划**，
不下载、不删除、不覆盖任何文件，也不写 `sync-state.json` 或
`.prism-version.json`，不创建输出目录。

```bash
prism-sync sync --dry-run --all --output ./assets     --resource-version "YOUR_RESOURCE_VERSION" --game-version "YOUR_GAME_VERSION"     --types txt --max-size 64KB
```

输出字段：

| 字段 | 含义 |
|---|---|
| `catalog assets` | Catalog 中的全部资源数（未筛选） |
| `selected` | 经 Selection 筛选后纳入计划的资源数 |
| `added` | 远端有、本地无 —— 预计新增下载 |
| `changed` | 远端有、本地也有但内容不符（大小等校验不通过，或 `--force`） |
| `unchanged` | 本地文件已符合远端要求，不会下载 |
| `removed` | **`sync-state.json` 曾记录过**、但当前 Catalog 已不再包含的条目 |
| `downloads` | 预计下载数量 = `added + changed` |
| `estimated download` | 预计下载数据量 = `added` 与 `changed` 的字节数之和 |
| `free space` | 目标文件系统的可用空间；无法取得时显示 `unavailable` |
| `enough space` | 可用空间是否不少于预计下载量；未知时显示 `unknown` |

要点：

- **Selection 只决定下载范围**：`--types` / `--include` / `--exclude` /
  `--max-size` 只影响 `selected` 与 `added`/`changed`/`unchanged`，
  **不会让未被选中的远端资源变成 `removed`**。`removed` 始终基于完整
  Catalog 与本地记录比较。
- **`removed` 比较的是历史 `sync-state.json` 条目与当前完整 Catalog，
  不扫描输出目录。** 由此产生几条重要推论：
  - 只有此前被 `prism-sync` 记录过的路径才可能被计为 `removed`；
  - 用户手动放入输出目录、从未被记录过的文件**不会**参与统计；
  - **不保证对应文件在磁盘上实际存在**。state 中的
    `status: failed` 条目代表当初下载失败，可能从未生成过最终文件；
  - **不保证条目曾经成功下载**。任何被记录后又从 Catalog 消失的条目都会被
    计入，无论其 `status` 是成功、`skipped` 还是 `failed`；
  - 因此该数字是「工具跟踪范围内已失效的条目数」，既不是磁盘孤儿文件
    总数，也不是成功下载过的文件数。
- **`removed` 不会真的删除任何文件**。它是信息性的：本批次尚未提供
  `--prune`。文件（如果存在）会一直留在磁盘上。
- **`removed` 不是永久历史记录，会随同步生命周期清零。** 正常同步成功后，
  同步实现会依据当前完整 Catalog 重写 state，并丢弃已不在 Catalog 中的旧
  路径。因此：
  - 远端删除资源后、下一次成功同步之前，dry-run 可能统计到它；
  - 下一次成功同步之后，这条旧记录会被清除；
  - 再之后的 dry-run 中 `removed` 可能归零。

  这是当前 sync-state 生命周期的既有行为，不是 dry-run 的缺陷。
- **`unchanged` 不会计入下载量**，`removed` 也不计入。
- `--dry-run` 沿用现有全量保护：未提供筛选参数时仍需显式 `--all`。
- `--dry-run` 不能与 `--check` 或 `--list-types` 同时使用（退出码 `2`）。
- 发现 `added`/`changed`/`removed` **不算失败**，正常完成返回 `0`。
- 磁盘空间**只是估算**：断点续传临时文件、并发下载和压缩/解密后的
  实际落盘量都可能与之不同，不保证下载一定成功。若目标目录尚不存在，
  可用空间取自最近的已存在上级目录（不会为此创建目录）。
- **校验依据**：判断 `changed`/`unchanged` 时默认只比较**文件大小**；
  `--deep` 追加 **SHA-256** 比对；对于无法从 Catalog 推导输出大小的资源
  （LZ4/加密），`--verify` 会保守地判为 `changed`。dry-run 不使用 MD5 ——
  MD5 只在真正下载时用于校验传输完整性（对比 `x-goog-hash`/`ETag`），
  不参与本地文件分类。

#### 只读查询：--check 与 --list-types

两个命令都**不下载任何资源**，也不会创建输出目录、`.prism-version.json`
或 `sync-state.json`。

`--check` 比较远端版本与本地标记后立即退出：

```bash
prism-sync sync --check --output ./assets     --resource-version "YOUR_RESOURCE_VERSION" --game-version "YOUR_GAME_VERSION"
```

输出远端版本、本地版本与 `status: up-to-date` / `update-available`。
退出码：`0` 已是最新，`1` 有可用更新，`2` 参数错误，`4` 远端查询失败。
无本地标记时按 `update-available` 处理。

`--list-types` 拉取 Catalog 并打印每种类型的数量与字节数，按类型名排序：

```bash
prism-sync sync --list-types --output ./assets     --resource-version "YOUR_RESOURCE_VERSION" --game-version "YOUR_GAME_VERSION"     --types txt
```

表格列含义：

| 列 | 含义 |
|---|---|
| `type` | 资源类型名 |
| `count` | 该类型在 **Catalog 中全部资源**的数量 |
| `bytes` | 该类型在 **Catalog 中全部资源**的字节数 |
| `selected` | 经 Selection（`--types` / `--include` / `--exclude` / `--max-size`）筛选后**被选中**的数量 |

末行 `total` 是全部资源的数量与字节数合计。未被选中的资源仍计入 `count` 与
`bytes`，只是不计入 `selected`。

两个命令的约束：

- **不执行任何资源下载**，也不创建输出目录、`.prism-version.json` 或
  `sync-state.json`。
- **不需要 `--all`**（不存在「未筛选即全量」的风险），加上它也不会被拒绝。
- **不支持 `--force` 和显式 `--jobs`**：这两个参数只对下载有意义，指定时以
  退出码 `2` 结束。
- `--check` **是全局版本检查**，因此**不支持资源筛选参数**（`--types` /
  `--include` / `--exclude` / `--max-size`）；指定任一筛选参数会以退出码 `2`
  结束，而不是被静默忽略。
- `--check` 与 `--list-types` 不能同时使用，组合时以退出码 `2` 结束。
- `--list-types` 会优先读取已有 Catalog 缓存；缓存缺失时在线下载但
  **不写回缓存**（`writable=False`），因此不会污染缓存目录。

#### 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功，或已经是最新版本 |
| 1 | `--check` 发现有可用更新 |
| 2 | 参数错误（含未提供筛选且未加 `--all`） |
| 3 | Profile 缺失、格式错误、名称不存在，或同名 profile 冲突 |
| 4 | 远端版本查询失败；提取器不可用或结果不确定；`profile import-device` 缺少游戏版本 |
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

当前版本 **0.2.0**。Python 库与 `prism-sync` 命令行工具已能完成完整资源同步，
并提供只读诊断（`--check` / `--dry-run` / `--list-types`）与 Android 版本接入
（`profile import-device`）。

尚未迁移：剪裁（`--prune`）、MasterData 与本地化。

### 真实环境验证情况

以下数据来自对游戏版本 **2.20.0** 的完整同步实测，只描述这一次的结果，
**不代表其他版本、其他 Catalog 布局或未来资源目录**，也不是网络流量或性能基准：

| 项 | 结果 |
|---|---|
| Catalog 条目 | 194,507（3 个 Catalog 树） |
| 全量同步 | 194,507 / 194,507 成功，0 失败 |
| 落盘资源 | 26,220,322,693 B |
| 独立 SHA-256 校验 | 194,507 / 194,507 匹配，缺失/多余/不符均为 0 |
| 分类型验收 | JSON、USM、PNG 各自完成真实同步 |
| 断点续传 | USM 单文件中断后续传，确认 `Range` 非零偏移与 `206` 响应 |

各资源类型的落盘体积与 Catalog 记录并不总是一致：多数类型是原样透传或
仅剥离固定前缀。`json` 类型的条目包含 `RawLz4Compressed`、`RawEncrypted`
和 `BundlePaddedStart` 等 ResourceType：

- `RawLz4Compressed` —— LZ4 解压；
- `RawEncrypted` —— 先 AES 解密，再 LZ4 解压；
- `BundlePaddedStart` —— 剥离固定的 4 字节前缀。

其中含压缩的条目在解压后体积明显增加。本机对 2.20.0 的全部 `json` 条目
实测，落盘总体积约为 Catalog 记录的 5.2 倍。该比例是全部 `json` 条目的
聚合结果，并非单个文件的膨胀比例，也不适用于其他资源类型或游戏版本。

## 许可证

本项目采用 [MIT License](LICENSE)，版权归 DonTenD 所有（2026）。

完整授权文本见仓库根目录的 [`LICENSE`](LICENSE) 文件。

请注意：MIT License 只覆盖本仓库自身的代码。游戏资源、游戏客户端以及相关第三方
内容的授权与使用条件由其各自的权利人决定，与本项目的许可证无关。
