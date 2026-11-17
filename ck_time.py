"""时间格式化单源（仅标准库，不依赖 Qt）。

改前 launcher 里有 21 处 ``strftime`` 散写，实际只有 5 个格式串：

    full   2026-11-13 21:04:05   日志时间戳 / JSONL ts / 退出留痕
    hms    21:04:05              调试行前缀 / 图表读数 / 端口“最后更新”
    hm     21:04                 图表轴宽刻度（spacing >= 60）
    md_hm  11-13 21:04           会话列表 / 会话详情时间列
    stamp  20261113_210405       导出文件名（AI 调用记录 / 诊断包）

口径变化只在这里改一次；调用方一律 ``ck_time.fmt(kind, t)``：

    t=None           当前时间
    t=float/int      epoch 秒（按本地时区展开，与原 time.localtime 一致）
    t=struct_time    直传（图表轴 tickStrings 已拿到 localtime 结果）
    t=datetime       直传（原 datetime.fromtimestamp(...).strftime 等价）
"""
import datetime as _datetime
import time

__all__ = ['FULL', 'HMS', 'HM', 'MD_HM', 'STAMP', 'PATTERNS', 'fmt']

FULL = 'full'
HMS = 'hms'
HM = 'hm'
MD_HM = 'md_hm'
STAMP = 'stamp'

PATTERNS = {
    FULL: '%Y-%m-%d %H:%M:%S',
    HMS: '%H:%M:%S',
    HM: '%H:%M',
    MD_HM: '%m-%d %H:%M',
    STAMP: '%Y%m%d_%H%M%S',
}


def fmt(kind=FULL, t=None):
    """按 kind 格式化时间；kind 非法直接抛 ValueError（拼错不静默）。"""
    pat = PATTERNS.get(kind)
    if pat is None:
        raise ValueError('ck_time.fmt: unknown kind %r' % (kind,))
    if t is None:
        return time.strftime(pat)
    if isinstance(t, _datetime.datetime):
        return t.strftime(pat)
    if isinstance(t, time.struct_time):
        return time.strftime(pat, t)
    return time.strftime(pat, time.localtime(float(t)))
