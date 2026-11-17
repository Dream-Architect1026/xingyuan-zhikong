"""版本单源：三值一致断言（VERSION / exe FileVersion / 冻结点 ux.APP_VERSION）。

构建末段门禁（build_cuktech.py）与出包前门禁（package_portable.py）共用本模块，
保证两处判定口径完全一致，不再各写一遍。只做只读探测，不改产物。

三值：
  VERSION 文件                         —— 唯一手写来源
  exe.VersionInfo.FileVersion          —— PyInstaller 写入
  exe --version（读冻结的 ux.APP_VERSION）—— 页脚同源
"""
import os
import subprocess

# ── UTF-8 BOM 门禁 ───────────────────────────────────────────────────────
# 背景（2026-11-12）：用 PowerShell `Set-Content` 改 VERSION 时写入了 UTF-8 BOM，
# version_gate 以 utf-8-sig 读（BOM 被吞掉，看起来正常），但冻结包里的
# ux._ck_read_version 用 utf-8 读 → 版本串变成 "\ufeff1.3.4" → exe 启动即
# "Unhandled exception in script"，VERSION_PROBE 被崩溃对话框卡到超时。
# 因此：源头文件与生成文件都必须「首三字节不是 EF BB BF」，违反即 BUILD_FAIL。
BOM = b'\xef\xbb\xbf'
_CK = os.path.dirname(os.path.abspath(__file__))


def default_sources():
    """VERSION（手写唯一来源）+ 生成文件（version_info / ui_kit）。"""
    return [os.path.join(_CK, 'VERSION'),
            os.path.join(os.path.dirname(_CK), 'version_info_cuktech.txt'),
            os.path.join(_CK, 'ui_kit.py')]


def check_bom(paths):
    """逐文件查首三字节。返回 (rows, bad)；rows 为 (path, state) 列表。"""
    rows, bad = [], []
    for p in paths:
        try:
            with open(p, 'rb') as f:
                head = f.read(3)
        except OSError as exc:
            rows.append((p, 'UNREADABLE'))
            bad.append((p, 'UNREADABLE:%s' % exc))
            continue
        if head == BOM:
            rows.append((p, 'BOM'))
            bad.append((p, 'BOM'))
        elif not head:
            rows.append((p, 'EMPTY'))
            bad.append((p, 'EMPTY'))
        else:
            rows.append((p, 'OK'))
    return rows, bad


def verify_sources(paths=None):
    """BOM 门禁。不合格直接 BUILD_FAIL（构建前置，早于 PyInstaller）。"""
    paths = list(paths or default_sources())
    rows, bad = check_bom(paths)
    for p, st in rows:
        print('BOM_FILE', st, os.path.basename(p))
    print('BOM_GATE_OK', not bad, 'files=%d bad=%d' % (len(paths), len(bad)))
    if bad:
        raise SystemExit('BUILD_FAIL: UTF-8 BOM/空文件（首三字节必须不是 EF BB BF）：%s'
                         % ', '.join('%s:%s' % (os.path.basename(p), r) for p, r in bad))
    return {'ok': not bad, 'files': rows}


def read_version(version_file):
    with open(version_file, 'r', encoding='utf-8-sig') as f:
        return f.read().strip()


def probe_footer(exe, work_dir, timeout=120):
    """跑 exe --version（由程序把文本写到 CK_VERSION_OUT），返回 (footer, rc, stdout)。"""
    out = os.path.join(work_dir, '_version_probe.txt')
    try:
        if os.path.exists(out):
            os.remove(out)
    except OSError:
        pass
    env = dict(os.environ)
    env['CK_VERSION_OUT'] = out
    rc, so = None, ''
    try:
        r = subprocess.run([exe, '--version'], capture_output=True, text=True,
                           timeout=timeout, env=env)
        rc, so = r.returncode, (r.stdout or '').strip()
    except (OSError, subprocess.SubprocessError) as exc:
        print('VERSION_PROBE_ERR', exc)
    print('VERSION_PROBE_RC', rc, 'stdout=', so)
    footer = ''
    try:
        with open(out, 'r', encoding='utf-8') as f:
            footer = f.read().strip().split()[-1]
    except (OSError, IndexError):
        footer = ''
    return footer, rc, so


def probe_file_version(exe, timeout=90):
    ps = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         "(Get-Item -LiteralPath '%s').VersionInfo.FileVersion" % exe.replace("'", "''")],
        capture_output=True, text=True, timeout=timeout)
    return (ps.stdout or '').strip()


def verify(exe, version_file, work_dir):
    """执行断言并打印与旧门禁同格式的两行输出，返回结果字典（只读）。"""
    verify_sources()          # 先过 BOM 闸，避免 utf-8-sig 把 BOM 吞掉后误判通过
    ver = read_version(version_file)
    if not ver:
        raise SystemExit('BUILD_FAIL: VERSION 为空')
    footer, rc, _so = probe_footer(exe, work_dir)
    file_ver = probe_file_version(exe)
    ok = bool(footer) and (ver == footer == file_ver)
    print('VERSION_GATE_OK', ok, 'VERSION=', ver, 'footer=', footer, 'file=', file_ver)
    return {'ok': ok, 'version': ver, 'footer': footer, 'file': file_ver, 'probe_rc': rc}


if __name__ == '__main__':
    # 只跑 BOM 闸（供负向自证：python version_gate.py 带 BOM 的 VERSION 必须 rc=1）
    verify_sources()
    print('BOM_GATE_ONLY_OK')
