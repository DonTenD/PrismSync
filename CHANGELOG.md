# Changelog

本文件记录 PrismSync 的显著变更。格式参考
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.2.0] - 未发布

在 `0.1.0` 的同步内核之上补齐只读诊断与 Android 版本接入能力。
本版本新增的能力全部复用既有下载与校验实现，未引入新的依赖。

### 新增：命令行只读查询

- `--check`：只比较远端版本与本地标记，输出 `remote`/`local`/`status`
  后立即退出，不下载实际资源，也不写 `.prism-version.json`、`sync-state.json`
  或 Catalog 缓存（该命令不访问 Catalog）。更新可用时返回 `1`。
- `--dry-run`：拉取 Catalog 并输出预计的 `added`/`changed`/`unchanged`/
  `removed` 统计、下载量估算与磁盘余量判断，**不写入任何文件**，
  也不会创建输出目录。
- `--list-types`：按资源类型输出数量与字节数，并标出经 `Selection`
  筛选后命中的数量。读取已有 Catalog 缓存但不回写。

三个命令共用 `Console(command=...)` 前缀，各自标注自己的子命令名。

### 新增：Android 版本哈希接入

- `prism_sync.hosttools.extract_android_version_hash()`：调用外部工具
  （如 PrismVersionHashExtractor）读取客户端 `versionHash`，并对返回的
  JSON 施加八条安全判据（40 位小写十六进制、`confidence`、唯一候选、
  `format_valid`、`nul_terminated`、空 `warnings` 等）。任一项不满足即拒绝。
- `prism-sync profile import-device`：把提取到的哈希写入 profile 文档。
  默认要求交互确认，同名条目需显式 `--replace`，`--check-remote` 可选用
  新哈希查询一次远端版本。**该命令不会自动下载任何资源。**
- 提取器通过 `--extractor` / `--extractor-project` 指定，不内置默认路径，
  也不把 Frida 或任何设备工具链引入库依赖。

### 新增：连接与版本参数

- `--resource-version`：直接传入版本串并跳过 gRPC 查询。
- `--game-version`：为根 Catalog 的 label CRC 提供游戏版本。
- HTTP 参数：`--connect-timeout`、`--read-timeout`、`--retries`、
  `--base-url`、`--no-proxy`。
- gRPC 参数：`--grpc-timeout`、`--grpc-attempts`。
- 以上参数缺省时完全沿用库层默认值。

### 改进

- `VersionProfile` 不再内置任何游戏版本或哈希；调用方必须显式提供。
- 未提供筛选条件时仍强制要求 `--all`，该保护对只读命令不适用。
- `Console` 支持子命令标签，输出前缀与所执行的命令一致。

### 修复

- `profile import-device` 的远端查询失败时，明确提示 profile 已保存，
  避免用户误以为整次导入失败而重复执行。
- Windows 下子进程输出按 UTF-8 解码（含 BOM 容忍），不再受控制台代码页影响。

### 真实环境验证

针对游戏版本 **2.20.0** 的实际资源目录（不代表其他版本或未来 Catalog）：

- Catalog：3 个树，**194,507** 个条目。
- **全量同步**：194,507 / 194,507 成功，0 失败、0 跳过、0 续传。
- **落盘**：26,220,322,693 B（`sync-state.json` 记录合计，
  不含 Catalog 缓存与状态文件）。
- **独立 SHA-256 校验**：重新读取全部 24.42 GiB 逐文件计算，
  **194,507 / 194,507 匹配**；缺失、多余、哈希不符、读取失败均为 0。
- 分类型验收：JSON、USM、PNG 各自独立完成真实同步。
- **断点续传**：USM 单文件中断于 48,234,496 / 120,390,752 字节后重跑，
  确认 `Range` 从非零偏移发出并收到 `206 Partial Content`，
  `resumed` 计数为 1，最终文件 SHA-256 与记录一致。

### 尚未验证

- 除 Windows 之外的平台。
- 高于 `--jobs 4` 的并发在超大规模下的表现。
- 其他游戏版本或不同 Catalog 布局。

