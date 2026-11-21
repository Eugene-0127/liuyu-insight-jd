"""Local durable storage. Each operation uses its own SQLite connection."""
import csv
import hashlib
import io
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


DEFAULTS = {
    "incremental": True,
    "browser_mode": "cdp",
    "keyword": "榴莲", "sku_input": "", "max_products": 20,
    "search_pages": 2, "max_reviews": 200, "scroll_rounds": 40,
    "delay_seconds": 10, "interval_hours": 24,
    "schedule_enabled": False, "filters": ["全部"],
    "exclude_words": "榴莲饼,榴莲酥,榴莲糖,榴莲蛋糕,榴莲披萨,榴莲冰淇淋,榴莲干",
    "region_note": "以采集浏览器当前配送地区及页面显示价格为准",
}


def validate_settings(data):
    out = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in data:
            out[key] = data[key]
    for key, lo, hi in [("max_products", 1, 500), ("search_pages", 1, 30),
                        ("max_reviews", 1, 5000), ("scroll_rounds", 1, 500),
                        ("delay_seconds", 5, 600), ("interval_hours", 1, 720)]:
        if isinstance(out[key], bool):
            raise ValueError(f"{key} 必须为整数")
        n = int(out[key])
        if not lo <= n <= hi:
            raise ValueError(f"{key} 应在 {lo}–{hi} 之间")
        out[key] = n
    for key in ["keyword", "sku_input", "exclude_words", "region_note"]:
        out[key] = str(out[key]).strip()[:20000 if key == "sku_input" else 1000]
    if not out["keyword"] and not out["sku_input"]:
        raise ValueError("请填写搜索词或商品 ID / 链接")
    if not isinstance(out["incremental"], bool):
        raise ValueError("采集方式格式错误")
    if not isinstance(out["schedule_enabled"], bool):
        raise ValueError("定时开关格式错误")
    if not isinstance(out["filters"], list) or not out["filters"]:
        raise ValueError("至少选择一种评价分类")
    if any(x not in ["全部", "好评", "中评", "差评", "追评"] for x in out["filters"]):
        raise ValueError("评价分类无效")
    out["filters"] = list(dict.fromkeys(out["filters"]))
    return out


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "durian.sqlite3"
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS products(
              sku TEXT PRIMARY KEY,title TEXT NOT NULL DEFAULT '',shop TEXT NOT NULL DEFAULT '',
              url TEXT NOT NULL,price TEXT,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,
              detail TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS snapshots(
              id INTEGER PRIMARY KEY,run_id INTEGER,sku TEXT NOT NULL,captured_at TEXT NOT NULL,
              title TEXT,price TEXT,shop TEXT,region_note TEXT,detail TEXT);
            CREATE INDEX IF NOT EXISTS snapshot_sku ON snapshots(sku,captured_at);
            CREATE TABLE IF NOT EXISTS reviews(
              sku TEXT NOT NULL,review_key TEXT NOT NULL,platform_id TEXT,identity_quality TEXT,
              content TEXT NOT NULL,rating INTEGER,review_date TEXT,variant TEXT,append_text TEXT,
              images TEXT,videos TEXT,reference_sku TEXT,first_seen TEXT,last_seen TEXT,
              source TEXT NOT NULL,PRIMARY KEY(sku,review_key));
            CREATE TABLE IF NOT EXISTS review_observations(
              run_id INTEGER,sku TEXT,review_key TEXT,filter_name TEXT,captured_at TEXT,
              PRIMARY KEY(run_id,sku,review_key,filter_name));
            CREATE TABLE IF NOT EXISTS runs(
              id INTEGER PRIMARY KEY,kind TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT,
              started_at TEXT,finished_at TEXT,config TEXT,message TEXT,new_reviews INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS run_items(
              run_id INTEGER,sku TEXT,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER DEFAULT 0,
              message TEXT DEFAULT '',review_count INTEGER DEFAULT 0,new_reviews INTEGER DEFAULT 0,
              PRIMARY KEY(run_id,sku));
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY,run_id INTEGER,created_at TEXT,level TEXT,message TEXT);
            CREATE TABLE IF NOT EXISTS tag_runs(run_id INTEGER,sku TEXT,name TEXT,display_count TEXT,status TEXT,PRIMARY KEY(run_id,sku,name));
            """)
            from crud import initialize
            initialize(db)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).lastrowid

    def settings(self):
        rows = self.rows("SELECT value FROM settings WHERE key='config'")
        return {**DEFAULTS, **(json.loads(rows[0]["value"]) if rows else {})}

    def save_settings(self, data):
        config = validate_settings(data)
        self.execute("INSERT OR REPLACE INTO settings VALUES('config',?)", (json.dumps(config, ensure_ascii=False),))
        return config

    def event(self, run_id, message, level="info"):
        self.execute("INSERT INTO events(run_id,created_at,level,message) VALUES(?,?,?,?)", (run_id, now(), level, str(message)[:3000]))

    def create_run(self, kind="crawl", config=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT id FROM runs WHERE status IN ('queued','running','waiting') LIMIT 1").fetchone()
            if active:
                raise ValueError(f"任务 #{active['id']} 尚未结束，请等待或停止当前任务")
            return db.execute("INSERT INTO runs(kind,status,created_at,config,message) VALUES(?,'queued',?,?,?)",
                              (kind, now(), json.dumps(config or self.settings(), ensure_ascii=False), "等待执行")).lastrowid

    def save_product(self, run_id, item, region_note, snapshot=False):
        stamp = now()
        with self.connect() as db:
            old = db.execute("SELECT * FROM products WHERE sku=?", (item["sku"],)).fetchone()
            merged = dict(old) if old else {}
            for key in ["title", "shop", "url", "price"]:
                if item.get(key) not in (None, ""):
                    merged[key] = item[key]
            merged.setdefault("url", f"https://item.jd.com/{item['sku']}.html")
            detail = item.get("detail", {})
            if not detail and old:
                detail = json.loads(old["detail"] or "{}")
            db.execute("""INSERT INTO products VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(sku) DO UPDATE SET
                       title=excluded.title,shop=excluded.shop,url=excluded.url,price=excluded.price,
                       last_seen=excluded.last_seen,detail=excluded.detail""",
                       (item["sku"], merged.get("title", ""), merged.get("shop", ""), merged["url"],
                        merged.get("price"), stamp, stamp, json.dumps(detail, ensure_ascii=False)))
            if snapshot:
                # Record the observed price, including missing values. Do not reuse an old price as a new observation.
                db.execute("INSERT INTO snapshots(run_id,sku,captured_at,title,price,shop,region_note,detail) VALUES(?,?,?,?,?,?,?,?)",
                           (run_id, item["sku"], stamp, merged.get("title"), item.get("price"),
                            merged.get("shop"), region_note, json.dumps(detail, ensure_ascii=False)))
            override=db.execute("SELECT fields FROM record_overrides WHERE entity='products' AND sku=? AND record_key=''",(item['sku'],)).fetchone()
            if override:
                fields={k:v for k,v in json.loads(override[0]).items() if k in ['title','shop','price','detail']}
                if fields:db.execute('UPDATE products SET '+','.join(k+'=?' for k in fields)+' WHERE sku=?',list(fields.values())+[item['sku']])

    def save_review(self, run_id, sku, review, filter_name):
        stamp = now()
        platform_id = str(review.get("platform_id") or "")
        if platform_id:
            key = "id:" + platform_id
            quality = "platform_id"
        else:
            # Anonymous missing-ID reviews cannot be perfectly distinguished; mark that explicitly.
            identity = [review.get(k) or "" for k in ["content", "review_date", "variant", "reference_sku"]]
            identity.append(review.get("images") or [])
            key = "hash:" + hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            quality = "fallback_hash"
        with self.connect() as db:
            old = db.execute("SELECT * FROM reviews WHERE sku=? AND review_key=?", (sku, key)).fetchone()
            values = dict(review)
            if old:
                for k in ["content", "rating", "review_date", "variant", "append_text", "reference_sku"]:
                    if values.get(k) in (None, ""):
                        values[k] = old[k]
                for k in ["images", "videos"]:
                    values[k] = list(dict.fromkeys(json.loads(old[k] or "[]") + (values.get(k) or [])))
            override=db.execute("SELECT fields FROM record_overrides WHERE entity='reviews' AND sku=? AND record_key=?",(sku,key)).fetchone()
            if override:values.update(json.loads(override[0]))
            db.execute("""INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sku,review_key)
            DO UPDATE SET content=excluded.content,rating=excluded.rating,review_date=excluded.review_date,
            variant=excluded.variant,append_text=excluded.append_text,images=excluded.images,videos=excluded.videos,
            reference_sku=excluded.reference_sku,last_seen=excluded.last_seen""",
                       (sku, key, platform_id, quality, values.get("content", ""), values.get("rating"),
                        values.get("review_date", ""), values.get("variant", ""), values.get("append_text", ""),
                        json.dumps(values.get("images") or [], ensure_ascii=False),
                        json.dumps(values.get("videos") or [], ensure_ascii=False), values.get("reference_sku", ""),
                        stamp, stamp, values.get("source", "network")))
            observed = db.execute("INSERT OR IGNORE INTO review_observations VALUES(?,?,?,?,?)", (run_id, sku, key, filter_name, stamp)).rowcount
            if not old:
                db.execute("UPDATE runs SET new_reviews=new_reviews+1 WHERE id=?", (run_id,))
                db.execute("UPDATE run_items SET new_reviews=new_reviews+1 WHERE run_id=? AND sku=?", (run_id, sku))
            if observed:
                db.execute("""UPDATE run_items SET review_count=(SELECT count(DISTINCT review_key)
                           FROM review_observations WHERE run_id=? AND sku=?) WHERE run_id=? AND sku=?""", (run_id, sku, run_id, sku))
        return not bool(old), key

    def export_csv(self, kind):
        queries = {
            "products": "SELECT * FROM active_products ORDER BY last_seen DESC",
            "reviews": "SELECT * FROM active_reviews ORDER BY first_seen DESC",
            "snapshots": "SELECT * FROM snapshots ORDER BY id DESC",
            "tasks": "SELECT * FROM run_items ORDER BY run_id DESC,sku",
            "tags": "SELECT t.*,(SELECT count(DISTINCT review_key) FROM review_observations o WHERE o.run_id=t.run_id AND o.sku=t.sku AND o.filter_name=t.name) collected,(SELECT count(DISTINCT review_key) FROM review_observations o WHERE o.sku=t.sku AND o.filter_name=t.name) local_count FROM tag_runs t ORDER BY run_id DESC,sku,name",
        }
        if kind not in queries:
            raise ValueError("导出类型无效")
        stream = io.StringIO(newline="")
        stream.write("\ufeff")
        writer = csv.writer(stream)
        with self.connect() as db:
            cur = db.execute(queries[kind])
            rows = cur.fetchall()
            extras = []
            attributes = []
            if kind == 'products':
                for row in rows:
                    detail = json.loads(row['detail'] or '{}')
                    attrs = detail.get('attributes') or detail.get('parameters') or {}
                    attributes.append(attrs)
                    for key in attrs:
                        if key not in extras:
                            extras.append(key)
            writer.writerow([d[0] for d in cur.description] + extras)
            for index,row in enumerate(rows):
                safe = []
                for cell in list(row) + ([attributes[index].get(k,'') for k in extras] if kind == 'products' else []):
                    value = "" if cell is None else str(cell)
                    if re.match(r"^[\s]*[=+@-]", value):
                        value = "'" + value
                    safe.append(value)
                writer.writerow(safe)
        return stream.getvalue().encode("utf-8")
