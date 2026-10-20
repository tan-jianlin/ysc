# -*- coding: utf-8 -*-
"""
影视仓聚合接口 - 每日自动查找与验证影视源
每天 12:00（北京时间）自动运行，触发方式有两种：
  - Windows 计划任务（本机）
  - GitHub Actions（.github/workflows/update.yml，UTC 04:00）
流程：
  1. 验证 config / 候选池 / 黑名单到期项 / 历史台账里的源（下载 + TVBox 结构 + 站点抽查 + 真实搜索探针）
  2. 连续 3 天失效的源自动剔除，但会留在 known_sources.json 台账里，每 7 天重试一次
  3. 黑名单（blocklist.json）带 30 天冷却期，到期自动复核，通过就放回 config
  4. 从候选源池自动验证可用新源并补充进 config，超过上限时按"最近可用日期"截断
  5. 更新 status.json 记录每个源的检测状态
  6. 同步 config / config_kids / status.json / blocklist.json / known_sources.json / live_list.m3u 到远端
"""
import json
import os
import re
import socket
import subprocess
import ssl
import sys
import time
import base64
import urllib.request
import urllib.parse
import urllib.error
import concurrent.futures
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config')
STATUS_PATH = os.path.join(BASE_DIR, 'status.json')
BLOCKLIST_PATH = os.path.join(BASE_DIR, 'blocklist.json')      # 带冷却期的黑名单（运行时权威数据）
KNOWN_PATH = os.path.join(BASE_DIR, 'known_sources.json')      # 历史台账：见过的源都留档，剔除后仍可回归
LOG_PATH = os.path.join(BASE_DIR, 'update_log.txt')
REMOTES_PATH = os.path.join(BASE_DIR, 'remotes.json')
LIVE_SOURCE_PATH = os.path.join(BASE_DIR, 'live_source.json')
# 独立托管的地址台账（GitHub 之外的兜底线路，每天重新上传）
MIRROR_PASTE_PATH = os.path.join(BASE_DIR, 'mirror_paste.json')
PASTE_RS = 'https://paste.rs'

# status.json 里"电视直播"这条记录的键。沿用 2026-10-17 写入的同一个键，
# 否则每天都会新建一条、旧的那条永远停在 10-17（2026-10-20 就踩到了）。
LIVE_SOURCE_STATUS_KEY = 'https://gitee.com/tan-jianlin/ysc/raw/master/live_source.json'

MAX_FAIL_DAYS = 3            # 连续失败天数阈值，达到后自动剔除
MAX_SOURCES = 15             # config 中最多保留的源数量

# ---------------- 淘汰与复活策略（2026-10-20 重构） ----------------
# 旧版 BLOCKLIST 是一张只进不出的硬编码名单：某个源因为"当时确实不行"被写进去，
# 之后哪怕它恢复了，脚本也只会打一行"跳过电视黑名单"然后连 fetch 都不发。
# 名单越积越长，config 里的活跃源反而越来越少（2026-10-20 实测：14 条黑名单
# 全部电脑侧可达，却已经连续被跳过 12 轮）。
# 现在拆成三层，保证"能回来"：
#   1. 黑名单带冷却期：到期自动复核一次，通过就放回 config
#   2. 历史台账：所有见过的源都留档，被自动剔除后仍保留低频重试资格
#   3. 复核不通过才继续黑着，冷却期从最后一次复核重新计时
BLACKLIST_COOLDOWN_DAYS = 30   # 黑名单冷却期：到期自动复核一次
MAX_BLOCKLIST_REVIVE = 3       # 每轮最多放行几条黑名单源（避免一次涌入太多，电视端受不了）
RETIRED_RETRY_DAYS = 7         # 被自动剔除的历史源：每 7 天重试一次，不必每天浪费请求
MAX_RETIRED_RETRY = 8          # 单轮最多重试多少条历史源（控制总耗时）
VERIFY_WORKERS = 6             # 验证并发数（源变多后串行太慢）
SEARCH_KEYWORDS = ('斗罗大陆', '流浪地球')   # 搜索探针用词，任一命中即算通
SEARCH_PROBE_SITES = 5         # 每个源最多抽几个真·苹果CMS 接口发搜索请求
SEARCH_PROBE_MIN = 2           # 可探测接口少于这个数就不下"搜不到片"的结论（样本不足）
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
    # 顺序即优先级。前三条是 jsDelivr 系，电视端实测最稳，保持 2026-10-17 定下的主线路不变；
    # 后面几条是直读 GitHub raw 的加速代理，作为 jsDelivr 被打断时的兜底。
    # 注意：jsDelivr 对 @master 分支有最长 12h 缓存，刚推上去的列表它可能还是旧的那份，
    # 所以"线上条数与本地不一致"的告警在 jsDelivr 上属于正常现象，不代表推送失败。
    ('jsdelivr', 'https://cdn.jsdelivr.net/gh/tan-jianlin/ysc@master/%s'),
    ('testingcf', 'https://testingcf.jsdelivr.net/gh/tan-jianlin/ysc@master/%s'),
    ('gcore', 'https://gcore.jsdelivr.net/gh/tan-jianlin/ysc@master/%s'),
    ('ghfast', 'https://ghfast.top/https://raw.githubusercontent.com/tan-jianlin/ysc/master/%s'),
    # GitHub Pages：2026-10-20 通过 API 开通，cdn 走 Fastly、域名与 raw.githubusercontent 完全分开。
    # 实测 live_light.m3u 200 / 262561B / 1180 条。它的价值在于"raw 域名被墙时还有一条路"，
    # 而且推送后会自动重新构建，不需要额外维护。
    ('pages', 'https://tan-jianlin.github.io/ysc/%s'),
    ('gh-proxy', 'https://gh-proxy.com/https://raw.githubusercontent.com/tan-jianlin/ysc/master/%s'),
    ('onmicrosoft', 'https://jsd.onmicrosoft.cn/gh/tan-jianlin/ysc@master/%s'),
    ('jsdmirror', 'https://cdn.jsdmirror.com/gh/tan-jianlin/ysc@master/%s'),
]

# 电视实测不可用黑名单的「初始种子」：url -> 加入日期。
# 运行时权威数据存在 blocklist.json（脚本自己读写、自己推进冷却期），
# 这里只在全新环境首次生成 blocklist.json 时用一次，之后改这里不再生效。
# 加入原因统一是：家庭网络无法访问 / 影视仓解析失败（电脑侧能下到但电视放不出）。
# 日期故意写成 30 天前：让首次运行就到期复核，用实际结果说话，而不是永远信任旧结论。
BLOCKLIST_SEED = {   # url -> (加入日期, 显示名)
    "https://jihulab.com/mengzhu2/ysc/-/raw/main/YSC.json": ('2026-09-20', 'YSC全量源'),
    "https://jihulab.com/ygbh1/box/-/raw/main/dcang/dc.json": ('2026-09-20', '月光宝盒'),
    "http://ztha.top/TVBox/GYCK.json": ('2026-09-20', '挺好分享'),
    "http://home.jundie.top:81/top98.json": ('2026-09-20', '俊哥源'),
    "http://xmbjm.fh4u.org/dc.txt": ('2026-09-20', '拾光多仓'),
    "http://xhztv.top/dc": ('2026-09-20', '小盒子多仓'),
    "https://www.yingm.cc/dm/dm.json": ('2026-09-20', '动漫专属'),          # 电视打不开
    "http://影视仓.com/": ('2026-09-20', '影视仓com'),                        # 中文域名：电视不支持
    "http://tvbox.王二小放牛娃.top": ('2026-09-20', '王二小'),                 # 中文域名：电视不支持
    "https://gh-proxy.com/https://raw.githubusercontent.com/qist/tvbox/master/jsm.json": ('2026-09-20', 'jsm大而全'),
    "https://gh-proxy.com/https://raw.githubusercontent.com/qist/tvbox/master/fty.json": ('2026-09-20', '饭太硬fty'),
    "https://gh-proxy.com/https://raw.githubusercontent.com/qist/tvbox/master/xiaosa/api.json": ('2026-09-20', '潇洒'),
    "https://gh-proxy.com/https://raw.githubusercontent.com/gaotianliuyun/gao/master/js.json": ('2026-09-20', '高天流云'),
    "https://gh-proxy.com/https://raw.githubusercontent.com/xyq254245/xyqonlinerule/main/XYQTVBox.json": ('2026-09-20', '香雅情'),
}

# 站点级抽查：验证通过后均匀抽 N 个站点，防止"文件在但内容全失效"
# 3 → 5：抽样太少时，一条正常源也会因为恰好抽到两个死站而显示 0/3（2026-10-20）
SITE_CHECK_N = 5

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

# 本轮直播体检摘要（由 filter_overseas 等写、main 读，落进 status.json 的 note）。
# 为什么需要：体检判定"本机网络异常"时会保留原样不动，旧版只在日志里打一行就 return，
# 结果 status 里照样写"直播OK"，等于当天这次体检悄悄作废、对外看起来却是绿灯。
LIVE_HEALTH = {}

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

# 每轮从"多仓源"里提取并验证几条新子源（候选自动发现，见 discover_candidates）。
# 120 个子源按每轮 6 条轮转，约 20 轮覆盖一遍；试满 3 次不通过的会被踢出轮转。
MAX_DISCOVER_PER_RUN = 6

