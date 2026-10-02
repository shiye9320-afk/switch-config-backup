# switch-config-backup

基于 **Python + Netmiko** 的批量交换机 / 路由器配置文件自动备份工具。
一份 CSV 清单，一条命令，把全网设备的 running-config 拉下来归档，并自动和上次备份做 diff。

---

## 特性

| 能力 | 说明 |
| --- | --- |
| 批量并发 | `ThreadPoolExecutor` 并发登录，默认 10 台一批，几十台设备几十秒搞定 |
| 多平台 | 思科 IOS / NX-OS、华为 VRP、H3C Comware、锐捷、Arista、Juniper 等自动匹配备份命令 |
| 自动探测 | `device_type` 填 `auto` 时，用 Netmiko `SSHDetect` 自动识别厂商，识别不出再逐个候选类型重试 |
| 变更检测 | 每次备份与上一份做 `diff`，只标记真正改过的设备，未变更的设备不生成 diff 文件 |
| 归档结构 | 按 `设备名__IP/` 分目录，文件名为时间戳，`latest.cfg` 永远是最新的那份 |
| 失败隔离 | 单台认证失败 / 超时不影响其他设备，最后统一打印失败明细 |
| 密码安全 | 密码支持 `env:环境变量名` 写法，不把明文写进 CSV |
| 三种产物 | 控制台汇总表 + `logs/report-<时间戳>.json` 机器可读报告 + 运行日志 |
| 历史清理 | `--keep-days 90` 自动删除 90 天前的旧备份 |

---

## 目录结构

```
switch-config-backup/
├── backup.py               # 主脚本
├── devices.csv.example     # 设备清单模板
├── requirements.txt
├── .gitignore              # 已忽略 devices.csv / backups/ / logs/
└── backups/                # 备份产物（自动生成，不入库）
    └── Core-SW-01__192.168.1.10/
        ├── 20261002-154900.cfg
        ├── 20261002-154900.diff
        └── latest.cfg
```

---

## 安装

```bash
git clone https://github.com/shiye9320-afk/switch-config-backup.git
cd switch-config-backup

python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

> 要求 **Python 3.8+**（推荐 3.10+）。Python 3.7 装不上 netmiko 4.x。

---

## 设备清单

复制模板并填写真实设备：

```bash
cp devices.csv.example devices.csv
```

| 列名 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 否 | 备注名，缺省用 `host` |
| `host` | **是** | IP 或域名 |
| `device_type` | **是** | Netmiko 设备类型，见下表；不确定填 `auto` |
| `username` | **是** | 登录用户名 |
| `password` | **是** | 登录密码，推荐写 `env:SW_PASSWORD` |
| `port` | 否 | 默认 22；telnet 类型默认 23 |
| `secret` | 否 | enable 密码，有则自动进特权模式 |
| `cmd` | 否 | 自定义备份命令，留空用平台默认 |
| `group` | 否 | 分组，配合 `--only` 只备份某一组 |

### 常用 device_type

| 厂商 / 系统 | device_type | 自动执行的命令 |
| --- | --- | --- |
| Cisco IOS / IOS-XE | `cisco_ios` / `cisco_xe` | `show running-config` |
| Cisco NX-OS | `cisco_nxos` | `show running-config` |
| 华为 VRP | `huawei` / `huawei_vrp` | `display current-configuration` |
| H3C Comware | `hp_comware` | `display current-configuration` |
| 锐捷 | `ruijie_os` | `show running-config` |
| Arista EOS | `arista_eos` | `show running-config` |
| Juniper JunOS | `juniper_junos` | `show configuration \| display set` |
| 中兴 | `zte_zxros` | `show running-config` |
| Telnet 方式 | 类型后加 `_telnet`，如 `cisco_ios_telnet` | 同上 |

完整类型列表见 [Netmiko 官方支持平台](https://github.com/ktbyers/netmiko/blob/develop/PLATFORMS.md)。

### 密码用环境变量

```powershell
# Windows PowerShell
$env:SW_PASSWORD = "YourPassw0rd"
```

```bash
# Linux / macOS
export SW_PASSWORD='YourPassw0rd'
```

CSV 里写 `env:SW_PASSWORD` 即可。**`devices.csv` 已被 `.gitignore` 忽略，不会被提交。**

---

## 用法

```bash
# 最常用：按清单备份全部设备
python backup.py

