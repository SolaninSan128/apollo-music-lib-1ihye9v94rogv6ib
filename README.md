# Harmonia 的八音盒

单文件离线音乐库（GitHub Pages）。仓库里没有账号、令牌或聊天记录。

- 歌单 / **每日推荐** / 歌手；每日按天卡片 → `#/daily/<date>` 详情打分
- 评分自动同步：点一下打分，页面静默 POST 到 Cloudflare Worker（Durable Object 存储 + KV 镜像）；
  换设备/清缓存后自动从服务端恢复；断网时先存本地队列，联网后重试。
  页面角落的小圆点显示同步状态；设置折叠里保留「复制短包 / GitHub 预填 Issue / 立即重试」作后备。
- 前端不含任何密钥；写入有严格校验、大小与频率限制。

Open `index.html` or the Pages URL. iPhone: Share → Add to Home Screen.
