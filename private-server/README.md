# 私服搭建指南（Mac + OpenDoctoratePy）

> ⚠️ 合规提示：私服绕过官方服务器，使用存在账号封禁风险，请用测试账号、勿在官方正式服投入资源。

## 架构

```
安卓模拟器(游戏客户端) ←Frida hook→ 本地 Flask 服务器(127.0.0.1:8443)
    │                            │
    └─ 战斗在客户端本地模拟 ──────┘（客户端自己算伤害，我们只需读结果）
```

- 服务器：`server/app.py`（Python Flask，Mac 可直接跑）
- 客户端 hook：`fridahook.py` + `_.js`（Frida 重定向客户端请求到本地）
- 验证方式：私服里打关卡 → 客户端战斗引擎给出真实总伤/DPS → 与我们的模型对比

## 一、服务器端（已完成 ✅）

```bash
cd private-server
./start_server.sh        # 或 .venv/bin/python server/app.py
```
验证：`curl -s http://127.0.0.1:8443/` 返回 404 即服务器在响应（404 正常，根路径无路由）。

依赖已装：flask / frida / pure-python-adb / pycryptodome / requests。

## 二、客户端侧（需要你操作，Mac 版）

### 1. 安卓模拟器（需支持 root + adb）
OpenDoctoratePy 官方只测了 **LDPlayer9（Windows）**。Mac 备选：
- **Android Studio AVD**（Google APIs 镜像，自带 root 的 adb shell）
- **MuMu 模拟器 Mac 版**（网易，设置里可开 root）
- **Genymotion**（VirtualBox，可开 root）

关键：模拟器内需能运行 `frida-server`（arm64/x86_64 对应版本）。

### 2. 游戏客户端（CN TapTap 版）
- 包名：`com.hypergryph.arknights`（fridahook.py 里写死）
- 从 TapTap 或可信 APK 源下载安装到模拟器
- 注意版本需与 OpenDoctoratePy 支持的版本匹配（该 fork 更新到较新版本，若登录异常需在 [仓库 Issues](https://github.com/baiqilingnai/OpenDoctoratePy) 查对应版本）

### 3. frida-server
```bash
# 下载与 frida 版本(17.17.0)匹配的 frida-server
# https://github.com/frida/frida/releases 选 frida-server-17.17.0-android-<arch>
adb push frida-server /data/local/tmp/
adb shell "chmod 755 /data/local/tmp/frida-server"
adb shell "/data/local/tmp/frida-server &"   # 模拟器内启动
```

### 4. 连接测试
```bash
cd private-server
.venv/bin/python fridahook.py   # 会自动拉起游戏并 hook
```

## 三、自定义配置（`config/config.json`）

- `customUnitInfo`：已配置 史尔特尔/能天使/阿米娅 = **精二满级 0潜 专三 满信赖**（与我们模型口径一致）
- `selectedCrisis`：切换合约赛季（data/crisis/cc0-cc11+.json）
- `assistUnit`：配置助战干员

## 四、验证实验设计（搭建完成后）

1. **法术 5% 保底判定**：找法抗 100 敌人（如特定 BOSS 开技能时），法术干员打出的伤害若为 0 → 无保底（我们模型对）；若为 atk×5% → 有保底（ArkDPS 对）
2. **总伤/平均DPS 对照**：用 0潜专三的史尔特尔/能天使/艾雅法拉，在自定义关卡打固定敌人（选我们 enemy_stats_manual 里的敌人），对比客户端实测总伤 vs 我们模型（operator_eval.csv）
3. **攻击前摇/索敌/多目标**：差异 >5% 时，用实测数据反推前摇/索敌修正系数
