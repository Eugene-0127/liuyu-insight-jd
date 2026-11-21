"""Import exports from the user's normal browser; no network requests."""
import hashlib
import json
from storage import now
from collector import sku_from


def import_dataset(store, dataset):
    if not isinstance(dataset, dict) or not isinstance(dataset.get("reviews"), list):
        raise ValueError("请选择油猴脚本导出的完整 JSON，需包含 reviews 数组")
    products = dataset.get("products", [])
    if not isinstance(products, list):
        raise ValueError("products 应为数组")
    prepared = []
    product_map = {}
    for p in products:
        if not isinstance(p, dict) or not sku_from(p.get("sku", "")):
            raise ValueError("商品编号无效")
        product_map[sku_from(p["sku"])] = p
    for row in dataset["reviews"]:
        if not isinstance(row, dict) or not sku_from(row.get("sku", "")):
            raise ValueError("评价缺少有效商品编号")
        score = row.get("score")
        try:
            score = int(score) if str(score) in ["1", "2", "3", "4", "5"] else None
        except (ValueError, TypeError):
            score = None
        content = row.get("content", "")
        if not isinstance(content, str):
            raise ValueError("评价正文必须为文本")
        sku = sku_from(row["sku"])
        product_map.setdefault(sku, {})
        media = lambda values: [v for v in values if isinstance(v, str) and v.startswith(("https://", "http://"))] if isinstance(values, list) else []
        append = row.get("append") or []
        if isinstance(append, str):
            append_text = append
        elif isinstance(append, list):
            append_text = "\n".join(str(a.get("content") or a.get("text") or "") for a in append if isinstance(a, dict))
        else:
            append_text = ""
        prepared.append((sku, {"platform_id": str(row.get("id") or ""), "content": content,
                         "rating": score, "review_date": str(row.get("time") or ""),
                         "variant": str(row.get("spec") or ""), "append_text": append_text,
                         "images": media(row.get("images")), "videos": media(row.get("videos")),
                         "reference_sku": str(row.get("referenceId") or ""), "source": "normal_browser_import"}))
    if not product_map:
        raise ValueError("文件中没有商品或评价")
    digest = hashlib.sha256(json.dumps(dataset, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    with store.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS imports(digest TEXT PRIMARY KEY,run_id INTEGER)")
        prior = db.execute("SELECT run_id FROM imports WHERE digest=?", (digest,)).fetchone()
    if prior:
        return {"run_id": prior[0], "duplicate": True}
    run = store.create_run("import")
    try:
        store.execute("UPDATE runs SET status='running' WHERE id=?", (run,))
        for sku, p in product_map.items():
            store.save_product(run, {"sku": sku, "title": str(p.get("title") or ""), "price": p.get("price"),
                               "shop": str(p.get("shop") or ""), "detail": {"source": "normal_browser_import",
                               "captured_at": p.get("capturedAt"), "exported_at": dataset.get("exportedAt"),
                               "parameters": p.get("parameters"), "media": p.get("media"), "detail_text": p.get("detailText")}}, "从正常浏览器导入，时间与价格口径见详情", False)
            store.execute("INSERT INTO run_items(run_id,sku,status) VALUES(?,?,'done')", (run, sku))
        for sku, review in prepared:
            store.save_review(run, sku, review, "浏览器导入")
        message = f"导入 {len(product_map)} 件商品、{len(prepared)} 条评价（数据库自动去重）"
        store.execute("UPDATE runs SET status='done',message=?,finished_at=? WHERE id=?", (message, now(), run))
        store.execute("INSERT INTO imports VALUES(?,?)", (digest, run))
        store.event(run, message)
        return {"run_id": run, "duplicate": False, "products": len(product_map), "reviews": len(prepared)}
    except Exception:
        store.execute("UPDATE runs SET status='failed',message='导入失败，请检查文件' WHERE id=?", (run,))
        raise
