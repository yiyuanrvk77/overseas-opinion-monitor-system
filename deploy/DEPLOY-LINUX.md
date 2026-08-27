# Linux 部署说明

本包是"社科院海外舆情监测系统 V1.0"的 Linux 部署补充，包含：

- `../start.sh`：Linux 一键启动脚本（对应 Windows 的 `start.ps1`）
- `../backend/requirements.txt`：后端运行依赖（已去掉仅测试用的 `httpx2`）
- `../backend/requirements-dev.txt`：运行依赖 + 测试依赖（要跑自动化测试时用）
- `opinion-monitor.service`：systemd 常驻服务配置
- `opinion-monitor-collector.service`、`.timer`：可选的公开源定时刷新
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

已有线上服务时先查看并保留现有单元中的部署路径、用户、监听地址和数据库路径，不要直接覆盖。只在核对差异后更新需要的字段。

常用命令：

```bash
sudo systemctl restart opinion-monitor   # 重启
sudo systemctl stop opinion-monitor      # 停止
sudo journalctl -u opinion-monitor -f    # 看日志
```

## 四、公开数据刷新

首次部署先运行一次公开源采集并导入。采集器对 12 个固定入口各发一个匿名请求，只保存公开标题、短摘要、时间和来源链接；受限状态会记录，但不会绕过。

```bash
cd /opt/overseas-opinion-monitor
./.venv/bin/python scripts/collect_public_web.py --timeout 18 --limit 24
./.venv/bin/python scripts/import_public_demo.py --db backend/data/opinion_monitor.db
```

需要定时刷新时，先把模板中的路径和运行用户改成实际值，再启用定时器：

```bash
sudo cp deploy/opinion-monitor-collector.service /etc/systemd/system/
sudo cp deploy/opinion-monitor-collector.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now opinion-monitor-collector.timer
sudo systemctl list-timers opinion-monitor-collector.timer
```

默认间隔为 6 小时并带随机延迟。可用 `systemctl start opinion-monitor-collector.service` 手动触发一次，用 `journalctl -u opinion-monitor-collector.service` 查看结果。正式供应商连接器到位后，应停用该试采定时器或把它保留为独立的公开源补充层。

## 五、启用服务端认证和 HTTPS

API 内置一层默认关闭的 HTTP Basic 认证。启用后，账号角色由服务端配置绑定；研究员即使修改 URL 或请求体里的 `role=core` 也会返回 403。核心写操作还会按接口路径再次校验。未启用时保留本地演示行为，前端角色切换不构成身份认证。

推荐把密钥放入 systemd 的独立环境文件，文件不要提交到仓库：

```bash
sudo install -m 600 /dev/null /etc/opinion-monitor.env
sudoedit /etc/opinion-monitor.env
```

多账号配置示例（密码必须替换，JSON 保持在一行）：

```text
OPINION_MONITOR_AUTH_MODE=basic
OPINION_MONITOR_BASIC_USERS='{"analyst":{"password":"replace-with-long-random-password","role":"researcher"},"admin":{"password":"replace-with-another-long-random-password","role":"core"}}'
```

也可只配置一个账号：

```text
OPINION_MONITOR_AUTH_MODE=basic
OPINION_MONITOR_BASIC_USERNAME=admin
OPINION_MONITOR_BASIC_PASSWORD=replace-with-long-random-password
OPINION_MONITOR_BASIC_ROLE=core
```

仓库提供的 systemd 单元已读取 `/etc/opinion-monitor.env`。改完后重启并验证：

```bash
sudo systemctl restart opinion-monitor
curl -i http://127.0.0.1:8000/api/health                     # 应返回 401
curl --user analyst http://127.0.0.1:8000/api/health         # 交互输入密码，应返回 200
```

Basic 凭据不能在明文 HTTP 上传输。对外服务必须再由 nginx 提供 HTTPS：

```bash
sudo apt-get install -y nginx apache2-utils
sudo cp deploy/nginx-opinion-monitor.conf /etc/nginx/conf.d/opinion-monitor.conf
# 编辑该文件，配置 server_name 和 TLS 证书
sudo nginx -t
sudo systemctl reload nginx
```

如果只使用 nginx 的 `auth_basic`，它只能形成统一入口防护，不能把应用内“研究员/核心”角色绑定到不同账号。需要角色隔离时应启用上述应用认证，或接入正式 IAM/OIDC。不要同时配置两套使用不同凭据的 Basic Auth，否则两层会争用同一个 `Authorization` 请求头。

## 六、关于依赖（httpx2 说明）

后端运行时只需要 `fastapi`、`starlette`、`uvicorn` 三个包，服务本身不依赖 `httpx2`。`httpx2` 只被测试客户端（`starlette.testclient`）用到。为让生产部署更稳，本次把依赖拆成两份：

- 部署/运行：`pip install -r backend/requirements.txt`
- 想在本机跑自动化测试：`pip install -r backend/requirements-dev.txt`（会额外安装 `httpx2==2.12.0`）

跑测试：

```bash
cd /opt/overseas-opinion-monitor
./.venv/bin/python -m pip install -r backend/requirements-dev.txt
./.venv/bin/python -m unittest discover -s backend/tests -v
```

## 七、安全提醒

- `OPINION_MONITOR_AUTH_MODE` 默认为 `off`；此时“核心/研究员”只是前端演示切换。公网或多人环境必须设为 `basic`，或接入正式 IAM/OIDC。
- 内置 Basic 模式会把账号绑定到服务端角色并阻止请求参数越权，但它不替代 HTTPS、账号生命周期管理、多因素认证和集中式身份审计。
- 密码只放在权限为 0600 的环境文件或密钥管理服务中，不要写入 systemd 单元、源码、镜像、命令行或 Git。
- 默认只监听 `127.0.0.1`，请勿直接把 8000 端口暴露到公网。需要远程访问时走 nginx 反代并加认证/TLS。
- 首次安装依赖需要联网；完全离线环境可先在能联网的机器上装好 `.venv`，再把整个目录（含 `.venv` 和 `dist/`）拷到目标机。

## 八、常见问题

- 端口被占用：改 `PORT` 环境变量，如 `PORT=8001 ./start.sh`。
- 权限不足：确认 `opinion` 用户对项目目录和 `backend/data/` 有写权限（SQLite 会在这里建库）。
- 首次启动失败多半是 `pip install` 网络问题，先 `pip config` 检查内网镜像/代理。
