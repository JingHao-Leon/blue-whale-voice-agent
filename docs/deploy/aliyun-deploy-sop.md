# 阿里云 ECS 部署 SOP

> 等 ICP 备案过了，再做这步。**全程 SSH 即可，不需要 GUI**。
> 你的代码已经有 `Dockerfile` + `docker-compose.yml`，10 分钟跑起来。

---

## 0. 本地准备（一次性）

```bash
# 在你 Mac 上确认代码可推（不需要真推，scp 也行）
cd ~/Documents/Minimax_voice_agent/voice_agent

# 打包源码（不含 .venv / .git / .db）
tar czf /tmp/voice_agent.tar.gz \
  --exclude='.venv' --exclude='.git' --exclude='data/*.db' \
  --exclude='__pycache__' --exclude='.pytest_cache' .
ls -lh /tmp/voice_agent.tar.gz
```

## 1. 拿到 ECS 后——SSH 进去

阿里云控制台 → ECS → 实例 → **重置实例密码**（或创建时设）。

Mac 终端：
```bash
ssh root@<ECS 公网 IP>
# 第一次会问 yes/no，输 yes
# 输密码（你刚设的）
```

## 2. 装 Docker（一次性，5 分钟）

```bash
# Ubuntu 22.04 一键装 Docker
curl -fsSL https://get.docker.com | sh

# 验证
docker --version
docker compose version
```

## 3. 上传代码

```bash
# 在你 Mac 上
scp /tmp/voice_agent.tar.gz root@<ECS_IP>:/root/

# 在 ECS 上
mkdir -p /opt/voice_agent
cd /opt/voice_agent
tar xzf /root/voice_agent.tar.gz
ls
```

## 4. 配 .env

```bash
# 在 ECS 上
cd /opt/voice_agent
cp .env.example .env
nano .env   # 或 vim
```

填入（**生产级密钥**，别用 dev 的）：
```bash
DASHSCOPE_API_KEY=sk-你的真实key
WECHAT_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key
PARK_NAME=蓝色鲸鱼科技园
PUBLIC_BASE_URL=https://voice.你的域名.cn   # 跟域名一致
```

## 5. 起服务

```bash
cd /opt/voice_agent
docker compose up -d
docker compose logs -f    # 看到 "Voice agent started" + "boot greeting pre-synth" 就 OK
```

## 6. 配 nginx + Let's Encrypt

```bash
# 装 nginx + certbot
apt update
apt install -y nginx certbot python3-certbot-nginx

# 申请证书（必须先解析域名到 ECS IP）
certbot --nginx -d voice.你的域名.cn

# 自动配 nginx + 自动续期
```

certbot 会自动改 `/etc/nginx/sites-enabled/default`，重启 nginx：
```bash
nginx -t
systemctl restart nginx
```

## 7. nginx 反代 config（certbot 没自动建的话手写）

`/etc/nginx/sites-enabled/voice`：
```nginx
server {
    listen 443 ssl;
    server_name voice.你的域名.cn;

    ssl_certificate     /etc/letsencrypt/live/voice.你的域名.cn/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/voice.你的域名.cn/privkey.pem;

    # WebSocket upgrade 头（关键！）
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # WebSocket 支持
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}

server {
    listen 80;
    server_name voice.你的域名.cn;
    return 301 https://$host$request_uri;
}
```

```bash
nginx -t && systemctl reload nginx
```

## 8. 防火墙开 80/443

阿里云控制台 → ECS → **安全组** → 配置规则 → 放行：
- TCP 80
- TCP 443
- TCP 8000（仅本机调试用，可不开）

## 9. 改小程序里的 WS_URL

`/Users/ahs/WeChatProjects/miniprogram-1/miniprogram/pages/voice-checkin/voice-checkin.js`：
```js
const WS_URL = 'wss://voice.你的域名.cn/ws/browser';
```

## 10. 微信公众平台改合法域名

https://mp.weixin.qq.com/ → 开发管理 → 开发设置 → 服务器域名：
- request 合法域名：`https://voice.你的域名.cn`
- socket 合法域名：`wss://voice.你的域名.cn`

---

## 验证清单

- [ ] `curl https://voice.你的域名.cn/healthz` 返回 `{"status":"ok"...}`
- [ ] 浏览器打开 `https://voice.你的域名.cn/browser-test` 能进 WebRTC 页面
- [ ] 小程序能连上、对话跑通
- [ ] 企业微信收到推送

---

## 监控（可选）

```bash
# 看 docker 日志
docker compose logs -f

# 看系统资源
htop   # 或 top

# 装个简单监控
apt install -y netdata
# http://ECS_IP:19999 看 dashboard
```

## 备份

SQLite 数据库 `/opt/voice_agent/data/visitors.db` 每天 cron 备份到 OSS：

```bash
# 配 ossutil 后
0 3 * * * /usr/local/bin/ossutil cp /opt/voice_agent/data/visitors.db oss://your-bucket/backup/visitors-$(date +\%F).db
```
