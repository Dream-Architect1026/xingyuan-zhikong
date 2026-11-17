"""构建期法务门禁：外发域名一致性 + LICENSES 收集与完整性断言。

四道闸（都由 build_cuktech._build() 调用，任一不过即 BUILD_FAIL）：
  1) check_domains()    —— 隐私说明里写的域名集合必须 == 代码里出现的域名集合（本机回环不计）。
                           背景：小米云/巴法云曾在 UI 不可见但后端可激活，文档与代码对不上。
  2) collect_licenses() —— 按 _internal 实际打包的第三方包收集许可证原文；包内无原文时生成声明，
                           并断言覆盖齐（缺一即 LICENSES_INCOMPLETE）、协议名单与产物三方一致。
  3) check_mqtt_entry() —— 断言 MQTT 只有一个入口且位于「连接设置」页内（无独立子页/无第二开关）。
  4) check_legal_versions() —— 四份法律文档的“版本：”必须逐字等于 ux_core.CONSENT_VERSION。

本模块导入无副作用，可单独运行：
  python legal_gate.py domains
  python legal_gate.py licenses [internal_dir] [licenses_dir]
"""
import os
import re
import sys
import hashlib
import importlib.metadata as md

HERE = os.path.dirname(os.path.abspath(__file__))
LEGAL_DIR = os.path.join(HERE, 'legal')
CHECK_DATE = '2026-11-13'

LOCAL_HOSTS = {'127.0.0.1', 'localhost', '0.0.0.0', '::1'}
HOST_RE = re.compile(r'https?://([A-Za-z0-9._\-]+)')
# 这些目录不属于"随包代码/产物"，域名闸不扫
CODE_SKIP = {'dist', 'release', 'build', 'build_stage', 'build_stage3', '_verify',
             '__pycache__', 'legal', 'assets', 'mock', 'installer', 'logs'}
# 构建工具自身（本文件里的许可证声明模板含示例 URL），不参与外发域名统计
CODE_SKIP_FILES = {'legal_gate.py'}
# _internal 里属于本应用自己的目录，不算第三方包
APP_DIRS = {'assets', 'backend', 'icons', 'legal', 'numpy.libs', 'pywin32_system32'}
# packages_distributions() 认不出的顶层包名（多为命名空间/兼容目录）
DIST_ALIAS = {'win32': 'pywin32', 'pythonwin': 'pywin32', 'win32com': 'pywin32',
              'Crypto': 'pycryptodomex', 'Cryptodome': 'pycryptodomex',
              'yaml': 'PyYAML', 'winrt': 'winrt-runtime'}
LIC_RE = re.compile(r'^(licen[cs]e|copying|notice)', re.I)
# 合法域名：至少一个点，且不以点开头/结尾（滤掉文档示例里的 https://...）
HOST_OK_RE = re.compile(r'^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$')


# ── 闸一：外发域名一致性 ────────────────────────────────────────────────
def code_hosts(root=HERE):
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in CODE_SKIP]
        for fn in fns:
            if not fn.endswith('.py') or fn in CODE_SKIP_FILES:
                continue
            p = os.path.join(dp, fn)
            try:
                with open(p, encoding='utf-8', errors='ignore') as f:
                    t = f.read()
            except OSError:
                continue
            for h in HOST_RE.findall(t):
                h = h.lower().strip('.')
                if HOST_OK_RE.match(h):
                    out.setdefault(h, set()).add(os.path.relpath(p, root))
    return out


def doc_hosts(legal_dir=LEGAL_DIR, doc_name=u'隐私说明.md'):
    """只读隐私说明里的行内代码域名（`https://...`）——那才是"外发去向"的告知集合。"""
    out = {}
    p = os.path.join(legal_dir, doc_name)
    if not os.path.exists(p):
        raise SystemExit('BUILD_FAIL: DOC_MISSING %s' % p)
    with open(p, encoding='utf-8') as f:
        t = f.read()
    for span in re.findall(r'`([^`]+)`', t):
        for h in HOST_RE.findall(span):
            h = h.lower().strip('.')
            if HOST_OK_RE.match(h):
                out.setdefault(h, set()).add(doc_name)
    return out


