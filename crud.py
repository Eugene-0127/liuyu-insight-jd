"""Local record management, reversible deletion and durable manual corrections."""
import json
import uuid
from datetime import datetime
from storage import now
from collector import sku_from


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS record_flags(entity TEXT,sku TEXT,record_key TEXT,deleted_at TEXT,PRIMARY KEY(entity,sku,record_key));
    CREATE TABLE IF NOT EXISTS record_overrides(entity TEXT,sku TEXT,record_key TEXT,fields TEXT,PRIMARY KEY(entity,sku,record_key));
    CREATE TABLE IF NOT EXISTS edit_log(id INTEGER PRIMARY KEY,entity TEXT,sku TEXT,record_key TEXT,action TEXT,before_data TEXT,after_data TEXT,created_at TEXT);
    CREATE VIEW IF NOT EXISTS active_products AS SELECT p.* FROM products p WHERE NOT EXISTS(SELECT 1 FROM record_flags f WHERE f.entity='products' AND f.sku=p.sku AND f.deleted_at IS NOT NULL);
    CREATE VIEW IF NOT EXISTS active_reviews AS SELECT r.* FROM reviews r WHERE NOT EXISTS(SELECT 1 FROM record_flags f WHERE f.sku=r.sku AND f.deleted_at IS NOT NULL AND (f.entity='products' OR (f.entity='reviews' AND f.record_key=r.review_key)));
    ''')


def query(store, kind, args):
    if kind not in ['products','reviews','trash','tags','snapshots']:
        raise ValueError('记录类型无效')
    page=max(1,int(args.get('page',['1'])[0]))
    size=min(100,max(1,int(args.get('size',['20'])[0])))
    q=str(args.get('q',[''])[0]).strip()[:300]
    if kind=='snapshots':
        where='1=1'
        params=[]
        if q:
            term='%'+q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
            where+=" AND (sku LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\' OR json_extract(CASE WHEN json_valid(detail) THEN detail ELSE '{}' END,'$.selected_variant') LIKE ? ESCAPE '\\')"
            params.extend([term]*3)
        bounds={}
        for key in ['start','end']:
            value=args.get(key,[''])[0].strip()
            if value:
                try:
                    bounds[key]=datetime.fromisoformat(value).astimezone()
                except ValueError:
                    raise ValueError('采集时间格式无效') from None
                where+=' AND julianday(captured_at) '+('>=' if key=='start' else '<=')+' julianday(?)'
                params.append(bounds[key].isoformat())
        if len(bounds)==2 and bounds['start']>bounds['end']:
            raise ValueError('开始时间不能晚于结束时间')
        with store.connect() as db:
            total=db.execute('SELECT count(*) FROM snapshots WHERE '+where,params).fetchone()[0]
            rows=[dict(r) for r in db.execute('SELECT * FROM snapshots WHERE '+where+' ORDER BY julianday(captured_at) DESC,id DESC LIMIT ? OFFSET ?',params+[size,(page-1)*size])]
        return {'rows':rows,'total':total,'page':page,'size':size}
    if kind=='tags':
        with store.connect() as db:
            where="t.run_id=(SELECT max(t2.run_id) FROM tag_runs t2 WHERE t2.sku=t.sku) AND EXISTS(SELECT 1 FROM active_products p WHERE p.sku=t.sku)"
            params=[]
            if q:
                where+=' AND (t.sku LIKE ? OR t.name LIKE ?)';params.extend(['%'+q+'%']*2)
            sku=args.get('sku',[''])[0]
            if sku:where+=' AND t.sku=?';params.append(sku)
            total=db.execute('SELECT count(*) FROM tag_runs t WHERE '+where,params).fetchone()[0]
            sql="SELECT t.*,(SELECT count(DISTINCT o.review_key) FROM review_observations o JOIN active_reviews r ON r.sku=o.sku AND r.review_key=o.review_key WHERE o.run_id=t.run_id AND o.sku=t.sku AND o.filter_name=t.name) collected,(SELECT count(DISTINCT o.review_key) FROM review_observations o JOIN active_reviews r ON r.sku=o.sku AND r.review_key=o.review_key WHERE o.sku=t.sku AND o.filter_name=t.name) local_count FROM tag_runs t WHERE "+where+' ORDER BY t.sku,t.name LIMIT ? OFFSET ?'
            rows=[dict(r) for r in db.execute(sql,params+[size,(page-1)*size])]
            return {'rows':rows,'total':total,'page':page,'size':size}
    with store.connect() as db:
        if kind=='trash':
            table='record_flags'
            where='deleted_at IS NOT NULL'
            params=[]
            columns='*'
        else:
            table='active_'+kind
            where='1=1'
            params=[]
            columns='*'
            if kind=='reviews':columns="*,(SELECT group_concat(DISTINCT filter_name) FROM review_observations o WHERE o.sku=active_reviews.sku AND o.review_key=active_reviews.review_key AND o.filter_name NOT LIKE '页面%') tags"
            if q:
                cols=['sku','title','shop'] if kind=='products' else ['sku','content','append_text','variant','platform_id']
                where+=' AND ('+' OR '.join(c+' LIKE ? ESCAPE \'\\\'' for c in cols)+')'
                term='%'+q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
                params.extend([term]*len(cols))
                if kind=='reviews':
                    where=where[:-1]+" OR EXISTS(SELECT 1 FROM review_observations o WHERE o.sku=active_reviews.sku AND o.review_key=active_reviews.review_key AND o.filter_name LIKE ? ESCAPE '\\'))"
                    params.append(term)
            rating=args.get('rating',[''])[0]
            if kind=='reviews' and rating:
                if rating not in ['1','2','3','4','5']:raise ValueError('评分无效')
                where+=' AND rating=?';params.append(int(rating))
            if kind=='reviews':
                appended=args.get('append',[''])[0]
                body="trim(coalesce(append_text,''))<>''"
                tagged="EXISTS(SELECT 1 FROM review_observations o WHERE o.sku=active_reviews.sku AND o.review_key=active_reviews.review_key AND o.filter_name IN ('追评','页面点击:追评'))"
                if appended=='has':where+=' AND ('+body+' OR '+tagged+')'
                elif appended=='body':where+=' AND '+body
                elif appended=='tag':where+=' AND '+tagged
                elif appended=='none':where+=' AND NOT ('+body+' OR '+tagged+')'
                elif appended:raise ValueError('追评筛选无效')
                tag=args.get('tag',[''])[0].strip()[:100]
                if tag:
                    term='%'+tag.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
                    where+=" AND EXISTS(SELECT 1 FROM review_observations o WHERE o.sku=active_reviews.sku AND o.review_key=active_reviews.review_key AND o.filter_name NOT LIKE '页面默认%' AND o.filter_name LIKE ? ESCAPE '\\')"
                    params.append(term)
        total=db.execute(f'SELECT count(*) FROM {table} WHERE {where}',params).fetchone()[0]
        rows=[dict(r) for r in db.execute(f'SELECT {columns} FROM {table} WHERE {where} ORDER BY '+('deleted_at' if kind=='trash' else 'last_seen')+' DESC LIMIT ? OFFSET ?',params+[size,(page-1)*size])]
        options=[r[0] for r in db.execute("SELECT DISTINCT o.filter_name FROM review_observations o JOIN active_reviews r ON r.sku=o.sku AND r.review_key=o.review_key WHERE o.filter_name NOT LIKE '页面%' ORDER BY o.filter_name")] if kind=='reviews' else []
    return {'rows':rows,'total':total,'page':page,'size':size,'available_tags':options}


def mutate(store, data):
    kind=data.get('kind')
    if kind not in ['products','reviews']:raise ValueError('类型无效')
    action=data.get('action')
    if action not in ['create','update','delete','restore']:raise ValueError('操作无效')
    sku=sku_from(data.get('sku',''))
    if not sku:raise ValueError('请填写有效的京东 SKU 或商品链接')
    key=str(data.get('review_key') or '') if kind=='reviews' else ''
    fields=data.get('fields',{})
    if not isinstance(fields,dict):raise ValueError('字段格式无效')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        where='sku=?'+(' AND review_key=?' if kind=='reviews' else '')
        params=[sku]+([key] if kind=='reviews' else [])
        before=db.execute(f'SELECT * FROM {kind} WHERE {where}',params).fetchone()
        if action=='create':
            stamp=now()
            if kind=='products':
                if before:raise ValueError('该 SKU 已存在；若已删除请先从回收站恢复')
                db.execute('INSERT INTO products VALUES(?,?,?,?,?,?,?,?)',(sku,'','','https://item.jd.com/'+sku+'.html',None,stamp,stamp,'{}'))
            else:
                if not db.execute('SELECT sku FROM active_products WHERE sku=?',(sku,)).fetchone():raise ValueError('请先新增或恢复商品')
                key='manual:'+uuid.uuid4().hex
                params=[sku,key]
                db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(sku,key,'','manual','',None,'','','','[]','[]','',stamp,stamp,'manual'))
        elif not before:
            raise ValueError('记录不存在')
        if action in ['delete','restore']:
            db.execute('INSERT INTO record_flags VALUES(?,?,?,?) ON CONFLICT(entity,sku,record_key) DO UPDATE SET deleted_at=excluded.deleted_at',(kind,sku,key,now() if action=='delete' else None))
        else:
            allowed=['title','shop','price','attributes'] if kind=='products' else ['content','rating','review_date','variant','append_text']
            values={k:fields[k] for k in allowed if k in fields}
            for k,v in list(values.items()):
                if k not in ['rating','attributes']:
                    if v is None:values[k]=''
                    elif not isinstance(v,str) or len(v)>20000:raise ValueError('文本过长或格式无效')
            if 'rating' in values:
                v=values['rating']
                if v in [None,'']:values['rating']=None
                elif str(v) in ['1','2','3','4','5']:values['rating']=int(v)
                else:raise ValueError('评分应为 1 至 5，未知请留空')
            if kind=='products' and 'attributes' in values:
                attrs=values.pop('attributes')
                if not isinstance(attrs,dict) or any(not isinstance(k,str) or not isinstance(v,str) or len(v)>2000 for k,v in attrs.items()):raise ValueError('参数格式无效')
                current=db.execute('SELECT detail FROM products WHERE sku=?',(sku,)).fetchone()
                detail=json.loads(current['detail'] or '{}')
                detail['attributes']={**detail.get('attributes',{}),**attrs}
                values['detail']=json.dumps(detail,ensure_ascii=False)
            if values:
                db.execute(f'UPDATE {kind} SET '+','.join(k+'=?' for k in values)+' WHERE '+where,list(values.values())+params)
            prior=db.execute('SELECT fields FROM record_overrides WHERE entity=? AND sku=? AND record_key=?',(kind,sku,key)).fetchone()
            override={**(json.loads(prior[0]) if prior else {}),**values}
            db.execute('INSERT OR REPLACE INTO record_overrides VALUES(?,?,?,?)',(kind,sku,key,json.dumps(override,ensure_ascii=False)))
        after=db.execute(f'SELECT * FROM {kind} WHERE {where}',params).fetchone()
        db.execute('INSERT INTO edit_log(entity,sku,record_key,action,before_data,after_data,created_at) VALUES(?,?,?,?,?,?,?)',(kind,sku,key,action,json.dumps(dict(before),ensure_ascii=False) if before else None,json.dumps(dict(after),ensure_ascii=False),now()))
    return {'ok':True,'sku':sku,'review_key':key}
