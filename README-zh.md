<div align="center">

<img src=".doc/images/banner.png" alt="TG Media Archiver banner" width="100%" />

![Python](https://img.shields.io/badge/Python-3.10--3.12-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20Docker-lightgrey)

README 语言版本 [ [English](README.md) | [**简体中文**](README-zh.md) ]

</div>

# **TG Media Archiver**

<img src=".doc/images/logo.png" width="150" alt="TG Media Archiver logo" align="right" />

_**将转发给机器人的 Telegram 媒体归档到自己的存储空间。**_

TG Media Archiver 基于 [telegram_media_downloader](https://github.com/tangyoha/telegram_media_downloader)。授权用户在机器人私聊中转发图片、视频或文件，程序会将其保存到指定目录；混合相册中的每项媒体分别进入下载队列。

## 功能特性

- **限定用户与会话** — 只接收授权用户在机器人私聊中转发的媒体。群组自动下载可关闭。
- **持久队列** — 将未完成任务写入 SQLite，重启后恢复。当前文件的传输会从头开始，不续接已下载字节。
- **双层去重** — 先通过 Telegram 媒体标识检查已有文件，再对下载后的内容计算 SHA-256；仅在本地文件仍存在且校验通过时跳过。不同媒体标识的相同内容需要下载后才能比较哈希。
- **手动进度面板** — `/progress` 显示排队数、正在下载的文件、字节数及速度。打开后，新的私聊消息会让面板重新移至聊天末尾。
- **消息整理** — 文件保存或确认重复后，删除与机器人的私聊转发消息；多项任务全部成功处理后发送一次完成通知。
- **共享目录权限** — 新文件沿用目标目录的继承 ACL，方便 NAS 授权用户管理文件。

## 安装

### Docker Compose

在项目目录执行：

```sh
docker compose build
docker compose run --rm archiver configure
```

`configure` 会以隐藏输入方式读取 API ID、API Hash 和 Bot Token。随后编辑 `data/config.yaml` 的 `proxy` 配置，再运行：

```sh
docker compose run --rm archiver login
docker compose up -d
```

`login` 会登录 Telegram 用户账号，并将该账号设为唯一授权用户。示例 Compose 将 `./data` 挂载到容器 `/app`，将 `./downloads` 挂载到 `/app/downloads`。TrueNAS 的镜像构建与 Custom App YAML 见 [部署说明](.doc/TrueNAS.md)。

### 本地 Python

需要 Python 3.10 至 3.12：

```sh
python -m pip install .
tg-media-archiver --workdir ./data configure
```

编辑 `data/config.yaml` 的 `proxy` 配置后继续：

```sh
tg-media-archiver --workdir ./data login
tg-media-archiver --workdir ./data run
```

也可用 `python -m tg_media_archiver` 调用同一命令行。`--workdir` 默认当前目录；配置、会话、日志和临时文件都相对该目录。API ID 和 API Hash 在 [Telegram API Development Tools](https://my.telegram.org/apps) 创建，Bot Token 由 [@BotFather](https://t.me/BotFather) 创建。

### 自动构建 Docker 镜像

[`Docker` 工作流](.github/workflows/docker.yml) 在推送 `main`、`master` 或 `v*` 标签时构建 `linux/amd64` 和 `linux/arm64` 镜像，并发布到 `ghcr.io/<仓库所有者>/tg-media-archiver`。默认分支提供 `latest` 标签，版本标签生成对应版本号。PR 仅验证构建，也可在 GitHub Actions 中手动运行。

工作流使用仓库自带的 `GITHUB_TOKEN`，无需额外配置仓库密码。

## 快速开始

1. 完成 `configure`，按需编辑代理配置，然后执行 `login`。默认将文件保存到工作目录的 `downloads/`，并按 `年_月` 建立子目录。
2. 启动服务，向机器人私聊发送 `/help`，随后转发图片、视频、文件或混合相册。
3. 发送 `/progress` 查看队列；点击“关闭进度”或发送 `/progress off` 删除面板并停止更新。

示例配置见 [`config.example.yaml`](config.example.yaml)。运行配置保存在工作目录，包含凭据，请将工作目录留在私有存储中。

## 配置参考

| 配置项 | 用途 |
|---|---|
| `api_id`、`api_hash`、`bot_token` | Telegram 应用与机器人凭据 |
| `allowed_user_ids` | 允许转发媒体的 Telegram 数字用户 ID；`login` 自动填入当前账号 |
| `save_path` | 归档目录，`files.db` 与下载文件同目录 |
| `file_path_prefix`、`date_format` | 子目录规则；默认按 `%Y_%m` 归档 |
| `media_types`、`file_formats` | 媒体类型与文件格式设置 |
| `proxy` | Telegram 客户端代理 |
| `max_download_task`、`max_concurrent_transmissions` | 下载工作器数与 Telegram 同时传输数 |
| `chat` | 群组或频道历史下载设置；设为 `[]` 即关闭 |
| `web_enabled` | 是否启动 Web 服务，默认 `false` |

## 机器人指令

| 指令 | 作用 |
|---|---|
| `/start`、`/help` | 查看使用说明 |
| `/progress` | 打开或刷新进度面板 |
| `/progress off` | 删除进度面板并停止更新 |

## 项目结构

```text
tg-media-archiver/
├── src/tg_media_archiver/
│   ├── cli.py              # 命令行入口
│   ├── configure.py        # 隐藏输入配置
│   ├── login.py            # Telegram 用户登录
│   ├── media_downloader.py # 下载调度
│   ├── module/             # 机器人、队列、去重、进度面板
│   └── utils/              # 文件与日志工具
├── .doc/                 # 品牌图片与 TrueNAS 部署示例
├── .github/workflows/    # Docker 自动构建与发布
├── config.example.yaml
├── Dockerfile
├── compose.yaml
├── pyproject.toml
└── LICENSE
```

## 开源许可

本项目采用 [MIT 许可证](LICENSE)。基于 [telegram_media_downloader 的 MIT 许可](https://github.com/tangyoha/telegram_media_downloader/blob/fe7ccdc2cc5fce5b04a464bc66cfb1266bf7ef45/LICENSE) 开发；内置的 [CryptoJS](https://github.com/brix/crypto-js/blob/4.2.0/LICENSE) 也采用 MIT 许可。原作者版权署名保留在本项目许可证及内置组件源码中。