## [0.1.0] - 未发布

首个版本。Python 库与 `prism-sync` 命令行工具同时提供，两者共享同一套业务实现。

### 库 API

- `HttpClient`：基于 `httpx` 的客户端封装，支持重试、超时、并发连接数与 `with` 上下文管理。
- `resolve_remote_version()` / `GrpcVersionProvider`：通过 gRPC 查询远端资源版本，也支持直接传入已知版本串。
- `check_version()` / `read_local_version()` / `write_local_version()`：本地版本标记的读取、比较与写入。
- `load_profiles()` / `parse_profiles()` / `resolve_profiles_path()` / `save_profiles()`：版本配置文件的读写与查找。
- `discover_catalog_tree()` / `discover_catalog_assets()`：Catalog 递归发现，返回资源与 `assetinfo` 结构。
- `catalog_fingerprint()`：基于完整 Catalog 树的指纹。
- `catalog_entry_to_asset()`：Catalog 条目到可下载资源的转换，含路径安全校验。
- `parse_catalog()` / `decode_catalog()`：Catalog 二进制解析与 AES-CTR + LZ4 解密解压。
- `AssetDownloader` / `download_assets()`：单资源与并发批量下载，支持断点续传、重试、校验与资源转换。
- `Selection`：按资源类型、大小、包含/排除模式筛选资源。
- `SyncStateStore`：同步状态快照的读写。
- `sync_resources()` / `sync_if_needed()`：最小同步编排入口。
- 辅助工具：`crc64` / `root_label_crc` / `varint` / `manifest` 相关函数。

### 资源处理

- 支持五种 Catalog 资源类型：`BundlePlain`、`BundlePaddedStart`、`RawPlain`、
  `RawLz4Compressed`、`RawEncrypted`。
- 下载阶段使用 HTTP `Range` 续传，并校验 `Content-Range` 的起点、终点与总长度。
- 原始字节通过 CDN 的 `x-goog-hash` 或 `ETag` 做 MD5 校验。
- 最终文件经临时文件原子替换写入，失败时不会破坏已有文件。
- 支持协作式取消，中断后保留 `.part.raw` / `.part.json` 供下次续传。

### 命令行

- `prism-sync sync --output DIR`：解析 Profile、查询远端版本并执行同步。
- `--profile` / `--profile-name`：指定配置文件与其中的 profile。
- `--types` / `--max-size` / `--include` / `--exclude`：资源筛选。
- `--jobs` / `--force` / `--verify`：并发数与下载策略。
- `--quiet` / `--verbose`：输出详略（互斥）。
- 未提供任何筛选条件时必须显式加 `--all`，避免误触发全量下载。
- 退出码：`0` 成功、`2` 参数错误、`3` Profile 错误、`4` 版本查询失败、
  `5` 同步异常、`6` 部分资源失败、`130` 用户中断。

### 验证情况

- 单元与集成测试：638 个通过，1 个跳过（Windows 符号链接需要管理员权限）。
- 本地 HTTP 受控集成测试覆盖 Catalog 获取、递归发现、下载、校验与状态写入。
- 真实环境验证（游戏 2.20.0）：
  - gRPC 版本查询返回 `20006500@…`。
  - Catalog 树解析出 3 个 Catalog、194,507 个资源。
  - 实际下载 5 个精选资源，MD5 全部校验通过。
  - 状态快照与版本标记写入成功，二次运行正确判定为已是最新。
  - `force=True` 能强制重新下载。

### 尚未验证

- 真实全量同步（约 24 GB、19.5 万资源）。
- 真实 CDN 返回多值 `x-goog-hash` 的情形。
- 跨进程的中断续传恢复。
- Linux 与 macOS 平台。
- 高并发（`jobs` 接近上限 16）的实际表现。

> 以上 `0.1.0` 的未验证项中，真实全量同步与断点续传已在 `0.2.0`
> 完成验证，详见上方 `[0.2.0]` 章节。

## 许可证

本项目采用 MIT License（见 [`LICENSE`](LICENSE)）。
