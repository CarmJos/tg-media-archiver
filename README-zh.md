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

- **限定用户与会话** — 只接收已登录账号本人在机器人私聊中转发的媒体。
- **持久队列** — 将未完成任务写入 SQLite，重启后恢复。当前文件的传输会从头开始，不续接已下载字节。
- **双层去重** — 先通过 Telegram 媒体标识检查已有文件，再对下载后的内容计算 SHA-256；仅在本地文件仍存在且校验通过时跳过。不同媒体标识的相同内容需要下载后才能比较哈希。
- **手动进度面板** — `/progress` 显示排队数、正在下载的文件、字节数及速度。打开后，新的私聊消息会让面板重新移至聊天末尾。
- **消息整理** — 文件保存或确认重复后，删除与机器人的私聊转发消息；多项任务全部成功处理后发送一次完成通知。
- **共享目录权限** — 新文件沿用目标目录的继承 ACL，方便 NAS 授权用户管理文件。

## 安装

### Docker Compose

GHCR 提供 `linux/amd64` 和 `linux/arm64` 镜像。示例使用正式版 `1.0.0`；也可将标签改为 `latest`，跟随最新正式版。

将 [`compose.yaml`](compose.yaml) 下载到部署目录：

```sh
mkdir -p tg-media-archiver
cd tg-media-archiver
curl -fsSL https://raw.githubusercontent.com/CarmJos/tg-media-archiver/master/compose.yaml -o compose.yaml
```

服务使用 `image: ghcr.io/carmjos/tg-media-archiver:1.0.0`。拉取镜像并配置机器人：

```sh
docker compose pull
docker compose run --rm archiver configure
```

`configure` 会以隐藏输入方式读取 API ID、API Hash 和 Bot Token。随后编辑 `data/config.yaml` 的 `proxy` 配置，再运行：

```sh
docker compose run --rm archiver login
docker compose up -d
```

`login` 会用 Telegram 个人账号确定唯一授权用户，并授权机器人会话。归档服务运行时通过机器人会话接收消息。示例 Compose 将 `./data` 挂载到容器 `/app`，将 `./downloads` 挂载到 `/app/downloads`。在 TrueNAS 中通过 GHCR 镜像安装 Custom App 的步骤见 [部署说明](.doc/TrueNAS.md)。

升级时，将 `compose.yaml` 中的镜像标签改为目标版本，再执行：

```sh
docker compose pull
docker compose up -d
```

现有部署可继续使用原来的 `config.yaml`、`sessions/` 及含有 `files.db` 的下载目录。程序读取仍需使用的配置并恢复未完成队列；旧的 Web、群组下载和云端上传配置会被忽略。中断的文件传输会从头开始。

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

[`Docker` 工作流](.github/workflows/docker.yml) 仅在 GitHub Release 发布时构建并推送 `linux/amd64` 和 `linux/arm64` 镜像到 `ghcr.io/carmjos/tg-media-archiver`。Release `v1.0.0` 会生成 `v1.0.0`、`1.0.0` 和提交 SHA 镜像标签；正式版同时更新 `latest`，预发布版保持 `latest` 不变。推送 `main`、`master`，提交 PR 或手动运行工作流时，仅验证构建。

工作流使用仓库自带的 `GITHUB_TOKEN`，无需额外配置仓库密码。

### 验证与依赖更新

[`Validate` 工作流](.github/workflows/validate.yml) 在 Python 3.10、3.11 和 3.12 上构建并安装 Python 包，离线验证模块导入、命令行、配置、资源文件、文件发布、去重和持久队列恢复。

[Renovate](renovate.json) 更新依赖，包括主版本、GitHub Actions 和 Pyrogram 分支；Python 运行时版本保持不变。依赖 PR 在必需的 Docker 构建与 Python 验证均通过后自动合并。

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
| `file_name_prefix`、`file_name_prefix_split` | 保存文件的命名规则 |
| `media_types`、`file_formats` | 媒体类型与文件格式设置 |
| `proxy` | Telegram 客户端代理 |
| `max_download_task`、`max_concurrent_transmissions` | 下载工作器数与 Telegram 同时传输数 |
| `start_timeout`、`log_level` | 连接超时与日志等级 |

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
├── .github/scripts/      # Python 离线验证
├── .github/workflows/    # 验证、Docker 构建与发布
├── config.example.yaml
├── Dockerfile
├── compose.yaml
├── pyproject.toml
└── LICENSE
```

## 开源许可

本项目采用 [MIT 许可证](LICENSE)，基于 [telegram_media_downloader 的 MIT 许可](https://github.com/tangyoha/telegram_media_downloader/blob/fe7ccdc2cc5fce5b04a464bc66cfb1266bf7ef45/LICENSE) 开发。原作者版权署名保留在本项目许可证中。

## 法律声明与免责声明

本项目发布旨在供源代码学习、技术研究与交流。项目的公开提供不构成对违法行为或侵害第三方合法权益行为的授权。

使用者应自行遵守其行为所适用的法律法规、相关平台的服务条款，并尊重第三方合法权益。在访问、下载、存储、处理或分享任何内容前，使用者应取得必要授权或具备其他合法依据，并履行著作权、隐私及个人信息保护等方面的义务。

本软件依据 [MIT 许可证](LICENSE) 按“现状”提供。在适用法律允许的范围内，作者及贡献者不对使用者的独立行为，以及使用或不当使用本软件所引发的索赔、损失或损害承担责任。

本说明不变更 MIT 许可证授予的权利，也不排除或限制依法不得免除或限制的义务与责任。
