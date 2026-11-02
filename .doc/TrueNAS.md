# TrueNAS 部署

先将项目源码放到 TrueNAS，在 SSH 中进入项目根目录。以下路径使用 `/mnt/pool` 作为示例，请换成自己的数据集路径。

```sh
docker build -t tg-media-archiver:local .
mkdir -p /mnt/pool/apps/tg-media-archiver /mnt/pool/downloads
docker run --rm -it --init \
  -v /mnt/pool/apps/tg-media-archiver:/app \
  -v /mnt/pool/downloads:/app/downloads \
  tg-media-archiver:local configure
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
  tg-media-archiver:local login
```

在 TrueNAS **Apps → Discover Apps → Custom App → Install via YAML** 中粘贴 [`compose.truenas.yaml`](compose.truenas.yaml)，将两个 `/mnt/pool/...` 路径换成刚才使用的数据集路径，再安装。该 YAML 使用已构建的本地镜像，下载内容保存在第二个挂载目录，配置、会话、日志及临时文件保存在第一个挂载目录。

安装后向机器人私聊发送 `/help`，转发一项媒体，再用 `/progress` 查看队列。
