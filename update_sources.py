# -*- coding: utf-8 -*-
"""
影视仓聚合接口 - 每日自动查找与验证影视源
每天 12:00（北京时间）自动运行，触发方式有两种：
  - Windows 计划任务（本机）
  - GitHub Actions（.github/workflows/update.yml，UTC 04:00）
流程：
  1. 验证 config 中每个源的可用性（HTTP 200 + 合法 TVBox JSON 结构）
  2. 连续 3 天失效的源自动剔除
  3. 从候选源池自动验证可用新源并补充进 config
  4. 更新 status.json 记录每个源的检测状态
  5. 同步 config / config_kids / status.json / live_list.m3u 到远端
"""
import json
import os
import re
import subprocess
import ssl
import sys
import time
import urllib.request
import concurrent.futures
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config')
STATUS_PATH = os.path.join(BASE_DIR, 'status.json')
LOG_PATH = os.path.join(BASE_DIR, 'update_log.txt')

MAX_FAIL_DAYS = 3            # 连续失败天数阈值，达到后自动剔除
MAX_SOURCES = 15             # config 中最多保留的源数量
DOWNLOAD_LIMIT = 512 * 1024  # 单源下载上限 512KB
# 直播列表单独放宽上限：m3u 常年在几百 KB 到数 MB，沿用 512KB 会导致保鲜静默失败
LIVE_DOWNLOAD_LIMIT = 8 * 1024 * 1024

# 直播源直链：电视端"直播"页直接添加这个地址即可，与 config 线路完全解耦。
# 2026-10-17 起不再把直播塞进 config 的 urls（线路要求 sites 非空，纯直播会判解析失败）。
# 精简版只剔各省地方台（数量占一半、可用率最低、加载最慢），央视/卫视/港澳台/境外全留。
LIVE_LIGHT_PATH = 'live_light.m3u'
LIVE_LIGHT_SKIP_PREFIXES = ('☘️',)   # 各省频道分组前缀
LIVE_LIGHT_SKIP_GROUPS = ('地方频道', '数字频道')

# 直播源直链的镜像模板（%s 处填 live_list.m3u / live_light.m3u）
# 顺序即优先级：ghfast 直读 GitHub raw，永远最新；jsDelivr 系有独立缓存，只能当备用。
LIVE_FEED_MIRRORS = [
    ('ghfast', 'https://ghfast.top/https://raw.githubusercontent.com/tan-jianlin/ysc/master/%s'),
    ('jsdmirror', 'https://cdn.jsdmirror.com/gh/tan-jianlin/ysc@master/%s'),
    ('onmicrosoft', 'https://jsd.onmicrosoft.cn/gh/tan-jianlin/ysc@master/%s'),
    ('gh-proxy', 'https://gh-proxy.com/https://raw.githubusercontent.com/tan-jianlin/ysc/master/%s'),
]

# 电视实测不可用黑名单（家庭网络无法访问/影视仓解析失败），即使电脑验证通过也不加入 config
BLOCKLIST = [
    "https://jihulab.com/mengzhu2/ysc/-/raw/main/YSC.json",
    "https://jihulab.com/ygbh1/box/-/raw/main/dcang/dc.json",
    "http://ztha.top/TVBox/GYCK.json",
    "http://home.jundie.top:81/top98.json",
    "http://xmbjm.fh4u.org/dc.txt",
    "http://xhztv.top/dc",
    "https://www.yingm.cc/dm/dm.json",                      # 动漫专属：电视打不开
    "http://影视仓.com/",                                    # 中文域名：电视不支持
    "http://tvbox.王二小放牛娃.top",                          # 中文域名：电视不支持
    "https://gh-proxy.com/https://raw.githubusercontent.com/qist/tvbox/master/jsm.json",
    "https://gh-proxy.com/https://raw.githubusercontent.com/qist/tvbox/master/fty.json",
    "https://gh-proxy.com/https://raw.githubusercontent.com/qist/tvbox/master/xiaosa/api.json",
    "https://gh-proxy.com/https://raw.githubusercontent.com/gaotianliuyun/gao/master/js.json",
    "https://gh-proxy.com/https://raw.githubusercontent.com/xyq254245/xyqonlinerule/main/XYQTVBox.json",
]

# 站点级抽查：验证通过后抽查源内部前 N 个站点，防止"文件在但内容全失效"
SITE_CHECK_N = 3