def check_domains(root=HERE, legal_dir=LEGAL_DIR, verbose=True):
    ch = {h: v for h, v in code_hosts(root).items() if h not in LOCAL_HOSTS}
    dh = {h: v for h, v in doc_hosts(legal_dir).items() if h not in LOCAL_HOSTS}
    bad = ['IN_CODE_NOT_IN_DOC %s (%s)' % (h, sorted(ch[h])[0]) for h in sorted(set(ch) - set(dh))]
    bad += ['IN_DOC_NOT_IN_CODE %s (%s)' % (h, sorted(dh[h])[0]) for h in sorted(set(dh) - set(ch))]
    if verbose:
        print('DOMAIN_GATE_OK', not bad, 'code=', sorted(ch), 'doc=', sorted(dh))
        for b in bad:
            print('DOMAIN_MISMATCH', b)
    if bad:
        raise SystemExit('BUILD_FAIL: DOMAIN_MISMATCH ' + '; '.join(bad))
    return {'code': sorted(ch), 'doc': sorted(dh)}


# ── 闸二：LICENSES 收集与完整性 ────────────────────────────────────────
def components(internal):
    """按 _internal 实际内容算出第三方组件集合。

    有 dist-info 的按 dist-info；没有 dist-info 的（PySide6/shiboken6/psutil/pywin32 等
    PyInstaller 只复制包体、不复制元数据的）按顶层目录 + 构建环境元数据反查。
    """
    p2d = md.packages_distributions()
    comps = {}
    entries = sorted(os.listdir(internal))
    for d in entries:
        p = os.path.join(internal, d)
        if os.path.isdir(p) and d.endswith('.dist-info'):
            nm, _, ver = d[:-10].rpartition('-')
            comps[nm.replace('_', '-').lower()] = {'name': nm, 'version': ver, 'dists': [nm]}
    for d in entries:
        p = os.path.join(internal, d)
        if not os.path.isdir(p) or d.endswith('.dist-info') or d in APP_DIRS:
            continue
        if not any(fns for _dp, _dn, fns in os.walk(p)):   # 空壳目录不算组件
            continue
        cands = p2d.get(d) or p2d.get(d.replace('-', '_')) or []
        if not cands and d in DIST_ALIAS:
            cands = [DIST_ALIAS[d]]
        if not cands:
            cands = [d]
        keys = [c.replace('_', '-').lower() for c in cands]
        if set(keys) & set(comps):
            # 这个目录属于已有 dist-info 的组件（如 Cryptodome→pycryptodomex、yaml→PyYAML）
            continue
        ver = ''
        for c in cands:
            try:
                ver = md.version(c)
                break
            except Exception:
                continue
        key = keys[0] if len(keys) == 1 else d.replace('_', '-').lower()
        comps[key] = {'name': cands[0] if len(cands) == 1 else d, 'version': ver, 'dists': list(cands)}
    return comps


def _meta_of(dist_name):
    try:
        return md.metadata(dist_name)
    except Exception:
        return None


def _license_ids(dists):
    ids = []
    for c in dists:
        m = _meta_of(c)
        if m is None:
            continue
        v = m.get('License-Expression') or m.get('License') or ''
        v = ' '.join(str(v).split())
        if v and v not in ids:
            ids.append(v)
        for cl in m.get_all('Classifier') or []:
            if cl.startswith('License ::') and cl not in ids:
                ids.append(cl)
    return ids


def _component_license_files(internal, dists):
    """先找 _internal 里的 dist-info，再从构建环境 dist-info 补（PyInstaller 不复制元数据）。"""
    files = []
    want = {c.replace('_', '-').lower() for c in dists}
    for d in os.listdir(internal):
        if not d.endswith('.dist-info'):
            continue
        nm = d[:-10].rpartition('-')[0].replace('_', '-').lower()
        if nm not in want:
            continue
        root = os.path.join(internal, d)
        for dp, _dn, fns in os.walk(root):
            for fn in fns:
                if LIC_RE.match(fn):
                    files.append(os.path.join(dp, fn))
    for c in dists:
        try:
            dist = md.distribution(c)
        except Exception:
            continue
        for f in dist.files or []:
            s = str(f).replace('\\', '/')
            if '.dist-info/' not in s:
                continue
            tail = s.split('.dist-info/', 1)[1]
            base = os.path.basename(tail)
            if not (LIC_RE.match(base) or tail.startswith('licenses/')):
                continue
            p = str(dist.locate_file(f))
            try:
                if os.path.isfile(p) and os.path.getsize(p) <= 512 * 1024 and p not in files:
                    files.append(p)
            except OSError:
                pass
    return files


