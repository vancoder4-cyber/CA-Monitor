# 交互式 Lark 机器人(@指令)

在 Lark 群里 **@机器人 + 关键词**,它回你内容:

| 指令 | 回复 |
|---|---|
| `@机器人 日历` | 近期公司行动卡片 + **网页日历截图** |
| `@机器人 预警`(或 `面板`) | 当前待执行/冲突/空缺摘要 + 面板链接 |
| `@机器人 帮助` | 指令说明 |

数据来源:直接读 GitHub Pages 发布的 `data.json`,截图截 Pages 日历页 —— 与定时管道解耦,这里**不需要任何行情 API key**。

---

## 一、创建 Lark 自定义应用(拿 App ID / Secret)

> 注意:这是「自定义应用 App」,跟之前推送用的「自定义机器人 Webhook」不是一回事。两者可并存。

1. 打开 **Lark 开发者后台** https://open.larksuite.com/app → **创建企业自建应用**。
2. 应用详情页拿到 **App ID** 和 **App Secret**。
3. 左侧 **添加应用能力 → 机器人**,启用。
4. 左侧 **权限管理**,开通以下权限(scope):
   - `im:message`(接收消息)
   - `im:message:send_as_bot`(以机器人身份发消息)
   - `im:resource`(上传图片)
5. 左侧 **事件与回调 → 事件配置**:订阅方式选 **长连接(Long Connection)**(无需回调地址),
   添加事件 **接收消息 `im.message.receive_v1`**。
6. **创建版本并发布**(企业内部应用走发布审核/自动通过)。
7. 把机器人**加进你的目标群**(群设置 → 机器人 → 添加)。

## 二、部署到 PaaS(以 Railway 为例)

本目录(`bot/`)含 `Dockerfile`,任意支持 Docker 的平台都能跑。

1. 把整个仓库推到 GitHub(已在做)。
2. Railway → New Project → **Deploy from GitHub repo** → 选 CA-Monitor。
3. Settings → **Root Directory** 设为 `bot`(让它用 `bot/Dockerfile`)。
4. **Variables** 加环境变量:
   - `LARK_APP_ID` = 你的 App ID
   - `LARK_APP_SECRET` = 你的 App Secret
   - `SITE_URL` = `https://vancoder4-cyber.github.io/CA-Monitor/`
5. Deploy。日志出现「等待 @ 指令……」即成功。无需开放端口(长连接是出站的)。

> Render / fly.io 同理:用 Dockerfile 部署,设这三个环境变量,常驻运行即可。

## 三、验证

群里 `@机器人 日历` → 应回一张卡片 + 一张日历截图。
`@机器人 帮助` → 列出指令。

## 本地调试

```bash
pip install -r requirements.txt && playwright install chromium
export LARK_APP_ID=... LARK_APP_SECRET=... SITE_URL=https://vancoder4-cyber.github.io/CA-Monitor/
python bot.py
```