# 额外并入直播列表的国外频道源：(分组标签, m3u 地址, 最多并入条数, 分组名)
# 用 iptv-org 的公开列表，github.io 在国内可直连；每个源限量，避免列表膨胀到几千条拖慢电视
# 港台单独补源，并并入主源已有的"🌊港·澳·台"分组，不再单开两个分组
EXTRA_LIVE_SOURCES = [
    ("美国", "https://iptv-org.github.io/iptv/countries/us.m3u", 150, "🌍美国"),
    ("英国", "https://iptv-org.github.io/iptv/countries/uk.m3u", 120, "🌍英国"),
    ("德国", "https://iptv-org.github.io/iptv/countries/de.m3u", 80, "🌍德国"),
    ("韩国", "https://iptv-org.github.io/iptv/countries/kr.m3u", 60, "🌍韩国"),
    ("日本", "https://iptv-org.github.io/iptv/countries/jp.m3u", 40, "🌍日本"),
    ("新加坡", "https://iptv-org.github.io/iptv/countries/sg.m3u", 40, "🌍新加坡"),
    ("香港", "https://iptv-org.github.io/iptv/countries/hk.m3u", 60, "🌊港·澳·台"),
    ("台湾", "https://iptv-org.github.io/iptv/countries/tw.m3u", 60, "🌊港·澳·台"),
]

# 直播分组排序：境外频道紧跟央视/卫视，不再排在 53 个分组的最后面
# （2026-10-17 用户反馈"根本看不到外国台"，实际是分组排到最末尾，翻不到）
GROUP_ORDER = ['🕘️更新时间', '📺央视频道', '央视频道', '📡卫视频道', '卫视频道',
               '🌊港·澳·台', '🌍美国', '🌍英国', '🌍日本', '🌍韩国', '🌍德国', '🌍新加坡']

# 需要做"可达性体检"的境外分组（geo-block 死链最多，占了分组却放不出来）
OVERSEAS_GROUPS = ('🌊港·澳·台', '🌍美国', '🌍英国', '🌍日本', '🌍韩国', '🌍德国', '🌍新加坡')

# 合并时跳过的域名：实测在国内网络下恒定返回 4xx，占名额但放不出来
# （jmp2.uk 是 Pluto TV 的区域跳转，缺地区参数一律 400）
EXTRA_LIVE_SKIP_HOSTS = ('jmp2.uk',)

# 补充的国内频道源：(标签, m3u 地址, 最多并入条数)
# 主源 Guovin@gd 已于 2026-08-27 停止更新，这份 vbskycn 的 IPv4 列表仍在维护，
# 用来给主源兜底——即使主源冻结，列表每天仍能被刷新补充。
EXTRA_CHINA_SOURCES = [
    ("vbskycn", "https://cdn.jsdelivr.net/gh/vbskycn/iptv@master/tv/iptv4.m3u", 200),
]

# 候选源池：每次运行都会重新验证，可用且不在 config 的会自动补充（仅保留电视实测可访问的源）
CANDIDATES = [
    ("小马源", "https://szyyds.cn/tv/x.json"),
    ("HGYX影视", "https://api.hgyx.vip/hgyx.json"),
    ("聚玩盒子4K", "http://xhztv.top/4k.json"),
    ("王二小备用", "https://9280.kstore.space/wex.json"),
    ("青龙", "https://6758.kstore.space/1.json"),
]


def log(msg):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + "\n")
    except Exception:
        pass


def idna_url(url):
    """将中文域名转为 punycode，urllib 才能请求"""
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(url)
        if p.hostname and any(ord(c) > 127 for c in p.hostname):
            host = p.hostname.encode('idna').decode('ascii')
            netloc = host if p.port is None else '%s:%d' % (host, p.port)
            return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))
    except Exception:
        pass
    return url


def fetch(url, limit=DOWNLOAD_LIMIT):
    """下载 URL 内容，返回文本；失败返回 None（兼容 gzip、中文域名）"""
    try:
        url = idna_url(url)
        req = urllib.request.Request(url, headers={
            'User-Agent': 'okhttp/3.12.1',
            'Accept-Encoding': 'identity',
        })
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = resp.read(limit + 1)
            if len(body) > limit:
                log("  内容超过上限 %d 字节，跳过 %s" % (limit, url))
                return None
            enc = resp.headers.get('Content-Encoding', '')
            if enc.lower() == 'gzip':
                import gzip
                import io
                try:
                    body = gzip.GzipFile(fileobj=io.BytesIO(body)).read()
                except Exception:
                    pass
            return body.decode('utf-8', 'ignore')
    except Exception as e:
        log("  下载失败 %s : %s" % (url, e))
        return None


