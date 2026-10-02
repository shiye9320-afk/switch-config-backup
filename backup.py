#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
switch-config-backup
====================

基于 Netmiko 的批量交换机/路由器配置文件自动备份工具。

功能:
  * 从 CSV 清单批量读取设备, 并发 SSH/Telnet 登录
  * 自动识别平台并执行对应的备份命令 (思科 / 华为 / H3C / 锐捷 / Juniper / Arista ...)
  * 也支持 Netmiko 的 autodetect 自动探测设备类型
  * 按 设备名/IP/时间戳 归档配置, 维护 latest.cfg 软副本
  * 与上一次备份自动做 diff, 只记录真正发生变更的设备
  * 失败的主机单独记录原因, 不影响其他设备
  * 输出人类可读汇总表 + JSON 报告 + 运行日志

用法示例:
  python backup.py --devices devices.csv
  python backup.py --devices devices.csv --workers 20 --keep-days 90
  python backup.py --devices devices.csv --only core --diff
  python backup.py --devices devices.csv --dry-run

依赖:
  pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import csv
import difflib
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from netmiko import ConnectHandler
    from netmiko.exceptions import (
        NetmikoAuthenticationException,
        NetmikoTimeoutException,
    )
    from netmiko.ssh_autodetect import SSHDetect
except ImportError:  # pragma: no cover
    sys.exit("缺少 netmiko 依赖, 请先执行: pip install -r requirements.txt")


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

LOG = logging.getLogger("backup")

#: 不同平台默认备份命令 (key 为 netmiko device_type 的关键字片段)
PLATFORM_COMMANDS: Dict[str, str] = {
    "cisco_ios": "show running-config",
    "cisco_xe": "show running-config",
    "cisco_nxos": "show running-config",
    "cisco_asa": "show running-config",
    "arista_eos": "show running-config",
    "ruijie_os": "show running-config",
    "huawei": "display current-configuration",
    "huawei_vrp": "display current-configuration",
    "huawei_smartax": "display current-configuration",
    "hp_comware": "display current-configuration",   # H3C
    "h3c": "display current-configuration",
    "juniper_junos": "show configuration | display set",
    "juniper": "show configuration | display set",
    "extreme": "show configuration",
    "dell_force10": "show running-config",
    "brocade": "show running-config",
    "zte_zxros": "show running-config",
}
DEFAULT_COMMAND = "show running-config"

#: 自动探测时的候选类型 (按可能性排序, 探测失败会逐个尝试)
AUTODETECT_CANDIDATES = [
    "cisco_ios",
    "huawei",
    "hp_comware",
    "ruijie_os",
    "cisco_nxos",
    "arista_eos",
    "juniper_junos",
]

AUTO_TYPES = {"auto", "autodetect", "detect", ""}


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class Device:
    """一台待备份的设备."""

    name: str
    host: str
    device_type: str = "cisco_ios"
    username: str = ""
    password: str = ""
    port: Optional[int] = None
    secret: str = ""          # enable 密码
    cmd: str = ""             # 自定义备份命令 (留空则用平台默认)
    group: str = "default"

    # ---- 构造 ----
    @classmethod
    def from_row(cls, row: Dict[str, Any], defaults: Dict[str, Any]) -> "Device":
        get = lambda k, d="": str(row.get(k, d) or d).strip()
        port_raw = get("port")
        port = int(port_raw) if port_raw.isdigit() else None
        return cls(
            name=get("name") or get("host"),
            host=get("host"),
            device_type=get("device_type") or defaults.get("device_type", "cisco_ios"),
            username=get("username") or defaults.get("username", ""),
            password=get("password") or defaults.get("password", ""),
            port=port,
            secret=get("secret") or defaults.get("secret", ""),
            cmd=get("cmd") or defaults.get("cmd", ""),
            group=get("group", "default"),
        )

    # ---- 工具 ----
    @property
    def is_telnet(self) -> bool:
        return self.device_type.endswith("_telnet") or self.port == 23

    @property
    def resolved_port(self) -> int:
        if self.port:
            return self.port
        return 23 if self.device_type.endswith("_telnet") else 22

    @property
    def resolved_password(self) -> str:
        return resolve_secret(self.password)

    @property
    def resolved_secret(self) -> str:
        return resolve_secret(self.secret)

    def validate(self) -> Optional[str]:
        if not self.host:
            return "缺少 host 字段"
        if not self.username:
            return "缺少 username"
        if not self.resolved_password:
            return "缺少 password (或环境变量未设置)"
        return None

    def netmiko_params(self, conn_timeout: int, device_type: Optional[str] = None) -> Dict[str, Any]:
        return {
            "device_type": device_type or self.device_type,
            "host": self.host,
            "username": self.username,
            "password": self.resolved_password,
            "port": self.resolved_port,
            "secret": self.resolved_secret,
            "conn_timeout": conn_timeout,
            "fast_cli": True,
        }


