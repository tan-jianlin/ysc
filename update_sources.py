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
import subprocess
import urllib.request
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


def push_to_gitee():
    """将更新后的 config / config_kids / status / 直播列表 推送到远端，让电视不依赖电脑也能读取最新配置。

    远端由环境变量 GIT_PUSH_REMOTE 指定，默认 origin。
    注意：在 GitHub Actions 里 origin 指向 GitHub 本身，需要 workflow 授予
    contents: write 权限，否则 git push 会返回 403（原先失败被静默吞掉）。
    推送到 Gitee 由 workflow 的独立步骤负责，不由本函数承担。
    """
    remote = os.environ.get('GIT_PUSH_REMOTE', 'origin')
    try:
        steps = [
            ['git', '-C', BASE_DIR, 'add', 'config', 'config_kids', 'status.json', 'live_list.m3u', 'live_source.json'],
            ['git', '-C', BASE_DIR, 'commit', '-m', 'auto update %s' % datetime.date.today().isoformat()],
            # 用 HEAD:master 而不是 master：后者推的是本地 master 分支，
            # 一旦本地检出在 main，就会推上去旧内容却返回成功（静默假成功）。
            ['git', '-C', BASE_DIR, 'push', remote, 'HEAD:master'],
        ]
        for i, c in enumerate(steps):
            r = subprocess.run(c, capture_output=True, text=True, timeout=90)
            label = ' '.join(c[3:])
            log("  git %s -> 退出码 %d" % (label, r.returncode))
            if r.returncode == 0:
                continue
            if i == 0:
                log("  暂存失败：%s" % (r.stderr or '').strip()[:300])
                return False
            if i == 1:
                if 'nothing to commit' in (r.stdout or '') or 'nothing added' in (r.stdout or ''):
                    log("  无改动可提交（正常）")
                else:
                    log("  提交失败：%s" % (r.stderr or '').strip()[:300])
                    return False
            if i == 2:
                log("  推送 %s 失败：%s" % (remote, (r.stderr or '').strip()[:400]))
                return False
        log("已同步到远端 %s" % remote)
        return True
    except Exception as e:
        log("推送远端失败（网络或登录态问题，本地配置已更新）: %s" % e)
        return False


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


def refresh_live():
    """直播保鲜：拉取最新直播列表（Guovin），成功则更新本地列表，随下次 push 同步线上"""
    live_path = os.path.join(BASE_DIR, 'live_list.m3u')
    src = "https://cdn.jsdelivr.net/gh/Guovin/iptv-api@gd/output/result.m3u"
    content = fetch(src, limit=LIVE_DOWNLOAD_LIMIT)
    if content and content.lstrip().startswith('#EXTM3U') and len(content) > 5000:
        with open(live_path, 'w', encoding='utf-8') as f:
            f.write(content)
        log("直播列表已保鲜更新 (%d 字节)" % len(content.encode('utf-8')))
        return True
    log("直播列表更新失败，保留现有列表")
    return False


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
    push_to_gitee()
    log("===== 结束 =====")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("运行异常: %s" % e)