# 指定清单和并发数
python backup.py -d devices.csv -w 20

# 只备份 core 组
python backup.py --only core

# 备份并清理 90 天前历史
python backup.py --keep-days 90

# 先演练：只打印将要执行的命令，不连设备
python backup.py --dry-run

# 网络设备慢、配置长
python backup.py --conn-timeout 30 --read-timeout 300

# 调试单台看不到输出时
python backup.py --log-level DEBUG
```

### 全部参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `-d, --devices` | `devices.csv` | 清单路径 |
| `-o, --out` | `backups` | 备份输出目录 |
| `-w, --workers` | `10` | 并发数 |
| `--only` | 空 | 只备份指定 group |
| `--username / --password / --secret` | 空 | 覆盖清单里的凭据 |
| `--command` | 空 | 统一覆盖备份命令 |
| `--device-type` | 空 | 统一覆盖设备类型 |
| `--conn-timeout` | `15` | 连接超时（秒） |
| `--read-timeout` | `180` | 读取配置超时（秒） |
| `--keep-days` | `0` | 清理 N 天前备份，0 = 不清理 |
| `--no-diff` | 关 | 关闭差异对比 |
| `--dry-run` | 关 | 只校验，不连接 |
| `--log-level` | `INFO` | 日志级别 |

**退出码**：全部成功返回 `0`，有任何一台失败返回 `1`——可直接用于 crontab / 定时任务告警判断。

---

## 定时自动备份

### crontab（Linux / macOS）

```bash
0 3 * * * cd /opt/switch-config-backup && .venv/bin/python backup.py --keep-days 90 >> /var/log/swbackup.log 2>&1
```

### 任务计划程序（Windows）

```powershell
$action = New-ScheduledTaskAction -Execute "C:\path\to\.venv\Scripts\python.exe" `
  -Argument "backup.py --keep-days 90" -WorkingDirectory "C:\path\to\switch-config-backup"
$trigger = New-ScheduledTaskTrigger -Daily -At 3am
Register-ScheduledTask -TaskName "SwitchConfigBackup" -Action $action -Trigger $trigger
```

---

## 常见问题

**Q：备份出来是空的 / 只有几行？**
A：账号权限不够（需要能进特权模式）。填上 `secret` 让脚本自动 `enable`；或用 `--log-level DEBUG` 看设备原始回显。

**Q：华为 / H3C 提示超时？**
A：大配置回显慢，加大 `--read-timeout 300`。脚本在检测到输出过短时会自动用 `send_command_timing` 重试一次。

**Q：不确定设备是什么厂商？**
A：`device_type` 填 `auto`，脚本会先用 `SSHDetect` 探测，探测不出再按 思科 → 华为 → H3C → 锐捷 → ... 的顺序逐个试。

**Q：老设备只支持 Telnet？**
A：`device_type` 写 `cisco_ios_telnet`（或 `huawei_telnet`、`hp_comware_telnet`），端口会自动用 23。

**Q：只想看某台设备改了什么？**
A：`backups/<设备名>__<IP>/<时间戳>.diff` 就是标准 unified diff，`git diff --no-index` 或直接看都行。

---

## 安全提醒

- `devices.csv`（含真实 IP 和密码）和 `backups/`（含完整网络拓扑与配置）**都已在 `.gitignore` 中**，不会被提交。
- 仓库是公开的：请确认你没有把真实设备清单或配置 `git add -f` 进来。
- 生产环境建议用只读账号 + 专用备份账号，密码走环境变量或密钥管理工具。
- 若要把备份结果也纳入版本管理，**请改用私有仓库**。

---

## 许可

MIT