def _homepage(dists):
    for c in dists:
        m = _meta_of(c)
        if m is None:
            continue
        h = m.get('Home-page') or ''
        if h:
            return h.strip()
        for u in m.get_all('Project-URL') or []:
            if u.lower().startswith(('repository', 'homepage', 'source', 'documentation')):
                return u.split(',', 1)[-1].strip()
    return ''


def collect_licenses(internal, licenses_dir, verbose=True):
    comps = components(internal)
    if os.path.isdir(licenses_dir):
        import shutil
        shutil.rmtree(licenses_dir)
    os.makedirs(licenses_dir, exist_ok=True)

    gnu_dir = os.path.join(licenses_dir, '_GNU')
    os.makedirs(gnu_dir, exist_ok=True)
    for fn in ('LGPL-3.0.txt', 'GPL-3.0.txt'):
        src = os.path.join(LEGAL_DIR, fn)
        if os.path.exists(src):
            with open(src, 'rb') as fs, open(os.path.join(gnu_dir, fn), 'wb') as fd:
                fd.write(fs.read())

    manifest = ['# LICENSES 清单（构建时自动生成，勿手改）',
                '# 核查日期：%s' % CHECK_DATE,
                '# 每个第三方组件至少一个文件：有原文则复制原文，无原文则生成 DECLARATION.txt', '']
    covered, n_files = [], 0
    for key in sorted(comps):
        c = comps[key]
        cdir = os.path.join(licenses_dir, key)
        os.makedirs(cdir, exist_ok=True)
        srcs = _component_license_files(internal, c['dists'])
        copied, seen = [], set()
        for s in srcs:
            with open(s, 'rb') as f:
                data = f.read()
            digest = hashlib.md5(data).hexdigest()
            if digest in seen:          # 同一份原文被多个 dist 副本带出，只留一份
                continue
            seen.add(digest)
            base = os.path.basename(s)
            dst = os.path.join(cdir, base)
            if os.path.exists(dst):
                dst = os.path.join(cdir, '%s_%s' % (digest[:6], base))
            with open(dst, 'wb') as f:
                f.write(data)
            copied.append(os.path.basename(dst))
        ids = _license_ids(c['dists'])
        needs_gnu = any('LGPL' in i or 'GPL' in i for i in ids) or key.startswith(('pyside6', 'shiboken6'))
        if needs_gnu and 'DECLARATION.txt' not in copied:
            decl = ['%s %s' % (c['name'], c.get('version') or ''),
                    '许可证标识：%s' % (' ; '.join(ids) or '发行包内未声明'),
                    '包主页：%s' % (_homepage(c['dists']) or '发行包内未提供'),
                    '核查日期：%s' % CHECK_DATE,
                    '本项目按 LGPL-3.0 使用（动态链接、未修改）。',
                    '许可证全文：见 _GNU/LGPL-3.0.txt 与 _GNU/GPL-3.0.txt；'
                    'Qt 源码获取方式见 https://www.qt.io/ 或联系 dreamarchitect1026@gmail.com。']
            with open(os.path.join(cdir, 'DECLARATION.txt'), 'w', encoding='utf-8') as f:
                f.write('\n'.join(decl) + '\n')
            copied.append('DECLARATION.txt')
        if not copied:
            decl = ['%s %s' % (c['name'], c.get('version') or ''),
                    '许可证标识：%s' % (' ; '.join(ids) or '发行包内未声明'),
                    '包主页：%s' % (_homepage(c['dists']) or '发行包内未提供'),
                    '核查日期：%s' % CHECK_DATE,
                    '说明：发行包内未附许可证原文文件，本文件为该组件的许可证声明记录。']
            with open(os.path.join(cdir, 'DECLARATION.txt'), 'w', encoding='utf-8') as f:
                f.write('\n'.join(decl) + '\n')
            copied = ['DECLARATION.txt']
        if copied:
            covered.append(key)
        n_files += len(copied)
        manifest.append('%s | %s | %s | %s | %s'
                        % (key, c.get('version') or '-', ' ; '.join(ids) or '未声明',
                           _homepage(c['dists']) or '-', ','.join(copied)))
    with open(os.path.join(licenses_dir, 'MANIFEST.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(manifest) + '\n')

    uncovered = [k for k in comps if k not in covered]
    ok = (not uncovered) and n_files >= len(comps)
    if verbose:
        print('LICENSES_COVERAGE %d/%d files=%d' % (len(comps) - len(uncovered), len(comps), n_files))
        print('LICENSES_UNCOVERED', uncovered)
    if not ok:
        raise SystemExit('BUILD_FAIL: LICENSES_INCOMPLETE missing=%s files=%d components=%d'
                         % (uncovered, n_files, len(comps)))
    return {'components': len(comps), 'files': n_files, 'uncovered': uncovered,
            'keys': sorted(comps)}


# ── 三方一致：协议名单 ↔ LICENSES 目录 ↔ 实际打包集合 ────────────────────
def _doc_tokens(doc_path):
    toks = []
    with open(doc_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line.startswith('|'):
                continue
            cell = line.split('|')[1].strip()
            if not cell or cell.startswith('---') or cell == '组件':
                continue
            cell = re.sub(r'[（(][^）)]*[）)]', '', cell)
            for t in re.findall(r'[A-Za-z][A-Za-z0-9._*-]*', cell):
                if t.lower() == 'qt':
                    continue
                toks.append(t.lower())
    return toks


def check_component_docs(comp_keys, licenses_dir, doc_path):
    toks = _doc_tokens(doc_path)
    matched = set()
    bad_doc = []
    for t in toks:
        pref = t.rstrip('*')
        hit = [k for k in comp_keys if k.startswith(pref)]
        if not hit:
            bad_doc.append(t)
        matched.update(hit)
    missing_doc = [k for k in comp_keys if k not in matched]
    dirs = sorted(d for d in os.listdir(licenses_dir)
                  if os.path.isdir(os.path.join(licenses_dir, d)) and d != '_GNU')
    missing_dir = [k for k in comp_keys if k not in dirs]
    print('THIRDPARTY_CONSISTENCY doc_tokens=%d components=%d licenses_dirs=%d'
          % (len(toks), len(comp_keys), len(dirs)))
    if bad_doc:
        print('DOC_ONLY', bad_doc)
    if missing_doc:
        print('DOC_MISSING', missing_doc)
    if missing_dir:
        print('LICENSES_MISSING_DIR', missing_dir)
    if bad_doc or missing_doc or missing_dir:
        raise SystemExit('BUILD_FAIL: THIRDPARTY_MISMATCH doc_only=%s doc_missing=%s dir_missing=%s'
                         % (bad_doc, missing_doc, missing_dir))
    print('THIRDPARTY_GATE_OK True')


# ── 闸五：包内《第三方组件与开源许可》数字 ↔ LICENSES 实物 ────────────────
# 背景（）：dist 复制 legal 文档早于 4.4 数字定稿，导致已发出的许可文件写
# 「36 个组件 / 37 个目录 / 36/36」，实际 37/38/37-37。清单的全部价值就是可信，
# 所以出包后打开包内那份原文抽数字，与 components()/LICENSES 实算值比对，不等即失败。
_DOC_COMP_RE = re.compile(u'实际收录\\s*\\*\\*(\\d+)\\s*个组件\\*\\*')
_DOC_DIR_RE = re.compile(u'下\\s*(\\d+)\\s*个目录')
_DOC_COV_RE = re.compile(u'覆盖率\\s*(\\d+)\\s*/\\s*(\\d+)')


def check_packaged_licenses(internal, doc_path=None, verbose=True):
    """包内许可清单自述数字必须等于实际组件数/目录数/覆盖率。"""
    doc_path = doc_path or os.path.join(internal, 'legal', u'第三方组件与开源许可.md')
    if not os.path.exists(doc_path):
        raise SystemExit('BUILD_FAIL: PACKAGED_DOC_MISSING ' + doc_path)
    txt = open(doc_path, encoding='utf-8').read()
    got = {'components': _DOC_COMP_RE.search(txt),
           'dirs': _DOC_DIR_RE.search(txt),
           'coverage': _DOC_COV_RE.search(txt)}
    want = len(components(internal))
    lic_dir = os.path.join(os.path.dirname(os.path.abspath(internal)), 'LICENSES')
    real_dirs = (len([d for d in os.listdir(lic_dir)
                      if os.path.isdir(os.path.join(lic_dir, d))])
                 if os.path.isdir(lic_dir) else -1)
    problems = []
    if not got['components']:
        problems.append('NO_COMPONENT_NUMBER')
    elif int(got['components'].group(1)) != want:
        problems.append('DOC_COMPONENTS %s != actual %d'
                        % (got['components'].group(1), want))
    if not got['dirs']:
        problems.append('NO_DIR_NUMBER')
    elif int(got['dirs'].group(1)) != real_dirs:
        problems.append('DOC_DIRS %s != actual %d'
                        % (got['dirs'].group(1), real_dirs))
    if not got['coverage']:
        problems.append('NO_COVERAGE_NUMBER')
    else:
        c, t = int(got['coverage'].group(1)), int(got['coverage'].group(2))
        if c != want or t != want:
            problems.append('DOC_COVERAGE %d/%d != %d/%d' % (c, t, want, want))
    if verbose:
        print('PACKAGED_LICENSES_OK', not problems,
              'doc_components=', got['components'].group(1) if got['components'] else None,
              'doc_dirs=', got['dirs'].group(1) if got['dirs'] else None,
              'doc_coverage=', got['coverage'].group(0) if got['coverage'] else None,
              'actual_components=', want, 'actual_dirs=', real_dirs)
        for p in problems:
            print('PACKAGED_LICENSES_FAIL', p)
    if problems:
        raise SystemExit('BUILD_FAIL: PACKAGED_DOC_MISMATCH ' + '; '.join(problems))
    return {'components': want, 'dirs': real_dirs}


# ── 闸六：包根必须带仓库根 LICENSE（MIT），首行逐字为 "MIT License" ──────────
# 仓库根 LICENSE 覆盖 cuktech/；仓库怎么授权，包就怎么声明——包根缺这份文件
# 等于自相矛盾。其它产品的包根同样放 LICENSE，位置保持一致。
def check_root_license(package_dir, verbose=True):
    p = os.path.join(package_dir, 'LICENSE')
    first = None
    problems = []
    if not os.path.isfile(p):
        problems.append('MISSING %s' % p)
    else:
        with open(p, encoding='utf-8', errors='ignore') as f:
            for line in f:
                if line.strip():
                    first = line.strip()
                    break
        if first != 'MIT License':
            problems.append('FIRST_LINE %r' % first)
    if verbose:
        print('PACKAGE_LICENSE_OK', not problems, 'first_line=', first)
        for x in problems:
            print('PACKAGE_LICENSE_FAIL', x)
    if problems:
        raise SystemExit('BUILD_FAIL: PACKAGE_LICENSE ' + '; '.join(problems))
    return {'path': p, 'first_line': first}


# ── 闸三：MQTT 只有一个入口，且必须在「连接设置」页内（防回潮/防第二套）──
# 1.5.1 按用户决定恢复 MQTT 客户端，但不许回到独立子页、不许出现第二处开关。
# 断言：
#   a) launcher 里没有以 MQTT 命名的独立子页（new_tab('MQTT...')）；
#   b) 启用开关只有一处（add_check(...,'mqtt','enabled',...) 恰好 1 次）；
#   c) 该卡通过 _conn_row 加进连接设置页（card_form(_conn_row,'MQTT... + v_conn.addLayout）；
#   d) 后端确实有 MQTT 客户端（paho import + setup_mqtt），默认关闭；
#   e) 随包 LICENSES 必须有 paho-mqtt 原文目录。
def check_mqtt_entry(root=HERE, licenses_dir=None, verbose=True):
    launcher = os.path.join(root, 'cuktech_launcher.py')
    server = os.path.join(root, 'backend', 'ha_server.py')
    cfg = os.path.join(root, 'backend', 'config.py')
    problems = []
    try:
        lt = open(launcher, encoding='utf-8', errors='ignore').read()
        st = open(server, encoding='utf-8', errors='ignore').read()
        ct = open(cfg, encoding='utf-8', errors='ignore').read()
    except OSError as e:
        problems.append('READ_FAIL %s' % e)
        lt = st = ct = ''
    # a) 不许独立 MQTT 子页
    if re.search(r"new_tab\s*\(\s*['\"][^'\"]*MQTT", lt):
        problems.append('MQTT_SUBPAGE')
    # b) 只有一处启用开关
    n_sw = len(re.findall(r"add_check\s*\([^)]*['\"]mqtt['\"]\s*,\s*['\"]enabled['\"]", lt))
    if n_sw != 1:
        problems.append('MQTT_SWITCH_COUNT %d' % n_sw)
    if len(re.findall(r"QCheckBox\s*\([^)]*MQTT", lt)) > 0:
        problems.append('RAW_MQTT_CHECKBOX')
    # c) 必须挂在连接设置页的并排行里
    if not re.search(r"card_form\s*\(\s*_conn_row\s*,\s*['\"]MQTT", lt):
        problems.append('MQTT_NOT_IN_CONN_ROW')
    if 'v_conn.addLayout(_conn_row)' not in lt:
        problems.append('CONN_ROW_NOT_ADDED')
    # d) 后端客户端存在且默认关闭
    if 'import paho.mqtt.client as mqtt' not in st:
        problems.append('PAHO_IMPORT_MISSING')
    if 'async def setup_mqtt' not in st:
        problems.append('SETUP_MQTT_MISSING')
    if not re.search(r"class MQTTConfig[\s\S]*?enabled:\s*bool\s*=\s*False", ct):
        problems.append('MQTT_DEFAULT_NOT_OFF')
    # e) 许可原文
    bad_lic = []
    if licenses_dir and os.path.isdir(licenses_dir):
        bad_lic = [n for n in os.listdir(licenses_dir) if 'paho' in n.lower()]
        if not bad_lic:
            problems.append('LICENSES_PAHO_MISSING')
    if verbose:
        print('MQTT_ENTRY_GATE_OK', not problems,
              'switch=', n_sw, 'licenses=', bad_lic)
        for x in problems:
            print('MQTT_ENTRY_GATE_FAIL', x)
    if problems:
        raise SystemExit('BUILD_FAIL: MQTT_ENTRY ' + '; '.join(problems))
    return {'switch': n_sw, 'licenses': bad_lic}


# ── 闸四：四份法律文档与 CONSENT_VERSION 同源 ─────────────────────────
LEGAL_DOCS = [u'用户协议.md', u'隐私说明.md', u'安全与免责提示.md',
              u'第三方组件与开源许可.md']


def consent_version(ux_path=None):
    """从 ux_core.py 读 CONSENT_VERSION（不 import，避免把 PySide6 拉进构建门禁）。"""
    ux_path = ux_path or os.path.join(HERE, 'ux_core.py')
    with open(ux_path, encoding='utf-8') as f:
        m = re.search(r"^CONSENT_VERSION\s*=\s*'([^']+)'", f.read(), re.M)
    if not m:
        raise SystemExit('BUILD_FAIL: CONSENT_VERSION_NOT_FOUND')
    return m.group(1)


def check_legal_versions(legal_dir=LEGAL_DIR, ux_path=None, verbose=True):
    """四份文档的“版本：”必须逐字等于 CONSENT_VERSION。

    背景：用户协议/隐私说明曾写 v2、安全与免责/第三方许可停在 v1，而首启同意只记
    CONSENT_VERSION，导致“与程序内 CONSENT_VERSION 同源”这句话在代码里没有落地。
    """
    want = consent_version(ux_path)
    got, problems = {}, []
    for name in LEGAL_DOCS:
        p = os.path.join(legal_dir, name)
        if not os.path.exists(p):
            problems.append('MISSING_DOC %s' % name)
            continue
        with open(p, encoding='utf-8') as f:
            m = re.search(u'版本：\\s*([A-Za-z0-9._\\-]+)', f.read())
        got[name] = m.group(1) if m else None
        if not m:
            problems.append('NO_VERSION %s' % name)
        elif m.group(1) != want:
            problems.append('VERSION_DRIFT %s=%s want=%s'
                            % (name, m.group(1), want))
    if verbose:
        print('LEGAL_VERSION_OK', not problems, 'consent=', want, 'docs=', got)
        for p in problems:
            print('LEGAL_VERSION_FAIL', p)
    if problems:
        raise SystemExit('BUILD_FAIL: LEGAL_VERSION_DRIFT ' + '; '.join(problems))
    return {'want': want, 'docs': got}


def main(argv):
    mode = argv[1] if len(argv) > 1 else 'all'
    internal = argv[2] if len(argv) > 2 else os.path.join(HERE, 'dist', 'CuktechPanel', '_internal')
    ldir = argv[3] if len(argv) > 3 else os.path.join(os.path.dirname(internal), 'LICENSES')
    if mode in ('domains', 'all'):
        check_domains()
        check_mqtt_entry()
        check_legal_versions()
    if mode in ('licenses', 'all'):
        r = collect_licenses(internal, ldir)
        check_mqtt_entry(licenses_dir=ldir)
        check_component_docs(r['keys'], ldir, os.path.join(LEGAL_DIR, '第三方组件与开源许可.md'))
        check_packaged_licenses(internal)
        check_root_license(os.path.dirname(os.path.abspath(internal)))
    if mode == 'packaged':
        check_packaged_licenses(internal)
        check_root_license(os.path.dirname(os.path.abspath(internal)))


if __name__ == '__main__':
    main(sys.argv)
