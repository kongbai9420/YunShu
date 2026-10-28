# 开发说明

云枢是开源项目（MIT 许可证），代码在 <https://github.com/kongbai9420/YunShu>。参与开发（包括使用编码助手）前请先阅读仓库根目录的 [AGENTS.md](https://github.com/kongbai9420/YunShu/blob/main/AGENTS.md)，其中的规则优先于本页。

## 从哪里读起

| 文件 | 内容 |
| --- | --- |
| [README.md](https://github.com/kongbai9420/YunShu/blob/main/README.md) | 项目介绍与快速开始（[English](https://github.com/kongbai9420/YunShu/blob/main/README.en.md)） |
| [AGENTS.md](https://github.com/kongbai9420/YunShu/blob/main/AGENTS.md) | 参与开发必须遵守的规则：仓库里放什么、测试数据怎么写、分支与提交 |
| [backend/README.md](https://github.com/kongbai9420/YunShu/blob/main/backend/README.md) | 后端结构、接口、迁移、采集与控制、测试命令 |
| [frontend/README.md](https://github.com/kongbai9420/YunShu/blob/main/frontend/README.md) | 前端结构、本地预览、浏览器测试 |
| [deploy/README.md](https://github.com/kongbai9420/YunShu/blob/main/deploy/README.md) | 部署模板与说明 |
| [frontend/src/themes/README.md](https://github.com/kongbai9420/YunShu/blob/main/frontend/src/themes/README.md) | 主题变量约定 |

## 目录结构

| 目录 | 内容 |
| --- | --- |
| `backend/` | FastAPI 后端、Worker、采集与控制适配器、Alembic 迁移、后端测试 |
| `frontend/` | React + TypeScript + Vite 前端、Playwright 浏览器测试（`e2e/`） |
| `deploy/` | 三套 Docker 部署模板（lite / standard / enterprise）、Linux 与 Windows 安装说明 |
| `docker/` | 镜像的 Dockerfile 与入口脚本 |
| `packaging/` | Windows 安装包（MSI）相关文件 |
| `scripts/` | 本地预览、打包、供应链检查等脚本 |

## 测试数据规则

- 地址只用 `192.0.2.0/24`（或 `198.51.100.0/24`、`203.0.113.0/24`），域名用 `example.invalid` / `example.test`，密码与令牌写 `fake-…`。
- 仓库里不放密码、令牌、主密钥、`.env`、真实设备的 IP / 主机名 / 序列号、日志、截图、本地数据库与备份。
- 所有测试只连接伪造设备，不会访问真实网络设备。

## 本地运行

Windows 上最简单的是 `scripts\preview.bat`：自动准备虚拟环境和前端依赖，启动后端与前端开发服务器。`-Reset` 从干净数据开始（会显示首次设置向导），`-NoSeed` 不导入示例数据。

手动方式（Python 3.11、Node 20）：

```bash
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install --require-hashes -r requirements-dev.lock   # Linux/macOS 用 .venv/bin/python
.venv/Scripts/python -m uvicorn app.main:create_app --factory --port 8000

cd ../frontend
npm ci
npm run dev          # 开发服务器把 /api 转发到 http://127.0.0.1:8000（YUNSHU_API_URL 可改）
```

## 运行测试

| 命令（在对应目录） | 内容 |
| --- | --- |
| `python -m pytest`（backend） | 后端全部测试，默认 SQLite |
| `YUNSHU_TEST_DATABASE_URL=postgresql+psycopg://…/postgres python -m pytest`（backend） | 同一套测试在 PostgreSQL 上运行（需要能建库的账号） |
| `npm run typecheck`、`npm run build`（frontend） | 类型检查与构建 |
| `npm run check:help`（frontend） | 检查每个控件都有说明或「!」胶囊 |
| `npx playwright test`（frontend） | 浏览器测试（自动启动伪造设备的后端） |

## 说明文字

每个新增的设置项都要有说明：一句话写在控件下方（`hint`）；说不清的写进 `frontend/src/lib/help.ts`，用「!」胶囊显示；标签已经足够的标为 `plain`。`npm run check:help` 会检查。

## 反馈问题

在 GitHub 仓库提 issue，写明版本、部署方式、操作步骤和日志。**不要在 issue 里贴真实的 IP、主机名、序列号、密码或令牌。**
