# -*- coding: utf-8 -*-
"""把 Git 提交历史压缩成单条根提交，并同步强推 Gitee + GitHub。

背景（2026-10-20）：
    自动更新每天产生的提交比设想的多：update.yml 一轮就调两次 push_remotes()（各 1 条），
    refresh_config.yml 再 1 条，本机当天还会再补 1—2 条。2026-11-04 实测一天 4 条，
    一年约 1400 条，不是当初估的 730 条。
    提交历史本身不影响电视端（它只读最新 config），但仓库页面会被刷屏，
    而且仓库体积只增不减。这个脚本把历史合并成一个基线提交，
    内容一字不改，只把"过程记录"折掉。

安全设计：
    - 执行前把 .git 完整复制到 ../.git-bak-<时间戳>，随时可以整体还原；
    - 先推 Gitee（国内必达），再推 GitHub；Gitee 失败就中止，不动 GitHub；
    - GitHub 侧建树前先读远端文件清单，本机没有副本的文件按原 blob SHA 保留——
      2026-11-04 发现远端清单取不到时直接中止，不再盲建一棵树；
    - 压缩后逐路径比对远端文件集，少一个就返回失败并提示还原；
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


def gh_remote_blobs():
    """取 GitHub master 当前的文件清单 {path: {sha, mode}}。

    必须取：本机工作区未必有远端全部文件。2026-11-04 发现
    refresh_config.yml / refresh_config.py / source_pool.json 只存在于 GitHub
    （由仓库内的工作流自己提交上去的），本机一份都没有。以前建树只用本机清单，
    压缩历史会顺手把它们删掉——等于每天 01:00 那个源配置同步工作流永久消失。
    """
    ref = U._gh_api('GET', '/repos/%s/%s/git/ref/heads/%s'
                    % (U.GH_OWNER, U.GH_REPO, U.GH_BRANCH))
    if not ref or not (ref.get('object') or {}).get('sha'):
        return {}
    sha = ref['object']['sha']
    commit = U._gh_api('GET', '/repos/%s/%s/git/commits/%s' % (U.GH_OWNER, U.GH_REPO, sha))
    tree_sha = (commit or {}).get('tree', {}).get('sha')
    if not tree_sha:
        return {}
    tree = U._gh_api('GET', '/repos/%s/%s/git/trees/%s?recursive=1'
                     % (U.GH_OWNER, U.GH_REPO, tree_sha))
    if not tree:
        return {}
    if tree.get('truncated'):
        # 树被截断时清单不全，照它合并会漏文件；宁可不要保护，也不要基于残缺清单动手。
        log('  ⚠️ 远端文件清单被截断，无法保证完整，跳过远端独有文件保护')
        return {}
    return {t['path']: {'sha': t['sha'], 'mode': t.get('mode', '100644')}
            for t in (tree.get('tree') or []) if t.get('type') == 'blob'}


def gh_blobs_and_tree(files, remote=None):
    """把本地文件逐份写成 GitHub blob，再组装成一棵完整的树。

    本地没有、但远端已有的文件按原 blob SHA 直接挂进新树（blob 对象本来就还在
    仓库里，不需要重新上传），保证"压缩历史"只折提交、不删文件。
    """
    if remote is None:
        remote = gh_remote_blobs()
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
    local_paths = {e['path'] for e in entries}
    kept = sorted(p for p in remote if p not in local_paths)
    for p in kept:
        entries.append({'path': p, 'mode': remote[p]['mode'],
                        'type': 'blob', 'sha': remote[p]['sha']})
    if kept:
        log('  保留远端独有文件 %d 个（本机没有副本）：%s' % (len(kept), '、'.join(kept)))
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
        log('  6) GitHub：取远端文件清单 → blobs → tree（含远端独有文件）→ 无父提交 → 强制移动 master 引用')
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
    remote_all = gh_remote_blobs()
    remote_before = set(remote_all)
    if not remote_all:
        # 拿不到远端清单就没法保证不删东西。以前是照本机文件列表重建整棵树，
        # 结果 refresh_config.yml / refresh_config.py / source_pool.json
        # 这三个只存在于 GitHub 的文件被一并删掉，那条源配置同步工作流当场就没了。
        # 现在宁可不改 GitHub，也不能盲建一棵树。
        log('  ⚠️ 取不到 GitHub 现有文件清单，跳过 GitHub 重写（Gitee 已压缩，不影响播放）')
        log('  → 可单独重跑：python squash_history.py --resume')
        return 9
    rc, out, _ = git('ls-tree', '-r', '--name-only', 'HEAD')
    rels = [x for x in out.splitlines() if x.strip()]
    files = [(r, os.path.join(BASE, r.replace('/', os.sep))) for r in rels
             if os.path.exists(os.path.join(BASE, r.replace('/', os.sep)))]
    missing = [r for r in rels
               if not os.path.exists(os.path.join(BASE, r.replace('/', os.sep)))]
    if missing:
        # 索引里有、工作区没有 = 文件被删了却没提交。这类路径不会进新树，
        # 必须显式告诉用户，否则"压缩历史"会顺手把远端的文件删掉。
        log('  ⚠️ 本地索引有 %d 个文件在工作区不存在，将不会出现在新历史中：%s'
            % (len(missing), '、'.join(missing)))
    log('  待上传 %d 个文件' % len(files))
    try:
        tree_sha, n = gh_blobs_and_tree(files, remote=remote_all)
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
    # 提交数只是过程指标，真正不能出问题的是文件集：压缩前后 GitHub 上的路径必须一模一样。
    # 少了任何一个就说明合并逻辑漏了东西（历史上就是少了 refresh_config.yml 那三个文件）。
    after = set(gh_remote_blobs())
    lost = sorted(remote_before - after)
    if lost:
        log('  ❌ GitHub 压缩后文件丢失 %d 个：%s' % (len(lost), '、'.join(lost)))
        log('     → 用备份的 .git 还原，或从 GitHub 旧提交里把文件取回来')
        return 10
    if not after:
        log('  ⚠️ 校验时取不到 GitHub 文件清单，请手动确认远端文件齐全')
        return 11
    log('  GitHub 文件集校验：压缩前 %d 个 → 压缩后 %d 个，无丢失'
        % (len(remote_before), len(after)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
