"""· 1.3.3 便携版出包（不依赖 Inno Setup）。

流程：
  1) 三值一致断言（复用 version_gate.py，与 build_cuktech.py 同一份实现）
  2) 组装 release/CuktechPanel-<VERSION>-portable/（dist 产物整目录 + 便携说明 + 清单）
  3) 打 zip（含目录名，解压即用），打印体积与 sha256

不安装任何第三方工具；安装包（setup.exe）编译见 installer/build_installer.ps1。
断言不过直接退出 1，不出包。
"""
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile

import version_gate

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(HERE, 'VERSION')
DIST_DIR = os.path.join(HERE, 'dist', 'CuktechPanel')
EXE = os.path.join(DIST_DIR, 'CuktechPanel.exe')
REL_DIR = os.path.join(HERE, 'release')

PORTABLE_TXT = """星源智控控制面板 {ver} · 便携版（Windows x64）

用法
  1. 解压后整个目录放在任意位置（U 盘 / 桌面均可），双击 CuktechPanel.exe 启动。
  2. 不需要安装、不需要管理员权限；卸载 = 删掉整个目录。
  3. 本目录自带运行库与字体（_internal/），不要只拷 exe。

数据位置
  配置与历史库写在 %LOCALAPPDATA%\\CuktechPanel（不随目录移动）；
  换机迁移时一并拷走该目录即可保留会话与设置。

首次启动
  会弹出《隐私说明》与《用户协议》（同意版本 {consent}）。不启用 AI 分析时，
  端口控制、状态查看、历史记录与自动关闭全部在本机运行、不联网；
  拒绝同意也只停用 AI，离线功能仍可用。
  同意记录写在 HKCU\\Software\\DoubaoWork\\CuktechPanel，删掉本目录不会清除，
  需要彻底清理由「设置 → 进阶配置 → 隐私与协议」撤回，或手动删除该注册表项。

版本
  本包版本 {ver}；VERSION 文件、exe 文件属性、程序 --version 三处一致（出包前已断言）。
"""


def sha256(path, buf=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(buf)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _consent_version():
    """从 ux_core.py 读 CONSENT_VERSION（不 import ux_core，避免出包脚本拉起 Qt）。"""
    try:
        with open(os.path.join(HERE, 'ux_core.py'), 'r', encoding='utf-8') as f:
            m = re.search(r"^CONSENT_VERSION\s*=\s*'([^']+)'", f.read(), re.M)
        return m.group(1) if m else '未标注'
    except OSError:
        return '未标注'


def main():
    if not os.path.isfile(EXE):
        print('PACKAGE_FAIL: 产物不存在', EXE)
        return 1
    gate = version_gate.verify(EXE, VERSION_FILE, HERE)
    if not gate['ok']:
        print('PACKAGE_FAIL: version_gate', gate)
        return 1
    ver = gate['version']
    os.makedirs(REL_DIR, exist_ok=True)
    stage = os.path.join(REL_DIR, 'CuktechPanel-%s-portable' % ver)
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    shutil.copytree(DIST_DIR, stage)
    # 包内《第三方组件与开源许可》自述数字必须等于实物（复制早于文档定稿即失败）
    from legal_gate import check_packaged_licenses, check_root_license
    try:
        check_packaged_licenses(os.path.join(stage, '_internal'))
        check_root_license(stage)
    except SystemExit as e:
        print('PACKAGE_FAIL: packaged_licenses', e)
        return 1
    with open(os.path.join(stage, '便携说明.txt'), 'w', encoding='utf-8') as f:
        f.write(PORTABLE_TXT.format(ver=ver, consent=_consent_version()))
    exe_sha = sha256(os.path.join(stage, 'CuktechPanel.exe'))
    manifest = {'app': 'CuktechPanel', 'version': ver,
                'built_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'exe': 'CuktechPanel.exe', 'exe_bytes': os.path.getsize(os.path.join(stage, 'CuktechPanel.exe')),
                'exe_sha256': exe_sha,
                'version_gate': {'ok': gate['ok'], 'version': gate['version'],
                                 'footer': gate['footer'], 'file': gate['file']}}
    with open(os.path.join(stage, 'PORTABLE_MANIFEST.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    zpath = os.path.join(REL_DIR, 'CuktechPanel-%s-portable-win64.zip' % ver)
    if os.path.exists(zpath):
        os.remove(zpath)
    with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, _dirs, files in os.walk(stage):
            for fn in files:
                p = os.path.join(root, fn)
                z.write(p, os.path.relpath(p, REL_DIR))
    print('PORTABLE_DIR', stage)
    print('PORTABLE_FILES', sum(len(fs) for _r, _d, fs in os.walk(stage)))
    print('PORTABLE_ZIP', zpath, os.path.getsize(zpath))
    print('PORTABLE_SHA256', sha256(zpath))
    print('PORTABLE_EXE_SHA256', exe_sha)
    return 0


if __name__ == '__main__':
    sys.exit(main())
