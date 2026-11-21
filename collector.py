"""Read JD pages in a dedicated visible browser. Never replay or alter signed requests."""
import hashlib
import json
import math
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from storage import now
from incremental import Frontier


class Cancelled(Exception):
    pass


class NeedsAttention(Exception):
    pass


def sku_from(value):
    value = str(value).strip()
    if re.fullmatch(r"\d{5,20}", value):
        return value
    match = re.search(r"https?://item\.(?:m\.)?jd\.com/(\d{5,20})\.html", value, re.I)
    return match.group(1) if match else None


def parse_targets(value):
    return list(dict.fromkeys(filter(None, (sku_from(x) for x in re.split(r"[\s,，;；]+", value)))))


def parse_payload(text):
    text = text.strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        match = re.match(r"^[\w.$]+\s*\(([\s\S]*)\)\s*;?$", text)
        if match:
            try:
                return json.loads(match.group(1))
            except ValueError:
                pass
    return None


def parse_cookies(text):
    """Accept a local Cookie header or a browser-exported JSON array. Never log values."""
    text = str(text).strip()
    if not text or len(text) > 90000:
        raise ValueError("请在本地输入有效的京东 Cookie，最大 90 KB")
    try:
        raw = json.loads(text)
    except ValueError:
        raw = []
        text = re.sub(r"^cookie\s*:\s*", "", text, flags=re.I)
        for piece in text.split(";"):
            if "=" not in piece:
                continue
            name, value = piece.strip().split("=", 1)
            if name.lower() in ["path", "domain", "expires", "max-age", "samesite"]:
                continue
            raw.append({"name": name, "value": value, "domain": ".jd.com", "path": "/",
                        "secure": True, "httpOnly": name in ["thor", "pt_key"]})
    if isinstance(raw, dict):
        raw = raw.get("cookies", [])
    if not isinstance(raw, list):
        raise ValueError("Cookie JSON 应为数组，或包含 cookies 数组的对象")
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain", ".jd.com")).lower()
        if domain.lstrip(".") != "jd.com" and not domain.lstrip(".").endswith(".jd.com"):
            continue
        name, value = str(item.get("name", "")), str(item.get("value", ""))
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+", name) or not value or any(c in value for c in "\r\n"):
            continue
        cookie = {"name": name, "value": value, "domain": domain,
                  "path": str(item.get("path", "/")), "secure": bool(item.get("secure", True)),
                  "httpOnly": bool(item.get("httpOnly", name in ["thor", "pt_key"]))}
        same_site = str(item.get("sameSite", "Lax")).capitalize()
        cookie["sameSite"] = same_site if same_site in ["Strict", "Lax", "None"] else "Lax"
        expires = item.get("expires", item.get("expirationDate"))
        if expires is not None:
            try:
                expires = float(expires)
                if math.isfinite(expires) and expires > 0:
                    if expires <= time.time():
                        continue
                    cookie["expires"] = expires
            except (ValueError, TypeError):
                pass
        result.append(cookie)
    if not any(c["name"] in ["thor", "pt_key"] for c in result):
        raise ValueError("未找到京东会话 Cookie（thor / pt_key）。请复制已登录京东请求的完整 Cookie，或使用扫码登录")
    return result


def media_urls(value):
    result = []
    if isinstance(value, str):
        if value.startswith("//"):
            value = "https:" + value
        if value.startswith(("https://", "http://")):
            result.append(value)
    elif isinstance(value, list):
        for item in value:
            result.extend(media_urls(item))
    elif isinstance(value, dict):
        for key in ["url", "picURL", "picUrl", "imgUrl", "imageUrl", "mainUrl", "videoUrl", "playUrl"]:
            if key in value:
                result.extend(media_urls(value[key]))
    return list(dict.fromkeys(result))


def text_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (text_value(v) for v in value)))
    if isinstance(value, dict):
        return str(value.get("commentData") or value.get("content") or value.get("afterCommentData") or "")
    return ""