# 新发现的候选连续失败几次才转入低频重试。给两次机会：很多源只是间歇性抽风，
# 一次不通就永久放弃等于把 120 个子源池白白浪费掉一半。
MAX_CANDIDATE_FAILS = 2


def log(msg):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + "\n")
    except Exception:
        pass


def idna_url(url):
    """把中文域名转 punycode、中文路径转百分号编码，urllib 才能请求。

    2026-10-20 补：旧版只处理 hostname，路径里的中文原样交给 urllib，
    请求时直接抛 'ascii' codec can't encode... ordinal not in range(128)，
    于是所有"带中文路径"的源必然下载失败。影视仓生态里这类源不少
    （实测「肥猫」的多仓子源就是 http://我不是.肥猫.love:63/接口禁止贩卖）。
    """
    try:
        from urllib.parse import urlparse, urlunparse, quote
        p = urlparse(url)
        netloc = p.netloc
        if p.hostname and any(ord(c) > 127 for c in p.hostname):
            host = p.hostname.encode('idna').decode('ascii')
            netloc = host if p.port is None else '%s:%d' % (host, p.port)
        # 逐字符处理：ASCII 可见字符原样保留（已有的 %XX 不会被二次编码），
        # 非 ASCII、空格、控制字符统一转百分号编码。
        path = ''.join(c if 32 < ord(c) < 127 else quote(c) for c in p.path)
        query = ''.join(c if 32 < ord(c) < 127 else quote(c) for c in p.query)
        if netloc == p.netloc and path == p.path and query == p.query:
            return url
        return urlunparse((p.scheme, netloc, path, p.params, query, p.fragment))
    except Exception:
        pass
    return url


# 进程内下载缓存。config 里的多仓源在"验证"和"候选发现"两个阶段会读同一份内容，
# 不缓存就要把 15 条配置重复下载两遍。只缓存成功结果——失败不缓存，否则 verify_source
# 的重试会命中空结果，等于把重试废掉。生命周期仅限一次进程，不会读到昨天的旧配置。
_FETCH_CACHE = {}


def fetch(url, limit=DOWNLOAD_LIMIT):
    """下载 URL 内容，返回文本；失败返回 None（兼容 gzip、中文域名）"""
    cache_key = (url, limit)
    if cache_key in _FETCH_CACHE:
        return _FETCH_CACHE[cache_key]
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
            text = body.decode('utf-8', 'ignore')
            _FETCH_CACHE[cache_key] = text
            return text
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


def parse_tvbox(content):
    """尽力把配置解析成 dict：容忍 BOM、// 整行注释、尾随逗号、字符串里的裸换行。

    2026-10-20 踩到的坑：聚玩盒子4K 的 4k.json 里既有 `//////` 分隔注释行，
    又有字符串中间藏真实换行的 "ext" 字段（"Invalid control character"）。
    严格 json.loads 直接失败，旧版探针于是退回正则抓 api，结果把 drpy 的
    JS 脚本地址（drpy2.min.js）当成搜索接口去请求，把活得好好的源误判成
    "搜不到片"。影视仓自己的解析器能容忍这些，这边也必须跟上。
    """
    if not content:
        return None
    raw = content.lstrip('\ufeff').strip()
    # 整行注释两种写法都见过：//（聚玩盒子4K）和 ## json（HGYX影视）
    cleaned = re.sub(r',\s*([}\]])', r'\1',
                     re.sub(r'(?m)^\s*(?://|#).*$', '', raw))
    for cand in (raw, cleaned):
        for kw in ({'strict': False}, {}):
            try:
                j = json.loads(cand, **kw)
                if isinstance(j, dict):
                    return j
            except Exception:
                pass
    try:
        j, _ = json.JSONDecoder(strict=False).raw_decode(raw)
        if isinstance(j, dict):
            return j
    except Exception:
        pass
    return None


def _even_pick(items, k):
    """从列表里均匀抽 k 个（含首尾）。

    2026-10-20 的教训：这类 TVBox 配置里，同一发布者的站点常常扎堆排在前面，
    一起挂、一起活。取前 N 个必然踩到同一批已死的站，得出片面结论
    （聚玩盒子4K 就是前 3 个全是 JS 脚本 + 死域名，53 个站点里 4 个真能搜）。
    """
    n = len(items)
    if k <= 0 or n == 0:
        return []
    if n <= k:
        return list(items)
    if k == 1:
        return [items[0]]
    picked = []
    for i in range(k):
        u = items[round(i * (n - 1) / (k - 1))]
        if u not in picked:
            picked.append(u)
    return picked


def site_spot_check(content):
    """站点级抽查：均匀抽 N 个站点 API，能收到任何 HTTP 响应即视为可达（仅提醒，不参与剔除）"""
    j = parse_tvbox(content)
    if not j:
        return 0, 0
    sites = j.get("sites")
    if not isinstance(sites, list) or len(sites) == 0:
        return 0, 0
    urls = []
    for s in sites:
        if isinstance(s, dict):
            u = s.get("api") or s.get("url")
            if u and isinstance(u, str) and u.startswith(("http://", "https://")) and u not in urls:
                urls.append(u)
    urls = _even_pick(urls, SITE_CHECK_N)   # 均匀采样，不再取前 N 个
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


