"""JD durian local control panel. Only binds to loopback."""
import argparse
import json
import logging
import secrets
import threading
import time
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from collector import BrowserCollector, Cancelled, NeedsAttention, parse_cookies
from storage import Store, now
from importer import import_dataset
from crud import query, mutate

ROOT = Path(__file__).resolve().parent


class Coordinator:
    def __init__(self, store, start_worker=True):
        self.store = store
        self.cancel = threading.Event()
        self.shutdown = threading.Event()
        self.current = None
        self.lock = threading.Lock()
        self.thread = None
        self.pending_imports = {}
        self.playwright = None
        self.browser_context = None
        self.browser_remote = False
        if start_worker:
            self.thread = threading.Thread(target=self.loop, daemon=True, name="collector-worker")
            self.thread.start()

    def enqueue(self, kind="crawl", cookies=None):
        with self.lock:
            run_id = self.store.create_run(kind)
            if cookies:
                self.pending_imports[run_id] = cookies
            self.store.execute("UPDATE runs SET status='cancelled',message=message || '；已被新的手动任务替代' WHERE id<? AND status='needs_attention' AND (kind='login' OR ?='crawl')", (run_id, kind))
            self.store.event(run_id, "任务已排队" if kind == "crawl" else "正在准备独立京东登录窗口")
            return run_id

    def stop(self):
        with self.lock:
            self.cancel.set()
            self.store.execute("UPDATE runs SET status='cancelled',message='已取消排队任务' WHERE status='queued'")
            if self.current:
                self.store.event(self.current, "收到停止请求，将保存已收到的数据并保留浏览器供检查", "warn")

    def resume(self):
        with self.lock, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT id FROM runs WHERE status IN ('queued','running','waiting')").fetchone():
                raise ValueError("已有任务正在执行")
            row = db.execute("SELECT id FROM runs WHERE kind='crawl' AND status IN ('needs_attention','cancelled','failed','partial') ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                raise ValueError("没有可恢复的采集任务")
            if not db.execute("SELECT sku FROM run_items WHERE run_id=? AND status!='done' LIMIT 1", (row["id"],)).fetchone():
                count = db.execute("SELECT count(*) FROM run_items WHERE run_id=?", (row["id"],)).fetchone()[0]
                if count:
                    raise ValueError("该任务的商品已处理完毕，可启动新一轮采集")
            db.execute("UPDATE runs SET status='queued',message='等待恢复' WHERE id=?", (row["id"],))
            db.execute("UPDATE run_items SET status='pending' WHERE run_id=? AND status!='done'", (row["id"],))
            return row["id"]

    def schedule(self):
        settings = self.store.settings()
        if not settings["schedule_enabled"]:
            return
        with self.store.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='next_schedule'").fetchone()
        if not row:
            self.set_next(settings["interval_hours"])
            return
        if datetime.now().astimezone() < datetime.fromisoformat(row["value"]):
            return
        with self.store.connect() as db:
            unresolved = db.execute("SELECT id FROM runs WHERE status IN ('queued','running','waiting','needs_attention') LIMIT 1").fetchone()
        if unresolved:
            return
        self.enqueue()
        self.set_next(settings["interval_hours"])

    def set_next(self, hours):
        stamp = (datetime.now().astimezone() + timedelta(hours=hours)).isoformat(timespec="seconds")
        self.store.execute("INSERT OR REPLACE INTO settings VALUES('next_schedule',?)", (stamp,))

    def loop(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            self.playwright = playwright
            try:
                while not self.shutdown.is_set():
                    try:
                        self.schedule()
                        with self.store.connect() as db:
                            row = db.execute("SELECT * FROM runs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                        if row:
                            self.execute(dict(row))
                        elif self.browser_context:
                            # Pump events while idle so manual window closure is observed.
                            try:
                                pages = self.browser_context.pages
                                if pages:
                                    pages[0].wait_for_timeout(100)
                            except Exception:
                                self.browser_context = None
                    except Exception:
                        logging.exception("Worker loop")
                    self.shutdown.wait(1)
            finally:
                if self.browser_context:
                    try:
                        if self.browser_remote:
                            self.browser_context.browser.close()
                        else:
                            self.browser_context.close()
                    except Exception:
                        pass
                self.browser_context = None
                self.playwright = None

    def execute(self, run):
        with self.lock:
            with self.store.connect() as db:
                claimed = db.execute("UPDATE runs SET status='running',started_at=?,message='启动采集浏览器' WHERE id=? AND status='queued'", (now(), run["id"])).rowcount
            if not claimed:
                return
            self.current = run["id"]
            self.cancel.clear()
        config = json.loads(run["config"])
        collector = BrowserCollector(self.store, run["id"], config, self.cancel)
        status, message = "done", "任务完成"
        try:
            collector.open(self.playwright, context=self.browser_context)
            self.browser_context = collector.ctx
            self.browser_remote = collector.remote
            try:
                if run["kind"] == "session":
                    cookies = self.pending_imports.pop(run["id"], None)
                    collector.import_session(cookies)
                    message = "京东会话已导入；浏览器保留，可试采验证商品访问权限"
                elif run["kind"] == "login":
                    collector.login()
                    message = "登录资料已保存；浏览器保留"
                else:
                    self.crawl(collector, run, config)
                    with self.store.connect() as db:
                        counts = {r["status"]: r["n"] for r in db.execute("SELECT status,count(*) n FROM run_items WHERE run_id=? GROUP BY status", (run["id"],))}
                    status = "partial" if any(counts.get(x) for x in ["partial", "failed"]) else "done"
                    message = "；".join(f"{k}: {v}" for k, v in counts.items())
            finally:
                collector.close(keep_open=True)
        except Cancelled:
            status, message = "cancelled", "已停止；已收到的数据已保存，可恢复任务"
        except NeedsAttention as exc:
            status, message = "needs_attention", str(exc)
        except Exception as exc:
            status, message = "failed", str(exc)[:1500]
            logging.exception("Run failed")
        finally:
            self.pending_imports.pop(run["id"], None)
            self.store.execute("UPDATE run_items SET status='pending' WHERE run_id=? AND status='running'", (run["id"],))
            self.store.execute("UPDATE runs SET status=?,message=?,finished_at=? WHERE id=?", (status, message, now(), run["id"]))
            self.store.event(run["id"], message, "info" if status == "done" else "warn")
            with self.lock:
                self.current = None

    def crawl(self, collector, run, config):
        with self.store.connect() as db:
            count = db.execute("SELECT count(*) FROM run_items WHERE run_id=?", (run["id"],)).fetchone()[0]
        if not count:
            products = collector.discover()
            for item in products:
                self.store.save_product(run["id"], item, config["region_note"])
                self.store.execute("INSERT OR IGNORE INTO run_items(run_id,sku,status) VALUES(?,?,'pending')", (run["id"], item["sku"]))
        with self.store.connect() as db:
            items = [dict(r) for r in db.execute("SELECT * FROM run_items WHERE run_id=? AND status='pending' ORDER BY sku", (run["id"],))]
        for index, item in enumerate(items):
            if self.cancel.is_set():
                raise Cancelled()
            sku = item["sku"]
            self.store.execute("UPDATE runs SET message=? WHERE id=?", (f"处理 {index+1}/{len(items)}：SKU {sku}", run["id"]))
            for attempt in range(2):
                self.store.execute("UPDATE run_items SET status='running',attempts=attempts+1 WHERE run_id=? AND sku=?", (run["id"], sku))
                try:
                    status, note = collector.collect_product(sku)
                    self.store.execute("UPDATE run_items SET status=?,message=? WHERE run_id=? AND sku=?", (status, note, run["id"], sku))
                    break
                except (Cancelled, NeedsAttention):
                    raise
                except Exception as exc:
                    note = str(exc)[:2000]
                    self.store.event(run["id"], f"SKU {sku} 第 {attempt+1} 次失败：{note}", "warn")
                    self.store.execute("UPDATE run_items SET status='failed',message=? WHERE run_id=? AND sku=?", (note, run["id"], sku))
                    if attempt == 0:
                        collector.wait(config["delay_seconds"])
            if index < len(items)-1:
                collector.wait(config["delay_seconds"])


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, coordinator):
        self.store, self.coordinator = store, coordinator
        self.token = secrets.token_urlsafe(32)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if self.path != "/api/state":
            logging.info(fmt, *args)

    def send(self, data, status=200, content_type="application/json; charset=utf-8", headers=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        elif isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def local_request(self):
        expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host") not in expected:
            self.send({"error": "仅接受本机访问"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{h}" for h in expected}:
            self.send({"error": "拒绝跨站请求"}, 403)
            return False
        return True

    def do_GET(self):
        if not self.local_request():
            return
        path = urlparse(self.path)
        try:
            if path.path == "/":
                return self.send((ROOT / "dashboard.html").read_bytes(), content_type="text/html; charset=utf-8")
            if path.path == "/api/health":
                return self.send({"app": "jd-durian-local", "version": "1.0.0"})
            if path.path == "/api/state":
                with self.server.store.connect() as db:
                    counts = {key: db.execute(f"SELECT count(*) FROM {('active_'+key) if key!='snapshots' else key}").fetchone()[0] for key in ["products", "reviews", "snapshots"]}
                    runs = [dict(r) for r in db.execute("SELECT id,kind,status,created_at,message,new_reviews FROM runs ORDER BY id DESC LIMIT 15")]
                    events = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 60")]
                    products = [dict(r) for r in db.execute("SELECT p.*,(SELECT count(*) FROM active_reviews r WHERE r.sku=p.sku) review_count FROM active_products p ORDER BY last_seen DESC LIMIT 100")]
                    reviews = [dict(r) for r in db.execute("SELECT sku,platform_id,identity_quality,content,rating,review_date,variant,append_text,first_seen,source FROM active_reviews ORDER BY first_seen DESC LIMIT 100")]
                    snapshots = [dict(r) for r in db.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 100")]
                    nxt = db.execute("SELECT value FROM settings WHERE key='next_schedule'").fetchone()
                    tags=[dict(r) for r in db.execute('SELECT t.*,(SELECT count(DISTINCT review_key) FROM review_observations o WHERE o.run_id=t.run_id AND o.sku=t.sku AND o.filter_name=t.name) collected,(SELECT count(DISTINCT review_key) FROM review_observations o WHERE o.sku=t.sku AND o.filter_name=t.name) local_count FROM tag_runs t ORDER BY run_id DESC LIMIT 100')]
                return self.send({"token": self.server.token, "counts": counts, "runs": runs, "events": events,
                                  "products": products, "reviews": reviews, "snapshots": snapshots, "settings": self.server.store.settings(),
                                  "next_schedule": nxt[0] if nxt else None, "server_time": now(),"tags":tags})
            if path.path == "/api/export":
                kind = parse_qs(path.query).get("kind", ["reviews"])[0]
                return self.send(self.server.store.export_csv(kind), content_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="jd-durian-{kind}.csv"'})
            if path.path == '/api/records':
                args=parse_qs(path.query)
                return self.send(query(self.server.store,args.get('kind',['products'])[0],args))
            return self.send({"error": "不存在的地址"}, 404)
        except Exception as exc:
            logging.exception("GET")
            self.send({"error": str(exc)}, 500)

    def do_POST(self):
        if not self.local_request():
            return
        if not secrets.compare_digest(self.headers.get("X-Local-Token", ""), self.server.token):
            return self.send({"error": "请刷新本地管理页面"}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= (20_000_000 if urlparse(self.path).path == '/api/import' else 100_000):
                raise ValueError("请求过大")
            data = json.loads(self.rfile.read(length) or b"{}")
            path = urlparse(self.path).path
            if path == '/api/record':
                with self.server.coordinator.lock:
                    return self.send(mutate(self.server.store,data))
            if path == "/api/settings":
                old = self.server.store.settings()
                settings = self.server.store.save_settings(data)
                if settings["schedule_enabled"] and (not old["schedule_enabled"] or old["interval_hours"] != settings["interval_hours"]):
                    self.server.coordinator.set_next(settings["interval_hours"])
                return self.send({"ok": True})
            if path == "/api/import":
                with self.server.coordinator.lock:
                    return self.send(import_dataset(self.server.store, data))
            if path in ["/api/start", "/api/login"]:
                run_id = self.server.coordinator.enqueue("login" if path.endswith("login") else "crawl")
                return self.send({"ok": True, "run_id": run_id})
            if path == "/api/session":
                cookies = parse_cookies(data.get("cookie_text", ""))
                run_id = self.server.coordinator.enqueue("session", cookies=cookies)
                return self.send({"ok": True, "run_id": run_id})
            if path == "/api/stop":
                self.server.coordinator.stop()
                return self.send({"ok": True})
            if path == "/api/resume":
                return self.send({"ok": True, "run_id": self.server.coordinator.resume()})
            if path == "/api/shutdown":
                self.server.coordinator.stop()
                self.send({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            return self.send({"error": "不存在的操作"}, 404)
        except (ValueError, TypeError) as exc:
            return self.send({"error": str(exc)}, 400)
        except Exception as exc:
            logging.exception("POST")
            return self.send({"error": str(exc)}, 500)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--no-worker", action="store_true", help="仅供本地界面验证")
    args = parser.parse_args()
    store = Store(args.data_dir)
    logging.basicConfig(filename=str(store.root / "system.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    coordinator = Coordinator(store, start_worker=False)
    server = AppServer(("127.0.0.1", args.port), store, coordinator)
    # Bind before starting background work; a second server must not start a collector.
    store.execute("UPDATE runs SET status='needs_attention',message='上次进程中断，已保存数据；请恢复任务' WHERE status IN ('running','waiting')")
    store.execute("UPDATE run_items SET status='pending' WHERE status='running'")
    if not args.no_worker:
        coordinator.thread = threading.Thread(target=coordinator.loop, daemon=True)
        coordinator.thread.start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        coordinator.cancel.set()
        coordinator.shutdown.set()
        server.server_close()
        if coordinator.thread:
            coordinator.thread.join(timeout=55)


if __name__ == "__main__":
    main()
