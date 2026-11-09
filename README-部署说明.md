# 免费推送-公网访问慢 · 部署说明

这个文件夹是 `融合版本` 的一份**云端部署副本**，做了三处改动以便推到 Render（免费）上公网运行：

1. **模型改云端 API**（`core/llm.py` + `core/ai.py`）
   默认改用 Agnes AI（OpenAI 兼容）的免费模型 `agnes-2.5-flash`，不再依赖本地 Ollama 和那个 3G 的 qwen3 模型。部署后无需装任何模型，也不用管 GPU/内存。
2. **监听地址改公网**（`server.py`）
   `127.0.0.1` → `0.0.0.0`，端口读 `PORT` 环境变量（Render/Vercel 等平台的约定）。
3. **关闭向量检索**（`MEDKG_VEC=0`）
   向量检索依赖本地 Ollama embedding，云端没有，关闭后自动退回「图谱 + 字面」检索，功能不受影响。

> 源文件在上级目录的 `融合版本`，本文件夹的改动**不影响源文件**。

---

## 一、你需要准备的东西

只有一样：**一个 Agnes AI 的 API Key**（免费）。

1. 打开 https://agnes-ai.com/ 注册并登录
2. 进入开发者控制台 → API Key 管理 → 创建密钥
3. 复制 Key（格式形如 `sk-...`，只显示一次，请立即保存）

> Key 不要写进任何代码或公开仓库，部署时通过平台的环境变量注入。

---

## 二、部署到 Render（免费，推荐）

Render 免费档够用，国内访问速度一般（海外节点）。

**方法 A：通过 GitHub 推送（推荐，可自动部署）**

1. 把本文件夹初始化成 git 仓库并推到 GitHub：
   ```bash
   cd 免费推送-公网访问慢
   git init
   git add .
   git commit -m "云端部署版本：接入 Agnes API"
   git remote add origin https://github.com/你的用户名/medical-hub.git
   git push -u origin main
   ```
2. 打开 https://render.com 注册登录（可用 GitHub 账号）
3. New → Web Service → 连接你的 GitHub 仓库
4. Render 会自动读取 `render.yaml`，识别 Python 服务和启动命令
5. 在环境变量里填 `MEDQA_API_KEY`（你的 Agnes Key）
6. 点 Create，等待部署完成，得到 `https://xxx.onrender.com` 公网地址

**方法 B：直接上传（不装 git）**

也可以直接本地上传 zip，但推荐方法 A，后续改代码能自动重新部署。

---

## 三、环境变量说明

| 变量 | 作用 | 默认值 | 是否必填 |
|---|---|---|---|
| `MEDQA_API_KEY` | Agnes API Key | 无 | ✅ 必填（不填则退回证据摘录模式） |
| `MEDQA_BASE_URL` | API 地址 | `https://apihub.agnes-ai.com/v1` | 否 |
| `MEDQA_MODEL` | 模型名 | `agnes-2.5-flash` | 否 |
| `MEDKG_VEC` | 向量检索开关 | `0`（云端关闭） | 否 |
| `PORT` | 监听端口 | 平台自动注入 | 否 |

---

## 四、部署后你会得到什么

一个公网网址，任何人任何设备打开即可使用：

- `https://xxx.onrender.com/` → 首页（卡片入口）
- `https://xxx.onrender.com/app.html` → 主界面（问答/知识库/图谱/浏览/概览）
- `https://xxx.onrender.com/catalog.html` → 人体导航

**「大脑」在云端**：问答时后端调用 Agnes 模型改写证据，你无需维护任何服务器或模型。

---

## 五、已知限制（免费档的代价）

1. **国内访问慢**：Render 是海外节点，国内打开可能有延迟。
2. **冷启动**：免费档闲置约 15 分钟会休眠，下次访问首次请求要等几十秒。
3. **写入丢失**：反馈点赞、界面模式切换等写本地文件的功能，在 Render 重启后会重置（不影响核心问答/检索/图谱）。
4. **模型免费额度**：Agnes 免费 Key 有 RPM（每分钟请求数）限制，多人同时问可能触发 429，稍等即可。

如果之后想要国内访问快、不受冷启动影响，可以把同一套代码原样推到**阿里云轻量服务器**（约 ¥50-100/月），改动只有环境变量，代码无需再改。