def _search_one(api, keyword, timeout=6):
    """对单个站点 API 发一次苹果CMS 标准搜索，能收到带片名的结果就算命中。

    影视仓里绝大多数 http 型站点都是苹果CMS（provide/vod），搜索接口固定为
    `?ac=videolist&wd=关键词`（部分站点只认 ac=detail，两个都试）。
    Returns (hit, note)
    """
    base = api.strip()
    if not base.startswith(("http://", "https://")):
        return False, "非http"
    err = ""
    for ac in ('videolist', 'detail'):
        sep = '&' if '?' in base else '?'
        url = "%s%sac=%s&wd=%s" % (base, sep, ac, urllib.parse.quote(keyword))
        try:
            req = urllib.request.Request(idna_url(url), headers={
                'User-Agent': 'okhttp/3.12.1', 'Accept-Encoding': 'identity'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read(200 * 1024).decode('utf-8', 'ignore')
        except urllib.error.HTTPError as e:
            # 这里是 continue 不是 return：部分站点只对 ac=videolist 做防盗链（403/429），
            # 换 ac=detail 反而能出结果。以前直接 return 等于放弃了后半段。
            err = "HTTP %d" % e.code
            continue
        except Exception as e:
            err = type(e).__name__
            continue
        # JSON（苹果CMS v10 默认）或 XML（v8/v9）两种返回格式都认
        if ('"vod_id"' in body and '"vod_name"' in body) or '<video>' in body or '<vod_id>' in body:
            return True, "ac=%s 命中" % ac
        if '"list":[]' in body or '"list": []' in body or len(body.strip()) < 40:
            err = "ac=%s 空结果" % ac
            continue
    return False, err or "无片名字段"


def search_probe(content):
    """真实搜索探针：抽配置里几个真·苹果CMS 接口，发一次搜索看能不能搜出片。

    2026-10-20 修正（误判过聚玩盒子4K）：
      - 必须能结构化解析配置，解析不了就返回"无法探测"，绝不退回正则乱抓；
        正则会把 drpy 的 JS 脚本地址（xxx.min.js）当接口请求，必然失败。
      - 只探测 type 0/1（xml/json）且 api 是 http 且不以 .js 结尾的站点。
        type 3 是 spider/drpy，api 指向脚本文件或 csp_ 类名，网络侧无法搜索。
      - 可探测接口少于 SEARCH_PROBE_MIN 时，只报"样本不足"，不下负面结论。
    Returns (hit, tried, detail_text)
    """
    j = parse_tvbox(content)
    if not j:
        return 0, 0, "配置无法结构化解析，不判定"
    sites = j.get("sites")
    if not isinstance(sites, list) or not sites:
        return 0, 0, "无 sites 字段"
    apis = []
    spider_cnt = 0
    for s in sites:
        if not isinstance(s, dict):
            continue
        u = s.get("api") or s.get("url")
        if s.get("type") not in (0, 1, None) or not isinstance(u, str) \
                or not u.startswith(("http://", "https://")):
            spider_cnt += 1        # spider/drpy/扩展类，跳过
            continue
        low = u.split("?")[0].lower()
        if low.endswith(".js") or ".m.js" in low:
            spider_cnt += 1        # drpy 脚本地址，不是搜索接口
            continue
        if u not in apis:
            apis.append(u)
    if not apis:
        return 0, 0, "全部是内置站点（%d 个），无法网络探测" % spider_cnt
    # 均匀采样而不是取前 N 个：这类配置里同一发布者的站点常扎堆排在前面，
    # 只取前几个会踩到同一批已挂掉的站，得出片面结论（2026-10-20 聚玩盒子4K 就踩了）。
    apis = _even_pick(apis, SEARCH_PROBE_SITES)

    def probe(api):
        note = ""
        for kw in SEARCH_KEYWORDS:
            hit, note = _search_one(api, kw)
            if hit:
                return True, "%s:%s" % (kw, note)
        return False, note

    hit = 0
    details = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(apis)) as ex:
        for ok, note in ex.map(probe, apis):
            if ok:
                hit += 1
            details.append(note)
    return hit, len(apis), "%s（另 %d 个内置站点不可测）" % ("/".join(details), spider_cnt)


def verify_source(url):
    """单个源的完整验证：下载 + 结构校验 + 站点抽查 + 真实搜索探针。

    只做网络与解析，不碰 status/日志汇总，便于放进线程池并发跑。
    Returns dict(ok, note, spot_ok, spot_total, search_hit, search_tried, search_note)
    """
    content = fetch(url)
    if content is None:
        # 单次抖动不该算一次"失败日"：fail_days 每轮 +1，连续 3 轮就剔除，
        # 而 3 轮可能只跨 2 天。补一次重试，把"慢/抖"和"真挂"分开。
        time.sleep(1)
        content = fetch(url)
    ok, note = validate(content)
    r = {"ok": ok, "note": note, "spot_ok": 0, "spot_total": 0,
         "search_hit": 0, "search_tried": 0, "search_note": ""}
    if not ok:
        return r
    try:
        r["spot_ok"], r["spot_total"] = site_spot_check(content)
    except Exception:
        pass
    try:
        r["search_hit"], r["search_tried"], r["search_note"] = search_probe(content)
    except Exception as e:
        r["search_note"] = "探测异常:%s" % type(e).__name__
    return r


def discover_candidates(cfg, known, bl, skip_urls, limit=None):
    """从 config 里"多仓源"的 urls 字段提取子源地址，补充候选池。

    为什么需要（2026-10-20 审计）：内置 CANDIDATES 只有 5 条且全部已在 config，
    脚本只会验证"已知的源"，没有任何发现新源的途径。一旦现有源陆续坏掉，config 会
    慢性萎缩且无法回补——以前"挺好分享""月光宝盒"这些就是这么消失的。

    为什么不自动爬公开源聚合仓库：那会把死源、垃圾源、带广告劫持的源一起吸进来，
    风险远大于收益。而多仓配置里的 urls 字段是"你正在用的源"自己推荐的子源，
    来源可信、结构统一（实测 4 条多仓共带 120 个子源地址），是最干净的候选来源。

    本函数只负责"提名"，判定完全交给统一的 verify_source，和内置候选走同一条流程。
    返回 (本轮挑出的 [(name,url)], 总共新发现条数)。
    """
    limit = limit or MAX_DISCOVER_PER_RUN
    total = 0
    found = []
    for it in cfg.get("urls", []):
        u = it.get("url") if isinstance(it, dict) else None
        if not u:
            continue
        content = fetch(u)          # 验证阶段已下载过，这里直接命中进程内缓存
        j = parse_tvbox(content) if content else None
        if not isinstance(j, dict):
            continue
        subs = j.get("urls")
        if not isinstance(subs, list) or not subs:
            continue          # 单仓配置，没有子源可提
        for s in subs:
            if not isinstance(s, dict):
                continue
            su = s.get("url")
            if not isinstance(su, str) or not su.startswith(("http://", "https://")):
                continue
            if su in skip_urls or su in bl:
                continue
            rec = known.get(su, {})
            # 试过 3 次都不行的子源不再占名额，免得 120 个子源把轮转周期拖成一个月
            if (rec.get("probe_fail") or 0) >= 3:
                continue
            total += 1
            found.append((rec.get("last_try") or "", s.get("name") or su[:30], su))
    # 最久没验证过的优先，保证 120 个子源能均匀轮转，不会每次都卡在前面几个
    found.sort(key=lambda t: t[0])
    picked, seen_url = [], set()
    for _, name, su in found:
        if su in seen_url:
            continue
        seen_url.add(su)
        picked.append((name, su))
        if len(picked) >= limit:
            break
    return picked, total


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


# ---------------- 黑名单冷却期 + 历史台账 ----------------

def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        log("读取 %s 失败，用默认值: %s" % (os.path.basename(path), e))
        return default


def _save_json(path, obj):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_blocklist():
    """url -> {name, since, reason, retries, last_retry}。首次运行用种子生成。"""
    bl = _load_json(BLOCKLIST_PATH, None)
    if not isinstance(bl, dict):
        bl = {}
        for url, v in BLOCKLIST_SEED.items():
            since, name = v if isinstance(v, tuple) else (v, "")
            bl[url] = {"name": name, "since": since,
                       "reason": "电视实测不可用（初始种子）", "retries": 0, "last_retry": None}
    # 兼容旧格式与手工简写：url -> "2026-09-20" 或 url -> (日期, 名字)
    for u, v in list(bl.items()):
        if isinstance(v, str):
            bl[u] = {"name": "", "since": v, "reason": "", "retries": 0, "last_retry": None}
        elif isinstance(v, (tuple, list)) and len(v) == 2:
            bl[u] = {"name": v[1], "since": v[0], "reason": "", "retries": 0, "last_retry": None}
    return bl


def load_known():
    """url -> {name, first_seen, last_ok, last_fail, retired}。所有见过的源都留档。"""
    k = _load_json(KNOWN_PATH, {})
    return k if isinstance(k, dict) else {}


def days_between(a, b):
    """a、b 都是 ISO 日期字符串，返回 b-a 的天数；解析失败返回一个大数（视为早已到期）。"""
    try:
        return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days
    except Exception:
        return 10 ** 6


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {"storeName": "本地聚合", "urls": []}
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        log("读取 config 失败，重建: %s" % e)
        return {"storeName": "本地聚合", "urls": []}


# ---------------- GitHub API 直写（替代本机走不通的 HTTPS git） ----------------
GH_OWNER = 'tan-jianlin'
GH_REPO = 'ysc'
GH_BRANCH = 'master'
GH_API = 'https://api.github.com'


def _gh_token():
    """取 GitHub 令牌：先环境变量，再 Windows 凭证管理器（git credential fill）。

    令牌不写进仓库、不落明文文件——凭证管理器里存的是 DPAPI 加密的，
    脚本只在内存里用一次。
    """
    for k in ('GITHUB_TOKEN', 'GH_TOKEN'):
        v = os.environ.get(k)
        if v and v.strip():
            return v.strip()
    try:
        p = subprocess.run(['git', '-C', BASE_DIR, 'credential', 'fill'],
                           input='protocol=https\nhost=github.com\n\n',
                           capture_output=True, text=True, timeout=30)
        for line in (p.stdout or '').splitlines():
            if line.startswith('password='):
                return line[len('password='):].strip()
    except Exception:
        pass
    return None


def _gh_resolve_ips():
    """用 DoH 取 api.github.com 的真实 IP。

    必须绕开本机 DNS：这里把 api.github.com 解析成 140.82.112.3（github.com 的网页 IP），
    连上去 SNI 是 api.github.com、对面却是网页前端，于是稳定返回 404 / 301。
    2026-10-20 我因为这个绕了很久，先误判"仓库没了"，又误判"令牌没权限"。
    """
    ips = []
    for url in ('https://dns.alidns.com/resolve?name=api.github.com&type=A',
                'https://223.5.5.5/resolve?name=api.github.com&type=A'):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'ysc-updater',
                                                       'Accept': 'application/dns-json'})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=8) as r:
                d = json.loads(r.read().decode('utf-8', 'ignore'))
            for a in (d.get('Answer') or []):
                if a.get('type') == 1 and a.get('data') and a['data'] not in ips:
                    ips.append(a['data'])
        except Exception:
            continue
        if ips:
            break
    return ips


def _gh_api(method, path, payload=None, timeout=30):
    """调用 GitHub API：绕过代理 + 固定真实 IP，两件事都是本机必需的。

    绕代理是因为本机环境变量里的代理会把这个域名劫持成 301；
    固定 IP 是因为本机 DNS 返回的是 github.com 的地址，连上去只有 404。
    TLS 仍然按 api.github.com 做 SNI 和证书校验，IP 只是被替换掉解析结果。
    """
    token = _gh_token()
    if not token:
        log("  未取到 GitHub 令牌，跳过 API 通道")
        return None
    ips = _gh_resolve_ips() or ['20.205.243.168']
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    url = GH_API + path
    last_err = ''
    orig_gai = socket.getaddrinfo
    for ip in ips:
        def _pinned(host, port, *a, **kw):
            if host == 'api.github.com':
                return orig_gai(ip, port, *a, **kw)
            return orig_gai(host, port, *a, **kw)
        socket.getaddrinfo = _pinned
        try:
            req = urllib.request.Request(
                url, data=data, method=method,
                headers={'Authorization': 'token ' + token,
                         'Accept': 'application/vnd.github+json',
                         'Content-Type': 'application/json',
                         'User-Agent': 'ysc-updater'})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read()
            return json.loads(body.decode('utf-8')) if body else {}
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = e.read().decode('utf-8', 'ignore')[:160]
            except Exception:
                pass
            last_err = "HTTP %d %s" % (e.code, detail)
            # 401/403 是令牌问题，换 IP 没用；其余（404/5xx/超时）换下一个 IP 再试
            if e.code in (401, 403):
                break
        except Exception as e:
            last_err = str(e)[:100]
        finally:
            socket.getaddrinfo = orig_gai
    log("  GitHub API %s %s 失败：%s" % (method, path, last_err))
    return None


