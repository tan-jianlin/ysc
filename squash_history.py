# -*- coding: utf-8 -*-
"""把 Git 提交历史压缩成单条根提交，并同步强推 Gitee + GitHub。

背景（2026-10-20）：
    每天自动更新至少产生 1 条 "auto update" 提交，一年就是 730 条。
    提交历史本身不影响电视端（它只读最新 config），但仓库页面会被刷屏，
    而且仓库体积只增不减。这个脚本把历史合并成一个基线提交，
    内容一字不改，只把"过程记录"折掉。

安全设计：
    - 执行前把 .git 完整复制到 ../.git-bak-<时间戳>，随时可以整体还原；
    - 先推 Gitee（国内必达），再推 GitHub；Gitee 失败就中止，不动 GitHub；
    - 全程只改 git 元数据，工作区文件不删除、不重写；
    - --dry-run 只打印计划，不做任何写操作。

用法：
    python squash_history.py --dry-run
    python squash_history.py
"""
import base64
import datetime
import json
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import update_sources as U   # 复用令牌获取、IP 绕过、API 调用与日志

DRY = '--dry-run' in sys.argv
# 中途失败（比如分支改名撞名）后，从"第 4 步"接着做完，不必重跑前面已经成功的步骤
RESUME = '--resume' in sys.argv
TMP_BRANCH = '__squash_tmp__'


def git(*args, **kw):
    """跑 git 命令，返回 (rc, stdout, stderr)。"""
    r = subprocess.run(['git', '-C', BASE] + list(args),
                       capture_output=True, text=True, timeout=kw.get('timeout', 180))
    return r.returncode, (r.stdout or '').strip(), (r.stderr or '').strip()


def log(msg):
    print(msg, flush=True)


def step(msg):
    log('')
    log('▶ ' + msg)


def backup_git():
    ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    dst = os.path.join(os.path.dirname(BASE), '.git-bak-%s' % ts)
    if os.path.exists(dst):
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(os.path.join(BASE, '.git'), dst)
    size = sum(os.path.getsize(os.path.join(dp, f))
               for dp, _dn, fns in os.walk(dst) for f in fns)
    log('  备份 .git -> %s（%.1f MB）' % (dst, size / 1024.0 / 1024.0))
    return dst


def gh_blobs_and_tree(files):
    """把本地文件逐份写成 GitHub blob，再组装成一棵完整的树。"""
    entries = []
    for rel, full in files:
        with open(full, 'rb') as f:
            raw = f.read()
        data = U._gh_api('POST', '/repos/%s/%s/git/blobs' % (U.GH_OWNER, U.GH_REPO),
                         {'content': base64.b64encode(raw).decode('ascii'),
                          'encoding': 'base64'})
        if not data or not data.get('sha'):
            raise RuntimeError('blob 创建失败：%s' % rel)
        entries.append({'path': rel.replace('\\', '/'), 'mode': '100644',
                        'type': 'blob', 'sha': data['sha']})
    tree = U._gh_api('POST', '/repos/%s/%s/git/trees' % (U.GH_OWNER, U.GH_REPO),
                     {'tree': entries})
    if not tree or not tree.get('sha'):
        raise RuntimeError('tree 创建失败')
    return tree['sha'], len(entries)


def count_remote_commits():
    import re
    js, hdrs = None, None
    # 借 _gh_api 的底层能力：这里只需要一页，靠 Link 头数总数
    token = U._gh_token()
    if not token:
        return None
    import socket, urllib.request
    orig = socket.getaddrinfo
    for ip in (U._gh_resolve_ips() or ['20.205.243.168']):
        def pinned(host, port, *a, **kw):
            return orig(ip, port, *a, **kw) if host == 'api.github.com' else orig(host, port, *a, **kw)
        socket.getaddrinfo = pinned
        try:
            req = urllib.request.Request(
                U.GH_API + '/repos/%s/%s/commits?per_page=1' % (U.GH_OWNER, U.GH_REPO),
                headers={'Authorization': 'token ' + token,
                         'Accept': 'application/vnd.github+json',
                         'User-Agent': 'ysc-squash'})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=30) as r:
                body = r.read()
                link = r.headers.get('Link') or ''
            m = re.search(r'[?&]page=(\d+)>;\s*rel="last"', link)
            return int(m.group(1)) if m else len(json.loads(body.decode('utf-8')) or [])
        except Exception as e:
            log('  查询远端提交数失败：%s' % str(e)[:100])
        finally:
            socket.getaddrinfo = orig
    return None