def validate(content):
    """校验是否为合法 TVBox 配置（兼容 BOM、非严格 JSON、尾部附加内容）"""
    if content is None:
        return False, "下载失败"
    text = content.lstrip('\ufeff').strip()
    if not (text.startswith('{') or text.startswith('[')):
        return False, "非JSON"
    j = None
    try:
        j = json.loads(text)
    except Exception:
        try:
            # 兼容 JSON 后附加内容（如服务器拼接的统计 HTML）
            j, _ = json.JSONDecoder().raw_decode(text)
        except Exception:
            j = None
    if j is None:
        # 兼容非严格 JSON（TVBox 源偶有注释/宽松语法，影视仓可解析）
        if '"sites"' in text and ('"api"' in text or '"parses"' in text):
            return True, "单仓OK(宽松)"
        if '"urls"' in text and '"url"' in text:
            return True, "多仓OK(宽松)"
        return False, "JSON解析失败"
    if isinstance(j, dict):
        if 'sites' in j:
            return True, "单仓OK"
        if 'urls' in j:
            return True, "多仓OK"
        if 'lives' in j:
            return True, "直播OK"
    return False, "不是TVBox配置结构"


def site_spot_check(content):
    """站点级抽查：取配置内前 N 个站点 API，能收到任何 HTTP 响应即视为可达（仅提醒，不参与剔除）"""
    if not content:
        return 0, 0
    try:
        text = content.lstrip('\ufeff').strip()
        j = json.loads(text)
    except Exception:
        return 0, 0
    if not isinstance(j, dict):
        return 0, 0
    sites = j.get("sites")
    if not isinstance(sites, list) or len(sites) == 0:
        return 0, 0
    urls = []
    for s in sites[:SITE_CHECK_N]:
        if isinstance(s, dict):
            u = s.get("api") or s.get("url")
            if u and isinstance(u, str) and u.startswith(("http://", "https://")):
                urls.append(u)
    if not urls:
        return 0, 0
    ok = 0
    for u in urls:
        try:
            u2 = idna_url(u)
            req = urllib.request.Request(u2, headers={'User-Agent': 'okhttp/3.12.1', 'Accept-Encoding': 'identity'})
            with urllib.request.urlopen(req, timeout=6) as resp:
                ok += 1  # 任何 HTTP 响应都算站点可达
        except urllib.error.HTTPError:
            ok += 1  # 4xx/5xx 也算可达（源站有响应，多为防盗链）
        except Exception:
            pass  # 网络级失败（超时/拒绝/DNS）才算不可达
    return ok, len(urls)


def notify_change(msg):
    """Windows 桌面通知（源有新增/剔除/大面积失效时提醒用户）

    仅 Windows 有效；在 GitHub Actions 的 ubuntu-latest 上会失败，
    属预期行为，由 try/except 兜住，不影响主流程。
    """
    try:
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;"
            "$n.Visible=$true;"
            "$n.BalloonTipTitle='影视源自动更新';"
            "$n.BalloonTipText='%s';"
            "$n.ShowBalloonTip(10000);"
            "Start-Sleep 12;$n.Dispose()" % msg.replace("'", "''")
        )
        subprocess.run(['powershell', '-NoProfile', '-Command', ps], timeout=20)
        log("已发送桌面通知")
    except Exception as e:
        log("桌面通知发送失败（非 Windows 环境属正常）: %s" % e)


