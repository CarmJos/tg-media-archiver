# TrueNAS 部署

在 TrueNAS 的 SSH 终端拉取发布镜像并创建持久目录。以下路径使用 `/mnt/pool` 作为示例，请换成自己的数据集路径。

```sh
docker pull ghcr.io/carmjos/tg-media-archiver:1.2.0
mkdir -p /mnt/pool/apps/tg-media-archiver /mnt/pool/downloads
docker run --rm -it --init \
  -v /mnt/pool/apps/tg-media-archiver:/app \
  -v /mnt/pool/downloads:/app/downloads \
  ghcr.io/carmjos/tg-media-archiver:1.2.0 configure
```

编辑 `/mnt/pool/apps/tg-media-archiver/config.yaml` 中的 `proxy`。HTTP 代理示例：

```yaml
proxy:
  scheme: http
  hostname: proxy.example.com
  port: 7890
  username: ""
  password: ""
```

再登录 Telegram。程序会将登录账号设为唯一允许转发媒体的用户。

```sh
docker run --rm -it --init \
  -v /mnt/pool/apps/tg-media-archiver:/app \
  -v /mnt/pool/downloads:/app/downloads \
  ghcr.io/carmjos/tg-media-archiver:1.2.0 login
```

在 TrueNAS **Apps → Discover Apps → Custom App → Install via YAML** 中粘贴 [`compose.truenas.yaml`](compose.truenas.yaml)，将两个 `/mnt/pool/...` 路径换成刚才使用的数据集路径，再安装。YAML 的 `image` 与上述配置、登录命令使用同一 `1.2.0` 版本。下载内容保存在第二个挂载目录，配置、会话、日志及临时文件保存在第一个挂载目录。

安装后向机器人私聊发送 `/help`，转发一项媒体，再用 `/progress` 查看队列。
