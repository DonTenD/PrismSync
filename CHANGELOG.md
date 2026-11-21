# Changelog

本文件记录 PrismSync 的显著变更。格式参考
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### 待办

- 真实全量同步（约 24 GB）尚未验证，目前只验证了精选资源的同步路径。

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

## 许可证

本项目采用 MIT License（见 [`LICENSE`](LICENSE)）。