def push_github_via_api(files, message):
    """把本地文件写成 GitHub 上的一次提交，效果等同一次 push，但不走 git 协议。

    为什么必须这么做：2026-10-20 实测本机到 github.com:443 的 9 个已知 IP 全部超时
    （curl 直连 000，6 秒无应答），能 push 成功只是恰好命中沙箱代理；
    而 api.github.com:443 直连稳定 200（连测三次 1.0—1.8 秒）。
    既然 HTTPS git 这条路在本机是断的，就改用它底下的 API：Git Data API
    建 blob → tree → commit → 移动 ref，内容与本地逐字节一致。
    结果：GitHub 侧会形成一条独立于 Gitee 的提交链，这是刻意的——
    它只为"给电视端提供 m3u 回源"，不需要和 Gitee 共享历史。
    """
    ref = _gh_api('GET', '/repos/%s/%s/git/ref/heads/%s' % (GH_OWNER, GH_REPO, GH_BRANCH))
    if not ref or 'object' not in ref:
        return False
    base_sha = ref['object']['sha']
    base = _gh_api('GET', '/repos/%s/%s/git/commits/%s' % (GH_OWNER, GH_REPO, base_sha))
    if not base:
        return False
    base_tree = (base.get('tree') or {}).get('sha')
    entries = []
    for rel in files:
        path = os.path.join(BASE_DIR, rel)
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'rb') as f:
                content = base64.b64encode(f.read()).decode('ascii')
        except Exception as e:
            log("  API 读取 %s 失败：%s" % (rel, e))
            continue
        blob = _gh_api('POST', '/repos/%s/%s/git/blobs' % (GH_OWNER, GH_REPO),
                       {'content': content, 'encoding': 'base64'})
        if not blob or 'sha' not in blob:
            return False
        entries.append({'path': rel.replace('\\', '/'), 'mode': '100644',
                        'type': 'blob', 'sha': blob['sha']})
    if not entries:
        return False
    tree = _gh_api('POST', '/repos/%s/%s/git/trees' % (GH_OWNER, GH_REPO),
                   {'base_tree': base_tree, 'tree': entries})
    if not tree or 'sha' not in tree:
        return False
    commit = _gh_api('POST', '/repos/%s/%s/git/commits' % (GH_OWNER, GH_REPO),
                     {'message': message, 'tree': tree['sha'], 'parents': [base_sha]})
    if not commit or 'sha' not in commit:
        return False
    moved = _gh_api('PATCH', '/repos/%s/%s/git/refs/heads/%s' % (GH_OWNER, GH_REPO, GH_BRANCH),
                    {'sha': commit['sha'], 'force': True})
    if not moved:
        return False
    log("  已通过 GitHub API 提交 %s（%d 个文件）" % (commit['sha'][:8], len(entries)))
    return True