def normalize_review(raw):
    c = raw.get("commentInfo", raw)
    content = c.get("commentData", c.get("content", ""))
    if not isinstance(content, str):
        return None
    score = c.get("commentScore", c.get("score", c.get("star")))
    try:
        score = int(score) if float(score).is_integer() and 1 <= float(score) <= 5 else None
    except (ValueError, TypeError):
        score = None
    variant = c.get("wareAttribute") or c.get("productSpecifications") or ""
    if not isinstance(variant, str):
        variant = json.dumps(variant, ensure_ascii=False)
    variant = " ".join(str(x) for x in [variant, c.get("productColor"), c.get("productSize")] if x)
    return {
        "platform_id": str(c.get("commentId") or c.get("id") or c.get("guid") or c.get("cid") or ""),
        "content": content.strip(), "rating": score,
        "review_date": str(c.get("commentDate") or c.get("creationTime") or c.get("creationDate") or ""),
        "variant": variant,
        "append_text": text_value(c.get("afterComment") or c.get("appendComment") or c.get("afterUserComment") or c.get("afterUserCommentVo") or c.get("afterComments")),
        "reference_sku": str(c.get("referenceId") or c.get("skuId") or ""),
        "images": media_urls(c.get("pictureInfoList") or c.get("images") or c.get("pictures") or c.get("picList")),
        "videos": media_urls(c.get("videos") or c.get("videoInfoList")), "source": "network",
    }


def extract_reviews(payload):
    result = []
    seen = set()
    def walk(node, depth=0):
        if depth > 16:
            return
        if isinstance(node, list):
            for child in node:
                walk(child, depth + 1)
        elif isinstance(node, dict):
            candidate = node.get("commentInfo")
            if candidate is None and ("commentData" in node or ("content" in node and any(k in node for k in ["creationTime", "score", "guid", "referenceId"]))):
                candidate = node
            if isinstance(candidate, dict):
                review = normalize_review(candidate)
                if review is not None:
                    fingerprint = json.dumps(review, ensure_ascii=False, sort_keys=True)
                    if fingerprint not in seen:
                        seen.add(fingerprint)
                        result.append(review)
            for key, child in node.items():
                if key not in ["commentInfo", "afterComment", "appendComment", "afterUserComment", "afterUserCommentVo", "afterComments", "replies"] and isinstance(child, (list, dict)):
                    walk(child, depth + 1)
    walk(payload)
    return result


PRODUCTS_JS = r"""() => {
 const clean=x=>(x||'').replace(/\s+/g,' ').trim();
 const pick=(e,sels)=>{for(const s of sels){const n=e.querySelector(s);if(n&&clean(n.textContent))return clean(n.getAttribute('title')||n.textContent);}return '';};
 const rows=new Map();
 const nodes=[...document.querySelectorAll('[data-sku],li.gl-item')];
 for(const card of nodes){
  const sku=card.getAttribute('data-sku');if(!/^\d{5,20}$/.test(sku||''))continue;
  const title=pick(card,['[class*="goods_title"]','.p-name em','.p-name a','[class*="productName"]','[class*="title"]','[class*="_text_"]']);
  if(!title)continue;
  const price=pick(card,['.p-price i','[class*="_price_"]','.p-price','[class*="Price"]']);
  const item={sku,title,price,shop:pick(card,['.p-shop a','[class*="shop"] [class*="name"]','[class*="_name_"]']),url:`https://item.jd.com/${sku}.html`};
  if(!rows.has(sku)||title.length>rows.get(sku).title.length)rows.set(sku,item);
 }
 if(!rows.size)for(const a of document.querySelectorAll('a[href*="item.jd.com/"]')){
  const m=a.href.match(/item\.jd\.com\/(\d+)\.html/);if(!m)continue;
  const title=clean(a.getAttribute('title')||a.textContent);if(!title)continue;
  const card=a.closest('li,[class*="goodsCard"],[class*="goods_item"],[class*="product"]')||a;
  if(!rows.has(m[1]))rows.set(m[1],{sku:m[1],title,price:pick(card,['.p-price','[class*="_price_"]']),shop:pick(card,['.p-shop','[class*="shop"]']),url:`https://item.jd.com/${m[1]}.html`});
 }
 return [...rows.values()];
}"""

