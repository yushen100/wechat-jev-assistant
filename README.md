# 微信 Jev 对话助手

一个 Windows 桌面辅助工具：读取当前微信会话的本地文字记录，脱敏后调用 TypeSafe Jev，给出对话阶段、需求、意图、紧张程度、建议动作和回答复盘。

## 隐私边界

- API Key 只从 `TYPESAFE_API_KEY` 环境变量读取，不写入源码、数据库或日志。
- 本地聊天历史使用 AES-GCM 加密，密钥由 Windows DPAPI 绑定当前用户。
- 手机号、邮箱、身份证号、银行卡号和 URL 查询参数在发送前本地脱敏。
- 截图只用于识别当前会话标题，不保存到磁盘。
- 数据库桥接器只加载固定版本的 `wechatauto/db.py`，禁止发送、监听、UIA 热激活、媒体下载和密钥文件写出。
- `data/`、`logs/`、`.venv/`、数据库临时目录和上游完整源码均被 Git 忽略。

> 本工具会把脱敏后的对话文本发送给 TypeSafe API。请只分析你有权处理的会话，并遵守当地法律与平台规则。

## 安装

要求：Windows、微信桌面版、Python 3.12、Git。

完整步骤见：[部署指南](content/0922_部署指南_v1.md)。

推荐从 [Releases](https://github.com/yushen100/wechat-jev-assistant/releases) 下载经过校验的 Windows ZIP；自动化部署也可使用 GitHub Packages 中的 `WechatJevAssistant` NuGet 包。

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
```

然后在当前 Windows 用户环境变量中设置 `TYPESAFE_API_KEY`，双击 `0921_启动微信Jev助手_v1.cmd`。

## 使用

1. 打开目标微信聊天并保持在前台。
2. 按 `Ctrl+Alt+J`。
3. 在主窗口选择 100、150、200 或 250 条，助手识别当前会话标题后从本地数据库读取对应数量的文字与动画表情并分析。
4. 历史数据仅保存在本机加密数据库中。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -q
```

## 第三方依赖

只读数据库适配依赖 `fanyuantaier/wechatauto-replica` 的固定提交。`setup.ps1` 会下载该版本；其源码和许可证归上游项目所有，不会复制进本仓库。
