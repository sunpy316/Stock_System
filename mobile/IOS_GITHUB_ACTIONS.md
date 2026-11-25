# iOS App（.ipa）GitHub Actions 打包与安装指南

本工程 `D:\Stocks\Antigravity\stocksystem_android` 已配置好 **Capacitor iOS** 与 **GitHub Actions（`macos-latest`）** 自动编译工作流：
- 工作流文件：[build-ios.yml](file:///D:/Stocks/Antigravity/stocksystem_android/.github/workflows/build-ios.yml)
- 网络权限已放行 HTTP 接口：`ios/App/App/Info.plist` 已配置 `NSAppTransportSecurity -> NSAllowsArbitraryLoads = true`，可正常连接 `http://47.104.78.144`。

---

## 第一步：推送到 GitHub 自动编译 `.ipa`

本地仓库已为你初始化并提交好代码。你只需在 GitHub 新建一个仓库（公开或**私有仓库**均可），然后在本地执行：

```powershell
cd D:\Stocks\Antigravity\stocksystem_android
git remote add origin https://github.com/你的用户名/你的仓库名.git
git push -u origin main
```

推送后打开 GitHub 仓库的 **Actions** 页面，等待约 3~5 分钟即可看到 `Build iOS IPA` 运行完成，在页面底部 **Artifacts** 下载 `Stock_System_iOS_IPA`（内含 `Stock_System_iOS_unsigned.ipa`）。

---

## 第二步：安装到苹果手机（无需付费开发者账号）

默认产物为**未签名 `.ipa`**（不需要配置任何 Apple 开发者证书即可出包），安装到 iPhone 有两种方式：

1. **自用免费签名安装（推荐：Sideloadly / 爱思助手）**：
   - 在 Windows 电脑下载 **Sideloadly** 或 **爱思助手**，用数据线连接 iPhone。
   - 把下载好的 `Stock_System_iOS_unsigned.ipa` 拖入工具，输入你的普通 Apple ID 即可免费签名安装到手机（有效期 7 天，到期重新点一下即可）。
   - 首次打开前，在 iPhone `设置 -> 通用 -> VPN 与设备管理` 中点信任该 Apple ID。
2. **正式签名直装（如果你有付费 Apple 开发者账号）**：
   - 在 GitHub 仓库 `Settings -> Secrets and variables -> Actions` 中添加 3 个 Secret：
     - `IOS_P12_BASE64`（`.p12` 证书的 Base64 内容）
     - `IOS_P12_PASSWORD`（`.p12` 密码）
     - `IOS_PROVISION_BASE64`（`.mobileprovision` 描述文件的 Base64 内容）
   - 重新运行 Workflow 即可直接生成已签名的 `Stock_System_iOS_signed.ipa`。