DETAIL_JS = r"""() => {
 const clean=x=>(x||'').replace(/\s+/g,' ').trim();
 const pick=sels=>{for(const s of sels){const n=document.querySelector(s);if(n&&clean(n.textContent))return clean(n.textContent);}return '';};
 const attrs={};for(const n of document.querySelectorAll('#parameter2 li,.parameter2 li,.Ptable-item dl,.Ptable dl')){
  const k=n.querySelector('dt'); const v=n.querySelector('dd');
  if(k&&v){attrs[clean(k.textContent)]=clean(v.textContent);continue;}
  const t=clean(n.textContent);const p=t.indexOf('：');if(p>0)attrs[t.slice(0,p)]=t.slice(p+1);
 }
 const variants=[...document.querySelectorAll('#choose-attrs .item.selected,#choose-attrs .selected,[class*="sku"] [aria-checked="true"]')].map(x=>clean(x.textContent)).filter(Boolean);
 const images=[...document.querySelectorAll('#spec-img,#spec-list img,.preview-wrap img')].map(x=>x.currentSrc||x.src).filter(x=>/^https?:/.test(x));
 for(const n of document.querySelectorAll('.attrs .item,.attribute .list .item')){const k=n.querySelector('.name,.label,.key');const v=n.querySelector('.value');if(k&&v)attrs[clean(k.textContent)]=clean(v.textContent);}
 for(const n of document.querySelectorAll('.highlight-attrs .item')){const k=n.querySelector('.desc');const v=n.querySelector('.title');if(k&&v)attrs[clean(k.textContent)]=clean(v.getAttribute('title')||v.textContent);}
 return {title:pick(['.sku-title-name','.sku-name','[class*="itemName"]','[class*="productName"]'])||document.title.replace(/【行情.*$/,''),
  price:pick(['.page-right-price .product-price--main','.summary-price .price','.p-price .price','[class*="jd-price"]','[class*="summaryPrice"]']),
  shop:pick(['.top-shop-info .top-name','.name-goodshop','.shop-name','.J-hove-wrap .name','[class*="shop"] [class*="name"]']),
  detail:{attributes:attrs,parameter_text:clean(document.querySelector('.attrs')?.parentElement?.innerText),selected_variant:variants.join('；')||[...document.querySelectorAll('.specification-item-sku--selected')].map(e=>clean(e.textContent)).join('；'),images:[...new Set(images)],
   delivery:pick(['#store-prompt','#summary-stock','.store-selector .text','[class*="delivery"]']),
   promotion:pick(['#summary-promotion','.summary-promotion','[class*="promotion"]'])}};
}"""

DOM_REVIEWS_JS = r"""() => {
 const cards=[...document.querySelectorAll('.jdc-pc-rate-card,.comment-item')];
 return cards.map(c=>{
  const text=s=>c.querySelector(s)?.textContent?.trim()||'';
  const body=text('.jdc-pc-rate-card-main-desc,.comment-con,[class*="rate-card-main-desc"]');
  const explicit=c.getAttribute('data-score');const stars=c.querySelectorAll('[class*="star-full"],[class*="star-on"]').length;
  return {platform_id:c.getAttribute('data-comment-id')||c.getAttribute('data-guid')||'',content:body,
    rating:explicit&&/^[1-5]$/.test(explicit)?Number(explicit):(stars>=1&&stars<=5?stars:null),
    review_date:text('.comment-time,.order-info .date,.date.list,[class*="commentDate"]'),
    variant:text('.order-info,.rate-card-info .info,.info'),append_text:text('.append-comment,[class*="after-comment"]'),
    reference_sku:'',images:[...c.querySelectorAll('.pic-list img,.jdc-pc-rate-card-images img')].map(x=>x.currentSrc||x.src),videos:[],source:'dom'};
 }).filter(r=>r.content);
}"""