def resolve_secret(value: str) -> str:
    """支持 `env:VAR_NAME` 写法, 从环境变量读取密码, 避免明文入库."""
    if not value:
        return ""
    if value.startswith("env:"):
        return os.environ.get(value[4:].strip(), "")
    return value


def safe_name(text: str) -> str:
    """把设备名/提示符号清洗成安全的目录名."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", text).strip("_") or "unknown"


def clean_hostname(prompt: str) -> str:
    """从设备提示符里提取主机名: `[SW1]` / `<Huawei>` / `SW1#` / `user@SW1>`."""
    text = (prompt or "").strip()
    text = re.sub(r"^[\[\(<]+", "", text)
    text = re.sub(r"[\]\)>#:\s]+$", "", text)
    text = text.split("@")[-1]
    return safe_name(text)


def pick_command(device_type: str, override: str = "") -> str:
    """根据 device_type 选择备份命令."""
    if override:
        return override
    key = device_type.lower().replace("_telnet", "").replace("_ssh", "")
    if key in PLATFORM_COMMANDS:
        return PLATFORM_COMMANDS[key]
    for fragment, command in PLATFORM_COMMANDS.items():
        if fragment in key:
            return command
    return DEFAULT_COMMAND


# --------------------------------------------------------------------------- #
# 文件落盘 / diff / 清理
# --------------------------------------------------------------------------- #

def target_dir(out_root: Path, hostname: str, host: str) -> Path:
    return out_root / f"{safe_name(hostname)}__{safe_name(host)}"


def previous_backup(directory: Path, exclude: Path) -> Optional[Path]:
    """取该设备上一次备份文件 (时间戳命名, 字典序即时间序)."""
    candidates = [
        p for p in directory.glob("*.cfg")
        if p.name != "latest.cfg" and p.resolve() != exclude.resolve()
    ]
    return sorted(candidates, key=lambda p: p.name)[-1] if candidates else None


def write_latest(directory: Path, text: str) -> None:
    (directory / "latest.cfg").write_text(text, encoding="utf-8")


def make_diff(old_text: str, new_text: str, old_name: str, new_name: str) -> str:
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{old_name}",
            tofile=f"b/{new_name}",
            n=2,
        )
    )


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def cleanup_old(out_root: Path, keep_days: int, log=LOG) -> int:
    """删除超过 keep_days 天的历史备份 (latest.cfg 不删)."""
    if keep_days <= 0 or not out_root.exists():
        return 0
    deadline = time.time() - keep_days * 86400
    removed = 0
    for path in out_root.rglob("*.cfg"):
        if path.name == "latest.cfg":
            continue
        try:
            if path.stat().st_mtime < deadline:
                path.unlink()
                removed += 1
        except OSError as exc:
            log.warning("清理失败 %s: %s", path, exc)
    if removed:
        log.info("已清理 %s 个超过 %s 天的历史备份", removed, keep_days)
    return removed


# --------------------------------------------------------------------------- #
# 单台设备备份
# --------------------------------------------------------------------------- #

def autodetect_type(dev: Device, conn_timeout: int) -> Optional[str]:
    """用 Netmiko SSHDetect 探测设备类型."""
    params = dev.netmiko_params(conn_timeout, device_type="autodetect")
    params.pop("secret", None)
    try:
        detector = SSHDetect(**params)
        guess = detector.autodetect()
    except Exception as exc:  # noqa: BLE001
        LOG.debug("%s 自动探测异常: %s", dev.host, exc)
        return None
    if guess:
        LOG.info("%s 自动探测结果: %s", dev.host, guess)
    return guess


def connect_with_fallback(dev: Device, args) -> Any:
    """建立连接; 必要时先自动探测类型, 失败则在候选类型里逐个尝试."""
    candidates: List[str] = []
    if dev.device_type.lower() in AUTO_TYPES:
        guess = autodetect_type(dev, args.conn_timeout)
        candidates = ([guess] if guess else []) + AUTODETECT_CANDIDATES
    else:
        candidates = [dev.device_type]

    last_error: Optional[Exception] = None
    for dtype in candidates:
        try:
            conn = ConnectHandler(**dev.netmiko_params(args.conn_timeout, device_type=dtype))
            LOG.debug("%s 使用 %s 连接成功", dev.host, dtype)
            return conn
        except NetmikoAuthenticationException:
            raise  # 账号密码错, 换类型也没用, 直接抛出
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            LOG.debug("%s 用 %s 连接失败: %s", dev.host, dtype, exc)
    raise last_error or RuntimeError("连接失败")


def backup_one(dev: Device, args, out_root: Path, stamp: str) -> Dict[str, Any]:
    """备份单台设备, 返回结构化结果 (不抛异常)."""
    started = time.time()
    result: Dict[str, Any] = {
        "name": dev.name,
        "host": dev.host,
        "group": dev.group,
        "device_type": dev.device_type,
        "status": "failed",
        "hostname": "",
        "command": "",
        "path": "",
        "diff_path": "",
        "changed": True,
        "bytes": 0,
        "sha256": "",
        "seconds": 0.0,
        "error": "",
    }

    problem = dev.validate()
    if problem:
        result["error"] = problem
        LOG.error("[%s] %s", dev.host, problem)
        return result

    conn = None
    try:
        conn = connect_with_fallback(dev, args)

        if dev.resolved_secret:
            try:
                conn.enable()
            except Exception as exc:  # noqa: BLE001
                LOG.debug("%s 进入 enable 失败(可忽略): %s", dev.host, exc)

        prompt = conn.find_prompt()
        hostname = clean_hostname(prompt) or safe_name(dev.name)
        result["hostname"] = hostname
        result["device_type"] = str(getattr(conn, "device_type", "") or dev.device_type)

        command = pick_command(result["device_type"], dev.cmd or args.command)
        result["command"] = command
        LOG.info("[%s] 执行: %s", dev.host, command)

        output = conn.send_command(command, read_timeout=args.read_timeout)

        if not output or len(output.strip()) < 20:
            # 输出异常短, 大概率被分页/权限截断, 用 timing 模式再取一次
            LOG.warning("[%s] 首次输出过短(%s 字节), 改用 send_command_timing 重试",
                        dev.host, len(output or ""))
            output = conn.send_command_timing(command, delay_factor=4, read_timeout=args.read_timeout)

        if not output or not output.strip():
            raise RuntimeError("设备返回空输出, 请检查账号权限或备份命令")

        directory = target_dir(out_root, hostname, dev.host)
        directory.mkdir(parents=True, exist_ok=True)
        cfg_path = directory / f"{stamp}.cfg"
        cfg_path.write_text(output, encoding="utf-8")
        write_latest(directory, output)

        result["path"] = str(cfg_path)
        result["bytes"] = len(output.encode("utf-8", "replace"))
        result["sha256"] = sha256(output)

        if args.diff:
            prev = previous_backup(directory, cfg_path)
            if prev is None:
                result["changed"] = True
            else:
                old_text = prev.read_text(encoding="utf-8", errors="replace")
                if sha256(old_text) == result["sha256"]:
                    result["changed"] = False
                else:
                    diff_text = make_diff(old_text, output, prev.name, cfg_path.name)
                    diff_path = directory / f"{stamp}.diff"
                    diff_path.write_text(diff_text, encoding="utf-8")
                    result["diff_path"] = str(diff_path)

        result["status"] = "ok"
        LOG.info("[%s] 备份完成 -> %s (%s 字节, %s)",
                 dev.host, cfg_path, result["bytes"],
                 "有变更" if result["changed"] else "无变更")

    except NetmikoAuthenticationException as exc:
        result["error"] = f"认证失败: {exc}"
        LOG.error("[%s] 认证失败: %s", dev.host, exc)
    except NetmikoTimeoutException as exc:
        result["error"] = f"连接超时: {exc}"
        LOG.error("[%s] 连接超时: %s", dev.host, exc)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        LOG.error("[%s] 备份失败: %s", dev.host, exc)
    finally:
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:  # noqa: BLE001
                pass
        result["seconds"] = round(time.time() - started, 2)

    return result


# --------------------------------------------------------------------------- #
# 清单读取
# --------------------------------------------------------------------------- #

REQUIRED_COLUMNS = {"host"}


def load_devices(path: Path, defaults: Dict[str, Any]) -> List[Device]:
    if not path.exists():
        sys.exit(f"设备清单不存在: {path}\n请先复制 devices.csv.example 为 devices.csv 并填写。")

    devices: List[Device] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(set(reader.fieldnames)):
            sys.exit(f"{path} 至少要有 host 列")
        for lineno, row in enumerate(reader, start=2):
            if not any((v or "").strip() for v in row.values()):
                continue
            if str(row.get("host", "")).strip().startswith("#"):
                continue
            try:
                devices.append(Device.from_row(row, defaults))
            except Exception as exc:  # noqa: BLE001
                LOG.warning("第 %s 行解析失败, 已跳过: %s", lineno, exc)

    # 去重 (同名同 IP 只保留一条)
    seen = set()
    unique: List[Device] = []
    for dev in devices:
        key = (dev.host, dev.name)
        if key in seen:
            LOG.warning("重复条目已跳过: %s / %s", dev.name, dev.host)
            continue
        seen.add(key)
        unique.append(dev)
    return unique


# --------------------------------------------------------------------------- #
# 报告输出
# --------------------------------------------------------------------------- #

def print_summary(results: List[Dict[str, Any]], elapsed: float) -> None:
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] != "ok"]
    changed = [r for r in ok if r["changed"]]

    line = "=" * 78
    print("\n" + line)
    print(f"备份汇总: 总计 {len(results)} 台 | 成功 {len(ok)} | 失败 {len(failed)} "
          f"| 有变更 {len(changed)} | 耗时 {elapsed:.1f}s")
    print(line)

    if ok:
        print(f"{'设备':<22}{'IP':<18}{'大小':>10}{'状态':>10}  耗时")
        print("-" * 78)
        for r in sorted(ok, key=lambda x: x["name"]):
            state = "变更" if r["changed"] else "未变更"
            print(f"{r['name'][:21]:<22}{r['host']:<18}{r['bytes']:>8}B{state:>10}  {r['seconds']}s")

    if failed:
        print("\n失败明细:")
        print("-" * 78)
        for r in failed:
            print(f"  ✗ {r['name']} ({r['host']}): {r['error']}")
    print(line + "\n")


def write_report(results: List[Dict[str, Any]], logs_dir: Path, stamp: str, elapsed: float) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    report = logs_dir / f"report-{stamp}.json"
    payload = {
        "timestamp": stamp,
        "elapsed_seconds": round(elapsed, 2),
        "total": len(results),
        "success": sum(1 for r in results if r["status"] == "ok"),
        "failed": sum(1 for r in results if r["status"] != "ok"),
        "changed": sum(1 for r in results if r["status"] == "ok" and r["changed"]),
        "devices": results,
    }
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backup.py",
        description="批量登录交换机并备份配置文件 (Netmiko)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-d", "--devices", default="devices.csv", help="设备清单 CSV 路径 (默认 devices.csv)")
    parser.add_argument("-o", "--out", default="backups", help="备份输出目录 (默认 backups/)")
    parser.add_argument("-w", "--workers", type=int, default=10, help="并发数 (默认 10)")
    parser.add_argument("--only", default="", help="只备份指定 group 的设备")
    parser.add_argument("--username", default="", help="覆盖清单里的用户名")
    parser.add_argument("--password", default="", help="覆盖清单里的登录密码")
    parser.add_argument("--secret", default="", help="覆盖清单里的 enable 密码")
    parser.add_argument("--command", default="", help="覆盖默认备份命令 (所有设备统一使用)")
    parser.add_argument("--device-type", default="", help="覆盖清单里的 device_type")
    parser.add_argument("--conn-timeout", type=int, default=15, help="连接超时秒数 (默认 15)")
    parser.add_argument("--read-timeout", type=int, default=180, help="读取配置超时秒数 (默认 180)")
    parser.add_argument("--keep-days", type=int, default=0, help="清理 N 天前的历史备份, 0 表示不清理")
    parser.add_argument("--no-diff", action="store_true", help="不做与上次备份的差异对比")
    parser.add_argument("--dry-run", action="store_true", help="只校验清单和命令, 不实际连接设备")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="日志级别 (默认 INFO)")
    return parser


def setup_logging(level: str, logs_dir: Path, stamp: str) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    LOG.setLevel(getattr(logging, level))
    LOG.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    LOG.addHandler(console)

    file_handler = logging.FileHandler(logs_dir / f"backup-{stamp}.log", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    LOG.addHandler(file_handler)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    args.diff = not args.no_diff

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_root = Path(args.out)
    logs_dir = Path("logs")
    setup_logging(args.log_level, logs_dir, stamp)

    defaults = {
        "username": args.username,
        "password": args.password,
        "secret": args.secret,
        "device_type": args.device_type,
        "cmd": args.command,
    }

    devices = load_devices(Path(args.devices), defaults)
    if args.only:
        devices = [d for d in devices if d.group == args.only]
        if not devices:
            LOG.warning("没有匹配 group=%s 的设备", args.only)

    if not devices:
        LOG.error("没有可备份的设备, 退出")
        return 1

    LOG.info("载入 %s 台设备, 并发 %s, 输出目录 %s", len(devices), args.workers, out_root.resolve())

    if args.dry_run:
        print(f"\n{'设备':<22}{'IP':<18}{'类型':<18}备份命令")
        print("-" * 78)
        for dev in devices:
            cmd = pick_command(dev.device_type, dev.cmd or args.command)
            print(f"{dev.name[:21]:<22}{dev.host:<18}{dev.device_type:<18}{cmd}")
        print(f"\n[dry-run] 共 {len(devices)} 台, 未实际连接。\n")
        return 0

    started = time.time()
    results: List[Dict[str, Any]] = []
    workers = max(1, min(args.workers, len(devices)))
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        tasks = {pool.submit(backup_one, dev, args, out_root, stamp): dev for dev in devices}
        for task in futures.as_completed(tasks):
            results.append(task.result())

    elapsed = time.time() - started
    print_summary(results, elapsed)
    report = write_report(results, logs_dir, stamp, elapsed)
    LOG.info("JSON 报告: %s", report)

    if args.keep_days:
        cleanup_old(out_root, args.keep_days)

    failed = sum(1 for r in results if r["status"] != "ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