def push_remotes():
    """把 config / 直播列表同步到远端，让电视不依赖电脑也能读到最新配置。

    2026-10-17 起同时推 origin(GitHub) 与 gitee，两个远端独立成败：
      - gitee 承载 config（点播线路），国内必达；
      - origin 承载直播 m3u（Gitee 对含 #EXTM3U 的内容一律 451，只能放 GitHub）。
    分开推的原因：当天本机到 GitHub 的 TLS 链路中断时，若只推 origin，Gitee 上的
    config 也会跟着停更，电视点播一起挂；而每日任务会因此天天弹窗，实际线路是好的。
    只有两个远端都失败才算故障。

    GIT_PUSH_REMOTE 显式指定时只推该远端（GitHub Actions 里用它推自己）。
    --no-push 时只更新本地文件、不提交不推送（试跑用，确认结果满意再正式跑一次）。
    返回 {remote: bool}。
    """
    if '--no-push' in sys.argv:
        log("--no-push：只更新本地文件，跳过提交与推送")
        return {'origin': True, 'gitee': True}
    only = os.environ.get('GIT_PUSH_REMOTE')
    in_ci = bool(os.environ.get('GITHUB_ACTIONS'))
    if only:
        targets = [only]
    elif in_ci:
        targets = ['origin', 'gitee']      # CI 里 HTTPS git 正常，照旧推两个
    else:
        # 本机到 github.com:443 的 9 个 IP 全部超时，git push origin 必然白等，
        # 不再浪费每天几分钟去撞它；GitHub 改走下面的 API 通道。
        targets = ['gitee']
    result = {t: False for t in targets}
    try:
        # 云端 CI 每天 12:00 强推到 gitee，本地仓库必然落后；直接 push 会被
        # "! [rejected] HEAD -> master (fetch first)" 拒绝——2026-10-20 查明，
        # 自从 10-17 云端接手后本地就没成功推过一次，日志里那句"推送失败："后面是空的。
        # 办法：先取回远端最新，再把 HEAD 软挪到它上面（工作区改动原样保留），
        # 之后的提交就是能快进的，push 不再被拒。
        if not only and not os.environ.get('GITHUB_ACTIONS'):
            f = subprocess.run(['git', '-C', BASE_DIR, 'fetch', 'gitee', 'master'],
                               capture_output=True, text=True, timeout=120)
            if f.returncode == 0:
                s = subprocess.run(['git', '-C', BASE_DIR, 'reset', '--soft', 'FETCH_HEAD'],
                                   capture_output=True, text=True, timeout=60)
                if s.returncode == 0:
                    log("  已对齐 gitee 最新提交（soft reset），本地改动保留")
                else:
                    log("  ⚠️ 对齐远端失败：%s" % (s.stderr or '').strip()[:200])
            else:
                log("  ⚠️ 取回 gitee 最新提交失败：%s" % (f.stderr or '').strip()[:200])
        # live_light.m3u 等文件可能尚未生成，排除不存在的路径，避免 git add 整体失败
        # 脚本自身也一起提交：云端 CI 每天用仓库里的旧代码跑完再强推 Gitee，
        # 不把本机的 update_sources.py 带上，本机的改动第二天就会被覆盖回去。
        addable = [p for p in ('update_sources.py', 'config', 'config_kids', 'status.json', 'remotes.json',
                               'blocklist.json', 'known_sources.json',
                               '.github/workflows/update.yml',
                               'mirror_paste.json',
                               'live_list.m3u', 'live_light.m3u', 'live_source.json', 'live_full.json')
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
            if r.returncode != 0:
                detail0 = ((r.stderr or '') + ' ' + (r.stdout or ''))
                # GitHub 那条分支现在可能被本机的 API 提交改写过，普通 push 会因
                # non-fast-forward 被拒。CI 侧以 Gitee 为准，这里直接强推，
                # 保证 GitHub 上的文件内容与 Gitee 一致（内容才是电视端要的）。
                if remote == 'origin' and ('rejected' in detail0 or 'non-fast-forward' in detail0):
                    log("  %s 分叉，改为强推" % remote)
                    r = subprocess.run(['git', '-C', BASE_DIR, 'push', remote, '+HEAD:master'],
                                       capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                result[remote] = True
                log("已同步到远端 %s" % remote)
            else:
                # 以前只打 stderr，Git 把"被拒/fetch first"写在 stdout 里，
                # 于是日志里只剩一句"推送失败："后面空着，排查时等于没有信息。
                detail = ((r.stderr or '') + ' ' + (r.stdout or '')).strip().replace('\n', ' ')
                log("  推送 %s 失败(rc=%d)：%s" % (remote, r.returncode, detail[:400]))
        # 本机模式：GitHub 走 API。放最后是因为它依赖上面刚 commit 好的工作区内容。
        if not in_ci and not only:
            msg = 'auto update %s' % datetime.date.today().isoformat()
            if push_github_via_api(addable, msg):
                result['github_api'] = True
                log("已同步到远端 GitHub（API 通道）")
            else:
                result['github_api'] = False
                log("  ⚠️ GitHub API 通道失败：GitHub 侧今天不会更新，m3u 直链会继续用旧内容")
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


# 境外直播流测速门槛：达不到就是"点开转圈半天再失败"，收录了也没意义
# 90KB/s ≈ 720kbps，低于这个值连标清直播都会持续缓冲
OVERSEAS_MIN_RATE = 90 * 1024


def _probe_stream(url, timeout=5, min_rate=OVERSEAS_MIN_RATE):
    """境外直播流体检：返回实测速率（字节/秒），0 表示不可用。

    只看"能不能连上"不够——2026-10-17 用户反馈"国外台转半天圈还是不行"，
    正是那种返回 200 却拖不动的流：连接建立了，分片迟迟不来。
    所以这里必须真的下载数据并计时；HLS 的 playlist 本身只有几 KB，
    要顺着它取第一个分片，测出来的才是真实播放速率。
    """
    headers = {'User-Agent': 'okhttp/3.12.1', 'Accept-Encoding': 'identity'}
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def grab(target, want, head_max):
        got, head, t0 = 0, b'', time.time()
        req = urllib.request.Request(target, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            if resp.status != 200:
                return 0, 0.0, b''
            while got < want:
                chunk = resp.read(min(48 * 1024, want - got))
                if not chunk:
                    break
                if not head:
                    head = chunk[:head_max]
                got += len(chunk)
                if time.time() - t0 >= timeout:
                    break
        return got, max(time.time() - t0, 0.05), head

    try:
        got, dt, head = grab(url, 256 * 1024, 32 * 1024)
        if not got:
            return 0.0
        if head.lstrip()[:7] != b'#EXTM3U':
            rate = got / dt
            return rate if rate >= min_rate else 0.0
        # HLS：顺着 playlist 取第一个分片，最多跟两层
        target = url
        text = head.decode('utf-8', 'ignore')
        for _ in range(3):
            seg = ''
            for line in text.splitlines():
                s = line.strip()
                if s and not s.startswith('#'):
                    seg = s
                    break
            if not seg:
                return 0.0
            target = urllib.parse.urljoin(target, seg)
            got, dt, head = grab(target, 256 * 1024, 800)
            if not got:
                return 0.0
            if head.lstrip()[:7] != b'#EXTM3U':
                rate = got / dt
                return rate if rate >= min_rate else 0.0
            text = head.decode('utf-8', 'ignore')
        return 0.0
    except Exception:
        return 0.0


def prune_dead_overseas(text, timeout=5, workers=24):
    """境外频道体检：连得上但拖不动的流一并剔除，别让电视点进去干等

    只动境外分组，国内频道一条不碰。保留的按实测速率从快到慢排序——
    用户点第一个就是最流畅的台，不用逐个试错。
    若整体可用率低于 10%，判定本机网络异常，保留原样不动
    （宁可留着让电视自己试，也不能因为一次断网把好频道全删了）。
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
    rates = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for (idx, url), r in zip(targets, ex.map(lambda t: _probe_stream(t[1], timeout), targets)):
            rates[idx] = r
    live_cnt = sum(1 for v in rates.values() if v > 0)
    ratio = live_cnt / float(len(targets))
    if ratio < 0.1:
        # 不剔除是对的（一次断网不该把好频道全删），但必须留下痕迹：
        # 让 status.json 里能看出"今天这次体检没生效"，而不是误当成通过。
        LIVE_HEALTH['overseas'] = "境外体检未生效(仅%.0f%%可用,疑本机网络异常,已保留原样)" % (ratio * 100)
        log("境外频道体检可用率仅 %.0f%%，疑似本机网络异常，本次不剔除" % (ratio * 100))
        return text
    LIVE_HEALTH['overseas'] = "境外频道 %.0f%% 可播(%d/%d)" % (ratio * 100, live_cnt, len(targets))
    normal, buckets = [], {}
    for idx, (extinf, middle, url) in enumerate(blocks):
        m = re.search(r'group-title="([^"]*)"', extinf)
        g = m.group(1) if m else ''
        if g in OVERSEAS_GROUPS:
            r = rates.get(idx, 0)
            if r <= 0:
                continue
            buckets.setdefault(g, []).append((r, extinf, middle, url))
        else:
            normal.append((extinf, middle, url))
    out = [header]
    for extinf, middle, url in normal:
        out.append(extinf)
        out.extend(middle)
        out.append(url)
    for g, items in buckets.items():
        for _r, extinf, middle, url in sorted(items, key=lambda x: -x[0]):
            out.append(extinf)
            out.extend(middle)
            out.append(url)
    log("境外频道测速体检：%d 条流畅 / 共 %d 条，剔除卡顿与死链 %d 条"
        % (live_cnt, len(targets), len(targets) - live_cnt))
    for g, items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        log("    %s 保留 %d 条（最快 %.0f KB/s）"
            % (g, len(items), max(x[0] for x in items) / 1024))
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
        # 正常情况下 config 里本来就没有直播仓库条目（直播走 live_source.json，
        # 不塞进 config 的 urls），这里返回 True 是"无需检查"，不必每天打一行日志。
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


def record_live_status(status, ok, note=''):
    """把直播自检结果写进 status.json。

    2026-10-20 发现的问题：main() 只对 config 里的源写状态，直播自检的结果从来没落账，
    于是 status.json 里"电视直播"这条永远停在 2026-10-17，看状态文件会误以为直播三天没维护。
    这里把它补上，与点播源一样按天刷新 last_ok / fail_days。
    """
    rec = status.setdefault(LIVE_SOURCE_STATUS_KEY,
                            {"name": "电视直播", "last_ok": None, "fail_days": 0})
    rec["name"] = "电视直播"
    today = datetime.date.today().isoformat()
    # 把本轮体检摘要并进 note：即使总判定是 ok，也要能看出来"境外那部分体检没做成"。
    base = note or ("直播OK" if ok else "直播自检未通过")
    extra = LIVE_HEALTH.get('overseas')
    if extra:
        base += " | " + extra
    if ok:
        rec["last_ok"] = today
        rec["fail_days"] = 0
        rec.pop("last_fail_date", None)
        rec["status"] = "ok"
        rec["note"] = base
    else:
        # 同点播源：同一天只记一天，避免一天多跑几轮把计数刷满
        if rec.get("last_fail_date") != today:
            rec["fail_days"] = rec.get("fail_days", 0) + 1
            rec["last_fail_date"] = today
        rec["status"] = "fail"
        rec["note"] = base
    log("直播状态已记录：%s（连续失败 %d 天）" % (rec["status"], rec["fail_days"]))
    return rec


def refresh_live_source():
    """按当天实测结果重写 live_source.json，只保留真正取得到、内容最新的镜像。

    为什么必须每天重写：这些地址全挂在第三方 CDN / 加速代理上，谁被墙、谁缓存旧列表每天在变。
    2026-10-20 实测：jsdmirror 只有 972 条（缓存偏旧），onmicrosoft 单次 8.9 秒，
    gitmirror 与 statically 直接连不上——靠手工维护这张表，就是等电视端打不开才被发现。

    为什么备选里没有 Gitee：Gitee 会对含 IPTV 地址的 m3u 做内容审核，命中后返回
    451 "The content may contain violation information"。2026-10-20 17:56 我一度测到
    200（756 条、0.73s）以为放行了，18:02 复测又回到 451——那是审核还没跑完的窗口期，
    不是政策变化。所以 Gitee 只承载 config / status.json / live_source.json 这类 JSON，
    直播列表只能放 GitHub 系，其余冗余靠下面的备选托管（见 publish_independent）。
    """
    local_cnt = 0
    light_path = os.path.join(BASE_DIR, LIVE_LIGHT_PATH)
    if os.path.exists(light_path):
        try:
            with open(light_path, 'r', encoding='utf-8', errors='ignore') as f:
                local_cnt = len(parse_m3u_blocks(f.read()))
        except Exception:
            local_cnt = 0

    cands = []
    for host, tpl in LIVE_FEED_MIRRORS:
        url = tpl % LIVE_LIGHT_PATH
        t0 = time.time()
        try:
            text = fetch_retry(url, limit=LIVE_DOWNLOAD_LIMIT, attempts=2, wait=1)
        except Exception as e:
            log("  镜像不可用(%s)：%s" % (host, str(e)[:40]))
            continue
        cost = time.time() - t0
        if not (text and text.lstrip().startswith('#EXTM3U')):
            log("  镜像内容异常(%s)" % host)
            continue
        cnt = len(parse_m3u_blocks(text))
        if cnt < 500:
            log("  镜像条目过少(%s)：%d 条" % (host, cnt))
            continue
        stale = bool(local_cnt) and cnt < int(local_cnt * 0.98)
        log("  镜像可用 %s：%d 条，%.1fs%s" % (host, cnt, cost, "（缓存偏旧）" if stale else ""))
        cands.append({'host': host, 'url': url, 'cnt': cnt, 'cost': cost, 'stale': stale})

    if not cands:
        log("⚠️ 没有任何可用镜像，保留原 live_source.json 不动")
        return False

    # 不按延迟重排：声明顺序就是电视端实测出来的优先级（jsDelivr 主线路在前），
    # 只把当天取不到的剔除掉。留 5 条：三条 jsDelivr + ghfast（直读最新）+ Pages（独立域名），
    # 再加 publish_independent() 追加的独立托管，一共 6 条，覆盖"CDN 被墙 / 仓库被删"两类事故。
    cands = cands[:5]
    lives = []
    for i, c in enumerate(cands):
        lives.append({
            "name": "央视卫视直播" if i == 0 else "央视卫视直播(备用%d)" % i,
            "type": 0,
            "playerType": 2,
            "url": c['url'],
        })
    data = {"name": "电视直播", "sites": [], "parses": [], "lives": lives}

    old = None
    if os.path.exists(LIVE_SOURCE_PATH):
        try:
            with open(LIVE_SOURCE_PATH, 'r', encoding='utf-8') as f:
                old = f.read()
        except Exception:
            old = None
    new_text = json.dumps(data, ensure_ascii=False, indent=2)
    if old is not None and old.strip() == new_text.strip():
        log("直播直链列表无变化（%d 条线路）" % len(lives))
        return False
    with open(LIVE_SOURCE_PATH, 'w', encoding='utf-8') as f:
        f.write(new_text)
    log("直播直链列表已更新：%d 条线路（主线路 %s）" % (len(lives), cands[0]['host']))
    return True


def align_with_gitee():
    """云端 CI 专用：跑之前先把自己对齐到 Gitee 的最新提交。

    为什么必须做：workflow 最后一步是 `git push --force` 到 Gitee。GitHub 仓库的代码
    只要比 Gitee 落后一天，CI 就会拿旧代码跑一遍、再用 --force 把 Gitee 上的新代码覆盖回去，
    本机的改动第二天就消失了（2026-10-20 差点发生）。而本机到 github.com 的 push 是间歇性的，
    做不到"两边永远同步"，所以把"以 Gitee 为准"写进代码，不依赖 workflow 文件有没有被改过。
    """
    if not os.environ.get('GITHUB_ACTIONS'):
        return
    try:
        f = subprocess.run(['git', '-C', BASE_DIR, 'fetch', 'gitee', 'master'],
                           capture_output=True, text=True, timeout=120)
        if f.returncode != 0:
            log("⚠️ CI 对齐 Gitee 失败，仍按当前检出运行：%s" % (f.stderr or '').strip()[:160])
            return
        head = subprocess.run(['git', '-C', BASE_DIR, 'rev-parse', 'HEAD'],
                              capture_output=True, text=True, timeout=30).stdout.strip()
        remote = subprocess.run(['git', '-C', BASE_DIR, 'rev-parse', 'FETCH_HEAD'],
                                capture_output=True, text=True, timeout=30).stdout.strip()
        if head and head == remote:
            log("CI：当前代码已与 Gitee 一致（%s）" % head[:8])
            return
        r = subprocess.run(['git', '-C', BASE_DIR, 'reset', '--hard', 'FETCH_HEAD'],
                           capture_output=True, text=True, timeout=60)
        log("CI：已对齐到 Gitee 最新提交 %s（%s）"
            % (remote[:8], '成功' if r.returncode == 0 else '失败'))
    except Exception as e:
        log("⚠️ CI 对齐异常：%s" % e)


def upload_paste_rs(path, timeout=120):
    """把文件原样传到 paste.rs，返回可直读的 URL；失败返回 None。

    为什么要这一步：live_source.json 里那几条对外直链，本质全部回源到同一个 GitHub 仓库，
    仓库一旦被投诉下架，几条线路会一起死。Gitee 承载不了 m3u（内容审核直接 451），
    所以必须找一家跟 GitHub 毫无关系的托管。paste.rs 是 2026-10-20 实测唯一可用的匿名直读服务：
    POST 原文即返回 URL，GET 回来就是 text/plain 原文、不跳转、不做登录墙
    （实测 169704 字节 / 756 条 / HTTP 200，连读三次一致）。
    代价是慢——国内拉 169KB 要 11 秒，所以它只做"GitHub 没了也能看"的兜底，不进主线路。
    """
    try:
        with open(path, 'rb') as f:
            body = f.read()
        req = urllib.request.Request(
            PASTE_RS, data=body, method='POST',
            headers={'User-Agent': 'okhttp/3.12.1',
                     'Content-Type': 'text/plain; charset=utf-8'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            url = resp.read(256).decode('utf-8', 'ignore').strip()
        if url.startswith('https://paste.rs/'):
            return url
        log("  paste.rs 返回异常内容(%s)：%s" % (os.path.basename(path), url[:60]))
        return None
    except Exception as e:
        log("  paste.rs 上传失败(%s)：%s" % (os.path.basename(path), str(e)[:70]))
        return None


def publish_independent():
    """把 m3u 与 live_source.json 同步一份到 GitHub 之外的托管，返回 {文件名: url}。

    每天重新上传而不是复用昨天那一条：paste.rs 没有长期保留承诺，旧 URL 随时会失效；
    地址记进 mirror_paste.json，出问题时可以直接翻台账找到当天那份。
    失败不影响主流程——GitHub 那 4 条线路还在，电视端照常能看。
    """
    out = {}
    light = os.path.join(BASE_DIR, LIVE_LIGHT_PATH)
    if os.path.exists(light):
        u = upload_paste_rs(light)
        if u:
            out[LIVE_LIGHT_PATH] = u
            log("  独立托管就绪：%s -> %s" % (LIVE_LIGHT_PATH, u))
    if out.get(LIVE_LIGHT_PATH) and os.path.exists(LIVE_SOURCE_PATH):
        try:
            with open(LIVE_SOURCE_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # 每次都先剔掉旧的独立托管条目，否则一天一条、列表会被堆满
            lives = [l for l in (data.get('lives') or [])
                     if 'paste.rs' not in (l.get('url') or '')]
            lives.append({"name": "央视卫视直播(独立托管)", "type": 0,
                          "playerType": 2, "url": out[LIVE_LIGHT_PATH]})
            data['lives'] = lives
            with open(LIVE_SOURCE_PATH, 'w', encoding='utf-8') as f:
                f.write(json.dumps(data, ensure_ascii=False, indent=2))
            u2 = upload_paste_rs(LIVE_SOURCE_PATH)
            if u2:
                out[os.path.basename(LIVE_SOURCE_PATH)] = u2
                log("  独立托管就绪：%s -> %s" % (os.path.basename(LIVE_SOURCE_PATH), u2))
        except Exception as e:
            log("  ⚠️ 独立托管 live_source 失败：%s" % e)
    # config 也镜像一份：电视端点播线路的入口本身如果只挂在 GitHub/Gitee，
    # 那两个站同时抽风时连"入口"都拿不到。这份是整条链路唯一不依赖它们的一环。
    cfg_path = os.path.join(BASE_DIR, 'config')
    if os.path.exists(cfg_path):
        u3 = upload_paste_rs(cfg_path)
        if u3:
            out['config'] = u3
            log("  独立托管就绪：config -> %s" % u3)
    if out:
        try:
            with open(MIRROR_PASTE_PATH, 'w', encoding='utf-8') as f:
                f.write(json.dumps(
                    {"updated": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                     "note": "GitHub 之外的最后一条退路；拉取较慢（约 11s/169KB），仅在前四条全挂时使用",
                     "urls": out}, ensure_ascii=False, indent=2))
        except Exception as e:
            log("  ⚠️ 写 mirror_paste.json 失败：%s" % e)
    return out


def save_remotes(push_res):
    """把每个远端的推送结果落盘成 remotes.json，方便事后判断"是新列表没生成还是没推上去"。

    之前的问题：推送结果只出现在当天日志里，第二天翻日志才知道 GitHub 断了多久；
    而且 GitHub 仓库实际是公开可写的（2026-10-20 用 git ls-remote 证实），
    单看 api.github.com 返回 404 会误判成"仓库没了"。
    """
    data = {
        "checked_at": datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "remotes": {k: ("ok" if v else "fail") for k, v in (push_res or {}).items()},
        "note": "gitee=源码/配置备份（m3u 在 Gitee 会 451）；github_api=本机用 GitHub API 直写（git over HTTPS 在本机 443 全超时）",
    }
    try:
        with open(REMOTES_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log("远端台账已写入 remotes.json：%s" % data["remotes"])
    except Exception as e:
        log("⚠️ 写远端台账失败：%s" % e)
    return data


def run_live_only():
    """CI 专用入口：只做直播列表保鲜与推送，绝不碰点播源的可用性判定。

    为什么需要这条路径（2026-10-20 审计发现）：
    GitHub Actions 每天 04:19 UTC（北京 12:19）跑一次，本机任务 12:45 跑一次，
    等于两个执行端每天各判一次源可用性。但 CI 的出口在美国机房，访问国内源天然吃亏，
    它没有资格决定"电视上到底能不能播"。以前没出事，纯粹是因为本机比 CI 晚 26 分钟，
    本机的结果最后落地、把 CI 的误判覆盖掉了——这个顺序一改就会出错。

    所以 CI 侧砍掉验证职责，只保留它真正擅长且不可替代的部分：抓频道、重写直链列表、推送。
    这样即使本机某天不开机，直播列表仍有 CI 保活；config / status 里的点播源条目完全不动。
    """
    log("===== 开始直播列表保鲜（CI 模式：不参与点播源判定）=====")
    LIVE_HEALTH.clear()
    align_with_gitee()
    status = load_status()
    validate_kids()
    refresh_live()
    push_res = push_remotes()
    live_repo_ok = check_live_repo()
    live_feed_ok = check_live_feed()
    live_pub_ok = check_public_urls()
    live_ok = bool(live_repo_ok and live_feed_ok and live_pub_ok)
    record_live_status(status, live_ok,
                       note="直播OK" if live_ok else "直播自检未全通过，见 update_log.txt")
    save_status(status)
    src_changed = refresh_live_source()
    try:
        mirror = publish_independent()
        if mirror:
            src_changed = True
    except Exception as e:
        log("⚠️ 独立托管步骤异常（不影响主线）：%s" % e)
    save_remotes(push_res)
    push_res2 = push_remotes()
    log("二次推送（直链列表有变化=%s）：%s" % (src_changed, push_res2))
    gh_key = 'origin' if ('origin' in push_res2 or 'gitee' not in push_res2) else 'github_api'
    if not push_res2.get(gh_key):
        log("⚠️ GitHub 未同步：直播直链会继续用线上旧列表，链路恢复后需补推")
    log("===== 结束 =====")
    return any(push_res2.values())


def main():
    LIVE_HEALTH.clear()
    # SKIP_VERIFY=1 由 .github/workflows/update.yml 注入：CI 不做点播源验证。
    # 判定源可用性必须以国内网络为准，美国机房下不了这个结论（详见 run_live_only 注释）。
    if os.environ.get('SKIP_VERIFY') == '1':
        return run_live_only()
    log("===== 开始自动查找与验证影视源 =====")
    # 云端 CI 先对齐 Gitee 最新代码再干活，否则会用旧代码跑完再 --force 覆盖 Gitee
    align_with_gitee()
    today = datetime.date.today().isoformat()
    status = load_status()
    cfg = load_config()
    store = cfg.get("storeName", "本地聚合")

    bl = load_blocklist()
    known = load_known()

    # ---- 1. 黑名单复核：按加入日期排队，本轮只放行最老的几条 ----
    # 旧版是一张永久名单，加了就再也不看；现在到期必须重新测一次。
    due = [u for u, r in bl.items()
           if days_between(r.get("since") or today, today) >= BLACKLIST_COOLDOWN_DAYS]
    due.sort(key=lambda u: bl[u].get("since") or "")
    # --recheck-blocklist：手动一次性把到期的全测了，不用等每天 3 条慢慢排队
    force = '--recheck-blocklist' in sys.argv
    reviving = due if force else due[:MAX_BLOCKLIST_REVIVE]
    due_set = set(due)
    for u, r in bl.items():
        if u not in due_set:
            left = BLACKLIST_COOLDOWN_DAYS - days_between(r.get("since") or today, today)
            log("黑名单冷却中 %s（%d 天后复核）" % (u, max(left, 1)))
    waiting = [u for u in due if u not in set(reviving)]
    if waiting:
        shown = "、".join(bl[u].get("name") or u[:40] for u in waiting[:6])
        log("黑名单另有 %d 条冷却期已过、排队等名额：%s%s" % (
            len(waiting), shown, "……" if len(waiting) > 6 else ""))
    for u in reviving:
        log("黑名单到期复核：%s（%s）" % (u, bl[u].get("name") or "未命名"))

    # ---- 2. 组装本轮待验证清单 ----
    todo = []   # (url, name, source)：source ∈ config / candidate / revive / retired
    seen = set()
    for it in cfg.get("urls", []):
        u = it.get("url") if isinstance(it, dict) else None
        # 黑名单里的源一律交给下面的 revive 分支处理：否则万一它还被遗留在 config 里，
        # 会被当成普通源按 3 天阈值处理，冷却期永远不推进。
        if u and u not in seen and u not in bl:
            seen.add(u)
            todo.append((u, it.get("name", "未命名"), "config"))
    for name, url in CANDIDATES:
        if url not in seen and url not in bl:
            seen.add(url)
            todo.append((url, name, "candidate"))
    for url in reviving:
        if url not in seen:
            seen.add(url)
            todo.append((url, bl[url].get("name") or "黑名单复核", "revive"))
    # 历史台账里被剔除的源：低频重试，给"当时确实不行、后来又活了"的源留一条回家的路。
    #
    # 这里同时纳入 retired 和 waiting 两种，但两者含义完全不同：
    #   retired = 验不过（连续 3 天失效 / 候选两次都没通）→ 真死源，等它自己活过来
    #   waiting = 验过了、排不进 MAX_SOURCES 上限 → 活源，只是没空位（2026-10-20 拆分）
    # 旧版把 waiting 也写成 retired，导致台账里"活着的源"被记成"已死"，
    # 统计上分不清"该报故障"和"该扩上限"，日志还会说"已剔除"这种误导性的话。
    # 两者的重试节奏一致（都是低频），差别只体现在标记和统计口径上。
    retired = [u for u, r in known.items()
               if (r.get("retired") or r.get("waiting")) and u not in seen
               and days_between(r.get("last_fail") or r.get("last_full") or "1970-01-01",
                                today) >= RETIRED_RETRY_DAYS]
    retired.sort(key=lambda u: known[u].get("last_fail") or known[u].get("last_full") or "")
    for url in retired[:MAX_RETIRED_RETRY]:
        seen.add(url)
        todo.append((url, known[url].get("name") or "历史源", "retired"))

    # ---- 3. 并发验证（源多了以后串行太慢，最坏要跑十几分钟）----
    def _run_verify(items):
        if not items:
            return {}
        out = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=VERIFY_WORKERS) as ex:
            futs = {ex.submit(verify_source, url): url for url, _, _ in items}
            for fu in concurrent.futures.as_completed(futs):
                url = futs[fu]
                try:
                    out[url] = fu.result()
                except Exception as e:
                    out[url] = {"ok": False, "note": "验证异常:%s" % type(e).__name__,
                                "spot_ok": 0, "spot_total": 0,
                                "search_hit": 0, "search_tried": 0, "search_note": ""}
        return out

    log("本轮待验证 %d 个源（config %d / 候选 %d / 黑名单复核 %d / 历史重试 %d，并发 %d）" % (
        len(todo),
        sum(1 for t in todo if t[2] == "config"), sum(1 for t in todo if t[2] == "candidate"),
        sum(1 for t in todo if t[2] == "revive"), sum(1 for t in todo if t[2] == "retired"),
        VERIFY_WORKERS))
    results = _run_verify(todo)

    # ---- 3.5 候选发现：从多仓源自己推荐的子源里提名新候选，本轮顺带验证 ----
    # 放在验证之后，是因为此时 config 源的配置内容已在 fetch 缓存里，提取零下载开销。
    if '--no-discover' not in sys.argv:
        picked, disc_total = discover_candidates(cfg, known, bl, seen)
        if disc_total:
            log("候选发现：多仓源共推荐 %d 个未收录子源，本轮抽取 %d 个验证"
                % (disc_total, len(picked)))
            extra = [(u, n, "candidate") for n, u in picked]
            for u, _n, _s in extra:
                seen.add(u)
            results.update(_run_verify(extra))
            todo.extend(extra)

    # ---- 4. 按结果落账（串行，避免多线程抢着写 status）----
    keep = []  # (name, url)：候选写入 config
    added_map = {}   # url -> 通知里显示的文字（真正进 config 后才计入 added）
    for url, name, source in todo:
        r = results.get(url) or {"ok": False, "note": "未验证", "spot_ok": 0, "spot_total": 0,
                                 "search_hit": 0, "search_tried": 0, "search_note": ""}
        ok, note = r["ok"], r["note"]
        rec = status.setdefault(url, {"name": name, "last_ok": None, "fail_days": 0})
        rec["name"] = name
        k = known.setdefault(url, {"first_seen": today})
        k["name"] = name
        k["last_try"] = today   # 候选发现的轮转依据：最久没验证过的优先
        tag = ""

        if ok:
            # 站点级抽查 + 真实搜索探针：都只记录提醒，不参与可用性判定（防误伤）
            if r["spot_total"]:
                note = "%s(站点抽查%d/%d)" % (note, r["spot_ok"], r["spot_total"])
                # 只抽到 1 个站点时不下"不可达"结论——样本太薄，一条偶发超时就会误报。
                if r["spot_ok"] == 0 and r["spot_total"] >= 2:
                    note += " ⚠️站点不可达提醒"
            if r["search_tried"]:
                rec["search"] = "%d/%d" % (r["search_hit"], r["search_tried"])
                rec["search_tried"] = r["search_tried"]
                note += " 搜索%d/%d" % (r["search_hit"], r["search_tried"])
                if r["search_hit"] > 0:
                    rec["search_hit"] = r["search_hit"]
                elif r["search_tried"] >= SEARCH_PROBE_MIN:
                    rec["search_hit"] = 0
                    note += " ⚠️搜不到片"
                else:
                    # 可探测接口太少，样本不足，不给负面结论也不给负面分
                    rec.pop("search_hit", None)
                    note += "（样本不足，不判定）"
            else:
                # 本轮无法探测（内置 spider、或配置解析不了）：
                # 清掉旧分数，别让它拿历史成绩参与排序
                for _k in ("search_hit", "search", "search_tried"):
                    rec.pop(_k, None)
            rec["fail_days"] = 0
            rec.pop("last_fail_date", None)   # 恢复后重置计数窗口，下次失败算新的一天
            rec["last_ok"] = today
            rec["status"] = "ok"
            rec["note"] = note
            keep.append((name, url))
            k["last_ok"] = today
            k["retired"] = False
            k.pop("waiting", None)      # 本轮排进 config 了，不再是"等位"
            k.pop("probe_fail", None)   # 验证通过就清掉历史失败次数，重新给满机会
            if source == "revive":
                bl.pop(url, None)          # 复核通过，正式出狱
                added_map[url] = "%s(黑名单复核通过)" % name
                tag = "黑名单复核通过 | "
            elif source == "retired":
                added_map[url] = "%s(恢复)" % name
                tag = "历史源验证通过 | "
            elif source == "candidate":
                added_map[url] = name
        else:
            # fail_days 的语义是"连续失败的天数"，不是"失败的次数"。
            # 旧版每跑一轮就 +1，同一天跑多轮会虚增：CI（北京 12:19）与本机（12:45）
            # 各跑一次，一个取不到的源一天就 +2；手动试跑再跑几次能直接冲破阈值。
            # 改为同一天只记一次，重复失败只刷新 last_fail_date。
            if rec.get("last_fail_date") != today:
                rec["fail_days"] = rec.get("fail_days", 0) + 1
                rec["last_fail_date"] = today
            rec["status"] = "fail"
            rec["note"] = note
            k["last_fail"] = today
            if source == "revive":
                # 复核没过：冷却期从今天重新算，而不是沿用旧日期（否则每天都会来试一次）
                bl[url]["since"] = today
                bl[url]["last_retry"] = today
                bl[url]["retries"] = bl[url].get("retries", 0) + 1
                tag = "❌ 复核未通过，继续冷却 %d 天 | " % BLACKLIST_COOLDOWN_DAYS
            elif rec.get("last_ok") is None and source in ("candidate", "retired"):
                # 新发现的候选一次失败不代表永久死掉（很多源是间歇性抽风），
                # 给两次机会：第一次失败继续在发现轮转里排队，第二次才转入 7 天低频重试。
                k["probe_fail"] = k.get("probe_fail", 0) + 1
                if k["probe_fail"] >= MAX_CANDIDATE_FAILS:
                    k["retired"] = True
                    tag = "从未验证通过（第%d次，转入低频重试）| " % k["probe_fail"]
                else:
                    tag = "从未验证通过（第%d次，继续轮转）| " % k["probe_fail"]
            elif rec["fail_days"] < MAX_FAIL_DAYS:
                keep.append((name, url))   # 曾可用，未达阈值先保留
            else:
                tag = "连续%d天失效，已剔除（进历史台账，%d 天后重试）| " % (
                    MAX_FAIL_DAYS, RETIRED_RETRY_DAYS)
                k["retired"] = True
                k.pop("waiting", None)   # 等位的源真失效了，就升级成死源，别两个标记并存
        log("  %s%s -> %s" % (tag, name, note))

    # 写回 config：先按新鲜度排序，再按数量上限截断。
    # 旧版按"config 原有顺序 + 候选追加"截断，超限时先砍候选新源；
    # 现在改成 last_ok 越近越靠前（同一天时 sort 稳定、保持原顺序），
    # 同一天再比"搜索是否真能搜出片"——保证"昨天还能用、今天也搜得到"的源
    # 不会因为排在后排被挤掉。搜索探针只影响排序，本身不参与剔除。
    def _fresh(item):
        r = status.get(item[1], {})
        hit = r.get("search_hit")
        tried = r.get("search_tried") or 0
        # 2=真能搜到片，1=无法探测/样本不足，0=测了足够多的接口但一条都搜不到
        if hit is None:
            rank = 1
        elif hit > 0:
            rank = 2
        elif tried >= SEARCH_PROBE_MIN:
            rank = 0
        else:
            rank = 1
        return (r.get("last_ok") or "", rank)

    keep.sort(key=_fresh, reverse=True)
    new_urls = []
    seen = set()
    dropped = []
    for name, url in keep:
        if url in seen:
            continue
        seen.add(url)
        if len(new_urls) >= MAX_SOURCES:
            dropped.append(name)
            kk = known.setdefault(url, {"first_seen": today})
            if (results.get(url) or {}).get("ok"):
                # 验过了、只是排不进上限——这是活源等位，不是死源。
                # 旧版这里无脑写 retired=True + last_fail=today，于是"今天验证通过"的源
                # 在台账里被记成"已失效"，日志还打"已剔除"，5 条源的记录全是这么来的。
                kk.pop("retired", None)
                kk["waiting"] = True
                kk["last_ok"] = today
                kk["last_full"] = today   # 没位置的那天，作为低频重试的计时起点
            else:
                # 本轮没验过就不改判定：既不升级成死源，也不假装活着
                kk["last_fail"] = today
            continue
        new_urls.append({"name": name, "url": url})
    if dropped:
        log("⚠️ 可用源超过 %d 条上限，本轮搁置：%s（验过的标记为等位，%d 天后自动重试）"
            % (MAX_SOURCES, "、".join(dropped), RETIRED_RETRY_DAYS))

    # 新增/移出按 config 的实际差异算，而不是按验证结果算——
    # 否则"验证通过但被数量上限挤掉"的源会出现在"新增源"通知里，自相矛盾。
    new_set = set(it["url"] for it in new_urls)
    added = [added_map[u] for u in new_set if u in added_map]
    removed = [it.get("name") or it.get("url") for it in cfg.get("urls", [])
               if isinstance(it, dict) and it.get("url") not in new_set]

    # 清理"从未成功过"的僵尸状态记录。这类源只有两种：
    #   a) 候选自动发现抽到、但没通过验证的子源（每轮 6 个，109 个子源轮下来能堆上百条）
    #   b) 早期失败且再没恢复的源
    # 它们的完整台账（probe_fail / last_try / retired）都在 known_sources.json 里，
    # status.json 只该保留"真正在用的源"，否则这个文件会从 20 条膨胀到 130+ 条。
    # 注意：能进 config 的源必然有 last_ok（落账时 last_ok is None 的不予保留），
    # 所以这里不会误删任何线上在用的源。
    for u in [u for u, r in status.items() if r.get("last_ok") is None]:
        status.pop(u, None)

    # 黑名单源的状态记录每次复核都会重建，留着只会让 status.json 堆一堆僵尸条目。
    # 注意用运行时的 bl 而不是旧的硬编码列表：本轮刚复核放行的源已经不在 bl 里，不会被误删。
    for u in list(status.keys()):
        if u in bl:
            status.pop(u, None)

    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump({"storeName": store, "urls": new_urls}, f, ensure_ascii=False, indent=2)
    save_status(status)
    _save_json(BLOCKLIST_PATH, bl)
    _save_json(KNOWN_PATH, known)
    log("台账已更新：黑名单 %d 条（%d 条待复核），历史源 %d 条（其中已失效 %d 条 / 等位 %d 条）" % (
        len(bl),
        len([u for u, r in bl.items()
             if days_between(r.get("since") or today, today) >= BLACKLIST_COOLDOWN_DAYS]),
        len(known),
        len([u for u, r in known.items() if r.get("retired")]),
        len([u for u, r in known.items() if r.get("waiting")])))

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
            msg += "移出配置：%s。" % "、".join(removed)
        msg += "当前配置 %d 个源。" % len(new_urls)
        notify_change(msg)
    elif ok_count >= 2 and len([it for it in new_urls if status.get(it["url"], {}).get("last_ok") == today]) < 1:
        notify_change("今日源检测异常，请查看 update_log.txt")
    # 儿童聚合验证 + 直播列表保鲜
    validate_kids()
    skip_live = '--skip-live' in sys.argv   # 只关心点播源时跳过，省掉 5 分钟测速
    if skip_live:
        log("--skip-live：跳过直播列表刷新与直链自检")
        push_res = push_remotes()
        # --no-push 是试跑：push_remotes 会直接返回全 True，写进台账就成了"什么都没推
        # 却说两个远端都 ok"，留着会误导以后排查。试跑时保持台账原样。
        if '--no-push' not in sys.argv:
            save_remotes(push_res)
        log("===== 结束 =====")
        return any(push_res.values())
    refresh_live()
    push_res = push_remotes()
    push_ok = any(push_res.values())
    # 本机模式 targets 只有 gitee，GitHub 走的是 github_api 键；CI 模式才是 origin。
    # 以前这里固定查 'origin'，本机每天都会误报"GitHub 未同步"（2026-10-20 实测，
    # 19:45:49 那条日志紧跟在"已同步到远端 GitHub（API 通道）"之后，自相矛盾）。
    gh_key = 'origin' if ('origin' in push_res or 'gitee' not in push_res) else 'github_api'
    if not push_res.get(gh_key):
        log("⚠️ GitHub 未同步：直播直链会继续用线上旧列表，链路恢复后需补推")
    # 推送之后再做对外地址自检，确认电视端真的能下载到直播列表
    live_repo_ok = check_live_repo()
    live_feed_ok = check_live_feed()
    live_pub_ok = check_public_urls()
    live_ok = bool(live_repo_ok and live_feed_ok and live_pub_ok)
    # 直播自检结果必须落账，否则 status.json 里"电视直播"会一直停在旧日期（2026-10-20 的坑）
    record_live_status(status, live_ok,
                       note="直播OK" if live_ok else "直播自检未全通过，见 update_log.txt")
    save_status(status)
    # 再按当天实测重写对外直链列表；有变化就补一次推送，让电视端当天就能拿到新线路
    src_changed = refresh_live_source()
    # GitHub 之外的兜底托管：四条直链其实同源，仓库出事就一起死；这份独立副本只在前四条
    # 全挂时才用得上，慢但跟 GitHub 无关。失败不影响主流程。
    try:
        mirror = publish_independent()
        if mirror:
            src_changed = True
    except Exception as e:
        log("⚠️ 独立托管步骤异常（不影响主线）：%s" % e)
    if '--no-push' not in sys.argv:   # 试跑不写台账，理由同上面的 --skip-live 分支
        save_remotes(push_res)
    # 无论直链列表有没有变，都要再推一次：remotes.json 是这一步才写出来的，
    # 不推的话它永远停在本地未提交状态，第二天被 soft reset 带过去才补上。
    push_res2 = push_remotes()
    log("二次推送（直链列表有变化=%s）：%s" % (src_changed, push_res2))
    if not any(push_res2.values()):
        log("⚠️ 二次推送失败：电视端仍会读到旧的直播直链列表")
    # 提交历史每天稳定增长 1-2 条：不影响播放，只影响仓库观感与体积。
    # 不说的话，半年后又是几百条"auto update"，到时候没人记得还能收拾。
    try:
        _g = subprocess.run(['git', '-C', BASE_DIR, 'rev-list', '--count', 'HEAD'],
                            capture_output=True, text=True, timeout=20)
        _n = int((_g.stdout or '0').strip() or 0)
        if _n >= 200:
            log("ℹ️ 提交历史已 %d 条，可运行 squash_history.py 折叠成一条基线（不影响播放）" % _n)
    except Exception:
        pass
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