TAGS_JS = r"""()=>{const root=document.querySelector('[class*="_rateListBox"],.jdc-page-overlay')||document;const rows=[];const seen=new Set();
for(const e of root.querySelectorAll('[class*="tag-name"],.item')){const count=e.matches('.item')?e.querySelector(':scope > .count'):e.parentElement.querySelector('[class*="tag-comment"],.count');if(!count)continue;const name=(e.matches('.item')?e.querySelector(':scope > div:not(.count)'):e)?.textContent.trim();if(!name||seen.has(name)||name.length>30)continue;seen.add(name);rows.push({name,count:count.textContent.trim()});}return rows;}"""


class BrowserCollector:
    def __init__(self, store, run_id, config, stop):
        self.store, self.run_id, self.config, self.stop = store, run_id, config, stop
        self.ctx = None
        self.page = None
        self.current_sku = None
        self.active_filter = "全部"
        self.accepting = False
        self.buffer = []
        self.network_seen = False
        self.context_title = ""
        self.request_owners = {}
        self.tag_cap = None
        self.tag_keys = set()
        self.frontier=Frontier()
        self.remote = self.config.get('browser_mode', 'cdp') == 'cdp'

    def log(self, message, level="info"):
        self.store.event(self.run_id, message, level)

    def wait(self, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if self.stop.is_set():
                raise Cancelled()
            self.page.wait_for_timeout(min(500, max(1, int((until-time.monotonic())*1000))))

    def open(self, playwright, context=None):
        if context is not None:
            try:
                page = next((p for p in context.pages if not p.is_closed() and p.evaluate("window.name")=='durian-collector'), None)
                if page is None:
                    page = context.new_page()
                page.evaluate("1")
                self.ctx, self.page = context, page
            except Exception:
                self.ctx = None
        if self.ctx is None:
            if self.remote:
                from browser_link import ensure_browser
                self.remote_browser = playwright.chromium.connect_over_cdp(ensure_browser(self.store.root))
                self.ctx = self.remote_browser.contexts[0]
                self.page = self.ctx.new_page()
                self.log('已连接专用 Edge，沿用浏览器的 UA、Cookie 与存储')
            else:
                self.ctx = playwright.chromium.launch_persistent_context(
                    str(self.store.root / "browser-profile"), channel="msedge", headless=False,
                    viewport={"width": 1360, "height": 900}, locale="zh-CN", accept_downloads=False,
                    chromium_sandbox=True,
                )
                self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(5000)
        self.page.evaluate("window.name='durian-collector'")
        self.page.on("request", self.request_started)
        self.page.on("response", self.response)

    def request_started(self, request):
        if self.accepting and self.current_sku:
            text = request.url + " " + (request.post_data or "")
            if re.search(r"comment|pc-rate|productpage", text, re.I):
                self.request_owners[request] = (self.current_sku, self.active_filter)

    def close(self, keep_open=False):
        if self.page:
            try:
                self.page.remove_listener("request", self.request_started)
                self.page.remove_listener("response", self.response)
            except Exception:
                pass
        self.request_owners.clear()
        if self.ctx and not keep_open:
            self.ctx.close()

    def response(self, response):
        owner = self.request_owners.pop(response.request, None)
        if not self.accepting or not self.current_sku:
            return
        if not owner or owner[0] != self.current_sku:
            return
        parsed_url = urlparse(response.url)
        if not (parsed_url.hostname == "jd.com" or (parsed_url.hostname or "").endswith(".jd.com")):
            return
        request_text = response.url + " " + (response.request.post_data or "")
        if not re.search(r"comment|pc-rate|productpage|wareBusiness", request_text, re.I):
            return
        try:
            if response.status != 200:
                return
            length = response.headers.get("content-length")
            if length and int(length) > 8_000_000:
                return
            body = response.text()
            if len(body) > 8_000_000:
                return
            reviews = extract_reviews(parse_payload(body))
            if reviews:
                self.network_seen = True
                for review in reviews:
                    review["_filter"] = owner[1]
                    review['_batch']=time.monotonic_ns()
                batch_id=time.monotonic_ns()
                for review in reviews:review['_batch']=batch_id
                self.buffer.extend(reviews)
        except Exception:
            # Many page responses are not JSON. A lack of usable reviews is reported by collect_product.
            return

    def blocked(self):
        url = self.page.url.lower()
        if any(x in url for x in ["passport.jd.com", "login.jd.com", "safe.jd.com", "captcha", "pf.jd.com"]):
            return True
        try:
            return self.page.locator('text=/^(请完成安全验证|请完成验证|访问过于频繁|请拖动滑块|安全验证)$/').first.is_visible()
        except Exception:
            return False

    def check_access(self):
        try:
            unavailable=self.page.get_by_text(re.compile(r'^页面正在维修中[，,]?\s*请稍后重试$')).first.is_visible()
        except Exception:
            unavailable=False
        if unavailable:
            self.diagnostic('comments-unavailable')
            raise NeedsAttention('京东网页评价暂不可用（页面提示维修中）。已保留本轮进度，稍后可恢复。')
        try:
            rate_limited = "pf.jd.com" in self.page.url.lower() or bool(re.search(
                r"访问频繁导致无法搜索|访问过于频繁|访问太频繁|请求过于频繁",
                self.page.locator('body').inner_text(timeout=3000)))
        except Exception:
            rate_limited = False
        if rate_limited:
            self.diagnostic("rate-limited")
            raise NeedsAttention("京东提示访问频繁，已停止本轮请求。请稍后再试；系统不会自动反复重试该限制。")
        if not self.blocked():
            return
        self.log("需要人工处理：请在采集浏览器中完成登录或验证。系统最多等待 10 分钟。", "attention")
        self.store.execute("UPDATE runs SET status='waiting',message=? WHERE id=?", ("请在 Edge 中完成京东登录或验证", self.run_id))
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            self.wait(2)
            if not self.blocked():
                self.store.execute("UPDATE runs SET status='running',message='人工验证后继续' WHERE id=?", (self.run_id,))
                self.wait(2)
                return
        raise NeedsAttention("等待登录或验证超时；已保留数据，可登录后恢复任务")

    def login(self):
        self.page.goto("https://www.jd.com/", wait_until="domcontentloaded", timeout=45000)
        self.store.execute("UPDATE runs SET status='waiting',message='请在打开的 Edge 中登录京东' WHERE id=?", (self.run_id,))
        self.log("已打开独立 Edge 浏览器。请点击京东登录并扫码；不需要向本系统提供密码。", "attention")
        for _ in range(300):
            self.wait(2)
            cookies = self.ctx.cookies("https://www.jd.com")
            if any(c["name"] in ["thor", "pt_key"] and c.get("value") for c in cookies) and not self.blocked():
                self.log("检测到京东会话标记，已保存独立浏览器资料。实际访问权限将在采集时再次检查。")
                try:
                    self.page.evaluate("""()=>{const note=document.createElement('div');note.textContent='榴莲观察：登录资料已保存。此窗口将保留，可回到管理页面开始采集。';Object.assign(note.style,{position:'fixed',top:'15px',left:'10%',width:'80%',padding:'20px',background:'#285b40',color:'white',zIndex:'2147483647',fontSize:'18px'});document.body.append(note);setTimeout(()=>note.remove(),15000)}""")
                except Cancelled:
                    raise
                except Exception:
                    pass
                return
        raise NeedsAttention("登录等待超时，可再次点击登录")

    def import_session(self, cookies):
        if not cookies:
            raise NeedsAttention("会话导入内容未保留，请在本地管理页重新导入")
        try:
            self.ctx.add_cookies(cookies)
        except Exception:
            raise ValueError("会话导入失败，请检查 Cookie JSON 格式或改用扫码登录") from None
        self.page.goto("https://www.jd.com/", wait_until="domcontentloaded", timeout=30000)
        self.wait(4)
        self.log(f"已将 {len(cookies)} 个京东 Cookie 写入独立浏览器资料；商品访问状态将在采集时验证")

    def discover(self):
        explicit = parse_targets(self.config["sku_input"])
        if self.config["sku_input"].strip() and not explicit:
            raise ValueError("没有识别到有效商品 ID 或 item.jd.com 商品链接")
        if explicit:
            return [{"sku": sku, "url": f"https://item.jd.com/{sku}.html"} for sku in explicit[:self.config["max_products"]]]
        result = {}
        excludes = [x.strip() for x in re.split(r"[,，\n]", self.config["exclude_words"]) if x.strip()]
        for index in range(self.config["search_pages"]):
            url = f"https://search.jd.com/Search?keyword={quote(self.config['keyword'])}&enc=utf-8&page={index*2+1}"
            self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
            self.wait(4)
            self.check_access()
            for _ in range(5):
                for item in self.page.evaluate(PRODUCTS_JS):
                    if any(word in item.get("title", "") for word in excludes):
                        continue
                    result[item["sku"]] = item
                if len(result) >= self.config["max_products"]:
                    break
                self.page.mouse.wheel(0, 800)
                self.wait(2)
            self.log(f"搜索第 {index+1} 页，累计发现 {len(result)} 件商品")
            if len(result) >= self.config["max_products"]:
                break
            self.wait(self.config["delay_seconds"])
        if not result:
            self.diagnostic("search")
            raise NeedsAttention("未提取到商品。请检查浏览器中的登录、验证和搜索页面；也可手动填写商品 ID 后采集")
        return list(result.values())[:self.config["max_products"]]

    def diagnostic(self, label):
        folder = self.store.root / "diagnostics"
        folder.mkdir(exist_ok=True)
        try:
            self.page.screenshot(path=str(folder / f"run-{self.run_id}-{label}.png"), full_page=False)
            info = self.page.evaluate("""()=>({url:location.origin+location.pathname,title:document.title,text:document.body.innerText.slice(0,12000),nodes:[...document.querySelectorAll('[class*=price],[class*=Price],[class*=sku],[class*=itemName],[class*=productName],[class*=rateList],[class*=comment],[class*=Comment]')].slice(0,80).map(e=>({tag:e.tagName,cls:e.className,text:e.textContent.trim().slice(0,400)}))})""")
            (folder / f"run-{self.run_id}-{label}.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def click_text(self, labels, scope=None):
        for label in labels:
            try:
                candidates = (scope or self.page).get_by_text(label, exact=True)
                for i in range(min(candidates.count(), 10)):
                    loc = candidates.nth(i)
                    if loc.is_visible():
                        loc.click(timeout=4000)
                        self.wait(2)
                        return True
            except Exception:
                continue
        return False

    def overlay(self):
        for selector in ['[class*="_rateListBox"]', '.jdc-page-overlay', '#comment']:
            loc = self.page.locator(selector).first
            if loc.count() and loc.is_visible():
                return loc
        return None

    def flush(self, keys, filter_name):
        limit = self.config["max_reviews"]
        batch, self.buffer = self.buffer, []
        groups={}
        for row in batch:
            if row.get('_batch') and row.get('_filter')==self.active_filter:
                groups.setdefault(row['_batch'],[]).append(row)
        for rows in groups.values():self.frontier.feed(rows)
        for review in batch:
            # Cap the total unique sample per product, across all requested filters.
            fingerprint = review.get("platform_id") or hashlib.sha256(json.dumps(
                [review.get(k) for k in ["content", "review_date", "variant", "images"]], ensure_ascii=False).encode()).hexdigest()
            if fingerprint not in keys and len(keys) >= limit:
                continue
            if self.tag_cap and fingerprint not in self.tag_keys and len(self.tag_keys)>=self.tag_cap:
                continue
            self.store.save_review(self.run_id, self.current_sku, review, review.get("_filter", filter_name))
            keys.add(fingerprint)
            self.tag_keys.add(fingerprint)

    def collect_product(self, sku):
        self.current_sku = sku
        self.buffer = []
        self.network_seen = False
        self.active_filter = "全部"
        self.accepting = True
        keys = set()
        try:
            self.page.goto(f"https://item.jd.com/{sku}.html", wait_until="domcontentloaded", timeout=45000)
            self.wait(4)
            self.check_access()
            if sku_from(self.page.url) != sku:
                self.page.goto(f"https://item.jd.com/{sku}.html", wait_until="domcontentloaded", timeout=45000)
                self.wait(3)
                self.check_access()
                if sku_from(self.page.url) != sku:
                    raise NeedsAttention("商品页未停留在预期商品，请在浏览器确认登录及访问状态")
            ready = False
            for _ in range(15):
                self.check_access()
                ready = self.page.evaluate("""()=>!![...document.querySelectorAll('.sku-title-name,.sku-name,[class*=itemName],[class*=productName],.p-price,[class*=summaryPrice]')].find(e=>e.textContent.trim().length>1)""")
                if ready:
                    break
                self.wait(2)
            if not ready:
                self.diagnostic(sku)
                raise ValueError("商品内容在 30 秒内未出现，已保存诊断；本轮未将加载页记为商品")
            self.click_text(['商品详情'])
            self.wait(2)
            item = self.page.evaluate(DETAIL_JS)
            if not item.get("title") or "登录" in item["title"]:
                raise ValueError("商品标题缺失或页面不正确")
            item["sku"] = sku
            self.store.save_product(self.run_id, item, self.config["region_note"], snapshot=True)
            warnings = []
            if not item.get("price"):
                warnings.append("详情页未识别价格；该次价格快照留空")
            self.page.mouse.wheel(0, 650)
            self.wait(1)
            opened = self.click_text(["全部评价", "查看全部评价", "商品评价"])
            if not opened:
                # A native DOM click supports labels with appended count text.
                opened = self.page.evaluate(r"""()=>{const a=[...document.querySelectorAll('a,button,span,div')].find(e=>e.children.length<3&&/^(全部评价|查看全部评价)\s*[（(\d]/.test(e.textContent.trim())&&e.getBoundingClientRect().width>0);if(a){a.click();return true;}return false;}""")
                self.wait(2)
            self.check_access()
            if not self.overlay() and not self.network_seen:
                warnings.append("未确认评价区打开")
            tags=self.page.evaluate(TAGS_JS)
            tag_limits={}
            plan=list(self.config['filters'])
            if tags:
                plan=[]
                for tag in tags:
                    digits=re.search(r'\d+(?:\.\d+)?',tag['count'])
                    count=float(digits.group()) if digits else 0
                    if '万' in tag['count']:count*=10000
                    tag_limits[tag['name']]=int(count) if 0<count<=100 and '%' not in tag['count'] else 20
                    skip=count>=1000
                    self.store.execute('INSERT OR REPLACE INTO tag_runs VALUES(?,?,?,?,?)',(self.run_id,sku,tag['name'],tag['count'],'跳过大标签' if skip else '等待'))
                    if not skip:plan.append(tag['name'])
            plan.sort(key=lambda name:0 if name=='差评' else 1 if name=='中评' else 2)
            for filter_name in plan:
                if len(keys)>=self.config['max_reviews']:
                    break
                self.tag_cap=(self.config['max_reviews'] if filter_name=='差评' else tag_limits.get(filter_name,20)) if tags else None
                self.tag_keys=set()
                if tags:self.buffer=[]
                self.network_seen=False
                self.active_filter = filter_name
                area = self.overlay()
                if tags or filter_name != "全部":
                    if not area or not self.click_text([filter_name], area):
                        warnings.append(f"未切换到{filter_name}，未将默认结果标记为该分类")
                        self.store.execute("UPDATE tag_runs SET status='未切换' WHERE run_id=? AND sku=? AND name=?",(self.run_id,sku,filter_name))
                        continue
                # Use native page controls. Never edit a request body after it has been signed.
                area = self.overlay()
                latest = self.click_text(["最新", "最新评论", "时间排序"], area) if area else False
                known=self.store.rows("SELECT DISTINCT r.platform_id FROM reviews r JOIN review_observations o ON o.sku=r.sku AND o.review_key=r.review_key WHERE r.sku=? AND o.filter_name IN (?,?) AND r.platform_id<>''",(sku,filter_name,'页面点击:'+filter_name))
                self.frontier=Frontier([r['platform_id'] for r in known],enabled=self.config.get('incremental',True) and latest and filter_name!='追评')
                if not latest:
                    warnings.append(f"{filter_name}：未确认最新排序，按页面实际顺序采集")
                self.flush(keys, "页面默认" if filter_name == "全部" else f"页面点击:{filter_name}")
                stalled = 0
                for _ in range(self.config["scroll_rounds"]):
                    self.check_access()
                    if self.frontier.reached:
                        break
                    if len(keys) >= self.config["max_reviews"] or (self.tag_cap and len(self.tag_keys)>=self.tag_cap):
                        break
                    before = len(self.tag_keys)
                    if not self.network_seen:
                        self.buffer.extend(self.page.evaluate(DOM_REVIEWS_JS))
                    self.flush(keys, "页面默认" if filter_name == "全部" else f"页面点击:{filter_name}")
                    moved = self.page.evaluate(r"""()=>{const root=document.querySelector('[class*="_rateListBox"],.jdc-page-overlay');if(root){const c=root.querySelector('[class*="rateListContainer"],[data-viewport-type]')||root;const targets=[c,...c.querySelectorAll('div')].filter(e=>e.scrollHeight>e.clientHeight+100&&/(auto|scroll)/.test(getComputedStyle(e).overflowY));if(targets.length){targets[0].scrollTop+=700;return true;}}window.scrollBy(0,700);return false;}""")
                    if not moved and stalled >= 1:
                        # Old-style evaluation areas can have an explicit native next-page control.
                        area = self.overlay()
                        if area:
                            self.click_text(["下一页", "下一页 >"], area)
                    self.wait(2)
                    self.flush(keys, "页面默认" if filter_name == "全部" else f"页面点击:{filter_name}")
                    stalled = stalled + 1 if len(self.tag_keys) == before else 0
                    if stalled >= 5:
                        break
                self.store.execute("UPDATE tag_runs SET status=? WHERE run_id=? AND sku=? AND name=?",('达到分类上限' if self.tag_cap and len(self.tag_keys)>=self.tag_cap else '页面停止新增，未确认全量',self.run_id,sku,filter_name))
                if self.frontier.reached:
                    self.store.execute("UPDATE tag_runs SET status='增量检查到已知评价，停止翻页' WHERE run_id=? AND sku=? AND name=?",(self.run_id,sku,filter_name))
            self.store.execute("UPDATE tag_runs SET status='达到总上限，未采' WHERE run_id=? AND sku=? AND status='等待'",(self.run_id,sku))
            self.flush(keys, "页面默认")
            if not keys:
                warnings.append("未取得评价；不能据此判断该商品没有评价")
                self.diagnostic(sku)
            if not self.network_seen and keys:
                warnings.append("使用页面文本回退，部分评分/ID/日期可能缺失")
            note = f"本轮 {len(keys)} 条；达到样本上限" if len(keys) >= self.config["max_reviews"] else f"本轮 {len(keys)} 条；页面停止新增或达到滚动上限，不代表全量"
            if warnings:
                note += "；" + "；".join(dict.fromkeys(warnings))
            self.log(f"SKU {sku}：{note}", "warn" if warnings else "info")
            return "partial" if warnings else "done", note
        finally:
            # Already received records survive cancellation and page failures.
            self.flush(keys, "页面实际返回")
            self.accepting = False
            self.current_sku = None
