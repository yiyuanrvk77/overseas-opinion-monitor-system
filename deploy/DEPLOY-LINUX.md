# Linux 部署说明

本包是"社科院海外舆情监测系统 V1.0"的 Linux 部署补充，包含：

- `../start.sh`：Linux 一键启动脚本（对应 Windows 的 `start.ps1`）
- `../backend/requirements.txt`：后端运行依赖（已去掉仅测试用的 `httpx2`）
- `../backend/requirements-dev.txt`：运行依赖 + 测试依赖（要跑自动化测试时用）
- `opinion-monitor.service`：systemd 常驻服务配置
- `nginx-opinion-monitor.conf`：可选的反向代理配置

## 一、前置要求

- Python 3.10 及以上（含 `python3`、`venv`、`pip`）
- 可选：Node.js 18+（只在需要重新构建前端时用；随包的 `dist/` 已可直接使用，服务器可不装 Node）
- 可选：nginx（需要用域名/HTTPS 对外访问时）

建议操作系统：Debian / Ubuntu（使用 systemd 的发行版均可）。

## 二、最简方式：前台试跑

把整个项目目录放到服务器上（例如 `/opt/overseas-opinion-monitor`），然后：

```bash
cd /opt/overseas-opinion-monitor
chmod +x start.sh
./start.sh
```

首次运行会自动：创建 `.venv` 虚拟环境 → 安装后端依赖 → 启动服务。完成后访问：

```text
http://127.0.0.1:8000
```

接口文档在 `http://127.0.0.1:8000/docs`。默认只监听本机，外部访问需要反向代理或改绑定地址。

## 三、生产方式：systemd 常驻

1. 创建运行用户并给项目授权（不要用 root 直接跑）：

```bash
sudo useradd --system --home /opt/overseas-opinion-monitor --shell /usr/sbin/nologin opinion
sudo chown -R opinion:opinion /opt/overseas-opinion-monitor
```

2. 先手动跑一次 `./start.sh`，确认能起来后按 `Ctrl+C` 停掉（这一步会先把依赖装好）。

3. 安装服务并启动：

```bash
sudo cp deploy/opinion-monitor.service /etc/systemd/system/opinion-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now opinion-monitor
sudo systemctl status opinion-monitor
```

注意：`opinion-monitor.service` 里的 `WorkingDirectory`、`User`、`Group`、`ExecStart`、`OPINION_MONITOR_DB` 路径要改成你实际的部署路径。

常用命令：

```bash
sudo systemctl restart opinion-monitor   # 重启
sudo systemctl stop opinion-monitor      # 停止
sudo journalctl -u opinion-monitor -f    # 看日志
```

## 四、可选：nginx 反向代理 + 认证

系统目前没有登录鉴权，对外开放前必须加防护。推荐 nginx 提供 HTTPS，并加一层 Basic Auth：

```bash
sudo apt-get install -y nginx apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd 你的用户名
sudo cp deploy/nginx-opinion-monitor.conf /etc/nginx/conf.d/opinion-monitor.conf
# 编辑该文件，改 server_name，并放开 auth_basic 两行
sudo nginx -t
sudo systemctl reload nginx
```

## 五、关于依赖（httpx2 说明）

后端运行时只需要 `fastapi`、`starlette`、`uvicorn` 三个包，服务本身不依赖 `httpx2`。`httpx2` 只被测试客户端（`starlette.testclient`）用到。为让生产部署更稳，本次把依赖拆成两份：

- 部署/运行：`pip install -r backend/requirements.txt`
- 想在本机跑自动化测试：`pip install -r backend/requirements-dev.txt`（会额外安装 `httpx2==2.12.0`）

跑测试：

```bash
cd /opt/overseas-opinion-monitor
./.venv/bin/python -m pip install -r backend/requirements-dev.txt
./.venv/bin/python -m unittest discover -s backend/tests -v
```

## 六、安全提醒

- 角色"核心/研究员"是前端演示切换，不是真正的身份认证；任何能访问服务的人都能切换为"核心"并看到全部数据、做写操作。生产使用前必须接入统一身份认证、HTTPS 和服务端授权。
- 默认只监听 `127.0.0.1`，请勿直接把 8000 端口暴露到公网。需要远程访问时走 nginx 反代并加认证/TLS。
- 首次安装依赖需要联网；完全离线环境可先在能联网的机器上装好 `.venv`，再把整个目录（含 `.venv` 和 `dist/`）拷到目标机。

## 七、常见问题

- 端口被占用：改 `PORT` 环境变量，如 `PORT=8001 ./start.sh`。
- 权限不足：确认 `opinion` 用户对项目目录和 `backend/data/` 有写权限（SQLite 会在这里建库）。
- 首次启动失败多半是 `pip install` 网络问题，先 `pip config` 检查内网镜像/代理。