def load_status():
    if os.path.exists(STATUS_PATH):
        try:
            with open(STATUS_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_status(status):
    with open(STATUS_PATH, 'w', encoding='utf-8') as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {"storeName": "本地聚合", "urls": []}
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        log("读取 config 失败，重建: %s" % e)
        return {"storeName": "本地聚合", "urls": []}


def push_remotes():
    """把 config / 直播列表同步到远端，让电视不依赖电脑也能读到最新配置。

    2026-10-17 起同时推 origin(GitHub) 与 gitee，两个远端独立成败：
      - gitee 承载 config（点播线路），国内必达；
      - origin 承载直播 m3u（Gitee 对含 #EXTM3U 的内容一律 451，只能放 GitHub）。
    分开推的原因：当天本机到 GitHub 的 TLS 链路中断时，若只推 origin，Gitee 上的
    config 也会跟着停更，电视点播一起挂；而每日任务会因此天天弹窗，实际线路是好的。
    只有两个远端都失败才算故障。

    GIT_PUSH_REMOTE 显式指定时只推该远端（GitHub Actions 里用它推自己）。
    返回 {remote: bool}。
    """
    only = os.environ.get('GIT_PUSH_REMOTE')
    targets = [only] if only else ['origin', 'gitee']
    result = {t: False for t in targets}
    try:
        # live_light.m3u 等文件可能尚未生成，排除不存在的路径，避免 git add 整体失败
        addable = [p for p in ('config', 'config_kids', 'status.json', 'live_list.m3u',
                               'live_light.m3u', 'live_source.json', 'live_full.json')
                   if os.path.exists(os.path.join(BASE_DIR, p))]
        steps = [
            ['git', '-C', BASE_DIR, 'add'] + addable,
            ['git', '-C', BASE_DIR, 'commit', '-m', 'auto update %s' % datetime.date.today().isoformat()],
        ]
        for i, c in enumerate(steps):
            r = subprocess.run(c, capture_output=True, text=True, timeout=90)
            log("  git %s -> 退出码 %d" % (' '.join(c[3:]), r.returncode))
            if r.returncode == 0:
                continue
            if i == 0:
                log("  暂存失败：%s" % (r.stderr or '').strip()[:300])
                return result
            if 'nothing to commit' in (r.stdout or '') or 'nothing added' in (r.stdout or ''):
                log("  无改动可提交（正常）")
            else:
                log("  提交失败：%s" % (r.stderr or '').strip()[:300])
                return result
        # 用 HEAD:master 而不是 master：后者推的是本地 master 分支，
        # 一旦本地检出在 main，就会推上去旧内容却返回成功（静默假成功）。
        for remote in targets:
            r = subprocess.run(['git', '-C', BASE_DIR, 'push', remote, 'HEAD:master'],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                result[remote] = True
                log("已同步到远端 %s" % remote)
            else:
                log("  推送 %s 失败：%s" % (remote, (r.stderr or '').strip()[:300]))
        return result
    except Exception as e:
        log("推送远端失败（网络或登录态问题，本地配置已更新）: %s" % e)
        return result


def validate_kids():
    """儿童聚合验证：儿童聚合里的源若连续失效则自动剔除（保持可用性）"""
    path = os.path.join(BASE_DIR, 'config_kids')
    if not os.path.exists(path):
        return
    try:
        with open(path, 'r', encoding='utf-8') as f:
            kids = json.load(f)
    except Exception:
        return
    status = load_status()
    urls = kids.get("urls", [])
    if not isinstance(urls, list) or len(urls) == 0:
        return
    new_urls = []
    for it in urls:
        if not isinstance(it, dict) or not it.get("url"):
            continue
        rec = status.get(it["url"], {})
        if rec.get("status") == "ok":
            new_urls.append(it)
        elif rec.get("fail_days", 0) < MAX_FAIL_DAYS:
            new_urls.append(it)  # 曾可用未达阈值，保留
        else:
            log("儿童聚合剔除失效源 %s (%s)" % (it.get("name", "未命名"), it["url"]))
    if len(new_urls) != len(urls):
        kids["urls"] = new_urls
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(kids, f, ensure_ascii=False, indent=2)
        log("config_kids 已更新：%d -> %d 个源" % (len(urls), len(new_urls)))


def fetch_retry(url, limit=DOWNLOAD_LIMIT, attempts=3, wait=2):
    """带重试的下载。额外频道源只是补充，不该因为一次抖动就整批丢掉。"""
    for i in range(attempts):
        content = fetch(url, limit=limit)
        if content:
            return content
        if i < attempts - 1:
            log("  重试 %s (%d/%d)" % (url, i + 1, attempts))
            time.sleep(wait)
    return None


def parse_m3u_blocks(text):
    """把 m3u 拆成 [(EXTINF行, 中间指令行, URL), ...]

    中间的 #EXTVLCOPT / #KODIPROP / #EXTGRP 等指令必须原样保留——
    有些源要靠它们携带 Referer / User-Agent，丢了就播不出来。
    """
    blocks = []
    extinf = None
    middle = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#EXTINF'):
            extinf = line
            middle = []
        elif line.startswith('#') and extinf is not None:
            middle.append(line)
        elif line.startswith(('http://', 'https://')) and extinf is not None:
            blocks.append((extinf, middle, line))
            extinf = None
            middle = []
    return blocks


def set_group(extinf, group):
    """替换 EXTINF 里的 group-title，没有就在频道名前插入"""
    if 'group-title="' in extinf:
        return re.sub(r'group-title="[^"]*"', 'group-title="%s"' % group, extinf, count=1)
    idx = extinf.rfind(',')
    if idx < 0:
        return extinf
    return extinf[:idx] + ' group-title="%s"' % group + extinf[idx:]


def merge_extra_live(main_text):
    """把国外频道源并入主直播列表，按 URL 去重，并给它们单独分组"""
    blocks = parse_m3u_blocks(main_text)
    seen = set(u for _, _, u in blocks)
    header = main_text.splitlines()[0].strip() if main_text.strip() else '#EXTM3U'
    out = [header]
    for extinf, middle, url in blocks:
        out.append(extinf)
        out.extend(middle)
        out.append(url)
    merged_count = 0
    for label, url, cap, group in EXTRA_LIVE_SOURCES:
        text = fetch_retry(url, limit=LIVE_DOWNLOAD_LIMIT)
        if not text:
            log("额外频道源获取失败，跳过：%s" % label)
            continue
        added = 0
        for extinf, middle, u in parse_m3u_blocks(text):
            if added >= cap:
                break
            if u in seen:
                continue
            idx = extinf.rfind(',')
            name = extinf[idx + 1:] if idx > 0 else ''
            # [Geo-blocked] 表示对外地区封锁，国内基本放不出来，直接跳过
            if 'Geo-blocked' in name:
                continue
            host = u.split('//', 1)[-1].split('/', 1)[0].split(':')[0].lower()
            if any(host == h or host.endswith('.' + h) for h in EXTRA_LIVE_SKIP_HOSTS):
                continue
            seen.add(u)
            out.append(set_group(extinf, group))
            out.extend(middle)
            out.append(u)
            added += 1
        merged_count += added
        log("合并国外频道 %s：+%d 条" % (label, added))
    # 国内补充源：保留它自己的分组名（央视频道/卫视频道等），和主源自然混在一起
    for label, url, cap in EXTRA_CHINA_SOURCES:
        text = fetch_retry(url, limit=LIVE_DOWNLOAD_LIMIT)
        if not text:
            log("补充国内源获取失败，跳过：%s" % label)
            continue
        added = 0
        for extinf, middle, u in parse_m3u_blocks(text):
            if added >= cap:
                break
            if u in seen:
                continue
            idx = extinf.rfind(',')
            name = extinf[idx + 1:] if idx > 0 else ''
            if 'Geo-blocked' in name:
                continue
            host = u.split('//', 1)[-1].split('/', 1)[0].split(':')[0].lower()
            if any(host == h or host.endswith('.' + h) for h in EXTRA_LIVE_SKIP_HOSTS):
                continue
            seen.add(u)
            out.append(extinf)
            out.extend(middle)
            out.append(u)
            added += 1
        merged_count += added
        log("合并补充国内源 %s：+%d 条" % (label, added))
    log("直播列表合并完成：本地 %d 条 + 额外 %d 条 = %d 条"
        % (len(blocks), merged_count, len(blocks) + merged_count))
    return '\n'.join(out) + '\n'


def reorder_groups(text):
    """按 GROUP_ORDER 重排分组：境外频道紧跟央视/卫视，其余保持原顺序

    2026-10-17：用户反馈"根本看不到外国台"，实际是合并时把境外分组追加到了
    53 个分组的最后面，电视上要一路往右翻才找得到，等于白放。
    """
    blocks = parse_m3u_blocks(text)
    if not blocks:
        return text
    header = text.splitlines()[0].strip() if text.strip() else '#EXTM3U'

    def group_of(extinf):
        m = re.search(r'group-title="([^"]*)"', extinf)
        return m.group(1) if m else ''

    def rank(block):
        g = group_of(block[0])
        return GROUP_ORDER.index(g) if g in GROUP_ORDER else len(GROUP_ORDER)

    out = [header]
    for extinf, middle, url in sorted(blocks, key=rank):  # sorted 稳定：同组内保持原顺序
        out.append(extinf)
        out.extend(middle)
        out.append(url)
    return '\n'.join(out) + '\n'


def _probe_stream(url, timeout=4):
    """单条直播流可达性探测：能拿到 200 且读得到字节就算活"""
    try:
        u = idna_url(url)
        req = urllib.request.Request(u, headers={
            'User-Agent': 'okhttp/3.12.1',
            'Accept-Encoding': 'identity',
        })
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status == 200 and len(resp.read(256)) > 0
    except Exception:
        return False


def prune_dead_overseas(text, timeout=4, workers=16):
    """境外频道体检：geo-block / 死链先删掉，别占着分组让电视逐个点都放不出来

    只动境外分组，国内频道一条不碰；若整体可用率低于 30%，判定为本机网络异常，
    保留原样不动（宁可留着让电视自己试，也不能因为一次断网把好频道全删了）。
    """
    blocks = parse_m3u_blocks(text)
    if not blocks:
        return text
    header = text.splitlines()[0].strip() if text.strip() else '#EXTM3U'
    targets = []
    for idx, (extinf, _middle, url) in enumerate(blocks):
        m = re.search(r'group-title="([^"]*)"', extinf)
        if m and m.group(1) in OVERSEAS_GROUPS:
            targets.append((idx, url))
    if not targets:
        return text
    alive = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for (idx, url), ok in zip(targets, ex.map(lambda t: _probe_stream(t[1], timeout), targets)):
            alive[idx] = ok
    live_cnt = sum(1 for v in alive.values() if v)
    ratio = live_cnt / float(len(targets))
    if ratio < 0.3:
        log("境外频道体检可用率仅 %.0f%%，疑似本机网络异常，本次不剔除" % (ratio * 100))
        return text
    out = [header]
    for idx, (extinf, middle, url) in enumerate(blocks):
        if idx in alive and not alive[idx]:
            continue
        out.append(extinf)
        out.extend(middle)
        out.append(url)
    log("境外频道体检：%d 条可用 / 共 %d 条，剔除死链 %d 条"
        % (live_cnt, len(targets), len(targets) - live_cnt))
    return '\n'.join(out) + '\n'


def refresh_live():
    """直播保鲜：拉取主列表 + 并入国外频道，更新本地列表后随下次 push 同步线上"""
    live_path = os.path.join(BASE_DIR, 'live_list.m3u')
    src = "https://cdn.jsdelivr.net/gh/Guovin/iptv-api@gd/output/result.m3u"
    content = fetch(src, limit=LIVE_DOWNLOAD_LIMIT)
    if not (content and content.lstrip().startswith('#EXTM3U') and len(content) > 5000):
        log("直播列表更新失败，保留现有列表")
        return False
    merged = merge_extra_live(content)
    merged = prune_dead_overseas(merged)
    merged = reorder_groups(merged)
    old = None
    if os.path.exists(live_path):
        try:
            with open(live_path, 'r', encoding='utf-8', errors='ignore') as f:
                old = f.read()
        except Exception:
            old = None
    # 上游若长期不更新，这里每天都会"成功"生成同一份内容——必须显式指出，
    # 否则会像 2026-08 ~ 2026-10 那样静默空转两个月没人发现。
    if old is not None and merged.strip() == old.strip():
        log("⚠️ 直播列表内容与上次完全相同（主源可能已停更）")
    with open(live_path, 'w', encoding='utf-8') as f:
        f.write(merged)
    log("直播列表已更新 (%d 字节)" % len(merged.encode('utf-8')))

    # 同步产出精简版：电视端"直播"页直连用，加载更快（约少一半条目）
    try:
        light = build_live_light(merged)
        light_path = os.path.join(BASE_DIR, LIVE_LIGHT_PATH)
        with open(light_path, 'w', encoding='utf-8') as f:
            f.write(light)
        log("精简直播列表已更新 (%d 字节)" % len(light.encode('utf-8')))
    except Exception as e:
        log("⚠️ 精简直播列表生成失败：%s" % e)
    return True


def build_live_light(text):
    """剔除各省地方台，只留央视/卫视/港澳台/境外/主题频道，给电视端直连用"""
    blocks = parse_m3u_blocks(text)
    if not blocks:
        return text
    header = text.splitlines()[0].strip() if text.strip() else '#EXTM3U'
    out = [header]
    skipped = 0
    for extinf, middle, url in blocks:
        m = re.search(r'group-title="([^"]*)"', extinf)
        group = m.group(1) if m else ''
        if group.startswith(LIVE_LIGHT_SKIP_PREFIXES) or group in LIVE_LIGHT_SKIP_GROUPS:
            skipped += 1
            continue
        out.append(extinf)
        out.extend(middle)
        out.append(url)
    log("精简直播列表：保留 %d 条，略过地方台 %d 条" % (len(blocks) - skipped, skipped))
    return '\n'.join(out) + '\n'


def check_live_repo():
    """仓库级自检：电视端"点开电视直播就提示解析失败"，说明 config 里那条 url 本身取不到。

    2026-10-17 就踩过：直播仓库被指到 gh-proxy / jsdelivr，电脑能下、电视取不到，
    于是两个直播仓库双双"解析失败"，而脚本每天照样报"直播OK"。
    这里直接校验 config 中的直播仓库地址（能拿到合法 JSON 且含 lives）。
    """
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception as e:
        log("自检：读取 config 失败：%s" % e)
        return True
    repo_urls = [it.get('url') for it in (cfg.get('urls') or [])
                 if isinstance(it, dict) and
                 ('live_source' in (it.get('url') or '') or 'live_full' in (it.get('url') or ''))]
    if not repo_urls:
        log("自检：config 里没有直播仓库条目")
        return True
    ok_repo = 0
    for u in repo_urls:
        ok, note = validate(fetch(u, limit=LIVE_DOWNLOAD_LIMIT))
        if ok:
            ok_repo += 1
            log("  直播仓库可用：%s" % u)
        else:
            log("  ⚠️ 直播仓库不可用：%s (%s)" % (u, note))
    if ok_repo == 0:
        notify_change("两个直播仓库地址都取不到，电视端会提示解析失败")
        return False
    log("直播仓库自检通过：%d/%d 条可用" % (ok_repo, len(repo_urls)))
    return True


def check_live_feed():
    """直播源直链自检：电视端"直播"页直接添加的地址是否还能取到列表。

    这批地址不经过 config 线路，所以必须单独盯——否则 GitHub 推送失败 / 加速站挂掉，
    电视上就是一片空白，而每日日志依旧显示"直播OK"。
    """
    ok_cnt = 0
    local_cnt = 0
    live_path = os.path.join(BASE_DIR, 'live_list.m3u')
    if os.path.exists(live_path):
        try:
            with open(live_path, 'r', encoding='utf-8', errors='ignore') as f:
                local_cnt = len(parse_m3u_blocks(f.read()))
        except Exception:
            local_cnt = 0
    for host, tpl in LIVE_FEED_MIRRORS:
        url = tpl % 'live_list.m3u'
        try:
            # ghfast 直读偶发超过单次 12s 超时，用重试避免把"慢"误判成"挂"
            text = fetch_retry(url, limit=LIVE_DOWNLOAD_LIMIT, attempts=2, wait=2)
        except Exception as e:
            log("  ⚠️ 直播直链不可用(%s)：%s" % (host, str(e)[:40]))
            continue
        if not (text and text.lstrip().startswith('#EXTM3U')):
            log("  ⚠️ 直播直链内容异常(%s)" % host)
            continue
        cnt = len(parse_m3u_blocks(text))
        if cnt < 500:
            log("  ⚠️ 直播直链条目过少(%s)：%d 条" % (host, cnt))
            continue
        ok_cnt += 1
        log("  直播直链可用：%s（%d 条）" % (host, cnt))
        if local_cnt and cnt != local_cnt:
            log("  ⚠️ %s 与本地列表不一致（线上 %d 条 / 本地 %d 条）：镜像缓存或推送未生效" % (host, cnt, local_cnt))
    if ok_cnt == 0:
        notify_change("直播源直链全部取不到，电视「直播」页会空白")
        return False
    log("直播源直链自检通过：%d/%d 条镜像可用" % (ok_cnt, len(LIVE_FEED_MIRRORS)))
    return True


def check_public_urls():
    """对外地址自检：确认 live_source.json 里给电视用的地址真的能下载到 m3u。

    2026-10 曾发生 Gitee 对 live_list.m3u 返回 451（内容违规拦截），
    电视端一个台都放不出来，而脚本一直报"保鲜成功"——因为从没校验过对外可用性。
    """
    path = os.path.join(BASE_DIR, 'live_source.json')
    if not os.path.exists(path):
        return True
    try:
        with open(path, 'r', encoding='utf-8') as f:
            src = json.load(f)
    except Exception as e:
        log("自检：读取 live_source.json 失败：%s" % e)
        return True
    urls = [it.get('url') for it in (src.get('lives') or [])
            if isinstance(it, dict) and it.get('url')]
    if not urls:
        return True
    ok = 0
    for u in urls:
        content = fetch(u, limit=LIVE_DOWNLOAD_LIMIT)
        if content and content.lstrip().startswith('#EXTM3U'):
            ok += 1
            log("  对外地址可用：%s" % u)
        else:
            log("  ⚠️ 对外地址不可用：%s" % u)
    if ok == 0:
        notify_change("直播列表所有对外地址都不可用，请检查 live_source.json")
        return False
    log("对外地址自检通过：%d/%d 条线路可用" % (ok, len(urls)))
    return True


def main():
    log("===== 开始自动查找与验证影视源 =====")
    today = datetime.date.today().isoformat()
    status = load_status()
    cfg = load_config()
    store = cfg.get("storeName", "本地聚合")

    # 合并现有源与候选池，按 url 去重
    existing = {}  # url -> (name, 是否已在config)
    for it in cfg.get("urls", []):
        if isinstance(it, dict) and it.get("url"):
            existing[it["url"]] = (it.get("name", "未命名"), True)
    for name, url in CANDIDATES:
        if url not in existing:
            existing[url] = (name, False)

    keep = []  # (name, url)：写入 config
    old_urls = set(u for u, (n, ex) in existing.items() if ex)  # 更新前 config 里的源
    removed = []
    added = []
    for url, (name, is_existing) in existing.items():
        if url in BLOCKLIST:
            log("跳过电视黑名单源 %s (%s)" % (name, url))
            if is_existing:
                removed.append(name)
            continue
        log("验证 %s (%s)" % (name, url))
        content = fetch(url)
        ok, note = validate(content)
        rec = status.setdefault(url, {"name": name, "last_ok": None, "fail_days": 0})
        rec["name"] = name
        if ok:
            # 站点级抽查：仅记录提醒，不参与可用性判定（防止误伤正常源）
            s_ok, s_total = site_spot_check(content)
            if s_total > 0:
                note = "%s(站点抽查%d/%d)" % (note, s_ok, s_total)
                if s_ok == 0:
                    note += " ⚠️站点不可达提醒"
            rec["fail_days"] = 0
            rec["last_ok"] = today
            rec["status"] = "ok"
            rec["note"] = note
            keep.append((name, url))
            if not is_existing:
                added.append(name)
        else:
            rec["fail_days"] = rec.get("fail_days", 0) + 1
            rec["status"] = "fail"
            rec["note"] = note
            if rec.get("last_ok") is None:
                log("  %s 从未验证通过，不加入配置" % name)
            elif rec["fail_days"] < MAX_FAIL_DAYS:
                keep.append((name, url))  # 曾可用，未达阈值先保留
            else:
                log("  %s 连续%d天失效，已剔除" % (name, MAX_FAIL_DAYS))
                if is_existing:
                    removed.append(name)
        log("  -> %s" % note)

    # 写回 config（只保留验证通过或曾可用未达阈值的源，控制上限）
    new_urls = []
    seen = set()
    for name, url in keep:
        if url in seen:
            continue
        seen.add(url)
        new_urls.append({"name": name, "url": url})
        if len(new_urls) >= MAX_SOURCES:
            break

    # 清理长期失效且从未成功的僵尸状态记录，保持 status.json 精简
    for u in [u for u, r in status.items() if r.get("last_ok") is None and r.get("fail_days", 0) >= MAX_FAIL_DAYS]:
        status.pop(u, None)

    # 清掉已进黑名单源的历史记录：它们永远不会再被刷新，留着只会让 status.json
    # 堆积一堆 status=ok 的僵尸条目，误导人为"当前可用源"很多。
    for u in list(status.keys()):
        if u in BLOCKLIST:
            status.pop(u, None)

    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump({"storeName": store, "urls": new_urls}, f, ensure_ascii=False, indent=2)
    save_status(status)

    # 可用数只统计 config 中实际保留的源；status 里还留着已进黑名单的历史记录，
    # 直接用 status 统计会得出"保留 6 个源、当前可用 19 个"这种自相矛盾的数字。
    ok_count = sum(1 for it in new_urls if status.get(it["url"], {}).get("status") == "ok")
    log("更新完成：config 保留 %d 个源，其中当前可用 %d 个" % (len(new_urls), ok_count))
    # 有变化时发送桌面通知
    if added or removed:
        msg = ""
        if added:
            msg += "新增源：%s。" % "、".join(added)
        if removed:
            msg += "剔除失效源：%s。" % "、".join(removed)
        msg += "当前配置 %d 个源。" % len(new_urls)
        notify_change(msg)
    elif ok_count >= 2 and len([it for it in new_urls if status.get(it["url"], {}).get("last_ok") == today]) < 1:
        notify_change("今日源检测异常，请查看 update_log.txt")
    # 儿童聚合验证 + 直播列表保鲜
    validate_kids()
    refresh_live()
    push_res = push_remotes()
    push_ok = any(push_res.values())
    if not push_res.get('origin'):
        log("⚠️ GitHub 未同步：直播直链会继续用线上旧列表，链路恢复后需补推")
    # 推送之后再做对外地址自检，确认电视端真的能下载到直播列表
    check_live_repo()
    check_live_feed()
    check_public_urls()
    log("===== 结束 =====")
    # 推送失败必须显式告警：以前异常被吞掉，Windows 任务显示 LastTaskResult=0、
    # Actions 的 step 也是绿的，故障完全不可见（2026-10-17 就出现过）。
    if not push_ok:
        notify_change("自动更新跑完了，但推送到远端失败，请检查网络")
    return push_ok


if __name__ == "__main__":
    try:
        _ok = main()
    except Exception as e:
        log("运行异常: %s" % e)
        _ok = False
    # 让调用方（Windows 计划任务 / GitHub Actions）能从退出码看出成败
    sys.exit(0 if _ok else 1)