def main():
    rc, branch, _ = git('rev-parse', '--abbrev-ref', 'HEAD')
    if rc != 0:
        log('不是 git 仓库，中止')
        return 2
    rc, total, _ = git('rev-list', '--count', 'HEAD')
    rc, head, _ = git('rev-parse', 'HEAD')
    total = int(total or 0)
    log('当前分支 %s，提交 %d 条，HEAD %s' % (branch, total, head[:8]))

    if total <= 1 and not RESUME:
        log('提交数已经是 %d 条，无需压缩。' % total)
        return 0

    cfg = json.load(open(os.path.join(BASE, 'config'), encoding='utf-8'))
    status_n = len(json.load(open(os.path.join(BASE, 'status.json'), encoding='utf-8')))
    msg = ('影视仓聚合配置基线（%s）\n\n'
           '本次把 %d 条提交折叠为单一基线，文件内容不变。\n'
           '当前快照：config %d 个源 / status %d 条。\n'
           '后续每日自动更新仍会追加新提交；累计过多时重跑 squash_history.py 可再次压缩。'
           % (datetime.date.today().isoformat(), total,
              len(cfg.get('urls') or []), status_n))

    if DRY:
        log('')
        log('[dry-run] 将执行：')
        log('  1) 备份 .git')
        log('  2) 用 --orphan 建新根提交（内容 = 当前工作区跟踪文件）')
        log('  3) 删除旧 %s 分支，新分支改名 master' % branch)
        log('  4) reflog expire + gc 清掉不可达对象')
        log('  5) 强推 Gitee')
        log('  6) GitHub：blobs → tree → 无父提交 → 强制移动 master 引用')
        log('')
        log('新提交说明：')
        for line in msg.splitlines():
            log('  | ' + line)
        return 0

    if not RESUME:
        step('1/6 备份 .git')
        backup_git()

        step('2/6 提交未保存的改动（保证工作区干净）')
        git('add', '-A')
        rc, out, err = git('commit', '-m', 'pre-squash %s' % datetime.date.today().isoformat())
        if rc != 0 and 'nothing to commit' not in (out + err):
            log('  提交失败：%s' % (err or out)[:300])
            return 3
        log('  工作区已就绪')

        step('3/6 建立新的根提交')
        rc, _o, err = git('checkout', '--orphan', TMP_BRANCH)
        if rc != 0:
            log('  切换 orphan 分支失败：%s' % err[:300])
            return 4
        git('rm', '-r', '--cached', '.', '-q')     # 只清索引，工作区文件原样保留
        git('add', '-A')
        rc, out, err = git('commit', '-m', msg)
        if rc != 0:
            log('  新根提交失败：%s' % (err or out)[:300])
            return 5
        rc, new_sha, _ = git('rev-parse', 'HEAD')
        log('  新根提交 %s' % new_sha[:8])
    else:
        log('（--resume：跳过备份与根提交，直接从分支收尾开始）')

    step('4/6 切换分支名并清理旧对象')
    rc, cur, _ = git('rev-parse', '--abbrev-ref', 'HEAD')
    log('  当前分支 %s' % cur)
    if cur != 'master':
        # 本机历史上同时存在 main（本机提交线）和 master（CI/远端那条线），
        # 直接改名会撞名失败。旧 master 指向的正是要丢弃的旧历史，先删掉再强制改名。
        rc, _o, _e = git('branch', '-D', 'master')
        if rc == 0:
            log('  已删除旧的同名 master 分支（其历史本就要丢弃，.git 已备份）')
        rc, _o, err = git('branch', '-M', 'master')
        if rc != 0:
            log('  改名 master 失败：%s' % err[:300])
            return 6
    rc, _o, _e = git('branch', '-D', TMP_BRANCH)
    log('  当前分支已定为 master')
    git('reflog', 'expire', '--expire=now', '--all')
    git('gc', '--prune=now', '--quiet', timeout=300)
    rc, after, _ = git('rev-list', '--count', 'HEAD')
    size = sum(os.path.getsize(os.path.join(dp, f))
               for dp, _dn, fns in os.walk(os.path.join(BASE, '.git')) for f in fns)
    log('  本地现在 %s 条提交，.git %.1f MB' % (after, size / 1024.0 / 1024.0))

    step('5/6 强推 Gitee')
    rc, out, err = git('push', 'gitee', '+HEAD:master', timeout=180)
    if rc != 0:
        log('  Gitee 强推失败，中止（GitHub 保持原状）：%s' % (err or out)[:400])
        log('  可执行 `git push gitee +HEAD:master` 手动重试，或用备份的 .git 还原')
        return 7
    log('  Gitee 已更新')

    step('6/6 重写 GitHub 历史')
    rc, out, _ = git('ls-tree', '-r', '--name-only', 'HEAD')
    rels = [x for x in out.splitlines() if x.strip()]
    files = [(r, os.path.join(BASE, r.replace('/', os.sep))) for r in rels
             if os.path.exists(os.path.join(BASE, r.replace('/', os.sep)))]
    log('  待上传 %d 个文件' % len(files))
    try:
        tree_sha, n = gh_blobs_and_tree(files)
        log('  tree %s（%d 项）' % (tree_sha[:8], n))
        commit = U._gh_api('POST', '/repos/%s/%s/git/commits' % (U.GH_OWNER, U.GH_REPO),
                           {'message': msg, 'tree': tree_sha, 'parents': []})
        if not commit or not commit.get('sha'):
            raise RuntimeError('根提交创建失败（GitHub 可能不允许无父提交）')
        log('  根提交 %s' % commit['sha'][:8])
        moved = U._gh_api('PATCH', '/repos/%s/%s/git/refs/heads/master' % (U.GH_OWNER, U.GH_REPO),
                          {'sha': commit['sha'], 'force': True})
        if not moved or not moved.get('object'):
            raise RuntimeError('强制移动 master 引用失败（可能开了分支保护）')
        log('  GitHub master 已指向新根提交')
    except Exception as e:
        log('  GitHub 侧失败：%s' % e)
        log('  → Gitee 已是单条历史，GitHub 仍是旧历史；两边内容一致，不影响播放。')
        log('  → 需要时可单独重跑：python squash_history.py（本地已压缩，不会再变）')
        return 8

    step('验证')
    rc, n_local, _ = git('rev-list', '--count', 'HEAD')
    n_remote = count_remote_commits()
    log('  本地提交数：%s' % n_local)
    log('  GitHub 提交数：%s' % n_remote)
    log('  Gitee 提交数：需在网页确认（强推成功后即为 1）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
