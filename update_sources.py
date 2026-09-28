# -*- coding: utf-8 -*-
"""
影视仓聚合接口 - 每日自动查找与验证影视源
每天 12:00 由 Windows 计划任务调用，自动完成：
  1. 验证 config 中每个源的可用性（HTTP 200 + 合法 TVBox JSON 结构）
  2. 连续 3 天失效的源自动剔除
  3. 从候选源池自动验证可用新源并补充进 config
  4. 更新 status.json 记录每个源的检测状态
"""
import json
import os
import urllib.request
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config')
STATUS_PATH = os.path.join(BASE_DIR, 'status.json')
LOG_PATH = os.path.join(BASE_DIR, 'update_log.txt')

MAX_FAIL_DAYS = 3            # 连续失败天数阈值，达到后自动剔除
MAX_SOURCES = 15             # config 中最多保留的源数量
DOWNLOAD_LIMIT = 512 * 1024  # 单源下载上限 512KB

# 候选源池：每次运行都会重新验证，可用且不在 config 的会自动补充
CANDIDATES = [
    ("YSC全量源", "https://jihulab.com/mengzhu2/ysc/-/raw/main/YSC.json"),
    ("小马源", "https://szyyds.cn/tv/x.json"),
    ("HGYX影视", "https://api.hgyx.vip/hgyx.json"),
    ("聚玩盒子4K", "http://xhztv.top/4k.json"),
    ("拾光多仓", "http://xmbjm.fh4u.org/dc.txt"),
    ("挺好分享", "http://ztha.top/TVBox/GYCK.json"),
    ("月光宝盒", "https://jihulab.com/ygbh1/box/-/raw/main/dcang/dc.json"),
    ("小盒子多仓", "http://xhztv.top/dc"),
    ("小盒子单仓", "http://xhztv.top/xhz"),
    ("猫技多仓", "https://agit.ai/ddx/TVBox/raw/branch/master/d.json"),
    ("影视仓多仓", "https://gitlab.com/ygbh1/666/-/raw/main/dcang/ygbh.json"),
    ("运输车源", "https://tvkj.top/DC/ysc/tvkj/svip.json"),
    ("香雅情源", "https://github.moeyy.xyz/https://raw.githubusercontent.com/xyq254245/xyqonlinerule/main/XYQTVBox.json"),
    ("饭太硬源", "http://www.饭太硬.com/tv"),
    ("嗷呜源", "http://itv666.cc/aowu/config.webp"),
    ("VOX源", "http://rihou.cc:88/demo.php"),
    ("凯速源", "https://6800.kstore.vip/fish.json"),
    ("pandown源", "http://pandown.pro/tvbox/tvbox.json"),
    ("俊哥源", "http://home.jundie.top:81/top98.json"),
    ("4K云盘源", "https://9xi4o.tk/0725.json"),
    ("毒盒多仓", "https://tv.youdu.fan:666"),
    ("运输车辆", "https://weixine.net/api.json"),
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


def fetch(url):
    """下载 URL 内容，返回文本；失败返回 None（兼容 gzip、中文域名）"""
    try:
        url = idna_url(url)
        req = urllib.request.Request(url, headers={
            'User-Agent': 'okhttp/3.12.1',
            'Accept-Encoding': 'identity',
        })
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = resp.read(DOWNLOAD_LIMIT + 1)
            if len(body) > DOWNLOAD_LIMIT:
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
    return False, "不是TVBox配置结构"


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
    for url, (name, is_existing) in existing.items():
        log("验证 %s (%s)" % (name, url))
        content = fetch(url)
        ok, note = validate(content)
        rec = status.setdefault(url, {"name": name, "last_ok": None, "fail_days": 0})
        rec["name"] = name
        if ok:
            rec["fail_days"] = 0
            rec["last_ok"] = today
            rec["status"] = "ok"
            rec["note"] = note
            keep.append((name, url))
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

    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump({"storeName": store, "urls": new_urls}, f, ensure_ascii=False, indent=2)
    save_status(status)

    ok_count = len([u for u, r in status.items() if r.get("status") == "ok"])
    log("更新完成：config 保留 %d 个源，其中当前可用 %d 个" % (len(new_urls), ok_count))
    log("===== 结束 =====")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("运行异常: %s" % e)
